from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from teamkit.store import Store
from teamkit.wake import listener


class StoreWakeTests(unittest.TestCase):
    """Real temporary SQLite and Unix datagrams; no model/HC/UI boundaries."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / 'state.sqlite'
        self.store = Store(self.state)
        self.addCleanup(self.store.close)
        for target in ('subprocess.run', 'subprocess.Popen', 'subprocess.call',
                       'subprocess.check_call', 'subprocess.check_output', 'os.kill',
                       'teamkit.core_runtime._call', 'teamkit.providers.identity'):
            guard = patch(target, side_effect=AssertionError('real process/HC/model forbidden: '+target))
            guard.start()
            self.addCleanup(guard.stop)

    @contextmanager
    def listeners(self):
        with listener(self.state, 'context') as context, listener(self.state, 'dispatch') as dispatch:
            yield context, dispatch

    def messages(self, sock):
        values = []
        while True:
            try:
                values.append(sock.recv(8192))
            except BlockingIOError:
                return values

    def assert_messages(self, sockets, count):
        for sock in sockets:
            self.assertEqual(self.messages(sock), [b'changed']*count)

    def read_quota(self, key):
        with closing(sqlite3.connect(self.state)) as db:
            row = db.execute('SELECT body FROM quotas WHERE profile=?', (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def test_quota_recovery_autocommit_notifies_both_consumers_after_persistence(self):
        self.store.quota('codex/team', {'used': 100})
        with self.listeners() as sockets:
            self.store.quota('codex/team', {'used': 10})
            self.assert_messages(sockets, 1)
            self.assertEqual(self.read_quota('codex/team'), {'used': 10})

    def test_transaction_quota_setters_wake_once_after_commit_never_before(self):
        with self.listeners() as sockets:
            with self.store.transaction():
                self.store.quota('codex/team', {'used': 10})
                self.store.quota('codex/personal', {'used': 20})
                self.assert_messages(sockets, 0)
                self.assertIsNone(self.read_quota('codex/team'))
            self.assert_messages(sockets, 1)
            self.assertEqual(self.read_quota('codex/team'), {'used': 10})

    def test_rollback_never_notifies_and_leaves_no_quota_change(self):
        with self.listeners() as sockets:
            with self.assertRaisesRegex(ValueError, 'fixture rollback'):
                with self.store.transaction():
                    self.store.quota('codex/team', {'used': 10})
                    raise ValueError('fixture rollback')
            self.assert_messages(sockets, 0)
            self.assertIsNone(self.read_quota('codex/team'))

    def test_failed_sqlite_commit_rolls_back_without_notifying(self):
        self.store.db.execute('PRAGMA foreign_keys=ON')
        self.store.db.execute('CREATE TABLE parent (id INTEGER PRIMARY KEY)')
        self.store.db.execute('CREATE TABLE child (parent_id INTEGER REFERENCES parent(id) '
                              'DEFERRABLE INITIALLY DEFERRED)')
        with self.listeners() as sockets:
            with self.assertRaises(sqlite3.IntegrityError):
                with self.store.transaction():
                    self.store.quota('codex/team', {'used': 10})
                    self.store.db.execute('INSERT INTO child VALUES (1)')
            self.assert_messages(sockets, 0)
            self.assertFalse(self.store.db.in_transaction)
            self.assertIsNone(self.read_quota('codex/team'))

    def test_getters_and_initialization_do_not_wake(self):
        self.store.create('EXAMPLE', 'example', 'leader', str(self.state.parent), 'a'*64, 'Synthetic task',
                          basis_kind='instructions')
        self.store.quota('codex/team', {'used': 10})
        with self.listeners() as sockets:
            other = Store(self.state)
            try:
                other.get('EXAMPLE')
                other.list()
                other.events('EXAMPLE')
                other.quota('codex/team')
                other.quota('codex/personal')
            finally:
                other.close()
            self.assert_messages(sockets, 0)

    def test_legacy_park_and_complete_wake_after_the_state_return_is_saved(self):
        for task, role in [('LEADER', 'leader'), ('WORKER', 'implementer')]:
            self.store.create(task, 'example', role, str(self.state.parent), 'a'*64, 'Synthetic task',
                              basis_kind='instructions')
            with self.store.transaction():
                self.store._save(dict(self.store.get(task), state='running', owner='fixture-owner',
                                      runtime={'identity_checked': True}), 'fixture_receipted')
        with self.listeners() as sockets:
            self.store.park('LEADER', 'fixture-owner')
            self.assert_messages(sockets, 1)
            self.assertEqual(self.store.get('LEADER')['state'], 'ready')
            self.store.complete('WORKER', 'fixture-owner', 'a'*64, ['fixture evidence'])
            self.assert_messages(sockets, 1)
            self.assertEqual(self.store.get('WORKER')['state'], 'awaiting_review')

    def test_notify_error_cannot_rollback_or_fail_a_committed_transaction(self):
        statements = []
        self.store.db.set_trace_callback(statements.append)
        with patch('teamkit.wake.notify', side_effect=RuntimeError('fixture notify failure')) as notify:
            with self.store.transaction():
                self.store.quota('codex/team', {'used': 10})
            notify.assert_called_once_with(self.store.path)
        self.assertEqual(self.read_quota('codex/team'), {'used': 10})
        self.assertIn('COMMIT', statements)
        self.assertNotIn('ROLLBACK', statements)

    def test_notify_error_keeps_the_quota_autocommit_result(self):
        with patch('teamkit.wake.notify', side_effect=OSError('fixture notify failure')) as notify:
            self.assertEqual(self.store.quota('codex/team', {'used': 10}), {'used': 10})
            notify.assert_called_once_with(self.store.path)
        self.assertEqual(self.read_quota('codex/team'), {'used': 10})

    def test_two_writers_keep_atomic_commits_and_one_event_per_commit(self):
        ready = threading.Barrier(2)
        first_inside = threading.Event()
        second_waiting = threading.Event()
        release_first = threading.Event()

        def writer(number):
            store = Store(self.state)
            try:
                ready.wait(timeout=3)
                if number == 2:
                    self.assertTrue(first_inside.wait(timeout=3))
                    second_waiting.set()
                with store.transaction():
                    store.quota('writer-'+str(number), {'value': number})
                    if number == 1:
                        first_inside.set()
                        self.assertTrue(release_first.wait(timeout=3))
            finally:
                store.close()

        with self.listeners() as sockets, ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(writer, 1)
            second = pool.submit(writer, 2)
            try:
                self.assertTrue(second_waiting.wait(timeout=3))
                self.assert_messages(sockets, 0)
                self.assertIsNone(self.read_quota('writer-1'))
                self.assertIsNone(self.read_quota('writer-2'))
            finally:
                release_first.set()
            first.result(timeout=3)
            second.result(timeout=3)
            self.assert_messages(sockets, 2)
            self.assertEqual(self.read_quota('writer-1'), {'value': 1})
            self.assertEqual(self.read_quota('writer-2'), {'value': 2})


if __name__ == '__main__':
    unittest.main()
