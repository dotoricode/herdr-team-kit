import unittest
from datetime import datetime, timezone

from teamkit.policy import account_order, choose_account
from test_cli import Fixture
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from teamkit.config import load


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

    def test_sunday_to_monday_boundary(self):
        config = {'account_selection': {'timezone': 'Asia/Seoul',
                  'weekdays': ['team', 'personal'], 'weekends': ['personal', 'team']}}
        sunday = datetime(2026, 11, 1, 14, 59, tzinfo=timezone.utc).timestamp()
        self.assertEqual(account_order(config, sunday), ('personal', 'team'))
        self.assertEqual(account_order(config, sunday + 60), ('team', 'personal'))

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


SUNDAY = datetime(2026, 11, 1, 14, 59, tzinfo=timezone.utc).timestamp()
SCHEDULE = {'timezone': 'Asia/Seoul', 'weekdays': ['team', 'personal'], 'weekends': ['personal', 'team']}


def eligible_quota(now):
    return {'observed_at': now, 'five_hour': {'used': 0, 'resets_at': now + 3600},
            'weekly': {'used': 0, 'resets_at': now + 86400}}


class ScheduledPathTests(Fixture):
    def test_config_load_rejects_invalid_account_selection(self):
        for schedule in [None, {}, {'timezone': 'Unknown/Zone', 'weekdays': ['team', 'personal'], 'weekends': ['personal', 'team']},
                         {**SCHEDULE, 'weekdays': ['team', 'team']}, {**SCHEDULE, 'extra': 1}]:
            with self.subTest(schedule=schedule):
                self.value['account_selection'] = schedule
                self.config.write_text(json.dumps(self.value))
                with self.assertRaises(ValueError):
                    load(self.config)
        self.value['account_selection'] = SCHEDULE
        self.config.write_text(json.dumps(self.value))
        self.assertEqual(load(self.config)['account_selection'], SCHEDULE)

    def test_cli_route_without_account_selection_keeps_company_first_on_weekend(self):
        self.assertNotIn('account_selection', self.value)
        with patch('teamkit.__main__.time.time', return_value=SUNDAY), \
                patch('teamkit.__main__.cached_quota', return_value=eligible_quota(SUNDAY)):
            self.assertEqual(self.call('route', '--provider', 'codex'), {'account': 'team', 'action': 'dispatch'})

    def test_cli_route_flips_at_sunday_monday_boundary(self):
        self.value['account_selection'] = SCHEDULE
        self.config.write_text(json.dumps(self.value))
        for now, account in ((SUNDAY, 'personal'), (SUNDAY + 60, 'team')):
            with self.subTest(now=now), patch('teamkit.__main__.time.time', return_value=now), \
                    patch('teamkit.__main__.cached_quota', return_value=eligible_quota(now)):
                self.assertEqual(self.call('route', '--provider', 'codex'), {'account': account, 'action': 'dispatch'})

    def dispatch_with_clock(self, clock):
        from teamkit.policy import choose_account as real
        orders = []
        def spy(*args, **kwargs):
            orders.append(kwargs['order'])
            return real(*args, **kwargs)
        with patch.dict(os.environ, {'HERDR_ENV': '1'}), patch('teamkit.__main__.time.time', side_effect=clock), \
                patch('teamkit.__main__.cached_quota', side_effect=lambda *a: eligible_quota(SUNDAY)), \
                patch('teamkit.__main__.choose_account', side_effect=spy), \
                patch('teamkit.providers.identity') as identity, patch('teamkit.__main__.Herdr') as herdr:
            herdr.return_value.launch.return_value = {'name': 'fixture', 'pane': 'w1:p1'}
            try:
                self.call('dispatch', 'T1')
            except Exception as error:
                return orders, identity, error
        return orders, identity, None

    def test_dispatch_second_check_uses_the_same_schedule(self):
        self.value['account_selection'] = SCHEDULE
        self.config.write_text(json.dumps(self.value))
        self.create()
        orders, identity, _ = self.dispatch_with_clock(lambda: SUNDAY)
        self.assertGreaterEqual(len(orders), 2)
        self.assertEqual(orders[:2], [('personal', 'team')] * 2)
        self.assertTrue(identity.call_args[0][1]['home'].endswith('personal'))

    def test_dispatch_fails_closed_when_order_changes_between_checks(self):
        self.value['account_selection'] = SCHEDULE
        self.config.write_text(json.dumps(self.value))
        self.create()
        ticks = iter([SUNDAY, SUNDAY] + [SUNDAY + 60] * 50)
        orders, _, error = self.dispatch_with_clock(lambda: next(ticks))
        self.assertEqual(orders[:2], [('personal', 'team'), ('team', 'personal')])
        self.assertIsInstance(error, ValueError)
        self.assertIn('quota changed during preflight', str(error))
        self.assertEqual(self.call('show', 'T1')['state'], 'queued')


class CoordinatorScheduleTests(unittest.TestCase):
    def test_recovery_uses_the_schedule_for_both_account_checks(self):
        from teamkit import app_coordinator
        from teamkit.policy import choose_account as real
        class Stop(Exception):
            pass
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = root / 'state.sqlite'
            state.touch()
            private = root / 'private'
            (private / 'cp').mkdir(parents=True)
            inventory = root / 'inventory.json'
            inventory.write_text('{}')
            bindings = root / 'bindings.json'
            bindings.write_text(json.dumps({'inventory': str(inventory)}))
            config = {'runtime_backend': 'core', 'quota_ttl_seconds': 120, 'account_selection': SCHEDULE,
                      'coordinator': {'provider': 'codex', 'root': str(private / 'cp'), 'model': 'fixture'},
                      'profiles': {'codex': {a: {'home': str(root / a), 'identity': {'email': a + '@example.invalid'}}
                                             for a in ('team', 'personal')}}}
            config_path = root / 'config.json'
            config_path.write_text('{}')
            import hashlib
            digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
            request = root / 'request.json'
            request.write_text(json.dumps({'config': str(config_path), 'state': str(state), 'bindings': str(bindings),
                                           'nonce': 'n1', 'config_digest': digest}))
            app_coordinator.save(app_coordinator.record_path(state), {'phase': 'recovering', 'nonce': 'n1'})
            orders = []
            def spy(*args, **kwargs):
                orders.append(kwargs['order'])
                return real(*args, **kwargs)
            with patch.object(app_coordinator, 'require_managed'), patch.object(app_coordinator, 'load', return_value=config), \
                    patch('teamkit.workflow.private_root', return_value=private.resolve()), \
                    patch.object(app_coordinator, 'cached_quota', side_effect=lambda *a: eligible_quota(SUNDAY)), \
                    patch.object(app_coordinator, 'choose_account', side_effect=spy), \
                    patch.object(app_coordinator.providers, 'identity') as identity, \
                    patch.object(app_coordinator, 'launch_spec', return_value={'env': {}, 'args': []}), \
                    patch.object(app_coordinator, '_call', side_effect=Stop), \
                    patch('teamkit.app_coordinator.time.time', return_value=SUNDAY):
                with self.assertRaises(Stop):
                    app_coordinator.run_job(request)
            self.assertEqual(orders, [('personal', 'team')] * 2)
            self.assertTrue(identity.call_args[0][1]['home'].endswith('personal'))
