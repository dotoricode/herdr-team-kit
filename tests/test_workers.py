import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from teamkit import workers
from teamkit.config import binding
from teamkit.resources import ResourceLedger
from teamkit.store import Store
from teamkit.tickets import TicketStore


class WorkerTests(unittest.TestCase):
    def setUp(self):
        for target in ('subprocess.Popen', 'subprocess.run', 'subprocess.call',
                       'subprocess.check_call', 'subprocess.check_output',
                       'teamkit.core_runtime._call', 'teamkit.workers._call',
                       'teamkit.workers.providers.identity',
                       'teamkit.workers.CoreRuntime.launch',
                       'teamkit.workers.CoreRuntime.accept',
                       'teamkit.workers.CoreRuntime._native'):
            guard = patch(target, side_effect=AssertionError('unmocked external boundary: ' + target))
            guard.start()
            self.addCleanup(guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state.sqlite'
        self.config_path = self.root / 'config.json'
        profiles = {}
        for provider in ('codex', 'claude'):
            profiles[provider] = {}
            for account in ('team', 'personal'):
                home = self.root / (provider + '-' + account)
                home.mkdir()
                if provider == 'codex':
                    (home / 'config.toml').write_text('[mcp_servers.atlassian]\nurl="https://example.invalid/mcp"\n')
                else:
                    (home / '.claude.json').write_text(json.dumps({'mcpServers': {'atlassian': {'url': 'https://example.invalid/mcp'}}}))
                profiles[provider][account] = {'home': str(home), 'model': 'profile-model', 'effort': 'low',
                                               'identity': {'email': 'person@example.invalid'}}
        self.config = {'version': 1, 'runtime_backend': 'core', 'profiles': profiles,
                       'teams': {'alpha': {'root': str(self.root), 'roles': {
                           'leader': {'provider': 'claude', 'model': 'leader-model', 'effort': 'medium'},
                           'implementer': {'provider': 'claude', 'model': 'implementation-model', 'effort': 'medium'},
                           'reviewer': {'provider': 'codex', 'model': 'review-model', 'effort': 'high'}}}},
                       'quota_ttl_seconds': 120, 'operations': {'enabled': True}}
        self.config_path.write_text(json.dumps(self.config))
        self.queue = TicketStore(self.state)
        self.addCleanup(self.queue.close)
        self.store = Store(self.state)
        self.addCleanup(self.store.close)
        for provider in profiles:
            for account, profile in profiles[provider].items():
                self.store.quota(provider + '/' + account, {'observed_at': time.time(),
                    'binding': binding(profile), 'source': 'codex-native-quota' if provider == 'codex' else 'claude-statusline-response',
                    'five_hour': {'used': 10, 'resets_at': time.time() + 300},
                    'weekly': {'used': 10, 'resets_at': time.time() + 300}})
        self.approval = self.root / 'approval.txt'
        self.approval.write_text('User authorizes this fixture investigation.')

    def ticket(self, role='investigator', kind='investigation', product='alpha', **policy):
        return self.queue.enqueue({'id': 'T-' + role + '-' + product, 'product': product, 'role': role, 'kind': kind,
            'goal': 'Inspect fixture files', 'completion': ['A report exists'], 'checks': ['report'],
            'scope': {'paths': [str(self.root)], 'actions': ['read', 'write'], 'data': ['fixture']},
            'profiles': ['codex/team', 'codex/personal', 'claude/team', 'claude/personal'],
            'resources': [{'kind': 'path', 'name': str(self.root), 'mode': 'read'}],
            'approval': {'state': 'approved', 'reference': str(self.approval)}, **policy}, 'ticket-' + role + '-' + product, user_approved=True)

    def reserve(self, ticket):
        with patch.object(workers.providers, 'identity'):
            plan = workers.plans(self.config, self.state, ticket)[0]
        return self.queue.reserve(ticket['id'], plan, None,
                                  {'observed_at': time.time(), 'complete': True, 'terminals': []})

    def native(self, executor, turn=0):
        return {'address': 't100', 'state': 'live', 'name': 'tk-' + executor['owner'][:20],
                'turn': turn, 'foreground': {'name': 'claude', 'pid': 123},
                'labels': {'teamkit.owner': executor['owner'], 'teamkit.task': executor['task'],
                           'teamkit.name': 'tk-' + executor['owner'][:20]},
                'status': {'app': 'claude-code', 'state': 'idle'}}

    def fake_launch(self, task, spec, name, on_created, *, keep=True):
        self.assertIs(keep,False)
        on_created({'backend': 'core', 'name': name, 'terminal': 't100', 'pane': 't100',
                    'session_cwd': task['root'], 'managed_workspace': False, 'ephemeral': True})
        row = self.queue.attempt(self.attempt['id'])
        self.assertEqual(row['executor']['terminal'], 't100')
        self.assertIsNone(row['executor']['pid'])
        on_created({'backend': 'core', 'name': name, 'terminal': 't100', 'pane': 't100',
                    'session_cwd': task['root'], 'managed_workspace': False, 'ephemeral': True, 'native_pid': 123})
        return {'backend': 'core', 'name': name, 'terminal': 't100', 'pane': 't100',
                'session_cwd': task['root'], 'native_pid': 123, 'ephemeral': True}

    def test_plans_preserve_role_model_and_effort_instead_of_using_warm_leader_model(self):
        for role, kind, model, effort in [('implementer', 'investigation', 'implementation-model', 'medium'),
                                          ('reviewer', 'review', 'review-model', 'high')]:
            ticket = self.ticket(role, kind)
            with patch.object(workers.providers, 'identity'):
                plans = workers.plans(self.config, self.state, ticket)
            self.assertTrue(plans)
            self.assertEqual((plans[0]['model'], plans[0]['effort']), (model, effort))
            self.assertTrue(plans[0]['new'])
            self.assertIsNone(plans[0]['pid'])
            self.assertIsNone(plans[0]['terminal'])

    def test_user_fixed_model_survives_flexible_provider_preference(self):
        self.config['execution_preference'] = {'provider_order': ['claude', 'codex'],
                                                'weekend_account_first': True}
        decision = {'provider': 'codex', 'model': 'user-fixed-model', 'effort': 'high', 'failure_count': 0}
        ticket = self.ticket(user_decision=decision)
        with patch.object(workers.providers, 'identity'):
            plans = workers.plans(self.config, self.state, ticket)
        self.assertTrue(plans)
        self.assertEqual({(p['provider'], p['model'], p['effort']) for p in plans},
                         {('codex', 'user-fixed-model', 'high')})

    def test_failure_evidence_blocks_automatic_new_worker_after_high_effort(self):
        ticket = self.ticket(failures=[{'kind': 'reasoning', 'cause': 'same-root-cause', 'effort': 'high'}])
        with patch.object(workers.providers, 'identity') as identity:
            self.assertEqual(workers.plans(self.config, self.state, ticket), [])
        identity.assert_not_called()

    def test_designation_cannot_replace_user_approved_provider_but_does_not_empty_the_plans(self):
        ticket = self.ticket(user_decision={'provider': 'codex', 'model': 'user-fixed-model',
                                           'effort': 'high', 'failure_count': 0},
                             execution_constraint={'provider': 'claude', 'account': 'personal'})
        with patch.object(workers.providers, 'identity'):
            plans = workers.plans(self.config, self.state, ticket)
        self.assertTrue(plans)
        self.assertEqual({(p['provider'], p['model']) for p in plans}, {('codex', 'user-fixed-model')})

    def test_new_user_decision_only_resets_failure_evidence_already_reviewed(self):
        ticket = self.ticket(user_decision={'provider': 'codex', 'model': 'user-fixed-model',
                                           'effort': 'high', 'failure_count': 1},
                             failures=[{'kind': 'reasoning', 'cause': 'reviewed-cause', 'effort': 'high'}])
        with patch.object(workers.providers, 'identity'):
            plans = workers.plans(self.config, self.state, ticket)
        self.assertTrue(plans)
        self.assertEqual({(p['provider'], p['model'], p['effort']) for p in plans},
                         {('codex', 'user-fixed-model', 'high')})

    def test_policy_fields_are_forwarded_and_fenced_against_stale_caller(self):
        policy = {'user_decision': {'provider': 'codex', 'model': 'user-fixed-model',
                                   'effort': 'high', 'failure_count': 0},
                  'failures': [], 'execution_constraint': {'provider': 'codex', 'account': 'personal'},
                  'classification': {'kind': 'general', 'risks': ['security']}}
        ticket = self.ticket(**policy)
        task = workers._policy_task(self.config, ticket)
        for field, value in policy.items():
            self.assertEqual(task.get(field), value)
            changed = {**ticket, field: None}
            with self.subTest(field=field), patch.object(workers.providers, 'identity') as identity:
                with self.assertRaisesRegex(ValueError, 'contract changed'):
                    workers.plans(self.config, self.state, changed)
                identity.assert_not_called()

    def test_expired_quota_or_missing_mcp_configuration_cannot_plan_a_worker(self):
        ticket = self.ticket()
        for account in ('team', 'personal'):
            value = self.store.quota('claude/' + account)
            value['observed_at'] = time.time() - 121
            self.store.quota('claude/' + account, value)
        with patch.object(workers.providers, 'identity'):
            self.assertEqual(workers.plans(self.config, self.state, ticket), [])
        for account in ('team', 'personal'):
            value = self.store.quota('claude/' + account)
            value['observed_at'] = time.time()
            self.store.quota('claude/' + account, value)
            (Path(self.config['profiles']['claude'][account]['home']) / '.claude.json').unlink()
        with patch.object(workers.providers, 'identity') as identity:
            self.assertEqual(workers.plans(self.config, self.state, ticket), [])
            identity.assert_not_called()

    def test_launch_binds_existing_reservation_and_does_not_send_a_model_prompt(self):
        ticket = self.ticket()
        self.attempt = self.reserve(ticket)
        executor = self.attempt['executor']
        with patch.object(workers.providers, 'identity'), \
             patch.object(workers, 'require_managed', return_value='t1'), \
             patch.object(workers.CoreRuntime, 'launch', side_effect=self.fake_launch) as launch, \
             patch.object(workers.CoreRuntime, '_native', return_value=self.native(executor)), \
             patch.object(workers, '_call', return_value={'complete': True, 'text': '❯ Try "write a test"\n──────'}), \
             patch.object(workers.CoreRuntime, 'accept') as accept:
            ready = workers.launch(self.config_path, self.state, ticket, self.attempt)
        launch.assert_called_once()
        self.assertIs(launch.call_args.kwargs.get('keep'), False)
        accept.assert_not_called()
        self.assertEqual(ready['pid'], 123)
        self.assertEqual(ready['empty_prompt'], 'Try "write a test"')
        reservation = ResourceLedger._rows(self.queue.db)
        self.assertEqual(len(reservation), 1)
        self.assertEqual(reservation[0]['attempt'], self.attempt['id'])
        self.assertEqual(reservation[0]['session']['pid'], 123)
        actor = self.store.get(ready['task'])
        self.assertTrue(actor['operation_actor'])
        self.assertEqual(actor['operation_ticket'], ticket['id'])
        self.assertEqual(actor['runtime']['resource_attempt'], self.attempt['id'])
        self.assertIn('model_startup_seconds', self.queue.attempt(self.attempt['id']))

    def test_native_creation_timeout_never_creates_a_second_actor_on_retry(self):
        ticket = self.ticket()
        self.attempt = self.reserve(ticket)
        with patch.object(workers.providers, 'identity'), \
             patch.object(workers, 'require_managed', return_value='t1'), \
             patch.object(workers.CoreRuntime, 'launch', side_effect=TimeoutError('unknown')) as create:
            with self.assertRaises(TimeoutError):
                workers.launch(self.config_path, self.state, ticket, self.attempt)
            with self.assertRaises(ValueError):
                workers.launch(self.config_path, self.state, ticket, self.attempt)
        self.assertEqual(create.call_count, 1)
        self.assertEqual(len(self.store.list()), 1)

    def test_split_worker_launches_in_the_separate_directory(self):
        from teamkit.tickets import execution_resources
        ticket = self.ticket('reviewer', 'review')
        self.attempt = self.reserve(ticket)
        executor = self.attempt['executor']
        target = self.root/'isolated'
        target.mkdir()
        split = dict(source_root=str(self.root), worktree=str(target),
                     resources=[dict(kind='path', name=str(target), mode='read')])
        self.attempt['worktree_split'] = split
        self.queue._save_attempt(self.attempt)
        lease = ResourceLedger._rows(self.queue.db)[0]
        lease['resources'] = execution_resources(ticket, self.attempt)
        ResourceLedger._write(self.queue.db, lease)
        native = self.native(executor)
        native['foreground']['cwd'] = str(target)
        with patch.object(workers.providers, 'identity'), \
             patch.object(workers, 'require_managed', return_value='t1'), \
             patch.object(workers.CoreRuntime, 'launch', side_effect=self.fake_launch) as launch, \
             patch.object(workers.CoreRuntime, '_native', return_value=native), \
             patch.object(workers, '_call', return_value={'complete': True, 'text': '❯ \n──────'}):
            ready = workers.launch(self.config_path, self.state, ticket, self.attempt)
        self.assertEqual(str(target), launch.call_args.args[0]['root'])
        self.assertEqual(str(target), ready['execution_root'])

    def test_split_worker_wrong_native_cwd_is_not_ready_or_relaunched(self):
        from teamkit.tickets import execution_resources
        ticket = self.ticket('reviewer', 'review')
        self.attempt = self.reserve(ticket)
        executor = self.attempt['executor']
        target = self.root/'isolated'
        target.mkdir()
        self.attempt['worktree_split'] = dict(source_root=str(self.root), worktree=str(target),
            resources=[dict(kind='path', name=str(target), mode='read')])
        self.queue._save_attempt(self.attempt)
        lease = ResourceLedger._rows(self.queue.db)[0]
        lease['resources'] = execution_resources(ticket, self.attempt)
        ResourceLedger._write(self.queue.db, lease)
        native = self.native(executor)
        native['foreground']['cwd'] = str(self.root)
        with patch.object(workers.providers, 'identity'), \
             patch.object(workers, 'require_managed', return_value='t1'), \
             patch.object(workers.CoreRuntime, 'launch', side_effect=self.fake_launch) as launch, \
             patch.object(workers.CoreRuntime, '_native', return_value=native):
            with self.assertRaisesRegex(ValueError, 'did not start in the split worktree'):
                workers.launch(self.config_path, self.state, ticket, self.attempt)
            with self.assertRaisesRegex(ValueError, 'already attempted'):
                workers.launch(self.config_path, self.state, ticket, self.attempt)
        self.assertEqual(1, launch.call_count)

    def test_changed_approval_blocks_launch_before_native_creation(self):
        ticket = self.ticket()
        self.attempt = self.reserve(ticket)
        self.approval.write_text('Approval changed')
        with patch.object(workers, 'require_managed', return_value='t1'), \
             patch.object(workers.CoreRuntime, 'launch') as create:
            with self.assertRaises(ValueError):
                workers.launch(self.config_path, self.state, ticket, self.attempt)
        create.assert_not_called()

    def test_stale_caller_ticket_cannot_select_a_different_role(self):
        ticket = self.ticket()
        with patch.object(workers.providers, 'identity') as identity:
            with self.assertRaises(ValueError):
                workers.plans(self.config, self.state, dict(ticket, role='reviewer', kind='review'))
            identity.assert_not_called()

    def test_fresh_hint_is_not_learned_after_attachment_or_a_model_turn(self):
        ticket = self.ticket()
        with patch.object(workers.providers, 'identity'):
            executor = workers.plans(self.config, self.state, ticket)[0]
        executor.update(terminal='t100', runtime={'name': 'tk-' + executor['owner'][:20]})
        for altered in [self.native(executor, turn=1),
                        {**self.native(executor), 'labels': {'herdr.space': 'space-one'}}]:
            with self.subTest(native=altered), \
                 patch.object(workers, '_call', return_value={'complete': True, 'text': '❯ Try "write a test"\n─────'}):
                with self.assertRaises(ValueError):
                    workers._hint(executor, altered)

    def test_identity_failure_excludes_profile_without_changing_role_policy(self):
        ticket = self.ticket()
        with patch.object(workers.providers, 'identity', side_effect=workers.NativeError('unavailable')):
            self.assertEqual(workers.plans(self.config, self.state, ticket), [])

    def test_common_department_worker_requires_explicit_role_alias_and_keeps_its_model(self):
        self.config['operations']['departments'] = {'docs': {'root': str(self.root), 'roles': {
            'implementer': {'provider': 'claude', 'model': 'document-model', 'effort': 'medium'}}}}
        ticket = self.ticket('documenter', 'document', product='docs')
        with patch.object(workers.providers, 'identity'):
            self.assertEqual(workers.plans(self.config, self.state, ticket), [])
            self.config['operations']['worker_roles'] = {'documenter': 'implementer'}
            plan = workers.plans(self.config, self.state, ticket)[0]
        self.assertEqual((plan['model'], plan['effort']), ('document-model', 'medium'))
        self.assertEqual(plan['product'], 'docs')
        self.assertEqual(plan['roles'], ['documenter'])
        self.assertEqual(set(self.config['teams']), {'alpha'})

    def test_common_department_launch_uses_existing_single_reservation(self):
        self.config['require_design_approval'] = True
        self.config['operations'].update(departments={'docs': {'root': str(self.root), 'roles': {
            'implementer': {'provider': 'claude', 'model': 'document-model', 'effort': 'medium'}}}},
            worker_roles={'documenter': 'implementer'})
        self.config_path.write_text(json.dumps(self.config))
        ticket = self.ticket('documenter', 'document', product='docs')
        self.attempt = self.reserve(ticket)
        executor = self.attempt['executor']
        with patch.object(workers.providers, 'identity'), \
             patch.object(workers, 'require_managed', return_value='t1'), \
             patch.object(workers.CoreRuntime, 'launch', side_effect=self.fake_launch), \
             patch.object(workers.CoreRuntime, '_native', return_value=self.native(executor)), \
             patch.object(workers, '_call', return_value={'complete': True, 'text': '❯ Try "write a test"\n──────'}):
            ready = workers.launch(self.config_path, self.state, ticket, self.attempt)
        self.assertEqual(self.store.get(ready['task'])['team'], 'docs')
        self.assertEqual(len(ResourceLedger._rows(self.queue.db)), 1)

    def test_product_implementation_still_requires_existing_workflow_approval(self):
        self.config['require_design_approval'] = True
        self.config_path.write_text(json.dumps(self.config))
        ticket = self.ticket('implementer', 'investigation')
        self.attempt = self.reserve(ticket)
        with patch.object(workers.providers, 'identity'), \
             patch.object(workers, 'require_managed', return_value='t1'), \
             patch.object(workers.CoreRuntime, 'launch') as launch:
            with self.assertRaisesRegex(ValueError, 'linked approved work'):
                workers.launch(self.config_path, self.state, ticket, self.attempt)
        launch.assert_not_called()

    def test_common_department_review_does_not_skip_workflow_approval(self):
        self.config['require_design_approval'] = True
        self.config['operations']['departments'] = {'quality': {'root': str(self.root), 'roles': {
            'reviewer': {'provider': 'codex', 'model': 'review-model', 'effort': 'high'}}}}
        self.config_path.write_text(json.dumps(self.config))
        ticket = self.ticket('reviewer', 'review', product='quality')
        self.attempt = self.reserve(ticket)
        with patch.object(workers.providers, 'identity'), \
             patch.object(workers, 'require_managed', return_value='t1'), \
             patch.object(workers.CoreRuntime, 'launch') as launch:
            with self.assertRaisesRegex(ValueError, 'linked approved work'):
                workers.launch(self.config_path, self.state, ticket, self.attempt)
        launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
