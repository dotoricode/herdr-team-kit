import copy
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from teamkit import ui_jobs
from teamkit.config import binding
from teamkit.resources import ResourceLedger
from teamkit.store import Store
from teamkit.tickets import TicketStore, digest, file_digest


class UIJobsTests(unittest.TestCase):
    """Real ledger/files/hash/admission; all process, HC and lab calls are guarded."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state.sqlite'
        self.config_path = self.root / 'config.json'
        self.queue = TicketStore(self.state)
        self.addCleanup(self.queue.close)
        profiles = {account: dict(home=str(self.root / account), model='fixture', effort='low',
                    identity={'email': 'fixture@example.invalid'}) for account in ('team', 'personal')}
        worker = dict(task='UI-ACTOR', owner='ui-owner', provider='codex', account='personal',
                      model='fixture', effort='low', profile_binding=binding(profiles['personal']),
                      runtime=dict(backend='core', name='ui-native', terminal='t1', native_pid=42))
        self.config = dict(version=1, profiles={'codex': profiles}, teams={'example': {
            'root': str(self.root), 'roles': {'leader': {'provider': 'codex', 'model': 'fixture'}}}},
            runtime_backend='core', quota_ttl_seconds=120,
            operations={'enabled': True, 'ui_lab': {'enabled': True, 'command': ['fixture-ui-lab'],
                'worker': worker, 'evidence_roots': [str(self.root / 'evidence')]}})
        self.registry = dict(terminal='t1', provider='codex', account='personal',
                             profile_home=profiles['personal']['home'], ui_host='test-macbook')
        self.jobs = {}
        self.calls = []
        self.native_state = 'idle'
        self.screen = {'complete': True, 'text': '› '}
        self.crash = False
        self.timeout = False
        self.observation = dict(observed_at=time.time(), complete=True, terminals=[])
        self.approval = self.root / 'approval.txt'
        self.approval.write_text('User approves a bounded fixture UI check.')
        self.spec = dict(id='UI-TICKET', product='example', role='ui', kind='ui',
            goal='Check the fixture settings dialog', completion=['Dialog closes correctly'],
            checks=['dialog-close'], profiles=['codex/personal'],
            scope={'paths': [str(self.root)], 'actions': ['read', 'write'], 'data': ['fixture']},
            resources=[copy.deepcopy(ui_jobs.SCREEN)],
            approval={'state': 'approved', 'reference': str(self.approval)},
            ui={'target': {'url': 'https://example.invalid/fixture', 'version': 'fixture-version'},
                'steps': ['Open the fixture and close the dialog'], 'scope': 'Only fixture data; no publishing'})
        self.native = dict(address='t1', state='live', name='ui-native',
            labels={'teamkit.task': 'UI-ACTOR', 'teamkit.owner': 'ui-owner', 'teamkit.name': 'ui-native'},
            foreground={'name': 'codex', 'pid': 42, 'argv': ['codex', '--model', 'fixture', '-c',
                'model_reasoning_effort="low"', '--no-daemon']}, status={'app': 'codex', 'state': 'idle'})
        store = Store(self.state)
        try:
            store.quota('codex/personal', dict(observed_at=time.time(), binding=binding(profiles['personal']),
                source='codex-native-quota', five_hour={'used': 10, 'resets_at': time.time()+300},
                weekly={'used': 10, 'resets_at': time.time()+300}))
        finally:
            store.close()
        for target, kwargs in [
            ('subprocess.run', {'side_effect': AssertionError('real process call forbidden')}),
            ('subprocess.Popen', {'side_effect': AssertionError('real process launch forbidden')}),
            ('subprocess.call', {'side_effect': AssertionError('real process call forbidden')}),
            ('subprocess.check_call', {'side_effect': AssertionError('real process call forbidden')}),
            ('subprocess.check_output', {'side_effect': AssertionError('real process call forbidden')}),
            ('teamkit.ui_jobs.CoreRuntime.launch', {'side_effect': AssertionError('real model launch forbidden')}),
            ('teamkit.ui_jobs.CoreRuntime.accept', {'side_effect': AssertionError('real model input forbidden')}),
            ('os.kill', {'side_effect': AssertionError('real process control forbidden')}),
            ('teamkit.core_runtime._call', {'side_effect': self.hc}),
            ('teamkit.ui_jobs._call', {'side_effect': self.read_screen}),
            ('teamkit.ui_jobs.require_managed', {'return_value': 'tctrl'}),
            ('teamkit.ui_jobs.providers.identity', {'return_value': {'verified': True}}),
            ('teamkit.admission.observe_native', {'side_effect': lambda: {**self.observation, 'observed_at': time.time()}}),
            ('teamkit.ui_jobs._lab', {'side_effect': self.lab})]:
            mocked = patch(target, **kwargs)
            mocked.start()
            self.addCleanup(mocked.stop)

    def hc(self, *args, **kwargs):
        self.assertEqual(args[0], 'get')
        value = copy.deepcopy(self.native)
        value['status']['state'] = self.native_state
        return value

    def read_screen(self, *args, **kwargs):
        self.assertEqual(args[:2], ('read', '--json'))
        return self.screen

    def lab(self, settings, action, ident=None, payload=None):
        self.calls.append((action, ident, copy.deepcopy(payload)))
        if action == 'status':
            return dict(registry=copy.deepcopy(self.registry), jobs=[dict(id=k,status=v['status']) for k,v in self.jobs.items()])
        if action == 'result':
            return copy.deepcopy(self.jobs[ident])
        self.assertEqual(action, 'submit')
        self.assertNotIn(payload['id'], self.jobs, 'duplicate official UI submission')
        self.jobs[payload['id']] = dict(id=payload['id'], status='queued', request=copy.deepcopy(payload))
        if self.timeout:
            raise subprocess.TimeoutExpired(['fixture-ui-lab'],30)
        if self.crash:
            raise SystemExit('consumer crashed after official submit persisted the job')
        return dict(id=payload['id'], status='queued', notification='sent')

    def start(self, **changes):
        spec = {**self.spec, **changes}
        self.queue.enqueue(spec, spec['id'], user_approved=True)
        return self.run_tick()

    def run_tick(self):
        self.config_path.write_text(json.dumps(self.config))
        return ui_jobs.tick(self.config_path, self.state)

    def ticket(self):
        return self.queue.get('UI-TICKET')

    def submissions(self):
        return [x for x in self.calls if x[0] == 'submit']

    def passed(self):
        job = next(iter(self.jobs.values()))
        evidence = self.root / 'evidence' / 'screen.txt'
        evidence.parent.mkdir(exist_ok=True)
        evidence.write_text('Fixture UI observed and closed correctly.')
        job.update(status='passed', owner='t1', started_at='fixture-start', finished_at='fixture-finish',
            result=dict(status='passed', summary='Fixture checked', host='test-macbook', tool='fixture-ui-tool',
                target_version='fixture-version', request_digest=digest(job['request']), writer_stopped=True,
                checks=[dict(name='dialog-close',status='passed')], evidence=[str(evidence)],
                evidence_sha256={str(evidence):file_digest(str(evidence))}))
        return job

    def test_submission_is_not_claim_or_completion_and_is_never_repeated(self):
        result = self.start()
        self.assertEqual(len(result['submitted']), 1)
        self.assertIn('outside control', result['coverage'])
        self.assertEqual(self.ticket()['state'], 'received')
        self.run_tick()
        self.assertEqual(self.ticket()['state'], 'received')
        self.assertIn('claim', self.ticket()['reason'])
        self.assertEqual(len(self.submissions()), 1)
        self.assertTrue(ResourceLedger._rows(self.queue.db)[0]['execution_reserved'])

    def test_crash_after_submit_recovers_by_query_without_resubmission(self):
        self.crash = True
        with self.assertRaises(SystemExit):
            self.start()
        attempt = self.queue.attempt(self.ticket()['attempt'])
        self.assertEqual(attempt['ui_delivery']['phase'], 'intent')
        self.crash = False
        self.run_tick()
        self.assertEqual(len(self.submissions()), 1)
        self.passed()
        self.run_tick()
        self.assertEqual(self.ticket()['state'], 'completed')
        self.assertEqual(len(self.submissions()), 1)

    def test_pass_requires_actual_claim_and_releases_execution_and_screen(self):
        self.start()
        self.passed()
        self.run_tick()
        self.assertEqual(self.ticket()['state'], 'completed')
        row = ResourceLedger._rows(self.queue.db)[0]
        self.assertFalse(row['execution_reserved'])
        self.assertFalse(row['writes_held'])
        proof = self.queue.attempt(self.ticket()['attempt'])['verification']
        self.assertEqual(proof['verifier'], 'official-ui-lab-result')

    def test_changed_target_or_evidence_or_check_keys_cannot_complete(self):
        self.start()
        job = self.passed()
        original = copy.deepcopy(job['result'])
        for changes in [dict(target_version='other'),dict(request_digest='0'*64),
                        dict(checks=[dict(name='unapproved',status='passed')]),
                        dict(evidence_sha256={job['result']['evidence'][0]:'0'*64}),
                        dict(writer_stopped=False),dict(host='other-host')]:
            with self.subTest(changes=changes):
                job['result'] = {**original, **changes}
                self.run_tick()
                self.assertNotEqual(self.ticket()['state'], 'completed')
                self.assertTrue(ResourceLedger._rows(self.queue.db)[0]['execution_reserved'])
        job['result'] = original
        self.run_tick()
        self.assertEqual(self.ticket()['state'], 'completed')

    def test_worker_owner_pid_draft_or_working_state_cannot_release_or_complete(self):
        self.start()
        job = self.passed()
        job['owner'] = 'other-terminal'
        self.run_tick()
        self.assertNotEqual(self.ticket()['state'], 'completed')
        job['owner'] = 't1'
        self.native['foreground']['pid'] = 99
        self.run_tick()
        self.assertNotEqual(self.ticket()['state'], 'completed')
        self.native['foreground']['pid'] = 42
        self.native_state = 'working'
        self.run_tick()
        self.assertNotEqual(self.ticket()['state'], 'completed')
        self.native_state = 'idle'
        self.screen = dict(complete=True,text='› unsent draft')
        self.run_tick()
        self.assertNotEqual(self.ticket()['state'], 'completed')
        self.screen = dict(complete=True,text='› ')
        self.run_tick()
        self.assertEqual(self.ticket()['state'], 'completed')

    def test_failed_or_blocked_result_is_reason_not_completion_or_retry(self):
        self.start()
        job = next(iter(self.jobs.values()))
        job.update(status='blocked', owner='t1', started_at='start', finished_at='finish',
                   result=dict(status='blocked',summary='Screen unavailable',reason='screen locked',writer_stopped=True))
        self.run_tick()
        self.assertIn('screen locked', self.ticket()['reason'])
        self.assertNotEqual(self.ticket()['state'], 'completed')
        self.assertFalse(ResourceLedger._rows(self.queue.db)[0]['execution_reserved'])
        self.run_tick()
        self.assertEqual(len(self.submissions()), 1)

    def test_unknown_lookup_after_intent_never_submits_again(self):
        self.start()
        self.jobs.clear()
        self.run_tick()
        self.run_tick()
        self.assertEqual(len(self.submissions()), 1)
        self.assertNotEqual(self.ticket()['state'], 'completed')

    def test_missing_binding_or_known_host_block_never_calls_ui_lab(self):
        self.config['operations']['ui_lab'].pop('worker')
        self.start()
        self.assertFalse(self.calls)
        self.assertIn('binding is missing', self.ticket()['reason'])
        self.config['operations']['ui_lab']['blocked_reason'] = 'screen locked; recording permission denied'
        self.run_tick()
        self.assertFalse(self.calls)
        self.assertIn('screen locked', self.ticket()['reason'])

    def test_existing_external_ui_job_preserves_its_screen_and_no_new_attempt(self):
        self.jobs['EXTERNAL'] = dict(id='EXTERNAL',status='running')
        self.start()
        self.assertFalse(self.submissions())
        self.assertIsNone(self.ticket()['attempt'])
        self.assertIn('queued/running', self.ticket()['reason'])

    def test_unapproved_screen_or_wrong_registered_account_never_dispatches(self):
        self.start(resources=[dict(kind='path',name=str(self.root),mode='read')])
        self.assertFalse(self.submissions())
        self.assertIn('screen:test-macbook',self.ticket()['reason'])

    def test_changed_registry_and_expired_quota_preserve_pending(self):
        self.registry['account'] = 'team'
        self.start()
        self.assertFalse(self.submissions())
        self.registry['account'] = 'personal'
        store = Store(self.state)
        try:
            quota=store.quota('codex/personal');quota['observed_at']=time.time()-121
            store.quota('codex/personal',quota)
        finally:
            store.close()
        self.run_tick()
        self.assertFalse(self.submissions())
        self.assertIn('quota',self.ticket()['reason'])

    def test_common_capacity_reservation_blocks_ui_start(self):
        self.config['operations']['resource_policy'] = {'global_limit': 1}
        ResourceLedger(self.queue.db).reserve('outside', 'OTHER', 'codex/personal', [], None,
                                             self.observation)
        self.start()
        self.assertFalse(self.submissions())
        self.assertIsNone(self.ticket()['attempt'])
        self.assertIn('capacity',self.ticket()['reason'])

    def test_changed_approval_blocks_pass_adoption(self):
        self.start()
        self.passed()
        self.approval.write_text('Changed approval without a new grant')
        self.run_tick()
        self.assertNotEqual(self.ticket()['state'], 'completed')
        self.assertTrue(ResourceLedger._rows(self.queue.db)[0]['execution_reserved'])

    def after_reservation_change(self, change):
        reserve = TicketStore.reserve
        def changed(queue, *args, **kwargs):
            result = reserve(queue, *args, **kwargs)
            change(result)
            return result
        with patch.object(TicketStore, 'reserve', changed):
            self.start()
        self.assertEqual(self.submissions(), [])

    def test_approval_revoked_after_reservation_never_submits(self):
        self.after_reservation_change(lambda _: self.approval.write_text('Revoked after reservation'))

    def test_config_changed_after_reservation_never_submits(self):
        self.after_reservation_change(lambda _: self.config_path.write_text(json.dumps(dict(self.config, quota_ttl_seconds=100))))

    def test_failure_policy_changed_after_reservation_never_submits(self):
        def change(attempt):
            ticket = self.ticket()
            ticket['failures'] = [{'kind':'reasoning','cause':'unresolved','effort':'high'}]
            self.queue.db.execute('UPDATE operation_tickets SET body=? WHERE id=?',
                                  (json.dumps(ticket),ticket['id']))
        self.after_reservation_change(change)

    def test_input_changed_after_reservation_never_submits(self):
        self.after_reservation_change(lambda _: self.screen.update(text='› unrelated draft'))

    def test_quota_expired_after_reservation_never_submits(self):
        def expired(attempt):
            store = Store(self.state)
            try:
                quota = store.quota('codex/personal')
                quota['observed_at'] = time.time()-121
                store.quota('codex/personal', quota)
            finally:
                store.close()
        self.after_reservation_change(expired)

    def test_first_ui_step_requires_exact_official_receipt_before_any_ui_action(self):
        self.start()
        attempt = self.queue.attempt(self.ticket()['attempt'])
        first = self.submissions()[0][2]['steps'][0]
        self.assertIn('queue receipt UI-TICKET --attempt '+attempt['id']+' --owner ui-owner', first)
        self.assertIn('If receipt fails', first)
        self.assertIn(str(self.state), first)

    def test_timeout_after_submit_preserves_uncertain_attempt_without_retry(self):
        self.timeout = True
        self.start()
        self.assertEqual(self.ticket()['state'], 'uncertain')
        self.timeout = False
        self.run_tick()
        self.assertEqual(len(self.submissions()), 1)
        self.passed()
        self.run_tick()
        self.assertEqual(self.ticket()['state'], 'completed')

    def test_second_ticket_waits_for_official_queue_and_named_screen_lease(self):
        self.start()
        self.start(id='UI-SECOND')
        second = self.queue.get('UI-SECOND')
        self.assertIsNone(second['attempt'])
        self.assertIn('queued/running',second['reason'])
        self.assertEqual(len(self.submissions()),1)

    def test_disabled_adapter_never_reads_external_ui_lab(self):
        self.config['operations']['ui_lab']['enabled'] = False
        self.assertFalse(self.start()['enabled'])
        self.assertFalse(self.calls)
        self.assertIn('not configured',self.ticket()['reason'])


if __name__ == '__main__':
    unittest.main()
