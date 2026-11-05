from pathlib import Path
import plistlib
import tempfile
import time
import unittest

from teamkit.service import definition, install
from teamkit.wake import listener, notify, wait


class ServiceTest(unittest.TestCase):
    def test_launchd_restarts_guard_and_guard_uses_existing_cli(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / 'state.sqlite'
            config = Path(folder) / 'config.json'
            result = install(config, state, folder)
            actual = plistlib.loads(Path(result['path']).read_bytes())
            self.assertTrue(actual['KeepAlive'])
            self.assertTrue(actual['RunAtLoad'])
            self.assertEqual(['service', 'guard'], actual['ProgramArguments'][-2:])
            self.assertNotIn('HC_TERMINAL_ID', actual['EnvironmentVariables'])
            self.assertFalse(install(config, state, folder)['changed'])

    def test_real_local_event_wakes_periodic_wait(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / 'state.sqlite'
            with listener(state) as sock:
                self.assertTrue(notify(state))
                began = time.monotonic()
                wait(sock, 5)
                self.assertLess(time.monotonic() - began, .5)
            self.assertFalse(notify(state))


if __name__ == '__main__':
    unittest.main()
