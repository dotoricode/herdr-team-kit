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

    def executors(self):
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

    def enqueue(self, ident, dependencies=None):
        queue = TicketStore(self.state)
        try:
            return queue.enqueue(dict(id=ident, product='example', role='investigator', kind='investigation',
                goal='Inspect only the test fixture', completion=['nonempty report'], checks=['report'],
                verification_commands={'report':[sys.executable, '-c',
                    'import pathlib,sys;sys.exit(not pathlib.Path(sys.argv[1]).read_text().startswith("Verified fixture"))', '{artifact}']},
                profiles=['codex/personal'], priority=0, dependencies=dependencies or [],
                scope=dict(paths=[str(self.root)], actions=['read'], data=['fixture']),
                resources=[dict(kind='path', name=str(self.root), mode='read')],
                approval=dict(state='approved', reference=str(self.approval))), ident)
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


if __name__ == '__main__':
    unittest.main()
