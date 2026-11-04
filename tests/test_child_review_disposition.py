import json
import hashlib
import subprocess
from pathlib import Path
import unittest
from teamkit.tickets import TicketStore, file_digest, digest
from tests import test_tickets as fixtures


class ChildReviewDispositionTest(unittest.TestCase):
    setUp = fixtures.TicketsTest.setUp
    spec = fixtures.TicketsTest.spec
    executor = fixtures.TicketsTest.executor
    observation = fixtures.TicketsTest.observation
    def prepare(self, production_verifier=False):
        self.queue.enqueue(self.spec('P',kind='review',review_required=False,verification_commands={'source':['true']}), 'p', user_approved=True)
        ex=self.executor()
        parent=self.queue.reserve('P',ex,{},self.observation())
        self.queue.receipt('P',parent['id'],ex['owner'],ex['terminal'],dict(ex,status='idle'))
        artifact=self.root/'parent.md'; artifact.write_text('Original authored review')
        def git(*args):
            return subprocess.check_output(['git','-C',str(self.root),*args],stderr=subprocess.DEVNULL)
        if not (self.root/'.git').exists():
            git('init','-q'); (self.root/'source.c').write_text('base\n');git('add','source.c')
            git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','base')
            (self.root/'source.c').write_text('head\n');git('add','source.c')
            git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','head')
        base=git('rev-parse','HEAD^').decode().strip();head=git('rev-parse','HEAD').decode().strip()
        source={'root':str(self.root),'head':head,'fingerprint':hashlib.sha256(git('diff','--no-ext-diff','--no-textconv',base,head)).hexdigest()}
        self.queue.submit('P',parent['id'],ex['owner'],dict(artifact=str(artifact),writer_stopped=True,source=source))
        self.queue.record_stop('P',parent['id'],dict(ex,status='done'))
        child=self.spec('C',kind='review',role='reviewer',parent_id='P',parent_attempt=parent['id'],relation='required',
                        exclude_owners=[ex['owner']],verification_commands={'source':['true']})
        if production_verifier:
            from teamkit.artifact_verifier import command
            child['verification_commands'] = {'source': command(self.queue.attempt(parent['id'])['submission'])}
        saved = self.queue.enqueue(child,'c',user_approved=True)
        if saved['state'] == 'waiting_approval':
            approval = self.root/'child-scope-approval.json'
            approval.write_text(json.dumps(self.queue.approval_template('C')['packet']))
            self.queue.approve('C', str(approval))
        plan=dict(self.executor(owner='external-plan'),new=True,terminal=None,pid=None,runtime={})
        attempt=self.queue.reserve('C',plan,{},self.observation())
        self.queue.delivery(attempt['id'],False,dict(error='ValueError',reason='product execution requires linked approved work'))
        report=self.root/'independent.json'; report.write_text(json.dumps(dict(artifact_digest=file_digest(artifact),source=source,verdict='pass',findings=[])))
        authority=self.root/'operator-approval.txt'; authority.write_text('Approve replacing unsent child with independently performed review evidence')
        packet=dict(child='C',attempt=attempt['id'],parent='P',parent_attempt=parent['id'],
            contract_digest=digest({k:self.queue.get('C').get(k) for k in ('goal','scope','resources','checks','verification_commands','approval','execution_constraint','exclude_owners','parent_id','parent_attempt','relation')}),
            parent_artifact_digest=file_digest(artifact),source=source,source_proof=dict(method='git-diff-sha256-v1',base=base),artifact=str(report),artifact_digest=file_digest(report),
            approval_reference=str(authority),approval_digest=file_digest(authority),
            reviewer=dict(provider='codex',account='personal',identity='independent Codex reviewer',not_parent_author=True),
            independence_evidence='Operator checked independent reviewer authorship and exact source; this is not an actor receipt.',
            verdict='pass',findings=[],reason='Exact review already performed outside failed native attempt; do not duplicate')
        evidence=self.root/'disposition.json'; evidence.write_text(json.dumps(packet))
        return attempt, evidence

    def apply(self, attempt, evidence, approved=True):
        from teamkit.child_review_disposition import replace
        return replace(self.queue,'C',attempt['id'],str(evidence),operator_approved=approved)

    def test_unsent_child_replaced_without_fabricated_completion(self):
        attempt,evidence=self.prepare()
        self.assertEqual(['C'],self.queue.incomplete_children('P'))
        child=self.apply(attempt,evidence)
        self.assertEqual('cancelled',child['state'])
        self.assertEqual([],self.queue.incomplete_children('P'))
        old=self.queue.attempt(attempt['id'])
        self.assertEqual('preflight_replaced',old['phase'])
        self.assertNotIn('actor_receipt',old);self.assertNotIn('submission',old)
        self.assertEqual('awaiting_verification',self.queue.get('P')['state'])
        packet=json.loads(evidence.read_text());Path(packet['artifact']).write_text('changed')
        self.assertEqual(['C'],self.queue.incomplete_children('P'))

    def test_user_approval_is_required(self):
        attempt,evidence=self.prepare()
        with self.assertRaises(ValueError):self.apply(attempt,evidence,False)
        self.assertEqual(['C'],self.queue.incomplete_children('P'))

    def test_launch_receipt_submission_and_other_failures_never_released(self):
        for field,value in [('launch_intent',{'at':1}),('actor_receipt',{'owner':'x'}),('submission',{'artifact':'x'}),('delivery',{'accepted':True})]:
            with self.subTest(field=field):
                attempt,evidence=self.prepare_case()
                current=self.queue.attempt(attempt['id']);current[field]=value;self.queue._save_attempt(current)
                with self.assertRaises(ValueError):self.apply(attempt,evidence)
                self.assertEqual(['C'],self.queue.incomplete_children('P'))

    def prepare_case(self):
        self.queue.db.execute('DELETE FROM operation_tickets');self.queue.db.execute('DELETE FROM operation_attempts');self.queue.db.execute('DELETE FROM operation_budgets')
        if self.queue.db.execute("select 1 from sqlite_master where name='resource_reservations'").fetchone(): self.queue.db.execute('DELETE FROM resource_reservations')
        return self.prepare()

    def test_parent_source_and_scope_and_independence_changes_rejected(self):
        for field,value in [('source',{'head':'different'}),('verdict','fail'),('findings',[{'blocking':True}]),('parent_artifact_digest','bad'),('contract_digest','bad'),('reviewer',{'provider':'claude','account':'team','identity':'owner-one','not_parent_author':False})]:
            with self.subTest(field=field):
                attempt,evidence=self.prepare_case();packet=json.loads(evidence.read_text());packet[field]=value;evidence.write_text(json.dumps(packet))
                with self.assertRaises(ValueError):self.apply(attempt,evidence)

    def test_changed_approval_or_parent_result_reblocks_required_edge(self):
        attempt,evidence=self.prepare(); self.apply(attempt,evidence)
        packet=json.loads(evidence.read_text());Path(packet['approval_reference']).write_text('changed')
        self.assertEqual(['C'],self.queue.incomplete_children('P'))

    def test_stale_attempt_and_non_review_parent_rejected(self):
        attempt,evidence=self.prepare()
        with self.assertRaises(ValueError):
            from teamkit.child_review_disposition import replace
            replace(self.queue,'C','unknown',str(evidence),operator_approved=True)
        parent=self.queue.get('P');parent['review_required']=True;self.queue._save(parent,'fixture')
        with self.assertRaises(ValueError):self.apply(attempt,evidence)

    def test_replacement_does_not_complete_parent_and_is_idempotent(self):
        attempt,evidence=self.prepare();first=self.apply(attempt,evidence)
        self.assertEqual(first,self.apply(attempt,evidence))
        self.assertEqual('awaiting_verification',self.queue.get('P')['state'])
        current=self.queue.attempt(attempt['id']);current['actor_receipt']={'owner':'unexpected'};self.queue._save_attempt(current)
        self.assertEqual(['C'],self.queue.incomplete_children('P'))

    def test_staged_unclaimed_task_cancelled_but_history_preserved(self):
        from teamkit.store import Store
        attempt,evidence=self.prepare(); legacy=Store(self.queue.path)
        ex=self.queue.attempt(attempt['id'])['executor']
        ex.update(task='OP-STAGED',policy_role='reviewer',actor_root=str(self.root),base='a'*40)
        current=self.queue.attempt(attempt['id']);current['executor']=ex;self.queue._save_attempt(current)
        legacy.create(ex['task'],'example','reviewer',str(self.root),'a'*40,self.queue.get('C')['goal'])
        self.apply(attempt,evidence)
        self.assertEqual('cancelled',legacy.get(ex['task'])['state'])
        self.assertIsNone(legacy.get(ex['task'])['owner'])
        self.assertEqual([],self.queue.incomplete_children('P'))
        legacy.close()

    def test_production_verifier_valid_fail_cannot_be_replaced_by_packet_pass(self):
        from teamkit.operations import verify
        attempt,evidence=self.prepare(production_verifier=True)
        packet=json.loads(evidence.read_text())
        actual=dict(artifact_digest=packet['parent_artifact_digest'],source=packet['source'],verdict='fail',
                    findings=[dict(blocking=True,problem='real defect',evidence='fixed source',impact='wrong result')])
        Path(packet['artifact']).write_text(json.dumps(actual));packet['artifact_digest']=file_digest(packet['artifact'])
        evidence.write_text(json.dumps(packet))
        # The production verifier correctly validates fail as a well-formed review;
        # its zero exit status is not an approval verdict.
        proof=verify(self.queue.get('C'),{'submission':dict(artifact=packet['artifact'],artifact_digest=packet['artifact_digest'])})
        self.assertEqual({'source':True},proof['checks'])
        with self.assertRaises(ValueError):self.apply(attempt,evidence)
        self.assertEqual(['C'],self.queue.incomplete_children('P'))
        self.assertEqual('uncertain',self.queue.get('C')['state'])

    def test_actual_review_must_match_parent_and_all_packet_findings(self):
        for field,value in [('source',{'head':'other'}),('artifact_digest','other'),
                            ('findings',[{'blocking':False,'problem':'nonblocking observation'}])]:
            with self.subTest(field=field):
                attempt,evidence=self.prepare_case(); packet=json.loads(evidence.read_text())
                actual=json.loads(Path(packet['artifact']).read_text()); actual[field]=value
                Path(packet['artifact']).write_text(json.dumps(actual)); packet['artifact_digest']=file_digest(packet['artifact'])
                evidence.write_text(json.dumps(packet))
                with self.assertRaises(ValueError):self.apply(attempt,evidence)
                self.assertEqual(['C'],self.queue.incomplete_children('P'))

    def test_production_verifier_pass_adopted_then_review_change_reblocks(self):
        attempt,evidence=self.prepare(production_verifier=True)
        self.apply(attempt,evidence)
        self.assertEqual([],self.queue.incomplete_children('P'))
        packet=json.loads(evidence.read_text()); actual=json.loads(Path(packet['artifact']).read_text())
        actual.update(verdict='fail',findings=[dict(blocking=True,problem='defect',evidence='source',impact='failure')])
        Path(packet['artifact']).write_text(json.dumps(actual))
        self.assertEqual(['C'],self.queue.incomplete_children('P'))

    def test_nonexistent_commit_or_false_source_fingerprint_rejected(self):
        for field,value in [('root','/not/a/repository'),('head','a'*40),('fingerprint','b'*64)]:
            with self.subTest(field=field):
                attempt,evidence=self.prepare_case();packet=json.loads(evidence.read_text())
                packet['source'][field]=value
                parent=self.queue.get('P');origin=self.queue.attempt(parent['attempt'])
                origin['submission']['source']=packet['source'];self.queue._save_attempt(origin)
                actual=json.loads(Path(packet['artifact']).read_text());actual['source']=packet['source']
                Path(packet['artifact']).write_text(json.dumps(actual));packet['artifact_digest']=file_digest(packet['artifact'])
                evidence.write_text(json.dumps(packet))
                with self.assertRaises(ValueError):self.apply(attempt,evidence)
                self.assertEqual(['C'],self.queue.incomplete_children('P'))

    def test_historical_source_proof_accepts_different_current_head_and_dirty_worktree(self):
        attempt,evidence=self.prepare(production_verifier=True)
        (self.root/'source.c').write_text('later checkout\n')
        subprocess.check_call(['git','-C',str(self.root),'add','source.c'])
        subprocess.check_call(['git','-C',str(self.root),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','later'])
        (self.root/'source.c').write_text('dirty current file\n')
        self.apply(attempt,evidence)
        self.assertEqual([],self.queue.incomplete_children('P'))
