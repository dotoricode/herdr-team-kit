"""Send candidates follow the product team's real staff; uncertain slots are judged by read-only proof."""
import json
import os
from unittest.mock import patch
import unittest

import test_operations as operation_fixtures
from teamkit import queue_actions as actions
from teamkit import session_pool as pool
from teamkit.operations import NativeRuntime

SPACE = 'space-example'


def slot(terminal, phase, owner, **extra):
    base = dict(phase=phase, owner=owner, generation=1, terminal=terminal, native_pid=700, name='tk-' + owner[:20],
                provider='codex', account='personal', team='example', root='/work/example', binding='b',
                task='POOL-LEADER-EXAMPLE', model='m', effort='medium', runtime=dict(name='tk-' + owner[:20]),
                spec=dict(args=['--model', 'm']))
    base.update(extra)
    return base


class RosterRuntime(operation_fixtures.NativeFixture):
    """Fixture runtime: executors plus the roster hook the real runtime implements with `hc get`/`hc ls`."""
    def __init__(self, state, executors, info, unregistered=()):
        super().__init__(state)
        self._executors, self.info, self._unregistered = executors, info, list(unregistered)

    def executors(self, ticket=None):
        return self._executors

    def roster(self, ticket, executors):
        return dict(info=self.info, unregistered=self._unregistered)


class CandidateRosterTest(unittest.TestCase):
    setUp = operation_fixtures.OperationsTest.setUp
    enqueue = operation_fixtures.OperationsTest.enqueue

    def executor(self, terminal, owner, product='example', **extra):
        return dict(self.runtime.executor, terminal=terminal, owner=owner, product=product,
                    roles=['investigator'], **extra)

    def candidates(self, executors, info, unregistered=()):
        self.enqueue('ONE')
        self.runtime = RosterRuntime(self.state, executors, info, unregistered)
        return actions.candidates(self.config, self.state, 'ONE', runtime=self.runtime)

    def test_other_team_executor_is_flagged_and_team_executor_is_not(self):
        own, other = self.executor('t1', 'own'), self.executor('t2', 'other', product='elsewhere')
        result = self.candidates([own, other], {'t1': dict(state='live'), 't2': dict(state='live')})
        flags = {row['terminal']: row['other_team'] for row in result['candidates']}
        self.assertEqual({'t1': False, 't2': True}, flags)

    def test_background_temporary_worker_is_hidden_by_default_like_the_sidebar(self):
        result = self.candidates([self.executor('t1', 'own'), self.executor('t2', 'temp')],
                                 {'t1': dict(state='live'), 't2': dict(state='live', background=True)})
        hidden = {row['terminal']: row['hidden_by_default'] for row in result['candidates']}
        self.assertEqual({'t1': False, 't2': True}, hidden)

    def test_finished_terminal_is_excluded_with_recorded_reason(self):
        gone = self.executor('t9', 'gone')
        result = self.candidates([self.executor('t1', 'own'), gone],
                                 {'t1': dict(state='live'), 't9': dict(state='finished')})
        self.assertEqual(['t1'], [row['terminal'] for row in result['candidates']])
        self.assertEqual(['t9'], [row['terminal'] for row in result['excluded']])
        self.assertIn('종료', result['excluded'][0]['reason_ko'])

    def test_name_uses_terminal_label_then_generated_alias(self):
        result = self.candidates([self.executor('t1', 'own'), self.executor('t2', 'plain')],
                                 {'t1': dict(state='live', label='박과장 (팀)'), 't2': dict(state='live')})
        names = {row['terminal']: row['alias'] for row in result['candidates']}
        self.assertEqual('박과장 (팀)', names['t1'])
        self.assertRegex(names['t2'], r'\((개인|팀)\)$')
        self.assertNotEqual('박과장 (팀)', names['t2'])

    def test_unverified_team_terminal_shows_live_verification_reason(self):
        result = self.candidates([self.executor('t1', 'own')], {'t1': dict(state='live')},
                                 [dict(terminal='t174', label='권대리 (팀)')])
        row = next(row for row in result['candidates'] if row['terminal'] == 't174')
        self.assertEqual('권대리 (팀)', row['alias'])
        self.assertFalse(row['eligible'])
        self.assertFalse(row['override_allowed'])
        self.assertFalse(row['other_team'])
        self.assertIn('실시간', row['reason_ko'])
        self.assertNotIn('등록 필요',row['reason_ko'])


class UncertainSlotTest(unittest.TestCase):
    setUp = operation_fixtures.OperationsTest.setUp

    def runtime_with(self, *slots):
        manifest = {'version': 1, 'slots': {'example/codex/%d' % n: s for n, s in enumerate(slots)}}
        from teamkit.tickets import TicketStore
        TicketStore(self.state).close()
        pool._write(self.state, manifest)
        os.chmod(pool._path(self.state), 0o600)
        return NativeRuntime(json.loads(self.config.read_text()), self.state)

    def test_static_uncertain_slot_is_not_an_executor(self):
        runtime = self.runtime_with(slot('t7','uncertain','a'*32),slot('t8','preparing','b'*32))
        self.assertEqual([],runtime.executors())

    def test_uncertain_slot_without_terminal_or_pid_is_skipped_not_fatal(self):
        # R1-1: the launch can fail before a terminal or PID is recorded; such a slot is not a candidate.
        no_pid, no_terminal = slot('t7', 'uncertain', 'a' * 32), slot('t8', 'uncertain', 'b' * 32)
        del no_pid['native_pid']
        del no_terminal['terminal']
        text_pid = slot('t6', 'uncertain', 'd' * 32, native_pid='700')
        runtime = self.runtime_with(no_pid, no_terminal, text_pid, slot('t9', 'uncertain', 'c' * 32))
        found = [e['terminal'] for e in runtime.executors() if e['task'] == 'POOL-LEADER-EXAMPLE']
        self.assertEqual([], found)

    def test_automatic_tick_is_not_blocked_by_an_incomplete_uncertain_slot(self):
        from unittest.mock import patch
        from teamkit.operations import tick
        broken = slot('t7', 'uncertain', 'a' * 32)
        del broken['native_pid']
        runtime = self.runtime_with(broken)
        operation_fixtures.OperationsTest.enqueue(self, 'ONE')
        with patch('teamkit.workers.plans', return_value=[]):
            tick(self.config, self.state, runtime)
        queue = operation_fixtures.TicketStore(self.state)
        try:
            self.assertNotIn('native_pid', json.dumps(queue.get('ONE'), ensure_ascii=False))
        finally:
            queue.close()

    def test_static_prepared_slot_is_not_an_executor(self):
        runtime = self.runtime_with(slot('t7','prepared','a'*32))
        self.assertEqual([],runtime.executors())

    def proof(self, provider='codex', **native):
        from teamkit.operations import prove_uncertain_slot
        executor = dict(terminal='t7', pid=700, provider=provider, root='/work/example', pool_phase='uncertain',
                        pool_launch_args=['--model', 'm'])
        body = dict(state='live', foreground=dict(pid=700, cwd='/work/example', argv=['codex', '--model', 'm']))
        for key, value in native.items():
            if key == 'foreground':
                body['foreground'] = {**body['foreground'], **value}
            else:
                body[key] = value
        with patch('teamkit.core_runtime._call', return_value=body):
            return prove_uncertain_slot(executor)

    def test_proven_uncertain_slot_passes(self):
        self.proof()

    def test_restored_session_resume_suffix_is_part_of_the_proof(self):
        sid = '62493f9a-3707-49e1-ba93-62a39019f96c'
        self.proof(foreground=dict(argv=['codex', '--model', 'm', 'resume', sid]))
        self.proof(provider='claude', foreground=dict(argv=['claude', '--model', 'm', '--resume', sid]))
        for provider, tail in (('codex', ['--resume', sid]), ('claude', ['resume', sid]),
                               ('codex', ['resume', 'not-a-session']), ('codex', ['resume', sid, '--extra'])):
            with self.subTest(provider=provider, tail=tail), self.assertRaisesRegex(ValueError, 'previous start result unknown'):
                self.proof(provider=provider, foreground=dict(argv=[provider, '--model', 'm', *tail]))

    def test_other_pid_argv_cwd_or_dead_terminal_stays_unknown(self):
        for change in (dict(foreground=dict(pid=701)), dict(foreground=dict(argv=['codex'])),
                       dict(foreground=dict(cwd='/elsewhere')), dict(state='finished')):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'previous start result unknown'):
                self.proof(**change)

    def test_unknown_reason_is_korean(self):
        self.assertIn('이전 시작', actions.reason_ko('pool slot uncertain: previous start result unknown'))
