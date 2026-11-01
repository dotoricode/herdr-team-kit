"""Context and current-account usage are independent handoff metrics."""
import copy
import json
from pathlib import Path
from unittest.mock import patch

from test_cli import Fixture
from teamkit.config import binding
from teamkit.handoff_trigger import trigger
from teamkit.policy import normalize_quota
from teamkit.store import Store

NOW = 2000000000


class HandoffTriggerTests(Fixture):
    def setUp(self):
        super().setUp()
        self.value['runtime_backend'] = 'core'
        self.value['quota_ttl_seconds'] = 120
        self.value['profiles']['claude'] = {
            account: {'home': str(self.root / ('claude-'+account)),
                      'identity': {'email': account+'@example.invalid'}, 'model': 'fixture'}
            for account in ('team', 'personal')}
        self.store = Store(self.state)
        self.addCleanup(self.store.close)
        self.target = {'provider': 'codex', 'account': 'personal', 'session_id': 'fixture-session'}
        self.identity = patch('teamkit.providers.identity', side_effect=AssertionError('no identity query'))
        self.identity_mock = self.identity.start()
        self.addCleanup(self.identity.stop)

    def context(self, used=90, **changes):
        return dict({'session_id': 'fixture-session', 'source': 'codex-rollout',
                     'used': used, 'observed_at': NOW-5}, **changes)

    def quota(self, five_hour=20, weekly=30, **changes):
        return dict({'source': 'codex-native-quota', 'observed_at': NOW-10,
                     'five_hour': {'used': five_hour, 'resets_at': NOW+1000},
                     'weekly': {'used': weekly, 'resets_at': NOW+1000}}, **changes)

    def seed(self, quota, provider=None, account=None):
        provider = provider or self.target['provider']
        account = account or self.target['account']
        quota = copy.deepcopy(quota)
        quota.setdefault('binding', binding(self.value['profiles'][provider][account]))
        self.store.quota(provider+'/'+account, quota)

    def call_trigger(self, context=None):
        with patch('teamkit.app_usage.time.time', return_value=NOW):
            result = trigger(self.value, self.store, self.target, context, NOW)
        self.identity_mock.assert_not_called()
        return result

    def test_fresh_native_context_precedes_high_account_usage_without_mutating_input(self):
        self.seed(self.quota(97))
        context = self.context()
        original = copy.deepcopy(context)
        result = self.call_trigger(context)
        self.assertEqual(result, dict(context, metric='context'))
        self.assertEqual(context, original)
        self.assertEqual(result['observed_at'], NOW-5)

    def test_low_context_with_high_five_hour_is_account_usage_not_context(self):
        self.seed(self.quota(97))
        self.assertEqual(self.call_trigger(self.context(18)), {
            'metric': 'five_hour', 'used': 97, 'observed_at': NOW-10,
            'source': 'codex-native-quota', 'provider': 'codex', 'account': 'personal'})

    def test_weekly_only_normalized_response_retains_metric_and_original_time(self):
        quota = normalize_quota('codex', {'ordinaryUsageAllowed': True, 'rateLimits': {
            'primary': {'windowDurationMins': 10080, 'usedPercent': 90, 'resetsAt': NOW+1000},
            'secondary': None}}, NOW-10)
        self.seed(quota)
        result = self.call_trigger()
        self.assertEqual((result['metric'], result['used'], result['observed_at']), ('weekly', 90, NOW-10))
        self.assertNotIn('session_id', result)

    def test_bad_or_unknown_context_falls_back_to_valid_usage(self):
        self.seed(self.quota(90))
        for context in (None, [], {}, self.context(None), self.context(True), self.context(101),
                        self.context(90, source='unknown'), self.context(90, source='claude-statusline-response'),
                        self.context(90, observed_at=NOW-121), self.context(90, observed_at=NOW+1),
                        self.context(90, session_id='../escape'), self.context(90, session_id='another-session')):
            with self.subTest(context=context):
                self.assertEqual(self.call_trigger(context)['metric'], 'five_hour')

    def test_context_exact_threshold_and_freshness_boundaries(self):
        self.assertIsNone(self.call_trigger(self.context(89.99)))
        self.assertEqual(self.call_trigger(self.context(90, observed_at=NOW-120))['metric'], 'context')
        self.assertEqual(self.call_trigger(self.context(100))['used'], 100)
        self.assertIsNone(self.call_trigger(self.context(90, observed_at=NOW-120.01)))
        self.assertIsNone(self.call_trigger(self.context(90, unavailable=True)))

    def test_account_binding_freshness_and_source_are_required(self):
        for changes in ({'binding': 'old-profile'}, {'source': 'unknown'}, {'source': []}, {'source': {}},
                        {'source': 'claude-native-usage'},
                        {'observed_at': NOW-121}, {'observed_at': NOW+1}, {'observed_at': True}):
            with self.subTest(changes=changes):
                self.seed(self.quota(97, **changes))
                self.assertIsNone(self.call_trigger())
        self.value['quota_ttl_seconds'] = 30
        self.seed(self.quota(90, observed_at=NOW-31))
        self.assertIsNone(self.call_trigger())
        self.seed(self.quota(90, observed_at=NOW-30))
        self.assertEqual(self.call_trigger()['metric'], 'five_hour')

    def test_invalid_windows_and_cumulative_tokens_never_become_percentages(self):
        for used in (None, True, '97', -1, 101, float('inf'), float('nan'), 10**1000):
            with self.subTest(used=str(used)[:20]):
                self.seed(self.quota(used, used))
                self.assertIsNone(self.call_trigger())
        self.seed({'source': 'codex-native-quota', 'observed_at': NOW,
                   'total_tokens': 999999, 'last_token_usage': {'total_tokens': 999999}})
        self.assertIsNone(self.call_trigger())
        for window in ({'used': 97}, {'used': 97, 'resets_at': NOW}, {'used': 97, 'resets_at': True}, []):
            quota = self.quota(); quota['five_hour'] = window
            self.seed(quota)
            self.assertIsNone(self.call_trigger())

    def test_explicit_codex_block_is_boolean_proof_not_fake_percentage(self):
        self.seed(self.quota(20, 30, ordinary_usage_allowed=False, unavailable=True))
        self.assertEqual(self.call_trigger(), {'metric': 'account_blocked', 'used': None, 'blocked': True,
            'observed_at': NOW-10, 'source': 'codex-native-quota', 'provider': 'codex', 'account': 'personal'})
        for changes in ({'ordinary_usage_allowed': None}, {'ordinary_usage_allowed': 0},
                        {'ordinary_usage_allowed': 'false'}, {'observed_at': NOW-121}, {'source': 'unknown'}):
            quota = self.quota(ordinary_usage_allowed=False, unavailable=True); quota.update(changes)
            self.seed(quota)
            self.assertIsNone(self.call_trigger())
        self.seed(self.quota(unavailable=True))
        self.assertIsNone(self.call_trigger())
        self.seed(self.quota(unavailable=True, spend_control_reached=True))
        self.assertIsNone(self.call_trigger())

    def test_other_account_exhaustion_does_not_trigger_current_account_handoff(self):
        self.seed(self.quota(100, 100), account='team')
        self.seed(self.quota())
        self.assertIsNone(self.call_trigger(self.context(18)))

    def test_claude_statusline_context_and_usage_sources(self):
        self.target['provider'] = 'claude'
        self.seed(self.quota(95, source='claude-statusline-response'))
        context = self.context(90, source='claude-statusline-response')
        self.assertEqual(self.call_trigger(context)['metric'], 'context')
        result = self.call_trigger(self.context(18, source='claude-statusline-response'))
        self.assertEqual((result['metric'], result['source']), ('five_hour', 'claude-statusline-response'))
        self.seed(self.quota(source='claude-statusline-response', ordinary_usage_allowed=False, unavailable=True))
        self.assertIsNone(self.call_trigger())

    def test_verified_native_claude_usage_without_reset_uses_original_probe_time(self):
        self.target['provider'] = 'claude'
        self.target['account'] = 'team'
        bound = binding(self.value['profiles']['claude']['team'])
        values = {'five_hour': 97, 'weekly': 40}
        attempt = {'cleaned': True, 'zero_token_usage': True, 'binding': bound,
            'nonce': 'a'*32, 'terminal': 't1', 'observed_at': NOW-10, 'attempt_at': NOW-11,
            'native': {'foreground_name': 'claude', 'foreground_pid': 123, 'status_app': 'claude-code',
                       'status_state': 'idle'}, 'values': values}
        observation = {'claude/team': dict(values, source='native-cli-manual-observation',
                                           observed_at=NOW-10, binding=bound)}
        for suffix, value in (('.native-usage-team.json', attempt), ('.usage-observations.json', observation)):
            path = Path(str(self.state)+suffix)
            path.write_text(json.dumps(value)); path.chmod(0o600)
        result = self.call_trigger()
        self.assertEqual((result['metric'], result['used'], result['source'], result['observed_at']),
                         ('five_hour', 97, 'claude-native-usage', NOW-10))
        self.assertEqual(result['account'], 'team')

    def test_missing_data_or_invalid_target_is_unknown(self):
        self.assertIsNone(self.call_trigger())
        for target in ({}, {'provider': 'unknown', 'account': 'personal'},
                       {'provider': 'codex', 'account': 'unconfigured'}, {'provider': [], 'account': 'personal'},
                       {'provider': 'codex', 'account': []}):
            self.assertIsNone(trigger(self.value, self.store, target, self.context(), NOW))
