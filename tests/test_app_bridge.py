import json
from pathlib import Path
from unittest.mock import patch
import test_workflow
import test_cli

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

    def test_view_is_read_only_and_never_calls_a_provider(self):
        self.setup_work();self.call('work','submit','W1')
        from teamkit.app_bridge import view
        before=self.state.read_bytes()
        with patch('teamkit.app_bridge.core_read',return_value={'terminals':[{'address':'t1','status':'idle','state':'live'}]}), patch('teamkit.providers.identity') as identity:
            value=view(self.config,self.state,self.binding_file())
        self.assertEqual(value['teams'][0]['app_state'],'idle')
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
        self.assertIn('feature.txt',doc['body'])

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
        self.assertEqual(result['teams'][0]['app_state'],'미확인')

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
