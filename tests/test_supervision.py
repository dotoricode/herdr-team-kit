import json
from unittest.mock import patch
from test_cli import Fixture, StoreContext


class SupervisionTests(Fixture):
    def running(self):
        task=self.create()
        with StoreContext(self.state) as db:
            db.claim('T1','owner','codex','team','fixture','medium')
            db.update('T1','owner','launching',{'name':'fixture','pane':'w1:p1','context_footer':True,'supervision_enrolled':True})
            db.update('T1','owner','submitted',{})
            db.receipt('T1','owner')
        self.value['context_supervision']=True
        self.config.write_text(json.dumps(self.value))
        return task

    def test_native_footer_only_and_unknown_remain_distinct(self):
        from teamkit.supervisor import context_used
        self.assertEqual(context_used('model low · Context 100% left\n','codex',True),0)
        self.assertEqual(context_used('model low · Context 30% left\n','codex',True),70)
        for text in ('Context 101% left','Context unknown','Context 30% left\nprompt',''):
            self.assertIsNone(context_used(text,'codex',True))
        self.assertIsNone(context_used('model · Context 30% left','codex',False))
        self.assertIsNone(context_used('model · Context 30% left','claude',True))

    def test_boundary_pins_actual_git_and_requires_current_receipted_owner(self):
        task=self.running();p=self.root/'boundary.json';p.write_text(json.dumps({'verification':['real fixture check'],'next':'next bounded step'}))
        with self.assertRaises(ValueError):self.call('boundary','T1','--owner','wrong','--evidence-file',str(p))
        result=self.call('boundary','T1','--owner','owner','--evidence-file',str(p),'--rotate-if-unknown')
        self.assertEqual(result['state'],'running')
        self.assertEqual(result['boundary']['head'],task['base'])
        self.assertEqual(result['boundary']['owner'],'owner')
        self.assertEqual(len(result['boundary']['worktree_fingerprint']),64)

    def test_prepare_advice_never_stops_a_worker_without_boundary(self):
        from teamkit.supervisor import supervise_once
        self.running()
        with patch('teamkit.supervisor.Herdr') as h:
            h.return_value.call.return_value={'agent':{'name':'fixture','agent':'codex','agent_status':'done','pane_id':'w1:p1'}}
            h.return_value.read.return_value='model · Context 25% left\n'
            result=supervise_once(self.value,self.state,'T1','owner',apply=False)
            self.assertEqual(result['action'],'prepare')
            self.assertFalse(any(c.args[:2]==('agent','prompt') for c in h.return_value.call.call_args_list))
        self.assertEqual(self.call('show','T1')['state'],'running')

    def boundary(self):
        self.running();p=self.root/'boundary.json';p.write_text(json.dumps({'verification':['fixture check'],'next':'continue fixture'}))
        return self.call('boundary','T1','--owner','owner','--evidence-file',str(p),'--rotate-if-unknown')

    def native(self,h,remaining=10,status='done',exit_error=False):
        from teamkit.runtime import RuntimeError
        agent={'name':'fixture','agent':'codex','agent_status':status,'pane_id':'w1:p1','interactive_ready':True}
        info={'process_info':{'pane_id':'w1:p1','shell_pid':1,'foreground_process_group_id':125,'foreground_processes':[{'pid':125,'name':'codex'}]}}
        def call(*args):
            if args[:2]==('agent','get'):return {'agent':agent}
            if args[:2]==('pane','process-info'):return info
            if args[:2]==('agent','prompt') and exit_error:raise RuntimeError('unknown delivery')
            if args[:2]==('agent','prompt'):return {'agent':dict(agent,agent_status='working')}
            return {}
        h.return_value.call.side_effect=call
        h.return_value.read.return_value=f'model · Context {remaining}% left\n'

    def test_busy_or_changed_worktree_never_stops_writer(self):
        from teamkit.supervisor import supervise_once
        self.boundary()
        with patch('teamkit.supervisor.Herdr') as h:
            self.native(h,status='working')
            self.assertEqual(supervise_once(self.value,self.state,'T1','owner')['action'],'busy')
            self.native(h)
            (self.repo/'changed.txt').write_text('uncheckpointed')
            self.assertEqual(supervise_once(self.value,self.state,'T1','owner')['action'],'inspect')
            self.assertFalse(any(c.args[:2]==('agent','prompt') for c in h.return_value.call.call_args_list))

    def test_known_low_context_continues_same_owner_only_once(self):
        import os
        from teamkit.supervisor import supervise_once
        self.boundary()
        with patch.dict(os.environ,{'HERDR_ENV':'1'}),patch('teamkit.supervisor.Herdr') as h:
            self.native(h,remaining=99)
            self.assertEqual(supervise_once(self.value,self.state,'T1','owner',apply=True)['action'],'continued')
            self.assertEqual(supervise_once(self.value,self.state,'T1','owner',apply=True)['action'],'keep')
            self.assertEqual(sum(c.args[:2]==('agent','prompt') for c in h.return_value.call.call_args_list),1)
        self.assertEqual(self.call('show','T1')['owner'],'owner')

    def test_unknown_continue_delivery_is_not_repeated(self):
        import os
        from teamkit.supervisor import supervise_once
        from teamkit.runtime import RuntimeError
        self.boundary()
        with patch.dict(os.environ,{'HERDR_ENV':'1'}),patch('teamkit.supervisor.Herdr') as h:
            self.native(h,remaining=99,exit_error=True)
            with self.assertRaises(RuntimeError):supervise_once(self.value,self.state,'T1','owner',apply=True)
            self.assertEqual(supervise_once(self.value,self.state,'T1','owner',apply=True)['action'],'inspect')
            self.assertEqual(sum(c.args[:2]==('agent','prompt') for c in h.return_value.call.call_args_list),1)

    def test_graceful_exit_then_verified_stop_records_handoff(self):
        import os
        from teamkit.supervisor import supervise_once
        self.boundary()
        with patch.dict(os.environ,{'HERDR_ENV':'1'}),patch('teamkit.supervisor.Herdr') as h,patch('teamkit.supervisor.process_family',return_value=[125]),patch('teamkit.supervisor.stopped',return_value=True):
            self.native(h)
            self.assertEqual(supervise_once(self.value,self.state,'T1','owner',apply=True)['action'],'handed_off')
            h.return_value.call.assert_any_call('agent','prompt','fixture','/quit')
        task=self.call('show','T1');self.assertEqual(task['state'],'handed_off');self.assertIsNone(task['owner'])
        self.assertEqual(task['checkpoint']['next'],'continue fixture')

    def test_uncertain_exit_retains_owner_and_never_resends(self):
        import os
        from teamkit.supervisor import supervise_once
        from teamkit.runtime import RuntimeError
        self.boundary()
        with patch.dict(os.environ,{'HERDR_ENV':'1'}),patch('teamkit.supervisor.Herdr') as h,patch('teamkit.supervisor.process_family',return_value=[125]),patch('teamkit.supervisor.stopped',return_value=False):
            self.native(h,exit_error=True)
            with self.assertRaises(RuntimeError):supervise_once(self.value,self.state,'T1','owner',apply=True)
            self.assertEqual(supervise_once(self.value,self.state,'T1','owner',apply=True)['action'],'inspect')
            self.assertEqual(sum(c.args[:2]==('agent','prompt') for c in h.return_value.call.call_args_list),1)
        task=self.call('show','T1');self.assertEqual((task['state'],task['owner']),('stopping','owner'))

    def test_live_descendant_prevents_stopped_claim(self):
        import subprocess,sys,os
        from teamkit.supervisor import stopped
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
        try:
            with patch('teamkit.supervisor.Herdr') as h:
                h.return_value.call.return_value={'process_info':{'pane_id':'w1:p1','shell_pid':os.getpid(),'foreground_process_group_id':os.getpid(),'foreground_processes':[{'pid':os.getpid()}]}}
                task={'runtime':{'pane':'w1:p1','supervision_pending':{'pids':[child.pid]}}}
                self.assertFalse(stopped(h.return_value,task))
        finally:
            child.terminate();child.wait(timeout=5)

    def test_stop_race_is_rejected_atomically(self):
        self.boundary()
        with StoreContext(self.state) as db:
            stale=db.get('T1');db.note('T1','owner',{'changed':True})
            with self.assertRaises(ValueError):db.supervise_transition('T1','owner',stale,stopping=True,detail={'pids':[125]})
            self.assertEqual(db.get('T1')['state'],'running')

    def test_dispatch_wires_opt_in_watcher_and_native_footer(self):
        import os,time
        from teamkit.config import binding
        self.create();self.value['context_supervision']=True;self.config.write_text(json.dumps(self.value))
        now=time.time()
        with StoreContext(self.state) as db:
            db.quota('codex/team',{'binding':binding(self.value['profiles']['codex']['team']),'observed_at':now,
                'five_hour':{'used':1,'resets_at':now+900},'weekly':{'used':1,'resets_at':now+900}})
        with patch.dict(os.environ,{'HERDR_ENV':'1'}),patch('teamkit.providers.identity'),patch('teamkit.__main__.Herdr') as h,patch('teamkit.supervisor.start_watcher',return_value=12345) as start:
            h.return_value.launch.return_value={'name':'fixture','pane':'w1:p1'}
            task=self.call('dispatch','T1')
            self.assertTrue(task['runtime']['context_footer'])
            self.assertEqual(task['runtime']['supervisor_pid'],12345)
            start.assert_called_once()
            self.assertIn('tui.status_line=["model-with-reasoning", "context-remaining"]',h.return_value.launch.call_args.args[1]['args'])
            self.assertIn('--rotate-if-unknown',h.return_value.prompt.call_args.args[1])

    def test_resume_refreshes_only_selected_codex_provider_then_dispatches(self):
        from argparse import Namespace
        from teamkit.supervisor import resume_task
        self.boundary()
        with patch('teamkit.__main__.run',return_value={'action':'wait'}) as call:
            result=resume_task(Namespace(config=str(self.config),state=str(self.state),task='T1'),self.value)
            self.assertEqual(result['action'],'wait')
            self.assertEqual([c.args[0].command for c in call.call_args_list],['refresh','refresh','dispatch'])
            self.assertEqual([c.args[0].provider for c in call.call_args_list[:2]],['codex','codex'])

    def test_applied_supervision_requires_real_managed_environment(self):
        import os
        from teamkit.supervisor import supervise_once
        self.running()
        with patch.dict(os.environ,{'HERDR_ENV':''}),patch('teamkit.supervisor.Herdr') as h:
            with self.assertRaises(ValueError):supervise_once(self.value,self.state,'T1','owner',apply=True)
            h.assert_not_called()

    def test_failed_boundary_preserves_failure_for_stopped_handoff(self):
        self.running();p=self.root/'boundary.json'
        p.write_text(json.dumps({'verification':['bounded attempt failed'],'next':'diagnose cause',
                                'failure':{'cause':'same','kind':'reasoning'}}))
        result=self.call('boundary','T1','--owner','owner','--evidence-file',str(p))
        self.assertEqual(result['boundary']['failure']['cause'],'same')

    def test_real_process_and_group_must_both_stop(self):
        import subprocess,sys,os
        from teamkit.supervisor import stopped,process_family
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)
        try:
            pids=process_family([child.pid]);self.assertIn(child.pid,pids)
            with patch('teamkit.supervisor.Herdr') as h:
                h.return_value.call.return_value={'process_info':{'pane_id':'w1:p1','shell_pid':os.getpid(),'foreground_process_group_id':os.getpid(),'foreground_processes':[{'pid':os.getpid()}]}}
                task={'runtime':{'pane':'w1:p1','supervision_pending':{'pids':pids,'pgid':child.pid}}}
                self.assertFalse(stopped(h.return_value,task))
                child.terminate();child.wait(timeout=5)
                self.assertTrue(stopped(h.return_value,task))
        finally:
            if child.poll() is None:child.terminate();child.wait(timeout=5)

    def test_new_attempt_does_not_inherit_old_runtime_or_boundary(self):
        task=self.boundary()
        with StoreContext(self.state) as db:
            db.note('T1','owner',{'supervision_uncertain':True,'supervisor_pid':123})
            proof={k:v for k,v in task['boundary'].items() if k not in ('owner','rotate_if_unknown')}
            db.handoff('T1','owner',proof,True)
            new=db.claim('T1','next-owner','codex','team','fixture','medium')
            self.assertEqual(new['runtime'],{})
            self.assertNotIn('boundary',new)

    def test_auth_home_overlap_or_missing_ledger_cannot_be_opened(self):
        from teamkit.supervisor import supervise_once
        target=self.root/'team'/'state.sqlite';target.parent.mkdir();target.write_bytes(b'private sentinel')
        with patch('teamkit.supervisor.Herdr') as h:
            with self.assertRaises(ValueError):supervise_once(self.value,target,'T1','owner')
            h.assert_not_called()
        self.assertEqual(target.read_bytes(),b'private sentinel')
        missing=self.root/'missing'/'state.sqlite'
        with self.assertRaises(ValueError):supervise_once(self.value,missing,'T1','owner')
        self.assertFalse(missing.parent.exists())

    def test_environment_checkpoint_waits_without_model_or_quota_calls(self):
        from argparse import Namespace
        from teamkit.supervisor import resume_task
        task=self.boundary()
        proof={k:v for k,v in task['boundary'].items() if k not in ('owner','rotate_if_unknown')}
        proof['failure']={'cause':'missing-tool','kind':'environment'}
        with StoreContext(self.state) as db:db.handoff('T1','owner',proof,True)
        with patch('teamkit.__main__.run') as call:
            self.assertEqual(resume_task(Namespace(config=str(self.config),state=str(self.state),task='T1'),self.value)['action'],'wait_environment')
            call.assert_not_called()

    def test_invalid_boundary_never_releases_or_stops_owner(self):
        self.running();p=self.root/'boundary.json'
        for proof in ({'verification':[],'next':'next'}, {'verification':['actual'],'next':'next','failure':{'cause':'x','kind':'invalid'}}):
            p.write_text(json.dumps(proof))
            with self.assertRaises(ValueError):self.call('boundary','T1','--owner','owner','--evidence-file',str(p))
            self.assertEqual(self.call('show','T1')['state'],'running')

    def test_controller_lock_excludes_same_attempt_and_partitions_ledgers(self):
        from teamkit.supervisor import controller_lock
        self.running()
        with controller_lock(self.state,'T1','owner'):
            with self.assertRaises(ValueError):
                with controller_lock(self.state,'T1','owner'):pass
            with controller_lock(self.state.with_name('other.sqlite'),'T1','owner'):pass

    def test_low_context_cannot_send_into_unrelated_foreground(self):
        import os
        from teamkit.supervisor import supervise_once
        self.boundary()
        with patch.dict(os.environ,{'HERDR_ENV':'1'}),patch('teamkit.supervisor.Herdr') as h:
            self.native(h,remaining=99)
            previous=h.return_value.call.side_effect
            def call(*args):
                if args[:2]==('pane','process-info'):
                    return {'process_info':{'pane_id':'w1:p1','shell_pid':1,'foreground_process_group_id':126,'foreground_processes':[{'pid':126,'name':'editor'}]}}
                return previous(*args)
            h.return_value.call.side_effect=call
            self.assertEqual(supervise_once(self.value,self.state,'T1','owner',apply=True)['action'],'inspect')
            self.assertFalse(any(c.args[:2]==('agent','prompt') for c in h.return_value.call.call_args_list))
        self.assertIn('boundary',self.call('show','T1'))

    def test_index_only_content_change_changes_fingerprint(self):
        import subprocess
        from teamkit.checkpoint import fingerprint
        path=self.repo/'tracked.txt';path.write_text('base')
        subprocess.run(['git','-C',str(self.repo),'add','tracked.txt'],check=True)
        subprocess.run(['git','-C',str(self.repo),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','base'],check=True)
        path.write_text('index-a');subprocess.run(['git','-C',str(self.repo),'add','tracked.txt'],check=True);path.write_text('working')
        first=fingerprint(self.repo)
        path.write_text('index-b');subprocess.run(['git','-C',str(self.repo),'add','tracked.txt'],check=True);path.write_text('working')
        self.assertNotEqual(fingerprint(self.repo),first)

    def test_disabled_at_launch_attempt_is_not_adopted_after_config_opt_in(self):
        import os
        from teamkit.supervisor import supervise_once
        self.boundary()
        with StoreContext(self.state) as db:db.note('T1','owner',{'supervision_enrolled':False})
        with patch.dict(os.environ,{'HERDR_ENV':'1'}),patch('teamkit.supervisor.Herdr') as h:
            self.native(h,remaining=99)
            self.assertEqual(supervise_once(self.value,self.state,'T1','owner',apply=True)['action'],'inactive')
            h.assert_not_called()

    def test_native_becoming_working_during_observation_is_not_quit(self):
        import os
        from teamkit.supervisor import supervise_once
        self.boundary()
        with patch.dict(os.environ,{'HERDR_ENV':'1'}),patch('teamkit.supervisor.Herdr') as h,patch('teamkit.supervisor.process_family',return_value=[125]),patch('teamkit.supervisor.stopped',return_value=True):
            self.native(h)
            old=h.return_value.call.side_effect;gets=[]
            def call(*args):
                result=old(*args)
                if args[:2]==('agent','get'):
                    gets.append(True)
                    if len(gets)>1:result['agent']['agent_status']='working'
                return result
            h.return_value.call.side_effect=call
            self.assertEqual(supervise_once(self.value,self.state,'T1','owner',apply=True)['action'],'inspect')
            self.assertFalse(any(c.args[:2]==('agent','prompt') for c in h.return_value.call.call_args_list))

    def test_continuation_intent_is_durable_before_transport(self):
        self.boundary()
        with StoreContext(self.state) as db:
            before=db.get('T1');db.supervise_transition('T1','owner',before)
        task=self.call('show','T1')
        self.assertEqual(task['runtime']['supervision_delivery']['status'],'pending')
        self.assertTrue(task['runtime']['supervision_delivery']['token'])

    def test_git_change_during_process_observation_prevents_exit(self):
        import os
        from teamkit.supervisor import supervise_once
        self.boundary()
        with patch.dict(os.environ,{'HERDR_ENV':'1'}),patch('teamkit.supervisor.Herdr') as h,patch('teamkit.supervisor.process_family',return_value=[125]),patch('teamkit.supervisor.stopped',return_value=True):
            self.native(h);old=h.return_value.call.side_effect
            def call(*args):
                if args[:2]==('pane','process-info'):(self.repo/'late.txt').write_text('uncheckpointed')
                return old(*args)
            h.return_value.call.side_effect=call
            self.assertEqual(supervise_once(self.value,self.state,'T1','owner',apply=True)['action'],'inspect')
            self.assertFalse(any(c.args[:2]==('agent','prompt') for c in h.return_value.call.call_args_list))

    def test_accepted_exit_can_wait_and_recover_without_second_exit(self):
        import os
        from teamkit.supervisor import supervise_once
        self.boundary()
        with patch.dict(os.environ,{'HERDR_ENV':'1'}),patch('teamkit.supervisor.Herdr') as h,patch('teamkit.supervisor.process_family',return_value=[125]),patch('teamkit.supervisor.stopped',return_value=False) as stopped,patch('teamkit.supervisor.time.sleep'):
            self.native(h)
            self.assertEqual(supervise_once(self.value,self.state,'T1','owner',apply=True)['action'],'waiting_exit')
            stopped.return_value=True
            self.assertEqual(supervise_once(self.value,self.state,'T1','owner',apply=True)['action'],'handed_off')
            self.assertEqual(sum(c.args[:2]==('agent','prompt') for c in h.return_value.call.call_args_list),1)

    def test_settled_native_without_receipt_exits_watch_for_inspection(self):
        from argparse import Namespace
        from teamkit.supervisor import watch
        self.create();self.value['context_supervision']=True;self.config.write_text(json.dumps(self.value))
        with StoreContext(self.state) as db:
            db.claim('T1','owner','codex','team','fixture','medium')
            db.update('T1','owner','launching',{'name':'fixture','pane':'w1:p1','supervision_enrolled':True})
            db.update('T1','owner','submitted',{})
        with patch('teamkit.supervisor.Herdr') as h:
            h.return_value.call.return_value={'agent':{'name':'fixture','pane_id':'w1:p1','agent':'codex','agent_status':'done','completion_seq':1}}
            result=watch(Namespace(config=str(self.config),state=str(self.state),task='T1',owner='owner',interval=.2,max_seconds=1))
            self.assertEqual(result['action'],'inspect')
            h.return_value.read.assert_not_called()
        self.assertEqual(self.call('show','T1')['state'],'submitted')

    def test_foreign_provider_acknowledgement_is_not_accepted(self):
        import os
        from teamkit.supervisor import supervise_once
        from teamkit.runtime import RuntimeError
        self.boundary()
        with patch.dict(os.environ,{'HERDR_ENV':'1'}),patch('teamkit.supervisor.Herdr') as h:
            self.native(h,remaining=99);old=h.return_value.call.side_effect
            def call(*args):
                result=old(*args)
                if args[:2]==('agent','prompt'):result['agent']['agent']='claude'
                return result
            h.return_value.call.side_effect=call
            with self.assertRaises(RuntimeError):supervise_once(self.value,self.state,'T1','owner',apply=True)
        task=self.call('show','T1');self.assertEqual(task['runtime']['supervision_delivery']['status'],'pending')

    def test_continuation_rechecks_git_after_final_foreground_query(self):
        import os
        from teamkit.supervisor import supervise_once
        self.boundary()
        with patch.dict(os.environ,{'HERDR_ENV':'1'}),patch('teamkit.supervisor.Herdr') as h:
            self.native(h,remaining=99)
            old=h.return_value.call.side_effect;queries=[]
            def call(*args):
                result=old(*args)
                if args[:2]==('pane','process-info'):
                    queries.append(True)
                    if len(queries)==2:(self.repo/'late-edit.txt').write_text('external write')
                return result
            h.return_value.call.side_effect=call
            self.assertEqual(supervise_once(self.value,self.state,'T1','owner',apply=True)['action'],'inspect')
            self.assertFalse(any(c.args[:2]==('agent','prompt') for c in h.return_value.call.call_args_list))
        self.assertIn('boundary',self.call('show','T1'))
