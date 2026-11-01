"""Synthetic native context evidence, independent of account limits or live sessions."""
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from teamkit.context_observation import TAIL_BYTES, codex_observation, context_percent


class ContextPercentTests(unittest.TestCase):
    def test_percentages_and_remaining_fallback(self):
        for value in (0, 90, 100):
            with self.subTest(value=value):
                self.assertEqual(context_percent({'context_window': {'used_percentage': value}}), value)
                self.assertEqual(context_percent({'context_window': {'remaining_percentage': 100-value}}), value)
        self.assertEqual(context_percent({'context_window': {'used_percentage': 90, 'remaining_percentage': 99}}), 90)

    def test_invalid_or_unrelated_values_are_unknown(self):
        for value in (None, True, '90', -1, 101, float('nan'), float('inf'), 10**1000):
            with self.subTest(value=str(value)[:20]):
                self.assertIsNone(context_percent({'context_window': {'used_percentage': value, 'remaining_percentage': 10}}))
                self.assertIsNone(context_percent({'context_window': {'remaining_percentage': value}}))
        for raw in (None, [], {}, {'context_window': []}, {'rate_limits': {'used_percentage': 100}}):
            self.assertIsNone(context_percent(raw))


class CodexObservationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.cwd = str(self.home / 'workspace')
        self.session = 'fixture-session'
        self.path = self.home / 'sessions' / '2026' / '01' / ('rollout-fixture-' + self.session + '.jsonl')
        self.path.parent.mkdir(parents=True)
        self.meta = {'type': 'session_meta', 'payload': {'id': self.session, 'cwd': self.cwd}}
        self.event = {'type': 'event_msg', 'timestamp': '2026-01-02T03:04:05Z', 'payload': {
            'type': 'token_count', 'info': {'last_token_usage': {'total_tokens': 46200},
            'total_token_usage': {'total_tokens': 999999}, 'model_context_window': 50000}}}
        self.write()

    def write(self, records=None):
        self.path.write_text(''.join(json.dumps(row)+'\n' for row in (records if records is not None else [self.meta, self.event])))

    def observe(self):
        return codex_observation(self.home, self.session, self.cwd)

    def database(self, filename):
        with closing(sqlite3.connect(self.home / 'state_5.sqlite')) as db:
            db.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT)')
            db.execute('INSERT INTO threads VALUES (?, ?)', (self.session, str(filename)))
            db.commit()

    def test_last_usage_not_cumulative_and_original_event_time(self):
        os.utime(self.path, (1900000000, 1900000000))
        expected = {'session_id': self.session, 'used': 90.0, 'source': 'codex-rollout',
                    'observed_at': datetime.fromisoformat('2026-01-02T03:04:05+00:00').timestamp()}
        self.assertEqual(self.observe(), expected)
        self.assertNotIn('workspace', json.dumps(self.observe()))
        for tokens, percent in ((0, 0), (12000, 0), (46200, 90), (50000, 100)):
            self.event['payload']['info']['last_token_usage']['total_tokens'] = tokens
            self.write()
            self.assertEqual(self.observe()['used'], percent)

    def test_display_baseline_rounding_and_ninety_percent_boundary(self):
        info = self.event['payload']['info']
        for used, window, displayed in ((33200, 475000, 5),
                                         (46010, 50000, 89),
                                         (46011, 50000, 90),
                                         (46200, 50000, 90)):
            with self.subTest(used=used, window=window):
                info.update(last_token_usage={'total_tokens': used}, model_context_window=window)
                self.write()
                self.assertEqual(self.observe()['used'], displayed)

    def test_latest_matching_event_wins_not_other_sources(self):
        earlier = json.loads(json.dumps(self.event))
        earlier['payload']['info']['last_token_usage']['total_tokens'] = 1
        unrelated = {'type': 'response_item', 'payload': self.event['payload']}
        self.write([self.meta, earlier, self.event, unrelated])
        self.assertEqual(self.observe()['used'], 90)
        self.write([self.meta, unrelated])
        self.assertIsNone(self.observe())

    def test_recent_crossing_survives_later_compaction_without_refreshing_time(self):
        crossing = json.loads(json.dumps(self.event))
        low = json.loads(json.dumps(self.event))
        low['timestamp'] = '2026-01-02T03:04:15Z'
        low['payload']['info']['last_token_usage']['total_tokens'] = 12000
        self.write([self.meta, crossing, low])
        crossing_at = datetime.fromisoformat('2026-01-02T03:04:05+00:00').timestamp()
        self.assertEqual(self.observe()['used'], 0)
        result = codex_observation(self.home, self.session, self.cwd, since=crossing_at)
        self.assertEqual(result['used'], 90)
        self.assertEqual(result['observed_at'], crossing_at)
        result = codex_observation(self.home, self.session, self.cwd, since=crossing_at + 1)
        self.assertEqual(result['used'], 0)
        self.assertEqual(result['observed_at'], crossing_at + 10)
        self.write([self.meta, crossing, dict(crossing, payload={'type': 'token_count'})])
        self.assertIsNone(codex_observation(self.home, self.session, self.cwd, since=crossing_at))

    def test_latest_valid_crossing_and_invalid_since(self):
        crossing_at = datetime.fromisoformat('2026-01-02T03:04:05+00:00').timestamp()
        newer = json.loads(json.dumps(self.event))
        newer['timestamp'] = '2026-01-02T03:04:06Z'
        newer['payload']['info']['last_token_usage']['total_tokens'] = 50000
        low = json.loads(json.dumps(newer))
        low['timestamp'] = '2026-01-02T03:04:07Z'
        low['payload']['info']['last_token_usage']['total_tokens'] = 12000
        self.write([self.meta, self.event, newer, low])
        result = codex_observation(self.home, self.session, self.cwd, since=crossing_at)
        self.assertEqual(result['used'], 100)
        self.assertEqual(result['observed_at'], crossing_at + 1)
        for since in (True, 'bad', float('nan'), float('inf')):
            self.assertIsNone(codex_observation(self.home, self.session, self.cwd, since=since))
        self.write([self.meta, low])
        self.assertEqual(codex_observation(self.home, self.session, self.cwd, since=crossing_at)['used'], 0)

    def test_exact_database_mapping_is_read_only(self):
        self.database(self.path)
        before = (self.home / 'state_5.sqlite').read_bytes()
        self.assertEqual(self.observe()['used'], 90)
        self.assertEqual((self.home / 'state_5.sqlite').read_bytes(), before)
        self.assertFalse((self.home / 'state_5.sqlite-shm').exists())
        self.assertFalse((self.home / 'state_5.sqlite-wal').exists())

    def test_missing_database_row_falls_back_to_exact_filename(self):
        self.database(self.path)
        with closing(sqlite3.connect(self.home / 'state_5.sqlite')) as db:
            db.execute('UPDATE threads SET id = ?', ('other-session',))
            db.commit()
        self.assertEqual(self.observe()['used'], 90)
        self.path.rename(self.path.with_name('rollout-unrelated.jsonl'))
        self.assertIsNone(self.observe())

    def test_wrong_metadata_session_cwd_or_first_record(self):
        for records in ([dict(self.meta, payload={'id': 'other', 'cwd': self.cwd}), self.event],
                        [dict(self.meta, payload={'id': self.session, 'cwd': self.cwd+'-other'}), self.event],
                        [self.event, self.meta], [[], self.event]):
            with self.subTest(records=records):
                self.write(records)
                self.assertIsNone(self.observe())
        self.assertIsNone(codex_observation(self.home, '../escape', self.cwd))
        self.assertIsNone(codex_observation(self.home, self.session, 'relative'))

    def test_archived_mapping_and_search_budget(self):
        archived = self.home / 'archived_sessions' / self.path.name
        archived.parent.mkdir()
        self.path.rename(archived)
        self.database(archived)
        self.assertEqual(self.observe()['used'], 90)
        (self.home / 'state_5.sqlite').unlink()
        with patch('teamkit.context_observation.SEARCH_ENTRIES', 0):
            self.assertIsNone(self.observe())
        self.assertEqual(self.observe()['used'], 90)

    def test_unsafe_mapping_never_falls_back(self):
        outside = self.home / 'outside.jsonl'
        outside.write_bytes(self.path.read_bytes())
        self.database(outside)
        self.assertIsNone(self.observe())

    def test_symlink_file_and_directory_are_rejected(self):
        outside = self.home / 'outside.jsonl'
        self.path.rename(outside)
        self.path.symlink_to(outside)
        self.assertIsNone(self.observe())
        self.path.unlink()
        self.path.parent.rmdir()
        self.path.parent.symlink_to(self.home)
        self.assertIsNone(self.observe())

    def test_missing_malformed_partial_and_bad_latest_fail_closed(self):
        for data in ('', '{broken}\n', json.dumps(self.meta)+'\n'+json.dumps(self.event),
                     json.dumps(self.meta)+'\n{broken}\n'+json.dumps(self.event)+'\n'):
            with self.subTest(data=data):
                self.path.write_text(data)
                self.assertIsNone(self.observe())
        self.write([self.meta, self.event, {'type': 'event_msg', 'payload': {'type': 'token_count'}}])
        self.assertIsNone(self.observe())
        self.path.unlink()
        self.assertIsNone(self.observe())

    def test_invalid_usage_or_timestamp_does_not_use_file_mtime(self):
        for stamp in (None, 'bad', '2026-01-02T03:04:05'):
            self.event['timestamp'] = stamp
            self.write()
            self.assertIsNone(self.observe())
        self.event['timestamp'] = '2026-01-02T03:04:05Z'
        info = self.event['payload']['info']
        for used, window in ((-1, 50000), (50001, 50000), (46200, 0),
                             (True, 50000), (46200, None), (0, 12000), (1, 100)):
            info.update(last_token_usage={'total_tokens': used}, model_context_window=window)
            self.write()
            self.assertIsNone(self.observe())
        info.pop('last_token_usage')
        self.write()
        self.assertIsNone(self.observe())

    def test_only_bounded_tail_is_parsed(self):
        padding = {'type': 'response_item', 'payload': 'PRIVATE-FIXTURE-' + 'x'*TAIL_BYTES}
        self.write([self.meta, padding, self.event])
        self.assertEqual(self.observe()['used'], 90)
        self.write([self.meta, self.event, padding])
        self.assertIsNone(self.observe())

    def test_changed_file_fails_closed(self):
        original = os.fstat
        calls = 0
        def changing(fd):
            nonlocal calls
            calls += 1
            if calls == 2:
                with self.path.open('ab') as stream:
                    stream.write(b'{}\n')
            return original(fd)
        with patch('teamkit.context_observation.os.fstat', side_effect=changing):
            self.assertIsNone(self.observe())

    def test_duplicate_exact_filenames_are_ambiguous(self):
        duplicate = self.home / 'archived_sessions' / self.path.name
        duplicate.parent.mkdir()
        duplicate.write_bytes(self.path.read_bytes())
        self.assertIsNone(self.observe())
