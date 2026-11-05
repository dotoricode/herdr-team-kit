import concurrent.futures
import json
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

import test_operations as operation_fixtures
from teamkit import queue_actions as actions
from teamkit.tickets import TicketStore, digest


class QueueActionsTest(unittest.TestCase):
    setUp = operation_fixtures.OperationsTest.setUp
    enqueue = operation_fixtures.OperationsTest.enqueue

    def test_durable_enqueue_and_concurrent_clicks_send_once(self):
        self.enqueue('ONE')
        choices = actions.candidates(self.config, self.state, 'ONE', runtime=self.runtime)
        candidate = choices['candidates'][0]
        self.assertTrue(candidate['eligible'])
        def click(_):
            return actions.enqueue(self.config, self.state, 'send', 'click-one', ticket='ONE',
                candidate_id=candidate['id'], expected_ticket=choices['expected_ticket'])
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            results = list(pool.map(click, range(4)))
        self.assertEqual({'queued'}, {r['status'] for r in results})
        self.assertEqual([], self.runtime.sent)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda _: actions.consume(self.config, self.state, runtime=self.runtime), range(2)))
        self.assertEqual(1, len(self.runtime.sent))
        self.assertEqual('completed', actions.status(self.state, 'click-one')['status'])
        self.assertEqual('completed', click(None)['status'])

    def test_distinct_clicks_cannot_replace_unfinished_selection(self):
        self.enqueue('ONE')
        choices=actions.candidates(self.config,self.state,'ONE',runtime=self.runtime)
        for key in ('click-a','click-b'):
            if key == 'click-b':
                rejected=actions.enqueue(self.config,self.state,'send',key,ticket='ONE',
                    candidate_id=choices['candidates'][0]['id'],expected_ticket=choices['expected_ticket'])
                self.assertEqual('blocked',rejected['status'])
                self.assertIn('previous selected action',rejected['reason'])
            else:
                actions.enqueue(self.config,self.state,'send',key,ticket='ONE',
                    candidate_id=choices['candidates'][0]['id'],expected_ticket=choices['expected_ticket'])
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda _:actions.consume(self.config,self.state,runtime=self.runtime),range(2)))
        self.assertEqual(1,len(self.runtime.sent))
        q=TicketStore(self.state)
        try:self.assertEqual(1,len(list(q.db.execute('SELECT * FROM operation_attempts'))))
        finally:q.close()

    def test_manual_hold_committed_during_automatic_admission_blocks_reserve(self):
        from teamkit.operations import tick
        self.enqueue('ONE')
        choice=actions.candidates(self.config,self.state,'ONE',runtime=self.runtime)
        admitted=threading.Event()
        released=threading.Event()
        original=self.runtime.admit
        def paused(ticket,candidate):
            admitted.set()
            self.assertTrue(released.wait(timeout=5))
            return original(ticket,candidate)
        self.runtime.admit=paused
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            future=pool.submit(tick,self.config,self.state,self.runtime)
            self.assertTrue(admitted.wait(timeout=5))
            actions.enqueue(self.config,self.state,'send','racing-manual',ticket='ONE',
                candidate_id=choice['candidates'][0]['id'],expected_ticket=choice['expected_ticket'])
            released.set()
            future.result(timeout=5)
        q=TicketStore(self.state)
        try:
            self.assertEqual('pending',q.get('ONE')['state'])
            self.assertEqual([],list(q.db.execute('SELECT * FROM operation_attempts')))
        finally:q.close()
        self.assertEqual([],self.runtime.sent)
        # The automatic gate's diagnostic changed the preview revision, so the
        # original manual action also blocks instead of silently relaxing CAS.
        actions.consume(self.config,self.state,runtime=self.runtime)
        self.assertEqual('blocked',actions.status(self.state,'racing-manual')['status'])
        tick(self.config,self.state,self.runtime)
        self.assertEqual([],self.runtime.sent)

    def test_existing_durable_selection_is_held_without_new_metadata(self):
        from teamkit.operations import tick
        self.enqueue('ONE')
        choice=actions.candidates(self.config,self.state,'ONE',runtime=self.runtime)
        actions.enqueue(self.config,self.state,'send','old-send',ticket='ONE',
            candidate_id=choice['candidates'][0]['id'],expected_ticket=choice['expected_ticket'])
        q=TicketStore(self.state)
        try:q.db.execute('DROP TABLE queue_action_holds')
        finally:q.close()
        tick(self.config,self.state,self.runtime)
        self.assertEqual([],self.runtime.sent)
        actions.consume(self.config,self.state,runtime=self.runtime)
        self.assertEqual(1,len(self.runtime.sent))

    def test_stale_enqueue_is_durably_blocked_and_keeps_manual_intent(self):
        from teamkit.operations import tick
        self.enqueue('ONE')
        choice=actions.candidates(self.config,self.state,'ONE',runtime=self.runtime)
        q=TicketStore(self.state)
        try:q.block('ONE','fixture reason changed')
        finally:q.close()
        result=actions.enqueue(self.config,self.state,'send','stale-enqueue',ticket='ONE',
            candidate_id=choice['candidates'][0]['id'],expected_ticket=choice['expected_ticket'])
        self.assertEqual('blocked',result['status'])
        self.assertEqual('blocked',actions.status(self.state,'stale-enqueue')['status'])
        tick(self.config,self.state,self.runtime)
        self.assertEqual([],self.runtime.sent)

    def test_no_approval_and_quota_resources_remain_candidate_gates(self):
        self.enqueue('ONE')
        self.runtime.admit=lambda *args: (_ for _ in ()).throw(ValueError('profile quota unavailable'))
        choices=actions.candidates(self.config,self.state,'ONE',runtime=self.runtime)
        self.assertFalse(choices['send_allowed'])
        self.assertIn('quota',choices['candidates'][0]['reason'])
        self.approval.write_text('Changed approval')
        self.assertFalse(actions.candidates(self.config,self.state,'ONE',runtime=self.runtime)['send_allowed'])
        self.assertEqual([],self.runtime.sent)

    def test_supervisor_and_sync_preserve_failed_manual_selection_hold(self):
        from teamkit import context_handoff
        self.enqueue('ONE')
        self.enqueue('TWO')
        choices=actions.candidates(self.config,self.state,'ONE',runtime=self.runtime)
        actions.enqueue(self.config,self.state,'send','held-send',ticket='ONE',
            candidate_id=choices['candidates'][0]['id'],expected_ticket=choices['expected_ticket'])
        self.runtime.executor=dict(self.runtime.executor,owner='different-owner',terminal='t2',pid=43)
        delivered=[]
        def send(executor,text):
            delivered.append(text)
            return {'accepted':True}
        self.runtime.send=send
        from teamkit import operations
        actual_tick=operations.tick
        with patch.object(context_handoff,'settings',side_effect=lambda config:{'enabled':config.get('_consumer_fixture') is True}), \
             patch.object(context_handoff,'load',return_value={'_consumer_fixture':True,'runtime_backend':'core','operations':{'enabled':True}}), \
             patch('teamkit.admission.reconcile_stopped'), \
             patch.object(actions,'NativeRuntime',return_value=self.runtime), \
             patch.object(operations,'tick',side_effect=lambda config,state,**kw:actual_tick(config,state,
                 **dict(kw,runtime=self.runtime))):
            context_handoff._operations_tick(self.config,self.state)
            self.assertEqual('blocked',actions.status(self.state,'held-send')['status'])
            q=TicketStore(self.state)
            try:
                self.assertEqual('pending',q.get('ONE')['state'])
                self.assertEqual('received',q.get('TWO')['state'])
            finally:q.close()
            context_handoff._operations_tick(self.config,self.state)
            actions.enqueue(self.config,self.state,'sync','held-sync')
            context_handoff._operations_tick(self.config,self.state)
        self.assertEqual(1,len(delivered))
        self.assertIn('"id": "TWO"',delivered[0])
        # Fresh preview and a new action explicitly replace the failed selection.
        self.runtime.executor=dict(self.runtime.executor,owner='fresh-owner',terminal='t3',pid=44)
        choice=actions.candidates(self.config,self.state,'ONE',runtime=self.runtime)
        self.assertTrue(choice['send_allowed'])
        actions.enqueue(self.config,self.state,'send','reselected',ticket='ONE',
            candidate_id=choice['candidates'][0]['id'],expected_ticket=choice['expected_ticket'])
        self.assertEqual('queued',actions.status(self.state,'reselected')['status'])
        actions.consume(self.config,self.state,runtime=self.runtime)
        self.assertEqual('completed',actions.status(self.state,'reselected')['status'])
        self.assertEqual(2,len(delivered))

    def test_stale_ticket_or_executor_never_falls_back(self):
        for changed in ('ticket', 'executor'):
            with self.subTest(changed=changed):
                ident = changed.upper()
                self.enqueue(ident)
                choices = actions.candidates(self.config, self.state, ident, runtime=self.runtime)
                actions.enqueue(self.config, self.state, 'send', changed, ticket=ident,
                    candidate_id=choices['candidates'][0]['id'], expected_ticket=choices['expected_ticket'])
                if changed == 'ticket':
                    q=TicketStore(self.state)
                    with q.transaction():
                        ticket=q.get(ident);ticket['goal']='Changed goal';q._save(ticket,'fixture_changed')
                    q.close()
                else:
                    self.runtime.executor['pid'] += 1
                actions.consume(self.config, self.state, runtime=self.runtime)
                self.assertEqual('blocked', actions.status(self.state, changed)['status'])
        self.assertEqual([], self.runtime.sent)

    def test_uncertain_never_resends_and_sync_reports_actual_finalization(self):
        self.enqueue('ONE')
        choice=actions.candidates(self.config,self.state,'ONE',runtime=self.runtime)
        original=self.runtime.send
        self.runtime.send=lambda *args: (_ for _ in ()).throw(TimeoutError('unknown'))
        actions.enqueue(self.config,self.state,'send','send-one',ticket='ONE',
            candidate_id=choice['candidates'][0]['id'],expected_ticket=choice['expected_ticket'])
        actions.consume(self.config,self.state,runtime=self.runtime)
        self.assertEqual('uncertain',actions.status(self.state,'send-one')['status'])
        self.assertFalse(actions.candidates(self.config,self.state,'ONE',runtime=self.runtime)['send_allowed'])
        self.runtime.send=original
        actions.enqueue(self.config,self.state,'sync','sync-one')
        actions.consume(self.config,self.state,runtime=self.runtime)
        result=actions.status(self.state,'sync-one')['result']
        self.assertEqual([],result['dispatch']['started'])
        self.assertEqual('uncertain',result['tickets']['ONE']['after'])
        self.assertEqual(0,len(self.runtime.sent))

    def test_sync_reports_dispatch_and_verified_completion_separately(self):
        self.enqueue('TWO')
        actions.enqueue(self.config,self.state,'sync','sync-one')
        actions.consume(self.config,self.state,runtime=self.runtime)
        first=actions.status(self.state,'sync-one')['result']
        self.assertEqual(['TWO'],[x['ticket'] for x in first['dispatch']['started']])
        self.assertEqual([],first['completed'])
        actions.enqueue(self.config,self.state,'sync','sync-two')
        actions.consume(self.config,self.state,runtime=self.runtime)
        self.assertEqual('completed',actions.status(self.state,'sync-two')['result']['tickets']['TWO']['after'])
        self.assertEqual(1,len(self.runtime.sent))

    def test_consumer_crash_after_actor_receipt_keeps_uncertain_action(self):
        self.enqueue('ONE')
        choice=actions.candidates(self.config,self.state,'ONE',runtime=self.runtime)
        actions.enqueue(self.config,self.state,'send','crash-one',ticket='ONE',
            candidate_id=choice['candidates'][0]['id'],expected_ticket=choice['expected_ticket'])
        self.runtime.crash_after_receipt=True
        with self.assertRaises(SystemExit):
            actions.consume(self.config,self.state,runtime=self.runtime)
        self.assertEqual('uncertain',actions.status(self.state,'crash-one')['status'])
        self.runtime.crash_after_receipt=False
        actions.consume(self.config,self.state,runtime=self.runtime)
        self.assertEqual(1,len(self.runtime.sent))

    def test_action_and_outbox_exist_before_native_send(self):
        self.enqueue('ONE')
        choice=actions.candidates(self.config,self.state,'ONE',runtime=self.runtime)
        actions.enqueue(self.config,self.state,'send','persist-first',ticket='ONE',
            candidate_id=choice['candidates'][0]['id'],expected_ticket=choice['expected_ticket'])
        original=self.runtime.send
        def observed(executor,text):
            self.assertEqual('running',actions.status(self.state,'persist-first')['status'])
            q=TicketStore(self.state)
            try:
                ticket=q.get('ONE')
                self.assertEqual('dispatching',ticket['state'])
                self.assertEqual('reserved',q.attempt(ticket['attempt'])['phase'])
            finally:q.close()
            return original(executor,text)
        self.runtime.send=observed
        actions.consume(self.config,self.state,runtime=self.runtime)
        self.assertEqual(1,len(self.runtime.sent))

    def test_actual_cli_candidate_send_and_poll_fixture(self):
        self.enqueue('ONE')
        adapter="""import sys,runpy
from pathlib import Path
sys.path.insert(0,'tests')
from test_operations import NativeFixture
from teamkit import queue_actions
queue_actions.NativeRuntime=lambda config,state: NativeFixture(Path(state))
sys.argv=['teamkit_cli.py',*sys.argv[1:]]
runpy.run_path('teamkit_cli.py',run_name='__main__')
"""
        argv=[sys.executable,'-c',adapter,'--config',str(self.config),'--state',str(self.state),'queue']
        result=subprocess.run(argv+['candidates','ONE'],capture_output=True,text=True)
        self.assertEqual(0,result.returncode,result.stderr)
        choice=json.loads(result.stdout)
        self.assertTrue(choice['send_allowed'])
        send=argv+['send','ONE','--client-request-id','cli-send','--candidate-id',choice['candidates'][0]['id'],
                   '--expected-ticket',choice['expected_ticket']]
        result=subprocess.run(send,capture_output=True,text=True)
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertEqual('queued',json.loads(result.stdout)['status'])
        self.assertEqual([],self.runtime.sent)
        actions.consume(self.config,self.state,runtime=self.runtime)
        result=subprocess.run(argv+['action-status','--client-request-id','cli-send'],capture_output=True,text=True)
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertEqual('completed',json.loads(result.stdout)['status'])
        self.assertEqual(1,len(self.runtime.sent))

    def test_real_cli_enqueue_status_without_native_or_send(self):
        self.enqueue('ONE')
        argv=[sys.executable,'teamkit_cli.py','--config',str(self.config),'--state',str(self.state),'queue']
        result=subprocess.run(argv+['sync','--client-request-id','cli-sync'],capture_output=True,text=True)
        self.assertEqual(0,result.returncode,result.stderr)
        payload=json.loads(result.stdout)
        self.assertEqual('queued',payload['status'])
        result=subprocess.run(argv+['action-status','--client-request-id','cli-sync'],capture_output=True,text=True)
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertEqual('queued',json.loads(result.stdout)['status'])

    def test_reserve_cas_inside_transaction(self):
        ticket=self.enqueue('ONE')
        q=TicketStore(self.state)
        try:
            with self.assertRaisesRegex(ValueError,'changed'):
                q.reserve('ONE',self.runtime.executor,{},self.runtime.observation(),expected_revision='stale')
            self.assertEqual('pending',q.get('ONE')['state'])
            self.assertEqual([],list(q.db.execute('SELECT * FROM operation_attempts')))
        finally:q.close()
