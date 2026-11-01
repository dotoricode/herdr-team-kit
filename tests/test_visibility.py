import json
import time
from unittest.mock import patch
from test_cli import Fixture,StoreContext
import test_workflow as workflow_tests
from teamkit.config import binding
from teamkit.dashboard import model,render
from teamkit.policy import eligible
from teamkit.workflow import snapshot


class VisibilityTests(Fixture):
    setup_work=workflow_tests.WorkflowTests.setup_work
    approve=workflow_tests.WorkflowTests.approve
    task=workflow_tests.WorkflowTests.task

    def data(self):
        return snapshot(self.state)

    def test_account_usage_is_shown_and_absence_is_not_zero(self):
        self.setup_work();now=time.time()
        with StoreContext(self.state) as db:
            db.quota('codex/team',{'binding':binding(self.value['profiles']['codex']['team']),
                'observed_at':now,'five_hour':{'used':42,'resets_at':now+900},'weekly':{'used':73,'resets_at':now+900}})
        output=render(model(self.data(),self.value),width=120)
        self.assertIn('Codex 회사',output);self.assertIn('42%',output);self.assertIn('73%',output)
        personal=next(line for line in output.splitlines() if 'Codex 개인' in line)
        self.assertIn('미수집',personal);self.assertNotIn('0%',personal)

    def test_live_work_and_review_request_are_distinct(self):
        self.setup_work();self.call('work','submit','W1');self.task('D1','leader')
        data=self.data();data['tasks'][0]['runtime']={'name':'designer'}
        from teamkit.monitor import namespace
        data['monitor']={'namespace':namespace(self.value),'observed_at':data['observed_at'],'native_ttl':10,
                         'agents':[{'name':'designer','agent_status':'working'}]}
        output=render(model(data,self.value))
        self.assertIn('현재: 작업 중',output)
        self.assertIn('설계 검토 필요',output);self.assertIn('권한 팝업이 아닌',output)
        self.assertIn(str(self.root/'private-plans/W1/r1/plan.md'),output)
        self.assertIn('제품 완료 0',output)
        data['monitor']['agents'][0]['agent_status']='idle'
        self.assertEqual(model(data,self.value)['activity']['alpha']['label'],'대기 중')

    def test_old_native_observation_is_unknown_not_idle(self):
        self.setup_work();data=self.data()
        from teamkit.monitor import namespace
        data['monitor']={'namespace':namespace(self.value),'observed_at':data['observed_at']-20,'native_ttl':10,'agents':[]}
        result=model(data,self.value)
        self.assertEqual(result['activity']['alpha']['label'],'실행 상태 미확인')
        self.assertIsNone(result['activity']['alpha']['working'])

    def test_account_mismatch_stale_expired_and_failed_are_distinguished(self):
        self.setup_work();now=time.time()
        q={'binding':binding(self.value['profiles']['codex']['team']),'observed_at':now-180,
           'five_hour':{'used':42,'resets_at':now+900},'weekly':{'used':73,'resets_at':now-1}}
        with StoreContext(self.state) as db:db.quota('codex/team',q)
        view=model(self.data(),self.value)['accounts'][0]
        self.assertEqual(view['five_hour'],42);self.assertIsNone(view['weekly']);self.assertFalse(view['current'])
        with StoreContext(self.state) as db:db.quota('codex/team',{**q,'binding':'other'})
        view=model(self.data(),self.value)['accounts'][0]
        self.assertIsNone(view['five_hour']);self.assertEqual(view['status'],'미수집')

    def test_provider_failure_invalidates_recent_admission(self):
        q={'observed_at':100,'five_hour':{'used':1,'resets_at':1000},'weekly':{'used':1,'resets_at':1000},'unavailable':True}
        self.assertFalse(eligible(q,101))

    def test_monitor_updates_codex_without_claude_or_model_calls(self):
        self.setup_work()
        from teamkit.monitor import collect_codex,save_quotas,write_cache,namespace
        raw={'rateLimits':{'primary':{'usedPercent':12,'windowDurationMins':300,'resetsAt':time.time()+100},
                           'secondary':{'usedPercent':21,'windowDurationMins':10080,'resetsAt':time.time()+100}}}
        with patch('teamkit.monitor.codex_quota',return_value=raw) as quota:
            results=collect_codex(self.value);self.assertEqual(quota.call_count,2)
        save_quotas(self.state,self.value,results)
        write_cache(self.state,{'namespace':namespace(self.value),'observed_at':time.time(),'native_ttl':10,'agents':[]})
        data=self.data();self.assertEqual(data['quotas']['codex/team']['five_hour']['used'],12)
        with patch('teamkit.providers.identity') as identity,patch('teamkit.runtime.Herdr.call') as native:
            render(model(data,self.value));identity.assert_not_called();native.assert_not_called()
        save_quotas(self.state,self.value,[('team',binding(self.value['profiles']['codex']['team']),None)])
        with StoreContext(self.state) as db:
            cached=db.quota('codex/team');self.assertTrue(cached['unavailable']);self.assertFalse(eligible(cached,time.time()))
        self.assertIn('조회 실패',render(model(self.data(),self.value)))

    def test_new_workspace_label_describes_the_task(self):
        from teamkit.runtime import Herdr
        task={'id':'D1','team':'alpha','role':'leader','classification':{'kind':'design'},'work_id':'W1','root':'/srv/private/design'}
        with patch.object(Herdr,'call',side_effect=[{'root_pane':{'pane_id':'w9:p1'},'workspace':{'workspace_id':'w9'}},{},{'process_info':{'foreground_processes':[{'pid':123,'name':'codex'}]}}]) as call:
            value=Herdr().launch(task,{'env':{},'kind':'codex','args':[]},'tk-opaque')
        self.assertIn('W1 · 설계 · alpha',call.call_args_list[0].args)
        self.assertEqual(value['workspace'],'w9');self.assertTrue(value['ephemeral'])

    def stopped_task(self):
        self.create()
        with StoreContext(self.state) as db:
            task=db.get('T1');task.update(role='leader',provider='codex',state='ready',owner='owner',runtime={'name':'native','pane':'w9:p1','workspace':'w9','workspace_label':'W1 · 설계 · alpha','managed_workspace':True,'ephemeral':True,'native_pid':123})
            db._save(task,'fixture')

    def test_retirement_requires_same_stopped_owner_and_preserves_evidence(self):
        self.stopped_task()
        from teamkit.retirement import retire
        agent={'agent_status':'idle','interactive_ready':True,'pane_id':'w9:p1','workspace_id':'w9','completion_seq':3}
        workspace={'label':'W1 · 설계 · alpha','pane_count':1,'tab_count':1}
        with StoreContext(self.state) as db,patch('teamkit.retirement.Herdr') as h:
            h.return_value.call.side_effect=[{'agent':agent},{'workspace':workspace},{'process_info':{'foreground_processes':[{'pid':123,'name':'codex'}]}},{'agent':agent},{}]
            result=retire(db,self.value,'T1','owner',{'verification':['stopped native owner; no background jobs']},True)
            self.assertTrue(result['runtime']['workspace_closed']);self.assertEqual(result['state'],'ready')
            self.assertTrue(any(e['event']=='workspace_retired' for e in db.events('T1')))
            with self.assertRaises(ValueError):db.wake('T1','owner')
            with self.assertRaises(ValueError):retire(db,self.value,'T1','owner',{'verification':['duplicate']},True)
            self.assertEqual(h.return_value.call.call_count,5)

    def test_retirement_blocks_working_persistent_and_protected_workspaces(self):
        self.stopped_task()
        from teamkit.retirement import retire
        with StoreContext(self.state) as db,patch('teamkit.retirement.Herdr') as h:
            h.return_value.call.return_value={'agent':{'agent_status':'working'}}
            with self.assertRaises(ValueError):retire(db,self.value,'T1','owner',{'verification':['stopped']},True)
            h.return_value.call.assert_called_once()
            self.value['dashboard_workspaces']={'alpha':{'id':'w9','label':'Representative'}}
            h.reset_mock()
            with self.assertRaises(ValueError):retire(db,self.value,'T1','owner',{'verification':['stopped']},True)
            h.assert_not_called()

    def test_foreground_replacement_preserves_workspace(self):
        self.stopped_task()
        from teamkit.retirement import retire
        agent={'agent_status':'idle','interactive_ready':True,'pane_id':'w9:p1','workspace_id':'w9','completion_seq':3}
        with StoreContext(self.state) as db,patch('teamkit.retirement.Herdr') as h:
            h.return_value.call.side_effect=[{'agent':agent},{'workspace':{'label':'W1 · 설계 · alpha','pane_count':1,'tab_count':1}},
                {'process_info':{'foreground_processes':[{'pid':456,'name':'vim'}]}}]
            with self.assertRaises(ValueError):retire(db,self.value,'T1','owner',{'verification':['stopped']},True)
            self.assertEqual(h.return_value.call.call_count,3)
            self.assertNotIn('retire_requested',db.get('T1')['runtime'])

    def test_persistent_leader_is_never_closed(self):
        self.stopped_task()
        from teamkit.retirement import retire
        with StoreContext(self.state) as db,patch('teamkit.retirement.Herdr') as h:
            value=db.get('T1');value['runtime']['ephemeral']=False;db._save(value,'fixture')
            with self.assertRaises(ValueError):retire(db,self.value,'T1','owner',{'verification':['stopped']},True)
            h.assert_not_called()

    def test_unknown_close_outcome_is_not_retried(self):
        self.stopped_task()
        from teamkit.retirement import retire
        from teamkit.runtime import RuntimeError as KitError
        agent={'agent_status':'idle','interactive_ready':True,'pane_id':'w9:p1','workspace_id':'w9','completion_seq':3}
        with StoreContext(self.state) as db,patch('teamkit.retirement.Herdr') as h:
            h.return_value.call.side_effect=[{'agent':agent},{'workspace':{'label':'W1 · 설계 · alpha','pane_count':1,'tab_count':1}},
                {'process_info':{'foreground_processes':[{'pid':123,'name':'codex'}]}},{'agent':agent},KitError('unknown close outcome')]
            with self.assertRaises(KitError):retire(db,self.value,'T1','owner',{'verification':['stopped']},True)
            self.assertTrue(db.get('T1')['runtime']['retire_requested'])
            with self.assertRaises(ValueError):retire(db,self.value,'T1','owner',{'verification':['stopped']},True)
            self.assertEqual(h.return_value.call.call_count,5)

    def test_claude_usage_requires_response_source_and_four_accounts_remain_separate(self):
        self.setup_work();now=time.time()
        self.value['profiles']['claude']={a:{**pr,'home':str(self.root/('claude-'+a))} for a,pr in self.value['profiles']['codex'].items()}
        pr=self.value['profiles']['claude']['personal'];q={'binding':binding(pr),'observed_at':now,
            'five_hour':{'used':26,'resets_at':now+100},'weekly':{'used':38,'resets_at':now+100},'source':'redraw'}
        with StoreContext(self.state) as db:db.quota('claude/personal',q)
        rows=model(self.data(),self.value)['accounts'];self.assertEqual(len(rows),4)
        self.assertIsNone(rows[-1]['five_hour'])
        from teamkit.policy import CLAUDE_QUOTA_SOURCE
        with StoreContext(self.state) as db:db.quota('claude/personal',{**q,'source':CLAUDE_QUOTA_SOURCE})
        rows=model(self.data(),self.value)['accounts'];self.assertEqual(rows[-1]['five_hour'],26);self.assertIsNone(rows[-2]['five_hour'])

    def test_slow_quota_query_does_not_block_native_observation(self):
        from argparse import Namespace
        from threading import Event
        import signal
        from teamkit.monitor import run
        self.setup_work();started=Event();release=Event();reads=[]
        def quota(*unused):started.set();release.wait(5);return []
        def native(*unused):
            started.wait(1);reads.append(1)
            if len(reads)==2:release.set();signal.raise_signal(signal.SIGTERM)
            return []
        old=signal.getsignal(signal.SIGTERM)
        with patch('teamkit.monitor.collect_codex',side_effect=quota),patch('teamkit.monitor.read_native',side_effect=native):
            run(Namespace(config=str(self.config),state=str(self.state),interval=1,quota_interval=30,once=False),self.value)
        self.assertEqual(len(reads),2);self.assertEqual(signal.getsignal(signal.SIGTERM),old)

    def test_review_document_opens_only_current_submitted_revision(self):
        from teamkit.dashboard import open_review
        self.setup_work();self.call('work','submit','W1')
        import subprocess
        real=subprocess.run
        def route(command,**kwargs):
            if command[0] in ('open','xdg-open'):return None
            return real(command,**kwargs)
        with patch('subprocess.run',side_effect=route) as opened:
            open_review(self.state,self.value,'W1')
            commands=[c.args[0] for c in opened.call_args_list if c.args[0][0] in ('open','xdg-open')]
            self.assertEqual(commands[0][1],str((self.root/'private-plans/W1/r1/plan.md').resolve()))
            (self.root/'private-plans/W1/r1/plan.md').write_text('changed after submission')
            with self.assertRaises(ValueError):open_review(self.state,self.value,'W1')
            self.assertEqual(len([c for c in opened.call_args_list if c.args[0][0] in ('open','xdg-open')]),1)

    def test_cached_view_expires_native_data_without_another_ledger_read(self):
        self.setup_work();data=self.data()
        from teamkit.monitor import namespace
        data['monitor']={'namespace':namespace(self.value),'observed_at':data['observed_at'],'native_ttl':10,'agents':[]}
        self.assertEqual(model(data,self.value,now=data['observed_at'])['activity']['alpha']['label'],'대기 중')
        self.assertEqual(model(data,self.value,now=data['observed_at']+11)['activity']['alpha']['label'],'실행 상태 미확인')

    def test_created_pane_is_recorded_when_workspace_metadata_is_invalid(self):
        from teamkit.runtime import Herdr,RuntimeError as KitError
        from unittest.mock import Mock
        task={'id':'D1','team':'alpha','role':'leader','classification':{'kind':'design'},'root':'/srv/private/design'}
        recorded=Mock()
        with patch.object(Herdr,'call',return_value={'root_pane':{'pane_id':'w9:p1'},'workspace':{}}) as native:
            with self.assertRaises(KitError):Herdr().launch(task,{'env':{},'kind':'codex','args':[]},'native',recorded)
        recorded.assert_called_once_with({'name':'native','pane':'w9:p1'})
        self.assertEqual(native.call_count,1)

    def test_missing_ready_and_review_owner_are_unknown_not_idle(self):
        self.setup_work();self.task('D1','leader');data=self.data()
        from teamkit.monitor import namespace
        data['monitor']={'namespace':namespace(self.value),'observed_at':100,'native_ttl':10,'agents':[]}
        data['tasks'][0]['runtime']={'name':'missing-owner'}
        for state in ('ready','awaiting_review'):
            with self.subTest(state=state):
                data['tasks'][0]['state']=state
                activity=model(data,self.value,now=100)['activity']['alpha']
                self.assertEqual(activity['label'],'실행 상태 미확인');self.assertIsNone(activity['working'])

    def test_removed_codex_profile_discards_result(self):
        self.setup_work()
        from teamkit.monitor import save_quotas
        expected=binding(self.value['profiles']['codex']['team'])
        self.value['profiles']={'claude':self.value['profiles']['codex']}
        self.value['teams']['alpha']['roles']['implementer']['provider']='claude'
        self.config.write_text(json.dumps(self.value))
        from teamkit.config import load
        save_quotas(self.state,load(self.config),[('team',expected,None)])
        with StoreContext(self.state) as db:self.assertIsNone(db.quota('codex/team'))

    def test_review_revision_change_during_document_read_is_rejected(self):
        self.setup_work();self.call('work','submit','W1')
        from teamkit.workflow import document
        from teamkit.dashboard import open_review
        import subprocess
        real_run=subprocess.run;changed=[];opened=[]
        def replace(config,work):
            result=document(config,work)
            if not changed:
                changed.append(True);self.call('work','revise','W1');self.call('work','submit','W1')
            return result
        def route(command,**kwargs):
            if command[0] in ('open','xdg-open'):opened.append(command);return None
            return real_run(command,**kwargs)
        with patch('teamkit.workflow.document',side_effect=replace),patch('subprocess.run',side_effect=route):
            with self.assertRaises(ValueError):open_review(self.state,self.value,'W1')
        self.assertFalse(opened);self.assertEqual(self.call('work','show','W1')['revision'],2)

    def test_publisher_rechecks_observation_after_delayed_native_call(self):
        self.setup_work();self.value['dashboard_workspaces']={'alpha':{'id':'w1','label':'Alpha'}}
        from teamkit.monitor import namespace
        from teamkit.dashboard import publish
        data=self.data();data['monitor']={'namespace':namespace(self.value),'observed_at':100,'native_ttl':10,
            'agents':[{'name':'representative','workspace_id':'w1','agent_status':'working'}]}
        view=model(data,self.value,now=100);clock=[100];sent=[]
        def native(*args):
            if args[:2]==('workspace','get'):clock[0]=111;return {'workspace':{'label':'Alpha'}}
            sent.append(args);return {}
        with patch('teamkit.dashboard.Herdr') as h,patch('teamkit.dashboard.time.time',side_effect=lambda:clock[0]):
            h.return_value.call.side_effect=native;publish(view,self.value)
        summary=next(x for x in sent[0] if x.startswith('work_summary='))
        self.assertIn('실행 상태 미확인',summary)

    def test_company_failure_blocks_route_before_personal_query_finishes(self):
        self.setup_work()
        from argparse import Namespace
        from threading import Event
        import signal
        from teamkit.monitor import run
        now=time.time();expected=binding(self.value['profiles']['codex']['team'])
        with StoreContext(self.state) as db:db.quota('codex/team',{'binding':expected,'observed_at':now,
            'five_hour':{'used':1,'resets_at':now+1000},'weekly':{'used':1,'resets_at':now+1000}})
        release=Event();personal_started=Event();observed=[]
        def quota(pr):
            if pr['home']==self.value['profiles']['codex']['team']['home']:raise OSError('synthetic failure')
            personal_started.set();release.wait(5);return {}
        def native(*args):
            personal_started.wait(1)
            observed.append(self.call('route','--provider','codex')['action'])
            if len(observed)==2:release.set();signal.raise_signal(signal.SIGTERM)
            return []
        try:
            with patch('teamkit.monitor.codex_quota',side_effect=quota),patch('teamkit.monitor.read_native',side_effect=native):
                run(Namespace(config=str(self.config),state=str(self.state),interval=1,quota_interval=30,once=False),self.value)
        finally:release.set()
        self.assertEqual(observed[-1],'wait')

    def test_manual_usage_is_display_only_and_never_admission_quota(self):
        self.setup_work();now=time.time()
        self.value['profiles']['claude']={'team':{**self.value['profiles']['codex']['team'],'home':str(self.root/'claude')}}
        pr=self.value['profiles']['claude']['team']
        from pathlib import Path
        path=Path(str(self.state.resolve())+'.usage-observations.json')
        value={'claude/team':{'source':'native-cli-manual-observation','binding':binding(pr),
            'observed_at':now,'five_hour':0,'weekly':100}}
        path.write_text(json.dumps(value))
        result=model(self.data(),self.value,now=now)['accounts'][-1]
        self.assertEqual(result['five_hour'],0);self.assertEqual(result['weekly'],100)
        self.assertTrue(result['display_only']);self.assertEqual(result['status'],'CLI 확인값')
        self.assertNotIn('claude/team',self.data()['quotas'])
        self.assertEqual(model(self.data(),self.value,now=now+121)['accounts'][-1]['status'],'CLI 이전 확인값')
        for key,val in [('binding','other'),('observed_at',now+1),('weekly',101),('source','redraw')]:
            bad={'claude/team':{**value['claude/team'],key:val}};path.write_text(json.dumps(bad))
            self.assertIsNone(model(self.data(),self.value,now=now)['accounts'][-1]['five_hour'])

    def test_inline_review_is_read_only_and_refuses_modified_submission(self):
        from teamkit.dashboard import review_document
        self.setup_work();self.call('work','submit','W1')
        old=self.data()['works'][0]
        ident,title,body=review_document(self.state,self.value,'W1')
        self.assertEqual(ident,'W1');self.assertEqual(body,self.plan.read_text().rstrip('\n'))
        self.assertEqual(self.data()['works'][0],old)
        (self.root/'private-plans/W1/r1/plan.md').write_text('unsubmitted edit')
        with self.assertRaises(ValueError):review_document(self.state,self.value,'W1')
