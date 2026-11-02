import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from teamkit import operations
from teamkit.resources import ResourceLedger
from teamkit.tickets import TicketStore, file_digest


class SubmissionContractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state.sqlite'
        self.approval = self.root / 'approval.txt'
        self.approval.write_text('Approved isolated submission contract.')
        self.queue = TicketStore(self.state)
        self.addCleanup(self.queue.close)
        def forbidden(*args, **kwargs):
            raise AssertionError('Native/model/process execution is forbidden')
        for target in ('subprocess.Popen', 'subprocess.run', 'subprocess.check_output',
                       'teamkit.core_runtime._call', 'teamkit.core_runtime.CoreRuntime.launch',
                       'teamkit.core_runtime.CoreRuntime._native', 'teamkit.core_runtime.CoreRuntime.accept',
                       'teamkit.providers.identity', 'teamkit.admission.native_get', 'teamkit.admission.observe_native'):
            guard = patch(target, side_effect=forbidden)
            guard.start(); self.addCleanup(guard.stop)

    def spec(self, ident, kind='investigation'):
        return dict(id=ident, product='fixture-team', role='investigator', kind=kind,
            goal='Inspect only the isolated fixture', completion=['bounded report'], checks=['report'],
            verification_commands={'report':['fixture-verifier']}, profiles=['codex/personal'],
            scope=dict(paths=[str(self.root)], actions=['read','write'], data=['fixture']),
            resources=[dict(kind='path', name=str(self.root / ident), mode='write')],
            approval=dict(state='approved', reference=str(self.approval)),
            budget=dict(tickets=5, attempts=8, tokens=10000), estimated_tokens=100)

    def actor(self, ident='BAD'):
        return dict(provider='codex', account='personal', owner='owner-'+ident,
                    terminal='t'+ident.lower(), pid=42 if ident=='BAD' else 43)

    def native(self, actor):
        return dict(actor, status='idle')

    def begin(self, ident='BAD', kind='investigation', receipt=True):
        self.queue.enqueue(self.spec(ident,kind), ident, user_approved=True)
        actor = self.actor(ident)
        attempt = self.queue.reserve(ident, actor, {'global_limit':3},
                                     dict(observed_at=time.time(),complete=True,terminals=[]))
        if receipt:
            self.queue.receipt(ident,attempt['id'],actor['owner'],actor['terminal'],self.native(actor))
        artifact = self.root / (ident+'-report.txt')
        artifact.write_text('Bounded fixture report.')
        return actor,attempt,dict(artifact=str(artifact),writer_stopped=True,usage_tokens=5)

    def submit(self, actor, attempt, result):
        return self.queue.submit(attempt['ticket'],attempt['id'],actor['owner'],result)

    def inject_submission(self, attempt, packet):
        with self.queue.transaction():
            saved = self.queue.attempt(attempt['id'])
            saved['submission'] = packet
            self.queue._save_attempt(saved)
            item = self.queue.get(attempt['ticket'])
            item['state'] = 'awaiting_verification'
            self.queue._save(item,'fixture_old_submission')

    def test_submit_requires_explicit_true_writer_stop_without_mutation(self):
        actor,attempt,packet = self.begin()
        before = self.queue.get('BAD')
        for value in ('false','true',False,1,None):
            with self.subTest(writer_stopped=value):
                with self.assertRaisesRegex(ValueError,'writer_stopped'):
                    self.submit(actor,attempt,{**packet,'writer_stopped':value})
                self.assertEqual(before,self.queue.get('BAD'))
                self.assertNotIn('submission',self.queue.attempt(attempt['id']))

    def test_old_truthy_string_cannot_release_resources_or_finish(self):
        actor,attempt,packet = self.begin()
        proof = dict(packet,artifact_digest=file_digest(packet['artifact']))
        self.inject_submission(attempt,{**proof,'writer_stopped':'false'})
        with self.assertRaisesRegex(ValueError,'writer stop'):
            self.queue.record_stop('BAD',attempt['id'],self.native(actor))
        row = ResourceLedger._rows(self.queue.db)[0]
        self.assertTrue(row['execution_reserved']); self.assertTrue(row['writes_held'])
        self.inject_submission(attempt,proof)
        self.queue.record_stop('BAD',attempt['id'],self.native(actor))
        self.inject_submission(attempt,{**proof,'writer_stopped':'false'})
        with self.assertRaisesRegex(ValueError,'completion gate'):
            self.queue.finish('BAD',attempt['id'],dict(artifact_digest=proof['artifact_digest'],checks={'report':True}))
        self.assertEqual('awaiting_verification',self.queue.get('BAD')['state'])

    def test_submit_rejects_malformed_basic_fields_as_value_error(self):
        actor,attempt,packet = self.begin()
        bad = [None,'report',[],[['artifact',packet['artifact']]],
               {**packet,'artifact':{}},{**packet,'artifact':None},{**packet,'artifact':'relative.txt'},
               {**packet,'usage_tokens':'5'},{**packet,'usage_tokens':True},{**packet,'usage_tokens':-1},
               {**packet,'checks':[]},{**packet,'checks':{'report':'true'}}]
        for value in bad:
            with self.subTest(result=value):
                with self.assertRaises(ValueError): self.submit(actor,attempt,value)
                self.assertNotIn('submission',self.queue.attempt(attempt['id']))
        accepted = self.submit(actor,attempt,{**packet,'checks':{'report':True}})
        self.assertEqual('awaiting_verification',accepted['state'])

    def test_code_and_integration_require_typed_pinned_source(self):
        source = dict(root=str(self.root),head='a'*40,fingerprint='b'*64)
        bad = [None,'HEAD',[],{}, {**source,'root':1},{**source,'root':'relative'},
               {**source,'head':1},{**source,'head':'HEAD'},
               {**source,'fingerprint':[]},{**source,'fingerprint':True}]
        for ident,kind in [('CODE','code'),('INTEGRATION','integration')]:
            actor,attempt,packet = self.begin(ident,kind)
            with self.assertRaisesRegex(ValueError,'source'): self.submit(actor,attempt,packet)
            for value in bad:
                with self.subTest(kind=kind,source=value):
                    with self.assertRaisesRegex(ValueError,'source'):
                        self.submit(actor,attempt,{**packet,'source':value})
                    self.assertNotIn('submission',self.queue.attempt(attempt['id']))
            self.assertEqual('awaiting_verification',self.submit(actor,attempt,{**packet,'source':source})['state'])

    def test_actor_receipt_rechecks_approval_even_when_repeated(self):
        actor,attempt,packet = self.begin(receipt=False)
        original = self.approval.read_text()
        self.approval.write_text('Approval withdrawn before actor receipt.')
        with self.assertRaisesRegex(ValueError,'approval'):
            self.queue.receipt('BAD',attempt['id'],actor['owner'],actor['terminal'],self.native(actor))
        self.assertEqual('dispatching',self.queue.get('BAD')['state'])
        self.assertNotIn('actor_receipt',self.queue.attempt(attempt['id']))
        self.approval.write_text(original)
        self.queue.receipt('BAD',attempt['id'],actor['owner'],actor['terminal'],self.native(actor))
        self.approval.write_text('Approval withdrawn before duplicate receipt.')
        with self.assertRaisesRegex(ValueError,'approval'):
            self.queue.receipt('BAD',attempt['id'],actor['owner'],actor['terminal'],self.native(actor))

    def test_delivery_failure_exposes_its_reason_without_clearing_uncertainty(self):
        actor,attempt,packet = self.begin(receipt=False)
        detail = {'error':'ValueError','reason':'approval changed after reservation; no input sent'}
        self.queue.delivery(attempt['id'],False,detail)
        item = self.queue.get('BAD')
        self.assertEqual('uncertain',item['state'])
        self.assertIn(detail['reason'],item['reason'])
        self.assertIn('never resend',item['next_action'])
        self.assertEqual(detail,self.queue.attempt(attempt['id'])['delivery'])
        self.assertEqual(attempt['id'],item['attempt'])

    def test_stop_keeps_a_read_lease_until_finish_and_allows_independent_reader(self):
        actor,attempt,packet = self.begin()
        self.submit(actor,attempt,packet)
        self.queue.record_stop('BAD',attempt['id'],self.native(actor))
        writer_spec = self.spec('WRITER')
        writer_spec['resources'] = self.spec('BAD')['resources']
        self.queue.enqueue(writer_spec,'writer',user_approved=True)
        observer = dict(observed_at=time.time(),complete=True,terminals=[])
        with self.assertRaisesRegex(ValueError,'conflicting resource'):
            self.queue.reserve('WRITER',self.actor('WRITER'),{},observer)
        reader_spec = self.spec('REVIEWER','review')
        reader_spec.update(role='reviewer',resources=[dict(writer_spec['resources'][0],mode='read')])
        self.queue.enqueue(reader_spec,'reader',user_approved=True)
        reader = self.actor('REVIEWER')
        review_attempt = self.queue.reserve('REVIEWER',reader,{},observer)
        self.queue.receipt('REVIEWER',review_attempt['id'],reader['owner'],reader['terminal'],self.native(reader))
        report = self.root/'review.txt'; report.write_text('Independent fixture review.')
        self.queue.submit('REVIEWER',review_attempt['id'],reader['owner'],dict(artifact=str(report),writer_stopped=True))
        self.queue.record_stop('REVIEWER',review_attempt['id'],self.native(reader))
        self.queue.finish('REVIEWER',review_attempt['id'],dict(artifact_digest=file_digest(report),checks={'report':True}))
        self.queue.finish('BAD',attempt['id'],dict(artifact_digest=file_digest(packet['artifact']),checks={'report':True}))
        self.assertEqual('WRITER',self.queue.reserve('WRITER',self.actor('WRITER'),{},observer)['ticket'])

    def test_duplicate_stop_is_idempotent_and_does_not_drop_the_read_lease(self):
        actor,attempt,packet = self.begin()
        self.submit(actor,attempt,packet)
        self.queue.record_stop('BAD',attempt['id'],self.native(actor))
        before = ResourceLedger._rows(self.queue.db)
        self.assertTrue(before[0]['writes_held'])
        self.assertFalse(before[0]['execution_reserved'])
        self.assertEqual(['read'],[x['mode'] for x in before[0]['resources']])
        events = self.queue.db.execute('SELECT count(*) FROM operation_events').fetchone()[0]
        stopped = self.queue.attempt(attempt['id'])['stop_observation']
        self.queue.record_stop('BAD',attempt['id'],self.native(actor))
        self.assertEqual(before,ResourceLedger._rows(self.queue.db))
        self.assertEqual(events,self.queue.db.execute('SELECT count(*) FROM operation_events').fetchone()[0])
        self.assertEqual(stopped,self.queue.attempt(attempt['id'])['stop_observation'])

    def test_repair_wait_releases_the_verification_read_lease(self):
        actor,attempt,packet = self.begin()
        self.submit(actor,attempt,packet)
        self.queue.record_stop('BAD',attempt['id'],self.native(actor))
        child = self.spec('REPAIR')
        child.update(parent_id='BAD',relation='defect',resources=self.spec('BAD')['resources'])
        self.queue.enqueue(child,'repair')
        self.assertTrue(ResourceLedger._rows(self.queue.db)[0]['writes_held'])
        self.queue.wait_children('BAD',attempt['id'],actor['owner'],True,True)
        self.assertFalse(ResourceLedger._rows(self.queue.db)[0]['writes_held'])
        self.assertEqual('REPAIR',self.queue.reserve('REPAIR',self.actor('REPAIR'),{},
            dict(observed_at=time.time(),complete=True,terminals=[]))['ticket'])

    def test_old_invalid_submission_does_not_stop_unrelated_ticket_processing(self):
        actor,attempt,packet = self.begin()
        self.inject_submission(attempt,{**packet,'artifact_digest':file_digest(packet['artifact']),
                                        'writer_stopped':'false'})
        self.queue.enqueue(self.spec('GOOD'),'good',user_approved=True)
        config = self.root / 'config.json'
        profiles = {p:{a:dict(home=str(self.root/(p+'-'+a)),model='fixture',
                              identity={'email':'fixture@example.invalid'}) for a in ('team','personal')}
                    for p in ('codex','claude')}
        config.write_text(json.dumps(dict(version=1,profiles=profiles,
            teams={'fixture-team':dict(root=str(self.root),roles={'leader':dict(provider='codex',model='fixture')})},
            operations={'enabled':True,'resource_policy':{'global_limit':3}})))
        outer = self
        class LocalRuntime:
            sent = []
            def executors(self,ticket=None): return [outer.actor('GOOD')]
            def admit(self,ticket,candidate): return candidate
            def observation(self): return dict(observed_at=time.time(),complete=True,terminals=[])
            def native(self,executor,require_idle=True): return outer.native(executor)
            def send(self,executor,text):
                item = outer.queue.get('GOOD'); current = outer.queue.attempt(item['attempt'])
                self.sent.append(item['id'])
                outer.queue.receipt('GOOD',current['id'],executor['owner'],executor['terminal'],self.native(executor))
                report = outer.root/'good.txt'; report.write_text('Bounded good report.')
                outer.queue.submit('GOOD',current['id'],executor['owner'],dict(artifact=str(report),writer_stopped=True))
                return {'accepted':True}
        def verifier(argv,**kwargs):
            if argv != ['fixture-verifier']: raise AssertionError('Unexpected process')
            return type('Result',(),{'returncode':0,'stdout':'','stderr':''})()
        runtime = LocalRuntime()
        with patch('subprocess.run',side_effect=verifier):
            operations.tick(config,self.state,runtime)
            operations.tick(config,self.state,runtime)
        self.assertEqual(['GOOD'],runtime.sent)
        self.assertEqual('completed',self.queue.get('GOOD')['state'])
        self.assertEqual('awaiting_verification',self.queue.get('BAD')['state'])
        bad = next(x for x in ResourceLedger._rows(self.queue.db) if x['attempt']==attempt['id'])
        self.assertTrue(bad['writes_held'])
