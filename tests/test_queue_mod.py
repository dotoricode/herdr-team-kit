import shutil
import subprocess
from pathlib import Path
import unittest


class QueueModControlsTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required for the mod contract adapter')
    def test_real_mod_control_callbacks(self):
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(['node', 'tests/queue_mod_controls.cjs'], cwd=root,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
