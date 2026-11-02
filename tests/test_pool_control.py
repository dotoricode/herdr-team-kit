import json
import os
from pathlib import Path
import subprocess
import time
from unittest.mock import patch

from test_cli import Fixture
from test_session_pool import FakeNative
from teamkit import session_pool as pool, pool_control as control, context_handoff as context
from teamkit.config import load,binding
from teamkit.store import Store


class PoolControlTests(Fixture):
    def setUp(self):
        super().setUp()
        self.value['runtime_backend']='core'
        self.value['profiles']['claude']={a:{'home':str(self.root/('claude-'+a)),
            'identity':{'email':a+'@example.invalid'},'model':'claude-fixture','effort':'medium'} for a in ('team','personal')}
        self.value['teams']['alpha']['roles']={'leader':{'provider':'codex','model':'fixture','effort':'medium'}}
        self.value['session_pool']={'enabled':True,'bindings':str(self.root/'bindings.json'),'tasks':{'alpha':'L1'}}
        self.value['context_handoff']={'enabled':True,'threshold':90,'interval':10}
        self.value['context_bindings']=str(self.root/'bindings.json')
        self.value['private_plan_root']=str(self.root)
        self.config.write_text(json.dumps(self.value))
        self.call('create','L1','--team','alpha','--role','leader','--root',str(self.repo),'--goal-file',str(self.root/'goal.txt'))
        self.fake=FakeNative();original=subprocess.run
        def bounded_run(args,**kwargs):
            return self.fake.run(args,**kwargs) if args[:2]==['hc','read'] or Path(args[0]).name=='ensure-atlassian-mcp' else original(args,**kwargs)
        for name,value in [('teamkit.core_runtime._call',self.fake.call),('teamkit.session_pool._call',self.fake.call),('teamkit.session_pool.subprocess.run',bounded_run)]:
            p=patch(name,side_effect=value);p.start();self.addCleanup(p.stop)
        env=patch.dict(os.environ,{'HC_TERMINAL_ID':'tcontroller'});env.start();self.addCleanup(env.stop)
        self.identity=patch('teamkit.providers.identity');self.identity_mock=self.identity.start();self.addCleanup(self.identity.stop)

    def prepare(self,provider='codex'):
        if provider=='claude':
            self.value['teams']['alpha']['roles']['leader']={'provider':'claude','model':'claude-fixture','effort':'medium'}
            self.config.write_text(json.dumps(self.value))
        pool.prepare(self.config,self.state)
        now=time.time();store=Store(self.state)
        store.quota(provider+'/team',{'binding':binding(self.value['profiles'][provider]['team']),
            'source':'claude-statusline-response' if provider=='claude' else 'codex-native-quota','observed_at':now,
            'five_hour':{'used':1,'resets_at':now+3600},'weekly':{'used':1,'resets_at':now+86400}});store.close()

    def dispatched(self,provider='codex'):
        self.prepare(provider)
        def receipt(runtime,store,config_path,task,owner,text):
            self.assertEqual(runtime.task['owner'],owner)
            self.assertEqual(runtime.target['pool_owner'],owner)
            self.assertIn('receipt L1 --owner '+owner,text)
            self.assertEqual(task['runtime']['receipt_protocol'],'host-fresh-v1')
            store.receipt('L1',owner);store.park('L1',owner)
        def claude_prompt(actor,name,text):
            store=Store(self.state);task=store.get('L1');store.close()
            with patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':self.value['profiles']['claude']['team']['home']}):
                self.call('receipt','L1','--owner',task['owner'])
                self.call('park','L1','--owner',task['owner'])
        with patch('teamkit.context_handoff.ensure'),patch('teamkit.receipt.prompt',side_effect=receipt),patch('teamkit.core_runtime.CoreRuntime.prompt',new=claude_prompt):
            result=self.call('dispatch','L1')
        self.assertEqual(result['state'],'ready');self.assertEqual(result['runtime']['identity_checked'],True)
        self.assertEqual(pool.status(load(self.config),self.state)['counts'],{'leased':1,'prepared':3})
        self.assertEqual(sum(c[0]=='run' for c in self.fake.calls),4)
        self.identity_mock.assert_called()
        return result

    def test_request_constraint_waits_instead_of_using_another_eligible_account(self):
        from teamkit.__main__ import select_execution
        self.prepare()
        task = self.call('show', 'L1')
        task['execution_constraint'] = {'provider': 'codex', 'account': 'personal'}
        store = Store(self.state)
        try:
            selected = select_execution(load(self.config), store, task, time.time())
        finally:
            store.close()
        self.assertEqual(selected['action'], 'wait')

    def test_cli_pinned_assignment_waits_without_fallback_and_records_constraint(self):
        self.prepare()
        message = self.root / 'pin-request.txt'
        message.write_text('Inspect the approved SDK scope only.')
        with patch.object(context, 'ensure'), patch.object(context, 'refresh_candidates'):
            request = self.call('pool', 'assign', '--team', 'alpha', '--message-file', str(message),
                                '--provider', 'claude', '--account', 'team')
            control.advance(self.config, self.state, request['id'])
        self.assertEqual(request['execution_constraint'], {'provider': 'claude', 'account': 'team'})
        self.assertEqual(self.call('show', 'L1')['execution_constraint'], request['execution_constraint'])
        self.assertEqual(self.call('pool', 'plan', '--team', 'alpha')['action'], 'wait')
        self.assertEqual(self.call('plan', 'L1')['action'], 'wait')
        self.assertEqual(control.requests(self.state)[0]['phase'], 'pending')
        self.assertEqual(self.call('show', 'L1')['state'], 'queued')

    def test_requested_provider_account_survives_official_handoff_and_dispatch(self):
        previous = self.dispatched('claude')
        self.prepare('codex')
        message = self.root / 'fixed-request.txt'
        message.write_text('Inspect this SDK using only the requested profile.')
        with patch.object(context, 'ensure'):
            request = self.call('pool', 'assign', '--team', 'alpha', '--message-file', str(message),
                                '--provider', 'codex', '--account', 'team')
        with patch.object(context, 'observe', return_value=None):
            control.advance(self.config, self.state, request['id'])
        self.assertEqual(control.plan(self.config, self.state, 'alpha')['selection']['provider'], 'codex')
        job = next(j for j in context.jobs(self.state) if j['target']['id'] == 'L1')
        self.assertEqual((job['next_session']['provider'], job['next_session']['account']), ('codex', 'team'))
        with patch('teamkit.core_runtime.CoreRuntime.prompt') as sent:
            with self.assertRaises(ValueError):
                self.call('tell', 'L1', '--message-file', str(message), '--expected-owner', previous['owner'])
            sent.assert_not_called()
        evidence = self.root / 'stopped.json'
        evidence.write_text(json.dumps({'verification': ['fixture writer stopped'], 'next': 'Continue the fixed request.'}))
        self.call('handoff', 'L1', '--owner', previous['owner'], '--writer-stopped', '--evidence-file', str(evidence))

        def received(runtime, store, config_path, task, owner, text):
            store.receipt('L1', owner)
            store.park('L1', owner)

        with patch.object(context, 'ensure'), patch('teamkit.receipt.prompt', side_effect=received):
            result = self.call('dispatch', 'L1')
        self.assertEqual((result['provider'], result['account']), ('codex', 'team'))
        self.assertEqual(result['execution_constraint'], {'provider': 'codex', 'account': 'team'})

    def test_codex_dispatch_uses_prepared_native_with_common_claim_and_receipt(self):self.dispatched()
    def test_claude_dispatch_uses_prepared_statusline_and_actual_profile_receipt(self):self.dispatched('claude')

    def test_unknown_usage_waits_without_claim_or_preferred_slot_fallback(self):
        pool.prepare(self.config,self.state)
        route=control.plan(self.config,self.state,'alpha')
        self.assertEqual(route['action'],'wait')
        self.assertEqual(self.call('show','L1')['state'],'queued')
        self.assertEqual(pool.status(load(self.config),self.state)['counts'],{'prepared':4})

    def test_reservation_at_threshold_records_next_without_claiming_it(self):
        result=self.dispatched()
        target=context.targets(load(self.config),self.state)[0]
        job=context.request(load(self.config),self.state,target,
            {'session_id':'session-1','source':'codex-rollout','used':90,'observed_at':time.time()})
        self.assertEqual(job['phase'],'pending')
        self.assertEqual(job['next_session']['provider'],'codex')
        self.assertEqual(job['next_session']['account'],'team')
        self.assertEqual(pool.status(load(self.config),self.state)['counts'],{'leased':1,'prepared':3})
        self.assertEqual(self.call('show','L1')['owner'],result['owner'])

    def test_assign_delivers_once_and_rejects_different_pending_request(self):
        result=self.dispatched();message=self.root/'request.txt';message.write_text('Only inspect the approved scope; report and park.')
        first=control.enqueue(self.config,self.state,'alpha',message)
        self.assertEqual(control.enqueue(self.config,self.state,'alpha',message)['id'],first['id'])
        other=self.root/'other.txt';other.write_text('different pending work')
        with self.assertRaises(ValueError):control.enqueue(self.config,self.state,'alpha',other)
        with patch.object(context,'capacity_available',return_value=True),patch.object(context,'clean_prompt',return_value=True),patch.object(control,'official',return_value=result) as official:
            control.advance(self.config,self.state,first['id']);control.advance(self.config,self.state,first['id'])
            self.assertEqual(official.call_count,1)
            self.assertIn('--expected-owner',official.call_args.args)
        self.assertEqual(control.requests(self.state)[0]['phase'],'done')

    def test_uncertain_send_never_replays_or_changes_owner(self):
        result=self.dispatched();p=self.root/'request.txt';p.write_text('Approved remaining work only')
        job=control.enqueue(self.config,self.state,'alpha',p)
        with patch.object(context,'capacity_available',return_value=True),patch.object(context,'clean_prompt',return_value=True),patch.object(control,'official',side_effect=TimeoutError) as call:
            control.advance(self.config,self.state,job['id']);control.advance(self.config,self.state,job['id'])
            self.assertEqual(call.call_count,1)
        self.assertEqual(control.requests(self.state)[0]['phase'],'uncertain')
        self.assertEqual(self.call('show','L1')['owner'],result['owner'])

    def test_changed_quota_uses_handoff_instead_of_telling_previous_account(self):
        result=self.dispatched();store=Store(self.state);now=time.time()
        for account,used in [('team',91),('personal',2)]:
            store.quota('codex/'+account,{'binding':binding(self.value['profiles']['codex'][account]),'observed_at':now,
                'five_hour':{'used':used,'resets_at':now+3600},'weekly':{'used':used,'resets_at':now+86400}})
        store.close()
        route=control.plan(self.config,self.state,'alpha')
        self.assertEqual(route['action'],'handoff');self.assertEqual(route['selection']['account'],'personal')
        p=self.root/'request.txt';p.write_text('Preserve approved scope')
        request=control.enqueue(self.config,self.state,'alpha',p)
        with patch.object(context,'observe',return_value=None),patch.object(control,'official') as sent:
            control.advance(self.config,self.state,request['id']);sent.assert_not_called()
        saved=control.requests(self.state)[0]
        self.assertEqual(saved['phase'],'waiting_handoff')
        job=context.read(context.job_path(self.state,saved['handoff_job']))
        self.assertTrue(job['policy_rotation']);self.assertEqual(job['next_session']['account'],'personal')
        self.assertEqual(pool.status(load(self.config),self.state)['counts'],{'leased':1,'prepared':3})

    def test_quota_expiry_during_native_check_prevents_delivery(self):
        result=self.dispatched();p=self.root/'request.txt';p.write_text('Approved scope only')
        request=control.enqueue(self.config,self.state,'alpha',p)
        def expired(*args):
            store=Store(self.state);value=store.quota('codex/team');value['observed_at']-=1000;store.quota('codex/team',value);store.close();return True
        with patch.object(context,'capacity_available',return_value=True),patch.object(context,'clean_prompt',side_effect=expired),patch.object(control,'official',return_value=result) as sent:
            control.advance(self.config,self.state,request['id']);sent.assert_not_called()
        self.assertEqual(control.requests(self.state)[0]['phase'],'pending')

    def test_official_tell_rechecks_usage_after_identity_without_waking_on_expiry(self):
        result=self.dispatched();p=self.root/'message.txt';p.write_text('Approved remaining scope')
        def expire(*args,**kwargs):
            store=Store(self.state);value=store.quota('codex/team');value['observed_at']-=1000;store.quota('codex/team',value);store.close()
        with patch('teamkit.providers.identity',side_effect=expire),patch('teamkit.core_runtime.CoreRuntime.prompt') as sent:
            with self.assertRaises(ValueError):self.call('tell','L1','--message-file',str(p),'--expected-owner',result['owner'])
            sent.assert_not_called()
        self.assertEqual(self.call('show','L1')['state'],'ready')

    def test_official_tell_preserves_same_active_pool_generation(self):
        result=self.dispatched();p=self.root/'message.txt';p.write_text('Approved remaining scope')
        def parked(actor,name,text):
            self.assertIn('CONTINUATION OF AN ALREADY RECEIPTED TASK',text)
            self.assertEqual(actor.task['owner'],result['owner'])
            self.call('park','L1','--owner',result['owner'])
        with patch('teamkit.core_runtime.CoreRuntime.prompt',new=parked):
            after=self.call('tell','L1','--message-file',str(p),'--expected-owner',result['owner'])
        self.assertEqual(after['state'],'ready');self.assertEqual(after['runtime']['pool_generation'],1)
        self.assertEqual(sum(c[0]=='run' for c in self.fake.calls),4)

    def test_status_reports_four_shared_account_snapshots_without_creating_a_store(self):
        from types import SimpleNamespace
        missing=self.root/'absent'/'state.sqlite'
        result=control.handle(SimpleNamespace(config=self.config,state=missing,action='status'))
        self.assertEqual(len(result['accounts']),4)
        self.assertTrue(all(not row['eligible'] for row in result['accounts']))
        self.assertFalse(missing.parent.exists())

    def test_dispatch_checks_unknown_preferred_provider_before_using_cached_lower_priority(self):
        self.value['execution_preference']={'provider_order':['claude','codex'],'weekend_account_first':True}
        self.config.write_text(json.dumps(self.value));self.prepare()
        self.assertEqual(control.plan(self.config,self.state,'alpha')['selection']['provider'],'codex')
        def fresh(*args):
            now=time.time();store=Store(self.state)
            store.quota('claude/team',{'source':'claude-statusline-response','binding':binding(self.value['profiles']['claude']['team']),
                'observed_at':now,'five_hour':{'used':1,'resets_at':now+3600},'weekly':{'used':1,'resets_at':now+86400}});store.close()
        def stop(config,state,task,role):
            self.assertEqual(role['provider'],'claude')
            raise ValueError('stopped before claim')
        with patch.object(context,'refresh_candidates',side_effect=fresh) as refresh,patch.object(pool,'reserve',side_effect=stop),patch.object(context,'ensure'):
            with self.assertRaisesRegex(ValueError,'stopped before claim'):self.call('dispatch','L1')
            refresh.assert_called_once()

    def test_bootstrap_replaces_existing_representative_only_after_receipt(self):
        self.prepare()
        binding_path=Path(self.value['session_pool']['bindings'])
        binding_path.write_text(json.dumps({'teams':{'alpha':{'terminal':'told','space':'spalpha'}}}))
        def received(*args):
            store=Store(self.state);task=store.get('L1');store.close()
            return {**task,'state':'ready','runtime':{'terminal':'tnew','identity_checked':True}}
        with patch.object(control,'refresh_choice'),patch.object(context,'capacity_available',return_value=True),patch.object(control,'official',side_effect=received),patch.object(context,'attach') as attach:
            self.call('pool','bootstrap','--team','alpha')
        self.assertEqual(attach.call_args.args[2],{'kind':'task','id':'L1','terminal':'told'})
        self.assertEqual(attach.call_args.args[3],'tnew')
