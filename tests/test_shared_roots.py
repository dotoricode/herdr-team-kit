import json
import subprocess
from test_cli import Fixture
from teamkit.config import load


class SharedRootsTests(Fixture):
    def shared(self):
        root=self.root/'manual';root.mkdir()
        subprocess.run(['git','clone','-q',str(self.repo),str(root)],check=True)
        return root

    def test_explicit_shared_repository_can_create_and_pin_task(self):
        root=self.shared();self.value['shared_roots']=[str(root)];self.config.write_text(json.dumps(self.value))
        task=self.call('create','M1','--team','alpha','--root',str(root),'--goal-file',str(self.root/'goal.txt'))
        self.assertEqual(task['root'],str(root.resolve()))
        self.assertEqual(task['base'],subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip())

    def test_unlisted_sibling_and_symlink_escape_are_rejected(self):
        root=self.shared();link=self.repo/'escape';link.symlink_to(root,target_is_directory=True)
        for target in (root,link):
            with self.assertRaises(ValueError):
                self.call('create','M1','--team','alpha','--root',str(target),'--goal-file',str(self.root/'goal.txt'))

    def test_shared_root_does_not_bypass_git_requirement_or_team_identity(self):
        self.value['shared_roots']=[str(self.root)];self.config.write_text(json.dumps(self.value))
        with self.assertRaises(ValueError):
            self.call('create','M1','--team','alpha','--root',str(self.root),'--goal-file',str(self.root/'goal.txt'))
        with self.assertRaises(KeyError):
            self.call('create','M1','--team','unknown','--root',str(self.repo),'--goal-file',str(self.root/'goal.txt'))

    def test_invalid_allowlist_is_rejected_before_ledger_creation(self):
        for roots in ('/srv/example/manual',[123],['relative/path'],None):
            with self.subTest(roots=roots):
                self.value['shared_roots']=roots;self.config.write_text(json.dumps(self.value))
                with self.assertRaises(ValueError):load(self.config)
        self.assertFalse(self.state.exists())
