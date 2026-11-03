"""A designated account is a candidate-order priority, never an eligibility gate."""
import time
import unittest
from unittest.mock import patch

import test_operations as operation_fixtures
import test_workers as worker_fixtures
from teamkit import queue_actions as actions
from teamkit import workers
from teamkit.operations import tick
from teamkit.routing import execution_candidates


def routing_config():
    profiles = {p: {a: {'model': p + '-' + a + '-model'} for a in ('team', 'personal')} for p in ('codex', 'claude')}
    return {'profiles': profiles, 'teams': {'alpha': {'roles': {
        'implementer': {'provider': 'claude', 'model': 'claude-role-model', 'effort': 'medium'},
        'reviewer': {'provider': 'codex', 'model': 'review-model', 'effort': 'high'}}}}}


def pairs(candidates):
    return [(c['provider'], c['account']) for c in candidates]


class RoutingPriorityTest(unittest.TestCase):
    NOW = time.mktime((2026, 11, 3, 12, 0, 0, 0, 0, 0))  # a weekday

    def task(self, role='implementer', **extra):
        return dict(team='alpha', role=role, classification={'kind': 'general', 'risks': []}, **extra)

    def test_designated_account_comes_first_and_the_others_stay_candidates(self):
        config = routing_config()
        task = self.task(execution_constraint={'provider': 'claude', 'account': 'personal'})
        role = {'provider': 'claude', 'model': 'claude-role-model', 'effort': 'medium'}
        chosen = pairs(execution_candidates(config, task, role, self.NOW))
        self.assertEqual(('claude', 'personal'), chosen[0])
        self.assertIn(('claude', 'team'), chosen)

    def test_fixed_role_policy_skips_the_designation_but_keeps_its_own_accounts(self):
        config = routing_config()
        task = self.task('reviewer', execution_constraint={'provider': 'claude', 'account': 'team'})
        role = {'provider': 'codex', 'model': 'review-model', 'effort': 'high'}
        chosen = pairs(execution_candidates(config, task, role, self.NOW))
        self.assertEqual({('codex', 'team'), ('codex', 'personal')}, set(chosen))

    def test_user_decision_is_never_replaced_by_a_designation(self):
        config = routing_config()
        decision = {'provider': 'codex', 'model': 'fixed', 'effort': 'high', 'failure_count': 0}
        task = self.task(user_decision=decision, execution_constraint={'provider': 'claude', 'account': 'team'})
        role = {'provider': 'codex', 'model': 'fixed', 'effort': 'high'}
        self.assertEqual({'codex'}, {provider for provider, _ in pairs(execution_candidates(config, task, role, self.NOW))})

    def test_observer_pin_stays_exclusive(self):
        config = routing_config()
        task = self.task('leader', team_dispatch_team='alpha',
                         execution_constraint={'provider': 'codex', 'account': 'personal'})
        config['teams']['alpha']['roles']['leader'] = {'provider': 'codex', 'model': 'm', 'effort': 'low'}
        role = {'provider': 'codex', 'model': 'm', 'effort': 'low'}
        self.assertEqual([('codex', 'personal')], pairs(execution_candidates(config, task, role, self.NOW)))


class TicketAccountListTest(unittest.TestCase):
    setUp = operation_fixtures.OperationsTest.setUp
    enqueue = operation_fixtures.OperationsTest.enqueue

    def test_automatic_tick_sends_to_an_executor_outside_the_ticket_account_list(self):
        self.enqueue('ONE', profiles=['claude/team'])  # the fixture executor is codex/personal
        tick(self.config, self.state, self.runtime)
        tick(self.config, self.state, self.runtime)
        from teamkit.tickets import TicketStore
        store = TicketStore(self.state)
        try:
            self.assertEqual('completed', store.get('ONE')['state'])
        finally:
            store.close()
        self.assertEqual(1, len(self.runtime.sent))


class CandidatePreviewTest(unittest.TestCase):
    setUp = operation_fixtures.OperationsTest.setUp
    enqueue = operation_fixtures.OperationsTest.enqueue

    def two_executors(self):
        other = dict(self.runtime.executor, terminal='t2', pid=43, owner='other-owner',
                     provider='claude', account='team')
        self.runtime.executors = lambda ticket=None: [self.runtime.executor, other]

    def test_designated_account_is_listed_first_flagged_and_the_other_stays_eligible(self):
        self.enqueue('ONE', profiles=['claude/team'])
        self.two_executors()
        rows = actions.candidates(self.config, self.state, 'ONE', runtime=self.runtime)['candidates']
        self.assertEqual([('claude', 'team'), ('codex', 'personal')], [(r['provider'], r['account']) for r in rows])
        self.assertEqual([True, False], [r['preferred'] for r in rows])
        self.assertTrue(rows[1]['eligible'], rows[1]['reasons'])
        self.assertNotIn('account_list', [x['code'] for r in rows for x in r['reasons']])
        self.assertFalse(any('계정 목록' in r['reason_ko'] for r in rows))

    def test_no_designation_marks_nobody_preferred(self):
        self.enqueue('ONE', profiles=['codex/personal'], execution_constraint=None)
        rows = actions.candidates(self.config, self.state, 'ONE', runtime=self.runtime)['candidates']
        self.assertTrue(rows[0]['preferred'])  # profiles is itself a designation
        self.enqueue('TWO', profiles=['codex/team'])
        rows = actions.candidates(self.config, self.state, 'TWO', runtime=self.runtime)['candidates']
        self.assertFalse(rows[0]['preferred'])


class WorkerPlanPriorityTest(unittest.TestCase):
    setUp = worker_fixtures.WorkerTests.setUp
    ticket = worker_fixtures.WorkerTests.ticket

    def test_ticket_account_list_orders_plans_but_does_not_remove_other_accounts(self):
        ticket = self.ticket('implementer', 'investigation', profiles=['claude/personal'])
        with patch.object(workers.providers, 'identity'):
            plans = workers.plans(self.config, self.state, ticket)
        chosen = [(p['provider'], p['account']) for p in plans]
        self.assertEqual(('claude', 'personal'), chosen[0])
        self.assertIn(('claude', 'team'), chosen)


if __name__ == '__main__':
    unittest.main()
