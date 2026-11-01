"""Claude context capture uses local response evidence, never provider probes."""
import copy
import json
import os
import time
from unittest.mock import patch

from test_cli import Fixture
from teamkit.config import binding
from teamkit.context_handoff import capture_claude


class ClaudeContextCaptureTests(Fixture):
    def setUp(self):
        super().setUp()
        self.value['profiles']['claude'] = self.value['profiles'].pop('codex')
        self.value['teams']['alpha']['roles']['implementer']['provider'] = 'claude'
        self.value['runtime_backend'] = 'core'
        self.value['quota_ttl_seconds'] = 120
        self.value['context_handoff'] = {'enabled': True, 'threshold': 90, 'interval': 10}
        self.state.parent.mkdir()
        self.now = time.time()
        self.context_directory = self.state.parent / 'context-handoffs'
        self.observation = self.context_directory / 'observation-t1.json'
        environment = patch.dict(os.environ, {'HC_TERMINAL_ID': 't1', 'TEAMKIT_OWNER': 'fixture-owner',
                                             'CLAUDE_CONFIG_DIR': str(self.root / 'team')})
        environment.start()
        self.addCleanup(environment.stop)

    def raw(self, used=90, at=None, session='fixture-session'):
        raw = self.native_statusline(self.now-1 if at is None else at, session)
        raw['context_window']['used_percentage'] = used
        return raw

    def capture(self, raw, now=None):
        with patch('teamkit.context_handoff.time.time', return_value=self.now if now is None else now), \
                patch('teamkit.providers.identity', side_effect=AssertionError('unexpected provider identity query')) as identity:
            result = capture_claude(self.value, self.state, 'team', raw)
        identity.assert_not_called()
        return result

    def packet(self):
        return json.loads(self.observation.read_text())

    def test_real_response_anchor_and_compaction_latch_keep_original_crossing_time(self):
        self.capture(self.raw())
        initial = self.packet()
        self.assertEqual(initial['used'], 90)
        self.assertAlmostEqual(initial['observed_at'], self.now-1, places=5)
        self.assertEqual(initial['session_id'], 'fixture-session')
        self.assertEqual(initial['owner'], 'fixture-owner')
        self.assertEqual(initial['binding'], binding(self.value['profiles']['claude']['team']))
        self.assertEqual(initial['source'], 'claude-statusline-response')
        self.capture(self.raw(5, at=self.now+9), now=self.now+10)
        self.assertEqual(self.packet(), initial)
        self.capture(self.raw(5, at=self.now+129), now=self.now+130)
        self.assertEqual(self.packet()['used'], 5)
        self.assertAlmostEqual(self.packet()['observed_at'], self.now+129, places=5)
        stored = self.observation.read_text()
        self.assertNotIn('PRIVATE-CONTENT-FIXTURE', stored)
        self.assertNotIn('transcript_path', stored)

    def test_new_session_without_current_usage_invalidates_prior_crossing(self):
        self.capture(self.raw())
        new = self.raw(5, at=self.now, session='new-session')
        new['context_window'].pop('current_usage')
        self.capture(new)
        packet = self.packet()
        self.assertEqual(packet['session_id'], 'new-session')
        self.assertTrue(packet['unavailable'])
        self.assertIsNone(packet['used'])
        self.assertNotIn('observed_at', packet)

    def test_first_valid_callback_after_new_session_invalidation_is_accepted(self):
        self.capture(self.raw())
        new = self.raw(5, at=self.now, session='new-session')
        missing_usage = copy.deepcopy(new)
        missing_usage['context_window'].pop('current_usage')
        self.capture(missing_usage)
        self.capture(new)
        self.assertEqual(self.packet()['session_id'], 'new-session')
        self.assertEqual(self.packet()['used'], 5)
        self.assertNotIn('unavailable', self.packet())

    def test_zero_is_observed_and_unknown_is_not_inferred_from_usage_or_quota(self):
        self.capture(self.raw(0))
        self.assertEqual(self.packet()['used'], 0)
        self.observation.unlink()
        for used in (None, True, -1, 101, '90'):
            with self.subTest(used=used):
                self.capture(self.raw(used))
                self.assertFalse(self.observation.exists())
        raw = self.raw()
        raw['context_window'].pop('used_percentage')
        self.capture(raw)
        self.assertFalse(self.observation.exists())

    def test_stale_future_and_usage_mismatch_cannot_freshen_context(self):
        for at in (self.now-121, self.now+1):
            with self.subTest(at=at):
                self.capture(self.raw(at=at))
                self.assertFalse(self.observation.exists())
        raw = self.raw()
        raw['context_window']['current_usage'] = dict(raw['context_window']['current_usage'], input_tokens=999)
        self.capture(raw)
        self.assertFalse(self.observation.exists())
        self.capture(self.raw())
        before = self.observation.read_bytes()
        self.capture(self.raw(at=self.now-121))
        self.assertEqual(self.observation.read_bytes(), before)

    def test_wrong_environment_profile_missing_owner_and_non_core_are_ignored(self):
        raw = self.raw()
        for changes in ({'HC_TERMINAL_ID': ''}, {'HC_TERMINAL_ID': '../escape'},
                        {'TEAMKIT_OWNER': ''}, {'CLAUDE_CONFIG_DIR': str(self.root / 'personal')}):
            with self.subTest(changes=changes), patch.dict(os.environ, changes):
                self.capture(raw)
                self.assertFalse(self.observation.exists())
        self.value['runtime_backend'] = 'tui'
        self.capture(raw)
        self.assertFalse(self.observation.exists())

    def test_disabled_default_does_not_create_context_storage(self):
        self.value.pop('context_handoff')
        self.capture(self.raw())
        self.assertFalse(self.context_directory.exists())
        self.value['context_handoff'] = {'enabled': False, 'threshold': 90, 'interval': 10}
        self.capture(self.raw())
        self.assertFalse(self.context_directory.exists())
