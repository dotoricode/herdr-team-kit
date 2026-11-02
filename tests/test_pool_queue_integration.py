"""Queue consumer checks use actual files/SQLite and a doubled native boundary."""
import fcntl
import json
import os
import unittest
from unittest.mock import patch

import test_pool_control as fixtures
from teamkit import pool_control as control, context_handoff as context
from teamkit.config import load


class QueueIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.PoolControlTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.addCleanup(self.f.doCleanups)
        self.message = self.f.root/'request.txt'
        self.message.write_text('Perform the approved bounded task.')

    def work(self):
        self.f.value['private_plan_root']=str(self.f.root/'plans')
        self.f.config.write_text(json.dumps(self.f.value))
        plan=self.f.root/'plan.md';plan.write_text('Implement a bounded feature.')
        contract=self.f.root/'contract.json'
        contract.write_text(json.dumps({'title':'Feature','requirements':['Preserve behavior'],
            'allowed_paths':['feature.txt'],'steps':[{'id':'behavior','title':'Behavior','weight':100}]}))
        self.f.call('work','create','WORK-1','--team','alpha','--root',str(self.f.repo),
                    '--plan-file',str(plan),'--contract-file',str(contract))
        self.f.call('work','submit','WORK-1')
        return self.f.call('work','approve','WORK-1','--user-approved','--approval-reference','Fixture approval')

    def test_current_workflow_gate_cannot_be_replaced_by_free_text_resolution(self):
        actor=self.f.dispatched();work=self.work()
        job=control.enqueue(self.f.config,self.f.state,'alpha',self.message,work_id='WORK-1')
        from teamkit.workflow import document
        _,_,digest=document(load(self.f.config),work)
        path=control.folder(self.f.state)/(job['id']+'.json')
        value=context.read(path)
        value.update(phase='received',receiver={'owner':actor['owner'],'attempt':actor['attempt_count'],
                     'terminal':actor['runtime']['terminal']},workflow_ref={'work_id':'WORK-1',
                     'revision':work['revision'],'digest':digest,'root':str(self.f.repo)})
        context.write(path,value)
        evidence=self.f.root/'resolution.json'
        evidence.write_text(json.dumps({'work_id':'WORK-1','owner':actor['owner'],
            'attempt':actor['attempt_count'],'writer_stopped':True,'verification':['Local check reported pass.']}))
        with patch.object(control,'idle_native',return_value={'status':{'state':'done'}}):
            with self.assertRaises(ValueError):
                control.resolve(self.f.config,self.f.state,job['id'],'completed',evidence,actor['owner'],actor['attempt_count'])
        self.assertEqual(context.read(path)['phase'],'received')

    def test_long_transport_wait_does_not_hold_global_start_lock(self):
        actor=self.f.dispatched()
        job=control.enqueue(self.f.config,self.f.state,'alpha',self.message,work_id='WAIT')
        def transport(*args):
            fd=os.open(context.directory(self.f.state)/'model-start.lock',os.O_CREAT|os.O_WRONLY,0o600)
            try:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:self.fail('global start lock retained during transport wait')
            finally:os.close(fd)
            return actor
        with patch.object(context,'capacity_available',return_value=True),patch.object(control,'official',side_effect=transport):
            control.advance(self.f.config,self.f.state,job['id'])
        self.assertEqual(control.requests(self.f.state)[0]['phase'],'received')

    def test_priority_selects_waiting_work_without_preempting_received_work(self):
        actor=self.f.dispatched()
        first=control.enqueue(self.f.config,self.f.state,'alpha',self.message,work_id='LOW',priority=0)
        urgent=control.enqueue(self.f.config,self.f.state,'alpha',self.message,work_id='HIGH',priority=9)
        with patch.object(context,'capacity_available',return_value=True),patch.object(control,'official',return_value=actor) as sent:
            control.advance(self.f.config,self.f.state,first['id']);self.assertEqual(sent.call_count,0)
            control.advance(self.f.config,self.f.state,urgent['id']);self.assertEqual(sent.call_count,1)
            control.advance(self.f.config,self.f.state,first['id']);self.assertEqual(sent.call_count,1)
        self.assertEqual({j['work_id']:j['phase'] for j in control.requests(self.f.state)}, {'LOW':'pending','HIGH':'received'})

    def test_dependency_deferred_does_not_count_as_completed(self):
        self.f.dispatched()
        first=control.enqueue(self.f.config,self.f.state,'alpha',self.message,work_id='FIRST')
        path=control.folder(self.f.state)/(first['id']+'.json');value=context.read(path);value['phase']='deferred';context.write(path,value)
        second=control.enqueue(self.f.config,self.f.state,'alpha',self.message,work_id='SECOND',dependencies=[first['id']])
        with patch.object(control,'official') as sent:
            control.advance(self.f.config,self.f.state,second['id'])
        sent.assert_not_called()
        value=context.read(control.folder(self.f.state)/(second['id']+'.json'))
        self.assertEqual(value['phase'],'pending');self.assertTrue(value.get('reason'))

    def test_changed_approved_document_waits_before_native_transport(self):
        self.f.dispatched();self.work()
        job=control.enqueue(self.f.config,self.f.state,'alpha',self.message,work_id='LINK',workflow_id='WORK-1')
        private=self.f.root/'plans'/'WORK-1'/'r1'/'plan.md';private.write_text(private.read_text()+'\nChanged after approval.')
        with patch.object(control,'official') as sent:
            control.advance(self.f.config,self.f.state,job['id'])
        sent.assert_not_called()
        value=context.read(control.folder(self.f.state)/(job['id']+'.json'))
        self.assertEqual(value['phase'],'pending');self.assertTrue(value.get('reason'))

    def test_cli_exposes_linked_queue_contract(self):
        from teamkit.__main__ import parser
        parsed=parser().parse_args(['pool','assign','--team','alpha','--message-file','message.txt',
            '--work','ITEM','--workflow','WORK-1','--priority','3','--depends-on','a'*32,'--resource',str(self.f.repo)])
        self.assertEqual(parsed.workflow,'WORK-1');self.assertEqual(parsed.priority,3)
        parsed=parser().parse_args(['team-dispatch','status'])
        self.assertEqual(parsed.command,'team-dispatch')
