import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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
