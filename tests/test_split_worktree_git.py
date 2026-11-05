"""Worktree provisioning stays within fixture repositories and preserves existing data."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from teamkit.split_worktree import plan, create


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, text=True, capture_output=True).stdout.strip()


class SplitWorktreeGitTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q')
        git(self.repo, 'config', 'user.email', 'fixture@example.invalid')
        git(self.repo, 'config', 'user.name', 'Fixture')
        (self.repo / 'doc').mkdir()
        (self.repo / 'doc' / 'file').write_text('base')
        git(self.repo, 'add', 'doc/file')
        git(self.repo, 'commit', '-qm', 'base')
        self.resources = [dict(kind='path', name=str(self.repo/'doc'/'result'), mode='write'),
                          dict(kind='path', name=str(self.repo/'doc'/'file'), mode='read'),
                          dict(kind='named', name='device:fixture', mode='write')]

    def test_planning_is_read_only_and_maps_resources(self):
        before = git(self.repo, 'worktree', 'list', '--porcelain')
        result = plan('TICKET-1', self.resources, self.root / 'isolated')
        self.assertEqual(before, git(self.repo, 'worktree', 'list', '--porcelain'))
        self.assertEqual(str(self.repo), result['source_root'])
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), result['base'])
        target = Path(result['worktree'])
        self.assertFalse(target.exists())
        self.assertEqual(str(target/'doc'/'result'), result['resources'][0]['name'])
        self.assertEqual(str(target/'doc'/'file'), result['resources'][1]['name'])
        self.assertEqual(self.resources[2], result['resources'][2])

    def test_create_is_idempotent_and_preserves_uncommitted_output(self):
        proposal = plan('TICKET-1', self.resources, self.root / 'isolated')
        first = create(proposal)
        target = Path(first['worktree'])
        self.assertEqual('base', (target/'doc'/'file').read_text())
        (target/'doc'/'result').write_text('preserve')
        self.assertEqual(first, create(proposal))
        self.assertEqual('preserve', (target/'doc'/'result').read_text())

    def test_foreign_existing_destination_is_never_reused(self):
        proposal = plan('TICKET-1', self.resources, self.root / 'isolated')
        target = Path(proposal['worktree'])
        target.mkdir(parents=True)
        (target/'keep').write_text('preserve')
        with self.assertRaises(ValueError):
            create(proposal)
        self.assertEqual('preserve', (target/'keep').read_text())

    def test_original_head_advance_does_not_invalidate_persisted_intent(self):
        proposal = plan('TICKET-1', self.resources, self.root / 'isolated')
        create(proposal)
        (self.repo/'doc'/'file').write_text('new source commit')
        git(self.repo, 'add', 'doc/file')
        git(self.repo, 'commit', '-qm', 'source advanced')
        self.assertEqual(proposal, create(proposal))
        self.assertEqual('base', (Path(proposal['worktree'])/'doc'/'file').read_text())

    def test_intent_created_before_head_advance_uses_its_pinned_base(self):
        proposal = plan('TICKET-1', self.resources, self.root / 'isolated')
        (self.repo/'doc'/'file').write_text('new source commit')
        git(self.repo, 'add', 'doc/file')
        git(self.repo, 'commit', '-qm', 'source advanced')
        self.assertEqual(proposal, create(proposal))
        self.assertEqual(proposal['base'], git(proposal['worktree'], 'rev-parse', 'HEAD'))

    def test_untracked_write_destination_cannot_escape_source(self):
        outside = self.root/'outside'
        outside.mkdir()
        (self.repo/'link').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            plan('TICKET-1', [dict(kind='path',name=str(self.repo/'link'/'result'),mode='write'),
                              self.resources[0]], self.root/'isolated')

    def test_non_git_source_and_nested_destination_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'git 저장소'):
            plan('TICKET-1', [dict(kind='path',name=str(self.root/'outside'),mode='write')], self.root/'isolated')
        with self.assertRaises(ValueError):
            plan('TICKET-1', self.resources, self.repo/'nested')

if __name__ == '__main__':
    unittest.main()
