import json
from pathlib import Path
import tempfile
import unittest

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
        first = self.queue.enqueue(self.spec(), 'instruction-one')
        self.assertEqual(first['id'], self.queue.enqueue(self.spec(), 'instruction-one')['id'])
        with self.assertRaises(ValueError):
            self.queue.enqueue(self.spec(goal='Different instruction'), 'instruction-one')
        self.assertEqual(1, len(self.queue.list()))

    def test_child_cannot_expand_scope_or_inherit_new_feature_approval(self):
        self.queue.enqueue(self.spec(), 'parent')
        child = self.spec('T2', parent_id='T1', relation='required',
                          resources=[dict(kind='path', name='/unapproved', mode='write')],
                          scope=dict(paths=['/unapproved'], actions=['write'], data=['private']))
        saved = self.queue.enqueue(child, 'expanded-child')
        self.assertEqual('waiting_approval', saved['state'])
        self.assertEqual('pending', saved['approval']['state'])
        backlog = self.spec('T3', parent_id='T1', relation='improvement')
        self.assertEqual('backlog', self.queue.enqueue(backlog, 'optional-child')['state'])

    def test_required_child_blocks_parent_but_optional_child_does_not(self):
        self.queue.enqueue(self.spec(), 'parent')
        self.queue.enqueue(self.spec('T2', parent_id='T1', relation='required'), 'required')
        self.queue.enqueue(self.spec('T3', parent_id='T1', relation='improvement'), 'optional')
        self.assertEqual(['T2'], self.queue.incomplete_children('T1'))

    def test_dependency_cycle_and_unknown_dependency_are_rejected(self):
        self.queue.enqueue(self.spec(), 'one')
        self.queue.enqueue(self.spec('T2', dependencies=['T1']), 'two')
        with self.assertRaises(ValueError):
            self.queue.set_dependencies('T1', ['T2'])
        with self.assertRaises(ValueError):
            self.queue.set_dependencies('T1', ['missing'])

    def test_origin_followup_budget_applies_across_children(self):
        self.queue.enqueue(self.spec(budget=dict(tickets=2, attempts=3, tokens=500)), 'one')
        self.queue.enqueue(self.spec('T2', parent_id='T1', relation='required'), 'two')
        with self.assertRaisesRegex(ValueError, 'budget'):
            self.queue.enqueue(self.spec('T3', parent_id='T1', relation='defect'), 'three')
        self.assertEqual(2, len(self.queue.list()))

    def test_approval_file_change_blocks_readiness(self):
        self.queue.enqueue(self.spec(), 'one')
        self.assertIsNone(self.queue.gate('T1'))
        self.approval.write_text('Changed approval')
        self.assertIn('approval', self.queue.gate('T1'))

    def test_readiness_waits_for_dependencies_and_ages_priorities(self):
        self.queue.enqueue(self.spec(), 'one')
        self.queue.enqueue(self.spec('T2', dependencies=['T1']), 'two')
        self.assertIn('dependency', self.queue.gate('T2'))
        self.assertEqual(['T1'], [x['id'] for x in self.queue.ready()])


if __name__ == '__main__':
    unittest.main()
