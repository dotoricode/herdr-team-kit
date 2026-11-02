"""Consumer integration around observer CLI and existing context supervisor."""
import unittest
from unittest.mock import patch

import test_team_dispatch as fixtures
from teamkit import context_handoff as context
from teamkit.config import load


class DispatchIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.TeamDispatchTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.addCleanup(self.f.doCleanups)

    def test_registered_observer_is_a_context_target_without_becoming_product_leader(self):
        self.f.ready()
        targets=context.targets(load(self.f.config),self.f.state)
        self.assertEqual([t['id'] for t in targets],['D1'])
        self.assertEqual(targets[0]['provider'],'codex')
        self.assertEqual(targets[0]['account'],'personal')
        self.assertEqual(load(self.f.config)['session_pool']['tasks'],{'alpha':'L1'})

    def test_existing_supervisor_ticks_observer_without_an_extra_supervisor(self):
        self.f.ready()
        with patch.object(context,'targets',return_value=[]),patch.object(context,'jobs',return_value=[]),\
             patch('teamkit.pool_control.requests',return_value=[]),\
             patch('teamkit.team_dispatch.tick',return_value={'enabled':True,'teams':['alpha']}) as observe,\
             patch('teamkit.team_dispatch.advance') as advance:
            context.tick(self.f.config,self.f.state)
        observe.assert_called_once_with(self.f.config,self.f.state)
        advance.assert_called_once_with(self.f.config,self.f.state,'alpha')

    def test_status_cli_does_not_prepare_or_spawn_an_observer(self):
        self.f.prepared()
        with patch('teamkit.team_dispatch.prepare') as prepare,patch('teamkit.team_dispatch.bootstrap') as bootstrap:
            result=self.f.call('team-dispatch','status')
        self.assertTrue(result['enabled']);self.assertEqual(result['managers'][0]['state'],'queued')
        prepare.assert_not_called();bootstrap.assert_not_called()
