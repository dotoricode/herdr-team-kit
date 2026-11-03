import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from teamkit import admission, context_handoff
from teamkit.resources import PROFILES, ResourceLedger, ResourceUnavailable, validate_policy
from teamkit.store import Store


class UnlimitedCapacityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state = self.root / 'state.sqlite'
        self.store = Store(self.state)
        self.addCleanup(self.store.close)
        self.policy = {'global_limit': None}

    def observation(self):
        return dict(observed_at=time.time(), complete=True, terminals=[])

    def test_null_policy_round_trips_without_unlimiting_profiles(self):
        expected = validate_policy(self.policy)
        self.assertIsNone(expected['global_limit'])
        self.assertEqual(expected['profile_limits'], dict.fromkeys(PROFILES, 3))
        admission.configure(self.store.db, json.loads(json.dumps(self.policy)))
        self.assertEqual(admission.policy_for(None, self.store.db), (True, expected))
        self.assertEqual(validate_policy({'global_limit': 2})['profile_limits'], dict.fromkeys(PROFILES, 2))
        with self.assertRaises(ValueError):
            validate_policy({'global_limit': None, 'profile_limits': {'codex/team': None}})

    def reserve(self, ident, profile, resources=(), session=None):
        with self.store.transaction():
            return admission.reserve_in_transaction(self.store.db, ident, ident, profile, list(resources),
                                                     None, self.observation(), session)

    def test_more_than_three_profiles_admit_but_path_screen_profile_and_identity_still_reject(self):
        admission.configure(self.store.db, self.policy)
        for number, profile in enumerate(PROFILES):
            self.reserve('A' + str(number), profile, [dict(kind='path', name=str(self.root / str(number)), mode='write')])
        snap = admission.snapshot(self.store.db, native_observation=self.observation())
        self.assertEqual(snap['used'], 4)
        self.assertIsNone(snap['available'])
        self.assertIsNone(snap['global_limit'])
        with self.assertRaises(ResourceUnavailable) as error:
            self.reserve('PATH', 'codex/team', [dict(kind='path', name=str(self.root / '0' / 'child'), mode='write')])
        self.assertEqual(error.exception.code, 'resource_conflict')
        screen = [dict(kind='named', name='screen:fixture', mode='write')]
        self.reserve('SCREEN', 'codex/team', screen, dict(terminal='fixture', pid=12, owner='fixture-owner'))
        for ident, resources, session, code in [
                ('SCREEN2', screen, None, 'resource_conflict'),
                ('SESSION', [], dict(terminal='fixture', pid=12, owner='other'), 'session_conflict')]:
            with self.assertRaises(ResourceUnavailable) as error:
                self.reserve(ident, 'claude/team', resources, session)
            self.assertEqual(error.exception.code, code)
        self.reserve('THIRD', 'codex/team')
        with self.assertRaises(ResourceUnavailable) as error:
            self.reserve('FOURTH', 'codex/team')
        self.assertEqual(error.exception.code, 'profile_capacity')
        with self.assertRaises(ValueError):
            self.reserve('BAD', 'codex/unknown')
        with self.assertRaises(ResourceUnavailable) as error:
            with self.store.transaction():
                admission.reserve_in_transaction(self.store.db, 'STALE', 'STALE', 'claude/team', [],
                                                  None, dict(observed_at=0, complete=True, terminals=[]))
        self.assertEqual(error.exception.code, 'observation_unavailable')

    def test_legacy_claim_and_wake_use_null_stored_policy_and_context_handles_both_branches(self):
        self.store.create('WAKE', 'example', 'leader', str(self.root / 'wake'), 'a' * 40, 'Fixture')
        self.store.claim('WAKE', 'wake-owner', 'codex', 'personal', 'fixture', 'low')
        self.store.update('WAKE', 'wake-owner', 'launching', {})
        self.store.update('WAKE', 'wake-owner', 'submitted', {'terminal': 'wake', 'native_pid': 12})
        self.store.receipt('WAKE', 'wake-owner')
        self.store.park('WAKE', 'wake-owner')
        admission.configure(self.store.db, self.policy)
        with patch.object(admission, 'observe_native', side_effect=self.observation):
            for number, profile in enumerate(PROFILES):
                ident = 'LEGACY' + str(number)
                self.store.create(ident, 'example', 'implementer', str(self.root / ident), 'a' * 40, 'Fixture')
                provider, account = profile.split('/')
                self.store.claim(ident, ident + '-owner', provider, account, 'fixture', 'low')
            self.store.wake('WAKE', 'wake-owner')
            self.assertTrue(context_handoff.capacity_available(self.state))
            self.assertTrue(context_handoff.capacity_available(self.state, dict(kind='task', id='LEGACY0')))
        with patch.object(admission, 'observe_native', side_effect=RuntimeError('offline')):
            self.assertFalse(context_handoff.capacity_available(self.state))
