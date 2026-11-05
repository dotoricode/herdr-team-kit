"""Staff policy checks preserve ticket approval and use fake native boundaries."""
import unittest
import time
from unittest.mock import patch

import test_workers
from teamkit import workers, operations, docsstaff
from teamkit.tickets import digest


class DocumentStaffTests(unittest.TestCase):
    setUp = test_workers.WorkerTests.setUp
    ticket = test_workers.WorkerTests.ticket

    def configure(self):
        self.config['operations'].update(
            departments={'docs': {'root': str(self.root), 'roles': {
                'implementer': {'provider': 'claude', 'model': 'writing-model', 'effort': 'medium'}}}},
            worker_roles={'documenter': 'implementer'},
            document_staff={'department': 'docs', 'profiles': ['codex/personal', 'claude/personal']})
        self.config['execution_preference'] = {'provider_order': ['claude', 'codex'], 'weekend_account_first': True}
        return self.ticket(role='documenter', kind='document', product='docs')

    def plans(self, ticket):
        with patch.object(workers.providers, 'identity'):
            return workers.plans(self.config, self.state, ticket)

    def save_attempt(self, ticket, executor, phase='uncertain'):
        self.queue._save_attempt(dict(id='previous-' + executor['owner'], ticket=ticket['id'],
                                     executor=executor, phase=phase))

    def test_existing_broad_approved_ticket_is_narrowed_without_rewriting_it(self):
        ticket = self.configure()
        before = digest(ticket)
        plans = self.plans(ticket)
        self.assertEqual({(p['provider'], p['account']) for p in plans},
                         {('codex', 'personal'), ('claude', 'personal')})
        self.assertEqual(digest(self.queue.get(ticket['id'])), before)
        self.assertEqual({p['policy_role'] for p in plans}, {'implementer'})
        self.assertEqual(plans[0]['provider'], 'claude')

    def test_busy_or_uncertain_slots_never_spawn_replacements(self):
        ticket = self.configure()
        initial = self.plans(ticket)
        for candidate in initial:
            if candidate['account'] == 'personal':
                self.save_attempt(ticket, candidate)
        self.assertEqual(self.plans(ticket), [])

    def test_completed_actor_is_reused_instead_of_replaced(self):
        ticket = self.configure()
        candidate = next(p for p in self.plans(ticket) if p['provider'] == 'codex' and p['account'] == 'personal')
        candidate.update(new=False, terminal='t100', pid=123)
        self.save_attempt(ticket, candidate, 'completed')
        self.assertNotIn('codex', {p['provider'] for p in self.plans(ticket)})

    def test_company_existing_executor_rejected_before_native_io(self):
        ticket = self.configure()
        runtime = operations.NativeRuntime(self.config, self.state)
        executor = dict(provider='claude', account='team', product='docs', roles=['documenter'])
        with self.assertRaisesRegex(ValueError, 'personal document staff'):
            runtime.admit(ticket, executor)

    def test_product_reviewer_fixed_policy_is_unchanged(self):
        self.configure()
        ticket = self.ticket(role='reviewer', kind='review', product='alpha',
            user_decision={'provider': 'claude', 'model': 'fixed-review-model', 'effort': 'high', 'failure_count': 0},
            execution_constraint={'provider': 'claude', 'account': 'team'})
        plans = self.plans(ticket)
        self.assertEqual({(p['provider'], p['account'], p['model']) for p in plans},
                         {('claude', 'team', 'fixed-review-model')})

    def another_ticket(self, original, ident, path):
        spec = {key: original[key] for key in (
            'product', 'role', 'kind', 'goal', 'completion', 'checks', 'scope',
            'profiles', 'resources', 'approval')}
        spec = {**spec, 'id': ident, 'resources': [{'kind': 'path', 'name': str(path), 'mode': 'write'}]}
        return self.queue.enqueue(spec, ident, user_approved=True)

    def reserve_staff(self, ticket, actor):
        return self.queue.reserve(ticket['id'], actor, {},
            {'observed_at': time.time(), 'complete': True, 'terminals': []},
            before_reserve=lambda item, executor: docsstaff.check(self.config, self.queue.db, item, executor))

    def test_two_profiles_reserve_distinct_files_but_a_third_actor_cannot_take_a_slot(self):
        original = self.configure()
        first = self.another_ticket(original, 'T-first', self.root / 'first.md')
        # The approved fixture allows both profiles and the containing directory.
        second = self.another_ticket(first, 'T-second', self.root / 'second.md')
        third = self.another_ticket(first, 'T-third', self.root / 'third.md')
        plans = self.plans(first)
        codex = next(p for p in plans if p['provider'] == 'codex')
        claude = next(p for p in self.plans(second) if p['provider'] == 'claude')
        third_actor = next(p for p in self.plans(third) if p['provider'] == 'codex')
        self.reserve_staff(first, codex)
        self.reserve_staff(second, claude)
        with self.assertRaisesRegex(ValueError, 'slot already exists'):
            self.reserve_staff(third, third_actor)
        self.assertEqual(self.queue.get(third['id'])['state'], 'pending')
        self.assertEqual(self.queue.get(third['id'])['attempt_count'], 0)

    def test_same_file_conflict_survives_staff_policy(self):
        first = self.configure()
        second = self.another_ticket(first, 'T-second', self.root / 'same.md')
        third = self.another_ticket(first, 'T-third', self.root / 'same.md')
        codex = next(p for p in self.plans(second) if p['provider'] == 'codex')
        claude = next(p for p in self.plans(third) if p['provider'] == 'claude')
        self.reserve_staff(second, codex)
        with self.assertRaisesRegex(ValueError, 'resource|conflict'):
            self.reserve_staff(third, claude)
        self.assertEqual(self.queue.get(third['id'])['attempt_count'], 0)

    def test_reserved_owner_can_revalidate_launcher_but_new_owner_cannot_replace_it(self):
        ticket = self.configure()
        candidate = next(p for p in self.plans(ticket) if p['provider'] == 'claude')
        self.save_attempt(ticket, candidate, 'reserved')
        with patch.object(workers.providers, 'identity'):
            rechecked = workers.plans(self.config, self.state, ticket, reserved_owner=candidate['owner'])
        self.assertIn('claude', {p['provider'] for p in rechecked})
        self.assertNotIn('claude', {p['provider'] for p in self.plans(ticket)})

    def test_completed_official_actor_is_listed_for_reuse(self):
        ticket = self.configure()
        actor = next(p for p in self.plans(ticket) if p['provider'] == 'codex')
        actor.update(new=False, terminal='t100', pid=123)
        self.save_attempt(ticket, actor, 'completed')
        completed = {**ticket, 'state': 'completed', 'attempt': 'previous-' + actor['owner']}
        self.queue._save(completed, 'fixture_completed')
        with patch('teamkit.session_pool._read', return_value={'slots': {}}), \
                patch.object(workers, 'plans', return_value=[]):
            candidates = operations.NativeRuntime(self.config, self.state).executors(ticket)
        self.assertIn(actor, candidates)

    def test_registered_staff_blocks_new_plans_without_using_display_labels(self):
        ticket = self.configure()
        candidate = next(p for p in self.plans(ticket) if p['provider'] == 'claude')
        self.config['operations']['executors'] = [candidate]
        self.assertNotIn('claude', {p['provider'] for p in self.plans(ticket)})
        with self.assertRaisesRegex(ValueError, 'slot already exists'):
            docsstaff.check(self.config, self.queue.db, ticket, {**candidate, 'owner': 'another-owner'})

    def test_qa_investigator_is_limited_without_expanding_specialist_review(self):
        self.configure()
        self.config['operations']['departments']['qa'] = {
            'root': str(self.root), 'roles': {'leader': {'provider': 'claude', 'model': 'general-model', 'effort': 'medium'}}}
        self.config['operations']['qa_staff'] = {'department': 'qa', 'roles': ['investigator']}
        ticket = self.ticket(role='investigator', kind='investigation', product='qa')
        self.assertEqual(docsstaff.policy(self.config, ticket),
                         {'department': 'qa', 'profiles': ['claude/personal']})
        plans = self.plans(ticket)
        self.assertEqual({(p['provider'], p['account']) for p in plans}, {('claude', 'personal')})

    def test_malformed_staff_policy_fails_closed(self):
        ticket = self.configure()
        self.config['operations']['document_staff']['profiles'].append('claude/team')
        with self.assertRaisesRegex(ValueError, 'document_staff'):
            self.plans(ticket)


if __name__ == '__main__':
    unittest.main()
