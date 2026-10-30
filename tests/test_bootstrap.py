import json
import os
import time
from unittest.mock import patch

from test_cli import Fixture, StoreContext
from teamkit.config import binding


class BootstrapTests(Fixture):
    def test_dispatch_grants_only_ledger_parent_and_keeps_unicode_readable(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                self.state = self.root/(provider+'-state.sqlite')
                if provider == 'claude':
                    self.value['profiles']['claude'] = self.value['profiles'].pop('codex')
                    self.value['teams']['alpha']['roles']['implementer']['provider'] = provider
                    self.config.write_text(json.dumps(self.value))
                task = 'T-' + provider
                goal = '팀장은 승인 범위를 확인하고 대기합니다. 🔎'
                (self.root/'goal.txt').write_text(goal)
                self.call('create', task, '--team', 'alpha', '--root', str(self.repo),
                          '--goal-file', str(self.root/'goal.txt'))
                now = time.time()
                with StoreContext(self.state) as db:
                    db.quota(provider+'/team', {'binding':binding(self.value['profiles'][provider]['team']),
                        'observed_at':now, 'five_hour':{'used':1,'resets_at':now+900},
                        'weekly':{'used':1,'resets_at':now+900}})
                with patch.dict(os.environ, {'HERDR_ENV':'1'}), patch('teamkit.providers.identity'), patch('teamkit.__main__.Herdr') as runtime:
                    runtime.return_value.launch.return_value = {'name':'fixture','pane':'w1:p2'}
                    self.call('dispatch', task)
                    args = runtime.return_value.launch.call_args.args[1]['args']
                    grants = [args[i+1] for i,v in enumerate(args) if v == '--add-dir']
                    with self.subTest(contract='ledger grant'):
                        self.assertEqual(grants, [str(self.state.parent.resolve())])
                    self.assertNotIn(str(self.config), grants)
                    self.assertNotIn(str(self.root/'team'), grants)
                    prompt = runtime.return_value.prompt.call_args.args[1]
                    contract = json.loads(prompt.split('\n',1)[1].split('\nBefore editing,',1)[0])
                    self.assertEqual(contract['goal'], goal)
                    self.assertIn(goal, prompt)
