import json
from pathlib import Path
import tempfile
import unittest
import sys

from teamkit.operations import tick
from teamkit.tickets import TicketStore


class NativeFixture:
    """Only HC delivery is replaced; files, SQLite, verifiers and queue loop are real."""
    def __init__(self, state):
        self.state = state
        self.sent = []
        self.crash_after_receipt = False
        self.executor = dict(terminal='t1', pid=42, owner='fixture-owner', provider='codex',
                             account='personal', product='example', roles=['investigator'])

    def executors(self, ticket=None):
        return [self.executor]

    def admit(self, ticket, candidate):
        return candidate

    def observation(self):
        import time
        return dict(observed_at=time.time(), complete=True, terminals=[])

    def native(self, executor, require_idle=True):
        return dict(terminal='t1', pid=42, owner='fixture-owner', status='idle')

    def send(self, executor, text):
        self.sent.append(text)
        queue = TicketStore(self.state)
        try:
            item = next(t for t in queue.list() if t['state'] == 'dispatching')
            attempt = queue.attempt(item['attempt'])
            queue.receipt(item['id'], attempt['id'], executor['owner'], 't1', self.native(executor))
            report = self.state.parent / (item['id'] + '-report.txt')
            report.write_text('Verified fixture result for ' + item['id'])
            queue.submit(item['id'], attempt['id'], executor['owner'],
                         dict(artifact=str(report), writer_stopped=True, usage_tokens=5))
            if self.crash_after_receipt:
                raise SystemExit('consumer terminated after durable actor receipt')
        finally:
            queue.close()
        return {'accepted': True}


class OperationsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state.sqlite'
        self.approval = self.root / 'approval.txt'
        self.approval.write_text('User-approved isolated queue verification')
        self.config = self.root / 'config.json'
        profiles = {p: {a: dict(home=str(self.root / (p + '-' + a)),
                                model='fixture', identity={'email': 'fixture@example.invalid'})
                        for a in ('team', 'personal')} for p in ('codex', 'claude')}
        self.config.write_text(json.dumps(dict(version=1, profiles=profiles, quota_ttl_seconds=120,
            teams={'example': dict(root=str(self.root), roles={'leader':dict(provider='codex', model='fixture')})},
            operations={'enabled':True})))
        self.runtime = NativeFixture(self.state)

    def enqueue(self, ident, dependencies=None, **changes):
        queue = TicketStore(self.state)
        try:
            spec = dict(id=ident, product='example', role='investigator', kind='investigation',
                goal='Inspect only the test fixture', completion=['nonempty report'], checks=['report'],
                verification_commands={'report':[sys.executable, '-c',
                    'import pathlib,sys;sys.exit(not pathlib.Path(sys.argv[1]).read_text().startswith("Verified fixture"))', '{artifact}']},
                profiles=['codex/personal'], priority=0, dependencies=dependencies or [],
                scope=dict(paths=[str(self.root)], actions=['read'], data=['fixture']),
                resources=[dict(kind='path', name=str(self.root), mode='read')],
                approval=dict(state='approved', reference=str(self.approval)))
            spec.update(changes)
            return queue.enqueue(spec, ident, user_approved=True)
        finally:
            queue.close()

    def test_temporary_launch_keeps_routing_metadata_without_app_pane_optin(self):
        from teamkit.operations import NativeRuntime
        from unittest.mock import patch
        config=json.loads(self.config.read_text())
        config['operations']['spaces']={'example':'space-example'}
        runtime=NativeRuntime(config,self.state)
        executor=dict(self.runtime.executor,operation_actor=True)
        calls=[]
        with patch('teamkit.workers.launch',return_value=executor), \
             patch('teamkit.core_runtime._call',side_effect=lambda *args,**kw:calls.append(args) or {}), \
             patch.object(runtime,'admit',return_value=executor), \
             patch.object(runtime,'native',return_value={'turn':0}), \
             patch('teamkit.operations.time.sleep'):
            runtime.launch(self.config,{'id':'ONE','role':'investigator','product':'example'}, {'id':'attempt-one'})
        labels=calls[0][2:]
        self.assertIn('operations.product=example',labels)
        self.assertIn('operations.space=space-example',labels)
        self.assertIn('operations.presentation=background',labels)
        self.assertFalse(any(label.startswith(('herdr.space=','herdr.tab=')) for label in labels))

    def test_completed_operation_actor_precedes_fresh_plan_with_same_profile(self):
        from teamkit.operations import NativeRuntime
        from unittest.mock import patch
        self.runtime.executor['operation_actor']=True
        self.enqueue('ONE')
        tick(self.config,self.state,self.runtime)
        tick(self.config,self.state,self.runtime)
        ticket=self.enqueue('TWO')
        runtime=NativeRuntime(json.loads(self.config.read_text()),self.state)
        planned=dict(self.runtime.executor,owner='new-owner',terminal=None,pid=None,new=True)
        with patch('teamkit.session_pool._read',return_value={'slots':{}}), \
             patch('teamkit.workers.plans',return_value=[planned]), \
             patch('teamkit.workers._policy_task',return_value=None), \
             patch('teamkit.routing.execution_candidates',return_value=[{'provider':'codex','account':'personal'}]):
            candidates=runtime.executors(ticket)
        self.assertEqual([self.runtime.executor['owner'],'new-owner'],[item['owner'] for item in candidates])

    def test_result_submission_completion_and_next_ticket(self):
        self.enqueue('ONE')
        self.enqueue('TWO', ['ONE'])
        tick(self.config, self.state, self.runtime)
        tick(self.config, self.state, self.runtime)
        tick(self.config, self.state, self.runtime)
        queue = TicketStore(self.state)
        try:
            self.assertEqual(['completed', 'completed'], [x['state'] for x in queue.list()])
            self.assertEqual(2, len(self.runtime.sent))
        finally:
            queue.close()

    def test_consumer_exit_after_receipt_does_not_redeliver(self):
        self.enqueue('ONE')
        self.runtime.crash_after_receipt = True
        with self.assertRaises(SystemExit):
            tick(self.config, self.state, self.runtime)
        self.runtime.crash_after_receipt = False
        tick(self.config, self.state, self.runtime)
        self.assertEqual(1, len(self.runtime.sent))
        queue = TicketStore(self.state)
        try:
            self.assertEqual('completed', queue.get('ONE')['state'])
        finally:
            queue.close()

    def test_busy_candidate_does_not_abort_other_eligible_candidates(self):
        from teamkit.runtime import RuntimeError as NativeError
        self.enqueue('ONE')
        original = self.runtime.admit
        self.runtime.executors = lambda ticket=None: [dict(self.runtime.executor, busy=True), self.runtime.executor]
        def admit(ticket, candidate):
            if candidate.get('busy'):
                raise NativeError('Core agent is not idle and ready')
            return original(ticket, candidate)
        self.runtime.admit = admit
        tick(self.config, self.state, self.runtime)
        self.assertEqual(1, len(self.runtime.sent))

    def test_invalid_ticket_planning_does_not_stop_other_work(self):
        self.enqueue('BAD', kind='code', priority=10)
        self.enqueue('GOOD')
        def executors(ticket):
            if ticket['id']=='BAD':
                raise ValueError('code/integration ticket requires the existing pinned work approval')
            return [self.runtime.executor]
        self.runtime.executors=executors
        tick(self.config, self.state, self.runtime)
        store=TicketStore(self.state)
        try:
            self.assertIn('pinned work',store.get('BAD')['reason'])
            self.assertEqual('awaiting_verification',store.get('GOOD')['state'])
        finally: store.close()

    def test_warm_executor_cannot_ignore_explicit_account_constraint(self):
        from unittest.mock import patch
        from teamkit.operations import NativeRuntime
        self.enqueue('ONE', execution_constraint={'provider':'codex','account':'personal'})
        store=TicketStore(self.state)
        try: ticket=store.get('ONE')
        finally: store.close()
        config=json.loads(self.config.read_text())
        config['teams']['example']['roles']['leader']={'provider':'claude','model':'fixture','effort':'medium'}
        runtime=NativeRuntime(config,self.state)
        executor=dict(self.runtime.executor,provider='claude',account='team',roles=['investigator'])
        with patch.object(runtime,'native',return_value={}):
            with self.assertRaisesRegex(ValueError,'outside the existing role policy'):
                runtime.admit(ticket,executor)

    def test_final_native_observation_cannot_outlive_approval_or_configuration(self):
        for changed in ('approval','configuration'):
            with self.subTest(changed=changed):
                self.enqueue(changed.upper())
                original=self.runtime.native
                def native(executor,require_idle=True):
                    if changed=='approval':
                        self.approval.write_text('withdrawn grant')
                    else:
                        config=json.loads(self.config.read_text());config['operations']['enabled']=False
                        self.config.write_text(json.dumps(config))
                    return original(executor,require_idle)
                self.runtime.native=native
                tick(self.config,self.state,self.runtime)
                self.assertEqual([],self.runtime.sent)
                self.runtime.native=original
                self.approval.write_text('User-approved isolated queue verification')
                config=json.loads(self.config.read_text());config['operations']['enabled']=True
                self.config.write_text(json.dumps(config))

    def test_legacy_invalid_source_isolated_from_other_ticket(self):
        from unittest.mock import patch
        from teamkit.tickets import file_digest
        self.enqueue('BAD',kind='code')
        self.enqueue('GOOD')
        store=TicketStore(self.state)
        try:
            attempt=store.reserve('BAD',self.runtime.executor,{},self.runtime.observation())
            artifact=self.root/'historical-report.txt';artifact.write_text('Historical candidate')
            # Reproduce a pre-validation durable row; the new submit path rejects it.
            with store.transaction():
                attempt['submission']=dict(artifact=str(artifact),artifact_digest=file_digest(str(artifact)),
                                           writer_stopped=True,source='HEAD')
                store._save_attempt(attempt)
                ticket=store.get('BAD');ticket['state']='awaiting_verification'
                store._save(ticket,'historical_fixture')
        finally:store.close()
        with patch('teamkit.operations.work_gate',return_value=None):
            tick(self.config,self.state,self.runtime)
        store=TicketStore(self.state)
        try:
            self.assertIn('typed source',store.get('BAD')['reason'])
            self.assertEqual('awaiting_verification',store.get('GOOD')['state'])
        finally:store.close()

    def test_review_failure_repair_reverification_and_independent_review(self):
        self.enqueue('ONE', review_required=True)
        original_send = self.runtime.send
        reviewer = dict(self.runtime.executor, owner='review-owner', terminal='t2', pid=43)
        self.runtime.executors = lambda ticket: [reviewer if ticket['role']=='reviewer' else self.runtime.executor]
        self.runtime.native = lambda executor, require_idle=True: dict(executor, status='idle')
        review_count = []
        def send(executor, text):
            store = TicketStore(self.state)
            try:
                ticket = next(t for t in store.list() if t['state']=='dispatching')
                if ticket['role'] != 'reviewer':
                    return original_send(executor, text)
                attempt = store.attempt(ticket['attempt'])
                store.receipt(ticket['id'], attempt['id'], executor['owner'], executor['terminal'], self.runtime.native(executor))
                parent = store.attempt(ticket['parent_attempt'])
                review_count.append(ticket['id'])
                first = len(review_count)==1
                report = self.root / (ticket['id']+'.json')
                report.write_text(json.dumps(dict(verdict='fail' if first else 'pass',
                    artifact_digest=parent['submission']['artifact_digest'], source=parent['submission'].get('source'),
                    findings=[dict(blocking=True, problem='fixture defect', evidence='bounded artifact', impact='completion incorrect')] if first else [])))
                store.submit(ticket['id'], attempt['id'], executor['owner'],
                             dict(artifact=str(report), writer_stopped=True))
                return {'accepted':True}
            finally:
                store.close()
        self.runtime.send = send
        for _ in range(12):
            tick(self.config, self.state, self.runtime)
        store = TicketStore(self.state)
        try:
            parent = store.get('ONE')
            self.assertEqual('completed', parent['state'], parent.get('reason'))
            self.assertEqual('review-owner', parent['reviewer']['owner'])
            self.assertEqual(2, len(review_count))
            self.assertTrue(any(t['relation']=='defect' for t in store.list()))
            self.assertTrue(all(t['state']=='completed' for t in store.list()))
        finally:
            store.close()


if __name__ == '__main__':
    unittest.main()
