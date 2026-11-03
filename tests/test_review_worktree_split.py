"""Reservation behavior for isolated review/document work (temporary git repositories only)."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from teamkit.resources import ResourceLedger
from teamkit.tickets import TicketStore


class ReviewWorktreeSplitTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        subprocess.run(['git', '-C', str(self.repo), 'config', 'user.email', 'fixture@example.invalid'], check=True)
        subprocess.run(['git', '-C', str(self.repo), 'config', 'user.name', 'Fixture'], check=True)
        (self.repo / 'README').write_text('fixture\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', 'README'], check=True)
        subprocess.run(['git', '-C', str(self.repo), 'commit', '-qm', 'fixture'], check=True)
        self.approval = self.root / 'approval'
        self.approval.write_text('Fixture approval')
        self.queue = TicketStore(self.root / 'state.sqlite')
        self.addCleanup(self.queue.close)

    def register(self, ident, role, kind, root=None):
        root = root or self.repo
        return self.queue.enqueue(dict(id=ident, product='fixture', role=role, kind=kind,
            goal='Write bounded fixture output', completion=['output'], checks=['report'],
            profiles=['codex/personal'], dependencies=[],
            resources=[dict(kind='path', name=str(root / 'report'), mode='write')],
            scope=dict(paths=[str(root)], actions=['read', 'write'], data=['fixture']),
            approval=dict(state='approved', reference=str(self.approval))), ident, user_approved=True)

    def reserve(self, ident):
        return self.queue.reserve(ident, dict(provider='codex', account='personal', owner='owner-'+ident,
            terminal='t'+ident, pid=100 + len(ident), root=str(self.root)), {'global_limit': 8},
            dict(observed_at=time.time(), complete=True, terminals=[]))

    def test_conflicting_review_gets_distinct_worktree_and_preserves_approved_contract(self):
        self.register('ONE', 'implementer', 'code')
        original = self.register('TWO', 'reviewer', 'review')
        self.reserve('ONE')
        attempt = self.reserve('TWO')
        split = attempt['worktree_split']
        self.assertEqual(str(self.repo), split['source_root'])
        self.assertNotEqual(str(self.repo), split['worktree'])
        self.assertTrue(Path(split['worktree'], 'README').exists())
        self.assertEqual(original['scope'], self.queue.get('TWO')['scope'])
        lease = next(x for x in ResourceLedger._rows(self.queue.db) if x['attempt'] == attempt['id'])
        self.assertIn(dict(kind='path', name=str(Path(split['worktree'])/'report'), mode='write'), lease['resources'])

    def test_non_conflicting_document_stays_in_original_path(self):
        original = self.register('ONE', 'documenter', 'document')
        attempt = self.reserve('ONE')
        self.assertNotIn('worktree_split', attempt)
        self.assertEqual(original['resources'], next(x for x in ResourceLedger._rows(self.queue.db)
                                                     if x['attempt'] == attempt['id'])['resources'])

    def test_leader_review_kind_is_explicitly_eligible(self):
        self.register('ONE', 'implementer', 'code')
        self.register('TWO', 'leader', 'review')
        self.reserve('ONE')
        self.assertIn('worktree_split', self.reserve('TWO'))

    def test_intent_is_durable_before_git_changes_and_can_resume(self):
        self.register('ONE', 'implementer', 'code')
        self.register('TWO', 'reviewer', 'review')
        self.reserve('ONE')
        intent = self.queue.record_split_intent('TWO')
        self.assertFalse(Path(intent['worktree']).exists())
        self.queue.close()
        self.queue = TicketStore(self.root/'state.sqlite')
        self.addCleanup(self.queue.close)
        self.assertEqual(intent, self.queue.record_split_intent('TWO'))
        created = self.queue.materialize_split_intent('TWO')
        self.assertEqual(intent, created)
        self.assertTrue(Path(created['worktree'], 'README').is_file())
        self.assertEqual(created, self.queue.materialize_split_intent('TWO'))

    def test_intent_rejects_nonconflicting_or_code_work(self):
        self.register('ONE', 'documenter', 'document')
        with self.assertRaisesRegex(ValueError, 'conflict'):
            self.queue.record_split_intent('ONE')
        self.register('TWO', 'implementer', 'code')
        self.reserve('ONE')
        with self.assertRaisesRegex(ValueError, 'review or document'):
            self.queue.record_split_intent('TWO')

    def test_conflicting_code_still_waits(self):
        self.register('ONE', 'implementer', 'code')
        self.register('TWO', 'implementer', 'code')
        self.reserve('ONE')
        with self.assertRaisesRegex(ValueError, 'conflicting resource'):
            self.reserve('TWO')

    def test_conflicting_document_in_non_git_directory_still_waits_with_reason(self):
        plain = self.root / 'plain'
        plain.mkdir()
        self.register('ONE', 'implementer', 'code', plain)
        self.register('TWO', 'documenter', 'document', plain)
        self.reserve('ONE')
        with self.assertRaisesRegex(ValueError, 'git 저장소'):
            self.reserve('TWO')

    def test_preview_simulates_split_without_creating_worktree(self):
        self.register('ONE', 'implementer', 'code')
        self.register('TWO', 'reviewer', 'review')
        self.reserve('ONE')
        preview = object.__new__(TicketStore)
        preview.db = sqlite3.connect(':memory:', isolation_level=None)
        preview.path, preview.clock = self.queue.path, time.time
        self.queue.db.backup(preview.db)
        try:
            attempt = preview.reserve('TWO', dict(provider='codex', account='personal', owner='owner-TWO',
                terminal='tTWO', pid=103, root=str(self.root)), {'global_limit': 8},
                dict(observed_at=time.time(), complete=True, terminals=[]), preview=True)
        finally:
            preview.close()
        self.assertIn('worktree_split', attempt)
        self.assertFalse(Path(attempt['worktree_split']['worktree']).exists())
        self.assertEqual('pending', self.queue.get('TWO')['state'])

    def test_submission_is_limited_to_split_scope_and_verifier_runs_there(self):
        from teamkit.operations import verify
        self.register('ONE', 'implementer', 'code')
        self.register('TWO', 'reviewer', 'review')
        self.reserve('ONE')
        attempt = self.reserve('TWO')
        actor = attempt['executor']
        self.queue.receipt('TWO', attempt['id'], actor['owner'], actor['terminal'], dict(actor, status='idle'))
        original_report = self.repo/'report'
        original_report.write_text('original')
        with self.assertRaisesRegex(ValueError, 'outside approved'):
            self.queue.submit('TWO', attempt['id'], actor['owner'],
                              dict(artifact=str(original_report), writer_stopped=True))
        target = Path(attempt['worktree_split']['worktree'])
        report = target/'report'
        report.write_text('result')
        self.queue.submit('TWO', attempt['id'], actor['owner'], dict(artifact=str(report), writer_stopped=True))
        ticket = self.queue.get('TWO')
        ticket['verification_commands'] = {'report':[sys.executable, '-c',
            'import pathlib,sys;sys.exit(pathlib.Path.cwd() != pathlib.Path(sys.argv[1]).parent)', '{artifact}']}
        proof = verify(ticket, self.queue.attempt(attempt['id']))
        self.assertEqual({'report':True}, proof['checks'])


if __name__ == '__main__':
    unittest.main()
