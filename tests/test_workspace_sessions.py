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
        self.value['session_pool']={'enabled':True,'bindings':str(self.root/'bindings.json'),'tasks':{'alpha':'L1'}}
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
