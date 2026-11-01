import json
import math
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from teamkit import context_handoff as context


class ContextHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root/'state.sqlite'
        self.config = {'runtime_backend':'core','private_plan_root':str(self.root),
            'context_handoff':{'enabled':True,'threshold':90,'interval':10}}
        self.target = {'kind':'task','id':'T1','owner':'owner-1','provider':'claude',
            'account':'personal','root':str(self.root),'terminal':'t1','pid':123,'was_ready':False,'binding':'binding'}
        self.observation = {'session_id':'session-1','used':90,'observed_at':time.time(),'source':'claude-statusline-response'}
        self.config_path = self.root/'config.json'

    def tearDown(self):
        self.temp.cleanup()

    def request(self, used=90):
        return context.request(self.config,self.state,self.target,{**self.observation,'used':used})

    def test_crossing_90_latches_once_even_if_context_later_compacts(self):
        self.assertIsNone(self.request(89.9))
        job = self.request(90)
        self.assertEqual(job['phase'],'pending')
        self.assertIsNone(self.request(95))
        self.assertEqual(len(context.jobs(self.state)),1)
        self.assertEqual(context.jobs(self.state)[0]['trigger']['used'],90)

    def test_new_native_session_has_independent_latch(self):
        self.request()
        job=context.request(self.config,self.state,self.target,{**self.observation,'session_id':'session-2'})
        self.assertIsNotNone(job)

    def test_unknown_future_stale_and_nonfinite_never_trigger(self):
        for value in [None,{}, {**self.observation,'observed_at':time.time()-121},
                      {**self.observation,'observed_at':time.time()+5},
                      {**self.observation,'used':math.nan}, {**self.observation,'used':101}]:
            self.assertIsNone(context.request(self.config,self.state,self.target,value))
        self.assertEqual(context.jobs(self.state),[])

    def test_manual_rehearsal_cannot_force_a_real_product_target(self):
        with self.assertRaises(ValueError):
            context.request(self.config,self.state,self.target,None,manual=True)
        isolated={**self.target,'id':'CONTEXT-SMOKE-1'}
        self.assertTrue(context.request(self.config,self.state,isolated,None,manual=True)['verification_only'])

    def test_working_or_draft_does_not_receive_checkpoint_request(self):
        job=self.request()
        with patch.object(context,'load',return_value=self.config),patch.object(context,'current_target',return_value=self.target),patch.object(context,'native',return_value={}),patch.object(context,'clean_prompt',return_value=False),patch.object(context,'_call') as call:
            context.advance(self.config_path,self.state,job)
            call.assert_not_called()
        value=context.read(context.job_path(self.state,job['id']))
        self.assertEqual(value['phase'],'pending')
        self.assertIn('입력',value['reason'])

    def test_checkpoint_message_is_sent_once_and_never_replayed_after_uncertain_send(self):
        self.target['kind']='coordinator'
        job=self.request()
        with patch.object(context,'load',return_value=self.config),patch.object(context,'current_target',return_value=self.target),patch.object(context,'native',return_value={}),patch.object(context,'clean_prompt',return_value=True),patch.object(context,'observe',return_value=self.observation),patch.object(context,'capacity_available',return_value=True),patch.object(context,'_call',side_effect=TimeoutError) as call:
            context.advance(self.config_path,self.state,job)
            context.advance(self.config_path,self.state,job)
            self.assertEqual(call.call_count,1)
        self.assertEqual(context.read(context.job_path(self.state,job['id']))['phase'],'uncertain')

    def test_multiline_draft_after_hint_never_receives_handoff_message(self):
        self.target.update(kind='coordinator',empty_prompt='Try "write a fixture helper"')
        job=self.request()
        screen=type('R',(),{'stdout':'❯ Try "write a fixture helper"\n  unfinished multiline user draft'})()
        with patch.object(context,'load',return_value=self.config),patch.object(context,'current_target',return_value=self.target),patch.object(context,'native',return_value={'status':{'state':'done'}}),patch.object(context,'observe',return_value=self.observation),patch.object(context,'capacity_available',return_value=True),patch.object(context.subprocess,'run',return_value=screen),patch.object(context,'_call') as call:
            context.advance(self.config_path,self.state,job)
            call.assert_not_called()
        self.assertEqual(context.read(context.job_path(self.state,job['id']))['phase'],'pending')

    def test_changed_owner_cancels_before_native_input(self):
        job=self.request()
        with patch.object(context,'load',return_value=self.config),patch.object(context,'current_target',return_value=None),patch.object(context,'_call') as call:
            context.advance(self.config_path,self.state,job)
            call.assert_not_called()
        self.assertEqual(context.read(context.job_path(self.state,job['id']))['phase'],'cancelled')

    def test_exit_failure_never_releases_or_dispatches(self):
        job=self.request();context.set_phase(self.state,job,'stopping')
        with patch.object(context,'load',return_value=self.config),patch.object(context,'_call',return_value={'state':'finished','exit_code':1,'signal':None}) as call:
            context.advance(self.config_path,self.state,job)
            self.assertEqual(call.call_count,1)
        self.assertEqual(context.read(context.job_path(self.state,job['id']))['phase'],'uncertain')

    def test_evidence_must_be_from_actual_terminal_and_job_file(self):
        job=self.request();context.set_phase(self.state,job,'requested')
        with patch.object(context,'load',return_value=self.config),patch.object(context,'current_target',return_value=self.target),patch.dict(os.environ,{'HC_TERMINAL_ID':'t9'},clear=False):
            with self.assertRaises(ValueError):context.checkpoint(self.config_path,self.state,job['id'],str(self.root/'bad.json'),True)

    def test_read_rejects_symlink_and_invalid_job_names(self):
        p=self.root/'link.json';p.symlink_to(self.root/'missing')
        with self.assertRaises(ValueError):context.job_path(self.state,'../escape')
        target=self.root/'data.json';target.write_text('{}');p.unlink();p.symlink_to(target)
        with self.assertRaises(ValueError):context.read(p)

    def test_disabled_default_is_inert(self):
        self.assertFalse(context.settings({})['enabled'])
        with self.assertRaises(ValueError):context.settings({'context_handoff':{'enabled':True,'threshold':70,'interval':10}})

    def test_exact_checkpoint_file_and_writer_acknowledgement_required(self):
        target={**self.target,'kind':'coordinator'}
        job=context.request(self.config,self.state,target,self.observation)
        context.set_phase(self.state,job,'requested')
        env={'HC_TERMINAL_ID':'t1','TEAMKIT_GUI_COORDINATOR':'owner-1'}
        with patch.object(context,'load',return_value=self.config),patch.object(context,'current_target',return_value=target),patch.object(context,'native'),patch.object(context,'observe',return_value={'session_id':'session-1'}),patch.dict(os.environ,env):
            with self.assertRaises(ValueError):context.checkpoint(self.config_path,self.state,job['id'],str(self.root/'outside.json'),True)
            p=context.directory(self.state)/(job['id']+'-evidence.json')
            context.write(p,{'verification':['test passed'],'next':'continue approved step','pending_approvals':[], 'background_writers_stopped':False})
            with self.assertRaises(ValueError):context.checkpoint(self.config_path,self.state,job['id'],str(p),True)
            context.write(p,{'verification':['test passed'],'next':'continue approved step','pending_approvals':['design decision'], 'background_writers_stopped':True})
            result=context.checkpoint(self.config_path,self.state,job['id'],str(p),True)
            self.assertEqual(result['checkpoint'],'saved')
            saved=context.read(context.job_path(self.state,job['id']))
            self.assertEqual(saved['phase'],'checkpoint_ready')
            self.assertEqual(saved['checkpoint']['pending_approvals'],['design decision'])


from test_cli import Fixture
from teamkit.config import binding, load
from teamkit.store import Store


class ActualHandoffFlowTests(Fixture):
    def setUp(self):
        super().setUp()
        self.bindings=self.root/'bindings.json';self.bindings.write_text(json.dumps({'teams':{}}))
        self.value.update(runtime_backend='core',context_bindings=str(self.bindings),
            private_plan_root=str(self.root),context_handoff={'enabled':True,'threshold':90,'interval':10})
        self.config.write_text(json.dumps(self.value))
        self.create()
        store=Store(self.state)
        store.claim('T1','old-owner','codex','personal','fixture','medium')
        runtime={'backend':'core','name':'fixture-old','native_pid':123,'terminal':'t1'}
        store.update('T1','old-owner','launching',runtime)
        store.update('T1','old-owner','submitted',runtime)
        store.receipt('T1','old-owner');store.close()
        self.target=context.targets(load(self.config),self.state)[0]
        self.job=context.request(load(self.config),self.state,self.target,
            {'session_id':'session-fixture','used':90,'observed_at':time.time(),'source':'codex-rollout'})

    def save_checkpoint(self):
        context.set_phase(self.state,self.job,'requested')
        p=context.directory(self.state)/(self.job['id']+'-evidence.json')
        context.write(p,{'verification':['observable test passed'], 'next':'continue approved T1 scope',
                         'pending_approvals':[], 'background_writers_stopped':True})
        with patch.object(context,'native'),patch.dict(os.environ,{'HC_TERMINAL_ID':'t1','TEAMKIT_OWNER':'old-owner','CODEX_THREAD_ID':'session-fixture'}):
            context.checkpoint(self.config,self.state,self.job['id'],str(p),True)
        self.job=context.read(context.job_path(self.state,self.job['id']))
        cp=self.job['checkpoint']
        context.write(context.directory(self.state)/(self.job['id']+'-handoff.json'),
            {'verification':cp['verification'],'next':cp['next'],
             'context_continuation':{'was_ready':False,'pending_approvals':[]}})

    def test_checkpoint_exit_official_handoff_dispatch_and_receipt(self):
        self.save_checkpoint()
        with patch.object(context,'exit_session_matches',return_value=True),patch.object(context,'native',return_value={}),patch.object(context,'clean_prompt',return_value=True),patch.object(context,'_call') as call:
            context.advance(self.config,self.state,self.job)
            self.assertEqual(call.call_args.args,('send','t1','/exit'))
        with patch.object(context,'_call',return_value={'state':'finished','exit_code':0,'signal':None,'pid':123,'labels':{'teamkit.owner':'old-owner'}}):
            context.advance(self.config,self.state,self.job)
        store=Store(self.state);task=store.get('T1')
        self.assertEqual(task['state'],'handed_off');self.assertIsNone(task['owner'])
        quota={'binding':binding(self.value['profiles']['codex']['team']), 'observed_at':time.time(),
               'five_hour':{'used':1,'resets_at':time.time()+3600},'weekly':{'used':1,'resets_at':time.time()+86400}}
        store.quota('codex/team',quota);store.close()
        with patch.object(context,'capacity_available',return_value=True),patch.object(context,'_call',return_value={'address':'t3'}) as call:
            context.advance(self.config,self.state,self.job)
            self.assertIn('dispatch',call.call_args.args)
        store=Store(self.state)
        store.claim('T1','new-owner','codex','team','fixture','medium')
        runtime={'backend':'core','name':'fixture-new','native_pid':456,'terminal':'t2'}
        store.update('T1','new-owner','launching',runtime);store.update('T1','new-owner','submitted',runtime)
        store.receipt('T1','new-owner');store.close()
        with patch.object(context,'attach') as attach:
            context.advance(self.config,self.state,self.job)
            self.assertEqual(attach.call_args.args[-1],'t2')
        self.assertEqual(context.read(context.job_path(self.state,self.job['id']))['phase'],'done')

    def test_capacity_counts_unmanaged_models_and_reserves_pending_handoffs(self):
        terminals=[{'address':'outside'+str(i),'agent':'codex','status':'working'} for i in range(2)]
        with patch.object(context,'_call',return_value={'terminals':terminals}):
            # Existing running T1 + two other model turns exhausts the limit.
            self.assertFalse(context.capacity_available(self.state))
            self.assertTrue(context.capacity_available(self.state,self.target))
        context.set_phase(self.state,self.job,'dispatching')
        with patch.object(context,'_call',return_value={'terminals':terminals}):
            self.assertFalse(context.capacity_available(self.state))

    def test_budget_and_context_crossing_cannot_create_two_jobs_for_one_owner(self):
        config=load(self.config)
        self.assertIsNone(context.request(config,self.state,self.target,
            {'metric':'five_hour','used':97,'observed_at':time.time(),'source':'codex-native-quota'}))
        self.assertEqual(len(context.jobs(self.state)),1)

    def test_new_session_after_checkpoint_never_exits_or_releases_owner(self):
        self.save_checkpoint()
        context.save_registration(self.state,self.target,'new-session-after-checkpoint')
        with patch.object(context,'native',return_value={}),patch.object(context,'clean_prompt',return_value=True),patch.object(context,'_call') as call:
            context.advance(self.config,self.state,self.job)
            self.assertFalse(any(c.args==('send','t1','/exit') for c in call.call_args_list))
        saved=context.read(context.job_path(self.state,self.job['id']))
        self.assertEqual(saved['phase'],'uncertain')
        store=Store(self.state)
        try:self.assertEqual(store.get('T1')['owner'],'old-owner')
        finally:store.close()

    def test_changed_worktree_after_checkpoint_does_not_release_owner(self):
        self.save_checkpoint()
        (self.repo/'unexpected.txt').write_text('changed after checkpoint')
        context.set_phase(self.state,self.job,'stopping')
        with patch.object(context,'_call',return_value={'state':'finished','exit_code':0,'signal':None,'pid':123,'labels':{'teamkit.owner':'old-owner'}}):
            context.advance(self.config,self.state,self.job)
        store=Store(self.state)
        self.assertEqual(store.get('T1')['owner'],'old-owner');store.close()
        self.assertEqual(context.read(context.job_path(self.state,self.job['id']))['phase'],'uncertain')

    def test_no_eligible_account_keeps_checkpoint_and_waits(self):
        self.save_checkpoint();context.set_phase(self.state,self.job,'waiting_dispatch')
        with patch.object(context,'refresh_candidates') as refresh,patch.object(context,'_call') as native:
            context.advance(self.config,self.state,self.job)
            context.advance(self.config,self.state,self.job)
            self.assertEqual(refresh.call_count,1);native.assert_not_called()
        self.assertEqual(context.read(context.job_path(self.state,self.job['id']))['phase'],'waiting_dispatch')

    def test_old_thread_after_manual_clear_cannot_checkpoint_or_exit(self):
        context.set_phase(self.state,self.job,'requested')
        with patch.object(context,'native'),patch.dict(os.environ,{'HC_TERMINAL_ID':'t1','TEAMKIT_OWNER':'old-owner','CODEX_THREAD_ID':'new-session'}):
            with self.assertRaises(ValueError):context.checkpoint(self.config,self.state,self.job['id'],'invalid',True)
        store=Store(self.state)
        self.assertEqual(store.get('T1')['owner'],'old-owner')
        self.assertEqual(store.get('T1')['runtime']['context_session_id'],'new-session');store.close()


class SnapshotLifetimeTests(Fixture):
    def test_read_snapshot_closes_actual_connection(self):
        import sqlite3
        from teamkit.workflow import snapshot
        self.create()
        original=sqlite3.connect
        connections=[]
        def tracked(*args,**kwargs):
            db=original(*args,**kwargs);connections.append(db);return db
        with patch('teamkit.workflow.sqlite3.connect',side_effect=tracked):
            result=snapshot(self.state)
        self.assertEqual(result['tasks'][0]['id'],'T1')
        self.assertEqual(len(connections),1)
        with self.assertRaises(sqlite3.ProgrammingError):connections[0].execute('SELECT 1')
        for db in connections:db.close()


class NativePromptTests(unittest.TestCase):
    def test_claude_blank_prompt_does_not_consume_the_next_footer_line(self):
        target={'terminal':'t1'}
        value={'status':{'state':'done'}}
        for text,expected in [('response\n❯\u00a0\n────────\nContext 使用18%',True),
                              ('response\n❯ user draft\n────────',False),
                              ('response\n› Ask Codex to do anything\n  Context 93% left · weekly 52% left',True)]:
            with self.subTest(text=text),patch.object(context.subprocess,'run',return_value=type('Result',(),{'stdout':text})()):
                self.assertEqual(context.clean_prompt(value,target),expected)


class ParkedBudgetHandoffTests(Fixture):
    def setUp(self):
        super().setUp()
        self.value['teams']['alpha']['roles']['leader']=self.value['teams']['alpha']['roles']['implementer']
        self.value.update(runtime_backend='core',private_plan_root=str(self.root),
            context_bindings=str(self.root/'bindings.json'),context_handoff={'enabled':True,'threshold':90,'interval':10})
        self.config.write_text(json.dumps(self.value))
        self.call('create','L1','--team','alpha','--role','leader','--root',str(self.repo),'--goal-file',str(self.root/'goal.txt'))
        store=Store(self.state)
        store.claim('L1','standby-owner','codex','personal','fixture','medium')
        runtime={'backend':'core','name':'fixture-standby','native_pid':123,'terminal':'t1'}
        store.update('L1','standby-owner','launching',runtime);store.update('L1','standby-owner','submitted',runtime)
        store.receipt('L1','standby-owner');store.park('L1','standby-owner');store.close()
        self.target=context.targets(load(self.config),self.state)[0]
        self.job=context.request(load(self.config),self.state,self.target,
            {'metric':'weekly','used':100,'observed_at':time.time(),'source':'codex-native-quota'})

    def test_exhausted_parked_leader_cannot_infer_writer_ack_or_start_model_turn(self):
        result=type('Result',(),{'stdout':'• Received and parked. No product work started.\n\n› Ask Codex to do anything\n'})()
        original=context.subprocess.run
        def native_read(args,**kwargs):
            return result if args[:2]==['hc','read'] else original(args,**kwargs)
        with patch.object(context,'native',return_value={}),patch.object(context,'clean_prompt',return_value=True),patch.object(context,'_call',return_value={'terminals':[]}) as call,patch.object(context.subprocess,'run',side_effect=native_read):
            context.advance(self.config,self.state,self.job)
            self.assertFalse(any(c.args[0]=='send' for c in call.call_args_list))
        saved=context.read(context.job_path(self.state,self.job['id']))
        self.assertEqual(saved['phase'],'pending')
        self.assertTrue(saved['needs_you'])
        self.assertNotIn('checkpoint',saved)
        self.assertFalse((context.directory(self.state)/(self.job['id']+'-handoff.json')).exists())

    def test_active_descendant_prevents_host_checkpoint(self):
        rows=[{'address':'child','parent':'t1','status':'working'}]
        with patch.object(context,'native',return_value={}),patch.object(context,'clean_prompt',return_value=True),patch.object(context,'_call',return_value={'terminals':rows}):
            context.advance(self.config,self.state,self.job)
        saved=context.read(context.job_path(self.state,self.job['id']))
        self.assertEqual(saved['phase'],'pending')
        self.assertTrue(saved['needs_you'])
        self.assertNotIn('checkpoint',saved)


class ExitSessionEvidenceTests(unittest.TestCase):
    def test_native_status_requires_new_matching_output_and_current_registration(self):
        session='00000000-0000-0000-0000-000000000001'
        target={'provider':'codex','session_id':session,'terminal':'t1'}
        job={'target':target,'checkpoint_session_id':session}
        cases=[('old response','old response\nSession: '+session,True),
               ('Session: '+session,'Session: '+session,False),
               ('old response','Session: 00000000-0000-0000-0000-000000000002',False)]
        for before,after,expected in cases:
            with self.subTest(expected=expected,after=after),patch.object(context,'current_target',return_value=target),patch.object(context,'_call'),patch.object(context.subprocess,'run',side_effect=[type('R',(),{'stdout':before})(),type('R',(),{'stdout':after})()]):
                self.assertEqual(context.exit_session_matches({},None,job),expected)

    def test_claude_changed_or_unavailable_session_cannot_exit(self):
        target={'provider':'claude'};job={'target':target,'checkpoint_session_id':'old-session'}
        for packet in (None,{'session_id':'new-session'},{'session_id':'old-session','unavailable':True}):
            with patch.object(context,'current_target',return_value=target),patch.object(context,'observe',return_value=packet),patch.object(context,'_call') as call:
                self.assertFalse(context.exit_session_matches({},None,job));call.assert_not_called()


class ClaudeNativeExitTests(unittest.TestCase):
    def test_old_observation_alone_cannot_prove_current_session(self):
        target={'provider':'claude','terminal':'t1'}
        job={'target':target,'checkpoint_session_id':'00000000-0000-0000-0000-000000000001'}
        old={'session_id':job['checkpoint_session_id'],'observed_at':0}
        with patch.object(context,'current_target',return_value=target),patch.object(context,'observe',return_value=old),patch.object(context,'_call'),patch.object(context.subprocess,'run',return_value=type('R',(),{'stdout':'unchanged old screen'})()):
            self.assertFalse(context.exit_session_matches({},None,job))

    def test_fresh_status_session_must_match_and_own_status_dialog_is_closed(self):
        session='00000000-0000-0000-0000-000000000001'
        target={'provider':'claude','terminal':'t1'};job={'target':target,'checkpoint_session_id':session}
        for actual,expected in ((session,True),('00000000-0000-0000-0000-000000000002',False)):
            after='Settings  Status  Config  Usage\nSession ID: '+actual+'\nEsc to cancel'
            with self.subTest(actual=actual),patch.object(context,'current_target',return_value=target),patch.object(context,'observe',return_value={'session_id':session,'observed_at':0}),patch.object(context,'_call') as call,patch.object(context.subprocess,'run',side_effect=[type('R',(),{'stdout':'old response'})(),type('R',(),{'stdout':after})()]):
                self.assertEqual(context.exit_session_matches({},None,job),expected)
                self.assertIn(unittest.mock.call('keys','t1','escape'),call.call_args_list)


class RegisteredEmptyHintTests(unittest.TestCase):
    def test_only_the_registered_slot_hint_is_an_empty_prompt(self):
        hint='Try "edit <filepath> to..."'
        for actual,expected in ((hint,True),('Try "please overwrite my draft"',False)):
            with patch.object(context.subprocess,'run',return_value=type('R',(),{'stdout':'❯ '+actual+'\n──────\nContext'})()):
                self.assertEqual(context.clean_prompt({'status':{'state':'idle'}},{'terminal':'t1','empty_prompt':hint}),expected)


class MultilineDraftGuardTests(unittest.TestCase):
    def test_registered_hint_and_blank_first_line_do_not_hide_multiline_drafts(self):
        from teamkit.prompt_state import empty_input
        hint='Try "edit <filepath> to..."'
        for text in ('❯ '+hint+'\n  unfinished multiline user draft','❯ \n  unfinished multiline user draft','› Ask Codex to do anything\n  my multiline request','› \n  Context please keep this draft'):
            self.assertFalse(empty_input(text,hint))
        self.assertTrue(empty_input('❯ '+hint+'\n────────\nContext',hint))
        self.assertTrue(empty_input('› Ask Codex to do anything\n  Context 100% left · weekly 40% left'))
