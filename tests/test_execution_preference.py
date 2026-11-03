"""Real dispatch selection honors account priority without relaxing fixed work."""
import copy
from datetime import datetime
import json
import os
from unittest.mock import patch

from test_cli import Fixture, StoreContext
from teamkit.config import binding, load
from teamkit.policy import normalize_quota

SUNDAY = datetime.fromisoformat('2026-11-01T12:00:00+09:00').timestamp()
MONDAY = datetime.fromisoformat('2026-11-02T12:00:00+09:00').timestamp()


class ExecutionPreferenceTests(Fixture):
    def setUp(self):
        super().setUp()
        self.value['profiles']['claude'] = {
            account: {'home': str(self.root / ('claude-'+account)),
                      'identity': {'email': account+'@example.invalid'}, 'model': 'claude-profile', 'effort': 'low'}
            for account in ('team', 'personal')}
        for account, pr in self.value['profiles']['codex'].items():
            pr['model'] = 'codex-'+account
        self.value['teams']['alpha']['roles'] = {
            role: {'provider': 'claude', 'model': 'role-'+role, 'effort': 'medium'}
            for role in ('leader', 'implementer', 'reviewer')}
        self.value['routing'] = {'design': {'provider': 'codex', 'model': 'design-model', 'effort': 'high'}}
        self.value['execution_preference'] = {'provider_order': ['claude', 'codex'], 'weekend_account_first': True}
        self.value['account_selection'] = {'timezone': 'Asia/Seoul', 'weekdays': ['team', 'personal'], 'weekends': ['personal', 'team']}
        self.save()

    def save(self):
        self.config.write_text(json.dumps(self.value))

    def make(self, *extra):
        return self.call('create', 'T1', '--team', 'alpha', '--root', str(self.repo),
                         '--goal-file', str(self.root/'goal.txt'), *extra)

    def seed(self, now=SUNDAY, claude_personal=97, codex_personal=60):
        with StoreContext(self.state) as db:
            for provider in ('claude', 'codex'):
                for account in ('team', 'personal'):
                    used = (claude_personal if provider == 'claude' else codex_personal) if account == 'personal' else 0
                    if provider == 'codex':
                        quota = normalize_quota('codex', {'ordinaryUsageAllowed': True, 'rateLimits': {
                            'primary': {'windowDurationMins': 10080, 'usedPercent': used, 'resetsAt': now+1000},
                            'secondary': None}}, now)
                    else:
                        quota = {'observed_at': now, 'source': 'claude-statusline-response',
                                 'five_hour': {'used': used, 'resets_at': now+1000},
                                 'weekly': {'used': used, 'resets_at': now+1000}}
                    quota['binding'] = binding(self.value['profiles'][provider][account])
                    db.quota(provider+'/'+account, quota)

    def dispatch(self, now=SUNDAY, identity_effect=None):
        with patch.dict(os.environ, {'HERDR_ENV': '1'}), patch('teamkit.__main__.time.time', return_value=now), \
                patch('teamkit.providers.identity', side_effect=identity_effect) as identity, \
                patch('teamkit.__main__.Herdr') as herdr:
            herdr.return_value.launch.return_value = {'name': 'fixture', 'pane': 'w1:p1'}
            result = self.call('dispatch', 'T1')
            return result, identity, herdr

    def test_small_implementation_prefers_personal_codex_before_company_claude(self):
        self.value['routing']['small'] = {'provider': 'claude', 'model': 'haiku-fixture', 'effort': 'low'}
        self.save(); self.make('--work-kind', 'small'); self.seed()
        task, _, herdr = self.dispatch()
        self.assertEqual((task['provider'], task['account'], task['model'], task['effort']),
                         ('codex', 'personal', 'codex-personal', 'low'))
        spec = herdr.return_value.launch.call_args.args[1]
        self.assertIn('model_reasoning_effort="low"', spec['args'])

    def test_available_personal_claude_keeps_small_task_model_and_low_effort(self):
        self.value['routing']['small'] = {'provider': 'claude', 'model': 'haiku-fixture', 'effort': 'low'}
        self.save(); self.make('--work-kind', 'small'); self.seed(claude_personal=20)
        task, _, _ = self.dispatch()
        self.assertEqual((task['provider'], task['account'], task['model'], task['effort']),
                         ('claude', 'personal', 'haiku-fixture', 'low'))

    def test_actual_weekend_dispatch_uses_personal_codex_before_company_claude(self):
        self.make(); self.seed()
        task, identity, herdr = self.dispatch()
        self.assertEqual((task['provider'], task['account'], task['model'], task['effort']),
                         ('codex', 'personal', 'codex-personal', 'medium'))
        identity.assert_called_once()
        self.assertEqual(identity.call_args.args[0], 'codex')
        self.assertEqual(identity.call_args.args[1]['home'], self.value['profiles']['codex']['personal']['home'])
        spec = herdr.return_value.launch.call_args.args[1]
        self.assertIn('codex-personal', spec['args'])
        self.assertIn('model_reasoning_effort="medium"', spec['args'])

    def test_weekday_is_provider_first_and_no_preference_keeps_legacy_provider(self):
        self.make(); self.seed(MONDAY)
        task, _, _ = self.dispatch(MONDAY)
        self.assertEqual((task['provider'], task['account'], task['model']), ('claude', 'team', 'role-implementer'))

    def test_no_preference_preserves_legacy_provider_and_scheduled_account(self):
        self.value.pop('execution_preference'); self.save()
        self.make(); self.seed()
        task, _, _ = self.dispatch()
        self.assertEqual((task['provider'], task['account'], task['model']), ('claude', 'team', 'role-implementer'))

    def test_reviewers_keep_fixed_provider_and_model(self):
        self.make('--role', 'reviewer'); self.seed()
        task, _, _ = self.dispatch()
        self.assertEqual((task['provider'], task['account'], task['model']), ('claude', 'team', 'role-reviewer'))

    def test_risks_keep_role_provider_instead_of_personal_fallback(self):
        self.make('--risk', 'security'); self.seed()
        task, _, _ = self.dispatch()
        self.assertEqual((task['provider'], task['account'], task['model']), ('claude', 'team', 'role-implementer'))

    def test_design_and_user_decision_keep_explicit_provider_model(self):
        self.make('--role', 'leader', '--work-kind', 'design'); self.seed()
        task, _, _ = self.dispatch()
        self.assertEqual((task['provider'], task['account'], task['model']), ('codex', 'personal', 'design-model'))

    def test_explicit_user_decision_is_not_replaced(self):
        task = self.make(); self.seed()
        with StoreContext(self.state) as db:
            task['user_decision'] = {'provider': 'claude', 'model': 'chosen-model', 'effort': 'high', 'failure_count': 0}
            db._save(task, 'fixture_decision', {})
        task, _, _ = self.dispatch()
        self.assertEqual((task['provider'], task['account'], task['model'], task['effort']),
                         ('claude', 'team', 'chosen-model', 'high'))

    def test_all_personal_accounts_blocked_fall_back_to_company_claude(self):
        self.make(); self.seed(codex_personal=90)
        task, _, _ = self.dispatch()
        self.assertEqual((task['provider'], task['account']), ('claude', 'team'))

    def test_preflight_account_provider_change_stops_before_claim(self):
        self.make(); self.seed()
        def change(*args):
            with StoreContext(self.state) as db:
                quota = db.quota('codex/personal'); quota['unavailable'] = True
                db.quota('codex/personal', quota)
        with self.assertRaisesRegex(ValueError, 'changed during preflight'):
            self.dispatch(identity_effect=change)
        self.assertEqual(self.call('show', 'T1')['state'], 'queued')

    def test_plan_reports_actual_selection_without_launching(self):
        self.make(); self.seed()
        with patch('teamkit.__main__.time.time', return_value=SUNDAY), patch('teamkit.providers.identity') as identity:
            plan = self.call('plan', 'T1')
        self.assertEqual((plan['provider'], plan['account'], plan['model']), ('codex', 'personal', 'codex-personal'))
        identity.assert_not_called()
        self.assertEqual(self.call('show', 'T1')['state'], 'queued')

    def test_personal_claude_is_first_when_both_personal_accounts_eligible(self):
        self.make(); self.seed(claude_personal=20)
        task, _, _ = self.dispatch()
        self.assertEqual((task['provider'], task['account']), ('claude', 'personal'))

    def test_default_preference_schedule_uses_kst_personal_first_on_weekends(self):
        self.value.pop('account_selection'); self.save()
        self.make(); self.seed()
        task, _, _ = self.dispatch()
        self.assertEqual((task['provider'], task['account']), ('codex', 'personal'))

    def test_custom_schedule_timezone_and_weekend_order_are_respected(self):
        now = datetime.fromisoformat('2026-11-02T00:30:00+09:00').timestamp()
        self.value['account_selection']['timezone'] = 'UTC'; self.save()
        self.make(); self.seed(now)
        task, _, _ = self.dispatch(now)
        self.assertEqual((task['provider'], task['account']), ('codex', 'personal'))

    def test_disabled_weekend_account_first_preserves_provider_priority(self):
        self.value['execution_preference']['weekend_account_first'] = False; self.save()
        self.make(); self.seed()
        task, _, _ = self.dispatch()
        self.assertEqual((task['provider'], task['account']), ('claude', 'team'))

    def test_preflight_model_effort_or_profile_change_stops_before_claim(self):
        self.make(); self.seed()
        original = copy.deepcopy(self.value)
        for field in ('model', 'effort', 'profile'):
            def change(*args):
                if field == 'model':
                    self.value['profiles']['codex']['personal']['model'] = 'changed-profile-model'
                elif field == 'effort':
                    self.value['teams']['alpha']['roles']['implementer']['effort'] = 'high'
                else:
                    self.value['profiles']['codex']['personal']['home'] = str(self.root / 'changed-profile')
                self.save()
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'changed during preflight'):
                self.dispatch(identity_effect=change)
            self.assertEqual(self.call('show', 'T1')['state'], 'queued')
            self.value = copy.deepcopy(original); self.save(); self.seed()

    def test_custom_weekend_company_first_order_is_respected(self):
        self.value['account_selection']['weekends'] = ['team', 'personal']; self.save()
        self.make(); self.seed()
        task, _, _ = self.dispatch()
        self.assertEqual((task['provider'], task['account']), ('claude', 'team'))

    def test_company_codex_is_last_when_all_prior_candidates_blocked(self):
        self.make(); self.seed(codex_personal=90)
        with StoreContext(self.state) as db:
            quota = db.quota('claude/team'); quota['unavailable'] = True
            db.quota('claude/team', quota)
        task, _, _ = self.dispatch()
        self.assertEqual((task['provider'], task['account'], task['model']), ('codex', 'team', 'codex-team'))

    def test_fixed_review_provider_waits_when_its_accounts_unavailable(self):
        self.make('--role', 'reviewer'); self.seed()
        with StoreContext(self.state) as db:
            for account in ('team', 'personal'):
                quota = db.quota('claude/'+account); quota['unavailable'] = True
                db.quota('claude/'+account, quota)
        task, identity, herdr = self.dispatch()
        self.assertEqual(task['action'], 'wait')
        identity.assert_not_called(); herdr.assert_not_called()

    def test_all_candidates_unknown_stale_or_unbound_wait_without_identity(self):
        self.make(); self.seed()
        with StoreContext(self.state) as db:
            for provider in ('claude', 'codex'):
                for account in ('team', 'personal'):
                    quota = db.quota(provider+'/'+account)
                    if account == 'personal': quota['binding'] = 'obsolete'
                    else: quota['observed_at'] = SUNDAY-121
                    db.quota(provider+'/'+account, quota)
        task, identity, herdr = self.dispatch()
        self.assertEqual(task['action'], 'wait')
        identity.assert_not_called(); herdr.assert_not_called()

    def test_invalid_preference_is_rejected(self):
        for preference in (None, {}, {'provider_order': ['claude', 'claude'], 'weekend_account_first': True},
                           {'provider_order': ['unknown'], 'weekend_account_first': True},
                           {'provider_order': ['claude', 'codex'], 'weekend_account_first': 1}):
            with self.subTest(preference=preference):
                self.value['execution_preference'] = preference; self.save()
                with self.assertRaises(ValueError): load(self.config)

    def test_leader_priority_dispatches_company_claude_even_on_weekend(self):
        self.value['leader_execution_preference'] = {'provider':'claude','account':'team',
            'model':'claude-opus-5-5','effort':'medium'}
        self.save(); self.make('--role','leader'); self.seed(claude_personal=20)
        task, _, _ = self.dispatch()
        self.assertEqual((task['provider'],task['account'],task['model'],task['effort']),
                         ('claude','team','claude-opus-5-5','medium'))

    def test_leader_priority_still_rejects_blocked_company_quota(self):
        self.value['leader_execution_preference'] = {'provider':'claude','account':'team',
            'model':'claude-opus-5-5','effort':'medium'}
        self.save(); self.make('--role','leader'); self.seed(claude_personal=20)
        with StoreContext(self.state) as db:
            value = db.quota('claude/team')
            value['five_hour']['used'] = 99
            db.quota('claude/team',value)
        task, _, _ = self.dispatch()
        self.assertEqual((task['provider'],task['account'],task['model']),
                         ('claude','personal','claude-opus-5-5'))
