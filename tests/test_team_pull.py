import json
import unittest
from unittest.mock import patch

import test_operations as fixtures
from teamkit import allocation, operations, queue_actions
from teamkit.tickets import TicketStore


class TeamPullTests(unittest.TestCase):
    setUp = fixtures.OperationsTest.setUp
    enqueue = fixtures.OperationsTest.enqueue

    def usage(self):
        config = json.loads(self.config.read_text())
        config['operations']['allocation_policy'] = 'usage'
        self.config.write_text(json.dumps(config))
        return config

    def test_kst_schedule_overrides_old_account_schedule(self):
        config = self.usage()
        config['account_selection'] = {'timezone':'Asia/Seoul', 'weekdays':['personal','team'], 'weekends':['personal','team']}
        from datetime import datetime
        weekday = datetime.fromisoformat('2026-11-04T00:00:00+09:00').timestamp()
        weekend = datetime.fromisoformat('2026-11-07T00:00:00+09:00').timestamp()
        self.assertEqual(['team','personal','team','personal'], [x['account'] for x in allocation.profile_candidates(config, weekday)])
        self.assertEqual(['personal','personal','team','team'], [x['account'] for x in allocation.profile_candidates(config, weekend)])

    def test_candidates_exclude_other_team_in_usage_mode(self):
        self.usage()
        self.enqueue('ONE')
        own = self.runtime.executor
        self.runtime.executors = lambda ticket: [dict(own, product='foreign'), own]
        result = queue_actions.candidates(self.config, self.state, 'ONE', runtime=self.runtime)
        self.assertEqual(1, len(result['candidates']))

    def test_company_candidates_ignore_old_personal_designation(self):
        self.usage()
        self.enqueue('ONE', profiles=['codex/personal'], execution_constraint={'provider':'codex','account':'personal'})
        own = self.runtime.executor
        company = dict(own, account='team', owner='company', terminal='t2', pid=43)
        self.runtime.executors = lambda ticket: [own, company]
        result = queue_actions.candidates(self.config, self.state, 'ONE', runtime=self.runtime)
        self.assertEqual(['team', 'personal'], [x['account'] for x in result['candidates']])
        self.assertTrue(all(x['preferred'] is False for x in result['candidates']))

    def test_pull_selected_later_ticket_preserves_other_work(self):
        self.usage()
        self.enqueue('OLD', priority=10)
        self.enqueue('LATER')
        with patch('teamkit.allocation.refresh_usage'), patch('teamkit.operations.reconcile') as reconcile:
            result = operations.pull(self.config, self.state, 'example', ticket='LATER', runtime=self.runtime)
        self.assertEqual(['LATER'], [x['ticket'] for x in result['started']])
        reconcile.assert_called_once()
        self.assertEqual('example',reconcile.call_args.kwargs['team'])
        queue = TicketStore(self.state)
        try:
            self.assertEqual('pending', queue.get('OLD')['state'])
        finally:
            queue.close()

    def test_blocked_head_does_not_stop_later_ready_ticket(self):
        self.usage()
        self.enqueue('OLD', priority=10)
        self.enqueue('LATER')
        def admit(ticket, executor):
            if ticket['id']=='OLD':
                raise ValueError('fixture head unavailable')
            return executor
        self.runtime.admit = admit
        with patch('teamkit.allocation.refresh_usage'):
            result = operations.tick(self.config, self.state, self.runtime)
        self.assertEqual(['LATER'], [x['ticket'] for x in result['started']])

    def test_cross_team_selected_ticket_rejected_before_dispatch(self):
        self.usage()
        self.enqueue('FOREIGN', product='foreign')
        with self.assertRaisesRegex(ValueError, 'team'):
            operations.pull(self.config, self.state, 'example', ticket='FOREIGN', runtime=self.runtime)
        self.assertEqual([], self.runtime.sent)

    def test_cli_exposes_team_and_optional_ticket_without_executor_selection(self):
        from teamkit.__main__ import parser
        args = parser().parse_args(['queue', 'pull', 'LATER', '--team', 'example'])
        self.assertEqual(('pull', 'example', 'LATER'), (args.action, args.team, args.ticket))
        self.assertIsNone(args.candidate_id)
