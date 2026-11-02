import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from teamkit import app_coordinator as coordinator
from teamkit.__main__ import parser, run
from teamkit.config import binding, profile
from teamkit.runtime import RuntimeError as KitError

READ_ENVIRONMENT = coordinator._rebind_environment


class CoordinatorPolicyRebindTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.state = self.root/'state.sqlite'; self.state.touch()
        self.config = self.root/'config.json'; self.bindings = self.root/'bindings.json'
        self.owner = 'a'*32; self.sid = '11111111-2222-4333-8444-555555555555'
        self.value = {'version':1,'runtime_backend':'core','teams':{},
            'profiles':{'codex':{a:{'home':str(self.root/a),'model':'fixture-model','effort':'low',
                'identity':{'email':a+'@example.invalid'}} for a in ('team','personal')}},
            'coordinator':{'provider':'codex','model':'fixture-model','effort':'low','root':str(self.root)},
            'private_plan_root':str(self.root),'context_bindings':str(self.bindings)}
        self.config.write_text(json.dumps(self.value))
        self.bindings.write_text(json.dumps({'coordinator':{'terminal':'tfixture'}}))
        self.record = {'phase':'ready','nonce':self.owner,'native_pid':42,'terminal':'tfixture',
            'context_session_id':self.sid,'account':'personal','model':'fixture-model','effort':'low',
            'space':'spfixture','tab':'tbfixture','reason':'Existing coordinator, preserve this history.',
            'profile_binding':binding(profile(self.value,'codex','personal',self.value['coordinator']))}
        coordinator.save(coordinator.record_path(self.state),self.record)
        self.name = 'gui-coordinator-'+coordinator.scope(self.state)
        self.native = {'address':'tfixture','name':self.name,'state':'live','status':{'app':'codex','state':'idle'},
            'turn':5,'labels':{'teamkit.owner':self.owner,'teamkit.task':'GUI-COORDINATOR','teamkit.name':self.name,
                'teamkit.kind':'coordinator','teamkit.app-scope':coordinator.scope(self.state),
                'herdr.space':'spfixture','herdr.tab':'tbfixture'},
            'foreground':{'name':'codex','pid':42,'cwd':str(self.root),
                          'argv':['codex','resume',self.sid,'--no-daemon','--no-alt-screen']}}
        self.screen = {'complete':True,'text':'›\nContext 80% left\n'}
        self.layout = {'spaces':[{'id':'spfixture','tabs':[{'id':'tbfixture','terminals':[{'terminal':'tfixture'}]}]}]}
        self.calls = []; self.old_gone = False
        def hc(*argv,**kwargs):
            self.calls.append(argv)
            if argv[0]=='get' and argv[1]=='tfixture': return copy.deepcopy(self.native)
            if argv[0]=='read': return copy.deepcopy(self.screen)
            raise AssertionError('HC mutation or unexpected observation forbidden')
        def local(argv,**kwargs):
            if argv==['herdr-app','ls']:
                return SimpleNamespace(stdout=json.dumps(self.layout),returncode=0)
            raise AssertionError('Real process/auth execution forbidden')
        def kill(pid,signal):
            self.assertEqual(0,signal)
            if pid==42 and self.old_gone: raise ProcessLookupError()
            if pid not in (42,43,44): raise AssertionError('Unexpected OS process query')
        self.env = {'HOME':str(self.root),'CODEX_HOME':str(self.root/'personal'),'TEAMKIT_CONFIG':str(self.config),
                    'TEAMKIT_STATE':str(self.state),'TEAMKIT_GUI_COORDINATOR':self.owner}
        for target,kwargs in [
            ('teamkit.app_coordinator.require_managed',{'return_value':'tcontroller'}),
            ('teamkit.app_coordinator._call',{'side_effect':hc}),('teamkit.core_runtime._call',{'side_effect':hc}),
            ('subprocess.run',{'side_effect':local}),('subprocess.Popen',{'side_effect':AssertionError('Real process forbidden')}),
            ('teamkit.providers.identity',{'side_effect':AssertionError('Auth forbidden')}),
            ('teamkit.app_coordinator.os.kill',{'side_effect':kill}),
            ('teamkit.app_coordinator._rebind_environment',{'return_value':self.env,'create':True})]:
            p=patch(target,**kwargs);p.start();self.addCleanup(p.stop)
        self.checkpoint=self.root/'actor-checkpoint.json'
        self.packet={'task':'GUI-COORDINATOR','owner':self.owner,'terminal':'tfixture','native_pid':42,
            'session_id':self.sid,'config_sha256':hashlib.sha256(self.config.read_bytes()).hexdigest(),
            'native_turn':5,
            'profile_binding':self.record['profile_binding'],'writer_stopped':True,'background_writers':[],
            'verification':['I inspected my own background jobs and stopped all writers.'], 'observed_at':time.time()}
        self.checkpoint.write_text(json.dumps(self.packet))

    def prepare(self):
        return coordinator.prepare_rebind(self.config,self.state,self.checkpoint,self.owner,42,self.sid)

    def resumed(self,prepared):
        self.old_gone=True
        argv=prepared['resume_argv'];self.native['foreground']['pid']=43
        self.native['foreground']['argv']=argv[argv.index('codex'):]

    def complete(self,prepared,pid=43):
        return coordinator.complete_rebind(self.config,self.state,prepared['id'],self.owner,pid)

    def test_prepare_only_records_intent_and_returns_same_session_explicit_policy(self):
        prepared=self.prepare()
        self.assertEqual('prepared',prepared['phase'])
        argv=prepared['resume_argv']
        self.assertEqual(['resume',self.sid],argv[argv.index('codex')+1:argv.index('codex')+3])
        self.assertIn('model_reasoning_effort="low"',argv)
        self.assertIn('--model',argv)
        current=coordinator.read_record(self.state)
        self.assertEqual('rebinding',current['phase']);self.assertEqual(42,current['native_pid'])
        for key in ('nonce','space','tab','terminal','context_session_id','account','profile_binding','model','effort'):
            self.assertEqual(self.record[key],current[key])
        before=coordinator.record_path(self.state).read_bytes()
        self.assertEqual(prepared,self.prepare())
        self.assertEqual(before,coordinator.record_path(self.state).read_bytes())
        self.assertTrue(all(x[0] in ('get','read') for x in self.calls))

    def test_complete_rebinds_only_pid_and_preserves_history_idempotently(self):
        prepared=self.prepare();self.resumed(prepared)
        completed=self.complete(prepared)
        self.assertEqual('completed',completed['phase'])
        current=coordinator.read_record(self.state)
        self.assertEqual(43,current['native_pid']);self.assertEqual('ready',current['phase'])
        for key in ('nonce','space','tab','terminal','context_session_id','account','profile_binding','model','effort','reason'):
            self.assertEqual(self.record[key],current[key])
        self.assertEqual(1,len(current['policy_rebind_history']))
        before=coordinator.record_path(self.state).read_bytes()
        self.assertEqual(completed,self.complete(prepared))
        self.assertEqual(before,coordinator.record_path(self.state).read_bytes())
        with self.assertRaises(ValueError):self.complete(prepared,pid=44)

    def test_prepare_preserves_draft_or_unproven_writers_without_intent(self):
        original=coordinator.record_path(self.state).read_bytes()
        for packet in [dict(self.packet,writer_stopped='false'),dict(self.packet,background_writers=[123]),
                       dict(self.packet,verification=[]),dict(self.packet,session_id='other'),
                       dict(self.packet,profile_binding='changed')]:
            self.checkpoint.write_text(json.dumps(packet))
            with self.assertRaises(ValueError):self.prepare()
            self.assertEqual(original,coordinator.record_path(self.state).read_bytes())
        self.checkpoint.write_text(json.dumps(self.packet));self.screen['text']='› preserve my draft\n'
        with self.assertRaises(ValueError):self.prepare()
        self.assertEqual(original,coordinator.record_path(self.state).read_bytes())

    def test_complete_rejects_alive_old_pid_or_wrong_new_identity_and_keeps_intent(self):
        prepared=self.prepare();self.resumed(prepared);self.old_gone=False
        with self.assertRaises(ValueError):self.complete(prepared)
        self.old_gone=True;original=copy.deepcopy(self.native)
        for change in ('owner','session','effort','draft','profile'):
            self.native=copy.deepcopy(original);self.screen['text']='›\nContext 80% left\n';self.env['CODEX_HOME']=str(self.root/'personal')
            if change=='owner':self.native['labels']['teamkit.owner']='another-owner'
            if change=='session':self.native['foreground']['argv'][2]='another-session'
            if change=='effort':self.native['foreground']['argv']=[x.replace('"low"','"medium"') for x in self.native['foreground']['argv']]
            if change=='draft':self.screen['text']='› unsent user draft\n'
            if change=='profile':self.env['CODEX_HOME']=str(self.root/'team')
            with self.subTest(change=change):
                with self.assertRaises((ValueError,KitError)):self.complete(prepared)
                self.assertEqual(42,coordinator.read_record(self.state)['native_pid'])
                self.assertEqual('rebinding',coordinator.read_record(self.state)['phase'])

    def test_config_or_binding_change_blocks_completion_without_reset(self):
        prepared=self.prepare();self.resumed(prepared)
        for path in (self.config,self.bindings,self.checkpoint):
            original=path.read_bytes();path.write_bytes(original+b' ')
            with self.assertRaises(ValueError):self.complete(prepared)
            path.write_bytes(original)
            self.assertEqual(42,coordinator.read_record(self.state)['native_pid'])
        self.assertEqual('completed',self.complete(prepared)['phase'])

    def test_cli_uses_prepare_and_complete_without_process_actions(self):
        prefix=['--config',str(self.config),'--state',str(self.state),'service']
        prepared=run(parser().parse_args([*prefix,'rebind-prepare','--checkpoint-file',str(self.checkpoint),
            '--owner',self.owner,'--pid','42','--session',self.sid]))
        self.resumed(prepared)
        result=run(parser().parse_args([*prefix,'rebind-complete','--intent',prepared['id'],'--owner',self.owner,'--pid','43']))
        self.assertEqual('completed',result['phase'])

    def test_new_turn_or_stale_checkpoint_cannot_reuse_writer_stop_evidence(self):
        original=coordinator.record_path(self.state).read_bytes()
        self.native['turn']=6
        with self.assertRaisesRegex(ValueError,'another native turn'):self.prepare()
        self.native['turn']=5
        self.packet['observed_at']=time.time()-301;self.checkpoint.write_text(json.dumps(self.packet))
        with self.assertRaises(ValueError):self.prepare()
        self.assertEqual(original,coordinator.record_path(self.state).read_bytes())

    def test_prepare_and_complete_preserve_unknown_or_unrelated_app_bindings(self):
        self.layout['spaces'][0]['tabs'][0]['terminals'].append({'terminal':'tother'})
        with self.assertRaises(ValueError):self.prepare()
        self.assertEqual('ready',coordinator.read_record(self.state)['phase'])
        self.layout['spaces'][0]['tabs'][0]['terminals'].pop()
        prepared=self.prepare();self.resumed(prepared)
        self.native['labels']['herdr.tab']='different-tab'
        with self.assertRaises(ValueError):self.complete(prepared)
        self.assertEqual('rebinding',coordinator.read_record(self.state)['phase'])

    def test_existing_handoff_or_classification_is_not_replaced(self):
        directory=self.state.with_name('context-handoffs');directory.mkdir()
        job=directory/('b'*32+'.json')
        job.write_text(json.dumps({'target':{'kind':'coordinator'},'phase':'uncertain'}))
        with self.assertRaisesRegex(ValueError,'unresolved coordinator handoff'):self.prepare()
        job.unlink()
        db=sqlite3.connect(self.state)
        db.execute('CREATE TABLE operation_intake_attempts(body TEXT)')
        db.execute('INSERT INTO operation_intake_attempts VALUES (?)',(json.dumps({'owner':self.owner,'phase':'uncertain'}),))
        db.commit();db.close()
        with self.assertRaisesRegex(ValueError,'classification'):self.prepare()
        self.assertEqual('ready',coordinator.read_record(self.state)['phase'])

    def test_failure_after_intent_preserves_it_and_retries_without_another_backup(self):
        original=coordinator.record_path(self.state).read_bytes()
        native=copy.deepcopy(self.native)
        with patch.object(coordinator,'_rebind_native',side_effect=[native,ValueError('screen unknown')]):
            with self.assertRaisesRegex(ValueError,'screen unknown'):self.prepare()
        self.assertEqual('rebinding',coordinator.read_record(self.state)['phase'])
        folder=self.state.with_name('coordinator-rebinds')
        backups=list(folder.glob('*.original.json'));self.assertEqual(1,len(backups))
        self.assertEqual(original,backups[0].read_bytes())
        prepared=self.prepare()
        self.assertEqual('prepared',prepared['phase']);self.assertEqual(1,len(prepared['errors']))
        self.assertEqual(1,len(list(folder.glob('*.original.json'))))

    def test_unknown_old_os_pid_and_extra_resume_permissions_never_rebind(self):
        prepared=self.prepare();self.resumed(prepared)
        with patch.object(coordinator.os,'kill',side_effect=PermissionError('unobservable process')):
            with self.assertRaises(PermissionError):self.complete(prepared)
        self.native['foreground']['argv']+=['--add-dir','/unapproved-fixture']
        with self.assertRaisesRegex(ValueError,'exactly'):self.complete(prepared)
        self.assertEqual(42,coordinator.read_record(self.state)['native_pid'])

    def test_later_coordinator_record_is_not_overwritten_by_duplicate_completion(self):
        prepared=self.prepare();self.resumed(prepared);self.complete(prepared)
        later=coordinator.read_record(self.state);later['context_session_id']='later-session'
        coordinator.save(coordinator.record_path(self.state),later)
        before=coordinator.record_path(self.state).read_bytes()
        with self.assertRaisesRegex(ValueError,'changed'):self.complete(prepared)
        self.assertEqual(before,coordinator.record_path(self.state).read_bytes())

    def test_process_environment_reader_does_not_return_credentials(self):
        raw='codex resume fixture CODEX_HOME='+str(self.root/'personal')+' OPENAI_API_KEY=secret-fixture TEAMKIT_STATE='+str(self.state)
        with patch('subprocess.run',return_value=SimpleNamespace(stdout=raw)):
            result=READ_ENVIRONMENT(43)
        self.assertTrue(result['_api_credentials_present'])
        self.assertEqual(str(self.root/'personal'),result['CODEX_HOME'])
        self.assertNotIn('OPENAI_API_KEY',result)
        self.assertNotIn('secret-fixture',json.dumps(result))

    def test_unknown_environment_does_not_assume_a_default_personal_profile(self):
        self.env.clear()
        with self.assertRaisesRegex(ValueError,'environment is unavailable'):self.prepare()
        self.assertEqual('ready',coordinator.read_record(self.state)['phase'])

    def test_same_pid_metadata_change_during_screen_read_preserves_original_record(self):
        def changed_read(*args,**kwargs):
            self.native['foreground']['argv'][2]='different-session'
            return self.screen
        original=coordinator.record_path(self.state).read_bytes()
        with patch.object(coordinator,'_call',side_effect=changed_read):
            with self.assertRaisesRegex(ValueError,'metadata changed'):self.prepare()
        self.assertEqual(original,coordinator.record_path(self.state).read_bytes())
