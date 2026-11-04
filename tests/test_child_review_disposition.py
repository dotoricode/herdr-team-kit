import json
from pathlib import Path
import unittest
from teamkit.tickets import TicketStore, file_digest, digest
from tests import test_tickets as fixtures


class ChildReviewDispositionTest(unittest.TestCase):
    setUp = fixtures.TicketsTest.setUp
    spec = fixtures.TicketsTest.spec
    executor = fixtures.TicketsTest.executor
    observation = fixtures.TicketsTest.observation
    def prepare(self):
        self.queue.enqueue(self.spec('P',kind='review',review_required=False,verification_commands={'source':['true']}), 'p', user_approved=True)
        ex=self.executor()
        parent=self.queue.reserve('P',ex,{},self.observation())
        self.queue.receipt('P',parent['id'],ex['owner'],ex['terminal'],dict(ex,status='idle'))
        artifact=self.root/'parent.md'; artifact.write_text('Original authored review')
        source={'root':str(self.root),'head':'a'*40,'fingerprint':'b'*64}
        self.queue.submit('P',parent['id'],ex['owner'],dict(artifact=str(artifact),writer_stopped=True,source=source))
        self.queue.record_stop('P',parent['id'],dict(ex,status='done'))
        child=self.spec('C',kind='review',role='reviewer',parent_id='P',parent_attempt=parent['id'],relation='required',
                        exclude_owners=[ex['owner']],verification_commands={'source':['true']})
        self.queue.enqueue(child,'c',user_approved=True)
        plan=dict(self.executor(owner='external-plan'),new=True,terminal=None,pid=None,runtime={})
        attempt=self.queue.reserve('C',plan,{},self.observation())
        self.queue.delivery(attempt['id'],False,dict(error='ValueError',reason='product execution requires linked approved work'))
        report=self.root/'independent.md'; report.write_text('Independently reviewed exact approved source')
        authority=self.root/'operator-approval.txt'; authority.write_text('Approve replacing unsent child with independently performed review evidence')
        packet=dict(child='C',attempt=attempt['id'],parent='P',parent_attempt=parent['id'],
            contract_digest=digest({k:self.queue.get('C').get(k) for k in ('goal','scope','resources','checks','verification_commands','approval','execution_constraint','exclude_owners','parent_id','parent_attempt','relation')}),
            parent_artifact_digest=file_digest(artifact),source=source,artifact=str(report),artifact_digest=file_digest(report),
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
