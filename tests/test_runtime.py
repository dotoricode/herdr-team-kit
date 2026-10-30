import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from teamkit.policy import choose_account, normalize_quota, context_action
from teamkit.store import Store
from teamkit.runtime import Herdr, RuntimeError as KitError, launch_spec


NOW = 2000000000


def quota(used=20, weekly=30, at=NOW):
    return {'observed_at': at, 'five_hour': {'used': used, 'resets_at': NOW + 1000},
            'weekly': {'used': weekly, 'resets_at': NOW + 2000}}


class PolicyTests(unittest.TestCase):
    def test_team_preferred_and_threshold_inclusive(self):
        self.assertEqual(choose_account(quota(89.9), quota(), NOW), 'team')
        self.assertEqual(choose_account(quota(90), quota(), NOW), 'personal')
        self.assertEqual(choose_account(quota(0, 90), quota(), NOW), 'personal')

    def test_exhausted_unknown_stale_future_or_missing_window_wait(self):
        for bad in (None, quota(at=NOW-121), quota(at=NOW+1), {'observed_at': NOW}):
            self.assertIsNone(choose_account(bad, None, NOW))
        self.assertIsNone(choose_account(quota(90), quota(90), NOW))

    def test_expired_window_is_unknown_not_zero(self):
        q = quota()
        q['five_hour']['resets_at'] = NOW
        self.assertIsNone(choose_account(q, None, NOW))

    def test_invalid_numbers_fail_closed(self):
        for val in (True, -1, 101, float('nan'), float('inf'), '20'):
            self.assertIsNone(choose_account(quota(val), None, NOW))

    def test_active_account_is_not_moved_when_team_recovers(self):
        self.assertEqual(choose_account(quota(), quota(95), NOW, active='personal'), 'personal')

    def test_claude_and_codex_windows_normalize(self):
        c = {'rate_limits': {'five_hour': {'used_percentage': 42, 'resets_at': NOW+9},
                             'seven_day': {'used_percentage': 51, 'resets_at': NOW+99}}}
        x = {'rateLimits': {'primary': {'usedPercent': 42, 'windowDurationMins': 300, 'resetsAt': NOW+9},
                            'secondary': {'usedPercent': 51, 'windowDurationMins': 10080, 'resetsAt': NOW+99}}}
        self.assertEqual(normalize_quota('claude', c, NOW), normalize_quota('codex', x, NOW))
        x['rateLimits']['primary']['windowDurationMins'] = 15
        self.assertNotIn('five_hour', normalize_quota('codex', x, NOW))

    def test_context_unknown_and_safe_boundary(self):
        self.assertEqual(context_action(None, True), 'unknown')
        self.assertEqual(context_action(60, False), 'prepare')
        self.assertEqual(context_action(75, False), 'prepare')
        self.assertEqual(context_action(70, True), 'handoff')
        self.assertEqual(context_action(59, True), 'keep')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(Path(self.tmp.name)/'state.sqlite')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def task(self, name='T1', team='alpha', role='implementer'):
        return self.db.create(name, team, role, '/workspace/'+team, 'a'*40, 'fix behavior')

    def test_task_identity_and_duplicate_create(self):
        self.task()
        self.assertEqual(self.task()['state'], 'queued')
        with self.assertRaises(ValueError):
            self.db.create('T1', 'beta', 'implementer', '/workspace', 'b'*40, 'different')

    def test_claim_atomic_across_connections(self):
        self.task()
        other = Store(self.db.path)
        try:
            self.db.claim('T1', 'owner1', 'codex', 'team', 'model-a', 'medium')
            with self.assertRaises(ValueError):
                other.claim('T1', 'owner2', 'codex', 'team', 'model-a', 'medium')
        finally:
            other.close()

    def test_team_writer_and_global_limits(self):
        self.task()
        self.task('T2')
        self.db.claim('T1', 'o1', 'codex', 'team', 'm', 'medium')
        with self.assertRaises(ValueError):
            self.db.claim('T2', 'o2', 'codex', 'team', 'm', 'medium')
        for i in range(2,4):
            self.task('X'+str(i), 'team'+str(i))
            self.db.claim('X'+str(i), 'o'+str(i), 'codex', 'team', 'm', 'medium')
        self.task('X4', 'team4', 'reviewer')
        with self.assertRaises(ValueError):
            self.db.claim('X4', 'o4', 'codex', 'team', 'm', 'medium')

    def test_failed_delivery_remains_owned_and_not_retryable(self):
        self.task()
        self.db.claim('T1', 'o1', 'codex', 'team', 'm', 'medium')
        self.db.update('T1', 'o1', 'uncertain', {'reason':'timeout'})
        with self.assertRaises(ValueError):
            self.db.claim('T1', 'o2', 'codex', 'personal', 'm', 'medium')
        with self.assertRaises(ValueError):
            self.db.handoff('T1', 'o1', {'next':'retry'}, stopped=False)

    def test_handoff_releases_ownership_only_with_checkpoint(self):
        self.task()
        self.db.claim('T1', 'o1', 'codex', 'team', 'm', 'medium')
        with self.assertRaises(ValueError):
            self.db.handoff('T1', 'o1', {}, stopped=True)
        self.db.handoff('T1', 'o1', {'head':'a'*40, 'dirty':False, 'verification':[], 'next':'continue'}, stopped=True)
        self.db.claim('T1', 'o2', 'codex', 'personal', 'm', 'medium')
        self.assertEqual(self.db.get('T1')['account'], 'personal')

    def test_completion_requires_owner_head_and_evidence(self):
        self.task()
        self.db.claim('T1', 'o1', 'codex', 'team', 'm', 'medium')
        with self.assertRaises(ValueError):
            self.db.complete('T1', 'wrong', 'b'*40, ['test passed'])
        with self.assertRaises(ValueError):
            self.db.complete('T1', 'o1', 'b'*40, [])
        self.db.complete('T1', 'o1', 'b'*40, ['python -m unittest: pass'])
        self.assertEqual(self.db.get('T1')['state'], 'awaiting_review')

    def test_events_and_private_permissions(self):
        self.task()
        self.assertEqual(self.db.events('T1')[0]['event'], 'created')
        self.assertEqual(self.db.path.stat().st_mode & 0o777, 0o600)


class HerdrTests(unittest.TestCase):
    def test_outside_herdr_no_control_call(self):
        with patch.dict(os.environ, {'HERDR_ENV':''}), patch('subprocess.run') as run:
            with self.assertRaises(KitError):
                Herdr().call('agent', 'list')
            run.assert_not_called()

    def test_nonzero_or_malformed_response_never_success(self):
        with patch.dict(os.environ, {'HERDR_ENV':'1'}), patch('subprocess.run') as run:
            run.return_value.returncode = 1
            run.return_value.stderr = 'failure'
            with self.assertRaises(KitError):
                Herdr().call('agent', 'get', 'alpha')
            run.return_value.returncode = 0
            run.return_value.stdout = 'not json'
            with self.assertRaises(KitError):
                Herdr().call('agent', 'get', 'alpha')

    def test_launch_has_isolated_auth_and_explicit_model(self):
        profile = {'home':'/private/codex-team', 'model':'model-a', 'effort':'medium',
                   'identity': {'email':'team@example.invalid'}}
        spec = launch_spec('codex', profile, None)
        self.assertEqual(spec['env']['CODEX_HOME'], '/private/codex-team')
        self.assertEqual(spec['env']['OPENAI_API_KEY'], '')
        self.assertIn('cli_auth_credentials_store="file"', spec['args'])
        self.assertIn('model-a', spec['args'])
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', spec['args'])
        resumed = launch_spec('codex', profile, 'session-id')
        self.assertEqual(resumed['args'][:2], ['resume', 'session-id'])

    def test_missing_identity_or_relative_home_rejected(self):
        for p in ({'home':'/private/home','model':'m','effort':'medium'},
                  {'home':'relative','model':'m','effort':'medium','identity':{'email':'x'}}):
            with self.assertRaises(ValueError):
                launch_spec('codex', p, None)


if __name__ == '__main__':
    unittest.main()

class LeaderTests(unittest.TestCase):
    def test_idle_leaders_do_not_consume_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Store(Path(tmp)/'state.sqlite')
            try:
                for i in range(4):
                    task = 'L'+str(i)
                    db.create(task, 'team'+str(i), 'leader', '/workspace/'+str(i), 'a'*40, 'coordinate')
                    db.claim(task, task, 'codex', 'team', 'm', 'medium')
                    db.update(task, task, 'launching', {})
                    db.update(task, task, 'submitted', {})
                    db.update(task, task, 'running', {})
                    db.park(task, task)
                for i in range(3): db.wake('L'+str(i), 'L'+str(i))
                with self.assertRaises(ValueError): db.wake('L3', 'L3')
            finally:
                db.close()

    def test_shared_root_writers_from_different_teams_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Store(Path(tmp)/'state.sqlite')
            try:
                for name in ('A','B'):
                    db.create(name, name, 'implementer', '/shared/repo', 'a'*40, 'edit')
                db.claim('A', 'o1', 'codex', 'team', 'm', 'medium')
                with self.assertRaises(ValueError): db.claim('B', 'o2', 'codex', 'team', 'm', 'medium')
            finally:
                db.close()
