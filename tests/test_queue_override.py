"""Explicit manual priority send: bypass eligibility gates, keep delivery safety."""
import json
import time
import unittest

import test_operations as operation_fixtures
from teamkit import queue_actions as actions
from teamkit.operations import NativeRuntime
from teamkit.runtime import RuntimeError as NativeError
from teamkit.tickets import TicketStore


class QueueOverrideTest(unittest.TestCase):
    setUp = operation_fixtures.OperationsTest.setUp
    enqueue = operation_fixtures.OperationsTest.enqueue

    def mismatch(self):
        """Fixture executor that the official admission gate rejects."""
        def admit(ticket, candidate):
            raise ValueError('executor responsibility or role mismatch')
        self.runtime.admit = admit

    def choose(self, ticket='ONE'):
        choices = actions.candidates(self.config, self.state, ticket, runtime=self.runtime)
        return choices, choices['candidates'][0]

    def send(self, key, choices, candidate, **extra):
        return actions.enqueue(self.config, self.state, 'send', key, ticket='ONE', candidate_id=candidate['id'],
                               expected_ticket=choices['expected_ticket'], runtime=self.runtime, **extra)

    def busy_observation(self):
        self.runtime.observation = lambda: dict(observed_at=time.time(), complete=True, terminals=[
            dict(terminal='x%d' % n, pid=900 + n, provider='codex', account=None, status='working',
                 owner=None, attempt=None) for n in range(3)])

    def test_ineligible_candidate_is_listed_with_korean_reason_and_override_flag(self):
        self.enqueue('ONE')
        self.mismatch()
        choices, candidate = self.choose()
        self.assertFalse(candidate['eligible'])
        self.assertEqual('executor responsibility or role mismatch', candidate['reason'])
        self.assertIn('역할', candidate['reason_ko'])
        self.assertTrue(candidate['override_allowed'])
        self.assertFalse(choices['send_allowed'])

    def test_unknown_reason_keeps_original_text_in_korean_label(self):
        self.enqueue('ONE')
        self.runtime.admit = lambda *args: (_ for _ in ()).throw(ValueError('something new'))
        _, candidate = self.choose()
        self.assertIn('something new', candidate['reason_ko'])
        self.assertTrue(candidate['override_allowed'])

    def test_plain_send_to_ineligible_candidate_is_still_blocked(self):
        self.enqueue('ONE')
        self.mismatch()
        choices, candidate = self.choose()
        self.send('plain', choices, candidate)
        actions.consume(self.config, self.state, runtime=self.runtime)
        self.assertEqual('blocked', actions.status(self.state, 'plain')['status'])
        self.assertEqual([], self.runtime.sent)

    def test_override_sends_and_records_bypass_durably(self):
        self.enqueue('ONE')
        self.mismatch()
        choices, candidate = self.choose()
        queued = self.send('force', choices, candidate, override=True, override_reason='사용자가 직접 지정')
        self.assertEqual('queued', queued['status'])
        self.assertEqual('사용자가 직접 지정', queued['request']['override']['reason'])
        self.assertEqual(['executor responsibility or role mismatch'],
                         queued['request']['override']['original_reasons'])
        actions.consume(self.config, self.state, runtime=self.runtime)
        done = actions.status(self.state, 'force')
        self.assertEqual('completed', done['status'])
        self.assertEqual(1, len(self.runtime.sent))
        self.assertEqual(['executor responsibility or role mismatch'], done['result']['override']['bypassed'])
        store = TicketStore(self.state)
        try:
            attempt = store.attempt(store.get('ONE')['attempt'])
            self.assertEqual('force', attempt['override']['action'])
            self.assertEqual('사용자가 직접 지정', attempt['override']['reason'])
        finally:
            store.close()

    def test_override_requires_reason_and_explicit_flag_shape(self):
        self.enqueue('ONE')
        self.mismatch()
        choices, candidate = self.choose()
        for reason in (None, '', '   ', 'x' * 301):
            with self.assertRaisesRegex(ValueError, 'override reason'):
                self.send('bad-' + str(len(str(reason))), choices, candidate, override=True, override_reason=reason)
        with self.assertRaisesRegex(ValueError, 'override reason'):
            self.send('reason-only', choices, candidate, override=False, override_reason='사유만')

    def test_override_replay_with_other_reason_is_rejected(self):
        self.enqueue('ONE')
        self.mismatch()
        choices, candidate = self.choose()
        self.send('same', choices, candidate, override=True, override_reason='첫 사유')
        with self.assertRaisesRegex(ValueError, 'already binds'):
            self.send('same', choices, candidate, override=True, override_reason='다른 사유')

    def test_retry_with_same_id_and_reason_returns_the_existing_action(self):
        # R1-3: the stored action is the answer; a later preview must not make the retry look different.
        self.enqueue('ONE')
        self.mismatch()
        choices, candidate = self.choose()
        first = self.send('same', choices, candidate, override=True, override_reason='같은 사유')
        again = self.send('same', choices, candidate, override=True, override_reason='같은 사유')
        self.assertEqual(('same', 'queued'), (again['id'], again['status']))
        self.assertEqual(first['request'], again['request'])
        actions.consume(self.config, self.state, runtime=self.runtime)
        done = actions.status(self.state, 'same')
        after = self.send('same', choices, candidate, override=True, override_reason='같은 사유')
        self.assertEqual((done['id'], done['status']), (after['id'], after['status']))
        self.assertEqual('completed', after['status'])
        self.assertEqual(1, len(self.runtime.sent))
        with self.assertRaisesRegex(ValueError, 'already binds'):
            self.send('same', choices, candidate, override=True, override_reason='다른 사유')

    def test_capacity_and_profile_constraints_are_bypassed(self):
        self.enqueue('ONE', profiles=['claude/team'], execution_constraint={'provider': 'claude', 'account': 'team'})
        self.busy_observation()
        self.mismatch()
        choices, candidate = self.choose()
        self.assertFalse(candidate['eligible'])
        self.send('cap', choices, candidate, override=True, override_reason='용량 무시')
        actions.consume(self.config, self.state, runtime=self.runtime)
        self.assertEqual('completed', actions.status(self.state, 'cap')['status'])
        self.assertEqual(1, len(self.runtime.sent))

    def test_capacity_still_blocks_without_override(self):
        self.enqueue('ONE')
        self.busy_observation()
        _, candidate = self.choose()
        self.assertFalse(candidate['eligible'])
        self.assertIn('용량', candidate['reason_ko'])

    def test_draft_or_dialog_guard_is_never_bypassed(self):
        self.enqueue('ONE')
        self.mismatch()
        choices, candidate = self.choose()
        def native(executor, require_idle=True):
            raise ValueError('executor draft, dialog or incomplete screen')
        self.runtime.native = native
        self.send('draft', choices, candidate, override=True, override_reason='강제')
        actions.consume(self.config, self.state, runtime=self.runtime)
        done = actions.status(self.state, 'draft')
        self.assertEqual('blocked', done['status'])
        self.assertIn('draft', done['reason'])
        self.assertEqual([], self.runtime.sent)
        store = TicketStore(self.state)
        try:
            self.assertEqual('pending', store.get('ONE')['state'])
            self.assertEqual([], list(store.db.execute('SELECT * FROM operation_attempts')))
        finally:
            store.close()

    def test_owner_or_process_change_is_never_bypassed(self):
        self.enqueue('ONE')
        self.mismatch()
        choices, candidate = self.choose()
        def native(executor, require_idle=True):
            raise ValueError('registered executor PID changed')
        self.runtime.native = native
        self.send('pid', choices, candidate, override=True, override_reason='강제')
        actions.consume(self.config, self.state, runtime=self.runtime)
        self.assertEqual('blocked', actions.status(self.state, 'pid')['status'])
        self.assertEqual([], self.runtime.sent)

    def test_draft_appearing_at_delivery_is_reported_not_overwritten(self):
        self.enqueue('ONE')
        self.mismatch()
        choices, candidate = self.choose()
        calls = []
        def native(executor, require_idle=True):
            calls.append(1)
            if len(calls) > 1:
                raise ValueError('executor draft, dialog or incomplete screen')
            return dict(terminal='t1', pid=42, owner='fixture-owner', status='idle')
        self.runtime.native = native
        self.send('late', choices, candidate, override=True, override_reason='강제')
        actions.consume(self.config, self.state, runtime=self.runtime)
        self.assertEqual([], self.runtime.sent)
        self.assertNotEqual('completed', actions.status(self.state, 'late')['status'])

    def test_approval_gate_is_not_bypassed(self):
        self.enqueue('ONE')
        self.mismatch()
        choices, candidate = self.choose()
        self.approval.write_text('withdrawn')
        result = self.send('noapproval', choices, candidate, override=True, override_reason='강제')
        self.assertEqual('blocked', result['status'])
        self.assertEqual([], self.runtime.sent)

    def test_non_pending_ticket_is_never_resent_even_with_override(self):
        self.enqueue('ONE')
        choices, candidate = self.choose()
        self.send('first', choices, candidate)
        actions.consume(self.config, self.state, runtime=self.runtime)
        self.assertEqual(1, len(self.runtime.sent))
        self.mismatch()
        result = self.send('second', choices, candidate, override=True, override_reason='다시')
        self.assertEqual('blocked', result['status'])
        actions.consume(self.config, self.state, runtime=self.runtime)
        self.assertEqual(1, len(self.runtime.sent))

    def test_unfinished_previous_selection_blocks_override(self):
        self.enqueue('ONE')
        self.mismatch()
        choices, candidate = self.choose()
        self.send('one', choices, candidate, override=True, override_reason='강제')
        result = self.send('two', choices, candidate, override=True, override_reason='강제 둘')
        self.assertEqual('blocked', result['status'])
        self.assertIn('previous selected action', result['reason'])

    def test_planned_new_worker_is_not_overridable(self):
        self.enqueue('ONE')
        planned = dict(self.runtime.executor, owner='new-owner', terminal=None, pid=None, new=True)
        self.runtime.executors = lambda ticket=None: [planned]
        from teamkit.workers import PLAN_FIELDS
        self.runtime.admit = lambda *args: (_ for _ in ()).throw(ValueError('profile execution capacity is exhausted or unknown'))
        choices, candidate = self.choose()
        self.assertFalse(candidate['override_allowed'])
        with self.assertRaisesRegex(ValueError, 'planned'):
            self.send('new', choices, candidate, override=True, override_reason='새 직원')
        self.assertTrue(PLAN_FIELDS)

    def test_ui_role_is_not_overridable(self):
        self.enqueue('ONE', role='ui')
        choices = actions.candidates(self.config, self.state, 'ONE', runtime=self.runtime)
        self.assertEqual([], choices['candidates'])
        self.assertFalse(choices['send_allowed'])


class RealAdmitOverrideTest(unittest.TestCase):
    """R1-2: manual priority against the real NativeRuntime.admit.

    Replaced boundaries: HC `_call`, `native` (owner/PID/draft read), provider `identity`
    (a live login probe), `observation`, and `send`. Every gate in admit() is the real code.
    """
    setUp = operation_fixtures.OperationsTest.setUp
    enqueue = operation_fixtures.OperationsTest.enqueue

    def runtime_for(self, executor_changes=None, operations=None):
        from teamkit.config import binding, load, profile
        config = json.loads(self.config.read_text())
        config['operations'].update(operations or {})
        self.config.write_text(json.dumps(config))
        loaded = load(self.config)
        runtime = NativeRuntime(loaded, self.state)
        executor = dict(terminal='t1', pid=42, owner='fixture-owner', provider='codex', account='personal',
                        product='example', roles=['investigator'], root=str(self.root),
                        profile_binding=binding(profile(loaded, 'codex', 'personal')),
                        task='POOL-LEADER-EXAMPLE', runtime=dict(name='tk-fixture'), empty_prompt=None)
        executor.update(executor_changes or {})
        runtime.executors = lambda ticket=None: [executor]
        runtime.native = lambda item, require_idle=True: dict(terminal='t1', pid=42, owner='fixture-owner',
                                                              status='idle', turn=0)
        runtime.observation = lambda: dict(observed_at=time.time(), complete=True, terminals=[])
        runtime.sent = []
        def send(item, text, before_send=None):
            if before_send:
                before_send()
            runtime.sent.append(text)
            return {'accepted': True}
        runtime.send = send
        return runtime

    def native_call(self, args, *rest, **kw):
        if args == 'get':
            return dict(state='live', labels={}, foreground=dict(pid=42, cwd=str(self.root), argv=[
                'codex', '--model', 'fixture', '-c', 'model_reasoning_effort="medium"', '--no-daemon']))
        return {}

    def run_override(self, runtime, key='force'):
        from unittest.mock import patch
        with patch('teamkit.core_runtime._call', side_effect=self.native_call), \
             patch('teamkit.providers.identity', return_value=None):
            choices = actions.candidates(self.config, self.state, 'ONE', runtime=runtime)
            row = choices['candidates'][0]
            queued = actions.enqueue(self.config, self.state, 'send', key, ticket='ONE', candidate_id=row['id'],
                                     expected_ticket=choices['expected_ticket'], override=True,
                                     override_reason='사용자가 직접 지정', runtime=runtime)
            actions.consume(self.config, self.state, runtime=runtime)
        return row, queued, actions.status(self.state, key)

    def assert_blocked_by(self, runtime, needle, code):
        row, _, done = self.run_override(runtime)
        self.assertFalse(row['override_allowed'], row['reason'])
        self.assertIn(needle, row['reason'])
        self.assertIn(code, [reason['code'] for reason in row['reasons']])
        self.assertEqual('blocked', done['status'])
        self.assertIn(needle, done['reason'])
        self.assertEqual([], runtime.sent)
        store = TicketStore(self.state)
        try:
            self.assertEqual('pending', store.get('ONE')['state'])
            self.assertEqual([], list(store.db.execute('SELECT * FROM operation_attempts')))
        finally:
            store.close()
        return row

    def test_role_mismatch_does_not_hide_review_independence(self):
        self.enqueue('ONE', role='reviewer', kind='review', exclude_owners=['fixture-owner'])
        runtime = self.runtime_for(dict(roles=['investigator']))
        row = self.assert_blocked_by(runtime, 'review executor must be independent', 'review_independence')
        self.assertIn('executor responsibility or role mismatch', row['reason'])
        self.assertIn('리뷰는 구현 담당과 다른 직원', row['reason_ko'])

    def test_working_folder_outside_ticket_resources_is_never_overridden(self):
        self.enqueue('ONE')
        runtime = self.runtime_for(dict(root='/elsewhere/not-covering'))
        self.assert_blocked_by(runtime, 'executor working boundary does not cover ticket resources', 'working_boundary')

    def test_changed_profile_binding_is_never_overridden(self):
        self.enqueue('ONE')
        runtime = self.runtime_for(dict(profile_binding='stale-binding'))
        self.assert_blocked_by(runtime, 'executor profile binding changed', 'profile_binding')

    def test_personal_document_staff_boundary_is_never_overridden(self):
        departments = {'docs': {'root': str(self.root), 'roles': {
            'implementer': {'provider': 'claude', 'model': 'writing-model', 'effort': 'medium'}}}}
        self.enqueue('ONE', product='docs', role='documenter', kind='document')
        runtime = self.runtime_for(dict(product='example', roles=['documenter']), dict(
            enabled=True, departments=departments, worker_roles={'documenter': 'implementer'},
            document_staff={'department': 'docs', 'profiles': ['codex/personal', 'claude/personal']}))
        self.assert_blocked_by(runtime, 'executor is outside personal document staff', 'docs_staff')

    def test_provider_identity_is_probed_once_per_account_in_a_burst_and_failures_are_not_cached(self):
        from unittest.mock import patch
        self.enqueue('ONE')
        runtime = self.runtime_for()
        ticket = TicketStore(self.state)
        try:
            item = ticket.get('ONE')
        finally:
            ticket.close()
        executor = runtime.executors()[0]
        with patch('teamkit.core_runtime._call', side_effect=self.native_call), \
             patch('teamkit.providers.identity', return_value=None) as probe:
            runtime.admit_report(item, executor)
            runtime.admit_report(item, dict(executor, terminal='t2', pid=43))
        self.assertEqual(1, probe.call_count)
        broken = self.runtime_for()
        with patch('teamkit.core_runtime._call', side_effect=self.native_call), \
             patch('teamkit.providers.identity', side_effect=NativeError('login expired')) as probe:
            first = broken.admit_report(item, executor)
            second = broken.admit_report(item, executor)
        self.assertEqual(2, probe.call_count)
        self.assertIn('identity', [x['code'] for x in first + second])

    def test_role_mismatch_and_capacity_are_both_recorded_as_bypassed(self):
        self.enqueue('ONE')
        runtime = self.runtime_for(dict(roles=['implementer']))
        runtime.observation = lambda: dict(observed_at=time.time(), complete=True, terminals=[
            dict(terminal='x%d' % n, pid=900 + n, provider='codex', account=None, status='working',
                 owner=None, attempt=None) for n in range(3)])
        row, queued, done = self.run_override(runtime)
        self.assertTrue(row['override_allowed'])
        codes = [reason['code'] for reason in row['reasons']]
        self.assertIn('role', codes)
        self.assertIn('capacity', codes)
        self.assertIn('역할', row['reason_ko'])
        self.assertIn('용량', row['reason_ko'])
        self.assertEqual('completed', done['status'], done['reason'])
        self.assertEqual(1, len(runtime.sent))
        bypassed = done['result']['override']['bypassed']
        self.assertIn('executor responsibility or role mismatch', bypassed)
        self.assertTrue(any('execution capacity' in text for text in bypassed), bypassed)
        recorded = queued['request']['override']['original_reasons']
        self.assertIn('executor responsibility or role mismatch', recorded)
        self.assertTrue(any('execution capacity' in text for text in recorded), recorded)
        store = TicketStore(self.state)
        try:
            attempt = store.attempt(store.get('ONE')['attempt'])
            self.assertEqual(bypassed, attempt['override']['bypassed'])
        finally:
            store.close()


class ReasonTranslationTest(unittest.TestCase):
    def test_known_reasons_have_short_korean_phrases(self):
        samples = {
            'executor responsibility or role mismatch': '역할',
            'executor draft, dialog or incomplete screen': '입력',
            'profile execution capacity is exhausted or unknown': '용량',
            'global execution capacity is exhausted': '용량',
            'Core terminal owner/process changed; preserve it and inspect': '터미널',
            'registered executor PID changed': '프로세스',
            'profile quota unavailable, expired or limited': '사용량',
            'Core agent is not idle and ready; inspect before sending': '작업 중',
            'previous selected action is queued, running or uncertain; inspect original outcome before reselection': '이전',
            'native terminal already belongs to another live attempt': '다른 작업',
            'another attempt holds a conflicting resource lease': '충돌',
        }
        for english, word in samples.items():
            with self.subTest(english=english):
                korean = actions.reason_ko(english)
                self.assertIn(word, korean)
                self.assertNotEqual(english, korean)

    def test_empty_reason_stays_empty(self):
        self.assertEqual('', actions.reason_ko(''))


if __name__ == '__main__':
    unittest.main()
