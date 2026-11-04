import copy
import hashlib
import json
from pathlib import Path
from teamkit.runtime import RuntimeError as NativeFailure
from unittest.mock import patch
import test_cli
import test_pool_control as fixtures
from teamkit import pool_control as control, context_handoff as context

class AdoptionTests(fixtures.PoolControlTests):
    setUp=fixtures.PoolControlTests.setUp
    prepare=fixtures.PoolControlTests.prepare
    def dispatched(self,provider='claude'):
        return fixtures.PoolControlTests.dispatched(self,provider)
    legacy_lineage=fixtures.PoolControlTests.legacy_lineage

    def packet(self,pending=False):
        actor,job,lineage,path,original=self.legacy_lineage()
        if pending:
            job.update(phase='pending',receiver=None,work_id='WORK-EXAMPLE')
            context.write(path,job)
        self.fake.rows.pop(next(x for x in list(self.fake.rows) if x!= 'trestored' and self.fake.rows[x].get('state')=='finished'))
        self.fake.rows['trestored']['status']['state']='done'
        report=self.root/'report.md';report.write_text('Completed bounded historical investigation.')
        author={'type':'assistant','session_id':'11111111-2222-4333-8444-555555555555','timestamp':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
                'message':{'role':'assistant','model':'claude-fixture','stop_reason':'end_turn','content':[{'type':'text','text':str(report)}]}}
        transcript=Path(self.value['profiles']['claude']['team']['home'])/'projects'/'example'/('11111111-2222-4333-8444-555555555555.jsonl')
        transcript.parent.mkdir(parents=True,exist_ok=True)
        self.fake.rows['trestored']['foreground']['argv'][-2:]=['--resume',author['session_id']]
        raw=json.dumps(author);transcript.write_text(raw+'\n')
        packet={'request_id':job['id'],'request_record_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'request_digest':hashlib.sha256(job['message'].encode()).hexdigest(),
            'work_id':job.get('work_id',job['id']),'owner':actor['owner'],'attempt':actor['attempt_count'],
            'writer_stopped':True,'operator_reference':'User-approved historical result adoption',
            'verification':['All requested bounded review sections were checked.'],
            'completion_requirements':job.get('completion_requirements'),
            'result_artifact':str(report),'artifacts':[{'path':str(report),'sha256':hashlib.sha256(report.read_bytes()).hexdigest()}],
            'authorship':{'transcript':str(transcript),'record_sha256':hashlib.sha256(raw.encode()).hexdigest(),
                'timestamp':author['timestamp'],'session_id':author['session_id']},
            'lineage_event_seq':json.loads(lineage.read_text())['lineage_event_seq']}
        if pending:
            metadata=self.root/'result.json'
            metadata.write_text(json.dumps(dict(work_id=job['work_id'],status='completed',result_path=str(report),executor=dict(provider=actor['provider'],account=actor['account'],model=actor['model'],task=actor['id']))))
            packet['result_metadata']=str(metadata)
            packet['artifacts'].append(dict(path=str(metadata),sha256=hashlib.sha256(metadata.read_bytes()).hexdigest()))
        evidence=self.root/'adoption.json';evidence.write_text(json.dumps(packet))
        return actor,job,path,packet,evidence

    def adopt(self,actor,job,evidence,approved=True):
        def native(*args,**kwargs):
            if args[0]=='ls':return {'terminals':list(self.fake.rows.values())}
            return self.fake.call(*args,**kwargs)
        with patch('teamkit.core_runtime._call',side_effect=native):
            return control.adopt_result(self.config,self.state,job['id'],evidence,actor['owner'],actor['attempt_count'],approved)

    def test_adopts_legacy_result_without_receipt_or_input_mutation(self):
        actor,job,path,packet,evidence=self.packet()
        before_calls=len(self.fake.calls)
        result=self.adopt(actor,job,evidence)
        self.assertEqual('completed',result['phase'])
        self.assertEqual(job['receiver'],result['receiver'])
        self.assertNotIn('submission',result)
        self.assertEqual('operator',result['work_resolution']['authority'])
        self.assertFalse(any(c[0] in ('send','keys','interrupt') for c in self.fake.calls[before_calls:]))
        repeated=self.adopt(actor,job,evidence)
        self.assertEqual(result,repeated)

    def test_pending_adoption_does_not_invent_actor_receipt(self):
        actor,job,path,packet,evidence=self.packet(True)
        result=self.adopt(actor,job,evidence)
        self.assertEqual('completed',result['phase']);self.assertIsNone(result['receiver'])
        self.assertNotIn('delivery_id',result);self.assertNotIn('submission',result)

    def test_rejects_unapproved_changed_hash_scope_and_author(self):
        actor,job,path,original_packet,evidence=self.packet()
        for field,value in [('request_record_sha256','0'*64),('request_digest','0'*64),('work_id','OTHER'),('owner','OTHER')]:
            packet=copy.deepcopy(original_packet)
            packet[field]=value;evidence.write_text(json.dumps(packet))
            with self.assertRaises(ValueError):self.adopt(actor,job,evidence)
            self.assertEqual('done',json.loads(path.read_text())['phase'])
        evidence.write_text(json.dumps(original_packet))
        with self.assertRaises(ValueError):self.adopt(actor,job,evidence,False)


    def test_rejects_busy_or_changed_native_and_preserves_request(self):
        actor,job,path,packet,evidence=self.packet()
        original=path.read_bytes()
        self.fake.rows['trestored']['status']['state']='working'
        with self.assertRaises(NativeFailure):self.adopt(actor,job,evidence)
        self.assertEqual(original,path.read_bytes())
        self.fake.rows['trestored']['status']['state']='done'
        self.fake.rows['trestored']['foreground']['pid']+=1
        with self.assertRaises(NativeFailure):self.adopt(actor,job,evidence)
        self.assertEqual(original,path.read_bytes())

    def test_rejects_mutated_author_and_artifact(self):
        actor,job,path,packet,evidence=self.packet()
        original=path.read_bytes()
        Path(packet['authorship']['transcript']).write_text('{}\n')
        with self.assertRaises(ValueError):self.adopt(actor,job,evidence)
        self.assertEqual(original,path.read_bytes())
        Path(packet['result_artifact']).write_text('changed')
        with self.assertRaises(ValueError):self.adopt(actor,job,evidence)

    def test_rejects_active_lease_and_active_descendant(self):
        actor,job,path,packet,evidence=self.packet()
        self.fake.rows['tchild']={'address':'tchild','parent':'trestored','state':'live','status':'working'}
        with self.assertRaisesRegex(ValueError,'child terminal'):self.adopt(actor,job,evidence)
        self.fake.rows.pop('tchild')
        from teamkit.store import Store
        from teamkit.resources import ResourceLedger
        import time
        store=Store(self.state)
        ResourceLedger(store.db).reserve('busy-attempt','other-ticket','codex/team',[],{},
            dict(observed_at=time.time(),complete=True,terminals=[]),
            session=dict(terminal='trestored',pid=self.fake.rows['trestored']['foreground']['pid'],owner=actor['owner']))
        store.close()
        with self.assertRaisesRegex(ValueError,'lease'):self.adopt(actor,job,evidence)

    def test_cli_requires_explicit_operator_approval(self):
        actor,job,path,packet,evidence=self.packet()
        with patch('teamkit.pool_control.adopt_result',return_value={'phase':'completed'}) as adopt:
            result=self.call('pool','adopt-result','--request',job['id'],'--evidence-file',str(evidence),
                '--expected-owner',actor['owner'],'--expected-attempt','1','--operator-approved')
        self.assertEqual('completed',result['phase']);self.assertTrue(adopt.call_args.args[-1])


    def test_unrelated_draft_is_preserved_and_never_read(self):
        actor,job,path,packet,evidence=self.packet()
        self.fake.drafts['trestored']='❯ An unrelated unsent user instruction'
        before=list(self.fake.commands)
        self.adopt(actor,job,evidence)
        self.assertEqual('❯ An unrelated unsent user instruction',self.fake.drafts['trestored'])
        self.assertEqual(before,self.fake.commands)

    def test_linked_metadata_wrong_work_scope_is_rejected(self):
        actor,job,path,packet,evidence=self.packet(True)
        metadata=Path(packet['result_metadata']);value=json.loads(metadata.read_text());value['work_id']='OTHER'
        metadata.write_text(json.dumps(value))
        next(x for x in packet['artifacts'] if x['path']==str(metadata))['sha256']=hashlib.sha256(metadata.read_bytes()).hexdigest()
        evidence.write_text(json.dumps(packet))
        with self.assertRaisesRegex(ValueError,'scope'):self.adopt(actor,job,evidence)
        self.assertEqual('pending',json.loads(path.read_text())['phase'])

    def test_claimed_authorship_timestamp_and_stop_confirmation_are_required(self):
        actor,job,path,packet,evidence=self.packet()
        original=copy.deepcopy(packet)
        packet['writer_stopped']=False;evidence.write_text(json.dumps(packet))
        with self.assertRaises(ValueError):self.adopt(actor,job,evidence)
        packet=copy.deepcopy(original);packet['authorship']['timestamp']='2001-01-01T00:00:00Z';evidence.write_text(json.dumps(packet))
        with self.assertRaisesRegex(ValueError,'chronology'):self.adopt(actor,job,evidence)


    def test_different_directory_same_basename_cannot_reuse_author(self):
        actor,job,path,packet,evidence=self.packet()
        other=self.root/'unrelated'/Path(packet['result_artifact']).name
        other.parent.mkdir();other.write_text('An unrelated result with the same filename.')
        packet['result_artifact']=str(other)
        packet['artifacts']=[dict(path=str(other),sha256=hashlib.sha256(other.read_bytes()).hexdigest())]
        evidence.write_text(json.dumps(packet))
        original=path.read_bytes()
        with self.assertRaisesRegex(ValueError,'result reference'):self.adopt(actor,job,evidence)
        self.assertEqual(original,path.read_bytes())

    def test_explicit_directory_with_following_filename_binds_exact_path(self):
        actor,job,path,packet,evidence=self.packet()
        transcript=Path(packet['authorship']['transcript'])
        row=json.loads(transcript.read_text())
        artifact=Path(packet['result_artifact'])
        row['message']['content']=[dict(type='text',text='Files are in '+str(artifact.parent)+':\n- '+artifact.name+'\n- evidence')]
        raw=json.dumps(row);transcript.write_text(raw+'\n')
        packet['authorship']['record_sha256']=hashlib.sha256(raw.encode()).hexdigest()
        evidence.write_text(json.dumps(packet))
        self.assertEqual('completed',self.adopt(actor,job,evidence)['phase'])


    def test_reservation_inserted_after_authorship_blocks_completion(self):
        actor,job,path,packet,evidence=self.packet()
        original=path.read_bytes()
        from teamkit.result_adoption import _author
        from teamkit.resources import ResourceLedger
        from teamkit.store import Store
        import time
        def race(*args):
            proof=_author(*args)
            store=Store(self.state)
            ResourceLedger(store.db).reserve('racing-attempt','other-ticket','claude/team',[],{},
                dict(observed_at=time.time(),complete=True,terminals=[]),
                session=dict(terminal='trestored',pid=self.fake.rows['trestored']['foreground']['pid'],owner=actor['owner']))
            store.close()
            return proof
        with patch('teamkit.result_adoption._author',side_effect=race):
            with self.assertRaisesRegex(ValueError,'lease'):self.adopt(actor,job,evidence)
        self.assertEqual(original,path.read_bytes())


    def test_reservation_cannot_enter_while_request_completion_is_committed(self):
        actor,job,path,packet,evidence=self.packet()
        import sqlite3
        import threading
        import time
        from teamkit.store import Store
        from teamkit.resources import ResourceLedger
        store=Store(self.state);ResourceLedger(store.db);store.close()
        outcome=[]
        original_write=control.write_request
        def reserve():
            db=sqlite3.connect(self.state,timeout=0.05,isolation_level=None)
            try:
                ResourceLedger(db).reserve('concurrent-attempt','other-ticket','claude/team',[],{},
                    dict(observed_at=time.time(),complete=True,terminals=[]),
                    session=dict(terminal='trestored',pid=self.fake.rows['trestored']['foreground']['pid'],owner=actor['owner']))
                outcome.append('reserved')
            except sqlite3.OperationalError as error:
                outcome.append(str(error))
            finally:db.close()
        def commit(path,value):
            thread=threading.Thread(target=reserve);thread.start();thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(['database is locked'],outcome)
            original_write(path,value)
        with patch('teamkit.pool_control.write_request',side_effect=commit):
            self.assertEqual('completed',self.adopt(actor,job,evidence)['phase'])


    def test_linked_workflow_cannot_bypass_official_completion_gate(self):
        actor,job,path,packet,evidence=self.packet()
        job['workflow_ref']={'work':'WORK-EXAMPLE','revision':1,'digest':'0'*64}
        context.write(path,job)
        packet['request_record_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        evidence.write_text(json.dumps(packet))
        original=path.read_bytes()
        with self.assertRaisesRegex(ValueError,'workflow'):self.adopt(actor,job,evidence)
        self.assertEqual(original,path.read_bytes())


    def snapshot_then_consumer_refresh(self):
        actor,job,path,packet,evidence=self.packet(True)
        snapshot=self.root/'original-request.json';snapshot.write_bytes(path.read_bytes())
        blocker=dict(job,id='a'*32,phase='done',at=job['at']-1,receiver=dict(owner=actor['owner'],attempt=1,terminal='told'))
        context.write(control.folder(self.state)/('a'*32+'.json'),blocker)
        control.advance(self.config,self.state,job['id'])
        self.assertNotEqual(snapshot.read_bytes(),path.read_bytes())
        self.assertEqual('pending',json.loads(path.read_text())['phase'])
        return actor,job,path,packet,evidence,snapshot

    def test_default_raw_hash_stays_strict_after_consumer_refresh(self):
        actor,job,path,packet,evidence,snapshot=self.snapshot_then_consumer_refresh()
        original=path.read_bytes()
        with self.assertRaises(ValueError):self.adopt(actor,job,evidence)
        self.assertEqual(original,path.read_bytes())

    def test_pinned_snapshot_accepts_only_consumer_diagnostics_and_preserves_both(self):
        actor,job,path,packet,evidence,snapshot=self.snapshot_then_consumer_refresh()
        current=path.read_bytes()
        packet['request_record_snapshot']=str(snapshot);evidence.write_text(json.dumps(packet))
        result=self.adopt(actor,job,evidence)
        audit=result['operator_result_adoption']
        self.assertEqual('completed',result['phase'])
        self.assertEqual(snapshot.read_bytes(),Path(audit['request_record_snapshot_file']).read_bytes())
        self.assertEqual(current,Path(audit['original_request_file']).read_bytes())
        self.assertEqual({'reason','updated_at'},set(audit['diagnostic_refresh_fields']))

    def test_snapshot_does_not_ignore_unknown_or_immutable_fields(self):
        actor,job,path,packet,evidence,snapshot=self.snapshot_then_consumer_refresh()
        packet['request_record_snapshot']=str(snapshot);evidence.write_text(json.dumps(packet))
        refreshed=json.loads(path.read_text())
        for key,value in [('new_unknown_field',True),('error_type','changed'),('message','different request'),('task','OTHER'),('work_id','OTHER'),('phase','sending')]:
            current=dict(refreshed,**{key:value});context.write(path,current);before=path.read_bytes()
            with self.assertRaises(ValueError):self.adopt(actor,job,evidence)
            self.assertEqual(before,path.read_bytes())

    def test_snapshot_raw_bytes_must_match_pinned_packet_hash(self):
        actor,job,path,packet,evidence,snapshot=self.snapshot_then_consumer_refresh()
        packet['request_record_snapshot']=str(snapshot);evidence.write_text(json.dumps(packet))
        snapshot.write_text(snapshot.read_text()+' ')
        with self.assertRaises(ValueError):self.adopt(actor,job,evidence)


    def test_snapshot_rejects_ambiguous_duplicate_fields(self):
        actor,job,path,packet,evidence,snapshot=self.snapshot_then_consumer_refresh()
        raw=snapshot.read_text();raw=raw.replace('"phase": "pending"','"phase": "changed", "phase": "pending"')
        snapshot.write_text(raw);packet['request_record_sha256']=hashlib.sha256(snapshot.read_bytes()).hexdigest()
        packet['request_record_snapshot']=str(snapshot);evidence.write_text(json.dumps(packet))
        with self.assertRaisesRegex(ValueError,'duplicate'):self.adopt(actor,job,evidence)


def load_tests(loader, tests, pattern):
    import unittest
    return unittest.TestSuite(AdoptionTests(name) for name in AdoptionTests.__dict__ if name.startswith('test_'))
