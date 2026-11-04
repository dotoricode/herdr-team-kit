"""Public queue ingress, cancellation and concurrent admission."""
import json
import unittest
import time
from unittest.mock import patch
import test_operations as fixtures
from teamkit import pool_control
from teamkit.__main__ import parser, run
from teamkit.operations import tick
from teamkit.tickets import TicketStore
from teamkit.resources import ResourceLedger


class UnifiedQueueTests(unittest.TestCase):
    setUp = fixtures.OperationsTest.setUp
    enqueue = fixtures.OperationsTest.enqueue

    def test_pool_assign_without_static_pool_enqueues_one_sqlite_ticket_idempotently(self):
        message = self.root/'request.txt'; message.write_text('Read fixture and report only.')
        job = pool_control.enqueue(self.config,self.state,'example',message,work_id='WORK-ONE')
        queue = TicketStore(self.state); self.addCleanup(queue.close)
        self.assertEqual(job['id'],queue.list()[0]['id'])
        self.assertEqual('pending',job['state'])
        self.assertEqual(job,pool_control.enqueue(self.config,self.state,'example',message,work_id='WORK-ONE'))
        self.assertEqual([],pool_control.requests(self.state))
        message.write_text('A different request')
        with self.assertRaisesRegex(ValueError,'immutable|conflict'):
            pool_control.enqueue(self.config,self.state,'example',message,work_id='WORK-ONE')

    def test_three_same_team_reads_start_in_one_tick_and_only_same_path_writes_wait(self):
        config=json.loads(self.config.read_text()); config['operations']['resource_policy']={'global_limit':10,'profile_limits':{'codex/personal':10}}
        self.config.write_text(json.dumps(config))
        for ident in ('A','B','C'):self.enqueue(ident)
        actors = [dict(self.runtime.executor, terminal='t'+str(i+1),pid=42+i,owner='owner-'+str(i)) for i in range(3)]
        self.runtime.executors=lambda ticket=None:actors
        self.runtime.native=lambda actor,require_idle=True:dict(actor,status='idle',turn=1)
        self.runtime.send=lambda actor,text:dict(accepted=True,terminal=actor['terminal'],native_pid=actor['pid'],turn=1,turn_baseline=1)
        result=tick(self.config,self.state,self.runtime)
        self.assertEqual(3,len(result['started']))
        queue=TicketStore(self.state);self.addCleanup(queue.close)
        self.assertEqual({'t1','t2','t3'},{x['executor']['terminal'] for x in queue.list()})
        for ident,path in (('W1','same'),('W2','other'),('W3','same')):
            self.enqueue(ident,scope=dict(paths=[str(self.root)],actions=['read','write'],data=['fixture']),
                resources=[dict(kind='path',name=str(self.root/path),mode='write')],workflow_ref={'root':str(self.root)})
        # Work approval is tested independently; this admission check exercises the real SQLite path leases.
        observation=dict(observed_at=time.time(),complete=True,terminals=[])
        queue.reserve('W1',dict(actors[0],terminal='t4'),{'global_limit':10,'profile_limits':{'codex/personal':10}},observation)
        queue.reserve('W2',dict(actors[1],terminal='t5'),{'global_limit':10,'profile_limits':{'codex/personal':10}},observation)
        with self.assertRaisesRegex(ValueError,'conflicting resource'):
            queue.reserve('W3',dict(actors[2],terminal='t6'),{'global_limit':10,'profile_limits':{'codex/personal':10}},observation)

    def test_cli_cancel_preserves_history_and_rejects_any_executed_or_reserved_work(self):
        self.enqueue('CANCEL')
        args=lambda *extra:parser().parse_args(['--config',str(self.config),'--state',str(self.state),'queue','cancel','CANCEL',*extra])
        with self.assertRaises(ValueError):run(args('--reason','wrong assignment'))
        result=run(args('--reason','wrong assignment','--user-approved'))
        self.assertEqual('cancelled',result['state'])
        queue=TicketStore(self.state);self.addCleanup(queue.close)
        self.assertEqual(0,queue.get('CANCEL')['attempt_count'])
        events=[(r[0],json.loads(r[1])) for r in queue.db.execute('SELECT event,body FROM operation_events WHERE ticket=?',('CANCEL',))]
        self.assertTrue(any(event=='cancelled' and body['reason']=='wrong assignment' for event,body in events))
        self.enqueue('BUSY')
        attempt=queue.reserve('BUSY',self.runtime.executor,{},self.runtime.observation())
        with self.assertRaises(ValueError):queue.cancel('BUSY','operator request',True)
        self.assertTrue(ResourceLedger._rows(queue.db)[0]['execution_reserved'])
