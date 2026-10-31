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
        result=self.call('work','finish','W1','--review-task','R2')
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
