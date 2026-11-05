"""Reservation behavior for isolated review/document work (temporary git repositories only)."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def register(self, ident, role, kind, root=None, **extra):
        root = root or self.repo
        return self.queue.enqueue(dict(id=ident, product='fixture', role=role, kind=kind,
            goal='Write bounded fixture output', completion=['output'], checks=['report'],
            profiles=['codex/personal'], dependencies=[],
            resources=[dict(kind='path', name=str(root / 'report'), mode='write')],
            scope=dict(paths=[str(root)], actions=['read', 'write'], data=['fixture']),
            approval=dict(state='approved', reference=str(self.approval)), **extra), ident, user_approved=True)

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

    def test_explicit_git_base_is_used_and_pinned(self):
        old = subprocess.check_output(['git','-C',str(self.repo),'rev-parse','HEAD'], text=True).strip()
        (self.repo/'README').write_text('newer')
        subprocess.run(['git','-C',str(self.repo),'add','README'],check=True)
        subprocess.run(['git','-C',str(self.repo),'commit','-qm','newer'],check=True)
        self.register('ONE', 'implementer', 'code')
        self.register('TWO', 'reviewer', 'review', git_base=old)
        self.reserve('ONE')
        split = self.reserve('TWO')['worktree_split']
        self.assertEqual(old, split['base'])
        self.assertEqual('fixture\n', (Path(split['worktree'])/'README').read_text())

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

    def test_git_created_before_db_commit_is_recovered_without_deletion(self):
        from teamkit.split_worktree import create
        self.register('ONE', 'implementer', 'code')
        self.register('TWO', 'reviewer', 'review')
        self.reserve('ONE')
        intent = self.queue.record_split_intent('TWO')
        def interrupted(value):
            create(value)
            (Path(value['worktree'])/'keep').write_text('preserve')
            raise RuntimeError('fixture interruption before commit')
        with patch('teamkit.split_worktree.create', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'fixture interruption'):
                self.queue.materialize_split_intent('TWO')
        phase = self.queue.db.execute('SELECT phase FROM operation_worktree_intents WHERE ticket=?', ('TWO',)).fetchone()[0]
        self.assertEqual('planned', phase)
        self.assertEqual(intent, self.queue.materialize_split_intent('TWO'))
        self.assertEqual('preserve', (Path(intent['worktree'])/'keep').read_text())

    def test_resumed_ticket_keeps_split_after_original_conflict_ends(self):
        self.register('ONE', 'implementer', 'code')
        self.register('TWO', 'reviewer', 'review')
        first = self.reserve('ONE')
        split_attempt = self.reserve('TWO')
        target = Path(split_attempt['worktree_split']['worktree'])
        (target/'report').write_text('preserve')
        ledger = ResourceLedger(self.queue.db)
        for ident in (first['id'], split_attempt['id']):
            ledger.release(ident, execution_stopped=True, release_writes=True, writer_stopped=True)
        item = self.queue.get('TWO')
        item.update(state='pending', attempt=None)
        self.queue._save(item, 'fixture_resume')
        resumed = self.reserve('TWO')
        self.assertEqual(split_attempt['worktree_split'], resumed['worktree_split'])
        self.assertEqual('preserve', (target/'report').read_text())
        reservation = next(r for r in ResourceLedger._rows(self.queue.db) if r['attempt'] == resumed['id'])
        self.assertEqual(str(target/'report'), reservation['resources'][0]['name'])

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

    def test_candidate_response_previews_split_without_git_mutation(self):
        from teamkit.queue_actions import candidates
        self.register('ONE', 'implementer', 'code')
        self.register('TWO', 'reviewer', 'review')
        self.reserve('ONE')
        actor = dict(provider='codex', account='personal', owner='owner-TWO',
                     terminal='tTWO', pid=103, root=str(self.root), product='fixture')
        class Runtime:
            def executors(self, ticket): return [actor]
            def observation(self): return dict(observed_at=time.time(), complete=True, terminals=[])
        config = {'operations': {'enabled':True, 'resource_policy': {'global_limit':8}}}
        with patch('teamkit.queue_actions.load', return_value=config), \
             patch('teamkit.queue_actions._send_reason', return_value=None), \
             patch('teamkit.queue_actions.gate_issues', return_value=[]):
            result = candidates(self.root/'config', self.root/'state.sqlite', 'TWO', Runtime())
        choice = result['candidates'][0]
        self.assertTrue(choice['eligible'])
        self.assertTrue(choice['split_preview']['worktree'])
        self.assertFalse(Path(choice['split_preview']['worktree']).exists())
        self.assertEqual('pending', self.queue.get('TWO')['state'])

    def test_candidate_preview_reuses_recorded_intent_after_source_advances(self):
        from teamkit.queue_actions import candidates
        self.register('ONE', 'implementer', 'code')
        self.register('TWO', 'reviewer', 'review')
        self.reserve('ONE')
        intent = self.queue.record_split_intent('TWO')
        (self.repo/'README').write_text('newer')
        subprocess.run(['git','-C',str(self.repo),'add','README'],check=True)
        subprocess.run(['git','-C',str(self.repo),'commit','-qm','newer'],check=True)
        actor = dict(provider='codex', account='personal', owner='owner-TWO',
                     terminal='tTWO', pid=103, root=str(self.root), product='fixture')
        class Runtime:
            def executors(self, ticket): return [actor]
            def observation(self): return dict(observed_at=time.time(), complete=True, terminals=[])
        config = {'operations': {'enabled':True, 'resource_policy': {'global_limit':8}}}
        with patch('teamkit.queue_actions.load', return_value=config), \
             patch('teamkit.queue_actions._send_reason', return_value=None), \
             patch('teamkit.queue_actions.gate_issues', return_value=[]):
            result = candidates(self.root/'config', self.root/'state.sqlite', 'TWO', Runtime())
        choice = result['candidates'][0]
        self.assertTrue(choice['eligible'])
        self.assertEqual(intent, choice['split_preview'])
        self.assertFalse(Path(intent['worktree']).exists())
        actual = self.reserve('TWO')
        self.assertEqual(choice['split_preview'], actual['worktree_split'])

    def test_existing_executor_without_split_boundary_is_not_sent_or_created(self):
        self.register('ONE', 'implementer', 'code')
        self.register('TWO', 'reviewer', 'review')
        self.reserve('ONE')
        with self.assertRaisesRegex(ValueError, 'working boundary'):
            self.queue.reserve('TWO', dict(provider='codex', account='personal', owner='owner-TWO',
                terminal='tTWO', pid=103, root=str(self.repo)), {'global_limit': 8},
                dict(observed_at=time.time(), complete=True, terminals=[]))
        self.assertEqual('pending', self.queue.get('TWO')['state'])
        self.assertFalse((self.root/'operation-worktrees').exists())

    def test_caller_cannot_invent_inherited_worktree_authority(self):
        with self.assertRaisesRegex(ValueError, 'host-derived'):
            self.queue.enqueue(dict(id='FAKE', product='fixture', role='reviewer', kind='review',
                inherited_worktree={'worktree':str(self.root/'outside')}), 'FAKE', user_approved=True)

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

    def test_followup_inherits_only_mapped_parent_scope(self):
        from teamkit.operations import followup
        self.register('ONE', 'implementer', 'code')
        self.register('TWO', 'reviewer', 'review')
        self.reserve('ONE')
        attempt = self.reserve('TWO')
        actor = attempt['executor']
        self.queue.receipt('TWO', attempt['id'], actor['owner'], actor['terminal'], dict(actor, status='idle'))
        report = Path(attempt['worktree_split']['worktree'])/'report'
        report.write_text('result')
        self.queue.submit('TWO', attempt['id'], actor['owner'], dict(artifact=str(report), writer_stopped=True))
        child = followup(self.queue, self.queue.get('TWO'), self.queue.attempt(attempt['id']), 'defect', 'correct result')
        self.assertEqual('pending', child['state'])
        self.assertEqual(attempt['worktree_split'], child['inherited_worktree'])
        self.assertEqual([attempt['worktree_split']['worktree']], child['scope']['paths'])
        self.assertEqual(str(report), child['resources'][0]['name'])
        grandchild = followup(self.queue, child, {'id':'fixture-attempt'}, 'defect', 'correct again')
        self.assertEqual(attempt['worktree_split'], grandchild['inherited_worktree'])
        from teamkit.workers import _roots
        root, _ = _roots({'teams':{'fixture':{'root':str(self.repo)}}}, grandchild, self.queue)
        self.assertEqual(attempt['worktree_split']['worktree'], root)


if __name__ == '__main__':
    unittest.main()
