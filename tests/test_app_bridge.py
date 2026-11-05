import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch
import test_workflow
import test_cli
from test_app_feedback import FeedbackFixture

class AppBridgeTests(test_cli.Fixture):
    setup_work = test_workflow.WorkflowTests.setup_work
    approve = test_workflow.WorkflowTests.approve
    task = test_workflow.WorkflowTests.task
    def binding_file(self):
        self.value['runtime_backend']='core'
        self.config.write_text(json.dumps(self.value))
        p=self.root/'bindings.json'
        p.write_text(json.dumps({'version':1,'actions_enabled':True,'teams':{'alpha':{'label':'Alpha','terminal':'t1','space':'s1','legacy_task':'L1'}},'inventory':None}))
        return p

    def detail_task(self):
        self.setup_work();self.task()
        with test_cli.StoreContext(self.state) as store:
            task=store.get('T1')
            task.update(state='running',owner='owner',provider='codex',account='team',
                        model='fixture',effort='high',attempt_count=1,
                        runtime={'backend':'core','terminal':'t8','name':'tk-owner','native_pid':88})
            with store.transaction():store._save(task,'fixture_session')
        return {'address':'t8','state':'live','name':'tk-owner','status':{'app':'codex','state':'working'},
                'labels':{'teamkit.owner':'owner','teamkit.task':'T1','teamkit.name':'tk-owner'},
                'foreground':{'name':'codex','pid':88}}

    def test_work_detail_is_read_only_and_only_focuses_a_verified_session(self):
        native=self.detail_task();linked=self.binding_file()
        from teamkit.app_bridge import work_detail
        before=self.state.read_bytes()
        with patch('teamkit.app_bridge.core_read',return_value={'terminals':[{'address':'t8','state':'live','status':'working','name':'tk-owner','where':'Alpha / worker'}]}),patch('teamkit.app_bridge.session_read',return_value=native),patch('teamkit.providers.identity') as provider:
            result=work_detail(self.config,self.state,linked,'W1')
        self.assertEqual(result['tasks'][0]['goal'],(self.root/'goal.txt').read_text())
        self.assertEqual(result['tasks'][0]['native_state'],'working')
        self.assertTrue(result['tasks'][0]['can_focus'])
        self.assertEqual(result['tasks'][0]['session_location'],'Alpha / worker')
        self.assertEqual(before,self.state.read_bytes());provider.assert_not_called()

    def test_work_detail_rejects_replaced_native_owner_or_pid(self):
        native=self.detail_task();linked=self.binding_file()
        from teamkit.app_bridge import work_detail
        for change in ('owner','pid'):
            with self.subTest(change=change):
                import copy
                altered=copy.deepcopy(native)
                if change=='owner':altered['labels']['teamkit.owner']='replacement'
                else:altered['foreground']['pid']=99
                with patch('teamkit.app_bridge.core_read',return_value={'terminals':[{'address':'t8','state':'live','status':'working'}]}),patch('teamkit.app_bridge.session_read',return_value=altered):
                    result=work_detail(self.config,self.state,linked,'W1')
                self.assertFalse(result['tasks'][0]['can_focus'])
                self.assertEqual(result['tasks'][0]['native_state'],'unknown')

    def test_work_detail_native_failure_is_unknown_and_not_a_missing_session(self):
        self.detail_task();linked=self.binding_file()
        from teamkit.app_bridge import work_detail
        with patch('teamkit.app_bridge.core_read',side_effect=OSError('offline')):
            result=work_detail(self.config,self.state,linked,'W1')
        self.assertIsNone(result['tasks'][0]['session_present'])
        self.assertFalse(result['tasks'][0]['can_focus'])
        self.assertTrue(result['native_error'])

    def test_work_detail_does_not_guess_an_unregistered_work_or_use_tui(self):
        self.setup_work();linked=self.binding_file()
        from teamkit.app_bridge import work_detail
        with self.assertRaises(ValueError):work_detail(self.config,self.state,linked,'missing')
        self.value['runtime_backend']='tui';self.config.write_text(json.dumps(self.value))
        with self.assertRaises(ValueError):work_detail(self.config,self.state,linked,'W1')

    def test_view_is_read_only_and_never_calls_a_provider(self):
        self.setup_work();self.call('work','submit','W1')
        from teamkit.app_bridge import view
        before=self.state.read_bytes()
        with patch('teamkit.app_bridge.core_read',return_value={'terminals':[{'address':'t1','status':'idle','state':'live'}]}), patch('teamkit.providers.identity') as identity:
            value=view(self.config,self.state,self.binding_file())
        self.assertEqual(value['teams'][0]['app_state'],'unknown')
        self.assertFalse(value['teams'][0]['identity_verified'])
        self.assertEqual(value['works'][0]['percent'],None)
        self.assertFalse(value['works'][0]['approved'])
        self.assertEqual(before,self.state.read_bytes());identity.assert_not_called()

    def test_document_binds_revision_and_digest(self):
        self.setup_work();self.call('work','submit','W1')
        from teamkit.app_bridge import document_view
        value=document_view(self.config,self.state,'W1')
        self.assertEqual(value['revision'],1)
        self.assertEqual(len(value['digest']),64)
        self.assertIn('One approved design',value['body'])

    def test_ui_approval_rejects_stale_digest_without_a_write(self):
        self.setup_work();self.call('work','submit','W1')
        from teamkit.app_bridge import approve
        before=self.state.read_bytes()
        with self.assertRaises(ValueError):approve(self.config,self.state,self.binding_file(),'W1',1,'0'*64,True)
        self.assertEqual(before,self.state.read_bytes())
        self.assertEqual(self.call('work','show','W1')['phase'],'awaiting_approval')

    def test_ui_approval_requires_confirmation_and_enabled_actions(self):
        self.setup_work();self.call('work','submit','W1')
        from teamkit.app_bridge import approve,document_view
        doc=document_view(self.config,self.state,'W1');bindings=self.binding_file()
        with self.assertRaises(ValueError):approve(self.config,self.state,bindings,'W1',1,doc['digest'],False)
        data=json.loads(bindings.read_text());data['actions_enabled']=False;bindings.write_text(json.dumps(data))
        with self.assertRaises(ValueError):approve(self.config,self.state,bindings,'W1',1,doc['digest'],True)
        self.assertEqual(self.call('work','show','W1')['phase'],'awaiting_approval')

    def test_approval_passes_exact_selected_digest_to_transaction(self):
        self.setup_work();self.call('work','submit','W1')
        from teamkit.app_bridge import approve,document_view
        doc=document_view(self.config,self.state,'W1')
        value=approve(self.config,self.state,self.binding_file(),'W1',1,doc['digest'],True)
        self.assertIn('message',value)
        stored=self.call('work','show','W1')
        self.assertEqual(stored['approval']['digest'],doc['digest'])
        self.assertEqual(stored['phase'],'approved')

    def test_resume_does_not_send_to_tui_owner(self):
        self.setup_work();self.approve();self.task('T1','leader')
        from teamkit.app_bridge import resume
        with patch('teamkit.app_bridge.start_resume') as start:
            with self.assertRaises(ValueError):resume(self.config,self.state,self.binding_file(),'W1',1,'0'*64,'T1','stale',True)
            start.assert_not_called()

    def test_approved_document_remains_readable_without_reapproval(self):
        self.setup_work();self.approve()
        from teamkit.app_bridge import document_view
        doc=document_view(self.config,self.state,'W1')
        self.assertEqual(doc['phase'],'approved')
        self.assertFalse(doc['can_approve'])
        self.assertIn('feature.txt',doc['contract']['allowed_paths'])
        self.assertEqual(doc['body'],doc['source_body'])

    def test_tui_configuration_cannot_approve_through_app(self):
        self.setup_work();self.call('work','submit','W1')
        from teamkit.app_bridge import approve,document_view
        doc=document_view(self.config,self.state,'W1');path=self.binding_file()
        self.value.pop('runtime_backend');self.config.write_text(json.dumps(self.value))
        with self.assertRaises(ValueError):approve(self.config,self.state,path,'W1',1,doc['digest'],True)
        self.assertEqual(self.call('work','show','W1')['phase'],'awaiting_approval')

    def test_pending_resume_is_not_enqueued_twice(self):
        self.setup_work()
        from teamkit.app_bridge import start_resume
        old=self.state.parent/'app-actions'/'pending';old.mkdir(parents=True)
        (old/'request.json').write_text(json.dumps({'task':'T1'}))
        with patch('teamkit.app_bridge.subprocess.run') as native:
            with self.assertRaises(ValueError):start_resume(self.config,self.state,self.binding_file(),'W1',1,'a'*64,'T1','owner')
            native.assert_not_called()

    def test_missing_native_status_disables_actions_without_claiming_idle(self):
        self.setup_work()
        from teamkit.app_bridge import view
        with patch('teamkit.app_bridge.core_read',side_effect=ValueError('unavailable')):
            result=view(self.config,self.state,self.binding_file())
        self.assertFalse(result['actions_enabled'])
        self.assertEqual(result['teams'][0]['app_state'],'unknown')

    def test_approval_transaction_rejects_selected_revision(self):
        self.setup_work();self.call('work','submit','W1')
        with self.assertRaises(ValueError):
            self.call('work','approve','W1','--user-approved','--approval-reference','selected old version',
                      '--expected-revision','2','--expected-digest','0'*64)
        self.assertEqual(self.call('work','show','W1')['phase'],'awaiting_approval')

    def test_stale_owner_is_rejected_before_native_or_wake(self):
        self.create();before=self.call('events','T1')
        with patch('teamkit.core_runtime.require_managed') as native:
            with self.assertRaises(ValueError):
                self.call('tell','T1','--message-file',str(self.root/'goal.txt'),'--expected-owner','previous')
            native.assert_not_called()
        self.assertEqual(before,self.call('events','T1'))

    def test_stale_attempt_is_rejected_before_dispatch_admission(self):
        self.create();before=self.call('events','T1')
        with patch('teamkit.providers.identity') as identity:
            with self.assertRaises(ValueError):
                self.call('dispatch','T1','--expected-attempt','7')
            identity.assert_not_called()
        self.assertEqual(before,self.call('events','T1'))


REAL_RUN=subprocess.run
NATIVE={'address':'t1','state':'live','name':'tk-leader','status':{'app':'codex','state':'idle'},'turn':3,
        'labels':{'teamkit.owner':'leader-owner','teamkit.task':'L1','teamkit.name':'tk-leader'},'foreground':{'name':'codex','pid':77}}
OPTIONS=[{'option_id':'proceed','label':'이대로 진행'},{'option_id':'narrow','label':'범위 줄이기'}]


class DeliveryBridgeTests(FeedbackFixture):
    """Direct delivery through the real tell path; only hc itself is faked."""
    def setUp(self):
        super().setUp();self.setup_work();self.call('work','submit','W1');self.sent=[];self.native=copy.deepcopy(NATIVE);self.send_error=None
        self.call('create','L1','--team','alpha','--root',str(self.repo),'--goal-file',str(self.root/'goal.txt'))
        self.leader()
    def leader(self,state='ready',**runtime):
        with test_cli.StoreContext(self.state) as store:
            task=store.get('L1')
            task.update(role='leader',work_id=None,state=state,owner='leader-owner',provider='codex',account='team',model='fixture',
                        effort='high',attempt_count=1,runtime={'backend':'core','terminal':'t1','name':'tk-leader','native_pid':77,
                                                               'identity_checked':True,'identity_source':'host-fresh-provider',**runtime})
            with store.transaction():store._save(task,'fixture_leader')
    def hc(self):
        def call(*args, **options):
            if args[0]=='get':
                if args[1]=='t9':return {'address':'t9','state':'live'}
                if args[1]=='t1':return copy.deepcopy(self.native)
            if args[0]=='send':
                self.sent.append(args)
                if self.send_error:raise self.send_error
                return {'started':True,'wait':{'terminal':'t1','ended':True,'reason':'status','status':'idle','turn':4}}
            raise AssertionError(args)
        return patch('teamkit.core_runtime._call',side_effect=call)
    def fields(self,**extra):
        card=self.detail('W1','work')
        return {'client_request_id':'req-0001-aaaa','kind':'work','work_id':'W1','card_revision':card['revision'],
                'document_revision':card['document_revision'],'document_digest':card['document_digest'],
                'choice':'changes','comment':'범위를 줄여 주세요.',**extra}
    def controller(self,error=None):
        """Intercept only `hc run`; git and every other subprocess stay real because card digests depend on them."""
        self.runs=[]
        def fake(command,*args,**kwargs):
            if command and command[0]=='hc':
                self.runs.append(command)
                if error:raise error
                return subprocess.CompletedProcess(command,0,stdout='{"address":"t7"}',stderr='')
            return REAL_RUN(command,*args,**kwargs)
        return patch('teamkit.app_bridge.subprocess.run',side_effect=fake)
    def submit(self,**extra):
        from teamkit.app_bridge import card_send
        with self.hc(),self.controller():
            result=card_send(self.config,self.state,self.links,json.dumps(self.fields(**extra)))
        return result,self.runs
    def job(self,rid='req-0001-aaaa'):
        from teamkit.app_bridge import card_send_job
        with self.hc(),patch.dict(os.environ,{'HC_TERMINAL_ID':'t9'}):
            return card_send_job(self.config,self.state,self.links,self.feedback_root/'delivery'/rid/'request.json')
    def summary(self,rid='req-0001-aaaa'):
        from teamkit.app_bridge import card_send_status
        return card_send_status(self.state,rid)['request']

    # destination ---------------------------------------------------------
    def test_destination_is_the_one_official_ready_leader(self):
        from teamkit.app_bridge import destination
        with self.hc():dest=destination(self.config,self.state,self.links,'work','W1')
        self.assertEqual((dest['task'],dest['owner'],dest['attempt'],dest['terminal'],dest['native_pid']),('L1','leader-owner',1,'t1',77))

    def test_destination_refuses_every_unverified_recipient(self):
        from teamkit.app_bridge import destination,Refusal
        cases={'working':lambda n:n['status'].update(state='working'),'blocked':lambda n:n['status'].update(state='blocked'),
               'owner label':lambda n:n['labels'].update({'teamkit.owner':'other'}),'pid':lambda n:n['foreground'].update(pid=99),
               'renamed':lambda n:n.update(name='replacement'),'other app':lambda n:n['status'].update(app='claude')}
        for name,edit in cases.items():
            with self.subTest(name),self.hc():
                self.native=copy.deepcopy(NATIVE);edit(self.native)
                with self.assertRaises(Refusal):destination(self.config,self.state,self.links,'work','W1')
        self.native=copy.deepcopy(NATIVE)
        with self.hc():
            with self.assertRaisesRegex(Refusal,'공식 담당 작업 연결'):destination(self.config,self.state,self.links,'inventory','W47')
            for state in ('running','uncertain','queued'):
                self.leader(state);self.assertRaises(Refusal,destination,self.config,self.state,self.links,'work','W1')
            self.leader('ready',retire_requested=True);self.assertRaises(Refusal,destination,self.config,self.state,self.links,'work','W1')
            self.leader('ready',identity_checked=False);self.assertRaises(Refusal,destination,self.config,self.state,self.links,'work','W1')
            self.leader();self.linked['actions_enabled']=False;self.links.write_text(json.dumps(self.linked))
            self.assertRaises(Refusal,destination,self.config,self.state,self.links,'work','W1')

    def test_destination_never_guesses_between_two_leaders_or_a_different_terminal(self):
        from teamkit.app_bridge import destination,Refusal
        self.call('create','L2','--team','alpha','--root',str(self.repo),'--goal-file',str(self.root/'goal.txt'))
        with test_cli.StoreContext(self.state) as store:
            task=store.get('L2');task.update(role='leader',work_id=None,state='ready',owner='second',attempt_count=1,
                runtime={'backend':'core','terminal':'t1','name':'tk-second','native_pid':78,'identity_checked':True})
            with store.transaction():store._save(task,'fixture_leader')
        with self.hc(),self.assertRaisesRegex(Refusal,'하나가 아닙니다'):destination(self.config,self.state,self.links,'work','W1')
        self.linked['teams']['alpha']['terminal']='t5';self.links.write_text(json.dumps(self.linked))
        with self.hc(),self.assertRaises(Refusal):destination(self.config,self.state,self.links,'work','W1')

    # send + job ----------------------------------------------------------
    def test_send_records_a_durable_pending_request_and_starts_one_controller(self):
        result,started=self.submit()
        self.assertEqual(result['request']['state'],'pending');self.assertEqual(len(started),1)
        command=started[0];self.assertEqual(command[:2],['hc','run']);self.assertIn('card-send-job',command)
        stored=json.loads((self.feedback_root/'delivery/req-0001-aaaa/request.json').read_text())
        self.assertEqual(stored['destination']['task'],'L1');self.assertEqual(self.sent,[])

    def test_same_id_is_idempotent_and_different_payload_is_rejected_without_a_second_controller(self):
        self.submit()
        again,started=self.submit();self.assertEqual(len(started),0);self.assertIn('다시 보내지 않고',again['message'])
        with self.assertRaisesRegex(ValueError,'다른 내용'):self.submit(comment='전혀 다른 의견')
        self.assertEqual(self.sent,[])

    def test_job_delivers_exactly_once_through_official_tell_and_reports_transport_only(self):
        self.submit();self.job();seen=self.summary()
        self.assertEqual((seen['state'],seen['label']),('sent','전송 확인됨'));self.assertIn('내용 수신·실행·승인·완료는 별도',seen['reason'])
        self.assertEqual(len(self.sent),1)
        text=self.sent[0][-1]
        for expected in ('req-0001-aaaa','승인이나 구현 허가가 아닙니다','범위를 줄여 주세요.','수정이 필요합니다','L1','owner leader-owner'):
            if expected!='L1' and expected!='owner leader-owner':self.assertIn(expected,text)
        self.assertEqual((self.feedback_root/'delivery/req-0001-aaaa/message.txt').stat().st_mode&0o777,0o600)
        with self.assertRaises(ValueError):self.job()  # restart or double start cannot send again
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.call('show','L1')['state'],'running')

    def test_owner_change_after_send_is_unknown_never_not_sent(self):
        from teamkit.app_bridge import card_send_job
        self.submit()
        with self.hc() as boundary:
            original=boundary.side_effect
            def changed(*args, **options):
                result=original(*args, **options)
                if args[0]=='send':
                    result['wait'].update(ended=False,reason='message',status='working')
                    self.native['labels']['teamkit.owner']='replacement'
                return result
            boundary.side_effect=changed
            with patch.dict(os.environ,{'HC_TERMINAL_ID':'t9'}):
                card_send_job(self.config,self.state,self.links,self.feedback_root/'delivery/req-0001-aaaa/request.json')
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.summary()['state'],'unknown')

    def test_typed_answer_text_names_question_option_and_other_text(self):
        from teamkit.app_feedback import question_set
        author={'task':'L1','owner':'leader-owner','attempt':1,'terminal':'t1'}
        question_set(self.config,self.state,self.links,'work','W1',author,{'prompt':'설계 범위가 맞나요?','options':OPTIONS},0)
        q=self.detail('W1','work')['question']
        self.submit(question_id=q['question_id'],question_revision=1,option_id='other',other_text='가운데로 합시다',choice=None,comment='');
        self.job();text=self.sent[0][-1]
        for expected in ('설계 범위가 맞나요?','직접 입력','가운데로 합시다',q['question_id']):self.assertIn(expected,text)

    def test_refusal_before_send_is_a_durable_not_sent_and_does_not_start_a_controller(self):
        self.native['status']['state']='working'
        result,started=self.submit()
        self.assertEqual(result['request']['state'],'not_sent');self.assertEqual(len(started),0);self.assertEqual(self.sent,[])
        inventory=self.detail()
        card=inventory['revision']
        from teamkit.app_bridge import card_send
        with self.hc(),self.controller():
            out=card_send(self.config,self.state,self.links,json.dumps({'client_request_id':'req-0002-bbbb','kind':'inventory','work_id':'W47',
                'card_revision':card,'document_revision':0,'document_digest':inventory['document_digest'],'choice':'changes','comment':'확인 필요'}))
        self.assertEqual(out['request']['state'],'not_sent');self.assertIn('공식 담당 작업 연결',out['request']['reason'])
        self.assertEqual(self.runs,[])

    def strict_controller(self):
        """Apply the installed hc's own name rule (1-32 of a-z 0-9 - _, starting alphanumeric, not shaped like an address)."""
        import re
        self.runs=[]
        def fake(command,*args,**kwargs):
            if command and command[0]=='hc':
                self.runs.append(command)
                name=command[command.index('--name')+1]
                if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}',name) or re.fullmatch(r't[0-9a-z]+',name):
                    raise subprocess.CalledProcessError(2,command,stderr='{"error":{"kind":"invalid_params","message":"bad name \\"'+name+'\\""}}')
                return subprocess.CompletedProcess(command,0,stdout='{"address":"t7"}',stderr='')
            return REAL_RUN(command,*args,**kwargs)
        return patch('teamkit.app_bridge.subprocess.run',side_effect=fake)

    def test_controller_name_follows_hc_rule_for_every_allowed_request_id(self):
        from teamkit.app_bridge import card_send
        names=set()
        # Uppercase is a valid request ID; the first 12 characters of two IDs may also be identical.
        for rid in ('GUI-TEST-20261101-A1','gui-mo7k2x9a-abcd1234','gui-mo7k2x9a-efgh5678'):
            with self.hc(),self.strict_controller():
                out=card_send(self.config,self.state,self.links,json.dumps(self.fields(client_request_id=rid,comment='이름 규칙 '+rid)))
            self.assertEqual(out['request']['state'],'pending',rid)
            names.add(self.runs[0][self.runs[0].index('--name')+1])
            self.assertEqual(self.sent,[])
        self.assertEqual(len(names),3)

    def test_controller_start_failure_keeps_a_bounded_hc_reason_and_never_resends(self):
        error=subprocess.CalledProcessError(2,['hc'],stderr='bad name '+'x'*5000)
        with self.hc(),self.controller(error):
            from teamkit.app_bridge import card_send
            out=card_send(self.config,self.state,self.links,json.dumps(self.fields()))
        self.assertEqual(out['request']['state'],'unknown');self.assertEqual(self.sent,[])
        reason=out['request']['reason'];self.assertIn('bad name',reason);self.assertLess(len(reason),600)
        with self.hc(),self.controller():
            again=card_send(self.config,self.state,self.links,json.dumps(self.fields()))
        self.assertEqual(again['request']['state'],'unknown');self.assertEqual(self.runs,[]);self.assertEqual(self.sent,[])

    def test_stale_card_is_rejected_before_any_record_exists(self):
        fields=self.fields();(self.root/'private-plans/W1/r1/plan.md').write_text('설계가 바뀌었습니다')
        from teamkit.app_bridge import card_send
        with self.hc(),self.controller(),self.assertRaisesRegex(ValueError,'바뀌었습니다'):
            card_send(self.config,self.state,self.links,json.dumps(fields))
        self.assertEqual(self.runs,[]);self.assertFalse((self.feedback_root/'delivery').exists())

    def edit_leader(self,**fields):
        with test_cli.StoreContext(self.state) as store:
            task=store.get('L1');task.update(fields)
            with store.transaction():store._save(task,'fixture_edit')

    def test_job_rechecks_card_and_recipient_right_before_sending(self):
        from teamkit.app_bridge import card_send
        plan=self.root/'private-plans/W1/r1/plan.md';original=plan.read_text()
        changes={'owner':lambda:self.edit_leader(owner='replacement-owner'),
                 'plan':lambda:plan.write_text('바뀐 설계'),
                 'busy':lambda:self.native['status'].update(state='working'),
                 'attempt':lambda:self.edit_leader(attempt_count=2),
                 'pid':lambda:self.edit_leader(runtime={**self.call('show','L1')['runtime'],'native_pid':99})}
        for index,(name,change) in enumerate(changes.items()):
            with self.subTest(name):
                rid='req-%04d-race'%index;self.sent.clear();self.native=copy.deepcopy(NATIVE);plan.write_text(original);self.leader()
                with self.hc(),self.controller():
                    card_send(self.config,self.state,self.links,json.dumps(self.fields(client_request_id=rid,comment='변경 '+name)))
                change();self.job(rid)
                self.assertEqual(self.summary(rid)['state'],'not_sent',name);self.assertEqual(self.sent,[],name)

    def test_existing_tell_gates_still_apply_to_linked_design_leader(self):
        with test_cli.StoreContext(self.state) as store:
            task=store.get('L1');task.update(work_id='W1',classification={'kind':'general','risks':[]},design_revision=1)
            with store.transaction():store._save(task,'fixture_link')
        self.submit();self.job();seen=self.summary()
        self.assertEqual(seen['state'],'not_sent');self.assertIn('기존 전달 조건이 거절했습니다',seen['reason']);self.assertEqual(self.sent,[])

    def test_park_race_after_send_failure_remains_unknown_and_blocks_new_id(self):
        from teamkit.store import Store
        from teamkit.runtime import RuntimeError as KitError
        self.submit()
        self.send_error = KitError('Core outcome unknown; inspect before retry')
        note = Store.note

        def late_park(store, task, owner, detail):
            store.park(task, owner)
            return note(store, task, owner, detail)

        with patch.object(Store, 'note', late_park):
            self.job()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.summary()['state'], 'unknown')
        with self.assertRaisesRegex(ValueError, '이전 전달 결과'):
            self.submit(client_request_id='req-0002-bbbb')
        self.assertEqual(len(self.sent), 1)

    def test_timeout_or_unconfirmed_wait_is_unknown_and_never_resent(self):
        from teamkit.runtime import RuntimeError as KitError
        for index,error in enumerate((KitError('Core outcome unknown; inspect before retry'),KitError('Core command failed; inspect local runtime before retry'),OSError('pipe'))):
            with self.subTest(error=str(error)):
                rid='req-%04d-unkn'%index;self.sent.clear();self.native=copy.deepcopy(NATIVE);self.leader()
                from teamkit.app_bridge import card_send
                with self.hc(),self.controller():card_send(self.config,self.state,self.links,json.dumps(self.fields(client_request_id=rid,comment='시간 초과 '+str(index))))
                self.send_error=error
                try:self.job(rid)
                except BaseException:pass
                self.send_error=None
                self.assertEqual(self.summary(rid)['state'],'unknown');self.assertEqual(len(self.sent),1)
                with self.assertRaises(ValueError):self.job(rid)
                self.assertEqual(len(self.sent),1)
                with self.hc():
                    again,_=self.submit_again(rid,index)
                self.assertEqual(len(self.sent),1)
    def submit_again(self,rid,index):
        from teamkit.app_bridge import card_send
        with self.controller():
            return card_send(self.config,self.state,self.links,json.dumps(self.fields(client_request_id=rid,comment='시간 초과 '+str(index)))),self.runs

    def test_result_write_failure_after_send_leaves_sending_and_shows_unknown(self):
        self.submit()
        real=__import__('teamkit.app_delivery',fromlist=['record']).record
        def fail(state,rid,new,detail):
            if new=='sent':raise OSError('disk full')
            return real(state,rid,new,detail)
        with patch('teamkit.app_delivery.record',side_effect=fail):
            with self.assertRaises(OSError):self.job()
        self.assertEqual(len(self.sent),1);seen=self.summary();self.assertEqual(seen['state'],'sending')
        with self.assertRaises(ValueError):self.job()
        self.assertEqual(len(self.sent),1)
        from teamkit.app_delivery import summary
        self.assertEqual(summary(self.feedback_root/'delivery/req-0001-aaaa',now=time.time()+999)['effective_state'],'unknown')

    def test_job_rejects_a_request_path_that_is_not_the_recorded_one(self):
        from teamkit.app_bridge import card_send_job
        self.submit();other=self.root/'forged'/'request.json';other.parent.mkdir();other.write_text('{}')
        with self.hc(),patch.dict(os.environ,{'HC_TERMINAL_ID':'t9'}),self.assertRaises(ValueError):card_send_job(self.config,self.state,self.links,other)
        self.assertEqual(self.summary()['state'],'pending')

    def test_controller_start_failure_is_unknown_not_a_retry(self):
        from teamkit.app_bridge import card_send
        with self.hc(),self.controller(subprocess.TimeoutExpired('hc',10)):
            result=card_send(self.config,self.state,self.links,json.dumps(self.fields()))
        self.assertEqual(result['request']['state'],'unknown');self.assertEqual(len(self.runs),1)
        with self.assertRaises(ValueError):self.job()

    # authoring -----------------------------------------------------------
    def ask(self,**override):
        from teamkit.app_bridge import card_question_set
        file=self.root/'question.json';file.write_text(json.dumps({'prompt':'설계 범위가 맞나요?','options':OPTIONS}))
        args=dict(kind='work',ident='W1',task='L1',owner='leader-owner',attempt=1,question_file=file,expected=0);args.update(override)
        with self.hc(),patch.dict(os.environ,{'HC_TERMINAL_ID':'t1'}):
            return card_question_set(self.config,self.state,self.links,args['kind'],args['ident'],args['task'],args['owner'],args['attempt'],args['question_file'],args['expected'])

    def test_only_the_current_leader_in_its_own_terminal_can_author_a_question(self):
        self.leader('running');saved=self.ask()['question'];self.assertEqual(saved['author_task_id'],'L1')
        self.assertEqual(self.detail('W1','work')['question']['question_id'],saved['question_id'])
        for override in ({'owner':'someone-else'},{'attempt':2},{'task':'missing'},{'expected':5}):
            with self.subTest(override=override),self.assertRaises(ValueError):self.ask(**override)
        from teamkit.app_bridge import card_question_set
        with self.hc(),patch.dict(os.environ,{'HC_TERMINAL_ID':'t9'}):
            with self.assertRaisesRegex(ValueError,'자신의 관리 터미널'):
                card_question_set(self.config,self.state,self.links,'work','W1','L1','leader-owner',1,self.root/'question.json',1)
        with self.hc(),patch.dict(os.environ,{},clear=True):
            with self.assertRaises(Exception):card_question_set(self.config,self.state,self.links,'work','W1','L1','leader-owner',1,self.root/'question.json',1)
        self.native['foreground']['pid']=1
        with self.assertRaises(Exception):self.ask(expected=1)

    def test_question_file_must_be_a_small_regular_file(self):
        target=self.root/'real.json';target.write_text('{}');link=self.root/'link.json';link.symlink_to(target)
        with self.assertRaises(ValueError):self.ask(question_file=link)
        big=self.root/'big.json';big.write_text(' '*20000)
        with self.assertRaises(ValueError):self.ask(question_file=big)

    def test_inventory_question_can_be_authored_for_the_bound_leader_but_never_delivered(self):
        file=self.root/'q.json';file.write_text(json.dumps({'prompt':'화면에서 저장이 보였나요?','options':OPTIONS}))
        from teamkit.app_bridge import card_question_set
        with self.hc(),patch.dict(os.environ,{'HC_TERMINAL_ID':'t1'}):
            saved=card_question_set(self.config,self.state,self.links,'inventory','W47','L1','leader-owner',1,file,0)['question']
        self.assertEqual(self.detail()['question']['question_id'],saved['question_id']);self.assertTrue(self.detail()['can_answer'])

    # entry points ----------------------------------------------------------
    def cli(self,*argv,env=None):
        import io
        from contextlib import redirect_stdout,redirect_stderr
        from teamkit.app_bridge import main
        out,err=io.StringIO(),io.StringIO()
        with patch.object(sys,'argv',['teamkit_app.py','--config',str(self.config),'--state',str(self.state),'--bindings',str(self.links),*argv]),\
                patch.dict(os.environ,env or {}),redirect_stdout(out),redirect_stderr(err),self.hc(),self.controller():
            code=main()
        return code,(json.loads(out.getvalue()) if out.getvalue() else None),(json.loads(err.getvalue()) if err.getvalue() else None)

    def test_consumed_cli_runs_question_authoring_detail_send_and_status(self):
        file=self.root/'cli-question.json';file.write_text(json.dumps({'prompt':'CLI 질문입니까?','options':OPTIONS}))
        code,result,_=self.cli('card-question-set','--kind','work','--work','W1','--task','L1','--owner','leader-owner','--attempt','1',
                               '--question-file',str(file),'--expected-question-revision','0',env={'HC_TERMINAL_ID':'t1'})
        self.assertEqual(code,0);self.assertEqual(result['question']['question_revision'],1)
        code,card,_=self.cli('card-detail','--kind','work','--work','W1')
        self.assertEqual((code,card['question_state']['state']),(0,'ready'))
        fields=self.fields(question_id=card['question']['question_id'],question_revision=1,option_id='proceed',choice=None,comment='')
        code,sent,_=self.cli('card-send','--send-json',json.dumps(fields))
        self.assertEqual((code,sent['request']['state']),(0,'pending'));self.assertEqual(len(self.runs),1)
        code,status,_=self.cli('card-send-status','--request-id',fields['client_request_id'])
        self.assertEqual((code,status['request']['request_id']),(0,fields['client_request_id']))
        code,detail,_=self.cli('card-detail','--kind','work','--work','W1');self.assertEqual(detail['deliveries'][0]['request_id'],fields['client_request_id'])
        code,_,error=self.cli('card-send-status','--request-id','req-none-0000');self.assertEqual(code,1);self.assertIn('error',error)
        code,_,error=self.cli('card-send','--send-json','{not json');self.assertEqual(code,1)
        code,_,error=self.cli('card-question-set','--kind','work','--work','W1','--task','L1','--owner','leader-owner','--attempt','1',
                              '--question-file',str(file),'--expected-question-revision','1',env={'HC_TERMINAL_ID':'t9'});self.assertEqual(code,1)

    def test_concurrent_identical_sends_start_one_controller(self):
        import threading
        from teamkit.app_bridge import card_send
        body=json.dumps(self.fields());outcomes=[]
        def go():
            try:outcomes.append(card_send(self.config,self.state,self.links,body))
            except ValueError as error:outcomes.append(error)
        with self.hc(),self.controller():
            threads=[threading.Thread(target=go) for _ in range(6)];[t.start() for t in threads];[t.join() for t in threads]
        self.assertEqual(len(self.runs),1);self.assertEqual(len(outcomes),6)
        folder=self.feedback_root/'delivery/req-0001-aaaa';from teamkit.app_delivery import read_events
        self.assertEqual([e['state'] for e in read_events(folder)],['pending'])
        self.assertEqual(len([d for d in (self.feedback_root/'delivery').iterdir() if not d.name.startswith('.')]),1)
