from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import copy
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import patch

from test_cli import Fixture
from teamkit.config import binding, load
from teamkit.store import Store
from teamkit.policy import eligible
from teamkit.runtime import RuntimeError
from teamkit.statusline import collect, peek_quota


class StatuslineTests(Fixture):
    def setUp(self):
        super().setUp()
        self.value['profiles']['claude'] = self.value['profiles'].pop('codex')
        self.value['teams']['alpha']['roles']['implementer']['provider'] = 'claude'
        self.config.write_text(json.dumps(self.value))
        self.cfg = load(self.config)
        self.env = patch.dict(os.environ, {'CLAUDE_CONFIG_DIR':str(self.root/'team')})
        self.env.start()
        self.addCleanup(self.env.stop)

    def capture(self, raw):
        with patch('teamkit.providers.identity') as identity:
            result = collect(self.cfg, self.state, 'team', raw)
        return result, identity

    def test_response_time_is_fixed_through_redraw_resume_and_expiry(self):
        at = time.time()-1
        raw = self.native_statusline(at)
        first, identity = self.capture(raw)
        self.assertEqual(first['status'], 'observed')
        self.assertAlmostEqual(first['quota']['observed_at'], at, places=5)
        identity.assert_called_once()
        before = self.state.read_bytes()
        with patch('teamkit.statusline.time.time', return_value=at+60):
            duplicate, identity = self.capture(raw)
        self.assertEqual(duplicate['status'], 'unchanged')
        identity.assert_not_called()
        self.assertEqual(self.state.read_bytes(), before)
        with patch('teamkit.statusline.time.time', return_value=at+121):
            resumed, identity = self.capture(raw)
        self.assertEqual(resumed['status'], 'unavailable')
        identity.assert_not_called()
        self.assertFalse(eligible(peek_quota(self.state, 'claude/team'), at+121))
        self.assertEqual(self.state.read_bytes(), before)

    def test_stale_future_naive_and_partial_sources_never_create_state(self):
        now = time.time()
        for at in (now-600, now+600):
            with self.subTest(at=at):
                result, identity = self.capture(self.native_statusline(at))
                self.assertEqual(result['status'], 'unavailable')
                identity.assert_not_called()
        raw = self.native_statusline()
        path = Path(raw['transcript_path'])
        row = json.loads(path.read_text())
        row['timestamp'] = datetime.now().isoformat()
        path.write_text(json.dumps(row)+'\n')
        self.assertEqual(self.capture(raw)[0]['status'], 'unavailable')
        raw = self.native_statusline()
        with Path(raw['transcript_path']).open('a') as stream:
            stream.write('{"type":"assistant"')
        self.assertEqual(self.capture(raw)[0]['status'], 'unavailable')
        self.assertFalse(self.state.exists())

    def test_newest_response_must_match_and_symlink_cannot_escape_home(self):
        raw = self.native_statusline()
        path = Path(raw['transcript_path'])
        row = json.loads(path.read_text())
        row['message']['usage']['input_tokens'] += 1
        with path.open('a') as stream:
            stream.write(json.dumps(row)+'\n')
        self.assertEqual(self.capture(raw)[0]['status'], 'unavailable')
        raw = self.native_statusline()
        outside = self.root/'outside.jsonl'
        outside.write_bytes(path.read_bytes())
        path.unlink(); path.symlink_to(outside)
        self.assertEqual(self.capture(raw)[0]['status'], 'unavailable')
        self.assertFalse(self.state.exists())

    def test_large_last_record_and_malformed_input_fail_closed(self):
        raw = self.native_statusline()
        path = Path(raw['transcript_path'])
        row = json.loads(path.read_text())
        row['message']['content'] = 'x'*(1024*1024+100)
        path.write_text(json.dumps(row)+'\n')
        self.assertEqual(self.capture(raw)[0]['status'], 'unavailable')
        for bad in ([], {'context_window':[]}, {'session_id':'../escape'}, {}):
            self.assertEqual(self.capture(bad)[0]['status'], 'unavailable')
        self.assertFalse(self.state.exists())

    def test_wrong_namespace_or_identity_preserves_existing_quota(self):
        raw = self.native_statusline()
        self.capture(raw)
        before = self.state.read_bytes()
        raw = self.native_statusline(time.time()-.1, 'second-session')
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR':str(self.root/'personal')}), patch('teamkit.providers.identity') as identity:
            with self.assertRaises(ValueError):
                collect(self.cfg, self.state, 'team', raw)
            identity.assert_not_called()
        with patch('teamkit.providers.identity', side_effect=RuntimeError('account mismatch')):
            with self.assertRaises(RuntimeError):
                collect(self.cfg, self.state, 'team', raw)
        self.assertEqual(self.state.read_bytes(), before)

    def test_identity_delay_does_not_create_freshness(self):
        now = time.time()
        raw = self.native_statusline(now-119)
        with patch('teamkit.statusline.time.time', side_effect=[now, now+2]), patch('teamkit.providers.identity'):
            result = collect(self.cfg, self.state, 'team', raw)
        self.assertEqual(result['status'], 'unavailable')
        self.assertFalse(self.state.exists())

    def test_missing_invalid_windows_are_unknown_and_private_payload_is_absent(self):
        raw = self.native_statusline()
        raw['rate_limits']['five_hour']['used_percentage'] = {'private':'PRIVATE-QUOTA-FIXTURE'}
        raw['transcript'] = 'PRIVATE-RAW-FIXTURE'
        result, _ = self.capture(raw)
        self.assertNotIn('five_hour', result['quota'])
        self.assertFalse(eligible(result['quota'], time.time()))
        for sentinel in (b'PRIVATE-QUOTA-FIXTURE', b'PRIVATE-RAW-FIXTURE', b'PRIVATE-CONTENT-FIXTURE', str(raw['transcript_path']).encode()):
            self.assertNotIn(sentinel, self.state.read_bytes())
        raw = self.native_statusline(time.time()-.1, 'missing-rate-session')
        raw.pop('rate_limits')
        result, _ = self.capture(raw)
        self.assertNotIn('five_hour', result['quota'])
        self.assertNotIn('weekly', result['quota'])

    def test_same_timestamp_samples_only_reduce_availability(self):
        raw = self.native_statusline()
        first, _ = self.capture(raw)
        raw['rate_limits']['five_hour']['used_percentage'] = 100
        second, _ = self.capture(raw)
        self.assertEqual(second['quota']['observed_at'], first['quota']['observed_at'])
        self.assertEqual(second['quota']['five_hour']['used'], 100)
        raw['rate_limits']['five_hour']['used_percentage'] = 0
        third, _ = self.capture(raw)
        self.assertEqual(third['quota']['five_hour']['used'], 100)
        raw['rate_limits']['five_hour']['resets_at'] += 1
        last, _ = self.capture(raw)
        self.assertNotIn('five_hour', last['quota'])

    def test_concurrent_delayed_callback_cannot_replace_newer_sample(self):
        now = time.time()
        old = self.native_statusline(now-2, 'older-session')
        new = self.native_statusline(now-1, 'newer-session')
        new['rate_limits']['seven_day']['used_percentage'] = 100
        started, release = threading.Event(), threading.Event()
        calls = []
        def identity(*args, **kwargs):
            calls.append(True)
            if len(calls) == 1:
                started.set()
                self.assertTrue(release.wait(3))
        with patch('teamkit.providers.identity', side_effect=identity), ThreadPoolExecutor(2) as executor:
            slow = executor.submit(collect, self.cfg, self.state, 'team', old)
            self.assertTrue(started.wait(3))
            fast = executor.submit(collect, self.cfg, self.state, 'team', new)
            self.assertEqual(fast.result(3)['status'], 'observed')
            release.set()
            self.assertEqual(slow.result(3)['status'], 'unchanged')
        quota = peek_quota(self.state, 'claude/team')
        self.assertAlmostEqual(quota['observed_at'], now-1, places=5)
        self.assertEqual(quota['weekly']['used'], 100)

    def test_real_cli_preserves_renderer_bytes_exit_and_collector_failure(self):
        cli = str(Path(__file__).resolve().parents[1]/'teamkit_cli.py')
        payload = b'{"private":"original stdin unchanged"}\n'
        display = shlex.join([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(b"\\x1b[32m"+sys.stdin.buffer.read()); sys.exit(7)'])
        result = subprocess.run([sys.executable, cli, '--config',str(self.root/'missing.json'),
                                 '--state',str(self.state),'statusline','--account','team',
                                 '--display-command',display], input=payload, capture_output=True)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, b'\x1b[32m'+payload)
        self.assertNotIn(b'original stdin', result.stderr)
        self.assertFalse(self.state.exists())

    def test_real_cli_preserves_renderer_signal_termination(self):
        cli = str(Path(__file__).resolve().parents[1]/'teamkit_cli.py')
        for sig in (signal.SIGTERM, signal.SIGPIPE, signal.SIGKILL):
            with self.subTest(signal=sig):
                result = subprocess.run([sys.executable, cli,
                    '--config', str(self.root/'missing.json'),
                    '--state', str(self.state), 'statusline', '--account', 'team',
                    '--display-command', f'kill -{sig.value} $$'],
                    input=b'{}', capture_output=True)
                self.assertEqual(result.returncode, -sig.value)
        self.assertFalse(self.state.exists())

    def test_legacy_unanchored_cache_cannot_authorize_dispatch(self):
        now = time.time()
        store = Store(self.state)
        try:
            store.quota('claude/team', {'binding':binding(self.cfg['profiles']['claude']['team']),
                        'observed_at':now,'five_hour':{'used':1,'resets_at':now+900},
                        'weekly':{'used':1,'resets_at':now+900}})
        finally:
            store.close()
        self.assertEqual(self.call('route','--provider','claude')['action'],'wait')
