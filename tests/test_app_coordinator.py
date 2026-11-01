import json
from pathlib import Path
import tempfile
import unittest
import hashlib
import time
from unittest.mock import patch
from test_cli import Fixture, StoreContext
from teamkit.config import binding
from teamkit.runtime import RuntimeError as KitError

from teamkit import app_coordinator


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.state=self.root/'state.sqlite';self.state.touch()
        self.config=self.root/'config.json';self.config.write_text('{}')
        self.link=self.root/'bindings.json';self.link.write_text('{}')
        self.bindings={'coordinator':{'terminal':'t6'}}

    def tearDown(self):self.temp.cleanup()

    def test_closed_target_is_recoverable_without_a_new_model(self):
        with patch('teamkit.app_coordinator.core_read',return_value={'terminals':[]}):
            value=app_coordinator.status(self.state,self.bindings)
        self.assertEqual(value['state'],'missing');self.assertTrue(value['can_recover'])

    def test_unavailable_core_is_not_treated_as_a_closed_target(self):
        with patch('teamkit.app_coordinator.core_read',side_effect=OSError('offline')):
            value=app_coordinator.status(self.state,self.bindings)
        self.assertEqual(value['state'],'unknown');self.assertFalse(value['can_recover'])

    def test_live_target_is_reused_without_starting_a_controller(self):
        live={'state':'live','terminal':'t6','can_recover':False}
        with patch('teamkit.app_coordinator.status',return_value=live),patch('teamkit.app_coordinator._call') as run:
            value=app_coordinator.start(self.config,self.state,self.link,True)
        self.assertEqual(value['terminal'],'t6');run.assert_not_called()

    def test_confirmation_is_required_before_recovery(self):
        with patch('teamkit.app_coordinator._call') as run:
            with self.assertRaises(ValueError):app_coordinator.start(self.config,self.state,self.link,False)
            run.assert_not_called()

    def test_pending_recovery_does_not_start_a_second_controller(self):
        pending={'state':'recovering','terminal':None,'can_recover':False}
        with patch('teamkit.app_coordinator.status',return_value=pending),patch('teamkit.app_coordinator._call') as run:
            with self.assertRaises(ValueError):app_coordinator.start(self.config,self.state,self.link,True)
            run.assert_not_called()

    def test_recovery_intent_is_saved_before_controller_creation(self):
        missing={'state':'missing','terminal':'t6','can_recover':True}
        observed=[]
        def native(*args):
            observed.append(json.loads(app_coordinator.record_path(self.state).read_text()))
            return {'address':'t7'}
        with patch('teamkit.app_coordinator.status',return_value=missing),patch('teamkit.app_coordinator._call',side_effect=native):
            value=app_coordinator.start(self.config,self.state,self.link,True)
        self.assertEqual(observed[0]['phase'],'recovering')
        self.assertIn('복구',value['message'])


class RecoveryBoundaryTests(Fixture):
    def setUp(self):
        super().setUp()
        private=self.root/'private';private.mkdir()
        coord=private/'coordinator';coord.mkdir()
        self.value.update(runtime_backend='core',private_plan_root=str(private),
                          coordinator={'provider':'codex','model':'fixture','effort':'high','root':str(coord)})
        self.config.write_text(json.dumps(self.value));self.create()
        inventory=self.root/'inventory.md';inventory.write_text('Fixture inventory')
        linked=self.root/'bindings.json';linked.write_text(json.dumps({'inventory':str(inventory)}))
        with StoreContext(self.state) as store:
            now=time.time()
            store.quota('codex/team',{'binding':binding(self.value['profiles']['codex']['team']),
                        'observed_at':now,'five_hour':{'used':1,'resets_at':now+900},
                        'weekly':{'used':1,'resets_at':now+900}})
        job=self.state.parent/'job';job.mkdir()
        self.request=job/'request.json'
        self.request.write_text(json.dumps({'config':str(self.config),'state':str(self.state),
            'bindings':str(linked),'nonce':'fixture','config_digest':hashlib.sha256(self.config.read_bytes()).hexdigest()}))
        app_coordinator.save(app_coordinator.record_path(self.state),{'phase':'recovering','nonce':'fixture'})

    def test_unknown_creation_result_blocks_another_recovery(self):
        with patch('teamkit.app_coordinator.require_managed'),patch('teamkit.app_coordinator.providers.identity'),patch('teamkit.app_coordinator._call',side_effect=KitError('outcome unknown')):
            with self.assertRaises(KitError):app_coordinator.run_job(self.request)
        self.assertEqual(app_coordinator.read_record(self.state)['phase'],'uncertain')
        self.assertFalse(app_coordinator.status(self.state,{},terminals=[])['can_recover'])

    def test_quota_invalidated_during_identity_does_not_launch(self):
        def invalidate(*args):
            with StoreContext(self.state) as store:
                value=store.quota('codex/team');value['unavailable']=True;store.quota('codex/team',value)
        with patch('teamkit.app_coordinator.require_managed'),patch('teamkit.app_coordinator.providers.identity',side_effect=invalidate),patch('teamkit.app_coordinator._call',side_effect=AssertionError('quota-ineligible launch')) as native:
            with self.assertRaises(ValueError):app_coordinator.run_job(self.request)
        native.assert_not_called()
