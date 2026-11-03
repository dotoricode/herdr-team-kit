import json
import copy
import hashlib
import os
from pathlib import Path
import subprocess
import time
from unittest.mock import patch

from test_cli import Fixture, StoreContext
from test_session_pool import FakeNative
from teamkit import session_pool as pool, pool_control as control, context_handoff as context
from teamkit.config import load,binding
from teamkit.store import Store


class PoolControlTests(Fixture):
    def setUp(self):
        super().setUp()
        self.value['runtime_backend']='core'
        self.value['profiles']['claude']={a:{'home':str(self.root/('claude-'+a)),
            'identity':{'email':a+'@example.invalid'},'model':'claude-fixture','effort':'medium'} for a in ('team','personal')}
        self.value['teams']['alpha']['roles']={'leader':{'provider':'codex','model':'fixture','effort':'medium'}}
        self.value['session_pool']={'enabled':True,'bindings':str(self.root/'bindings.json'),'tasks':{'alpha':'L1'}}
        self.value['context_handoff']={'enabled':True,'threshold':90,'interval':10}
        self.value['context_bindings']=str(self.root/'bindings.json')
        self.value['private_plan_root']=str(self.root)
        self.config.write_text(json.dumps(self.value))
        self.call('create','L1','--team','alpha','--role','leader','--root',str(self.repo),'--goal-file',str(self.root/'goal.txt'))
        self.fake=FakeNative();original=subprocess.run
        def bounded_run(args,**kwargs):
            return self.fake.run(args,**kwargs) if args[:2]==['hc','read'] or Path(args[0]).name=='ensure-atlassian-mcp' else original(args,**kwargs)
        for name,value in [('teamkit.core_runtime._call',self.fake.call),('teamkit.session_pool._call',self.fake.call),('teamkit.session_pool.subprocess.run',bounded_run)]:
            p=patch(name,side_effect=value);p.start();self.addCleanup(p.stop)
        env=patch.dict(os.environ,{'HC_TERMINAL_ID':'tcontroller'});env.start();self.addCleanup(env.stop)
        self.identity=patch('teamkit.providers.identity');self.identity_mock=self.identity.start();self.addCleanup(self.identity.stop)

    def prepare(self,provider='codex'):
        if provider=='claude':
            self.value['teams']['alpha']['roles']['leader']={'provider':'claude','model':'claude-fixture','effort':'medium'}
            self.config.write_text(json.dumps(self.value))
        pool.prepare(self.config,self.state)
        now=time.time();store=Store(self.state)
        store.quota(provider+'/team',{'binding':binding(self.value['profiles'][provider]['team']),
            'source':'claude-statusline-response' if provider=='claude' else 'codex-native-quota','observed_at':now,
            'five_hour':{'used':1,'resets_at':now+3600},'weekly':{'used':1,'resets_at':now+86400}});store.close()

    def dispatched(self,provider='codex'):
        self.prepare(provider)
        def receipt(runtime,store,config_path,task,owner,text):
            self.assertEqual(runtime.task['owner'],owner)
            self.assertEqual(runtime.target['pool_owner'],owner)
            self.assertIn('receipt L1 --owner '+owner,text)
            self.assertEqual(task['runtime']['receipt_protocol'],'host-fresh-v1')
            store.receipt('L1',owner);store.park('L1',owner)
        def claude_prompt(actor,name,text):
            store=Store(self.state);task=store.get('L1');store.close()
            with patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':self.value['profiles']['claude']['team']['home']}):
                self.call('receipt','L1','--owner',task['owner'])
                self.call('park','L1','--owner',task['owner'])
        with patch('teamkit.context_handoff.ensure'),patch('teamkit.receipt.prompt',side_effect=receipt),patch('teamkit.core_runtime.CoreRuntime.prompt',new=claude_prompt):
            result=self.call('dispatch','L1')
        self.assertEqual(result['state'],'ready');self.assertEqual(result['runtime']['identity_checked'],True)
        self.assertEqual(pool.status(load(self.config),self.state)['counts'],{'leased':1,'prepared':3})
        self.assertEqual(sum(c[0]=='run' for c in self.fake.calls),4)
        self.identity_mock.assert_called()
        return result

    def test_pin_does_not_replace_explicit_model_policy(self):
        from teamkit.__main__ import select_execution
        self.prepare()
        task = self.call('show', 'L1')
        task['user_decision'] = {'provider': 'claude', 'model': 'approved-special', 'effort': 'high', 'failure_count': 0}
        task['execution_constraint'] = {'provider': 'codex', 'account': 'team'}
        store = Store(self.state)
        try:
            selected = select_execution(load(self.config), store, task, time.time())
        finally:
            store.close()
        self.assertEqual(selected['action'], 'wait')

    def test_pin_waits_while_native_is_working_despite_ledger_ready(self):
        self.dispatched()
        message = self.root / 'working-pin.txt'
        message.write_text('Do not interrupt the current work.')
        request = control.enqueue(self.config, self.state, 'alpha', message, 'claude', 'team')
        with patch('teamkit.core_runtime.CoreRuntime._native', return_value={'status': {'state': 'working'}}):
            control.advance(self.config, self.state, request['id'])
        self.assertNotIn('execution_constraint', self.call('show', 'L1'))
        self.assertEqual(control.requests(self.state)[0]['phase'], 'pending')

    def test_pin_identity_failure_is_uncertain_not_retryable(self):
        self.dispatched()
        message = self.root / 'identity-pin.txt'
        message.write_text('Preserve mismatched native identity.')
        request = control.enqueue(self.config, self.state, 'alpha', message, 'claude', 'team')
        with patch('teamkit.core_runtime.CoreRuntime._native', side_effect=RuntimeError('owner changed')):
            control.advance(self.config, self.state, request['id'])
        self.assertEqual(control.requests(self.state)[0]['phase'], 'uncertain')

    def test_unpinned_busy_native_is_pending_without_sending(self):
        self.dispatched()
        message = self.root / 'busy.txt'
        message.write_text('Wait for current work.')
        request = control.enqueue(self.config, self.state, 'alpha', message)
        with patch.object(context, 'capacity_available', return_value=True), patch('teamkit.core_runtime.CoreRuntime._native', return_value={'status': {'state': 'working'}}), patch.object(control, 'official') as sent:
            control.advance(self.config, self.state, request['id'])
        sent.assert_not_called()
        self.assertEqual(control.requests(self.state)[0]['phase'], 'pending')

    def test_new_explicit_request_can_replace_unsent_cancelled_policy_rotation(self):
        self.dispatched()
        config = load(self.config)
        target = next(t for t in context.targets(config,self.state) if t['id']=='L1')
        with patch.object(context,'observe',return_value=None):
            old = context.request(config,self.state,target,None,policy_rotation=True)
        context.set_phase(self.state,old,'cancelled','User changed requested account before sending.')
        message = self.root/'retry.txt'
        message.write_text('New explicitly approved request.')
        request = control.enqueue(self.config,self.state,'alpha',message,'codex','team')
        with patch.object(context,'observe',return_value=None):
            new = context.request(config,self.state,target,None,policy_rotation=True,pool_request=request['id'])
        self.assertIsNotNone(new)
        self.assertNotEqual(new['id'],old['id'])
        self.assertEqual(context.read(context.job_path(self.state,old['id']))['phase'],'cancelled')
        context.set_phase(self.state,new,'uncertain','Never resend uncertain rotation.')
        with patch.object(context,'observe',return_value=None):
            self.assertIsNone(context.request(config,self.state,target,None,policy_rotation=True,pool_request=request['id']))

    def test_distinct_work_ids_queue_fifo_and_received_head_blocks_next(self):
        result = self.dispatched()
        message = self.root / 'queue.txt'
        message.write_text('Inspect the approved SDK.')
        first = control.enqueue(self.config, self.state, 'alpha', message, work_id='SDK-ONE')
        second = control.enqueue(self.config, self.state, 'alpha', message, work_id='SDK-TWO')
        self.assertEqual(control.enqueue(self.config, self.state, 'alpha', message, work_id='SDK-ONE')['id'], first['id'])
        with patch.object(context, 'capacity_available', return_value=True), patch.object(context, 'clean_prompt', return_value=True), patch.object(control, 'official', return_value=result) as sent:
            control.advance(self.config, self.state, second['id'])
            sent.assert_not_called()
            control.advance(self.config, self.state, first['id'])
            control.advance(self.config, self.state, second['id'])
            self.assertEqual(sent.call_count, 1)
        self.assertEqual(context.read(control.folder(self.state)/(first['id']+'.json'))['phase'], 'received')
        self.assertEqual(context.read(control.folder(self.state)/(second['id']+'.json'))['phase'], 'pending')

    def test_queue_records_receiver_before_accept_only_transport(self):
        actor=self.dispatched();message=self.root/'long.txt';message.write_text('Long bounded work.')
        job=control.enqueue(self.config,self.state,'alpha',message,work_id='LONG')
        def transport(*args):
            saved=context.read(control.folder(self.state)/(job['id']+'.json'))
            self.assertEqual(saved['phase'],'sending')
            self.assertEqual(saved['receiver']['owner'],actor['owner'])
            self.assertEqual(saved['receiver']['attempt'],actor['attempt_count'])
            self.assertEqual(saved['receiver']['native_pid'],actor['runtime']['native_pid'])
            self.assertIn('--accept-only',args)
            self.assertEqual(args[args.index('--delivery-id')+1],saved['delivery_id'])
            # The accepted input keeps running beyond transport acknowledgement.
            with StoreContext(self.state) as store:store.wake('L1',actor['owner'])
            return {**actor,'state':'running','delivery':{'accepted':True,'delivery_id':saved['delivery_id'],
                'terminal':actor['runtime']['terminal'],'native_pid':actor['runtime']['native_pid']}}
        with patch.object(context,'capacity_available',return_value=True),patch.object(control,'official',side_effect=transport) as sent:
            control.advance(self.config,self.state,job['id'])
            control.advance(self.config,self.state,job['id'])
        self.assertEqual(sent.call_count,1)
        self.assertEqual(control.requests(self.state)[0]['phase'],'received')
        self.assertEqual(self.call('show','L1')['state'],'running')

    def test_uncertain_transport_preserves_intended_receiver_and_delivery_id(self):
        actor=self.dispatched();message=self.root/'unknown.txt';message.write_text('Do once.')
        job=control.enqueue(self.config,self.state,'alpha',message)
        with patch.object(context,'capacity_available',return_value=True),patch.object(control,'official',side_effect=TimeoutError) as sent:
            control.advance(self.config,self.state,job['id'])
            saved=control.requests(self.state)[0]
            self.assertEqual(saved['receiver']['owner'],actor['owner'])
            delivery_id=saved['delivery_id']
            control.advance(self.config,self.state,job['id'])
        self.assertEqual(sent.call_count,1)
        self.assertEqual(control.requests(self.state)[0]['delivery_id'],delivery_id)
        self.assertEqual(saved['phase'],'uncertain')

    def test_late_transport_result_cannot_replace_recorded_receiver(self):
        actor=self.dispatched();message=self.root/'late.txt';message.write_text('Preserve receiver.')
        job=control.enqueue(self.config,self.state,'alpha',message)
        with patch.object(context,'capacity_available',return_value=True),patch.object(control,'official',return_value={**actor,'owner':'different-owner'}):
            control.advance(self.config,self.state,job['id'])
        saved=control.requests(self.state)[0]
        self.assertEqual(saved['phase'],'uncertain')
        self.assertEqual(saved['receiver']['owner'],actor['owner'])

    def test_tell_accept_only_keeps_running_owner_and_records_acceptance(self):
        actor=self.dispatched();message=self.root/'accept.txt';message.write_text('Long bounded work.')
        receipt={'accepted':True,'terminal':actor['runtime']['terminal'],'native_pid':actor['runtime']['native_pid'],'turn_baseline':1}
        with patch('teamkit.core_runtime.CoreRuntime.accept',return_value=receipt) as accepted,patch('teamkit.core_runtime.CoreRuntime.prompt') as completed:
            result=self.call('tell','L1','--message-file',str(message),'--expected-owner',actor['owner'],
                             '--accept-only','--delivery-id','a'*32)
        accepted.assert_called_once();completed.assert_not_called()
        self.assertEqual(result['state'],'running')
        self.assertEqual(result['delivery']['delivery_id'],'a'*32)
        saved=self.call('show','L1')
        self.assertEqual(saved['runtime']['last_delivery']['delivery_id'],'a'*32)
        self.assertEqual(saved['runtime']['last_delivery']['owner'],actor['owner'])

    def test_accepted_delivery_retry_is_idempotent_while_work_is_running(self):
        actor=self.dispatched();message=self.root/'same.txt';message.write_text('Do only once.')
        receipt={'accepted':True,'terminal':actor['runtime']['terminal'],'native_pid':actor['runtime']['native_pid'],'turn_baseline':1}
        with patch('teamkit.core_runtime.CoreRuntime.accept',return_value=receipt) as accepted:
            first=self.call('tell','L1','--message-file',str(message),'--accept-only','--delivery-id','b'*32)
            self.fake.rows[actor['runtime']['terminal']]['status']['state']='working'
            second=self.call('tell','L1','--message-file',str(message),'--accept-only','--delivery-id','b'*32)
        self.assertEqual(accepted.call_count,1)
        self.assertEqual(first['delivery'],second['delivery'])
        self.assertEqual(second['state'],'running')

    def test_unknown_acceptance_retry_preserves_intent_without_resending(self):
        actor=self.dispatched();message=self.root/'unknown-accept.txt';message.write_text('Preserve unknown delivery.')
        with patch('teamkit.core_runtime.CoreRuntime.accept',side_effect=TimeoutError) as accepted:
            with self.assertRaises(TimeoutError):
                self.call('tell','L1','--message-file',str(message),'--accept-only','--delivery-id','c'*32)
            with self.assertRaises(ValueError):
                self.call('tell','L1','--message-file',str(message),'--accept-only','--delivery-id','c'*32)
        self.assertEqual(accepted.call_count,1)
        saved=self.call('show','L1')
        self.assertEqual(saved['runtime']['last_delivery']['phase'],'sending')
        self.assertEqual(saved['owner'],actor['owner'])

    def test_cached_acceptance_does_not_bless_replaced_native_process(self):
        actor=self.dispatched();message=self.root/'replaced.txt';message.write_text('Keep original native identity.')
        receipt={'accepted':True,'terminal':actor['runtime']['terminal'],'native_pid':actor['runtime']['native_pid'],'turn_baseline':1}
        with patch('teamkit.core_runtime.CoreRuntime.accept',return_value=receipt):
            self.call('tell','L1','--message-file',str(message),'--accept-only','--delivery-id','d'*32)
        self.fake.rows[actor['runtime']['terminal']]['foreground']['pid']+=1
        from teamkit.runtime import RuntimeError as KitError
        with self.assertRaises(KitError):
            self.call('tell','L1','--message-file',str(message),'--accept-only','--delivery-id','d'*32)

    def test_delivery_id_does_not_allow_different_message_content(self):
        actor=self.dispatched();message=self.root/'immutable.txt';message.write_text('Original scope.')
        receipt={'accepted':True,'terminal':actor['runtime']['terminal'],'native_pid':actor['runtime']['native_pid'],'turn_baseline':1}
        with patch('teamkit.core_runtime.CoreRuntime.accept',return_value=receipt) as accepted:
            self.call('tell','L1','--message-file',str(message),'--accept-only','--delivery-id','e'*32)
            message.write_text('Different scope.')
            with self.assertRaises(ValueError):
                self.call('tell','L1','--message-file',str(message),'--accept-only','--delivery-id','e'*32)
        self.assertEqual(accepted.call_count,1)

    def accepted_job(self, requirements=None):
        actor=self.dispatched();message=self.root/'submitted-work.txt';message.write_text('Write the bounded investigation report.')
        extra=None
        if requirements:
            extra=self.root/'requirements.txt';extra.write_text(requirements)
        job=control.enqueue(self.config,self.state,'alpha',message,work_id='REPORT',completion_file=extra)
        with patch.object(context,'capacity_available',return_value=True),patch.object(control,'official',return_value=actor):
            control.advance(self.config,self.state,job['id'])
        saved=control.requests(self.state)[0]
        receipt={'phase':'accepted','accepted':True,'delivery_id':saved['delivery_id'],
                 'message_sha256':saved['delivery_message_sha256'],'owner':actor['owner'],
                 'attempt':actor['attempt_count'],'terminal':actor['runtime']['terminal'],
                 'native_pid':actor['runtime']['native_pid']}
        with StoreContext(self.state) as store:
            task=store.get('L1');task['runtime']['last_delivery']=receipt;store._save(task,'fixture_accepted')
        artifact=self.root/'report.md';artifact.write_text('Investigation result and its limits.\n')
        import hashlib
        evidence=self.root/'result.json'
        evidence.write_text(json.dumps({'request_id':saved['id'],'work_id':saved['work_id'],
            'owner':actor['owner'],'attempt':actor['attempt_count'],'delivery_id':saved['delivery_id'],
            'writer_stopped':True,'verification':['Checked the report against the bounded inputs.'],
            'artifacts':[{'path':str(artifact),'sha256':hashlib.sha256(artifact.read_bytes()).hexdigest()}],
            **({'completion_requirements':requirements} if requirements else {})}))
        return actor,saved,evidence

    def submit_result(self,actor,job,evidence):
        with patch.dict(os.environ,{'HC_TERMINAL_ID':actor['runtime']['terminal']}):
            return control.submit(self.config,self.state,job['id'],evidence,actor['owner'],actor['attempt_count'],job['delivery_id'])

    def test_exact_accepted_receipt_recovers_new_uncertain_transport_without_send(self):
        actor,job,evidence=self.accepted_job()
        path=control.folder(self.state)/(job['id']+'.json');saved=context.read(path)
        saved['phase']='uncertain';context.write(path,saved)
        with patch.object(control,'official') as sent:
            control.reconcile(self.config,self.state,job['id'])
        sent.assert_not_called();self.assertEqual(context.read(path)['phase'],'received')

    def test_receipt_recovery_rejects_wrong_delivery_and_legacy_unknown(self):
        actor,job,evidence=self.accepted_job();path=control.folder(self.state)/(job['id']+'.json')
        for delivery_id in ('f'*32,None):
            saved=context.read(path);saved['phase']='uncertain'
            if delivery_id is None:saved.pop('delivery_id',None)
            else:saved['delivery_id']=delivery_id
            context.write(path,saved);before=path.read_bytes()
            control.reconcile(self.config,self.state,job['id'])
            self.assertEqual(path.read_bytes(),before)

    def test_submission_is_candidate_until_reconcile_and_releases_next_work(self):
        actor,job,evidence=self.accepted_job()
        self.submit_result(actor,job,evidence)
        self.assertEqual(control.requests(self.state)[0]['phase'],'received')
        control.reconcile(self.config,self.state,job['id'])
        self.assertEqual(control.requests(self.state)[0]['phase'],'completed')
        message=self.root/'next.txt';message.write_text('Next approved investigation.')
        second=control.enqueue(self.config,self.state,'alpha',message,work_id='NEXT')
        with patch.object(context,'capacity_available',return_value=True),patch.object(control,'official',return_value=actor) as sent:
            control.advance(self.config,self.state,second['id'])
        self.assertEqual(sent.call_count,1)

    def test_changed_submitted_evidence_or_artifact_never_completes_work(self):
        actor,job,evidence=self.accepted_job();self.submit_result(actor,job,evidence)
        original=evidence.read_bytes();evidence.write_text('{}')
        control.reconcile(self.config,self.state,job['id'])
        self.assertEqual(control.requests(self.state)[0]['phase'],'received')
        evidence.write_bytes(original);(self.root/'report.md').write_text('Changed after submission.')
        control.reconcile(self.config,self.state,job['id'])
        self.assertEqual(control.requests(self.state)[0]['phase'],'received')

    def test_additional_plain_text_conditions_remain_pending_after_submission(self):
        actor,job,evidence=self.accepted_job('Independent device behavior must be checked.')
        self.submit_result(actor,job,evidence);control.reconcile(self.config,self.state,job['id'])
        saved=control.requests(self.state)[0]
        self.assertEqual(saved['phase'],'received')
        self.assertIn('추가 완료 조건',saved['reason'])

    def test_submission_rejects_stale_delivery_and_self_review_flag_without_artifacts(self):
        actor,job,evidence=self.accepted_job();value=json.loads(evidence.read_text())
        value['delivery_id']='e'*32;evidence.write_text(json.dumps(value))
        with self.assertRaises(ValueError):self.submit_result(actor,job,evidence)
        value['delivery_id']=job['delivery_id'];value.pop('artifacts');value['review_passed']=True
        evidence.write_text(json.dumps(value))
        with self.assertRaises(ValueError):self.submit_result(actor,job,evidence)

    def test_submitted_result_cannot_complete_on_a_replaced_process(self):
        actor,job,evidence=self.accepted_job();self.submit_result(actor,job,evidence)
        with StoreContext(self.state) as store:
            task=store.get('L1');task['runtime']['native_pid']+=1
            store._save(task,'fixture_replaced')
        self.fake.rows[actor['runtime']['terminal']]['foreground']['pid']+=1
        control.reconcile(self.config,self.state,job['id'])
        self.assertEqual(control.requests(self.state)[0]['phase'],'received')

    def test_wrong_native_receipt_owner_cannot_submit_a_result(self):
        actor,job,evidence=self.accepted_job()
        with StoreContext(self.state) as store:
            task=store.get('L1');task['runtime']['last_delivery']['owner']='another-owner'
            store._save(task,'fixture_receipt_owner_changed')
        with self.assertRaises(ValueError):self.submit_result(actor,job,evidence)

    def test_submit_cli_exposes_exact_execution_identity(self):
        from teamkit.__main__ import parser
        args=parser().parse_args(['pool','submit','--request','a'*32,'--evidence-file','result.json',
            '--expected-owner','owner','--expected-attempt','2','--delivery-id','b'*32])
        self.assertEqual(args.action,'submit');self.assertEqual(args.delivery_id,'b'*32)

    def test_submit_cli_routes_the_actual_candidate_and_reconciliation(self):
        actor,job,evidence=self.accepted_job()
        with patch.dict(os.environ,{'HC_TERMINAL_ID':actor['runtime']['terminal']}):
            result=self.call('pool','submit','--request',job['id'],'--evidence-file',str(evidence),
                '--expected-owner',actor['owner'],'--expected-attempt',str(actor['attempt_count']),
                '--delivery-id',job['delivery_id'])
        self.assertEqual(result['phase'],'received');self.assertIn('submission',result)
        control.reconcile(self.config,self.state,job['id'])
        self.assertEqual(control.requests(self.state)[0]['phase'],'completed')

    def legacy_lineage(self):
        actor=self.dispatched();previous=actor['runtime']['terminal'];current='trestored'
        ident='b'*32;path=control.folder(self.state)/(ident+'.json')
        message='Historical bounded investigation.'
        job={'id':ident,'team':'alpha','task':'L1','message':message,
             'digest':hashlib.sha256(message.encode()).hexdigest(),'phase':'done','at':time.time(),
             'updated_at':time.time(),'error_type':'HistoricalTimeout','reason':'receipt only, not work completion',
             'receiver':{'terminal':previous,'owner':actor['owner'],'attempt':actor['attempt_count']}}
        context.write(path,job);original=path.read_bytes()
        self.fake.rows[current]=copy.deepcopy(self.fake.rows[previous]);self.fake.rows[current]['address']=current
        self.fake.rows[current]['foreground']['pid']+=500
        session='11111111-2222-4333-8444-555555555555'
        self.fake.rows[current]['foreground']['argv']+=['resume',session]
        self.fake.rows[previous].update(state='finished',foreground=None,status=None)
        with StoreContext(self.state) as store:
            task=store.get('L1');task['runtime'].update(terminal=current,pane=current,
                native_pid=self.fake.rows[current]['foreground']['pid'],resumed_session=session)
            store._save(task,'native-session-resumed',{'from_terminal':previous,'to_terminal':current,
                'session':session,'owner_preserved':True,'state_preserved':True})
            event=store.events('L1')[-1]
        evidence=self.root/'receiver-lineage.json'
        evidence.write_text(json.dumps({'request_id':ident,'task':'L1','owner':actor['owner'],
            'attempt':actor['attempt_count'],'from_terminal':previous,'to_terminal':current,
            'session_id':session,'lineage_event_seq':event['seq'],
            'request_record_sha256':hashlib.sha256(original).hexdigest(),
            'operator_reference':'Fixture user-approved operational lineage repair',
            'verification':['Observed the original terminal finished and the exact resumed session idle.']}))
        return actor,job,evidence,path,original

    def rebind_legacy(self,actor,job,evidence,approved=True):
        return control.rebind_receiver(self.config,self.state,job['id'],evidence,
                                       actor['owner'],actor['attempt_count'],approved)

    def test_official_legacy_receiver_rebind_preserves_original_and_does_not_complete(self):
        actor,job,evidence,path,original=self.legacy_lineage()
        result=self.rebind_legacy(actor,job,evidence)
        self.assertEqual(result['phase'],'done')
        self.assertEqual(result['receiver']['terminal'],'trestored')
        self.assertEqual(context.read(path)['message'],job['message'])
        self.assertEqual(result['error_type'],'HistoricalTimeout')
        audit=result['receiver_lineage'][-1]
        self.assertEqual(audit['previous_receiver'],job['receiver'])
        self.assertEqual(Path(audit['original_request_file']).read_bytes(),original)
        completion=self.root/'historical-completion.json'
        completion.write_text(json.dumps({'work_id':job['id'],'owner':actor['owner'],
            'attempt':actor['attempt_count'],'writer_stopped':True,
            'verification':['Historical investigation report has been checked within its declared scope.']}))
        resolved=control.resolve(self.config,self.state,job['id'],'completed',completion,actor['owner'],actor['attempt_count'])
        self.assertEqual(resolved['phase'],'completed')

    def test_legacy_receiver_rebind_requires_operator_approval_and_matching_session(self):
        actor,job,evidence,path,original=self.legacy_lineage()
        with self.assertRaises(ValueError):self.rebind_legacy(actor,job,evidence,approved=False)
        self.assertEqual(path.read_bytes(),original)
        value=json.loads(evidence.read_text());value['session_id']='aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
        evidence.write_text(json.dumps(value))
        with self.assertRaises(ValueError):self.rebind_legacy(actor,job,evidence)
        self.assertEqual(path.read_bytes(),original)

    def test_legacy_unknown_and_pending_requests_are_never_unblocked_by_rebind(self):
        actor,job,evidence,path,original=self.legacy_lineage()
        for phase in ['uncertain','pending','sending','completed']:
            value=json.loads(original);value['phase']=phase;context.write(path,value)
            before=path.read_bytes()
            with self.assertRaises(ValueError):self.rebind_legacy(actor,job,evidence)
            self.assertEqual(path.read_bytes(),before)

    def test_legacy_receiver_rebind_is_idempotent_and_never_redelivers(self):
        actor,job,evidence,path,original=self.legacy_lineage()
        self.rebind_legacy(actor,job,evidence)
        before=path.read_bytes();calls=len(self.fake.calls)
        repeated=self.rebind_legacy(actor,job,evidence)
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(len(repeated['receiver_lineage']),1)
        self.assertFalse(any(c[0]=='send' for c in self.fake.calls[calls:]))

    def test_legacy_receiver_rebind_requires_exact_original_hash_and_resume_event(self):
        actor,job,evidence,path,original=self.legacy_lineage()
        value=json.loads(evidence.read_text())
        for key,wrong in [('request_record_sha256','0'*64),('lineage_event_seq',1),
                          ('owner','another-owner'),('attempt',2)]:
            evidence.write_text(json.dumps({**value,key:wrong}))
            with self.assertRaises(ValueError):self.rebind_legacy(actor,job,evidence)
            self.assertEqual(path.read_bytes(),original)

    def test_legacy_receiver_rebind_rejects_live_previous_native_and_wrong_actual_session(self):
        actor,job,evidence,path,original=self.legacy_lineage()
        previous=self.fake.rows[job['receiver']['terminal']]
        previous['state']='running'
        with self.assertRaises(ValueError):self.rebind_legacy(actor,job,evidence)
        previous['state']='finished'
        self.fake.rows['trestored']['foreground']['argv'][-1]='different-conversation'
        with self.assertRaises(ValueError):self.rebind_legacy(actor,job,evidence)
        self.assertEqual(path.read_bytes(),original)

    def test_rebound_legacy_receiver_fences_later_process_replacement(self):
        actor,job,evidence,path,original=self.legacy_lineage()
        self.rebind_legacy(actor,job,evidence)
        with StoreContext(self.state) as store:
            task=store.get('L1');task['runtime']['native_pid']+=1
            store._save(task,'native-replaced',{})
        completion=self.root/'completion.json'
        completion.write_text(json.dumps({'work_id':job['id'],'owner':actor['owner'],
            'attempt':actor['attempt_count'],'writer_stopped':True,'verification':['Observed result.']}))
        with self.assertRaises(ValueError):
            control.resolve(self.config,self.state,job['id'],'completed',completion,actor['owner'],actor['attempt_count'])
        self.assertEqual(context.read(path)['phase'],'done')

    def test_legacy_receiver_rebind_cli_uses_the_official_fenced_path(self):
        actor,job,evidence,path,original=self.legacy_lineage()
        result=self.call('pool','rebind-receiver','--request',job['id'],
            '--evidence-file',str(evidence),'--expected-owner',actor['owner'],
            '--expected-attempt',str(actor['attempt_count']),'--operator-approved')
        self.assertEqual(result['phase'],'done')
        self.assertEqual(result['receiver']['terminal'],'trestored')
        self.assertEqual(len(result['receiver_lineage']),1)

    def test_legacy_rebind_never_replaces_a_new_delivery_protocol_receipt(self):
        actor,job,evidence,path,original=self.legacy_lineage()
        value=json.loads(original);value['delivery_id']='c'*32;context.write(path,value)
        before=path.read_bytes()
        with self.assertRaises(ValueError):self.rebind_legacy(actor,job,evidence)
        self.assertEqual(path.read_bytes(),before)


    def test_work_id_conflicting_content_is_preserved_not_overwritten(self):
        self.prepare()
        message = self.root / 'queue.txt'
        message.write_text('First approved scope.')
        first = control.enqueue(self.config, self.state, 'alpha', message, work_id='SDK-ONE')
        message.write_text('Different unapproved replacement.')
        with self.assertRaises(ValueError):
            control.enqueue(self.config, self.state, 'alpha', message, work_id='SDK-ONE')
        self.assertEqual(context.read(control.folder(self.state)/(first['id']+'.json'))['message'], 'First approved scope.')


    def test_resolve_requires_receipt_owner_and_completion_evidence_before_next(self):
        result = self.dispatched()
        message = self.root/'resolve.txt'
        message.write_text('Inspect SDK and report verified results.')
        first = control.enqueue(self.config,self.state,'alpha',message,work_id='SDK-ONE')
        second = control.enqueue(self.config,self.state,'alpha',message,work_id='SDK-TWO')
        evidence = self.root/'complete.json'
        evidence.write_text(json.dumps({'work_id':'SDK-ONE','owner':result['owner'],'attempt':result['attempt_count'],
            'writer_stopped':True,'verification':['Checked final report artifact.']}))
        with self.assertRaises(ValueError):
            control.resolve(self.config,self.state,first['id'],'completed',evidence,result['owner'],result['attempt_count'])
        with patch.object(context,'capacity_available',return_value=True),patch.object(context,'clean_prompt',return_value=True),patch.object(control,'official',return_value=result) as sent:
            control.advance(self.config,self.state,first['id'])
            with self.assertRaises(ValueError):
                control.resolve(self.config,self.state,first['id'],'completed',evidence,'wrong-owner',result['attempt_count'])
            resolved=control.resolve(self.config,self.state,first['id'],'completed',evidence,result['owner'],result['attempt_count'])
            self.assertEqual(resolved['phase'],'completed')
            control.advance(self.config,self.state,second['id'])
            self.assertEqual(sent.call_count,2)

    def test_uncertain_queue_head_blocks_later_work_and_cannot_be_resolved(self):
        result=self.dispatched()
        message=self.root/'uncertain-queue.txt'
        message.write_text('Approved work.')
        first=control.enqueue(self.config,self.state,'alpha',message,work_id='FIRST')
        second=control.enqueue(self.config,self.state,'alpha',message,work_id='SECOND')
        with patch.object(context,'capacity_available',return_value=True),patch.object(context,'clean_prompt',return_value=True),patch.object(control,'official',side_effect=TimeoutError) as sent:
            control.advance(self.config,self.state,first['id'])
            control.advance(self.config,self.state,first['id'])
            control.advance(self.config,self.state,second['id'])
            self.assertEqual(sent.call_count,1)
        records=control.requests(self.state)
        self.assertEqual([j['phase'] for j in records],['uncertain','pending'])
        self.assertEqual(records[0]['error_stage'],'sending')
        self.assertEqual(records[0]['error_code'],'official_execution_unconfirmed')
        evidence=self.root/'bad-resolve.json'
        evidence.write_text(json.dumps({'writer_stopped':True,'verification':['Not sufficient to resolve unknown delivery.']}))
        with self.assertRaises(ValueError):
            control.resolve(self.config,self.state,first['id'],'completed',evidence,result['owner'],result['attempt_count'])

    def test_legacy_done_is_receipt_only_preserved_and_blocks_new_work(self):
        self.prepare()
        legacy={'id':'a'*32,'team':'alpha','task':'L1','message':'Old work','digest':'legacy',
                'phase':'done','at':1,'updated_at':2}
        path=control.folder(self.state)/(legacy['id']+'.json')
        context.write(path,legacy)
        before=path.read_bytes()
        message=self.root/'new.txt';message.write_text('New work.')
        new=control.enqueue(self.config,self.state,'alpha',message,work_id='NEW')
        with patch.object(control,'plan') as plan:
            control.advance(self.config,self.state,new['id'])
        plan.assert_not_called()
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(control.requests(self.state)[0]['id'],legacy['id'])

    def test_concurrent_queue_drainers_send_only_one_head(self):
        from concurrent.futures import ThreadPoolExecutor
        result=self.dispatched()
        message=self.root/'concurrent.txt';message.write_text('Approved work.')
        first=control.enqueue(self.config,self.state,'alpha',message,work_id='FIRST')
        second=control.enqueue(self.config,self.state,'alpha',message,work_id='SECOND')
        with patch.object(context,'capacity_available',return_value=True),patch.object(context,'clean_prompt',return_value=True),patch.object(control,'official',return_value=result) as sent:
            with ThreadPoolExecutor(max_workers=4) as workers:
                list(workers.map(lambda ident:control.advance(self.config,self.state,ident),[first['id'],second['id'],first['id'],second['id']]))
            self.assertEqual(sent.call_count,1)

    def test_request_constraint_is_a_priority_and_falls_back_to_the_other_eligible_account(self):
        from teamkit.__main__ import select_execution
        self.prepare()
        task = self.call('show', 'L1')
        task['execution_constraint'] = {'provider': 'codex', 'account': 'personal'}
        store = Store(self.state)
        try:
            selected = select_execution(load(self.config), store, task, time.time())
        finally:
            store.close()
        # codex/personal has no fresh quota: the designation does not block the eligible codex/team.
        self.assertEqual((selected['action'], selected['provider'], selected['account']), ('dispatch', 'codex', 'team'))

    def test_cli_pinned_assignment_records_the_priority_and_plans_the_eligible_account(self):
        self.prepare()
        message = self.root / 'pin-request.txt'
        message.write_text('Inspect the approved SDK scope only.')
        with patch.object(context, 'ensure'), patch.object(context, 'refresh_candidates'), \
             patch.object(context, '_call', return_value={'terminals': []}), \
             patch.object(control, 'official', return_value={'state': 'unconfirmed'}) as official:
            request = self.call('pool', 'assign', '--team', 'alpha', '--message-file', str(message),
                                '--provider', 'claude', '--account', 'team')
            control.advance(self.config, self.state, request['id'])
        official.assert_called_once()
        self.assertIn('dispatch', official.call_args.args)
        self.assertEqual(request['execution_constraint'], {'provider': 'claude', 'account': 'team'})
        self.assertEqual(self.call('show', 'L1')['execution_constraint'], request['execution_constraint'])
        planned = self.call('pool', 'plan', '--team', 'alpha')
        self.assertEqual((planned['action'], planned['selection']['provider'], planned['selection']['account']),
                         ('dispatch', 'codex', 'team'))
        self.assertEqual(self.call('plan', 'L1')['account'], 'team')
        # An unconfirmed mocked dispatch is uncertain, never successful delivery.
        self.assertEqual(control.requests(self.state)[0]['phase'], 'uncertain')

    def test_requested_provider_account_survives_official_handoff_and_dispatch(self):
        previous = self.dispatched('claude')
        self.prepare('codex')
        message = self.root / 'fixed-request.txt'
        message.write_text('Inspect this SDK using only the requested profile.')
        with patch.object(context, 'ensure'):
            request = self.call('pool', 'assign', '--team', 'alpha', '--message-file', str(message),
                                '--provider', 'codex', '--account', 'team')
        with patch.object(context, 'observe', return_value=None):
            control.advance(self.config, self.state, request['id'])
        self.assertEqual(control.plan(self.config, self.state, 'alpha')['selection']['provider'], 'codex')
        job = next(j for j in context.jobs(self.state) if j['target']['id'] == 'L1')
        self.assertEqual((job['next_session']['provider'], job['next_session']['account']), ('codex', 'team'))
        with patch('teamkit.core_runtime.CoreRuntime.prompt') as sent:
            with self.assertRaises(ValueError):
                self.call('tell', 'L1', '--message-file', str(message), '--expected-owner', previous['owner'])
            sent.assert_not_called()
        evidence = self.root / 'stopped.json'
        evidence.write_text(json.dumps({'verification': ['fixture writer stopped'], 'next': 'Continue the fixed request.'}))
        self.call('handoff', 'L1', '--owner', previous['owner'], '--writer-stopped', '--evidence-file', str(evidence))

        def received(runtime, store, config_path, task, owner, text):
            store.receipt('L1', owner)
            store.park('L1', owner)

        with patch.object(context, 'ensure'), patch('teamkit.receipt.prompt', side_effect=received):
            result = self.call('dispatch', 'L1')
        self.assertEqual((result['provider'], result['account']), ('codex', 'team'))
        self.assertEqual(result['execution_constraint'], {'provider': 'codex', 'account': 'team'})

    def test_codex_dispatch_uses_prepared_native_with_common_claim_and_receipt(self):self.dispatched()
    def test_claude_dispatch_uses_prepared_statusline_and_actual_profile_receipt(self):self.dispatched('claude')

    def test_unknown_usage_waits_without_claim_or_preferred_slot_fallback(self):
        pool.prepare(self.config,self.state)
        route=control.plan(self.config,self.state,'alpha')
        self.assertEqual(route['action'],'wait')
        self.assertEqual(self.call('show','L1')['state'],'queued')
        self.assertEqual(pool.status(load(self.config),self.state)['counts'],{'prepared':4})

    def test_reservation_at_threshold_records_next_without_claiming_it(self):
        result=self.dispatched()
        target=context.targets(load(self.config),self.state)[0]
        job=context.request(load(self.config),self.state,target,
            {'session_id':'session-1','source':'codex-rollout','used':90,'observed_at':time.time()})
        self.assertEqual(job['phase'],'pending')
        self.assertEqual(job['next_session']['provider'],'codex')
        self.assertEqual(job['next_session']['account'],'team')
        self.assertEqual(pool.status(load(self.config),self.state)['counts'],{'leased':1,'prepared':3})
        self.assertEqual(self.call('show','L1')['owner'],result['owner'])

    def test_assign_delivers_once_and_rejects_different_pending_request(self):
        result=self.dispatched();message=self.root/'request.txt';message.write_text('Only inspect the approved scope; report and park.')
        first=control.enqueue(self.config,self.state,'alpha',message)
        self.assertEqual(control.enqueue(self.config,self.state,'alpha',message)['id'],first['id'])
        other=self.root/'other.txt';other.write_text('different pending work')
        self.assertNotEqual(control.enqueue(self.config,self.state,'alpha',other)['id'],first['id'])
        with patch.object(context,'capacity_available',return_value=True),patch.object(context,'clean_prompt',return_value=True),patch.object(control,'official',return_value=result) as official:
            control.advance(self.config,self.state,first['id']);control.advance(self.config,self.state,first['id'])
            self.assertEqual(official.call_count,1)
            self.assertIn('--expected-owner',official.call_args.args)
        self.assertEqual(control.requests(self.state)[0]['phase'],'received')

    def test_uncertain_send_never_replays_or_changes_owner(self):
        result=self.dispatched();p=self.root/'request.txt';p.write_text('Approved remaining work only')
        job=control.enqueue(self.config,self.state,'alpha',p)
        with patch.object(context,'capacity_available',return_value=True),patch.object(context,'clean_prompt',return_value=True),patch.object(control,'official',side_effect=TimeoutError) as call:
            control.advance(self.config,self.state,job['id']);control.advance(self.config,self.state,job['id'])
            self.assertEqual(call.call_count,1)
        self.assertEqual(control.requests(self.state)[0]['phase'],'uncertain')
        self.assertEqual(self.call('show','L1')['owner'],result['owner'])

    def test_changed_quota_uses_handoff_instead_of_telling_previous_account(self):
        result=self.dispatched();store=Store(self.state);now=time.time()
        for account,used in [('team',91),('personal',2)]:
            store.quota('codex/'+account,{'binding':binding(self.value['profiles']['codex'][account]),'observed_at':now,
                'five_hour':{'used':used,'resets_at':now+3600},'weekly':{'used':used,'resets_at':now+86400}})
        store.close()
        route=control.plan(self.config,self.state,'alpha')
        self.assertEqual(route['action'],'handoff');self.assertEqual(route['selection']['account'],'personal')
        p=self.root/'request.txt';p.write_text('Preserve approved scope')
        request=control.enqueue(self.config,self.state,'alpha',p)
        with patch.object(context,'observe',return_value=None),patch.object(control,'official') as sent:
            control.advance(self.config,self.state,request['id']);sent.assert_not_called()
        saved=control.requests(self.state)[0]
        self.assertEqual(saved['phase'],'waiting_handoff')
        job=context.read(context.job_path(self.state,saved['handoff_job']))
        self.assertTrue(job['policy_rotation']);self.assertEqual(job['next_session']['account'],'personal')
        self.assertEqual(pool.status(load(self.config),self.state)['counts'],{'leased':1,'prepared':3})

    def test_quota_expiry_during_native_check_prevents_delivery(self):
        result=self.dispatched();p=self.root/'request.txt';p.write_text('Approved scope only')
        request=control.enqueue(self.config,self.state,'alpha',p)
        def expired(*args):
            store=Store(self.state);value=store.quota('codex/team');value['observed_at']-=1000;store.quota('codex/team',value);store.close();return True
        with patch.object(context,'capacity_available',return_value=True),patch.object(context,'clean_prompt',side_effect=expired),patch.object(control,'official',return_value=result) as sent:
            control.advance(self.config,self.state,request['id']);sent.assert_not_called()
        self.assertEqual(control.requests(self.state)[0]['phase'],'pending')

    def test_official_tell_rechecks_usage_after_identity_without_waking_on_expiry(self):
        result=self.dispatched();p=self.root/'message.txt';p.write_text('Approved remaining scope')
        def expire(*args,**kwargs):
            store=Store(self.state);value=store.quota('codex/team');value['observed_at']-=1000;store.quota('codex/team',value);store.close()
        with patch('teamkit.providers.identity',side_effect=expire),patch('teamkit.core_runtime.CoreRuntime.prompt') as sent:
            with self.assertRaises(ValueError):self.call('tell','L1','--message-file',str(p),'--expected-owner',result['owner'])
            sent.assert_not_called()
        self.assertEqual(self.call('show','L1')['state'],'ready')

    def test_official_tell_preserves_same_active_pool_generation(self):
        result=self.dispatched();p=self.root/'message.txt';p.write_text('Approved remaining scope')
        def parked(actor,name,text):
            self.assertIn('CONTINUATION OF AN ALREADY RECEIPTED TASK',text)
            self.assertEqual(actor.task['owner'],result['owner'])
            self.call('park','L1','--owner',result['owner'])
        with patch('teamkit.core_runtime.CoreRuntime.prompt',new=parked):
            after=self.call('tell','L1','--message-file',str(p),'--expected-owner',result['owner'])
        self.assertEqual(after['state'],'ready');self.assertEqual(after['runtime']['pool_generation'],1)
        self.assertEqual(sum(c[0]=='run' for c in self.fake.calls),4)

    def test_status_reports_four_shared_account_snapshots_without_creating_a_store(self):
        from types import SimpleNamespace
        missing=self.root/'absent'/'state.sqlite'
        result=control.handle(SimpleNamespace(config=self.config,state=missing,action='status'))
        self.assertEqual(len(result['accounts']),4)
        self.assertTrue(all(not row['eligible'] for row in result['accounts']))
        self.assertFalse(missing.parent.exists())

    def test_dispatch_checks_unknown_preferred_provider_before_using_cached_lower_priority(self):
        self.value['execution_preference']={'provider_order':['claude','codex'],'weekend_account_first':True}
        self.config.write_text(json.dumps(self.value));self.prepare()
        self.assertEqual(control.plan(self.config,self.state,'alpha')['selection']['provider'],'codex')
        def fresh(*args):
            now=time.time();store=Store(self.state)
            store.quota('claude/team',{'source':'claude-statusline-response','binding':binding(self.value['profiles']['claude']['team']),
                'observed_at':now,'five_hour':{'used':1,'resets_at':now+3600},'weekly':{'used':1,'resets_at':now+86400}});store.close()
        def stop(config,state,task,role):
            self.assertEqual(role['provider'],'claude')
            raise ValueError('stopped before claim')
        with patch.object(context,'refresh_candidates',side_effect=fresh) as refresh,patch.object(pool,'reserve',side_effect=stop),patch.object(context,'ensure'):
            with self.assertRaisesRegex(ValueError,'stopped before claim'):self.call('dispatch','L1')
            refresh.assert_called_once()

    def test_bootstrap_replaces_existing_representative_only_after_receipt(self):
        self.prepare()
        binding_path=Path(self.value['session_pool']['bindings'])
        binding_path.write_text(json.dumps({'teams':{'alpha':{'terminal':'told','space':'spalpha'}}}))
        def received(*args):
            store=Store(self.state);task=store.get('L1');store.close()
            return {**task,'state':'ready','runtime':{'terminal':'tnew','identity_checked':True}}
        with patch.object(control,'refresh_choice'),patch.object(context,'capacity_available',return_value=True),patch.object(control,'official',side_effect=received),patch.object(context,'attach') as attach:
            self.call('pool','bootstrap','--team','alpha')
        self.assertEqual(attach.call_args.args[2],{'kind':'task','id':'L1','terminal':'told'})
        self.assertEqual(attach.call_args.args[3],'tnew')

    def test_usage_mode_explicit_later_request_is_not_blocked_by_pending_head(self):
        self.dispatched()
        self.value['operations']={'allocation_policy':'usage'}
        self.config.write_text(json.dumps(self.value))
        message=self.root/'pull-later.txt';message.write_text('Read approved fixture only.')
        with patch.object(control,'notify'):
            first=control.enqueue(self.config,self.state,'alpha',message)
            message.write_text('Read another approved fixture only.')
            later=control.enqueue(self.config,self.state,'alpha',message)
        def official(config,state,*args):
            self.assertEqual('tell',args[0])
            store=Store(state)
            try:return store.get(args[1])
            finally:store.close()
        with patch.object(context,'capacity_available',return_value=True),patch.object(control,'refresh_choice'),patch.object(control,'official',side_effect=official) as sent:
            control.advance(self.config,self.state,later['id'])
        by_id={job['id']:job for job in control.requests(self.state)}
        self.assertEqual('pending',by_id[first['id']]['phase'])
        self.assertEqual('received',by_id[later['id']]['phase'])
        self.assertEqual(1,sent.call_count)
