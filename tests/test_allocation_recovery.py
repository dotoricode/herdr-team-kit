import unittest
import test_operations as fixtures
from teamkit.operations import tick, reconcile
from teamkit.tickets import TicketStore

class RecoveryTest(unittest.TestCase):
    setUp = fixtures.OperationsTest.setUp
    enqueue = fixtures.OperationsTest.enqueue
    def test_blocked_submission_requests_help_instead_of_defect_child(self):
        self.enqueue('BLOCKED')
        old_submit=TicketStore.submit
        def submit(store, ident, attempt, owner, result):
            return old_submit(store,ident,attempt,owner,dict(result,status='incomplete',reason='account observation unavailable'))
        from unittest.mock import patch
        with patch.object(TicketStore,'submit',submit): tick(self.config,self.state,self.runtime)
        q=TicketStore(self.state)
        self.addCleanup(q.close)
        reconcile(q,self.runtime,{})
        self.assertEqual(1,len(q.list()))
        self.assertEqual('account observation unavailable',q.get('BLOCKED')['intervention']['reason'])
        self.assertTrue(q.get('BLOCKED')['intervention']['question'])

    def test_turn_ended_without_result_keeps_lease_and_requests_help(self):
        self.enqueue('STOPPED')
        def send(executor,text):
            q=TicketStore(self.state)
            item=q.get('STOPPED');a=q.attempt(item['attempt'])
            q.receipt(item['id'],a['id'],executor['owner'],'t1',dict(self.runtime.native(executor),turn=1));q.close()
            return {'accepted':True}
        self.runtime.send=send
        self.runtime.native=lambda executor,require_idle=True:dict(terminal='t1',pid=42,owner='fixture-owner',status='done',turn=2)
        tick(self.config,self.state,self.runtime)
        q=TicketStore(self.state);self.addCleanup(q.close)
        reconcile(q,self.runtime,{})
        self.assertEqual('running',q.get('STOPPED')['state'])
        self.assertEqual('missing_result',q.get('STOPPED')['intervention']['kind'])
        attempt=q.get('STOPPED')['attempt']
        with self.assertRaises(ValueError):q.recover_intervention('STOPPED',attempt,self.runtime.native({}),False,'operator confirms stop')
        q.recover_intervention('STOPPED',attempt,self.runtime.native({}),True,'operator confirms stop')
        self.assertEqual('pending',q.get('STOPPED')['state'])
        self.assertEqual('abandoned',q.attempt(attempt)['phase'])

    def test_replaced_process_can_be_recovered_without_old_native_match(self):
        self.enqueue('REPLACED')
        def send(executor,text):
            q=TicketStore(self.state)
            item=q.get('REPLACED');a=q.attempt(item['attempt'])
            q.receipt(item['id'],a['id'],executor['owner'],'t1',dict(self.runtime.native(executor),turn=1));q.close()
            return {'accepted':True}
        self.runtime.send=send
        tick(self.config,self.state,self.runtime)
        q=TicketStore(self.state);self.addCleanup(q.close)
        attempt=q.get('REPLACED')['attempt']
        q.process_replaced('REPLACED',attempt)
        self.assertEqual('intervention',q.get('REPLACED')['state'])
        with self.assertRaises(ValueError):q.recover_intervention('REPLACED',attempt,None,False,'operator confirms stop')
        with self.assertRaises(ValueError):q.recover_intervention('REPLACED',attempt,None,True,'  ')
        q.recover_intervention('REPLACED',attempt,None,True,'previous outcome inspected')
        self.assertEqual('pending',q.get('REPLACED')['state'])
        self.assertNotIn('intervention',q.get('REPLACED'))
        self.assertEqual('abandoned',q.attempt(attempt)['phase'])

    def test_parent_recovery_question_clears_after_children_complete(self):
        self.enqueue('PARENT')
        tick(self.config,self.state,self.runtime)
        q=TicketStore(self.state);self.addCleanup(q.close)
        item=q.get('PARENT')
        q.request_intervention('PARENT',item['attempt'],'child_blocked','child needs recovery')
        with q.transaction():
            item=q.get('PARENT');item['state']='waiting_children';q._save(item,'fixture_wait')
        q.resume_parents()
        self.assertEqual('pending',q.get('PARENT')['state'])
        self.assertNotIn('intervention',q.get('PARENT'))
        self.assertEqual('',q.get('PARENT')['reason'])

    def test_late_actor_submission_clears_intervention(self):
        self.enqueue('LATE')
        tick(self.config,self.state,self.runtime)
        q=TicketStore(self.state);self.addCleanup(q.close)
        item=q.get('LATE');attempt=item['attempt']
        q.request_intervention('LATE',attempt,'missing_result','result missing')
        # Repeated identical result is also the actor's reconciliation path.
        result=q.attempt(attempt)['submission'].copy();result.pop('artifact_digest')
        q.submit('LATE',attempt,self.runtime.executor['owner'],result)
        self.assertNotIn('intervention',q.get('LATE'))

    def received_without_submission(self):
        self.enqueue('OBSERVE')
        self.runtime.executor.update(task='fixture',runtime={'name':'fixture'})
        def send(executor,text):
            q=TicketStore(self.state)
            item=q.get('OBSERVE')
            q.receipt(item['id'],item['attempt'],executor['owner'],'t1',dict(self.runtime.native(executor),turn=1))
            q.close()
            return {'accepted':True}
        self.runtime.send=send
        tick(self.config,self.state,self.runtime)
        q=TicketStore(self.state);self.addCleanup(q.close)
        from teamkit.operations import NativeRuntime
        import json
        return q,NativeRuntime(json.loads(self.config.read_text()),self.state)

    def test_owner_process_observation_failure_is_durable_intervention(self):
        q,runtime=self.received_without_submission()
        before=q.get('OBSERVE')['attempt']
        from unittest.mock import patch
        from teamkit.resources import ResourceLedger
        with patch('teamkit.core_runtime.CoreRuntime._native',side_effect=ValueError('Core terminal owner/process changed')):
            reconcile(q,runtime,{})
        ticket=q.get('OBSERVE')
        self.assertEqual('observation_failed',ticket['intervention']['kind'])
        self.assertIn('owner/process changed',ticket['reason'])
        self.assertEqual(before,ticket['attempt'])
        self.assertEqual('running',ticket['state'])
        self.assertTrue(next(x for x in ResourceLedger._rows(q.db) if x['attempt']==before)['execution_reserved'])

    def test_normal_working_is_observed_without_idle_requirement(self):
        q,runtime=self.received_without_submission()
        from unittest.mock import patch
        native=dict(foreground={'pid':42},status={'state':'working'},turn=1)
        with patch('teamkit.core_runtime.CoreRuntime._native',return_value=native) as observed, \
             patch('teamkit.core_runtime._call',side_effect=AssertionError('idle screen must not be read')):
            reconcile(q,runtime,{})
        self.assertFalse(observed.call_args.kwargs['ready'])
        self.assertNotIn('intervention',q.get('OBSERVE'))
        self.assertEqual('',q.get('OBSERVE')['reason'])
