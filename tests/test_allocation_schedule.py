"""KST ordering at the shared policy and real planner interfaces."""
from datetime import datetime
import unittest
from unittest.mock import patch
import test_workers as fixtures
from teamkit import allocation, workers
from teamkit.operations import NativeRuntime


class ScheduleTests(unittest.TestCase):
    setUp = fixtures.WorkerTests.setUp
    ticket = fixtures.WorkerTests.ticket

    def test_weekday_and_weekend_kst_order_in_policy_and_all_general_planners(self):
        cases = [('2026-11-06T14:59:00+00:00', ['claude/team','claude/personal','codex/team','codex/personal']),
                 ('2026-11-06T15:00:00+00:00', ['claude/personal','codex/personal','claude/team','codex/team'])]
        self.config['operations']['allocation_policy'] = 'usage'
        ticket = self.ticket()
        for instant, expected in cases:
            now = datetime.fromisoformat(instant).timestamp()
            for provider, accounts in self.config['profiles'].items():
                for account in accounts:
                    q = self.store.quota(provider+'/'+account)
                    q['observed_at'] = now
                    for k in ('five_hour','weekly'): q[k]['resets_at'] = now+300
                    self.store.quota(provider+'/'+account, q)
            with self.subTest(instant=instant), patch('time.time', return_value=now), patch.object(workers.providers,'identity'):
                pairs = lambda rows: [x['provider']+'/'+x['account'] for x in rows]
                self.assertEqual(expected, pairs(allocation.profile_candidates(self.config)))
                from teamkit.__main__ import select_execution
                selected = select_execution(self.config, self.store, {'team':'alpha','role':'leader'}, now)
                self.assertEqual(expected[0], selected['provider']+'/'+selected['account'])
                self.assertEqual(expected, pairs(workers.plans(self.config, self.state, ticket)))
                self.assertEqual(expected, pairs(NativeRuntime(self.config, self.state).executors(ticket)))
                limited = self.store.quota(expected[0]); limited['weekly']['used']=90
                self.store.quota(expected[0], limited)
                self.assertEqual(expected[1:], pairs(workers.plans(self.config, self.state, ticket)))
                self.assertEqual(expected[1:], pairs(NativeRuntime(self.config, self.state).executors(ticket)))
                limited['weekly']['used']=10; self.store.quota(expected[0],limited)
