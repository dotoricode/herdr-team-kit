import concurrent.futures
from pathlib import Path
import tempfile
import threading
import time
import hashlib
import json
import unittest
from unittest.mock import patch

from teamkit import admission
from teamkit.resources import ResourceLedger, ResourceUnavailable
from teamkit.store import Store
from teamkit import app_coordinator, context_handoff


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.sqlite'
        self.store = Store(self.path)
        self.addCleanup(self.store.close)

    def observation(self, rows=None):
        return {'observed_at': time.time(), 'complete': True, 'terminals': rows or []}

    def create(self, ident, role='leader', root=None):
        return self.store.create(ident, 'alpha', role, str(root or Path(self.temp.name) / ident),
                                 'a' * 40, 'Run the approved task')

    def claim(self, ident, owner=None):
        return self.store.claim(ident, owner or ident + '-owner', 'codex', 'team', 'fixture', 'low')

    def make_running(self, ident, owner=None):
        owner = owner or ident + '-owner'
        self.claim(ident, owner)
        self.store.update(ident, owner, 'launching', {})
        self.store.update(ident, owner, 'submitted', {'terminal': ident, 'native_pid': 11,
                                                   'name': 'native-' + ident, 'backend': 'core'})
        self.store.receipt(ident, owner)
        return self.store.get(ident)

    def native(self, terminal, state='idle'):
        return {'address': terminal, 'state': 'live',
                'foreground': {'name': 'codex', 'pid': 11},
                'status': {'state': state, 'app': 'codex'},
                'labels': {'teamkit.owner': terminal + '-owner'}}

    def test_disabled_mode_preserves_original_team_serialization(self):
        self.create('T1', 'implementer')
        self.create('T2', 'implementer')
        self.claim('T1')
        with self.assertRaises(ValueError):
            self.claim('T2')

    def test_opt_in_config_policy_and_database_policy_are_the_same_contract(self):
        config = {'operations': {'enabled': True, 'resource_policy': {'global_limit': 2}}}
        enabled, policy = admission.policy_for(config, self.store.db)
        self.assertTrue(enabled)
        self.assertEqual(policy['global_limit'], 2)
        admission.configure(self.store.db, policy)
        self.assertEqual(admission.policy_for(None, self.store.db), (True, policy))
        self.assertFalse(admission.policy_for({'operations': {'enabled': False}}, self.store.db)[0])

    def test_direct_claim_counts_outside_models_and_fails_closed_on_observation_error(self):
        self.create('T1')
        admission.configure(self.store.db, {'global_limit': 1})
        outside = [{'terminal': 'outside', 'provider': 'codex', 'account': 'personal', 'status': 'working'}]
        with patch.object(admission, 'observe_native', return_value=self.observation(outside)):
            with self.assertRaises(ResourceUnavailable):
                self.claim('T1')
        with patch.object(admission, 'observe_native', side_effect=RuntimeError('unavailable')):
            with self.assertRaises(RuntimeError):
                self.claim('T1')
        self.assertEqual(self.store.get('T1')['state'], 'queued')

    def test_shared_ticket_reservations_limit_legacy_claim_and_wake(self):
        self.create('T1')
        self.create('T2')
        self.make_running('T2')
        self.store.park('T2', 'T2-owner')
        admission.configure(self.store.db, {'global_limit': 1})
        ledger = ResourceLedger(self.store.db)
        ledger.reserve('new-ticket-attempt', 'NEW-TICKET', 'codex/team', [], None, self.observation())
        with patch.object(admission, 'observe_native', return_value=self.observation()):
            with self.assertRaises(ResourceUnavailable):
                self.claim('T1')
            with self.assertRaises(ResourceUnavailable):
                self.store.wake('T2', 'T2-owner')

    def test_same_team_disjoint_worktrees_run_but_shared_path_still_waits(self):
        for ident in ['T1', 'T2', 'T3']:
            self.create(ident, 'implementer', Path(self.temp.name) / ('one' if ident != 'T2' else 'two'))
        admission.configure(self.store.db, {'global_limit': 4})
        with patch.object(admission, 'observe_native', return_value=self.observation()):
            self.claim('T1')
            self.claim('T2')
            with self.assertRaises(ResourceUnavailable) as error:
                self.claim('T3')
        self.assertEqual(error.exception.code, 'resource_conflict')

    def test_launch_failure_preserves_reservation_and_attempt_binding(self):
        self.create('T1')
        admission.configure(self.store.db, {'global_limit': 1})
        with patch.object(admission, 'observe_native', return_value=self.observation()):
            task = self.claim('T1')
            attempt = task['runtime']['resource_attempt']
            self.store.update('T1', 'T1-owner', 'launching', {})
            self.store.update('T1', 'T1-owner', 'uncertain', {'reason': 'native launch unconfirmed'})
            self.assertEqual(self.store.get('T1')['runtime']['resource_attempt'], attempt)
            self.assertEqual(admission.snapshot(self.path, native_observation=self.observation())['available'], 0)

    def test_park_during_model_turn_retains_slot_until_actual_idle(self):
        self.create('T1')
        admission.configure(self.store.db, {'global_limit': 1})
        with patch.object(admission, 'observe_native', return_value=self.observation()):
            self.make_running('T1')
        with patch.object(admission, 'native_get', return_value=self.native('T1', 'working')):
            self.store.park('T1', 'T1-owner')
            self.assertEqual(admission.snapshot(self.path, native_observation=self.observation())['available'], 0)
        with patch.object(admission, 'native_get', return_value=self.native('T1')):
            self.assertEqual(admission.snapshot(self.path, native_observation=self.observation())['available'], 1)

    def test_write_lease_needs_explicit_stopped_writer_in_addition_to_idle(self):
        shared = Path(self.temp.name) / 'shared'
        self.create('T1', 'implementer', shared)
        self.create('T2', 'implementer', shared)
        admission.configure(self.store.db, {'global_limit': 2})
        with patch.object(admission, 'observe_native', return_value=self.observation()):
            self.make_running('T1')
        with patch.object(admission, 'native_get', return_value=self.native('T1')):
            self.store.complete('T1', 'T1-owner', 'b' * 40, ['Focused verification passed'])
            with patch.object(admission, 'observe_native', return_value=self.observation()):
                with self.assertRaises(ResourceUnavailable):
                    self.claim('T2')
                self.store.release_resources('T1', 'T1-owner', writer_stopped=True)
                self.claim('T2')

    def test_preexisting_uncertain_work_without_resource_record_still_consumes_capacity(self):
        self.create('OLD')
        self.claim('OLD')
        self.store.update('OLD', 'OLD-owner', 'uncertain', {})
        self.create('NEW')
        admission.configure(self.store.db, {'global_limit': 1})
        with patch.object(admission, 'observe_native', return_value=self.observation()):
            with self.assertRaises(ResourceUnavailable):
                self.claim('NEW')

    def test_parallel_legacy_claims_share_atomic_limit_with_configurable_policy(self):
        self.create('T1')
        self.create('T2')
        admission.configure(self.store.db, {'global_limit': 1})
        barrier = threading.Barrier(2)

        def one(ident):
            store = Store(self.path)
            try:
                barrier.wait()
                store.claim(ident, ident + '-owner', 'codex', 'team', 'fixture', 'low')
                return 'claimed'
            except ResourceUnavailable as error:
                return error.code
            finally:
                store.close()

        with patch.object(admission, 'observe_native', side_effect=lambda: self.observation()):
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                outcomes = list(pool.map(one, ['T1', 'T2']))
        self.assertEqual(outcomes.count('claimed'), 1)
        self.assertEqual(outcomes.count('global_capacity'), 1)

    def test_context_capacity_uses_configured_global_limit_and_fail_closed_observation(self):
        admission.configure(self.store.db, {'global_limit': 1})
        outside = [{'terminal': 'outside', 'provider': 'codex', 'account': 'personal', 'status': 'working'}]
        with patch.object(admission, 'observe_native', return_value=self.observation(outside)):
            self.assertFalse(context_handoff.capacity_available(self.path))
        with patch.object(admission, 'observe_native', side_effect=RuntimeError('unavailable')):
            self.assertFalse(context_handoff.capacity_available(self.path))

    def test_coordinator_production_entry_cannot_bypass_shared_capacity(self):
        root = Path(self.temp.name)
        private = root / 'private'
        coordinator_root = private / 'coordinator'
        coordinator_root.mkdir(parents=True)
        config = {'runtime_backend': 'core', 'private_plan_root': str(private),
                  'context_handoff': {'enabled': False, 'threshold': 90, 'interval': 10},
                  'coordinator': {'provider': 'codex', 'model': 'fixture', 'effort': 'low',
                                  'root': str(coordinator_root)},
                  'profiles': {'codex': {'team': {'home': str(root / 'profile'),
                                               'identity': {'email': 'person@example.invalid'},
                                               'model': 'fixture', 'effort': 'low'}}},
                  'quota_ttl_seconds': 120,
                  'operations': {'enabled': True, 'resource_policy': {'global_limit': 1}}}
        config_path = root / 'config.json'
        config_path.write_text(json.dumps(config))
        inventory = root / 'inventory.md'
        inventory.write_text('Fixture inventory')
        bindings = root / 'bindings.json'
        bindings.write_text(json.dumps({'inventory': str(inventory)}))
        packet = {'state': str(self.path), 'config': str(config_path), 'nonce': 'nonce-one',
                  'bindings': str(bindings),
                  'config_digest': hashlib.sha256(config_path.read_bytes()).hexdigest()}
        request = root / 'request.json'
        request.write_text(json.dumps(packet))
        app_coordinator.save(app_coordinator.record_path(self.path),
                             {'nonce': 'nonce-one', 'phase': 'recovering'})
        ResourceLedger(self.store.db).reserve('busy', 'BUSY', 'codex/team', [], None, self.observation())
        with patch.object(app_coordinator, 'load', return_value=config), \
             patch.object(app_coordinator, 'require_managed'), \
             patch.object(app_coordinator, 'choose_account', return_value='team'), \
             patch.object(app_coordinator.providers, 'identity'), \
             patch.object(admission, 'observe_native', return_value=self.observation()), \
             patch.object(app_coordinator, '_call') as native_call:
            with self.assertRaises(ResourceUnavailable):
                app_coordinator.run_job(request)
        native_call.assert_not_called()

    def test_observation_enrichment_is_idempotent(self):
        self.create('OLD')
        self.claim('OLD')
        first = admission.observe(self.store.db, self.observation())
        self.assertEqual(first, admission.observe(self.store.db, first))

    def test_changed_provider_with_reused_native_identity_cannot_release_reservation(self):
        self.create('T1')
        admission.configure(self.store.db, {'global_limit': 1})
        with patch.object(admission, 'observe_native', return_value=self.observation()):
            self.make_running('T1')
        changed = self.native('T1')
        changed['foreground']['name'] = 'claude'
        changed['status']['app'] = 'claude-code'
        with patch.object(admission, 'native_get', return_value=changed):
            self.store.park('T1', 'T1-owner')
            self.assertEqual(admission.snapshot(self.path, native_observation=self.observation())['available'], 0)

    def test_ticket_caller_cannot_override_stored_policy_or_reset_it_to_default(self):
        admission.configure(self.store.db, {'global_limit': 1})
        ResourceLedger(self.store.db).reserve('busy', 'BUSY', 'codex/team', [], None, self.observation())
        for policy, code in [(None, 'global_capacity'), ({'global_limit': 3}, 'policy_changed')]:
            with self.subTest(policy=policy), self.store.transaction():
                with self.assertRaises(ResourceUnavailable) as error:
                    admission.reserve_in_transaction(self.store.db, 'next', 'NEXT', 'codex/team', [],
                                                       policy, self.observation())
                self.assertEqual(error.exception.code, code)

    def test_handoff_does_not_bind_new_attempt_to_old_native_and_waits_for_old_turn(self):
        self.create('T1')
        admission.configure(self.store.db, {'global_limit': 3})
        checkpoint = {'head': 'a' * 40, 'dirty': False, 'verification': ['Scope saved'], 'next': 'Continue'}
        with patch.object(admission, 'observe_native', side_effect=lambda: self.observation()):
            self.make_running('T1')
            with patch.object(admission, 'native_get', return_value=self.native('T1', 'working')):
                self.store.handoff('T1', 'T1-owner', checkpoint, True)
                with self.assertRaises(ResourceUnavailable):
                    self.claim('T1', 'next-owner')
            with patch.object(admission, 'native_get', return_value=self.native('T1')):
                task = self.claim('T1', 'next-owner')
            reservation = next(r for r in ResourceLedger._rows(self.store.db)
                               if r['attempt'] == task['runtime']['resource_attempt'])
            self.assertIsNone(reservation['session'])
            self.store.update('T1', 'next-owner', 'launching',
                              {'terminal': 'new-terminal', 'native_pid': 12, 'backend': 'core'})


if __name__ == '__main__':
    unittest.main()
