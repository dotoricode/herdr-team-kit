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
