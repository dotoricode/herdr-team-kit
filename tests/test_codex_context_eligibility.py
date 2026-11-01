"""Authenticated native Codex allowance evidence and context boundaries."""
import unittest

from teamkit.policy import context_action, eligible, normalize_quota

NOW = 2000000000


def native(used=20):
    return {'ordinaryUsageAllowed': True, 'rateLimitsByLimitId': {'codex': {
        'primary': {'usedPercent': used, 'windowDurationMins': 10080, 'resetsAt': NOW+1000},
        'secondary': None}}}


class CodexEligibilityTests(unittest.TestCase):
    def test_explicit_allowance_accepts_weekly_only_native_window(self):
        result = normalize_quota('codex', native(), NOW)
        self.assertTrue(eligible(result, NOW))
        self.assertEqual(result['source'], 'codex-native-quota')
        self.assertIs(result['ordinary_usage_allowed'], True)
        self.assertEqual(result['not_applicable'], ['five_hour'])
        self.assertNotIn('five_hour', result)

    def test_low_percentages_cannot_override_explicit_spend_blocks(self):
        for position in ('top', 'bucket'):
            for key, value in (('spendControlReached', True),
                               ('rateLimitReachedType', 'workspace_member_credits_depleted')):
                raw = native()
                raw['rateLimitsByLimitId']['codex']['secondary'] = {
                    'usedPercent': 10, 'windowDurationMins': 300, 'resetsAt': NOW+1000}
                target = raw if position == 'top' else raw['rateLimitsByLimitId']['codex']
                target[key] = value
                with self.subTest(position=position, key=key):
                    self.assertFalse(eligible(normalize_quota('codex', raw, NOW), NOW))
        raw['ordinaryUsageAllowed'] = False
        self.assertFalse(eligible(normalize_quota('codex', raw, NOW), NOW))

    def test_unknown_or_malformed_allowance_does_not_infer_missing_windows(self):
        for value in (None, False, 'true', 1, {}, []):
            raw = native()
            raw['ordinaryUsageAllowed'] = value
            with self.subTest(value=value):
                self.assertFalse(eligible(normalize_quota('codex', raw, NOW), NOW))
        raw.pop('ordinaryUsageAllowed')
        self.assertFalse(eligible(normalize_quota('codex', raw, NOW), NOW))

    def test_explicit_false_blocks_even_complete_low_windows(self):
        raw = native()
        raw['ordinaryUsageAllowed'] = False
        raw['rateLimitsByLimitId']['codex']['secondary'] = {
            'usedPercent': 0, 'windowDurationMins': 300, 'resetsAt': NOW+1000}
        self.assertFalse(eligible(normalize_quota('codex', raw, NOW), NOW))

    def test_present_windows_still_require_below_threshold_and_fresh_reset(self):
        for used, reset in ((90, NOW+1000), (-1, NOW+1000), (True, NOW+1000),
                            (float('nan'), NOW+1000), (20, NOW), (20, None)):
            raw = native()
            window = raw['rateLimitsByLimitId']['codex']['primary']
            window.update(usedPercent=used, resetsAt=reset)
            self.assertFalse(eligible(normalize_quota('codex', raw, NOW), NOW))
        result = normalize_quota('codex', native(), NOW)
        self.assertFalse(eligible(result, NOW+121))
        self.assertFalse(eligible(result, NOW-1))

    def test_malformed_or_unknown_bucket_never_creates_exemptions(self):
        for rates in (None, [], 'bad', {}, {'other': {'primary': {}}},
                      {'codex': None}, {'codex': []}, {'codex': {}},
                      {'codex': {'primary': {'windowDurationMins': 15, 'usedPercent': 0, 'resetsAt': NOW+1}}}):
            raw = {'ordinaryUsageAllowed': True, 'rateLimitsByLimitId': rates}
            self.assertFalse(eligible(normalize_quota('codex', raw, NOW), NOW))
        raw = native()
        raw['rateLimitsByLimitId']['codex']['secondary'] = {'windowDurationMins': 300, 'usedPercent': 'bad'}
        self.assertFalse(eligible(normalize_quota('codex', raw, NOW), NOW))

    def test_not_applicable_requires_exact_source_allowance_and_valid_shape(self):
        base = {'observed_at': NOW, 'source': 'codex-native-quota',
                'ordinary_usage_allowed': True, 'not_applicable': ['five_hour'],
                'weekly': {'used': 10, 'resets_at': NOW+1000}}
        self.assertTrue(eligible(base, NOW))
        for changes in ({'source': 'claude-native-usage'}, {'source': None},
                        {'ordinary_usage_allowed': 1}, {'ordinary_usage_allowed': 'true'},
                        {'not_applicable': 'five_hour'}, {'not_applicable': ['five_hour', 'weekly']},
                        {'not_applicable': ['five_hour', 'other']}, {'not_applicable': ['five_hour', 'five_hour']},
                        {'five_hour': {'used': 100, 'resets_at': NOW+1000}}):
            with self.subTest(changes=changes):
                self.assertFalse(eligible(dict(base, **changes), NOW))

    def test_legacy_complete_windows_and_claude_policy_remain_supported(self):
        raw = {'rateLimits': {'primary': {'usedPercent': 1, 'windowDurationMins': 300, 'resetsAt': NOW+10},
                              'secondary': {'usedPercent': 2, 'windowDurationMins': 10080, 'resetsAt': NOW+10}}}
        self.assertTrue(eligible(normalize_quota('codex', raw, NOW), NOW))
        legacy = normalize_quota('codex', raw, NOW)
        legacy.pop('five_hour')
        self.assertFalse(eligible(legacy, NOW))
        claude = {'observed_at': NOW, 'source': 'claude-native-usage',
                  'five_hour': {'used': 1}, 'weekly': {'used': 2}}
        self.assertTrue(eligible(claude, NOW))

    def test_context_threshold_requires_safe_boundary_at_ninety(self):
        for used in (0, 60, 70, 79.99):
            self.assertEqual(context_action(used, True), 'keep')
        for used in (80, 89.99, 90, 100):
            self.assertEqual(context_action(used, False), 'prepare')
        self.assertEqual(context_action(90, True), 'handoff')
        self.assertEqual(context_action(100, True), 'handoff')
