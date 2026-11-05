"""R2 review regressions: the real NativeRuntime.admit on the automatic path.

Replaced boundaries: HC `_call`, `native`, provider `identity`, `observation`, `send`, quota freshness.
Every gate in admit()/admit_report() is the real code.
"""
import time
import unittest
from unittest.mock import patch

import test_queue_override as override_fixtures
from teamkit import operations
from teamkit.operations import AdmitError, NativeRuntime, gate_issues, tick
from teamkit.runtime import RuntimeError as NativeError
from teamkit.tickets import TicketStore

NOT_IDLE = 'Core agent is not idle and ready; inspect before sending'
POLICY = dict(worker_roles={'investigator': 'leader'})


class AutoPathRegressionTest(unittest.TestCase):
    setUp = override_fixtures.RealAdmitOverrideTest.setUp
    enqueue = override_fixtures.RealAdmitOverrideTest.enqueue
    runtime_for = override_fixtures.RealAdmitOverrideTest.runtime_for
    native_call = override_fixtures.RealAdmitOverrideTest.native_call

    def ticket(self, ident='ONE'):
        store = TicketStore(self.state)
        try:
            return store.get(ident)
        finally:
            store.close()

    def quota_ok(self):
        return patch.multiple('teamkit.operations', cached_quota=lambda *a, **k: {}, eligible=lambda *a, **k: True)

    # R2-1: a new worker that is briefly busy must be waited for, not failed.
    def launched(self, native):
        self.enqueue('ONE')
        runtime = self.runtime_for(operations=POLICY)
        executor = runtime.executors()[0]
        runtime.native = native
        with patch('teamkit.workers.launch', return_value=executor), \
             patch('teamkit.core_runtime._call', side_effect=self.native_call), \
             patch('teamkit.providers.identity', return_value=None), \
             patch('teamkit.operations.time.sleep'), self.quota_ok():
            return executor, runtime.launch(self.config, self.ticket(), {'id': 'attempt-one'})

    def test_launch_waits_through_one_not_idle_read_with_the_real_admit(self):
        calls = []

        def native(item, require_idle=True):
            calls.append(require_idle)
            if require_idle and len(calls) == 1:
                raise NativeError(NOT_IDLE)
            return dict(terminal='t1', pid=42, owner='fixture-owner', status='working' if not require_idle else 'idle',
                        turn=0)
        executor, result = self.launched(native)
        self.assertIs(executor, result)
        self.assertGreater(len(calls), 1)

    def test_launch_does_not_retry_a_draft_or_dialog_failure(self):
        calls = []

        def native(item, require_idle=True):
            calls.append(require_idle)
            raise ValueError('executor draft, dialog or incomplete screen')
        with self.assertRaises(ValueError) as caught:
            self.launched(native)
        self.assertIn('draft', str(caught.exception))
        self.assertEqual(1, len(calls))

    # R2-2: an absent foreground process is a native failure, never an exception out of tick().
    def native_call_without_foreground(self, args, *rest, **kw):
        if args == 'get' and rest and rest[0] == 't2':
            return dict(state='live', labels={}, foreground=None)
        return self.native_call(args, *rest, **kw)

    def test_null_foreground_is_a_non_bypassable_native_issue(self):
        self.enqueue('ONE')
        runtime = self.runtime_for(dict(terminal='t2', pid=43), POLICY)
        with patch('teamkit.core_runtime._call', side_effect=self.native_call_without_foreground), \
             patch('teamkit.providers.identity', return_value=None), self.quota_ok():
            issues = runtime.admit_report(self.ticket(), runtime.executors()[0])
        native = [x for x in issues if x['code'] == 'native' and 'foreground' in x['reason']]
        self.assertEqual(1, len(native), issues)
        self.assertFalse(native[0]['bypassable'])
        self.assertNotIn('unclassified', [x['code'] for x in issues])

    def test_null_foreground_executor_does_not_stop_the_automatic_tick(self):
        self.enqueue('ONE')
        runtime = self.runtime_for(operations=POLICY)
        good = runtime.executors()[0]
        bad = dict(good, terminal='t2', pid=43, owner='other-owner')
        runtime.executors = lambda ticket=None: [bad, good]
        with patch('teamkit.core_runtime._call', side_effect=self.native_call_without_foreground), \
             patch('teamkit.providers.identity', return_value=None), self.quota_ok():
            tick(self.config, self.state, runtime)
        self.assertEqual(1, len(runtime.sent), 'the healthy executor is still assigned')
        store = TicketStore(self.state)
        try:
            attempt = store.attempt(store.get('ONE')['attempt'])
        finally:
            store.close()
        self.assertEqual('t1', attempt['executor']['terminal'])

    def test_unexpected_gate_exception_is_recorded_unbypassable_and_tick_continues(self):
        self.enqueue('ONE')
        runtime = self.runtime_for(operations=POLICY)
        with patch('teamkit.core_runtime._call', side_effect=self.native_call), \
             patch('teamkit.providers.identity', side_effect=TypeError('boom')), self.quota_ok():
            issues = runtime.admit_report(self.ticket(), runtime.executors()[0])
            tick(self.config, self.state, runtime)
        unclassified = [x for x in issues if x['code'] == 'unclassified']
        self.assertEqual(1, len(unclassified), issues)
        self.assertFalse(unclassified[0]['bypassable'])
        self.assertIn('TypeError', unclassified[0]['reason'])
        self.assertIn('boom', unclassified[0]['reason'])
        self.assertEqual([], runtime.sent)
        self.assertEqual('pending', self.ticket()['state'])

    # R2-3: the automatic path stops at the first failed gate; only previews evaluate every gate.
    def test_automatic_tick_does_not_read_native_or_identity_for_a_role_mismatch(self):
        self.enqueue('ONE')
        runtime = self.runtime_for(dict(roles=['implementer']), POLICY)
        counts = dict(native=0, call=0, identity=0)

        def native(item, require_idle=True):
            counts['native'] += 1
            return dict(terminal='t1', pid=42, owner='fixture-owner', status='idle', turn=0)

        def call(*args, **kw):
            counts['call'] += 1
            return self.native_call(*args, **kw)

        def identity(*args, **kw):
            counts['identity'] += 1
        runtime.native = native
        with patch('teamkit.core_runtime._call', side_effect=call), \
             patch('teamkit.providers.identity', side_effect=identity), self.quota_ok():
            tick(self.config, self.state, runtime)
        self.assertEqual(dict(native=0, call=0, identity=0), counts)
        self.assertEqual('pending', self.ticket()['state'])
        self.assertEqual([], runtime.sent)

    def test_preview_still_reports_every_gate_for_the_same_executor(self):
        self.enqueue('ONE')
        runtime = self.runtime_for(dict(roles=['implementer'], root='/elsewhere/not-covering'), POLICY)
        with patch('teamkit.core_runtime._call', side_effect=self.native_call), \
             patch('teamkit.providers.identity', return_value=None), self.quota_ok():
            codes = [x['code'] for x in runtime.admit_report(self.ticket(), runtime.executors()[0])]
        self.assertEqual(['role', 'working_boundary'], codes)

    # Reference 2: a runtime that cannot classify its failure must not be bypassable.
    def test_runtime_without_admit_report_reports_failures_as_not_bypassable(self):
        class Plain:
            def admit(self, ticket, executor):
                raise ValueError('something new')
        issues = gate_issues(Plain(), {}, {})
        self.assertEqual([dict(code='unclassified', bypassable=False, reason='something new')], issues)
        class Wrapped:
            def admit(self, ticket, executor):
                raise AdmitError([dict(code='role', bypassable=True, reason='mismatch')])
        self.assertEqual([dict(code='role', bypassable=True, reason='mismatch')], gate_issues(Wrapped(), {}, {}))


if __name__ == '__main__':
    unittest.main()
