import json
import os
import subprocess
from unittest.mock import patch

from test_cli import Fixture, StoreContext


class WorkflowTests(Fixture):
    def setup_work(self):
        self.value.update(private_plan_root=str(self.root/'private-plans'), require_design_approval=True)
        self.config.write_text(json.dumps(self.value))
        self.contract=self.root/'contract.json'
        self.contract.write_text(json.dumps({'title':'One bounded feature','requirements':['Preserve existing result semantics'],
            'allowed_paths':['feature.txt','tests/'], 'steps':[{'id':'behavior','title':'Verify behavior','weight':100}]}))
        self.plan=self.root/'plan.md';self.plan.write_text('One approved design. Keep ownership and failure behavior explicit.')
        return self.call('work','create','W1','--team','alpha','--root',str(self.repo),
                         '--plan-file',str(self.plan),'--contract-file',str(self.contract))

    def approve(self):
        self.call('work','submit','W1')
        return self.call('work','approve','W1','--user-approved','--approval-reference','User selected revision 1')

    def task(self, name='T1', role='implementer'):
        return self.call('create',name,'--team','alpha','--role',role,'--root',str(self.repo),
                         '--goal-file',str(self.root/'goal.txt'),'--work','W1')

    def test_unapproved_implementation_stops_before_provider_or_herdr(self):
        self.setup_work();self.task()
        with patch.dict(os.environ,{'HERDR_ENV':'1'}),patch('teamkit.providers.identity') as identity,patch('teamkit.__main__.Herdr') as herdr:
            with self.assertRaises(ValueError):self.call('dispatch','T1')
            identity.assert_not_called();herdr.assert_not_called()
        self.assertEqual(self.call('show','T1')['state'],'queued')

    def test_private_plan_never_uses_git_storage(self):
        self.value['private_plan_root']=str(self.repo/'private-plans')
        self.config.write_text(json.dumps(self.value))
        from teamkit.workflow import private_root
        with self.assertRaises(ValueError):private_root(self.value,create=True)
        self.assertFalse((self.repo/'private-plans').exists())

    def test_approval_binds_exact_plan_and_survives_new_store(self):
        work=self.setup_work();approved=self.approve()
        self.assertEqual(approved['phase'],'approved')
        task=self.task()
        from teamkit.workflow import check_task
        from teamkit.config import load
        with StoreContext(self.state) as store:
            self.assertEqual(check_task(load(self.config),store,task)['approval']['reference'],'User selected revision 1')
        path=self.root/'private-plans'/'W1'/'r1'/'plan.md'
        path.write_text('Unapproved semantic change')
        with StoreContext(self.state) as store:
            with self.assertRaises(ValueError):check_task(load(self.config),store,task)

    def test_changed_submitted_plan_cannot_be_approved(self):
        self.setup_work();self.call('work','submit','W1')
        (self.root/'private-plans/W1/r1/plan.md').write_text('changed after user inspection')
        with self.assertRaises(ValueError):self.call('work','approve','W1','--user-approved','--approval-reference','old revision')
        self.assertEqual(self.call('work','show','W1')['phase'],'awaiting_approval')

    def test_scope_violation_refuses_execution_without_overwriting_file(self):
        self.setup_work();self.approve();self.task()
        path=self.repo/'unrelated.txt';path.write_text('existing other work')
        with patch.dict(os.environ,{'HERDR_ENV':'1'}),patch('teamkit.providers.identity') as identity:
            with self.assertRaises(ValueError):self.call('dispatch','T1')
            identity.assert_not_called()
        self.assertEqual(path.read_text(),'existing other work')

    def test_approval_race_during_identity_read_does_not_claim(self):
        import time
        from teamkit.config import binding
        self.setup_work();self.approve();self.task()
        now=time.time()
        with StoreContext(self.state) as db:
            db.quota('codex/team',{'binding':binding(self.value['profiles']['codex']['team']),'observed_at':now,
                'five_hour':{'used':1,'resets_at':now+1000},'weekly':{'used':1,'resets_at':now+1000}})
        def change(*args):
            (self.root/'private-plans/W1/r1/plan.md').write_text('changed during preflight')
        with patch.dict(os.environ,{'HERDR_ENV':'1'}),patch('teamkit.providers.identity',side_effect=change),patch('teamkit.__main__.Herdr') as h:
            with self.assertRaises(ValueError):self.call('dispatch','T1')
            h.assert_not_called()
        self.assertEqual(self.call('show','T1')['state'],'queued')

    def test_revision_invalidates_old_tasks_and_retains_old_design(self):
        self.setup_work();self.approve();self.task()
        changed=self.call('work','revise','W1')
        self.assertEqual(changed['revision'],2)
        self.assertTrue((self.root/'private-plans/W1/r1/plan.md').is_file())
        self.approve()
        with patch.dict(os.environ,{'HERDR_ENV':'1'}):
            with self.assertRaises(ValueError):self.call('dispatch','T1')

    def test_restart_requires_exact_recorded_files_and_does_not_expire_daily(self):
        self.setup_work();self.approve();self.task()
        evidence=self.root/'restart.json';evidence.write_text(json.dumps({'verification':['previous unit verified'], 'next':'next bounded unit'}))
        self.call('work','checkpoint','W1','--evidence-file',str(evidence))
        from teamkit.workflow import check_task
        with StoreContext(self.state) as db,patch('teamkit.workflow.time.time',return_value=10**12):
            self.assertIsNotNone(check_task(self.value,db,db.get('T1')))
        (self.repo/'feature.txt').write_text('changed while stopped')
        with StoreContext(self.state) as db:
            with self.assertRaises(ValueError):check_task(self.value,db,db.get('T1'))

    def test_active_owner_prevents_revision_or_checkpoint(self):
        self.setup_work();self.approve();self.task()
        with StoreContext(self.state) as db:db.claim('T1','owner','codex','team','fixture','medium',approval_config=self.value)
        with self.assertRaises(ValueError):self.call('work','revise','W1')
        evidence=self.root/'restart.json';evidence.write_text(json.dumps({'verification':['claim is not termination'], 'next':'next'}))
        with self.assertRaises(ValueError):self.call('work','checkpoint','W1','--evidence-file',str(evidence))

    def test_all_steps_stay_below_100_without_latest_independent_review(self):
        from teamkit.dashboard import model
        from teamkit.workflow import snapshot
        self.setup_work();self.approve()
        evidence=self.root/'step.json';evidence.write_text(json.dumps({'verification':['fixture behavior checked']}))
        self.call('work','record','W1','--step','behavior','--evidence-file',str(evidence))
        self.assertEqual(model(snapshot(self.state),self.value)['rows'][0]['percent'],99)
        with self.assertRaises(ValueError):self.call('work','finish','W1','--review-task','missing')

    def test_verified_completion_needs_distinct_review_at_current_clean_head(self):
        from teamkit.dashboard import model
        from teamkit.workflow import snapshot
        self.setup_work();self.approve();self.task()
        with StoreContext(self.state) as db:
            db.claim('T1','implementer-owner','codex','team','fixture','medium',approval_config=self.value)
            db.update('T1','implementer-owner','launching',{'name':'implementation','pane':'w1:p1'})
            db.update('T1','implementer-owner','submitted',{});db.receipt('T1','implementer-owner')
            head=db.get('T1')['base'];db.complete('T1','implementer-owner',head,['fixture verified'])
        evidence=self.root/'step.json';evidence.write_text(json.dumps({'verification':['fixture behavior checked']}))
        self.call('work','record','W1','--step','behavior','--evidence-file',str(evidence))
        self.task('R1','reviewer')
        with StoreContext(self.state) as db:
            db.claim('R1','review-owner','codex','team','fixture','high',approval_config=self.value)
            db.update('R1','review-owner','launching',{'name':'reviewer','pane':'w2:p1'})
            db.update('R1','review-owner','submitted',{});db.receipt('R1','review-owner')
            db.complete('R1','review-owner',head,['independent fixture review'],head,'findings')
        with self.assertRaises(ValueError):self.call('work','finish','W1','--review-task','R1')
        self.task('R2','reviewer')
        with StoreContext(self.state) as db:
            db.claim('R2','review-owner-2','codex','team','fixture','high',approval_config=self.value)
            db.update('R2','review-owner-2','launching',{'name':'reviewer-2','pane':'w3:p1'})
            db.update('R2','review-owner-2','submitted',{});db.receipt('R2','review-owner-2')
            db.complete('R2','review-owner-2',head,['independent fixture review'],head,'pass')
        self.task('R3','reviewer')
        with self.assertRaises(ValueError):self.call('work','finish','W1','--review-task','R2')
        for name,verdict in (('R3','findings'),('R4','pass')):
            if name=='R4':self.task(name,'reviewer')
            with StoreContext(self.state) as db:
                owner=name+'-owner'
                db.claim(name,owner,'codex','team','fixture','high',approval_config=self.value)
                db.update(name,owner,'launching',{'name':name,'pane':'w4:p1'})
                db.update(name,owner,'submitted',{});db.receipt(name,owner)
                db.complete(name,owner,head,['latest independent review'],head,verdict)
            if name=='R3':
                with self.assertRaises(ValueError):self.call('work','finish','W1','--review-task','R2')
        result=self.call('work','finish','W1','--review-task','R4')
        self.assertEqual(result['phase'],'complete')
        self.assertEqual(model(snapshot(self.state),self.value)['rows'][0]['percent'],100)

    def test_private_source_document_is_rejected_if_already_in_git(self):
        self.setup_work()
        self.contract.rename(self.repo/'contract.json')
        with self.assertRaises(ValueError):self.call('work','create','W2','--team','alpha','--root',str(self.repo),
            '--plan-file',str(self.plan),'--contract-file',str(self.repo/'contract.json'))

    def test_private_directory_cannot_be_converted_to_git_after_approval(self):
        self.setup_work();self.approve();self.task()
        subprocess.run(['git','init','-q',str(self.root/'private-plans')],check=True)
        from teamkit.workflow import check_task
        with StoreContext(self.state) as db:
            with self.assertRaises(ValueError):check_task(self.value,db,db.get('T1'))

    def test_nested_revision_git_is_rejected_before_dispatch_or_revision_copy(self):
        self.setup_work();self.approve();self.task()
        subprocess.run(['git','init','-q',str(self.root/'private-plans/W1/r1')],check=True)
        from teamkit.workflow import check_task
        with StoreContext(self.state) as db:
            with self.assertRaises(ValueError):check_task(self.value,db,db.get('T1'))
        with self.assertRaises(ValueError):self.call('work','revise','W1')
        self.assertFalse((self.root/'private-plans/W1/r2').exists())

    def test_readonly_dashboard_never_calls_provider_or_creates_missing_state(self):
        from teamkit.dashboard import model,render
        from teamkit.workflow import snapshot
        missing=self.root/'missing'/'state.sqlite'
        with patch('teamkit.providers.identity') as identity,patch('teamkit.runtime.Herdr.call') as native:
            data=model(snapshot(missing),self.value)
            self.assertIn('등록된 제품 작업 없음',render(data))
            identity.assert_not_called();native.assert_not_called()
        self.assertFalse(missing.parent.exists())

    def test_dashboard_counts_product_work_not_model_attempts(self):
        from teamkit.dashboard import model
        from teamkit.workflow import snapshot
        self.setup_work();self.approve();self.task('T1');self.task('T2')
        view=model(snapshot(self.state),self.value)
        self.assertEqual(sum(view['teams']['alpha'].values()),1)
        self.assertEqual(view['rows'][0]['attempts'],0)
        with StoreContext(self.state) as db:
            db.claim('T1','first-owner','codex','team','fixture','medium',approval_config=self.value)
            db.note('T1','first-owner',{'name':'old-attempt','identity_checked':True})
            task=db.get('T1')
            db.handoff('T1','first-owner',{'head':task['base'],'dirty':False,'verification':['fake writer stopped'],'next':'continue'},True)
            current=db.claim('T1','next-owner','codex','team','fixture','medium',approval_config=self.value)
            self.assertEqual(current['runtime'],{})
        view=model(snapshot(self.state),self.value)
        self.assertEqual(view['rows'][0]['attempts'],2)

    def test_progress_text_cannot_inject_terminal_commands(self):
        from teamkit.dashboard import clean
        value=clean('title\x1b[2J\r\n')
        self.assertNotIn('\x1b',value);self.assertNotIn('\n',value)

    def test_design_launch_uses_high_private_cwd_and_private_temporary_files(self):
        import time
        from pathlib import Path
        from teamkit.config import binding
        self.setup_work()
        self.value['teams']['alpha']['roles']['leader']={'provider':'codex','model':'fixture','effort':'low'}
        self.value['routing']={'design':{'provider':'codex','model':'fixture','effort':'high'}}
        self.config.write_text(json.dumps(self.value))
        self.call('create','DESIGN1','--team','alpha','--role','leader','--work-kind','design',
            '--root',str(self.repo),'--work','W1','--goal-file',str(self.root/'goal.txt'))
        now=time.time()
        with StoreContext(self.state) as db:
            db.quota('codex/team',{'binding':binding(self.value['profiles']['codex']['team']),'observed_at':now,
                'five_hour':{'used':1,'resets_at':now+1000},'weekly':{'used':1,'resets_at':now+1000}})
        with patch.dict(os.environ,{'HERDR_ENV':'1'}),patch('teamkit.providers.identity'),patch('teamkit.__main__.Herdr') as h:
            h.return_value.launch.return_value={'name':'designer','pane':'w1:p1'}
            result=self.call('dispatch','DESIGN1')
            task,spec=h.return_value.launch.call_args.args[:2]
            self.assertEqual(result['effort'],'high')
            self.assertEqual(Path(task['root']),(self.root/'private-plans/W1/r1').resolve())
            self.assertEqual(Path(spec['env']['TMPDIR']),(self.root/'private-plans/W1/r1/.tmp').resolve())
            self.assertIn('sandbox_workspace_write.exclude_tmpdir_env_var=false',spec['args'])
            self.assertIn('PLANNING ONLY',h.return_value.prompt.call_args.args[1])

    def test_tui_highlight_uses_display_columns_after_korean_text(self):
        from argparse import Namespace
        from unittest.mock import MagicMock
        from teamkit.dashboard import live
        self.setup_work();self.call('work','submit','W1')
        screen=MagicMock();screen.getmaxyx.return_value=(40,120);screen.getch.return_value=ord('q')
        args=Namespace(state=str(self.state),publish=False,color='always',interval=3)
        with patch('teamkit.dashboard.curses.curs_set'),patch('teamkit.dashboard.curses.has_colors',return_value=True),patch('teamkit.dashboard.curses.start_color'),patch('teamkit.dashboard.curses.use_default_colors'),patch('teamkit.dashboard.curses.init_pair'),patch('teamkit.dashboard.curses.color_pair',return_value=0):
            live(screen,args,self.value)
        rows={c.args[0]:c.args[2] for c in screen.addnstr.call_args_list}
        for call in screen.addstr.call_args_list:
            y,x,label=call.args[:3]
            if label=='승인 대기':
                prefix=rows[y].split(label,1)[0]
                expected=sum(2 if '\uac00'<=char<='\ud7a3' else 1 for char in prefix)
                self.assertEqual(x,expected,'color overlay must not overwrite Korean text or counts')

    def test_preexisting_private_work_symlink_cannot_receive_revision_files(self):
        from pathlib import Path
        self.setup_work()
        for ident,target in (('W2',self.repo),('W3',self.root/'team')):
            target.mkdir(exist_ok=True)
            (self.root/'private-plans'/ident).symlink_to(target,target_is_directory=True)
            with self.assertRaises(OSError):self.call('work','create',ident,'--team','alpha','--root',str(self.repo),
                '--plan-file',str(self.plan),'--contract-file',str(self.contract))
            self.assertFalse((target/'r1').exists())

    def test_rename_cannot_hide_deletion_outside_approved_paths(self):
        path=self.repo/'unrelated.txt';path.write_text('keep tracked source')
        subprocess.run(['git','-C',str(self.repo),'add','unrelated.txt'],check=True)
        subprocess.run(['git','-C',str(self.repo),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','baseline'],check=True)
        self.setup_work();self.approve();self.task()
        subprocess.run(['git','-C',str(self.repo),'mv','unrelated.txt','feature.txt'],check=True)
        from teamkit.workflow import check_task
        with StoreContext(self.state) as db:
            with self.assertRaises(ValueError):check_task(self.value,db,db.get('T1'))
        self.assertEqual((self.repo/'feature.txt').read_text(),'keep tracked source')

    def test_old_planning_execution_cannot_adopt_new_revision(self):
        self.setup_work()
        self.value['teams']['alpha']['roles']['leader']={'provider':'codex','model':'fixture','effort':'low'}
        self.value['routing']={'design':{'provider':'codex','model':'fixture','effort':'high'}}
        self.config.write_text(json.dumps(self.value))
        self.call('create','DESIGN1','--team','alpha','--role','leader','--work-kind','design',
            '--root',str(self.repo),'--work','W1','--goal-file',str(self.root/'goal.txt'))
        self.call('work','revise','W1')
        with patch.dict(os.environ,{'HERDR_ENV':'1'}),patch('teamkit.providers.identity') as identity:
            with self.assertRaises(ValueError):self.call('dispatch','DESIGN1')
            identity.assert_not_called()

    def test_empty_native_metadata_ack_is_not_a_lifecycle_ack(self):
        from types import SimpleNamespace
        from teamkit.runtime import Herdr,RuntimeError
        with patch.dict(os.environ,{'HERDR_ENV':'1'}),patch('teamkit.runtime.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='',stderr='')):
            self.assertTrue(Herdr().call('--session','fixture','workspace','report-metadata','w1','--source','test','--token','x=1')['metadata_accepted'])
            with self.assertRaises(RuntimeError):Herdr().call('agent','start','worker','--kind','codex','--pane','w1:p1')

    def assert_stale_design_tell_refused(self, during_status):
        self.setup_work()
        self.call('create','D1','--team','alpha','--role','leader','--work-kind','design',
            '--root',str(self.repo),'--work','W1','--goal-file',str(self.root/'goal.txt'))
        with StoreContext(self.state) as db:
            db.claim('D1','owner','codex','team','fixture','high',approval_config=self.value)
            db.update('D1','owner','launching',{'name':'old-designer'})
            db.update('D1','owner','submitted',{})
            db.receipt('D1','owner');db.park('D1','owner')
        def revise(*args):
            self.call('work','revise','W1')
            return {'agent_status':'idle'}
        if not during_status:self.call('work','revise','W1')
        with patch.dict(os.environ,{'HERDR_ENV':'1'}),patch('teamkit.__main__.Herdr') as herdr:
            if during_status:herdr.return_value.call.side_effect=revise
            else:herdr.return_value.call.return_value={'agent_status':'idle'}
            with self.assertRaises(ValueError):
                self.call('tell','D1','--message-file',str(self.root/'goal.txt'))
            herdr.return_value.prompt.assert_not_called()
        self.assertEqual(self.call('show','D1')['state'],'ready')

    def test_tell_refuses_parked_design_from_old_revision(self):
        self.assert_stale_design_tell_refused(False)

    def test_tell_rechecks_revision_changed_during_status_read(self):
        self.assert_stale_design_tell_refused(True)

    def test_relocated_private_root_refuses_existing_design(self):
        import shutil
        from teamkit.workflow import document
        self.setup_work();self.approve()
        relocated=self.root/'relocated'
        shutil.copytree(self.root/'private-plans',relocated)
        self.value['private_plan_root']=str(relocated)
        with StoreContext(self.state) as db:
            from teamkit.workflow import get
            with self.assertRaises(ValueError):document(self.value,get(db,'W1'))

    def test_design_claim_clears_resolved_quota_hold(self):
        from teamkit.workflow import get,save
        self.setup_work()
        self.call('create','D1','--team','alpha','--role','leader','--work-kind','design',
            '--root',str(self.repo),'--work','W1','--goal-file',str(self.root/'goal.txt'))
        with StoreContext(self.state) as db:
            with db.transaction():
                w=get(db,'W1');w['hold']={'kind':'quota','reason':'waiting'};save(db,w,'quota_wait')
            db.claim('D1','designer','codex','team','fixture','high',approval_config=self.value)
            self.assertNotIn('hold',get(db,'W1'))
            self.assertEqual(get(db,'W1')['phase'],'planning')

    def test_finish_requires_step_evidence_for_current_source(self):
        self.setup_work();self.approve()
        evidence=self.root/'step.json';evidence.write_text(json.dumps({'verification':['old behavior checked']}))
        self.call('work','record','W1','--step','behavior','--evidence-file',str(evidence))
        (self.repo/'feature.txt').write_text('changed after verification')
        subprocess.run(['git','-C',str(self.repo),'add','feature.txt'],check=True)
        subprocess.run(['git','-C',str(self.repo),'-c','user.name=Fixture','-c','user.email=test@example.invalid','commit','-qm','change'],check=True)
        self.task('R1','reviewer')
        with StoreContext(self.state) as db:
            db.claim('R1','reviewer','codex','team','fixture','high',approval_config=self.value)
            db.update('R1','reviewer','launching',{'name':'independent'})
            db.update('R1','reviewer','submitted',{});db.receipt('R1','reviewer')
            head=db.get('R1')['base'];db.complete('R1','reviewer',head,['review'],head,'pass')
        with self.assertRaises(ValueError):self.call('work','finish','W1','--review-task','R1')
        self.call('work','record','W1','--step','behavior','--evidence-file',str(evidence))
        self.assertEqual(self.call('work','finish','W1','--review-task','R1')['phase'],'complete')

    def test_complete_work_is_not_counted_as_environment_wait(self):
        from teamkit.dashboard import model,publish
        from teamkit.workflow import snapshot,get,save
        self.setup_work();self.approve();self.task()
        with StoreContext(self.state) as db:
            task=db.get('T1');task.update(state='handed_off',checkpoint={'failure':{'kind':'environment','cause':'fixture'}})
            with db.transaction():
                db._save(task,'fixture')
                work=get(db,'W1');work['phase']='complete';save(db,work,'fixture')
        view=model(snapshot(self.state),self.value)
        self.assertEqual(view['rows'][0]['phase'],'complete')
        self.assertEqual(view['rows'][0]['percent'],100)
        self.assertEqual(view['teams']['alpha'],{'complete':1})
        self.value['dashboard_workspaces']={'alpha':{'id':'w1','label':'Alpha'}}
        with patch('teamkit.dashboard.Herdr') as h:
            h.return_value.call.return_value={'workspace':{'label':'Alpha'}}
            publish(view,self.value)
            args=h.return_value.call.call_args.args
            self.assertIn('work_summary=작업 1 · 완료 1',args)
            self.assertIn('work_blocked=',args)

    def test_publisher_renews_before_ttl_with_long_snapshot_interval(self):
        from argparse import Namespace
        from unittest.mock import MagicMock
        from teamkit.dashboard import live
        self.setup_work()
        screen=MagicMock();screen.getmaxyx.return_value=(40,120)
        screen.getch.side_effect=[-1,-1,-1,ord('q')]
        args=Namespace(state=str(self.state),publish=True,color='never',interval=60)
        with patch('teamkit.dashboard.curses.curs_set'),patch('teamkit.dashboard.time.monotonic',side_effect=[100,110,120,130]),patch('teamkit.dashboard.publish') as pub:
            live(screen,args,self.value)
        self.assertEqual(pub.call_count,4)

    def test_progress_bar_uses_verified_percent_and_lists_remaining_steps(self):
        from teamkit.dashboard import model,render
        from teamkit.workflow import snapshot
        self.setup_work();self.approve()
        view=model(snapshot(self.state),self.value)
        output=render(view)
        self.assertIn('[░░░░░░░░░░░░░░░░░░░░]   0% 0/1',output)
        self.assertIn('남은 작업 1건',output)
        self.assertIn('남은 단계: Verify behavior',output)
        evidence=self.root/'step.json';evidence.write_text(json.dumps({'verification':['checked']}))
        self.call('work','record','W1','--step','behavior','--evidence-file',str(evidence))
        output=render(model(snapshot(self.state),self.value))
        self.assertIn('[███████████████████░]  99% 1/1',output)
        self.assertIn('남은 단계: 최종 검증·독립 리뷰·완료 확인',output)

    def test_unapproved_progress_is_unknown_and_complete_has_no_remaining_work(self):
        from teamkit.dashboard import model,render
        from teamkit.workflow import snapshot
        self.setup_work()
        view=model(snapshot(self.state),self.value)
        self.assertIn('미정 · 0/1',render(view))
        self.approve()
        view=model(snapshot(self.state),self.value)
        view['rows'][0].update(phase='complete',percent=100,steps_done=1,remaining_steps=[])
        output=render(view)
        self.assertIn('[████████████████████] 100% 1/1',output)
        self.assertIn('남은 작업 0건',output)
        self.assertNotIn('남은 단계:',output)

    def test_narrow_live_screen_keeps_numeric_progress_visible(self):
        from argparse import Namespace
        from unittest.mock import MagicMock
        from teamkit.dashboard import live
        self.setup_work();self.approve()
        evidence=self.root/'step.json';evidence.write_text(json.dumps({'verification':['checked']}))
        self.call('work','record','W1','--step','behavior','--evidence-file',str(evidence))
        screen=MagicMock();screen.getmaxyx.return_value=(40,40);screen.getch.return_value=ord('q')
        with patch('teamkit.dashboard.curses.curs_set'):
            live(screen,Namespace(state=str(self.state),publish=False,color='never',interval=3),self.value)
        shown=[c.args[2][:c.args[3]] for c in screen.addnstr.call_args_list]
        self.assertTrue(any('[███████████████████░]  99% 1/1' in line for line in shown))

    def test_block_progress_keeps_counts_and_percent_at_narrow_widths(self):
        from teamkit.dashboard import model,render
        from teamkit.workflow import snapshot
        self.setup_work();self.approve()
        evidence=self.root/'step.json';evidence.write_text(json.dumps({'verification':['checked']}))
        self.call('work','record','W1','--step','behavior','--evidence-file',str(evidence))
        view=model(snapshot(self.state),self.value)
        for width in (19,39,59,79,119):
            with self.subTest(width=width):
                line=next(line for line in render(view,width=width).splitlines() if '99%' in line)
                self.assertLessEqual(len(line),width)
                self.assertIn('99% 1/1',line)
                self.assertIn('█',line)
                self.assertNotIn('#',line)
