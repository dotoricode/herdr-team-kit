import json
from pathlib import Path
import select
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from teamkit import context_handoff as context, wake


class DispatchWakeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state.sqlite'
        self.state.touch()
        self.config_path = self.root / 'config.json'
        self.config = {'runtime_backend': 'core', 'operations': {'enabled': True, 'consumer_workers': 2},
                       'context_handoff': {'enabled': True, 'threshold': 90, 'interval': 10}}
        for name, value in [('load', self.config), ('source_fingerprint', 'fixture-fingerprint')]:
            p = patch.object(context, name, return_value=value)
            p.start(); self.addCleanup(p.stop)
        p = patch.object(context, 'LOADED_SOURCE_FINGERPRINT', 'fixture-fingerprint')
        p.start(); self.addCleanup(p.stop)
        p = patch.object(context, '_call', side_effect=AssertionError('HC is forbidden'))
        p.start(); self.addCleanup(p.stop)

    def until(self, condition, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(.01)
        return False

    def test_notify_wakes_both_real_unix_datagram_channels(self):
        with wake.listener(self.state) as handoff, wake.listener(self.state, channel='dispatch') as dispatch:
            self.assertNotEqual(handoff.getsockname(), dispatch.getsockname())
            self.assertTrue(wake.notify(self.state))
            self.assertEqual({handoff, dispatch}, set(select.select([handoff, dispatch], [], [], .5)[0]))
            wake.wait(handoff, 5); wake.wait(dispatch, 5)

    def test_operations_wakes_while_the_context_scan_is_blocked(self):
        context_blocked = threading.Event(); release_context = threading.Event()
        first_dispatch = threading.Event(); next_dispatch = threading.Event()
        stop_source = threading.Event(); errors = []; options = []
        ticket = self.root / 'durable-ticket.json'
        def slow_context(*args, **kwargs):
            options.append(kwargs)
            context_blocked.set()
            release_context.wait(3)
            return {'enabled': True}
        def dispatch(*args):
            first_dispatch.set()
            if ticket.exists():
                json.loads(ticket.read_text())
                next_dispatch.set()
            return {'enabled': True}
        def fingerprint():
            return 'changed' if stop_source.is_set() else 'fixture-fingerprint'
        def serve():
            try: context.serve(self.config_path, self.state)
            except BaseException as error: errors.append(error)
        with patch.object(context, 'tick', side_effect=slow_context), \
             patch.object(context, '_operations_tick', side_effect=dispatch, create=True), \
             patch.object(context, 'source_fingerprint', side_effect=fingerprint):
            thread = threading.Thread(target=serve)
            thread.start()
            try:
                self.assertTrue(context_blocked.wait(1))
                self.assertTrue(first_dispatch.wait(1))
                began = time.monotonic()
                ticket.write_text('{"id":"READY"}')
                self.assertTrue(wake.notify(self.state))
                self.assertTrue(next_dispatch.wait(1), 'dispatch must not wait for the blocked context scan')
                self.assertLess(time.monotonic() - began, 1)
                self.assertTrue(thread.is_alive())
                self.assertFalse(release_context.is_set())
            finally:
                stop_source.set(); release_context.set(); wake.notify(self.state)
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertTrue(all(x.get('operations_consumer') is False for x in options))

    def test_dispatch_error_is_explicit_and_never_success(self):
        with patch.object(context, '_operations_tick', side_effect=ValueError('fixture observation unavailable'), create=True):
            with context.dispatch_consumer(self.config_path, self.state):
                self.assertTrue(self.until(lambda: context.dispatch_status(self.state)['phase'] == 'error'))
                saved = context.dispatch_status(self.state)
                self.assertFalse(saved['enabled'])
                self.assertIsNone(saved.get('last_success_at'))
                self.assertEqual('ValueError', saved['last_error']['type'])
                self.assertIn('observation unavailable', saved['last_error']['reason'])
        self.assertFalse(context.dispatch_status(self.state)['enabled'])

    def test_single_dispatch_thread_does_not_overlap_and_joins_before_return(self):
        active = 0; peak = 0; started = threading.Event(); release = threading.Event()
        def dispatch(*args):
            nonlocal active, peak
            active += 1; peak = max(peak, active); started.set()
            release.wait(2)
            active -= 1
            return {'enabled': True}
        with patch.object(context, '_operations_tick', side_effect=dispatch, create=True):
            with context.dispatch_consumer(self.config_path, self.state) as thread:
                try:
                    self.assertTrue(started.wait(1))
                    for _ in range(20): wake.notify(self.state)
                    self.assertEqual(1, peak)
                finally: release.set()
            self.assertFalse(thread.is_alive())
        self.assertEqual(0, active)
        self.assertEqual(1, peak)

    def test_source_change_stops_dispatch_without_adopting_tick_success(self):
        entered = threading.Event(); finish = threading.Event(); changed = threading.Event()
        def dispatch(*args):
            entered.set(); finish.wait(2)
            return {'enabled': True}
        with patch.object(context, '_operations_tick', side_effect=dispatch, create=True), \
             patch.object(context, 'source_fingerprint', side_effect=lambda: 'changed' if changed.is_set() else 'fixture-fingerprint'):
            with context.dispatch_consumer(self.config_path, self.state) as thread:
                try:
                    self.assertTrue(entered.wait(1))
                    changed.set(); finish.set(); wake.notify(self.state)
                    self.assertTrue(self.until(lambda: not thread.is_alive()))
                    status = context.dispatch_status(self.state)
                    self.assertTrue(status['reload_required'])
                    self.assertFalse(status['enabled'])
                    self.assertIsNone(status.get('last_success_at'))
                finally: finish.set()

    def test_once_preserves_the_existing_synchronous_tick(self):
        with patch.object(context, 'tick', return_value={'enabled': True}) as tick, \
             patch.object(context, 'dispatch_consumer', create=True) as consumer:
            self.assertEqual({'enabled': True}, context.serve(self.config_path, self.state, once=True))
        tick.assert_called_once()
        self.assertNotIn('operations_consumer', tick.call_args.kwargs)
        consumer.assert_not_called()

    def test_lost_wake_is_reconciled_by_the_five_second_periodic_scan(self):
        calls = []; repeated = threading.Event()
        def dispatch(*args):
            calls.append(time.monotonic())
            if len(calls) == 2: repeated.set()
            return {'enabled': True}
        with patch.object(context, '_operations_tick', side_effect=dispatch, create=True):
            with context.dispatch_consumer(self.config_path, self.state):
                self.assertTrue(repeated.wait(6), 'periodic dispatch must recover a lost wake')
        self.assertGreaterEqual(calls[1] - calls[0], 4.5)
        self.assertLess(calls[1] - calls[0], 6)

    def test_supervisor_holds_its_lifetime_lock_until_dispatch_has_drained(self):
        entered = threading.Event(); release = threading.Event(); changed = threading.Event(); errors = []
        def dispatch(*args):
            entered.set(); release.wait(3)
            return {'enabled': True}
        def serve():
            try: context.serve(self.config_path, self.state)
            except BaseException as error: errors.append(error)
        with patch.object(context, '_operations_tick', side_effect=dispatch, create=True), \
             patch.object(context, 'tick', return_value={'enabled': True}), \
             patch.object(context, 'source_fingerprint', side_effect=lambda: 'changed' if changed.is_set() else 'fixture-fingerprint'):
            supervisor = threading.Thread(target=serve)
            supervisor.start()
            try:
                self.assertTrue(entered.wait(1))
                changed.set(); wake.notify(self.state)
                with self.assertRaises(BlockingIOError):
                    context.serve(self.config_path, self.state, once=True)
            finally:
                release.set(); wake.notify(self.state); supervisor.join(3)
        self.assertFalse(supervisor.is_alive())
        self.assertEqual([], errors)
