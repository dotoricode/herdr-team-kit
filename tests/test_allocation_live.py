"""Host allocation behavior at the HC, process and file transport boundaries."""
import copy
import unittest
from unittest.mock import patch
import test_workspace_sessions as fixtures
from teamkit import workspace_sessions as ws, admission
from teamkit.operations import NativeRuntime
from teamkit.prompt_state import empty_input


class LiveAllocationTests(fixtures.WorkspaceTests):

    def test_discovery_needs_no_registration_and_honors_optout(self):
        self.assertFalse(ws.discover(self.c, self.state, 'alpha')[0]['registration_required'])
        self.native['labels']['teamkit.auto'] = 'off'
        self.assertEqual([], ws.discover(self.c, self.state, 'alpha'))

    def test_restarted_session_is_candidate_without_pool_or_receipt(self):
        self.c['operations'] = {'allocation_policy': 'usage'}
        self.native['foreground']['pid'] = 124
        runtime = NativeRuntime(self.c, self.state)
        candidates = runtime.executors({'product':'alpha', 'role':'investigator'})
        live = [x for x in candidates if x.get('terminal') == 't1']
        self.assertEqual(1, len(live))
        self.assertEqual(124, live[0]['pid'])
        self.assertEqual(124, runtime.native(live[0])['pid'])
        self.assertEqual('old', self.native['labels']['teamkit.owner'], 'preview must not relabel')

    def test_dead_old_pid_releases_even_when_terminal_is_live_with_new_owner(self):
        with patch.object(admission, 'native_get', return_value=copy.deepcopy(self.native)), \
             patch.object(admission.os, 'kill', side_effect=ProcessLookupError):
            self.assertTrue(admission._stopped({'terminal':'t1','pid':122,'owner':'retired'}, 'codex/team'))
        with patch.object(admission, 'native_get', return_value=copy.deepcopy(self.native)), \
             patch.object(admission.os, 'kill', side_effect=PermissionError):
            self.assertFalse(admission._stopped({'terminal':'t1','pid':122,'owner':'retired'}, 'codex/team'))

    def test_footer_shape_does_not_gate_idle_prompt_but_multiline_draft_does(self):
        self.assertTrue(empty_input('› \n\n  Totally new CLI status layout'))
        self.assertFalse(empty_input('› \n  unfinished multiline draft\n\n  Totally new CLI status layout'))


class ReplacedAttemptTests(unittest.TestCase):
    def test_only_replaced_attempt_loses_reservations_and_next_ticket_can_use_terminal(self):
        import test_operations as fixtures
        # Reuse the real queue setup; only HC and OS observations are replaced.
        fixtures.OperationsTest.setUp(self)
        fixtures.OperationsTest.enqueue(self, 'OLD')
        queue = ws.TicketStore(self.state)
        self.addCleanup(queue.close)
        actor = dict(self.runtime.executor, task='fixture', runtime={'name':'fixture'})
        attempt = queue.reserve('OLD', actor, {}, self.runtime.observation())
        queue.receipt('OLD', attempt['id'], actor['owner'], 't1', dict(actor, turn=1))
        runtime = NativeRuntime(__import__('json').loads(self.config.read_text()), self.state)
        from teamkit.operations import reconcile
        with patch.object(runtime, 'native', side_effect=ValueError('old PID changed')), \
             patch('teamkit.core_runtime._call', return_value={'address':'t1','state':'live','foreground':{'pid':43}}), \
             patch.object(admission.os, 'kill', side_effect=ProcessLookupError):
            reconcile(queue, runtime, {})
        self.assertEqual('process_replaced', queue.get('OLD')['intervention']['kind'])
        self.assertFalse(ws.ResourceLedger._rows(queue.db)[0]['execution_reserved'])
        self.assertFalse(ws.ResourceLedger._rows(queue.db)[0]['writes_held'])
        fixtures.OperationsTest.enqueue(self, 'NEXT')
        next_attempt = queue.reserve('NEXT', dict(actor, pid=43, owner='fresh'), {}, self.runtime.observation())
        self.assertEqual(43, next_attempt['executor']['pid'])
