import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from teamkit.store import Store


class DeliveryOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Store(Path(self.tmp.name)/'state.sqlite')
        self.db.create('T','alpha','leader','/workspace','a'*40,'coordinate')
        self.db.claim('T','owner','codex','team','m','medium')
        self.db.update('T','owner','launching',{})
        self.db.update('T','owner','submitted',{})

    def tearDown(self):
        self.db.close();self.tmp.cleanup()

    def test_receipt_before_failure_keeps_running(self):
        self.db.update('T','owner','running',{'identity_checked':True})
        self.db.delivery_failed('T','owner')
        self.assertEqual(self.db.get('T')['state'],'running')

    def test_late_verified_receipt_recovers_uncertain_without_new_owner(self):
        self.db.delivery_failed('T','owner')
        self.db.receipt('T','owner')
        self.assertEqual(self.db.get('T')['state'],'running')
        self.assertEqual(self.db.get('T')['owner'],'owner')
        self.assertTrue(self.db.get('T')['runtime']['identity_checked'])


class RevisionGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.git('init','-q')
        self.git('-c','user.name=Fixture','-c','user.email=test@example.invalid','commit','-qm','seed','--allow-empty')

    def tearDown(self): self.tmp.cleanup()

    def git(self,*args,check=True):
        return subprocess.run(['git','-C',str(self.root),*args],capture_output=True,text=True,check=check)

    def test_git_cannot_change_reviewed_head_during_completion(self):
        from teamkit.checkpoint import revision_guard
        original=self.git('rev-parse','HEAD').stdout.strip()
        # commit-tree needs an identity too; create an unattached commit explicitly.
        env=os.environ.copy()
        env.update(GIT_AUTHOR_NAME='Fixture',GIT_AUTHOR_EMAIL='test@example.invalid',GIT_COMMITTER_NAME='Fixture',GIT_COMMITTER_EMAIL='test@example.invalid')
        new=subprocess.run(['git','-C',str(self.root),'commit-tree','HEAD^{tree}','-p','HEAD','-m','candidate'],env=env,capture_output=True,text=True,check=True).stdout.strip()
        with revision_guard(self.root):
            update=self.git('update-ref','HEAD',new,check=False)
            self.assertNotEqual(update.returncode,0)
            self.assertEqual(self.git('rev-parse','HEAD').stdout.strip(),original)
        self.git('update-ref','HEAD',new)
        self.assertEqual(self.git('rev-parse','HEAD').stdout.strip(),new)

    def test_existing_git_lock_is_never_removed(self):
        from teamkit.checkpoint import revision_guard
        lock=self.root/'.git/index.lock';lock.write_text('owned by another process')
        with self.assertRaises(ValueError):
            with revision_guard(self.root): pass
        self.assertEqual(lock.read_text(),'owned by another process')

    def test_reftable_is_rejected_before_creating_file_locks(self):
        from teamkit.checkpoint import revision_guard
        repo = self.root/'reftable'
        result = subprocess.run(['git','init','--ref-format=reftable',str(repo)], capture_output=True, text=True)
        if result.returncode:
            self.skipTest('installed Git cannot create reftable fixtures')
        subprocess.run(['git','-C',str(repo),'-c','user.name=Fixture','-c','user.email=test@example.invalid',
                        'commit','-qm','seed','--allow-empty'],check=True,capture_output=True)
        subprocess.run(['git','-C',str(repo),'checkout','--detach'],check=True,capture_output=True)
        with self.assertRaises(ValueError):
            with revision_guard(repo):
                pass
        self.assertFalse((repo/'.git/index.lock').exists())

    def test_worktree_config_cannot_disguise_reftable_backend(self):
        from teamkit.checkpoint import revision_guard
        repo = self.root/'disguised'
        result = subprocess.run(['git','init','--ref-format=reftable',str(repo)], capture_output=True, text=True)
        if result.returncode:
            self.skipTest('installed Git cannot create reftable fixtures')
        def git(*args):
            return subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True,text=True).stdout.strip()
        git('-c','user.name=Fixture','-c','user.email=test@example.invalid','commit','-qm','seed','--allow-empty')
        git('checkout','--detach')
        git('config','extensions.worktreeConfig','true')
        git('config','--worktree','extensions.refStorage','files')
        self.assertEqual(git('config','--get','extensions.refStorage'),'files')
        self.assertEqual(git('rev-parse','--show-ref-format'),'reftable')
        with self.assertRaises(ValueError):
            with revision_guard(repo): pass
        self.assertFalse((repo/'.git/index.lock').exists())

    def test_older_git_files_backend_uses_common_config(self):
        from teamkit.checkpoint import revision_guard
        real_run = subprocess.run
        def old_git(command, *args, **kwargs):
            if command[-2:] == ['rev-parse','--show-ref-format']:
                return subprocess.CompletedProcess(command, 0, '--show-ref-format\n', '')
            return real_run(command, *args, **kwargs)
        with patch('teamkit.checkpoint.subprocess.run', side_effect=old_git):
            with revision_guard(self.root):
                self.assertTrue((self.root/'.git/index.lock').exists())
        self.assertFalse((self.root/'.git/index.lock').exists())

    def test_older_git_common_config_ignores_worktree_override(self):
        from teamkit.checkpoint import revision_guard
        repo = self.root/'older-reftable'
        result = subprocess.run(['git','init','--ref-format=reftable',str(repo)], capture_output=True, text=True)
        if result.returncode:
            self.skipTest('installed Git cannot create reftable fixtures')
        for args in (['config','extensions.worktreeConfig','true'], ['config','--worktree','extensions.refStorage','files']):
            subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True)
        real_run = subprocess.run
        def old_git(command, *args, **kwargs):
            if command[-2:] == ['rev-parse','--show-ref-format']:
                return subprocess.CompletedProcess(command, 0, '--show-ref-format\n', '')
            return real_run(command, *args, **kwargs)
        with patch('teamkit.checkpoint.subprocess.run', side_effect=old_git):
            with self.assertRaises(ValueError):
                with revision_guard(repo): pass
        self.assertFalse((repo/'.git/index.lock').exists())


if __name__ == '__main__':unittest.main()
