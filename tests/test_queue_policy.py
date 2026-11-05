import json
from pathlib import Path
import sqlite3

from test_cli import Fixture, StoreContext
from teamkit import queue_policy as policy


class QueuePolicyTests(Fixture):
    def setUp(self):
        super().setUp()
        self.value['private_plan_root'] = str(self.root/'plans')
        self.value['require_design_approval'] = True
        self.config.write_text(json.dumps(self.value))
        self.contract = self.root/'contract.json'
        self.contract.write_text(json.dumps({'title':'Bounded work', 'requirements':['One behavior'],
            'allowed_paths':['feature.txt'], 'steps':[{'id':'behavior','title':'Check behavior','weight':100}]}))
        self.plan = self.root/'plan.md'
        self.plan.write_text('Implement one approved behavior.')
        self.call('work','create','W1','--team','alpha','--root',str(self.repo),
                  '--plan-file',str(self.plan),'--contract-file',str(self.contract))
        self.job = {'id':'a'*32, 'team':'alpha', 'phase':'pending'}

    def approve(self):
        self.call('work','submit','W1')
        self.call('work','approve','W1','--user-approved','--approval-reference','Fixture approval')

    def linked(self):
        self.job['workflow_ref'] = policy.capture_work(self.value,self.state,'alpha','W1')
        self.job['resources'] = policy.resources(self.value,'alpha',self.job['workflow_ref'])
        return self.job

    def test_capture_is_read_only_and_missing_does_not_create_storage(self):
        before = self.state.read_bytes()
        mode = self.state.stat().st_mode
        reference = policy.capture_work(self.value,self.state,'alpha','W1')
        self.assertEqual(reference['work_id'],'W1')
        self.assertEqual(reference['revision'],1)
        self.assertEqual(reference['root'],str(self.repo.resolve()))
        self.assertEqual(len(reference['digest']),64)
        self.assertEqual(self.state.read_bytes(),before)
        self.assertEqual(self.state.stat().st_mode,mode)
        missing = self.root/'not-created'/'state.sqlite'
        with self.assertRaisesRegex(ValueError,'exist'):
            policy.capture_work(self.value,missing,'alpha','W1')
        self.assertFalse(missing.parent.exists())
        self.assertIsNone(policy.capture_work(self.value,missing,'alpha',None))

    def test_unapproved_waits_then_exact_approval_admits(self):
        job = self.linked()
        self.assertIsNotNone(policy.admission(self.value,self.state,job,[job]))
        self.approve()
        self.assertIsNone(policy.admission(self.value,self.state,job,[job]))

    def test_document_or_revision_change_does_not_inherit_approval(self):
        self.approve();job = self.linked()
        plan = self.root/'plans/W1/r1/plan.md'
        plan.write_text('A different unapproved behavior.')
        self.assertIsNotNone(policy.admission(self.value,self.state,job,[job]))
        with self.assertRaises(ValueError):policy.completion(self.value,self.state,job)
        plan.write_text('Implement one approved behavior.')
        self.call('work','revise','W1')
        self.assertIsNotNone(policy.admission(self.value,self.state,job,[job]))

    def test_capture_rejects_team_or_root_outside_configured_scope(self):
        self.value['teams']['beta'] = self.value['teams']['alpha']
        with self.assertRaises(ValueError):policy.capture_work(self.value,self.state,'beta','W1')
        self.value['teams']['alpha']['root'] = str(self.root/'other')
        with self.assertRaises(ValueError):policy.capture_work(self.value,self.state,'alpha','W1')

    def test_dependencies_require_verified_completion(self):
        self.job['dependencies'] = ['b'*32]
        for phase in ('pending','received','done','deferred','cancelled','uncertain'):
            with self.subTest(phase=phase):
                previous = {'id':'b'*32,'team':'beta','phase':phase}
                self.assertIsNotNone(policy.admission(self.value,self.state,self.job,[previous,self.job]))
        previous['phase'] = 'completed'
        self.assertIsNone(policy.admission(self.value,self.state,self.job,[previous,self.job]))
        self.assertIsNotNone(policy.admission(self.value,self.state,self.job,[self.job]))

    def test_shared_parent_and_symlink_alias_block_active_other_team(self):
        self.value['teams']['beta'] = {'root':str(self.root),'roles':self.value['teams']['alpha']['roles']}
        self.job['resources'] = policy.resources(self.value,'alpha')
        other = {'id':'b'*32,'team':'beta','phase':'received','resources':[str(self.root)]}
        self.assertIsNotNone(policy.admission(self.value,self.state,self.job,[other,self.job]))
        other['phase'] = 'pending'
        self.assertIsNone(policy.admission(self.value,self.state,self.job,[other,self.job]))
        other['phase'] = 'uncertain'
        alias = self.root/'alias'
        alias.symlink_to(self.repo, target_is_directory=True)
        other['resources'] = [str(alias)]
        self.assertIsNotNone(policy.admission(self.value,self.state,self.job,[other,self.job]))
        self.value['teams']['beta']['root'] = str(self.root/'independent')
        other['resources'] = [str(self.root/'independent')]
        self.assertIsNone(policy.admission(self.value,self.state,self.job,[other,self.job]))

    def test_extra_resources_cannot_expand_authorized_roots(self):
        shared = self.root/'shared'
        self.value['shared_roots'] = [str(shared)]
        roots = policy.resources(self.value,'alpha',extra=[str(shared/'server')])
        self.assertIn(str((shared/'server').resolve()),roots)
        for value in ('relative',str(self.root/'not-authorized')):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):policy.resources(self.value,'alpha',extra=[value])
        alias = self.repo/'outside-alias'
        alias.symlink_to(self.root/'not-authorized',target_is_directory=True)
        with self.assertRaises(ValueError):policy.resources(self.value,'alpha',extra=[str(alias)])

    def finish_work(self, approved=False):
        if not approved:self.approve()
        evidence = self.root/'step.json'
        evidence.write_text(json.dumps({'verification':['Fixture consumer behavior checked']}))
        self.call('work','record','W1','--step','behavior','--evidence-file',str(evidence))
        self.call('create','R1','--team','alpha','--role','reviewer','--root',str(self.repo),
                  '--goal-file',str(self.root/'goal.txt'),'--work','W1')
        with StoreContext(self.state) as store:
            store.claim('R1','review-owner','codex','team','fixture','high',approval_config=self.value)
            store.update('R1','review-owner','launching',{'name':'review-fixture'})
            store.update('R1','review-owner','submitted',{})
            store.receipt('R1','review-owner')
            head = store.get('R1')['base']
            store.complete('R1','review-owner',head,['Independent fixture review'],head,'pass')
        return self.call('work','finish','W1','--review-task','R1')

    def test_completion_uses_real_formal_finish_without_writing(self):
        self.approve();job = self.linked()
        self.assertIsNone(policy.completion(self.value,self.state,job))
        finished = self.finish_work(approved=True)
        before = self.state.read_bytes()
        result = policy.completion(self.value,self.state,job)
        self.assertEqual(result['work_id'],'W1')
        self.assertEqual(result['completed_head'],finished['completed_head'])
        self.assertEqual(result['review_task'],'R1')
        self.assertTrue(result['verification'])
        self.assertEqual(self.state.read_bytes(),before)

    def test_completion_refuses_missing_or_stale_milestone_and_reviewer(self):
        self.finish_work();job = self.linked()
        for mutation in ('head','review','milestone'):
            with self.subTest(mutation=mutation):
                with StoreContext(self.state) as store:
                    row = store.db.execute('SELECT body FROM work_items WHERE id=?',('W1',)).fetchone()[0]
                    work = json.loads(row)
                    if mutation == 'head':work['completed_head'] = 'invalid'
                    elif mutation == 'review':work['review_task'] = 'MISSING'
                    else:work['milestones']['behavior']['head'] = 'f'*40
                    store.db.execute('UPDATE work_items SET body=? WHERE id=?',(json.dumps(work),'W1'))
                with self.assertRaises(ValueError):policy.completion(self.value,self.state,job)
                with StoreContext(self.state) as store:store.db.execute('UPDATE work_items SET body=? WHERE id=?',(row,'W1'))

    def test_admission_reuses_the_original_approved_path_gate(self):
        self.approve();job = self.linked()
        (self.repo/'unapproved.txt').write_text('Existing source change outside contract.')
        self.assertIsNotNone(policy.admission(self.value,self.state,job,[job]))
        self.assertEqual((self.repo/'unapproved.txt').read_text(),'Existing source change outside contract.')

    def test_missing_document_is_not_a_false_success(self):
        self.approve();job = self.linked()
        (self.root/'plans/W1/r1/plan.md').unlink()
        self.assertIsNotNone(policy.admission(self.value,self.state,job,[job]))
        with self.assertRaisesRegex(ValueError,'documents'):policy.completion(self.value,self.state,job)

    def test_latest_unresolved_review_prevents_automatic_completion(self):
        self.finish_work();job = self.linked()
        with StoreContext(self.state) as store:
            other = store.get('R1')
            other.update(id='R2', state='queued', owner=None)
            store.db.execute('INSERT INTO tasks VALUES (?,?)',('R2',json.dumps(other)))
        with self.assertRaisesRegex(ValueError,'unresolved'):policy.completion(self.value,self.state,job)

    def test_invalid_dependencies_fail_closed(self):
        for values in ([{}],['not-a-request'],['b'*32,'b'*32]):
            with self.subTest(values=values):
                self.job['dependencies'] = values
                with self.assertRaises(ValueError):policy.admission(self.value,self.state,self.job,[self.job])

    def test_wal_is_rejected_without_creating_side_files(self):
        other = self.root/'wal.sqlite'
        with sqlite3.connect(other) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE work_items(id TEXT PRIMARY KEY,body TEXT)')
        db.close()
        before = set(self.root.iterdir())
        with self.assertRaisesRegex(ValueError,'WAL'):
            policy.capture_work(self.value,other,'alpha','W1')
        self.assertEqual(set(self.root.iterdir()),before)

    def test_unlinked_completion_does_not_read_or_create_ledger(self):
        self.assertIsNone(policy.completion(self.value,self.root/'missing.sqlite',self.job))
        self.assertFalse((self.root/'missing.sqlite').exists())
