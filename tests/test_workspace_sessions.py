import copy
import json
import os
from unittest.mock import patch
from test_cli import Fixture
from teamkit import workspace_sessions as ws, pool_control
from teamkit.config import load
from teamkit.tickets import TicketStore
from teamkit.runtime import RuntimeError as NativeError


class WorkspaceTests(Fixture):
    def setUp(self):
        super().setUp()
        self.value['runtime_backend']='core'
        self.value['profiles']['claude']={a:{'home':str(self.root/('claude-'+a)),'identity':{'email':a+'@example.invalid'},'model':'fixture','effort':'medium'} for a in ('team','personal')}
        self.value['session_pool']={'enabled':True,'bindings':str(self.root/'bindings.json'),'tasks':{team:'L'+str(i+1) for i,team in enumerate(self.value['teams'])}}
        (self.root/'bindings.json').write_text(json.dumps({'teams':{'alpha':{'space':'space-a'}}}))
        self.config.write_text(json.dumps(self.value))
        self.c=load(self.config)
        TicketStore(self.state).close()
        ws.Store(self.state).close()
        self.native={'address':'t1','name':'existing','state':'live','turn':4,
                     'labels':{'herdr.space':'space-a','teamkit.owner':'old'},
                     'status':{'state':'idle','app':'codex'},
                     'foreground':{'pid':123,'name':'codex','cwd':str(self.repo),
                       'argv':['codex','--model','fixture','-c','model_reasoning_effort="medium"','--no-daemon']}}
        def call(*args,**kwargs):
            if args[0]=='ls':return {'terminals':[{'address':'t1'}]}
            if args[0]=='get':return copy.deepcopy(self.native)
            if args[0]=='read':return {'complete':True,'text':'› '}
            if args[0]=='label':
                for label in args[2:]:
                    k,v=label.split('=',1);self.native['labels'][k]=v
                return {}
            raise AssertionError(args)
        for target,value in [('teamkit.workspace_sessions._call',call),
                             ('teamkit.core_runtime._call',call),
                             ('teamkit.workspace_sessions.require_managed',lambda:None),
                             ('teamkit.providers.identity',lambda *a:None),
                             ('teamkit.workspace_sessions.eligible',lambda *a:True),
                             ('teamkit.workspace_sessions._process_environment',lambda pid:{}),
                             ('teamkit.workspace_sessions._namespace_matches',lambda p,pr,e: pr['home']==self.c['profiles']['codex']['team']['home'])]:
            p=patch(target,new=value);p.start();self.addCleanup(p.stop)

    def test_discovery_uses_workspace_profile_and_actual_pid(self):
        result=ws.discover(self.c,self.state,'alpha')
        self.assertEqual(result[0]['pid'],123)
        self.assertEqual(result[0]['account'],'team')
        self.assertTrue(result[0]['registration_required'])
        self.assertEqual(self.native['labels']['teamkit.owner'],'old')
        self.native['labels']['herdr.space']='other'
        self.assertEqual(ws.discover(self.c,self.state,'alpha'),[])

    def test_working_and_draft_are_not_candidates(self):
        self.native['status']['state']='working'
        with self.assertRaisesRegex(ValueError,'working'):ws.observe(self.c,self.state,'alpha','t1')
        self.native['status']['state']='idle'
        original=ws._call
        with patch.object(ws,'_call',side_effect=lambda *a: {'complete':True,'text':'› unfinished'} if a[0]=='read' else original(*a)):
            with self.assertRaisesRegex(ValueError,'draft'):ws.observe(self.c,self.state,'alpha','t1')

    def test_registration_is_one_shot_fresh_owner_then_exact_receipt(self):
        with patch.object(ws.CoreRuntime,'accept',return_value={'accepted':True}):
            ws.register(self.config,self.state,'alpha','t1',True)
        q=TicketStore(self.state);row=ws.rows(q.db)[0];q.close()
        self.assertNotEqual(row['actor']['owner'],'old')
        self.assertEqual(ws.executors(self.state),[])
        with self.assertRaisesRegex(ValueError,'already exists'):
            ws.register(self.config,self.state,'alpha','t1',True)
        self.native['turn']=5
        with patch.dict(os.environ,{'HC_TERMINAL_ID':'t1'}):
            ws.receipt(self.config,self.state,'t1',row['actor']['owner'],row['nonce'])
            with self.assertRaisesRegex(ValueError,'consumed'):
                ws.receipt(self.config,self.state,'t1',row['actor']['owner'],row['nonce'])
        self.assertEqual(ws.executors(self.state)[0]['owner'],row['actor']['owner'])

    def test_pid_replacement_or_expired_receipt_never_becomes_ready(self):
        with patch.object(ws.CoreRuntime,'accept',return_value={'accepted':True}):ws.register(self.config,self.state,'alpha','t1',True)
        q=TicketStore(self.state);row=ws.rows(q.db)[0];q.close()
        self.native['foreground']['pid']=124;self.native['turn']=5
        with patch.dict(os.environ,{'HC_TERMINAL_ID':'t1'}):
            with self.assertRaises(NativeError):ws.receipt(self.config,self.state,'t1',row['actor']['owner'],row['nonce'])
        self.assertEqual(ws.executors(self.state),[])

    def test_transport_failure_preserves_intent_and_prevents_repeat(self):
        with patch.object(ws.CoreRuntime,'accept',side_effect=RuntimeError('unconfirmed')):
            with self.assertRaises(RuntimeError):ws.register(self.config,self.state,'alpha','t1',True)
        with self.assertRaisesRegex(ValueError,'already exists'):ws.register(self.config,self.state,'alpha','t1',True)
        self.assertEqual(ws.executors(self.state),[])

    def test_unknown_profile_and_expired_nonce_are_rejected(self):
        with patch.object(ws,'_namespace_matches',return_value=False):
            with self.assertRaisesRegex(ValueError,'ambiguous'):ws.observe(self.c,self.state,'alpha','t1')
        with patch.object(ws.CoreRuntime,'accept',return_value={'accepted':True}):ws.register(self.config,self.state,'alpha','t1',True)
        q=TicketStore(self.state);row=ws.rows(q.db)[0]
        with q.transaction():
            row['expires_at']=0;ws.save(q.db,row)
        q.close();self.native['turn']=5
        with patch.dict(os.environ,{'HC_TERMINAL_ID':'t1'}):
            with self.assertRaisesRegex(ValueError,'expired'):ws.receipt(self.config,self.state,'t1',row['actor']['owner'],row['nonce'])
        self.assertEqual(ws.executors(self.state),[])

    def test_only_proven_preparation_failure_can_resume_same_request(self):
        from teamkit.context_handoff import write, read
        ident='a'*32;path=pool_control.folder(self.state)/(ident+'.json')
        job=dict(id=ident,phase='uncertain',error_code='preparation_failed',error_stage='pending',error_type='RuntimeError',message='preserved')
        write(path,job)
        with patch('teamkit.core_runtime.require_managed'),patch('teamkit.pool_control.notify'):
            result=pool_control.resume_preparation(self.config,self.state,ident,True)
        self.assertEqual(result['id'],ident);self.assertEqual(result['phase'],'pending')
        self.assertEqual(read(path)['message'],'preserved')
        self.assertEqual(result['recovery_history'][0]['error_type'],'RuntimeError')
        for field in ('receiver','delivery_id','delivery_receipt','submission'):
            write(path,{**job,field:'existing'})
            with patch('teamkit.core_runtime.require_managed'):
                with self.assertRaisesRegex(ValueError,'pre-delivery'):pool_control.resume_preparation(self.config,self.state,ident,True)
            self.assertEqual(read(path)[field],'existing')

    def test_registered_actor_workspace_and_cwd_are_rechecked_before_native_use(self):
        from teamkit.operations import NativeRuntime
        with patch.object(ws.CoreRuntime,'accept',return_value={'accepted':True}):ws.register(self.config,self.state,'alpha','t1',True)
        q=TicketStore(self.state);row=ws.rows(q.db)[0];q.close()
        self.native['turn']=5
        with patch.dict(os.environ,{'HC_TERMINAL_ID':'t1'}):ws.receipt(self.config,self.state,'t1',row['actor']['owner'],row['nonce'])
        actor=ws.executors(self.state)[0]
        runtime=NativeRuntime(self.c,self.state)
        self.native['labels']['herdr.space']='another'
        with self.assertRaisesRegex(ValueError,'workspace'):runtime.native(actor)
        self.native['labels']['herdr.space']='space-a';self.native['foreground']['cwd']=str(self.root)
        with self.assertRaisesRegex(ValueError,'root'):runtime.native(actor)

    def test_registration_fences_previous_owner_reservations(self):
        with patch.object(ws.CoreRuntime,'accept',return_value={'accepted':True}):ws.register(self.config,self.state,'alpha','t1',True)
        q=TicketStore(self.state);ledger=ws.ResourceLedger(q.db)
        with self.assertRaisesRegex(ValueError,'registration'):
            ledger._session_available(q.db,'another',{'terminal':'t1','pid':123,'owner':'old'})
        q.close()

    def test_expired_before_delivery_does_not_send_and_can_be_explicitly_registered_again(self):
        now=[100.0];original=ws._call
        def call(*args,**kwargs):
            result=original(*args,**kwargs)
            if args[0]=='label':now[0]=500.0
            return result
        with patch.object(ws.time,'time',side_effect=lambda:now[0]),patch.object(ws,'_call',side_effect=call),patch.object(ws.CoreRuntime,'accept') as accept:
            with self.assertRaisesRegex(ValueError,'expired'):ws.register(self.config,self.state,'alpha','t1',True)
            accept.assert_not_called()
        q=TicketStore(self.state);row=ws.rows(q.db)[0];q.close()
        self.assertEqual(row['phase'],'expired_before_delivery')
        with patch.object(ws.CoreRuntime,'accept',return_value={'accepted':True}):ws.register(self.config,self.state,'alpha','t1',True)
        q=TicketStore(self.state);new=ws.rows(q.db)[0];q.close()
        self.assertNotEqual(new['actor']['owner'],row['actor']['owner'])
        self.assertEqual(new['history'][0]['actor']['owner'],row['actor']['owner'])

    def test_legacy_plan_uses_fresh_receipted_workspace_actor(self):
        with patch.object(ws.CoreRuntime,'accept',return_value={'accepted':True}):ws.register(self.config,self.state,'alpha','t1',True)
        q=TicketStore(self.state);row=ws.rows(q.db)[0];q.close();self.native['turn']=5
        with patch.dict(os.environ,{'HC_TERMINAL_ID':'t1'}):ws.receipt(self.config,self.state,'t1',row['actor']['owner'],row['nonce'])
        self.c['operations']={'allocation_policy':'usage'}
        result=ws.plan(self.c,self.state,'alpha')
        self.assertEqual(result['task'],row['actor']['task'])
        self.assertEqual(result['owner'],row['actor']['owner'])
        self.assertTrue(result['workspace'])
        self.native['labels']['herdr.space']='another'
        self.assertIsNone(ws.plan(self.c,self.state,'alpha'))

    def test_existing_legacy_request_is_sent_to_receipted_actor_without_replacing_id(self):
        from teamkit import context_handoff as context
        self.value['operations']={'allocation_policy':'usage'}
        self.config.write_text(json.dumps(self.value))
        self.call('create','L1','--team','alpha','--role','leader','--root',str(self.repo),'--goal-file',str(self.root/'goal.txt'))
        with patch.object(ws.CoreRuntime,'accept',return_value={'accepted':True}):ws.register(self.config,self.state,'alpha','t1',True)
        q=TicketStore(self.state);record=ws.rows(q.db)[0];q.close();self.native['turn']=5
        with patch.dict(os.environ,{'HC_TERMINAL_ID':'t1'}):ws.receipt(self.config,self.state,'t1',record['actor']['owner'],record['nonce'])
        message=self.root/'request.txt';message.write_text('Read the approved fixture only.')
        with patch.object(pool_control,'notify'):
            job=pool_control.enqueue(self.config,self.state,'alpha',message)
        def official(config,state,*args):
            self.assertEqual(args[0],'tell');self.assertEqual(args[1],record['actor']['task'])
            store=ws.Store(state);actor=store.get(args[1]);store.close()
            return actor
        with patch.object(context,'capacity_available',return_value=True),patch.object(pool_control,'refresh_choice'),patch.object(pool_control,'official',side_effect=official):
            pool_control.advance(self.config,self.state,job['id'])
        result=pool_control.requests(self.state)
        self.assertEqual(len(result),1);self.assertEqual(result[0]['id'],job['id'])
        self.assertEqual(result[0]['task'],'L1');self.assertEqual(result[0]['phase'],'received',result[0])
        self.assertEqual(result[0]['receiver']['task'],record['actor']['task'])
        self.assertEqual(result[0]['receiver']['owner'],record['actor']['owner'])

    def test_discovery_failure_has_safe_specific_code_and_redacts_unknown_provider_error(self):
        self.native['status']['state']='working'
        result=ws.discover(self.c,self.state,'alpha')[0]
        self.assertEqual(result['reason_code'],'working')
        result=ws.discovery_failure(ValueError('provider credential=private-value'))
        self.assertEqual(result['reason_code'],'observation_failed')
        self.assertNotIn('private-value',json.dumps(result))

    def test_public_tell_rejects_changed_workspace_before_prompt(self):
        with patch.object(ws.CoreRuntime,'accept',return_value={'accepted':True}):ws.register(self.config,self.state,'alpha','t1',True)
        q=TicketStore(self.state);row=ws.rows(q.db)[0];q.close();self.native['turn']=5
        with patch.dict(os.environ,{'HC_TERMINAL_ID':'t1'}):ws.receipt(self.config,self.state,'t1',row['actor']['owner'],row['nonce'])
        self.native['labels']['herdr.space']='another'
        message=self.root/'tell.txt';message.write_text('Approved fixture continuation.')
        with patch('teamkit.core_runtime.require_managed'),patch.object(ws.CoreRuntime,'prompt') as prompt:
            with self.assertRaisesRegex(ValueError,'workspace'):
                self.call('tell',row['actor']['task'],'--message-file',str(message))
            prompt.assert_not_called()
        store=ws.Store(self.state);self.assertEqual(store.get(row['actor']['task'])['state'],'ready');store.close()

    def test_public_tell_rechecks_workspace_in_prompt_send_fence(self):
        with patch.object(ws.CoreRuntime,'accept',return_value={'accepted':True}):ws.register(self.config,self.state,'alpha','t1',True)
        q=TicketStore(self.state);row=ws.rows(q.db)[0];q.close();self.native['turn']=5
        with patch.dict(os.environ,{'HC_TERMINAL_ID':'t1'}):ws.receipt(self.config,self.state,'t1',row['actor']['owner'],row['nonce'])
        message=self.root/'tell.txt';message.write_text('Approved fixture continuation.')
        sent=[]
        def prompt(runtime,name,text,before_send=None):
            self.native['foreground']['cwd']=str(self.root)
            if before_send is not None:before_send()
            sent.append(text)
        with patch('teamkit.core_runtime.require_managed'),patch.object(ws.CoreRuntime,'prompt',new=prompt):
            with self.assertRaisesRegex(ValueError,'root'):
                self.call('tell',row['actor']['task'],'--message-file',str(message))
        self.assertEqual(sent,[])

    def test_native_prompt_guard_failure_prevents_transport(self):
        with patch.object(ws.CoreRuntime,'accept',return_value={'accepted':True}):ws.register(self.config,self.state,'alpha','t1',True)
        q=TicketStore(self.state);row=ws.rows(q.db)[0];q.close();self.native['turn']=5
        with patch.dict(os.environ,{'HC_TERMINAL_ID':'t1'}):ws.receipt(self.config,self.state,'t1',row['actor']['owner'],row['nonce'])
        store=ws.Store(self.state);task=store.get(row['actor']['task']);store.close()
        runtime=ws.CoreRuntime(task)
        original=ws._call
        calls=[]
        def call(*args,**kwargs):calls.append(args[0]);return original(*args,**kwargs)
        with patch('teamkit.core_runtime.require_managed'),patch('teamkit.core_runtime._call',side_effect=call):
            with self.assertRaisesRegex(ValueError,'changed'):
                runtime.prompt(task['runtime']['name'],'Approved fixture',before_send=lambda:(_ for _ in ()).throw(ValueError('changed')))
        self.assertNotIn('send',calls)

    def test_resolve_workspace_result_does_not_require_new_work_quota(self):
        from teamkit.context_handoff import write
        with patch.object(ws.CoreRuntime,'accept',return_value={'accepted':True}):ws.register(self.config,self.state,'alpha','t1',True)
        q=TicketStore(self.state);row=ws.rows(q.db)[0];q.close();self.native['turn']=5
        with patch.dict(os.environ,{'HC_TERMINAL_ID':'t1'}):ws.receipt(self.config,self.state,'t1',row['actor']['owner'],row['nonce'])
        actor=row['actor'];ident='d'*32;path=pool_control.folder(self.state)/(ident+'.json')
        original=dict(id=ident,team='alpha',task='L1',phase='received',work_id='approved-fixture',
                      receiver=dict(task=actor['task'],terminal='t1',owner=actor['owner'],attempt=1,native_pid=123))
        evidence=self.root/'resolution.json'
        import time
        from teamkit.policy import eligible as actual_eligible
        store=ws.Store(self.state)
        now=time.time()
        store.quota('codex/team',dict(binding=ws.binding(self.c['profiles']['codex']['team']),
            source='codex-native-quota',observed_at=now,five_hour=dict(used=95,resets_at=now+3600),
            weekly=dict(used=10,resets_at=now+86400)))
        store.close()
        for outcome in ('completed','deferred'):
            write(path,original)
            evidence.write_text(json.dumps(dict(work_id='approved-fixture',owner=actor['owner'],attempt=1,
                writer_stopped=True,verification=['Verified bounded fixture result.'],next='Resume remaining approved fixture.')))
            with patch.object(ws,'eligible',new=actual_eligible),patch('teamkit.core_runtime.require_managed'),patch.object(pool_control,'notify'):
                result=self.call('pool','resolve','--request',ident,'--outcome',outcome,'--evidence-file',str(evidence),
                                 '--expected-owner',actor['owner'],'--expected-attempt','1')
            self.assertEqual(result['phase'],outcome)
        store=ws.Store(self.state);task=store.get(actor['task']);store.close()
        with patch.object(ws,'eligible',new=actual_eligible):
            with self.assertRaisesRegex(ValueError,'quota'):pool_control.idle_native(task,self.c,self.state)

    def test_legacy_pool_prefers_company_workspace_even_when_personal_registered_first(self):
        import time
        from teamkit import context_handoff as context
        from teamkit.policy import eligible
        from teamkit.config import binding, profile
        self.value['operations']={'allocation_policy':'usage'}
        self.config.write_text(json.dumps(self.value));self.c=load(self.config)
        self.call('create','L1','--team','alpha','--role','leader','--root',str(self.repo),'--goal-file',str(self.root/'goal.txt'))
        natives={'t1':copy.deepcopy(self.native)}
        natives['t2']=copy.deepcopy(self.native);natives['t2']['address']='t2';natives['t2']['foreground']['pid']=124
        def call(*args,**kwargs):
            if args[0]=='ls':return {'terminals':[{'address':key} for key in natives]}
            if args[0]=='read':return {'complete':True,'text':'› '}
            native=natives[args[1]]
            if args[0]=='get':return copy.deepcopy(native)
            if args[0]=='label':
                for value in args[2:]:
                    k,v=value.split('=',1);native['labels'][k]=v
                return {}
            raise AssertionError(args)
        store=ws.Store(self.state)
        try:
            for account in ('team','personal'):
                store.quota('codex/'+account,dict(source='codex-native-quota',observed_at=time.time(),
                    binding=binding(profile(self.c,'codex',account)),five_hour={'used':10,'resets_at':time.time()+300},weekly={'used':10,'resets_at':time.time()+300}))
        finally:store.close()
        with patch.object(ws,'_call',side_effect=call),patch('teamkit.core_runtime._call',side_effect=call), \
             patch.object(ws,'_process_environment',side_effect=lambda pid:{'account':'personal' if pid==123 else 'team'}), \
             patch.object(ws,'_namespace_matches',side_effect=lambda p,pr,e:pr['home']==self.c['profiles'][p][e['account']]['home']), \
             patch.object(ws,'eligible',side_effect=eligible),patch.object(ws.CoreRuntime,'accept',return_value={'accepted':True}):
            for terminal in ('t1','t2'):
                ws.register(self.config,self.state,'alpha',terminal,True)
                q=TicketStore(self.state);record=next(r for r in ws.rows(q.db) if r['terminal']==terminal);q.close()
                natives[terminal]['turn']=5
                with patch.dict(os.environ,{'HC_TERMINAL_ID':terminal}):ws.receipt(self.config,self.state,terminal,record['actor']['owner'],record['nonce'])
            company=next(a for a in ws.executors(self.state) if a['account']=='team')
            message=self.root/'pull-order.txt';message.write_text('Read fixture only.')
            with patch.object(pool_control,'notify'):job=pool_control.enqueue(self.config,self.state,'alpha',message)
            sent=[]
            def official(config,state,*args):
                sent.append(args[1]);store=ws.Store(state)
                try:return store.get(args[1])
                finally:store.close()
            with patch.object(context,'capacity_available',return_value=True),patch.object(pool_control,'refresh_choice'),patch.object(pool_control,'official',side_effect=official):
                pool_control.advance(self.config,self.state,job['id'])
            self.assertEqual([company['task']],sent)
            self.assertEqual('received',pool_control.requests(self.state)[0]['phase'])

    def expired_registration(self):
        from teamkit.tickets import digest
        with patch.object(ws.CoreRuntime,'accept',return_value={'accepted':True,'terminal':'t1','native_pid':123,'turn':4}):
            ws.register(self.config,self.state,'alpha','t1',True)
        q=TicketStore(self.state);row=ws.rows(q.db)[0]
        with q.transaction():row['expires_at']=0;ws.save(q.db,row)
        q.close();self.native['turn']=5
        return row,digest(row)

    def test_expired_receipt_can_be_renewed_once_and_only_new_actor_receipt_is_valid(self):
        row,expected=self.expired_registration();messages=[]
        def accept(runtime,name,text,before_send=None):
            if before_send:before_send()
            messages.append(text)
            return dict(accepted=True,terminal='t1',native_pid=123,turn=5)
        with patch.object(ws.CoreRuntime,'accept',new=accept),patch('teamkit.core_runtime.require_managed'):
            result=self.call('pool','workspace-renew','--team','alpha','--terminal','t1',
                '--expected-pid','123','--expected-owner',row['actor']['owner'],'--registration-digest',expected,
                '--operator-approved','--reason','Recover the expired fixture registration after sandbox IPC failure.')
        self.assertTrue(result['renewal_sent']);self.assertFalse(result['product_request_sent'])
        self.assertEqual(len(messages),1);self.assertIn('on-request approval',messages[0])
        q=TicketStore(self.state);new=ws.rows(q.db)[0];q.close()
        self.assertEqual(new['actor']['owner'],row['actor']['owner'])
        self.assertNotEqual(new['nonce'],row['nonce'])
        self.assertEqual(result['approval_scope']['command'][-1],new['nonce'])
        self.assertEqual(new['renewal']['approval_scope'],result['approval_scope'])
        self.assertNotIn(row['nonce'],messages[0])
        self.assertEqual(new['history'][-1]['delivery'],row['delivery'])
        self.native['turn']=6
        with patch.dict(os.environ,{'HC_TERMINAL_ID':'t1'}):
            with self.assertRaisesRegex(ValueError,'expired'):ws.receipt(self.config,self.state,'t1',row['actor']['owner'],row['nonce'])
            ws.receipt(self.config,self.state,'t1',row['actor']['owner'],new['nonce'])
        self.assertEqual(ws.executors(self.state)[0]['owner'],row['actor']['owner'])

    def test_renewal_requires_exact_registration_digest_and_owner(self):
        row,expected=self.expired_registration()
        for pid,owner,key in ((124,row['actor']['owner'],expected),(123,'wrong',expected),(123,row['actor']['owner'],'changed')):
            with self.assertRaisesRegex(ValueError,'exact expired'):
                ws.renew_registration(self.config,self.state,'alpha','t1',pid,owner,key,True,'Recover fixture.')
            self.assertEqual(ws.registration_status(self.state,'t1')['record_digest'],expected)

    def test_renewal_rejects_unknown_delivery_and_product_attempts(self):
        from teamkit.tickets import digest
        row,expected=self.expired_registration();q=TicketStore(self.state)
        with q.transaction():row['delivery']['accepted']=False;ws.save(q.db,row)
        with self.assertRaisesRegex(ValueError,'unknown'):
            ws.renew_registration(self.config,self.state,'alpha','t1',123,row['actor']['owner'],digest(row),True,'Recover fixture.')
        with q.transaction():
            row['delivery']['accepted']=True;ws.save(q.db,row)
            q.db.execute('INSERT INTO operation_attempts VALUES (?,?,?)',('prior-attempt','prior-ticket',json.dumps({'executor':row['actor']})))
        with self.assertRaisesRegex(ValueError,'product attempt'):
            ws.renew_registration(self.config,self.state,'alpha','t1',123,row['actor']['owner'],digest(row),True,'Recover fixture.')
        q.close()

    def test_renewal_transport_failure_is_fenced_against_repetition(self):
        row,expected=self.expired_registration()
        with patch.object(ws.CoreRuntime,'accept',side_effect=NativeError('unconfirmed')):
            with self.assertRaises(NativeError):ws.renew_registration(self.config,self.state,'alpha','t1',123,row['actor']['owner'],expected,True,'Recover fixture.')
        with self.assertRaisesRegex(ValueError,'exact expired'):
            ws.renew_registration(self.config,self.state,'alpha','t1',123,row['actor']['owner'],ws.registration_status(self.state,'t1')['record_digest'],True,'Recover fixture.')
        self.assertEqual(ws.executors(self.state),[])

    def test_renewal_rejects_existing_execution_lease_or_actor_replacement(self):
        row,expected=self.expired_registration();q=TicketStore(self.state)
        ledger=ws.ResourceLedger(q.db)
        with q.transaction():ledger._write(q.db,dict(attempt='lease',ticket='fixture',execution_reserved=True,session={'terminal':'t1','pid':123,'owner':'other'}))
        with self.assertRaisesRegex(ValueError,'active execution lease'):
            ws.renew_registration(self.config,self.state,'alpha','t1',123,row['actor']['owner'],expected,True,'Recover fixture.')
        with q.transaction():q.db.execute('DELETE FROM resource_reservations')
        q.close();self.native['foreground']['pid']=124
        with self.assertRaises(NativeError):
            ws.renew_registration(self.config,self.state,'alpha','t1',123,row['actor']['owner'],expected,True,'Recover fixture.')
        self.assertEqual(ws.registration_status(self.state,'t1')['record_digest'],expected)
