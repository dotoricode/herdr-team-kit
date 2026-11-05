import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from teamkit import coordinator_policy as policy, intake_dispatch
from teamkit.app_coordinator import record_path, scope
from teamkit.config import binding
from teamkit.intake import register
from teamkit.store import Store
from teamkit.tickets import TicketStore


class CoordinatorPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root/'state.sqlite'
        self.home = self.root/'profile'
        (self.home/'sessions').mkdir(parents=True)
        self.sid = '00000000-0000-4000-8000-000000000001'
        self.journal = self.home/'sessions'/('rollout-'+self.sid+'.jsonl')
        self.journal.write_text(json.dumps({'type':'session_meta','payload':{'id':self.sid}})+'\n')
        self.pr = dict(home=str(self.home),model='gpt-test',effort='medium',identity={'email':'test@example.invalid'})
        self.config = {'profiles':{'codex':{'personal':self.pr}},'coordinator':{
            'provider':'codex','model':'gpt-test','effort':'medium','root':str(self.root)}}
        self.record = dict(phase='ready',terminal='t1',native_pid=123,nonce='owner',
                           account='personal',profile_binding=binding(self.pr),context_session_id=self.sid)
        record_path(self.state).write_text(json.dumps(self.record))
        self.name = 'gui-coordinator-'+scope(self.state)
        self.argv = ['codex','--no-daemon','--no-alt-screen','resume',self.sid]
        self.native = dict(address='t1',state='live',name=self.name,turn=7,rows=57,
            labels={'teamkit.owner':'owner','teamkit.task':'GUI-COORDINATOR','teamkit.name':self.name},
            foreground={'name':'codex','pid':123,'argv':self.argv},status={'state':'idle','app':'codex'})
        self.status = ('/status\n\n  >_ OpenAI Codex (v0.0.0)\n\n'
            '  Model: GPT-Test (reasoning medium, summaries auto)\n  Model provider: openai\n'
            '  Permissions: Workspace [/synthetic] (Approve for me)\n'
            '  Account: test@example.invalid (Test)\n  Session: '+self.sid+'\n\n› Ask Codex to do anything\n')
        self.sent = 0
        self.draft = False
        self.incomplete = False
        self.ack_started = False
        self.send_timeout = False
        self.freeze_screen = False
        self.now = 1000
        self.calls = []
        for target in ('subprocess.Popen','subprocess.call','subprocess.check_call','subprocess.check_output',
                       'os.kill','teamkit.providers.identity','teamkit.core_runtime._call'):
            guard = patch(target,side_effect=AssertionError('real native/process/auth call forbidden: '+target))
            guard.start();self.addCleanup(guard.stop)
        for target,kwargs in [
            ('teamkit.coordinator_policy.subprocess.run',{'side_effect':self.process}),
            ('teamkit.coordinator_policy.require_managed',{'return_value':'tctrl'}),
            ('teamkit.coordinator_policy._call',{'side_effect':self.read}),
            ('teamkit.intake_dispatch._call',{'side_effect':self.read}),
            ('teamkit.core_runtime._call',{'side_effect':self.core}),
            ('teamkit.coordinator_policy.time.time',{'side_effect':lambda:self.now})]:
            mock=patch(target,**kwargs);mock.start();self.addCleanup(mock.stop)

    def core(self,*args,**kwargs):
        self.assertEqual(args,('get','t1'))
        return copy.deepcopy(self.native)

    def read(self,*args,**kwargs):
        self.assert_read(args)
        return {'complete':not self.incomplete,'how':'stitched' if '--lines' in args else 'screen',
                'text':'› unsent draft' if self.draft else
                self.status*(1 if self.freeze_screen else self.sent) if self.sent else '› Ask Codex to do anything\n'}

    def assert_read(self,args):
        self.assertEqual(args[:2],('read','--json'))
        self.assertEqual(args[-1],'t1')
        if len(args)!=3:
            self.assertEqual(len(args),5)
            self.assertEqual(args[2],'--lines')
            self.assertTrue(str(args[3]).isdigit())
            self.assertTrue(1<=int(args[3])<=8192)

    def process(self,argv,**kwargs):
        self.calls.append(argv)
        if argv==['/bin/ps','eww','-p','123','-o','command=']:
            return subprocess.CompletedProcess(argv,0,stdout=' '.join(self.argv)+' HOME='+str(self.root)+
                                               ' CODEX_HOME='+str(self.home)+' SECRET_TOKEN=not-for-cache')
        condition=['--if','idle'] if self.native['status']['state']=='idle' else []
        self.assertEqual(argv,['hc','send','--json',*condition,'t1','/status'])
        self.assertNotIn('HC_TERMINAL_ID',kwargs['env'])
        for key,value in os.environ.items():
            if key!='HC_TERMINAL_ID':self.assertEqual(kwargs['env'].get(key),value)
        self.sent+=1
        if self.send_timeout:
            raise subprocess.TimeoutExpired(argv,5)
        return subprocess.CompletedProcess(argv,0,stdout=json.dumps({'started':self.ack_started,'status':'idle','turn':7}))

    def context(self,model='gpt-test',effort='medium',at=1001):
        row={'type':'turn_context','timestamp':datetime.fromtimestamp(at,timezone.utc).isoformat(),
             'payload':{'model':model,'effort':effort,'cwd':str(self.root)}}
        with self.journal.open('a') as stream:stream.write(json.dumps(row)+'\n')

    def ready(self):
        return intake_dispatch._ready(self.config,self.state)

    def test_registered_resume_ready_uses_exact_local_status_and_preserves_record(self):
        before=record_path(self.state).read_bytes()
        result=self.ready()
        self.assertEqual(result[3]['_coordinator_policy']['mode'],'resumed-status')
        self.assertEqual(self.sent,1)
        self.assertEqual(record_path(self.state).read_bytes(),before)
        self.assertNotIn('SECRET_TOKEN',policy._path(self.state).read_text())

    def test_two_ready_observations_keep_the_same_policy_cid(self):
        one=self.ready()[3]['_coordinator_policy'];self.now+=1
        two=self.ready()[3]['_coordinator_policy']
        self.assertEqual(one,two)
        self.assertEqual(self.sent,2)

    def test_identical_clipped_live_viewport_uses_new_bounded_history_block(self):
        def read(*args,**kwargs):
            result=self.read(*args,**kwargs)
            if '--lines' not in args and self.sent:
                result['text']=self.status  # Both visible viewports look identical.
            return result
        with patch.object(policy,'_call',side_effect=read),patch.object(intake_dispatch,'_call',side_effect=read):
            first=self.ready()[3]['_coordinator_policy'];self.now+=1
            second=self.ready()[3]['_coordinator_policy']
        self.assertEqual(first,second)
        self.assertEqual(self.sent,2)

    def test_fresh_history_with_a_live_draft_cannot_verify(self):
        def read(*args,**kwargs):
            result=self.read(*args,**kwargs)
            if '--lines' not in args and self.sent:
                result['text']='› new live draft'
            return result
        with patch.object(policy,'_call',side_effect=read),patch.object(intake_dispatch,'_call',side_effect=read):
            with self.assertRaises(ValueError):self.ready()
        self.assertEqual(self.sent,1)
        self.assertEqual(json.loads(policy._path(self.state).read_text())['phase'],'uncertain')

    def test_history_unknown_format_or_byte_cap_fails_before_local_command(self):
        for changes in ({'how':'unknown'},{'complete':False},{'text':'x'*(policy.STATUS_HISTORY_BYTES+1)}):
            with self.subTest(fields=list(changes)):
                def read(*args,**kwargs):
                    result=self.read(*args,**kwargs)
                    if '--lines' in args or changes.get('complete') is False:result.update(changes)
                    return result
                with patch.object(policy,'_call',side_effect=read),patch.object(intake_dispatch,'_call',side_effect=read):
                    with self.assertRaises(ValueError):self.ready()
                self.assertEqual(self.sent,0)

    def test_incomplete_max_history_adapts_and_fixes_the_same_complete_budget(self):
        budgets=[]
        def read(*args,**kwargs):
            result=self.read(*args,**kwargs)
            if '--lines' not in args:
                return result
            lines=int(args[3]);budgets.append(lines)
            available=899+self.sent*10
            live=result['text'];live='\n'*max(0,57-len(live.splitlines()))+live
            result.update(how='history' if lines==8192 else 'stitched',complete=lines<=available,
                          text='prior context\n'*min(lines,available)+live)
            return result
        with patch.object(policy,'_call',side_effect=read),patch.object(intake_dispatch,'_call',side_effect=read):
            first=self.ready()[3]['_coordinator_policy'];self.now+=1
            second=self.ready()[3]['_coordinator_policy']
        self.assertEqual(first,second)
        self.assertEqual(budgets,[8192,898,898,8192,908,908])
        self.assertEqual(self.sent,2)

    def test_incomplete_post_command_history_is_unknown_and_never_resends(self):
        def read(*args,**kwargs):
            result=self.read(*args,**kwargs)
            if '--lines' in args and self.sent:result['complete']=False
            return result
        with patch.object(policy,'_call',side_effect=read),patch.object(intake_dispatch,'_call',side_effect=read), \
             patch.object(policy.time,'sleep'):
            with self.assertRaises(ValueError):self.ready()
        self.assertEqual(self.sent,1)
        with self.assertRaises(ValueError):self.ready()
        self.assertEqual(self.sent,1)

    def test_legacy_weak_freshness_cache_is_preserved_but_not_trusted_or_reprobed(self):
        self.ready();path=policy._path(self.state)
        cache=json.loads(path.read_text());cache['schema']=1;path.write_text(json.dumps(cache))
        old=path.read_bytes();calls=len(self.calls)
        with self.assertRaises(ValueError):intake_dispatch._bound(self.config,self.state)
        with self.assertRaises(ValueError):self.ready()
        self.assertEqual(path.read_bytes(),old)
        self.assertEqual(len(self.calls),calls)

    def test_old_preselection_turn_context_does_not_override_actual_status(self):
        self.context(effort='low',at=900)
        self.assertEqual(self.ready()[1]['effort'],'medium')

    def test_busy_cache_works_for_a_long_turn_without_status_or_process_calls(self):
        proof=self.ready()[3]['_coordinator_policy'];self.context();self.now=100000
        self.native['status']['state']='working'
        calls=len(self.calls)
        result=intake_dispatch._bound(self.config,self.state)
        self.assertEqual(result[3]['_coordinator_policy'],proof)
        policy.fence(self.config,self.state,self.record,self.pr,proof)
        self.assertEqual(len(self.calls),calls)
        self.assertEqual(self.sent,1)

    def test_busy_without_current_context_or_after_policy_change_fails_without_status(self):
        self.ready();self.native['status']['state']='working';calls=len(self.calls)
        with self.assertRaises(ValueError):intake_dispatch._bound(self.config,self.state)
        self.context(effort='high');self.now=1002
        with self.assertRaises(ValueError):intake_dispatch._bound(self.config,self.state)
        self.assertEqual(len(self.calls),calls)

    def test_draft_incomplete_busy_or_wrong_resume_never_sends_status(self):
        for change in ('draft','incomplete','busy','session','fork'):
            with self.subTest(change=change):
                old=copy.deepcopy(self.native)
                self.draft=change=='draft';self.incomplete=change=='incomplete'
                if change=='busy':self.native['status']['state']='working'
                if change=='session':self.native['foreground']['argv'][-1]='different-session'
                if change=='fork':self.native['foreground']['argv'].append('--fork')
                with self.assertRaises((ValueError,intake_dispatch.NativeError)):self.ready()
                self.native=old;self.draft=False;self.incomplete=False
                self.assertEqual(self.sent,0)

    def test_mismatched_model_effort_account_session_or_unknown_formatter_blocks(self):
        original=self.status
        for old,new in [('GPT-Test','GPT-Other'),('reasoning medium','reasoning low'),
                        ('test@example.invalid','other@example.invalid'),(self.sid,'other-session'),
                        ('Permissions: Workspace','Permissions: Unknown')]:
            with self.subTest(change=new):
                self.status=original.replace(old,new)
                with self.assertRaises(ValueError):self.ready()
                path=policy._path(self.state)
                if path.exists():path.unlink()  # Isolated test case; production never clears unknown automatically.

    def test_unknown_status_outcome_is_preserved_and_not_repeated(self):
        self.ack_started=True
        with self.assertRaises(ValueError):self.ready()
        self.ack_started=False
        with self.assertRaises(ValueError):self.ready()
        self.assertEqual(self.sent,1)

    def test_local_status_ack_timeout_can_verify_only_its_fresh_policy_output(self):
        self.send_timeout=True
        proof=self.ready()[3]['_coordinator_policy']
        self.assertEqual(proof['mode'],'resumed-status')
        self.assertEqual(self.sent,1)
        cache=json.loads(policy._path(self.state).read_text())
        self.assertEqual(cache['phase'],'verified')
        self.assertEqual(cache['transport'],'local_command_ack_timeout')
        self.assertEqual(self.native['turn'],7)

    def test_local_status_ack_timeout_without_fresh_output_stays_unknown_without_retry(self):
        self.sent=1;self.freeze_screen=True;self.send_timeout=True
        with patch.object(policy.time,'sleep'):
            with self.assertRaises(ValueError):self.ready()
        self.assertEqual(self.sent,2)
        cache=json.loads(policy._path(self.state).read_text())
        self.assertEqual(cache['phase'],'uncertain')
        self.assertEqual(cache['transport'],'local_command_ack_timeout')
        self.send_timeout=False
        with self.assertRaises(ValueError):self.ready()
        self.assertEqual(self.sent,2)

    def test_local_status_ack_timeout_never_bypasses_policy_or_same_turn_checks(self):
        self.send_timeout=True
        self.status=self.status.replace('test@example.invalid','other@example.invalid')
        with self.assertRaises(ValueError):self.ready()
        self.assertEqual(json.loads(policy._path(self.state).read_text())['phase'],'uncertain')

    def timeout_with_native_change(self, change):
        self.send_timeout=True;original=self.process
        def process(argv,**kwargs):
            try:
                return original(argv,**kwargs)
            except subprocess.TimeoutExpired:
                change()
                raise
        with patch.object(policy.subprocess,'run',side_effect=process):
            with self.assertRaises((ValueError,intake_dispatch.NativeError)):self.ready()
        self.assertEqual(self.sent,1)
        self.assertEqual(json.loads(policy._path(self.state).read_text())['phase'],'uncertain')

    def test_local_status_timeout_with_changed_native_turn_stays_unknown(self):
        self.timeout_with_native_change(lambda:self.native.update(turn=8))

    def test_local_status_timeout_with_changed_native_owner_stays_unknown(self):
        self.timeout_with_native_change(lambda:self.native['labels'].update({'teamkit.owner':'replacement'}))

    def test_invalid_ack_cannot_be_replaced_by_fresh_policy_output(self):
        original=self.process
        def invalid(argv,**kwargs):
            result=original(argv,**kwargs)
            if argv[0]=='hc':result.stdout='[]'
            return result
        with patch.object(policy.subprocess,'run',side_effect=invalid):
            with self.assertRaises(ValueError):self.ready()
        self.assertEqual(self.sent,1)
        self.assertEqual(json.loads(policy._path(self.state).read_text())['phase'],'uncertain')

    def test_other_command_failure_cannot_be_reconciled_from_fresh_output(self):
        original=self.process
        def failed(argv,**kwargs):
            result=original(argv,**kwargs)
            if argv[0]=='hc':raise subprocess.CalledProcessError(1,argv)
            return result
        with patch.object(policy.subprocess,'run',side_effect=failed):
            with self.assertRaises(ValueError):self.ready()
        self.assertEqual(self.sent,1)
        self.assertEqual(json.loads(policy._path(self.state).read_text())['phase'],'uncertain')

    def test_unchanged_old_status_screen_is_not_reused_as_fresh_evidence(self):
        self.sent=1;self.freeze_screen=True
        with patch.object(policy.time,'sleep'):
            with self.assertRaisesRegex(ValueError,'did not advance'):self.ready()
        self.assertEqual(self.sent,2)
        with self.assertRaises(ValueError):self.ready()
        self.assertEqual(self.sent,2)

    def stale_status_with_noise(self, noise):
        self.sent=1;self.freeze_screen=True
        before=self.status
        def read(*args,**kwargs):
            self.assert_read(args)
            text=before if self.sent<2 else noise(before) if callable(noise) else before+noise
            return {'complete':True,'how':'stitched' if '--lines' in args else 'screen','text':text}
        with patch.object(policy,'_call',side_effect=read), \
             patch.object(intake_dispatch,'_call',side_effect=read), patch.object(policy.time,'sleep'):
            with self.assertRaises(ValueError):self.ready()
        self.assertEqual(self.sent,2)
        self.assertEqual(json.loads(policy._path(self.state).read_text())['phase'],'uncertain')
        with self.assertRaises(ValueError):self.ready()
        self.assertEqual(self.sent,2)

    def test_old_valid_status_plus_unrelated_newline_does_not_verify(self):
        self.stale_status_with_noise('\n')

    def test_old_valid_status_plus_other_output_does_not_verify(self):
        self.stale_status_with_noise(lambda before:'background observation finished\n'+before)

    def test_new_status_echo_without_its_new_result_block_does_not_verify(self):
        self.stale_status_with_noise('\n/status\n› Ask Codex to do anything\n')

    def test_new_marker_and_complete_block_survive_unrelated_viewport_clipping(self):
        before='earlier unrelated history\n'+self.status
        after=self.status+self.status
        observed=policy._status_increment(before,after)
        self.assertEqual((observed['model'],observed['effort']),('gpt-test','medium'))

    def test_viewport_with_unobservable_command_increment_remains_unknown(self):
        with self.assertRaises(ValueError):
            policy._status_increment(self.status,self.status+'\n')
        with self.assertRaises(ValueError):
            policy._status_increment(self.status,self.status)  # Old marker clipped, no increment evidence.

    def test_malformed_cache_and_changed_cid_fail_without_status(self):
        proof=self.ready()[3]['_coordinator_policy'];calls=len(self.calls)
        path=policy._path(self.state);cache=json.loads(path.read_text());cache['cid']='0'*64
        path.write_text(json.dumps(cache))
        with self.assertRaises(ValueError):policy.fence(self.config,self.state,self.record,self.pr,proof)
        self.assertEqual(len(self.calls),calls)

    def test_explicit_model_arguments_keep_the_existing_guard_and_do_not_probe(self):
        self.native['foreground']['argv']=['codex','--model','gpt-test','-c','model_reasoning_effort="medium"','--no-daemon']
        result=self.ready()
        self.assertEqual(result[3]['_coordinator_policy']['mode'],'argv')
        self.assertEqual(self.calls,[])
        self.native['foreground']['argv'][2]='other-model'
        with self.assertRaises(ValueError):self.ready()

    def test_wrong_process_home_never_sends_status(self):
        original=self.process
        def process(argv,**kwargs):
            result=original(argv,**kwargs)
            if argv[0]=='/bin/ps':result.stdout=result.stdout.replace('CODEX_HOME='+str(self.home),'CODEX_HOME=/different-profile')
            return result
        with patch.object(policy.subprocess,'run',side_effect=process):
            with self.assertRaises(ValueError):self.ready()
        self.assertEqual(self.sent,0)

    def test_late_draft_blocks_local_status_after_controller_verification(self):
        def controller():
            self.draft=True
            return 'tctrl'
        with patch.object(policy,'require_managed',side_effect=controller):
            with self.assertRaises(ValueError):self.ready()
        self.assertEqual(self.sent,0)

    def test_only_controller_environment_marker_is_removed_for_literal_status(self):
        with patch.dict(os.environ,{'HC_TERMINAL_ID':'tctrl','POLICY_FIXTURE':'unchanged'}):
            self.ready()
            self.assertEqual(os.environ['HC_TERMINAL_ID'],'tctrl')
        self.assertEqual(self.sent,1)

    def test_done_native_uses_the_same_local_status_ready_contract(self):
        self.native['status']['state']='done'
        self.ready()
        self.assertEqual(self.sent,1)
        self.assertEqual(self.calls[-1],['hc','send','--json','t1','/status'])

    def test_cache_format_permissions_unknown_state_and_record_rebinding_fail_closed(self):
        self.ready();path=policy._path(self.state);original=path.read_text();calls=len(self.calls)
        for changed in ('format','permissions','native','pid','session'):
            with self.subTest(changed=changed):
                native=copy.deepcopy(self.native);record=copy.deepcopy(self.record)
                if changed=='format':path.write_text('[]')
                if changed=='permissions':path.chmod(0o644)
                if changed=='native':self.native['status']['state']='unknown'
                if changed=='pid':record['native_pid']=456
                if changed=='session':record['context_session_id']='different-session'
                record_path(self.state).write_text(json.dumps(record))
                with self.assertRaises((ValueError,intake_dispatch.NativeError)):
                    intake_dispatch._bound(self.config,self.state)
                self.native=native;record_path(self.state).write_text(json.dumps(self.record))
                path.write_text(original);path.chmod(0o600)
                self.assertEqual(len(self.calls),calls)

    def test_intake_attempt_submit_and_final_send_fence_use_the_same_cached_cid(self):
        self.now=datetime.now(timezone.utc).timestamp()
        self.config.update(quota_ttl_seconds=120,operations={'enabled':True,'intake':{'enabled':True}})
        config_path=self.root/'config.json';config_path.write_text('{}')
        queue=TicketStore(self.state);self.addCleanup(queue.close)
        instruction=register(queue.db,'Classify synthetic scope only',{'key':'fixture-instruction'})
        store=Store(self.state)
        store.quota('codex/personal',{'observed_at':self.now,'binding':binding(self.pr),
            'source':'codex-native-quota','five_hour':{'used':1,'resets_at':self.now+500},
            'weekly':{'used':1,'resets_at':self.now+500}});store.close()
        def accept(name,text,before_send=None):
            self.assertIsNotNone(before_send)
            calls=len(self.calls);before_send();self.assertEqual(len(self.calls),calls)
            self.native['status']['state']='working';self.native['turn']=8
            self.context(at=self.now+0.01);self.now+=0.02
            return {'accepted':True,'terminal':'t1','native_pid':123,'turn_baseline':7,'turn':8}
        with patch.object(intake_dispatch,'load',return_value=self.config), \
             patch.object(intake_dispatch.providers,'identity'), \
             patch('teamkit.admission.observe_native',return_value={'complete':True,'observed_at':self.now,'terminals':[]}), \
             patch.object(intake_dispatch.CoreRuntime,'accept',side_effect=accept):
            result=intake_dispatch.tick(config_path,self.state)
        self.assertEqual(result.get('phase'),'accepted',result)
        self.assertEqual(self.sent,2)
        before=len(self.calls)
        received=intake_dispatch.submit(self.config,self.state,instruction['id'],result['attempt'],
                                       'owner','t1',{'needs_clarification':'synthetic scope question'})
        self.assertEqual(received['state'],'needs_clarification')
        self.assertEqual(len(self.calls),before)


if __name__=='__main__':unittest.main()
