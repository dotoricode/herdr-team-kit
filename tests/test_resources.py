import concurrent.futures
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from teamkit.resources import ResourceLedger, ResourceUnavailable, observe_native, validate_policy


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / 'ledger.sqlite'
        self.now = 100.0
        self.ledger = ResourceLedger(self.state, clock=lambda: self.now)
        self.addCleanup(self.ledger.close)

    def observation(self, terminals=None, **extra):
        return {'observed_at': self.now, 'complete': True,
                'terminals': terminals or [], **extra}

    def resource(self, name='repo', mode='write'):
        return {'kind': 'path', 'name': str(Path(self.temp.name) / name), 'mode': mode}

    def reserve(self, attempt='a1', ticket='T1', profile='codex/team',
                resources=None, policy=None, observation=None):
        return self.ledger.reserve(attempt, ticket, profile,
                                   resources if resources is not None else [self.resource()],
                                   policy, observation or self.observation())

    def test_defaults_and_invalid_policy_are_explicit(self):
        self.assertEqual(validate_policy()['global_limit'], 3)
        for policy in [{'global_limit': True}, {'global_limit': 0},
                       {'profile_limits': {'unknown/team': 2}},
                       {'observation_ttl_seconds': 0}, {'not_a_policy': 1}]:
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                validate_policy(policy)

    def test_atomic_shared_transaction_rolls_back_ticket_and_reservation_together(self):
        conn = self.ledger.connection
        conn.execute('CREATE TABLE test_tickets (id TEXT PRIMARY KEY)')
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('INSERT INTO test_tickets VALUES (?)', ('T1',))
        self.ledger.reserve_in_transaction(conn, 'a1', 'T1', 'codex/team',
                                           [self.resource()], None, self.observation())
        conn.rollback()
        self.assertEqual(conn.execute('SELECT count(*) FROM test_tickets').fetchone()[0], 0)
        self.assertEqual(self.ledger.snapshot(None, self.observation())['reserved'], 0)

    def test_parallel_claims_cannot_exceed_one_available_slot(self):
        self.ledger.close()
        barrier = threading.Barrier(2)

        def worker(number):
            ledger = ResourceLedger(self.state, clock=lambda: self.now)
            try:
                barrier.wait()
                ledger.reserve('a'+str(number), 'T'+str(number), 'codex/team',
                               [self.resource(str(number))], {'global_limit': 1}, self.observation())
                return 'reserved'
            except ResourceUnavailable as error:
                return error.code
            finally:
                ledger.close()

        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(worker, [1, 2]))
        self.assertEqual(outcomes.count('reserved'), 1)
        self.assertEqual(outcomes.count('global_capacity'), 1)

    def test_same_team_is_not_a_resource_and_disjoint_paths_can_run(self):
        self.reserve(resources=[self.resource('worktree-one')])
        self.reserve('a2', 'T2', resources=[self.resource('worktree-two')])
        self.assertEqual(self.ledger.snapshot(None, self.observation())['reserved'], 2)

    def test_path_overlap_and_named_device_conflict(self):
        self.reserve(resources=[self.resource('repo')])
        with self.assertRaises(ResourceUnavailable) as caught:
            self.reserve('a2', 'T2', resources=[self.resource('repo/file')])
        self.assertEqual(caught.exception.code, 'resource_conflict')
        device = {'kind': 'named', 'name': 'device:test-phone', 'mode': 'write'}
        self.reserve('a3', 'T3', resources=[device])
        with self.assertRaises(ResourceUnavailable):
            self.reserve('a4', 'T4', resources=[device])

    def test_readers_share_but_writer_waits(self):
        self.reserve(resources=[self.resource(mode='read')])
        self.reserve('a2', 'T2', resources=[self.resource(mode='read')])
        with self.assertRaises(ResourceUnavailable) as caught:
            self.reserve('a3', 'T3', resources=[self.resource(mode='write')])
        self.assertEqual(caught.exception.code, 'resource_conflict')

    def test_parent_returns_execution_slot_without_silently_releasing_write_lease(self):
        self.reserve()
        self.ledger.release('a1', execution_stopped=True, release_writes=False)
        self.assertEqual(self.ledger.snapshot({'global_limit': 1}, self.observation())['available'], 1)
        with self.assertRaises(ResourceUnavailable):
            self.reserve('child', 'CHILD', resources=[self.resource('repo/child')])
        with self.assertRaises(ValueError):
            self.ledger.release('a1', execution_stopped=True, release_writes=True)
        self.ledger.release('a1', execution_stopped=True, release_writes=True, writer_stopped=True)
        self.reserve('child', 'CHILD', resources=[self.resource('repo/child')])

    def test_expired_timestamp_never_reclaims_a_live_or_unknown_attempt(self):
        self.reserve()
        self.now = 10000
        snap = self.ledger.snapshot({'global_limit': 1}, self.observation())
        self.assertEqual(snap['available'], 0)
        with self.assertRaises(ValueError):
            self.ledger.release('a1', execution_stopped=False, release_writes=True, writer_stopped=True)

    def test_native_union_includes_outside_coordinator_review_docs_and_ui(self):
        self.reserve(resources=[])
        self.ledger.bind('a1', {'terminal': 't1', 'pid': 11, 'owner': 'owner-one'})
        obs = self.observation([
            {'terminal': 't1', 'pid': 11, 'provider': 'codex', 'account': 'team', 'status': 'working'},
            {'terminal': 'coordinator', 'provider': 'codex', 'account': 'personal', 'status': 'working'},
            {'terminal': 'review', 'provider': 'claude', 'account': 'team', 'status': 'working'},
            {'terminal': 'docs', 'provider': 'claude', 'account': 'personal', 'status': 'working'},
            {'terminal': 'ui', 'provider': 'codex', 'account': 'personal', 'status': 'working'},
            {'terminal': 'shell', 'model': False, 'status': 'unknown'},
        ])
        snap = self.ledger.snapshot({'global_limit': 8}, obs)
        self.assertEqual(snap['used'], 5)
        self.assertEqual(snap['outside_ledger'], 4)
        self.assertEqual(snap['available'], 3)
        self.assertFalse(snap['outside_controlled'])
        self.assertIn('HC', snap['coverage'])

    def test_unknown_model_status_counts_conservatively_and_profile_cap_is_shared(self):
        self.reserve(resources=[])
        obs = self.observation([{'terminal': 'unknown', 'provider': 'codex', 'status': 'unknown'}])
        snap = self.ledger.snapshot(None, obs)
        self.assertEqual(snap['unknown_slots'], 1)
        with self.assertRaises(ResourceUnavailable) as caught:
            self.reserve('a2', 'T2', resources=[], policy={'global_limit': 5,
                         'profile_limits': {'codex/team': 2}}, observation=obs)
        self.assertEqual(caught.exception.code, 'profile_capacity')

    def test_stale_incomplete_future_and_duplicate_observations_fail_closed(self):
        observations = [self.observation(observed_at=1), self.observation(complete=False),
                        self.observation(observed_at=101),
                        self.observation([{'terminal': 't1', 'provider': 'codex', 'status': 'idle'},
                                          {'terminal': 't1', 'provider': 'codex', 'status': 'working'}])]
        for obs in observations:
            with self.subTest(observation=obs), self.assertRaises(ResourceUnavailable):
                self.reserve(observation=obs)

    def test_unknown_resource_and_implicit_permissions_are_rejected(self):
        for resource in [{'kind': 'unknown', 'name': 'x', 'mode': 'write'},
                         {'kind': 'path', 'name': 'relative', 'mode': 'write'},
                         {'kind': 'named', 'name': 'device:a'},
                         {'kind': 'named', 'name': 'device:a', 'mode': 'execute'}]:
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                self.reserve(resources=[resource])

    def test_idempotence_and_old_attempt_fencing(self):
        first = self.reserve()
        self.assertEqual(self.reserve(), first)
        with self.assertRaises(ValueError):
            self.reserve(ticket='OTHER')
        self.ledger.release('a1', execution_stopped=True, release_writes=True, writer_stopped=True)
        with self.assertRaises(ResourceUnavailable) as caught:
            self.reserve()
        self.assertEqual(caught.exception.code, 'attempt_released')
        self.reserve('a2')
        self.ledger.release('a1', execution_stopped=True, release_writes=True, writer_stopped=True)
        self.assertEqual(self.ledger.snapshot(None, self.observation())['reserved'], 1)

    def test_borrowed_connection_is_not_closed(self):
        conn = sqlite3.connect(':memory:', isolation_level=None)
        ledger = ResourceLedger(conn)
        ledger.close()
        self.assertEqual(conn.execute('SELECT 1').fetchone()[0], 1)
        conn.close()

    def test_native_binding_cannot_hide_a_changed_provider_or_owner(self):
        self.reserve(resources=[])
        self.ledger.bind('a1', {'terminal': 't1', 'pid': 11, 'owner': 'owner-one'})
        for extra in [{'provider': 'claude', 'owner': 'owner-one'},
                      {'provider': 'codex', 'owner': 'owner-two'}]:
            with self.subTest(extra=extra):
                row = {'terminal': 't1', 'pid': 11, 'account': 'team', 'status': 'working', **extra}
                snap = self.ledger.snapshot(None, self.observation([row]))
                self.assertEqual(snap['used'], 2)
                self.assertEqual(snap['outside_ledger'], 1)

    def test_adapter_measures_age_from_start_and_does_not_count_recovered_shell(self):
        clock = iter([100, 108])

        def call(*args):
            if args[0] == 'ls':
                return {'terminals': [{'address': 'shell', 'state': 'live', 'agent': 'codex'},
                                      {'address': 'model', 'state': 'live', 'agent': 'codex'}]}
            shell = args[1] == 'shell'
            return {'address': args[1], 'state': 'live',
                    'foreground': {'name': 'zsh' if shell else 'codex', 'pid': 11},
                    'status': {'app': 'codex', 'state': 'idle' if shell else 'unknown'},
                    'labels': {'teamkit.owner': 'owner-one'}}

        observation = observe_native(call, clock=lambda: next(clock))
        self.assertEqual(observation['observed_at'], 100)
        self.assertEqual(observation['finished_at'], 108)
        self.assertEqual([r['terminal'] for r in observation['terminals']], ['model'])
        self.assertEqual(observation['terminals'][0]['owner'], 'owner-one')

    def test_adapter_does_not_turn_rpc_failure_into_complete_empty_capacity(self):
        def failing_call(*args):
            raise RuntimeError('native unavailable')

        with self.assertRaises(RuntimeError):
            observe_native(failing_call)

    def test_one_native_terminal_cannot_be_reserved_by_two_live_attempts(self):
        session = {'terminal': 't1', 'pid': 11, 'owner': 'owner-one'}
        self.reserve(resources=[])
        self.ledger.bind('a1', session)
        with self.assertRaises(ResourceUnavailable) as caught:
            self.ledger.reserve('a2', 'T2', 'codex/team', [], None,
                                self.observation(), session=session)
        self.assertEqual(caught.exception.code, 'session_conflict')
        self.reserve('a2', 'T2', resources=[])
        with self.assertRaises(ResourceUnavailable):
            self.ledger.bind('a2', session)


if __name__ == '__main__':
    unittest.main()
