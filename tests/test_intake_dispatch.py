import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import time
import threading
import unittest
from unittest.mock import patch

from teamkit import intake_dispatch as dispatch
from teamkit.app_coordinator import record_path, scope
from teamkit.config import binding
from teamkit.intake import register
from teamkit.resources import ResourceLedger
from teamkit.store import Store
from teamkit.tickets import TicketStore


class IntakeDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / 'state.sqlite'
        self.path = self.root / 'config.json'
        pr = {'home': str(self.root / 'profile'), 'identity': {'email': 'fixture@example.invalid'},
              'model': 'fixture-model', 'effort': 'high'}
        self.config = {'version': 1, 'runtime_backend': 'core', 'quota_ttl_seconds': 120,
            'profiles': {'codex': {'team': pr, 'personal': {**pr, 'home': str(self.root / 'other-profile')}}},
            'teams': {'alpha': {'root': str(self.root), 'roles': {
                'leader': {'provider': 'codex', 'model': 'fixture-model', 'effort': 'high'}}}},
            'coordinator': {'provider': 'codex', 'model': 'fixture-model', 'effort': 'high', 'root': str(self.root)},
            'operations': {'enabled': True, 'intake': {'enabled': True}}}
        self.path.write_text(json.dumps(self.config))
        self.queue = TicketStore(self.state)
        self.addCleanup(self.queue.close)
        self.instruction = register(self.queue.db, 'Inspect the fixture and propose a task.',
                                    {'key': 'session:1', 'session': 'fixture-session', 'terminal': 't1'})
        self.record = dict(phase='ready', nonce='fixture-owner', terminal='t1', native_pid=123,
                           account='team', profile_binding=binding(pr), context_session_id='fixture-session')
        record_path(self.state).write_text(json.dumps(self.record))
        self.native = {'address': 't1', 'state': 'live', 'name': 'gui-coordinator-'+scope(self.state),
            'turn': 3, 'foreground': {'name': 'codex', 'pid': 123,
                'argv': ['codex', '--model', 'fixture-model', '-c', 'model_reasoning_effort="high"', '--no-daemon']},
            'labels': {'teamkit.owner': 'fixture-owner', 'teamkit.task': 'GUI-COORDINATOR',
                       'teamkit.name': 'gui-coordinator-'+scope(self.state)},
            'status': {'state': 'idle', 'app': 'codex'}}
        self.screen = {'complete': True, 'text': '› Ask Codex to do anything\nContext 80% left'}
        self.receipt = {'accepted': True, 'terminal': 't1', 'native_pid': 123, 'turn_baseline': 3, 'turn': 4}
        self.quota()
        for target in ('subprocess.Popen', 'subprocess.run', 'subprocess.call', 'subprocess.check_call',
                       'subprocess.check_output', 'teamkit.core_runtime._call', 'teamkit.intake_dispatch._call',
                       'teamkit.intake_dispatch.providers.identity', 'teamkit.intake_dispatch.CoreRuntime.accept'):
            guard = patch(target, side_effect=AssertionError('unmocked external boundary: '+target))
            guard.start()
            self.addCleanup(guard.stop)
        self.identity = patch.object(dispatch.providers, 'identity').start()
        self.addCleanup(patch.stopall)
        self.native_call = patch('teamkit.core_runtime._call', side_effect=self.core).start()
        self.screen_call = patch.object(dispatch, '_call', side_effect=self.read).start()
        self.accept = patch.object(dispatch.CoreRuntime, 'accept', side_effect=self.send).start()
        self.observation = patch('teamkit.admission.observe_native', return_value={
            'complete': True, 'observed_at': time.time(), 'terminals': []}).start()

    def quota(self, stale=False):
        store = Store(self.state)
        try:
            now = time.time()
            store.quota('codex/team', {'binding': self.record['profile_binding'], 'observed_at': now-121 if stale else now,
                'source': 'codex-native-quota', 'five_hour': {'used': 1, 'resets_at': now+500},
                'weekly': {'used': 1, 'resets_at': now+500}})
        finally:
            store.close()

    def core(self, *args, **kwargs):
        if args[0] != 'get' or args[1] != 't1':
            raise AssertionError('unexpected HC mutation: '+repr(args))
        return json.loads(json.dumps(self.native))

    def read(self, *args, **kwargs):
        self.assertEqual(args, ('read', '--json', 't1'))
        return self.screen

    def send(self, name, text):
        self.assertTrue(text.startswith(dispatch.PREFIX))
        attempts = list(self.queue.db.execute('SELECT body FROM operation_intake_attempts'))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(json.loads(attempts[0][0])['phase'], 'intent')
        self.assertTrue(ResourceLedger._rows(self.queue.db)[0]['execution_reserved'])
        self.native['status']['state'] = 'working'
        self.native['turn'] = 4
        return self.receipt

    def item(self):
        return json.loads(self.queue.db.execute('SELECT body FROM operation_instructions WHERE id=?',
                                                (self.instruction['id'],)).fetchone()[0])

    def spec(self):
        return {'product': 'alpha', 'role': 'investigator', 'kind': 'investigation', 'goal': 'inspect fixture',
                'completion': ['report exists'], 'checks': ['report'], 'profiles': ['codex/team'],
                'scope': {'paths': [str(self.root)], 'actions': ['read'], 'data': ['fixture']},
                'resources': [{'kind': 'path', 'name': str(self.root), 'mode': 'read'}]}

    def submit(self, attempt, packet, **identity):
        return dispatch.submit(self.config, self.state, self.instruction['id'], attempt['attempt'],
                               identity.get('owner', 'fixture-owner'), identity.get('terminal', 't1'), packet)

    def test_busy_original_turn_waits_then_dispatches_once_after_actual_idle(self):
        self.native['status']['state'] = 'working'
        result = dispatch.tick(self.path, self.state)
        self.assertIn('waiting', result)
        self.accept.assert_not_called()
        self.native['status']['state'] = 'idle'
        result = dispatch.tick(self.path, self.state)
        self.assertEqual(result['phase'], 'accepted')
        dispatch.tick(self.path, self.state)
        self.assertEqual(self.accept.call_count, 1)

    def test_lost_delivery_result_never_resends_or_fabricates_success(self):
        self.accept.side_effect = TimeoutError('unknown outcome')
        result = dispatch.tick(self.path, self.state)
        self.assertEqual(result['phase'], 'uncertain')
        dispatch.tick(self.path, self.state)
        self.assertEqual(self.accept.call_count, 1)
        self.assertEqual(self.queue.list(), [])

    def test_wrong_terminal_receipt_is_uncertain_not_accepted(self):
        self.receipt = dict(self.receipt, terminal='t-unrelated')
        result = dispatch.tick(self.path, self.state)
        self.assertEqual(result['phase'], 'uncertain')

    def test_changed_owner_pid_profile_draft_and_stale_quota_all_block_send(self):
        for change in ('owner', 'pid', 'profile', 'draft', 'incomplete', 'quota'):
            with self.subTest(change=change):
                original = json.loads(json.dumps(self.native))
                record = dict(self.record)
                if change == 'owner': self.native['labels']['teamkit.owner'] = 'someone-else'
                if change == 'pid': self.native['foreground']['pid'] = 456
                if change == 'profile':
                    record['profile_binding'] = 'changed'
                    record_path(self.state).write_text(json.dumps(record))
                if change == 'draft': self.screen['text'] = '› unsent draft'
                if change == 'incomplete': self.screen['complete'] = False
                if change == 'quota': self.quota(stale=True)
                result = dispatch.tick(self.path, self.state)
                self.assertIn('waiting', result)
                self.accept.assert_not_called()
                self.native = original
                record_path(self.state).write_text(json.dumps(self.record))
                self.screen = {'complete': True, 'text': '› Ask Codex to do anything'}
                self.quota()

    def test_root_proposals_wait_for_approval_and_release_only_after_actual_idle(self):
        result = dispatch.tick(self.path, self.state)
        packet = {'proposals': [self.spec()]}
        item = self.submit(result, packet)
        self.assertEqual(item['state'], 'classified')
        ticket = self.queue.get(item['ticket_ids'][0])
        self.assertEqual(ticket['state'], 'waiting_approval')
        self.assertEqual(ticket['instruction_id'], self.instruction['id'])
        self.assertTrue(ResourceLedger._rows(self.queue.db)[0]['execution_reserved'])
        self.assertEqual(self.submit(result, packet), item)
        with self.assertRaises(ValueError): self.submit(result, {'proposals': [dict(self.spec(), goal='different')]})
        dispatch.tick(self.path, self.state)
        self.assertTrue(ResourceLedger._rows(self.queue.db)[0]['execution_reserved'])
        self.native['status']['state'] = 'idle'
        dispatch.tick(self.path, self.state)
        self.assertFalse(ResourceLedger._rows(self.queue.db)[0]['execution_reserved'])

    def test_model_cannot_approve_or_install_new_verification_command(self):
        result = dispatch.tick(self.path, self.state)
        item = self.submit(result, {'proposals': [dict(self.spec(), verification_commands={'report': ['dangerous']})]})
        self.assertEqual(item['state'], 'needs_clarification')
        self.assertEqual(self.queue.list(), [])

    def test_existing_approved_parent_can_only_pass_its_original_narrow_scope(self):
        result = dispatch.tick(self.path, self.state)
        grant = self.root / 'approval.txt'
        grant.write_text('User authorizes the existing fixture parent.')
        parent = self.queue.enqueue({**self.spec(), 'id': 'PARENT', 'instruction_id': self.instruction['id'],
                                     'approval': {'state': 'approved', 'reference': str(grant)}},
                                    'existing-approved-parent', user_approved=True)
        item = self.submit(result, {'proposals': [dict(self.spec(), parent_id=parent['id'])]})
        self.assertIn(parent['id'], item['ticket_ids'])
        child = next(self.queue.get(ident) for ident in item['ticket_ids'] if ident != parent['id'])
        self.assertEqual(child['approval'], parent['approval'])
        self.assertEqual(child['state'], 'pending')
        self.assertEqual(child['parent_id'], parent['id'])

    def test_config_change_during_identity_blocks_before_reserving_or_accepting(self):
        def changed(*args):
            self.path.write_text(json.dumps(dict(self.config, quota_ttl_seconds=100)))
        self.identity.side_effect = changed
        result = dispatch.tick(self.path, self.state)
        self.assertIn('waiting', result)
        self.accept.assert_not_called()
        self.assertEqual(self.queue.db.execute('SELECT count(*) FROM operation_intake_attempts').fetchone()[0], 0)

    def test_invalid_later_proposal_rolls_back_earlier_ticket(self):
        result = dispatch.tick(self.path, self.state)
        item = self.submit(result, {'proposals': [self.spec(), dict(self.spec(), role='invalid')]})
        self.assertEqual(item['state'], 'needs_clarification')
        self.assertEqual(self.queue.list(), [])

    def test_submit_rejects_old_owner_and_native_identity_changes(self):
        result = dispatch.tick(self.path, self.state)
        with self.assertRaises(ValueError): self.submit(result, {'proposals': [self.spec()]}, owner='old-owner')
        self.native['foreground']['pid'] = 456
        with self.assertRaises(dispatch.NativeError): self.submit(result, {'proposals': [self.spec()]})
        self.assertEqual(self.queue.list(), [])

    def test_intent_survives_consumer_death_without_new_actor_or_resend(self):
        self.accept.side_effect = SystemExit('simulated consumer death')
        with self.assertRaises(SystemExit): dispatch.tick(self.path, self.state)
        dispatch.tick(self.path, self.state)
        self.assertEqual(self.accept.call_count, 1)

    def test_two_consumers_only_record_and_accept_one_attempt(self):
        barrier = threading.Barrier(2)
        self.identity.side_effect = lambda *args: barrier.wait(timeout=3)
        self.accept.side_effect = lambda *args: self.receipt
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: dispatch.tick(self.path, self.state), range(2)))
        self.assertEqual(self.accept.call_count, 1)
        self.assertEqual(self.queue.db.execute('SELECT count(*) FROM operation_intake_attempts').fetchone()[0], 1)
        self.assertEqual(len(ResourceLedger._rows(self.queue.db)), 1)
        self.assertTrue(any(result.get('phase') == 'accepted' for result in results))


if __name__ == '__main__':
    unittest.main()
