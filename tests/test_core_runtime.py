"""Core boundary tests use hc JSON fixtures; they do not launch real agents."""
import copy
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from teamkit.core_runtime import CoreRuntime, require_managed
from teamkit.runtime import RuntimeError as KitError
from teamkit.config import binding
from test_cli import Fixture, StoreContext


def terminal(address='t8', status='idle', owner='owner'):
    return {'address':address, 'state':'live', 'name':'tk-owner',
            'labels':{'teamkit.owner':owner,'teamkit.task':'T1','teamkit.name':'tk-owner'},
            'status':{'state':status,'app':'codex'}, 'turn':1,
            'foreground':{'pid':88,'name':'codex','argv':['codex','--no-daemon']}}


class HcFixture:
    def __init__(self):
        self.calls=[]
        self.agent=terminal()
        self.launch=terminal(status=None)
        self.wait={'terminal':'t8','ended':True,'reason':'status','status':'idle','turn':1,'children':[]}
        self.send={'started':True,'turn':1,'wait':{**self.wait,'turn':2}}
    def __call__(self, argv, **options):
        self.calls.append((argv, options))
        if argv[:2]==['hc','get']:
            value={'address':'t1','state':'live'} if argv[2]=='t1' else self.agent
        elif argv[:2]==['hc','run']:value=self.launch
        elif argv[:2]==['hc','wait']:value=self.wait
        elif argv[:2]==['hc','send']:value=self.send
        elif argv[:2]==['hc','label']:
            self.agent['labels'].update(item.split('=',1) for item in argv[4:]);value={'ok':True}
        else:raise AssertionError(argv)
        return subprocess.CompletedProcess(argv,0,json.dumps(value),'')


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.hc=HcFixture()
        self.env=patch.dict(os.environ,{'HC_TERMINAL_ID':'t1','HERDR_ENV':''});self.env.start()
        self.run=patch('teamkit.core_runtime.subprocess.run',side_effect=self.hc);self.run.start()
        self.task={'id':'T1','team':'alpha','role':'leader','root':'/work/alpha','provider':'codex','owner':'owner',
                   'runtime':{'backend':'core','name':'tk-owner','terminal':'t8','native_pid':88}}
        self.spec={'kind':'codex','env':{'TEAMKIT_OWNER':'owner','CODEX_HOME':'/private/home with spaces',
                                     'OPENAI_API_KEY':''},'args':['--no-daemon']}
    def tearDown(self):self.run.stop();self.env.stop()
    def test_controller_requires_real_live_local_address(self):
        for value in ('','machine/t1','--help'):
            with self.subTest(value=value),patch.dict(os.environ,{'HC_TERMINAL_ID':value}):
                with self.assertRaises(KitError):require_managed()
        with patch('teamkit.core_runtime.subprocess.run',return_value=subprocess.CompletedProcess([],0,'{"address":"t2","state":"live"}','')):
            with self.assertRaises(KitError):require_managed()
    def test_launch_records_address_before_readiness_and_passes_argv(self):
        observed=[]
        result=CoreRuntime().launch(self.task,self.spec,'tk-owner',lambda x:observed.append(copy.deepcopy(x)))
        self.assertEqual(observed[0]['terminal'],'t8');self.assertEqual(observed[0]['backend'],'core')
        self.assertEqual(result['native_pid'],88)
        argv=next(a for a,_ in self.hc.calls if a[:2]==['hc','run'])
        self.assertIn('--outlive-parent',argv);self.assertIn('--keep',argv)
        self.assertIn('CODEX_HOME=/private/home with spaces',argv)
        self.assertIn('teamkit.owner=owner',argv)
        self.assertEqual(argv[-2:],['codex','--no-daemon'])
        self.assertFalse(any(a[:2]==['hc','send'] for a,_ in self.hc.calls))
    def test_readiness_timeout_preserves_created_address_and_does_not_prompt(self):
        self.hc.wait.update(ended=False,reason='timeout',status=None)
        self.hc.agent['status']=None
        recorded=[]
        with self.assertRaises(KitError):CoreRuntime().launch(self.task,self.spec,'tk-owner',recorded.append)
        self.assertEqual(recorded[0]['terminal'],'t8')
        self.assertFalse(any(a[:2]==['hc','send'] for a,_ in self.hc.calls))
    def test_prompt_sends_exactly_once_to_stored_terminal(self):
        runtime=CoreRuntime(self.task)
        result=runtime.prompt('tk-owner','one task; $(never shell)')
        sends=[a for a,_ in self.hc.calls if a[:2]==['hc','send']]
        self.assertEqual(len(sends),1)
        self.assertIn('t8',sends[0]);self.assertEqual(sends[0][-1],'one task; $(never shell)')
        self.assertIn('--if',sends[0]);self.assertEqual(result['wait']['reason'],'status')
    def test_send_timeout_not_started_and_blocked_are_not_success(self):
        for result in ({'started':False,'wait':{**self.hc.wait,'reason':'not_started'}},
                       {'started':True,'wait':{**self.hc.wait,'ended':False,'reason':'timeout'}},
                       {'started':True,'wait':{**self.hc.wait,'status':'blocked'}},
                       {'started':None,'wait':None}):
            with self.subTest(result=result):
                self.hc.send=result
                with self.assertRaises(KitError):CoreRuntime(self.task).prompt('tk-owner','once')
    def test_wrong_owner_pid_provider_or_state_is_rejected_before_send(self):
        original=terminal()
        cases=[{**original,'labels':{'teamkit.owner':'other','teamkit.task':'T1'}},
               {**original,'foreground':{'pid':89,'name':'codex'}},
               {**original,'foreground':{'pid':88,'name':'zsh'}},
               {**original,'state':'finished'},
               {**original,'status':{'state':'blocked','app':'codex'}},
               {**original,'status':None}]
        for value in cases:
            with self.subTest(value=value):
                self.hc.agent=value
                with self.assertRaises(KitError):CoreRuntime(self.task).prompt('tk-owner','once')
        self.assertFalse(any(a[:2]==['hc','send'] for a,_ in self.hc.calls))
    def test_get_normalizes_only_reported_status(self):
        self.hc.agent['status']=None
        value=CoreRuntime(self.task).call('agent','get','tk-owner')
        self.assertEqual(value['agent']['agent_status'],'unknown')
    def test_other_program_status_does_not_mark_agent_ready(self):
        self.hc.agent['status']={'state':'idle','app':'other'}
        value=CoreRuntime(self.task).call('agent','get','tk-owner')
        self.assertEqual(value['agent']['agent_status'],'unknown')
        self.assertFalse(value['agent']['interactive_ready'])

    def adoption(self):
        self.hc.agent.update(name=None,labels={},cwd='/work/alpha')
        self.hc.agent['foreground']['argv']=['codex','--model','fixture','-c','model_reasoning_effort="low"','--no-daemon']
        self.hc.agent['foreground']['cwd']='/work/alpha'
        self.spec['args']=['--model','fixture','-c','model_reasoning_effort="low"','--no-daemon']
        return self.task,self.spec

    def test_adopt_labels_existing_terminal_without_launch_or_prompt(self):
        task,spec=self.adoption();observed=[]
        with tempfile.TemporaryDirectory() as folder:
            spec['env']['TEAMKIT_STATE']=folder+'/state.sqlite'
            result=CoreRuntime().adopt(task,spec,'tk-owner','t8',lambda x:observed.append(copy.deepcopy(x)))
        self.assertTrue(observed[0]['bind_intent'])
        self.assertIsNone(result['native_name']);self.assertTrue(result['binding_confirmed'])
        self.assertEqual(result['native_pid'],88)
        labels=[a for a,_ in self.hc.calls if a[:2]==['hc','label']]
        self.assertEqual(len(labels),1);self.assertIn('teamkit.name=tk-owner',labels[0])
        self.assertFalse(any(a[1] in ('run','send') for a,_ in self.hc.calls))
        self.task['runtime']=result
        self.assertEqual(CoreRuntime(self.task).call('agent','get','tk-owner')['agent']['agent_status'],'idle')

    def test_adoption_metadata_or_ownership_mismatch_is_read_only(self):
        task,spec=self.adoption();original=copy.deepcopy(self.hc.agent)
        cases=[{**original,'labels':{'teamkit.owner':'previous'}},
               {**original,'labels':{'teamkit.task':'old'}},
               {**original,'status':{'app':'codex','state':'working'}},
               {**original,'foreground':{**original['foreground'],'argv':['codex','--model','wrong','-c','model_reasoning_effort="low"']}},
               {**original,'foreground':{**original['foreground'],'argv':['codex','--model','fixture','-c','model_reasoning_effort="high"']}},
               {**original,'foreground':{**original['foreground'],'pid':None}},
               {**original,'cwd':'/other','foreground':{**original['foreground'],'cwd':'/other'}}]
        for native in cases:
            with self.subTest(native=native):
                self.hc.agent=native
                with self.assertRaises(KitError):CoreRuntime.validate_adoption(task,spec,'t8')
        self.assertFalse(any(a[1] in ('run','send','label') for a,_ in self.hc.calls))

    def test_adoption_rejects_controller_design_or_linked_work(self):
        task,spec=self.adoption()
        for changed,address in ((task,'t1'),({**task,'role':'implementer'},'t8'),
                                ({**task,'classification':{'kind':'design'}},'t8'),({**task,'work_id':'W1'},'t8')):
            with self.subTest(task=changed,address=address):
                with self.assertRaises(KitError):CoreRuntime.validate_adoption(changed,spec,address)
        self.assertFalse(any(a[1] in ('run','send','label') for a,_ in self.hc.calls))

    def test_adoption_requires_unambiguous_native_model_and_effort(self):
        task,spec=self.adoption()
        original=list(self.hc.agent['foreground']['argv'])
        for argv in (original+['-c','model_reasoning_effort="high"'],
                     original+['-c','model="other"'],
                     original+['--model','other'],
                     original[:-1]):
            with self.subTest(argv=argv):
                self.hc.agent['foreground']['argv']=argv
                with self.assertRaises(KitError):CoreRuntime.validate_adoption(task,spec,'t8')
        self.assertFalse(any(a[1]=='label' for a,_ in self.hc.calls))

    def test_adoption_label_failure_keeps_binding_intent(self):
        task,spec=self.adoption();observed=[]
        previous=self.hc.__call__
        def fail(argv,**opts):
            if argv[:2]==['hc','label']:raise subprocess.TimeoutExpired(argv,45)
            return previous(argv,**opts)
        with tempfile.TemporaryDirectory() as folder,patch('teamkit.core_runtime.subprocess.run',side_effect=fail):
            spec['env']['TEAMKIT_STATE']=folder+'/state.sqlite'
            with self.assertRaises(KitError):CoreRuntime().adopt(task,spec,'tk-owner','t8',observed.append)
        self.assertEqual(observed[0]['terminal'],'t8');self.assertTrue(observed[0]['bind_intent'])
        self.assertNotIn('binding_confirmed',observed[0])

    def test_transport_and_bad_json_are_never_success(self):
        for result in (subprocess.CompletedProcess([],1,'','not_found'),subprocess.CompletedProcess([],0,'not-json','')):
            with self.subTest(result=result),patch('teamkit.core_runtime.subprocess.run',return_value=result):
                with self.assertRaises(KitError):require_managed()


class CoreCliTests(Fixture):
    def setUp(self):
        super().setUp();self.value['runtime_backend']='core';self.config.write_text(json.dumps(self.value))
    def test_outside_core_dispatch_is_noop_before_claim(self):
        self.create()
        with patch.dict(os.environ,{'HC_TERMINAL_ID':'','HERDR_ENV':'1'}):
            with self.assertRaises(KitError):self.call('dispatch','T1')
        self.assertEqual(self.call('show','T1')['state'],'queued')
    def test_core_tell_does_not_retarget_existing_tui_owner(self):
        self.call('create','L1','--team','alpha','--role','leader','--root',str(self.repo),'--goal-file',str(self.root/'goal.txt'))
        with StoreContext(self.state) as db:
            db.claim('L1','owner','codex','team','fixture','low');db.update('L1','owner','launching',{})
            db.update('L1','owner','submitted',{'name':'tk-owner','pane':'w1:p1'});db.receipt('L1','owner');db.park('L1','owner')
        message=self.root/'message.txt';message.write_text('Do not send')
        before=self.call('events','L1')
        with patch.dict(os.environ,{'HC_TERMINAL_ID':'t1'}),patch('teamkit.core_runtime.subprocess.run') as run:
            with self.assertRaises(KitError):self.call('tell','L1','--message-file',str(message))
            run.assert_not_called()
        self.assertEqual(before,self.call('events','L1'))
    def test_launch_timeout_retains_core_address_and_never_relaunches(self):
        self.create();now=time.time()
        with StoreContext(self.state) as db:
            db.quota('codex/team',{'binding':binding(self.value['profiles']['codex']['team']),'observed_at':now,
                                 'five_hour':{'used':1,'resets_at':now+900},'weekly':{'used':1,'resets_at':now+900}})
        def partial(task,spec,name,callback):
            callback({'name':name,'backend':'core','terminal':'t8','pane':'t8'})
            raise KitError('startup uncertain')
        with patch('teamkit.core_runtime.require_managed'),patch('teamkit.providers.identity'),patch('teamkit.core_runtime.CoreRuntime') as runtime:
            runtime.return_value.launch.side_effect=partial
            with self.assertRaises(KitError):self.call('dispatch','T1')
            current=self.call('show','T1')
            self.assertEqual(current['state'],'uncertain')
            self.assertEqual(current['runtime']['backend'],'core')
            self.assertEqual(current['runtime']['terminal'],'t8')
            with self.assertRaises(ValueError):self.call('dispatch','T1')
            self.assertEqual(runtime.return_value.launch.call_count,1)
            runtime.return_value.prompt.assert_not_called()

    def test_terminal_dispatch_guard_is_before_claim(self):
        self.create()
        with patch('teamkit.core_runtime.require_managed'),patch('teamkit.providers.identity') as identity:
            with self.assertRaises(KitError):self.call('dispatch','T1','--terminal','t8')
            identity.assert_not_called()
        self.assertEqual(self.call('show','T1')['state'],'queued')

    def leader_quota(self):
        self.value['teams']['alpha']['roles']['leader']={'provider':'codex','model':'fixture','effort':'low'}
        self.config.write_text(json.dumps(self.value))
        self.call('create','L1','--team','alpha','--role','leader','--root',str(self.repo),'--goal-file',str(self.root/'goal.txt'))
        now=time.time()
        with StoreContext(self.state) as db:
            db.quota('codex/team',{'binding':binding(self.value['profiles']['codex']['team']),'observed_at':now,
                                 'five_hour':{'used':1,'resets_at':now+900},'weekly':{'used':1,'resets_at':now+900}})

    def test_adoption_native_preflight_failure_keeps_task_queued(self):
        self.leader_quota();before=self.call('events','L1')
        with patch('teamkit.core_runtime.require_managed'),patch('teamkit.providers.identity'),patch('teamkit.core_runtime.CoreRuntime.validate_adoption',side_effect=KitError('metadata mismatch')):
            with self.assertRaises(KitError):self.call('dispatch','L1','--terminal','t8')
        self.assertEqual(self.call('events','L1'),before)
        self.assertEqual(self.call('show','L1')['state'],'queued')

    def test_adoption_uses_common_claim_and_receipt_path_without_launch(self):
        self.leader_quota()
        def validate(*args):
            self.assertEqual(self.call('show','L1')['state'],'queued')
        def adopted(task,spec,name,address,callback):
            self.assertEqual(self.call('show','L1')['state'],'launching')
            result={'name':name,'backend':'core','terminal':address,'pane':address,'native_pid':88,'native_name':None}
            callback(result);return result
        def received(runtime,store,config_path,task,owner,text):
            self.assertIn('receipt L1 --owner '+owner,text)
            self.assertEqual(task['runtime']['receipt_protocol'],'host-fresh-v1')
            store.receipt('L1',owner);store.park('L1',owner)
        with patch('teamkit.core_runtime.require_managed'),patch('teamkit.providers.identity'),patch('teamkit.core_runtime.CoreRuntime') as runtime,patch('teamkit.receipt.prompt',side_effect=received) as receipt:
            runtime.validate_adoption.side_effect=validate
            runtime.return_value.adopt.side_effect=adopted
            result=self.call('dispatch','L1','--terminal','t8')
            self.assertEqual(result['state'],'ready');self.assertEqual(result['attempt_count'],1)
            runtime.return_value.launch.assert_not_called();receipt.assert_called_once()

    def test_backend_requires_explicit_supported_name(self):
        self.value['runtime_backend']='invalid';self.config.write_text(json.dumps(self.value))
        with self.assertRaises(ValueError):self.call('doctor')


if __name__=='__main__':unittest.main()
