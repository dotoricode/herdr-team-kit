"""Consumer integration around observer CLI and existing context supervisor."""
import json
import unittest
from unittest.mock import patch

import test_team_dispatch as fixtures
from teamkit import context_handoff as context
from teamkit.config import load, binding
from teamkit.store import Store


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

    def test_actual_tell_refuses_quota_or_profile_change_before_wake(self):
        for change in ('quota','profile'):
            with self.subTest(change=change):
                self.f.ready();self.f.observe()
                store=Store(self.f.state);task=store.get('D1')
                task['runtime']['profile_binding']=binding(self.f.value['profiles']['codex']['personal'])
                store._save(task,'fixture');store.close()
                native={**self.f.native(),'address':'tmanager','status':{'state':'idle','app':'codex'}}
                def native_read(*args,**kwargs):
                    if change=='quota':
                        store=Store(self.f.state);quota=store.quota('codex/personal')
                        quota['five_hour']['used']=90;store.quota('codex/personal',quota);store.close()
                    else:
                        self.f.value['profiles']['codex']['personal']['home']=str(self.f.root/'replacement-personal')
                        self.f.config.write_text(json.dumps(self.f.value))
                    return native
                with patch('teamkit.core_runtime.require_managed',return_value='tcontroller'),                     patch('teamkit.core_runtime.CoreRuntime._native',side_effect=native_read),                     patch('teamkit.core_runtime.CoreRuntime.prompt') as prompt,                     patch('teamkit.providers.identity'):
                    message=self.f.root/'event.txt';message.write_text('Observe this event.')
                    with self.assertRaises(ValueError):
                        self.f.call('tell','D1','--message-file',str(message))
                prompt.assert_not_called()
                self.assertEqual(self.f.call('show','D1')['state'],'ready')
                # Restore the original fixture configuration for the next branch.
                self.f.value['profiles']['codex']['personal']['home']=str(self.f.root/'personal')
                self.f.config.write_text(json.dumps(self.f.value))

    def test_actual_tell_accepts_current_personal_actor_and_fresh_admission(self):
        task=self.f.ready()
        store=Store(self.f.state)
        task['runtime']['profile_binding']=binding(self.f.value['profiles']['codex']['personal'])
        store._save(task,'fixture');store.close()
        native={**self.f.native(),'address':'tmanager','status':{'state':'idle','app':'codex'}}
        message=self.f.root/'event-valid.txt';message.write_text('Observe the supplied event.')
        with patch('teamkit.core_runtime.require_managed',return_value='tcontroller'),\
             patch('teamkit.core_runtime.CoreRuntime._native',return_value=native),\
             patch('teamkit.context_handoff.clean_prompt',return_value=True),\
             patch('teamkit.core_runtime.CoreRuntime.prompt') as prompt,\
             patch('teamkit.providers.identity') as identity:
            result=self.f.call('tell','D1','--message-file',str(message))
        self.assertEqual(result['state'],'running')
        prompt.assert_called_once();identity.assert_called_once()

    def test_final_native_probe_precedes_latest_quota_and_profile_check(self):
        for change in ('quota','profile'):
            with self.subTest(change=change):
                task=self.f.ready()
                store=Store(self.f.state)
                task['runtime']['profile_binding']=binding(self.f.value['profiles']['codex']['personal'])
                store._save(task,'fixture');store.close()
                native={**self.f.native(),'address':'tmanager','status':{'state':'idle','app':'codex'}}
                reads=[]
                def native_read(*args,**kwargs):
                    reads.append(True)
                    if len(reads)==2:
                        if change=='quota':
                            store=Store(self.f.state);quota=store.quota('codex/personal')
                            quota['five_hour']['used']=90;store.quota('codex/personal',quota);store.close()
                        else:
                            self.f.value['profiles']['codex']['personal']['home']=str(self.f.root/'late-profile')
                            self.f.config.write_text(json.dumps(self.f.value))
                    return native
                message=self.f.root/'late-event.txt';message.write_text('Observe current queue.')
                with patch('teamkit.core_runtime.require_managed',return_value='tcontroller'),\
                     patch('teamkit.core_runtime.CoreRuntime._native',side_effect=native_read),\
                     patch('teamkit.context_handoff.clean_prompt',return_value=True),\
                     patch('teamkit.core_runtime.CoreRuntime.prompt') as prompt,\
                     patch('teamkit.providers.identity'):
                    with self.assertRaises(ValueError):
                        self.f.call('tell','D1','--message-file',str(message))
                prompt.assert_not_called()
                self.assertEqual(self.f.call('show','D1')['state'],'ready')
                self.f.value['profiles']['codex']['personal']['home']=str(self.f.root/'personal')
                self.f.config.write_text(json.dumps(self.f.value))
