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
                self.state = self.root/'ledger'/(provider+'-state.sqlite')
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

    def test_dispatch_rejects_authentication_overlap_before_identity_or_launch(self):
        cases = [self.root/'team'/'state.sqlite',
                 self.root/'personal'/'ledger'/'state.sqlite',
                 self.root/'ancestor-state.sqlite']
        for index, state in enumerate(cases):
            with self.subTest(state=state):
                self.state = state
                task = 'UNSAFE-' + str(index)
                self.call('create', task, '--team', 'alpha', '--root', str(self.repo),
                          '--goal-file', str(self.root/'goal.txt'))
                now = time.time()
                with StoreContext(self.state) as db:
                    db.quota('codex/team', {'binding':binding(self.value['profiles']['codex']['team']),
                        'observed_at':now, 'five_hour':{'used':1,'resets_at':now+900},
                        'weekly':{'used':1,'resets_at':now+900}})
                before = self.call('events', task)
                with patch.dict(os.environ, {'HERDR_ENV':'1'}), patch('teamkit.providers.identity') as identity, patch('teamkit.__main__.Herdr') as runtime:
                    runtime.return_value.launch.return_value = {'name':'fixture','pane':'w1:p2'}
                    with self.assertRaisesRegex(ValueError, 'ledger.*authentication'):
                        self.call('dispatch', task)
                    identity.assert_not_called()
                    runtime.assert_not_called()
                self.assertEqual(self.call('show', task)['state'], 'queued')
                self.assertEqual(self.call('events', task), before)

    def test_dispatch_rejects_native_case_insensitive_auth_alias(self):
        home = self.root/'team'
        home.mkdir(exist_ok=True)
        alias = self.root/'TEAM'
        if not alias.exists():
            self.skipTest('native case-insensitive filesystem required')
        self.assertTrue(home.samefile(alias))
        self.state = alias/'ledger'/'state.sqlite'
        self.call('create', 'CASE-ALIAS', '--team', 'alpha', '--root', str(self.repo),
                  '--goal-file', str(self.root/'goal.txt'))
        now = time.time()
        with StoreContext(self.state) as db:
            db.quota('codex/team', {'binding':binding(self.value['profiles']['codex']['team']),
                'observed_at':now, 'five_hour':{'used':1,'resets_at':now+900},
                'weekly':{'used':1,'resets_at':now+900}})
        before = self.call('events', 'CASE-ALIAS')
        with patch.dict(os.environ, {'HERDR_ENV':'1'}), patch('teamkit.providers.identity') as identity, patch('teamkit.__main__.Herdr') as runtime:
            runtime.return_value.launch.return_value = {'name':'fixture','pane':'w1:p2'}
            with self.assertRaisesRegex(ValueError, 'ledger.*authentication'):
                self.call('dispatch', 'CASE-ALIAS')
            identity.assert_not_called()
            runtime.assert_not_called()
        self.assertEqual(self.call('show', 'CASE-ALIAS')['state'], 'queued')
        self.assertEqual(self.call('events', 'CASE-ALIAS'), before)
