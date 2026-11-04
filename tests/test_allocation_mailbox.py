"""File-only worker protocol; HC transport never writes the queue or submits results."""
import json
from pathlib import Path
import unittest
import test_operations as fixtures
from teamkit.operations import tick
from teamkit.resources import ResourceLedger
from teamkit.tickets import TicketStore


class MailboxTests(unittest.TestCase):
    setUp = fixtures.OperationsTest.setUp
    enqueue = fixtures.OperationsTest.enqueue

    def file_worker(self, missing=False):
        self.runtime.executor.update(root=str(self.root), task='fixture', runtime={'name':'fixture'})
        self.turn = 1
        self.runtime.native = lambda actor, require_idle=True: dict(terminal=actor['terminal'], pid=actor['pid'], owner=actor['owner'], status='idle', turn=self.turn)
        def send(actor, text):
            self.assertNotIn('queue receipt', text)
            self.assertNotIn('queue submit', text)
            # This fixture sees only cwd/.herdr/jobs; no host state access and no CLI.
            task = next((self.root/'.herdr/jobs').glob('*/*/task.md'))
            self.assertIn('result.json', task.read_text())
            if not missing:
                report = task.parent/'report.txt'; report.write_text('Verified fixture result via file-only worker')
                (task.parent/'result.json').write_text(json.dumps(dict(artifact='report.txt', writer_stopped=True)))
            return dict(accepted=True, terminal=actor['terminal'], native_pid=actor['pid'], turn_baseline=1, turn=1)
        self.runtime.send = send

    def test_host_acceptance_and_file_only_result_collection(self):
        self.enqueue('FILES')
        self.file_worker()
        tick(self.config, self.state, self.runtime)
        queue = TicketStore(self.state); self.addCleanup(queue.close)
        self.assertEqual('running', queue.get('FILES')['state'])
        self.turn = 2
        tick(self.config, self.state, self.runtime)
        self.assertEqual('completed', queue.get('FILES')['state'])
        artifact = Path(queue.get('FILES')['result']['artifact'])
        self.assertTrue(artifact.is_relative_to(self.state.parent.resolve()/'operation-results/FILES'))
        self.assertEqual('Verified fixture result via file-only worker', artifact.read_text())
        self.assertFalse(ResourceLedger._rows(queue.db)[0]['execution_reserved'])

    def test_missing_file_retains_outcome_question_but_frees_the_session(self):
        self.enqueue('MISSING')
        self.file_worker(missing=True)
        tick(self.config, self.state, self.runtime)
        self.turn = 2
        tick(self.config, self.state, self.runtime)
        queue = TicketStore(self.state); self.addCleanup(queue.close)
        self.assertEqual('missing_result', queue.get('MISSING')['intervention']['kind'])
        self.assertFalse(ResourceLedger._rows(queue.db)[0]['execution_reserved'])
        self.assertNotEqual('completed', queue.get('MISSING')['state'])

    def test_external_read_input_is_staged_readonly_and_does_not_block_admission(self):
        import tempfile
        from teamkit.operations import NativeRuntime
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as external:
            attachment = Path(external)/'attachment.txt'; attachment.write_text('approved external fixture')
            ticket = self.enqueue('INPUT', scope=dict(paths=[str(self.root),str(attachment)], actions=['read'], data=['fixture']),
                resources=[dict(kind='path',name=str(self.root),mode='read'),dict(kind='path',name=str(attachment),mode='read')])
            self.file_worker()
            runtime = NativeRuntime(json.loads(self.config.read_text()), self.state)
            with patch.object(runtime,'native',return_value={}), patch('teamkit.providers.identity'):
                issues = runtime.admit_report(ticket, self.runtime.executor)
            self.assertNotIn('working_boundary', [x['code'] for x in issues])
            tick(self.config, self.state, self.runtime)
            staged = list((self.root/'.herdr/jobs/INPUT').glob('*/inputs/*'))
            self.assertEqual(1, len(staged))
            self.assertEqual('approved external fixture', staged[0].read_text())
            self.assertEqual(0, staged[0].stat().st_mode & 0o222)
            task = next((self.root/'.herdr/jobs/INPUT').glob('*/task.md')).read_text()
            self.assertIn(str(attachment), task)
            self.assertIn(str(staged[0]), task)

    def test_external_file_only_contract_uses_team_root_for_mailbox_and_host_verification(self):
        import tempfile
        from teamkit.workers import _roots
        with tempfile.TemporaryDirectory() as external:
            attachment = Path(external)/'only-input.txt'; attachment.write_text('approved fixture')
            ticket = self.enqueue('EXTERNAL', scope=dict(paths=[str(attachment)],actions=['read'],data=['fixture']),
                resources=[dict(kind='path',name=str(attachment),mode='read')])
            config = json.loads(self.config.read_text())
            queue = TicketStore(self.state); self.addCleanup(queue.close)
            self.assertEqual(str(self.root.resolve()), _roots(config,ticket,queue)[0])
            self.file_worker()
            tick(self.config,self.state,self.runtime)
            self.turn = 2
            tick(self.config,self.state,self.runtime)
            self.assertEqual('completed',queue.get('EXTERNAL')['state'],queue.get('EXTERNAL')['reason'])
