import json
from pathlib import Path
import tempfile
import unittest
import time
from concurrent.futures import ThreadPoolExecutor

from teamkit.tickets import TicketStore


class TicketsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.approval = self.root / 'approval.txt'
        self.approval.write_text('User approves investigation of this fixture directory.')
        self.queue = TicketStore(self.root / 'state.sqlite')
        self.addCleanup(self.queue.close)

    def spec(self, ident='T1', **changes):
        value = dict(id=ident, product='example', role='investigator', kind='investigation',
                     goal='Inspect the fixture', completion=['report'], checks=['source'],
                     priority=0, dependencies=[], profiles=['codex/personal'],
                     resources=[dict(kind='path', name=str(self.root), mode='read')],
                     scope=dict(paths=[str(self.root)], actions=['read'], data=['fixture']),
                     approval=dict(state='approved', reference=str(self.approval)),
                     budget=dict(tickets=4, attempts=5, tokens=10000), estimated_tokens=100)
        value.update(changes)
        return value

    def test_duplicate_registration_and_conflicting_reuse(self):
        first = self.queue.enqueue(self.spec(), 'instruction-one', user_approved=True)
        self.assertEqual(first['id'], self.queue.enqueue(self.spec(), 'instruction-one', user_approved=True)['id'])
        with self.assertRaises(ValueError):
            self.queue.enqueue(self.spec(goal='Different instruction'), 'instruction-one')
        self.assertEqual(1, len(self.queue.list()))

    def test_child_cannot_expand_scope_or_inherit_new_feature_approval(self):
        self.queue.enqueue(self.spec(), 'parent', user_approved=True)
        child = self.spec('T2', parent_id='T1', relation='required',
                          resources=[dict(kind='path', name='/unapproved', mode='write')],
                          scope=dict(paths=['/unapproved'], actions=['write'], data=['private']))
        saved = self.queue.enqueue(child, 'expanded-child')
        self.assertEqual('waiting_approval', saved['state'])
        self.assertEqual('pending', saved['approval']['state'])
        backlog = self.spec('T3', parent_id='T1', relation='improvement')
        self.assertEqual('backlog', self.queue.enqueue(backlog, 'optional-child')['state'])

    def test_required_child_blocks_parent_but_optional_child_does_not(self):
        self.queue.enqueue(self.spec(), 'parent', user_approved=True)
        self.queue.enqueue(self.spec('T2', parent_id='T1', relation='required'), 'required')
        self.queue.enqueue(self.spec('T3', parent_id='T1', relation='improvement'), 'optional')
        self.assertEqual(['T2'], self.queue.incomplete_children('T1'))

    def test_dependency_cycle_and_unknown_dependency_are_rejected(self):
        self.queue.enqueue(self.spec(), 'one', user_approved=True)
        self.queue.enqueue(self.spec('T2', dependencies=['T1']), 'two')
        with self.assertRaises(ValueError):
            self.queue.set_dependencies('T1', ['T2'])
        with self.assertRaises(ValueError):
            self.queue.set_dependencies('T1', ['missing'])

    def test_origin_followup_budget_applies_across_children(self):
        self.queue.enqueue(self.spec(budget=dict(tickets=2, attempts=3, tokens=500)), 'one', user_approved=True)
        self.queue.enqueue(self.spec('T2', parent_id='T1', relation='required'), 'two')
        with self.assertRaisesRegex(ValueError, 'budget'):
            self.queue.enqueue(self.spec('T3', parent_id='T1', relation='defect'), 'three')
        self.assertEqual(2, len(self.queue.list()))

    def test_approval_file_change_blocks_readiness(self):
        self.queue.enqueue(self.spec(), 'one', user_approved=True)
        self.assertIsNone(self.queue.gate('T1'))
        self.approval.write_text('Changed approval')
        self.assertIn('approval', self.queue.gate('T1'))

    def test_readiness_waits_for_dependencies_and_ages_priorities(self):
        self.queue.enqueue(self.spec(), 'one', user_approved=True)
        self.queue.enqueue(self.spec('T2', dependencies=['T1']), 'two', user_approved=True)
        self.assertIn('dependency', self.queue.gate('T2'))
        self.assertEqual(['T1'], [x['id'] for x in self.queue.ready()])

    def test_changed_parent_approval_is_not_rebound_by_child_creation(self):
        self.queue.enqueue(self.spec(), 'parent', user_approved=True)
        self.approval.write_text('Different grant')
        child = self.queue.enqueue(self.spec('CHILD', parent_id='T1'), 'child')
        self.assertIsNotNone(self.queue.gate(child['id']))

    def test_child_verifier_change_does_not_inherit_parent_approval(self):
        self.queue.enqueue(self.spec(verification_commands={'source':['true']}), 'parent', user_approved=True)
        child = self.queue.enqueue(self.spec('CHILD', parent_id='T1',
            verification_commands={'source':['sh','-c','echo unapproved']}), 'child')
        self.assertEqual('waiting_approval', child['state'])

    def test_unapproved_create_is_durable_but_not_executable(self):
        self.queue.enqueue(self.spec(), 'one')
        self.assertEqual('waiting_approval', self.queue.get('T1')['state'])
        self.assertEqual([], self.queue.ready())

    def executor(self, terminal='t1', owner='owner-one'):
        return dict(terminal=terminal, pid=42, owner=owner, provider='codex', account='personal')

    def observation(self, executor=None):
        rows = [] if executor is None else [dict(executor, status='idle')]
        return dict(observed_at=time.time(), complete=True, terminals=rows)

    def test_concurrent_claim_has_one_attempt_and_one_reservation(self):
        self.queue.enqueue(self.spec(), 'one', user_approved=True)
        def claim():
            store = TicketStore(self.queue.path)
            try:
                return store.reserve('T1', self.executor(), {}, self.observation())['id']
            except ValueError:
                return None
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as workers:
            attempts = list(workers.map(lambda _: claim(), range(2)))
        self.assertEqual(1, sum(x is not None for x in attempts))
        self.assertEqual(1, self.queue.db.execute('SELECT COUNT(*) FROM operation_attempts').fetchone()[0])

    def test_parent_returns_execution_and_same_actor_can_continue_own_write_lease(self):
        from teamkit.resources import ResourceLedger
        root = self.spec(scope=dict(paths=[str(self.root)], actions=['read', 'write'], data=['fixture']),
                         resources=[dict(kind='path', name=str(self.root), mode='write')])
        self.queue.enqueue(root, 'one', user_approved=True)
        executor = self.executor()
        first = self.queue.reserve('T1', executor, {}, self.observation())
        native = dict(executor, status='idle')
        self.queue.receipt('T1', first['id'], executor['owner'], executor['terminal'], native)
        self.queue.enqueue(self.spec('T2', parent_id='T1'), 'two')
        self.queue.wait_children('T1', first['id'], executor['owner'], True, False)
        row = ResourceLedger._rows(self.queue.db)[0]
        self.assertFalse(row['execution_reserved'])
        self.assertTrue(row['writes_held'])
        # Complete the child fixture without impersonating a result-verification test.
        with self.queue.transaction():
            child = self.queue.get('T2')
            child['state'] = 'completed'
            self.queue._save(child, 'fixture_child_completed')
        self.queue.resume_parents()
        with self.assertRaisesRegex(ValueError, 'retained write lease'):
            self.queue.reserve('T1', self.executor('t2', 'another-owner'), {}, self.observation())
        second = self.queue.reserve('T1', executor, {}, self.observation(executor))
        rows = {x['attempt']:x for x in ResourceLedger._rows(self.queue.db)}
        self.assertFalse(rows[first['id']]['writes_held'])
        self.assertTrue(rows[second['id']]['writes_held'])
        with self.assertRaisesRegex(ValueError, 'stale result'):
            self.queue.submit('T1', first['id'], executor['owner'], {})

    def test_child_policy_change_requires_separate_approval(self):
        self.queue.enqueue(self.spec(execution_constraint={'account':'personal'}), 'parent', user_approved=True)
        child = self.queue.enqueue(self.spec('CHILD', parent_id='T1',
            execution_constraint={'account':'team'}), 'child')
        self.assertEqual('waiting_approval', child['state'])

    def test_observation_failure_does_not_reserve_or_complete(self):
        self.queue.enqueue(self.spec(), 'one', user_approved=True)
        with self.assertRaisesRegex(ValueError, 'observation'):
            self.queue.reserve('T1', self.executor(), {}, {'complete':False})
        self.assertEqual('pending', self.queue.get('T1')['state'])
        self.assertEqual(0, self.queue.db.execute('SELECT COUNT(*) FROM operation_attempts').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
