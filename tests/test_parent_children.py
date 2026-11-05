import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from teamkit.operations import tick
from teamkit.resources import ResourceLedger
from teamkit.tickets import TicketStore


class ParentChildrenTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state.sqlite'
        self.approval = self.root / 'approval.txt'
        self.approval.write_text('User-approved isolated parent and child regression.')
        self.queue = TicketStore(self.state)
        self.addCleanup(self.queue.close)
        original_run = subprocess.run
        def local_only(argv, *args, **kwargs):
            if Path(argv[0]).name in ('hc', 'herdr', 'herdr-app', 'codex', 'claude'):
                raise AssertionError('Native interaction is forbidden in this isolated regression')
            return original_run(argv, *args, **kwargs)
        blocker = patch('subprocess.run', side_effect=local_only)
        blocker.start(); self.addCleanup(blocker.stop)
        blocker = patch('teamkit.core_runtime._call', side_effect=AssertionError('HC is forbidden'))
        blocker.start(); self.addCleanup(blocker.stop)

    def spec(self, ident='PARENT', **changes):
        value = dict(id=ident, product='same-team', role='investigator', kind='investigation',
            goal='Inspect only the isolated fixture', completion=['verified report'], checks=['report'],
            verification_commands={'report': [sys.executable, '-c',
                'import pathlib,sys;sys.exit(not pathlib.Path(sys.argv[1]).read_text().startswith("Verified fixture"))', '{artifact}']},
            profiles=['codex/personal'], priority=0, dependencies=[],
            resources=[dict(kind='path', name=str(self.root / 'source'), mode='write')],
            scope=dict(paths=[str(self.root)], actions=['read', 'write'], data=['fixture']),
            approval=dict(state='approved', reference=str(self.approval)),
            budget=dict(tickets=5, attempts=8, tokens=10000), estimated_tokens=100)
        value.update(changes)
        return value

    def executor(self):
        return dict(terminal='tfixture', pid=42, owner='isolated-owner',
                    provider='codex', account='personal')

    def observation(self):
        return dict(observed_at=time.time(), complete=True, terminals=[])

    def parent_and_child(self, mode='read', disjoint=False):
        self.queue.enqueue(self.spec(), 'parent', user_approved=True)
        executor = self.executor()
        attempt = self.queue.reserve('PARENT', executor, {}, self.observation())
        self.queue.receipt('PARENT', attempt['id'], executor['owner'], executor['terminal'],
                           dict(executor, status='idle'))
        child = self.spec('CHILD', parent_id='PARENT', relation='required',
            resources=[dict(kind='path', name=str(self.root / ('other' if disjoint else 'source/file')), mode=mode)])
        self.queue.enqueue(child, 'child')
        return executor, attempt

    def test_required_read_child_waits_beside_retained_parent_write(self):
        # A read never conflicts with a write, so the parent keeps its write lease while a reading child runs.
        executor, attempt = self.parent_and_child('read')
        self.queue.request_children_wait('PARENT', attempt['id'], executor['owner'])
        self.queue.wait_children('PARENT', attempt['id'], executor['owner'], True)
        row = ResourceLedger._rows(self.queue.db)[0]
        self.assertFalse(row['execution_reserved'])
        self.assertTrue(row['writes_held'])
        child = self.queue.reserve('CHILD', executor, {}, self.observation())
        self.assertEqual('CHILD', child['ticket'])

    def test_required_write_child_is_rejected_before_stop_request_transition(self):
        executor, attempt = self.parent_and_child('write')
        with self.assertRaisesRegex(ValueError, 'CHILD.*--release-writes'):
            self.queue.request_children_wait('PARENT', attempt['id'], executor['owner'])
        self.assertEqual('running', self.queue.get('PARENT')['state'])
        self.assertEqual('pending', self.queue.get('CHILD')['state'])

    def test_disjoint_required_child_keeps_parent_lease_and_gets_returned_slot(self):
        executor, attempt = self.parent_and_child(disjoint=True)
        self.queue.request_children_wait('PARENT', attempt['id'], executor['owner'])
        self.queue.wait_children('PARENT', attempt['id'], executor['owner'], True)
        row = ResourceLedger._rows(self.queue.db)[0]
        self.assertFalse(row['execution_reserved'])
        self.assertTrue(row['writes_held'])
        child = self.queue.reserve('CHILD', executor, {}, self.observation())
        self.assertEqual('CHILD', child['ticket'])

    def test_child_cannot_reset_the_parent_failure_escalation_policy(self):
        self.queue.enqueue(self.spec(failures=2), 'parent', user_approved=True)
        changed = self.queue.enqueue(self.spec('CHANGED', parent_id='PARENT', failures=0), 'changed')
        self.assertEqual('waiting_approval', changed['state'])
        unchanged = self.queue.enqueue(self.spec('UNCHANGED', parent_id='PARENT', failures=2), 'unchanged')
        self.assertEqual('pending', unchanged['state'])

    def test_same_team_child_finishes_and_parent_resumes_through_service(self):
        profiles = {p: {a: dict(home=str(self.root / (p + '-' + a)), model='fixture',
                              identity={'email': 'fixture@example.invalid'})
                       for a in ('team', 'personal')} for p in ('codex', 'claude')}
        config = self.root / 'config.json'
        config.write_text(json.dumps(dict(version=1, profiles=profiles,
            teams={'same-team': dict(root=str(self.root), roles={'leader': dict(provider='codex', model='fixture')})},
            operations={'enabled': True, 'resource_policy': {'global_limit': 1}})))
        self.queue.enqueue(self.spec(), 'parent', user_approved=True)
        outer = self
        class LocalRuntime:
            sent = []
            def executors(self, ticket=None): return [outer.executor()]
            def admit(self, ticket, candidate): return candidate
            def observation(self): return outer.observation()
            def native(self, executor, require_idle=True): return dict(executor, status='idle')
            def send(self, executor, text):
                queue = TicketStore(outer.state)
                try:
                    item = next(x for x in queue.list() if x['state'] == 'dispatching')
                    attempt = queue.attempt(item['attempt'])
                    self.sent.append((item['id'], attempt['id']))
                    queue.receipt(item['id'], attempt['id'], executor['owner'], executor['terminal'], self.native(executor))
                    if item['id'] == 'PARENT' and attempt['ordinal'] == 1:
                        queue.enqueue(outer.spec('CHILD', parent_id='PARENT', relation='required',
                            resources=[dict(kind='path', name=str(outer.root / 'source/file'), mode='read')]), 'child')
                        queue.request_children_wait('PARENT', attempt['id'], executor['owner'], True)
                    else:
                        report = outer.root / (item['id'] + '-report.txt')
                        report.write_text('Verified fixture result for ' + item['id'])
                        queue.submit(item['id'], attempt['id'], executor['owner'],
                                     dict(artifact=str(report), writer_stopped=True, usage_tokens=5))
                finally:
                    queue.close()
                return {'accepted': True}
        runtime = LocalRuntime()
        tick(config, self.state, runtime)
        self.assertEqual('waiting_children_stop', self.queue.get('PARENT')['state'])
        tick(config, self.state, runtime)
        self.assertEqual('waiting_children', self.queue.get('PARENT')['state'])
        self.assertEqual('awaiting_verification', self.queue.get('CHILD')['state'])
        tick(config, self.state, runtime)
        self.assertEqual('completed', self.queue.get('CHILD')['state'])
        self.assertEqual('awaiting_verification', self.queue.get('PARENT')['state'])
        tick(config, self.state, runtime)
        self.assertEqual('completed', self.queue.get('PARENT')['state'])
        self.assertEqual(['PARENT', 'CHILD', 'PARENT'], [x[0] for x in runtime.sent])
        self.assertEqual(3, len({x[1] for x in runtime.sent}))
        self.assertTrue(all(not x['execution_reserved'] and not x['writes_held']
                            for x in ResourceLedger._rows(self.queue.db)))
