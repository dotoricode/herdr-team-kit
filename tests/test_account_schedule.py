import unittest
from datetime import datetime, timezone

from teamkit.policy import account_order, choose_account
from test_cli import Fixture
import json
from unittest.mock import patch


class AccountScheduleTests(unittest.TestCase):
    def test_local_weekend_boundary(self):
        config = {'account_selection': {'timezone': 'Asia/Seoul',
                  'weekdays': ['team', 'personal'], 'weekends': ['personal', 'team']}}
        friday = datetime(2026, 10, 30, 14, 59, tzinfo=timezone.utc).timestamp()
        saturday = friday + 60
        self.assertEqual(account_order(config, friday), ('team', 'personal'))
        self.assertEqual(account_order(config, saturday), ('personal', 'team'))

    def test_preference_never_makes_exhausted_or_unknown_account_eligible(self):
        now = datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()
        def quota(used):
            return {'observed_at': now, 'five_hour': {'used': used, 'resets_at': now + 3600},
                    'weekly': {'used': used, 'resets_at': now + 86400}}
        order = ('personal', 'team')
        self.assertEqual(choose_account(quota(0), quota(0), now, order=order), 'personal')
        self.assertEqual(choose_account(quota(0), quota(90), now, order=order), 'team')
        self.assertEqual(choose_account(quota(0), None, now, order=order), 'team')
        self.assertIsNone(choose_account(quota(90), None, now, order=order))
        self.assertEqual(choose_account(quota(0), quota(95), now, active='personal', order=order), 'personal')

    def test_default_keeps_existing_order(self):
        self.assertEqual(account_order({}, 0), ('team', 'personal'))

    def test_invalid_schedule_fails(self):
        for schedule in [None, {}, {'timezone': 'Unknown/Zone', 'weekdays': ['team', 'personal'],
                                  'weekends': ['personal', 'team']},
                         {'timezone': 'UTC', 'weekdays': ['team', 'team'], 'weekends': ['personal', 'team']}]:
            with self.subTest(schedule=schedule), self.assertRaises(ValueError):
                account_order({'account_selection': schedule}, 0)


class ScheduledRouteTests(Fixture):
    def test_cli_route_respects_weekend_order_and_eligibility(self):
        self.value['account_selection'] = {'timezone': 'Asia/Seoul',
            'weekdays': ['team', 'personal'], 'weekends': ['personal', 'team']}
        self.config.write_text(json.dumps(self.value))
        now = datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()
        quota = {'observed_at': now, 'five_hour': {'used': 0, 'resets_at': now + 3600},
                 'weekly': {'used': 0, 'resets_at': now + 86400}}
        with patch('teamkit.__main__.time.time', return_value=now), patch('teamkit.__main__.cached_quota', return_value=quota):
            self.assertEqual(self.call('route', '--provider', 'codex'), {'account': 'personal', 'action': 'dispatch'})
        with patch('teamkit.__main__.time.time', return_value=now), patch('teamkit.__main__.cached_quota', return_value=None):
            self.assertEqual(self.call('route', '--provider', 'codex')['action'], 'wait')
