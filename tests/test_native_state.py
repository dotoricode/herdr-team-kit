"""Consumer regression for the installed Herdr agent-info contract."""
import os
from unittest.mock import patch

from test_cli import Fixture, StoreContext
from teamkit.runtime import RuntimeError


class NativeStateTests(Fixture):
    def setUp(self):
        super().setUp()
        self.call('create', 'L1', '--team', 'alpha', '--role', 'leader',
                  '--root', str(self.repo), '--goal-file', str(self.root/'goal.txt'))
        self.message = self.root/'message.txt'
        self.message.write_text('Echo one harmless marker, then park.')
        with StoreContext(self.state) as db:
            db.claim('L1', 'owner', 'codex', 'team', 'fixture', 'low')
            db.update('L1', 'owner', 'launching', {})
            db.update('L1', 'owner', 'submitted', {'name':'fixture', 'pane':'w1:p2'})
            db.receipt('L1', 'owner')
            db.park('L1', 'owner')

    def test_native_idle_and_done_can_receive_tell(self):
        for status in ('idle', 'done'):
            with self.subTest(status=status):
                with patch.dict(os.environ, {'HERDR_ENV':'1'}), patch('teamkit.__main__.Herdr') as runtime:
                    runtime.return_value.call.return_value = {
                        'type':'agent_info', 'agent':{'agent_status':status, 'name':'fixture'}}
                    result = self.call('tell', 'L1', '--message-file', str(self.message))
                    self.assertEqual(result['state'], 'running')
                    runtime.return_value.prompt.assert_called_once()
                    self.assertIn('Echo one harmless marker', runtime.return_value.prompt.call_args.args[1])
                with StoreContext(self.state) as db:
                    db.park('L1', 'owner')

    def test_nonready_or_missing_native_state_preserves_parked_owner(self):
        for fields in ({'agent_status':'working'}, {'agent_status':'blocked'},
                       {'agent_status':'unknown'}, {}, {'status':'idle'}):
            with self.subTest(fields=fields):
                before = self.call('events', 'L1')
                with patch.dict(os.environ, {'HERDR_ENV':'1'}), patch('teamkit.__main__.Herdr') as runtime:
                    runtime.return_value.call.return_value = {'type':'agent_info', 'agent':fields}
                    with self.assertRaises(RuntimeError):
                        self.call('tell', 'L1', '--message-file', str(self.message))
                    runtime.return_value.prompt.assert_not_called()
                self.assertEqual(self.call('show', 'L1')['state'], 'ready')
                self.assertEqual(self.call('show', 'L1')['owner'], 'owner')
                self.assertEqual(self.call('events', 'L1'), before)
