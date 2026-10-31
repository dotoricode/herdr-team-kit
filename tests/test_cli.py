import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from teamkit.__main__ import parser, run
from teamkit.config import load, binding
from teamkit.providers import identity, match_identity
from teamkit.runtime import RuntimeError
from teamkit.store import Store


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root/'repo'
        self.repo.mkdir()
        for args in (['init','-q'], ['-c','user.name=Fixture','-c','user.email=test@example.invalid','commit','-qm','seed','--allow-empty']):
            subprocess.run(['git','-C',str(self.repo),*args], check=True)
        self.config = self.root/'config.json'
        self.state = self.root/'ledger'/'state.sqlite'
        self.value = {'version':1,'profiles':{'codex':{}},'teams':{'alpha':{'root':str(self.repo),'roles':{
            'implementer':{'provider':'codex','model':'model-fixture','effort':'medium'}}}}}
        for account in ('team','personal'):
            self.value['profiles']['codex'][account] = {'home':str(self.root/account),'identity':{'email':account+'@example.invalid'},'model':'fixture','effort':'medium'}
        self.config.write_text(json.dumps(self.value))
        (self.root/'goal.txt').write_text('Implement one observable behavior. Preserve unrelated work.')

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, *args):
        return run(parser().parse_args(['--config',str(self.config),'--state',str(self.state),*args]))

    def native_statusline(self, at=None, session='fixture-session', usage=None, account='team'):
        at = time.time()-1 if at is None else at
        usage = usage or {'input_tokens':2, 'output_tokens':4,
                          'cache_creation_input_tokens':100, 'cache_read_input_tokens':200}
        home = Path(self.value['profiles']['claude'][account]['home'])
        path = home/'projects'/'fixture'/ (session+'.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {'type':'assistant', 'sessionId':session, 'uuid':'response-fixture',
               'timestamp':datetime.fromtimestamp(at, timezone.utc).isoformat(),
               'message':{'usage':usage, 'content':'PRIVATE-CONTENT-FIXTURE'}}
        path.write_text(json.dumps(row)+'\n')
        return {'session_id':session, 'transcript_path':str(path),
                'context_window':{'current_usage':usage},
                'rate_limits':{'five_hour':{'used_percentage':3,'resets_at':at+3600},
                               'seven_day':{'used_percentage':4,'resets_at':at+86400}}}

    def create(self):
        return self.call('create','T1','--team','alpha','--root',str(self.repo),'--goal-file',str(self.root/'goal.txt'))

class IntegrationTests(Fixture):
    def test_cli_create_route_and_events(self):
        self.assertEqual(self.create()['state'], 'queued')
        self.assertEqual(self.call('route','--provider','codex')['action'], 'wait')
        self.assertEqual(self.call('events','T1')[0]['event'], 'created')

    def test_outside_herdr_dispatch_leaves_task_queued(self):
        self.create()
        with patch.dict(os.environ, {'HERDR_ENV':''}):
            with self.assertRaises(RuntimeError):
                self.call('dispatch','T1')
        self.assertEqual(self.call('show','T1')['state'], 'queued')

    def test_timeout_after_delivery_leaves_uncertain_and_never_retries(self):
        self.create()
        now = time.time()
        with StoreContext(self.state) as db:
            db.quota('codex/team', {'binding':binding(self.value['profiles']['codex']['team']), 'observed_at':now, 'five_hour':{'used':1,'resets_at':now+300},'weekly':{'used':1,'resets_at':now+600}})
        with patch.dict(os.environ, {'HERDR_ENV':'1'}), patch('teamkit.providers.identity'), patch('teamkit.__main__.Herdr') as h:
            h.return_value.launch.return_value = {'name':'fixture','pane':'w1:p1'}
            h.return_value.prompt.side_effect = RuntimeError('unknown delivery')
            with self.assertRaises(RuntimeError):
                self.call('dispatch','T1')
            self.assertEqual(self.call('show','T1')['state'], 'uncertain')
            with self.assertRaises(ValueError):
                self.call('dispatch','T1')
            self.assertEqual(h.return_value.prompt.call_count, 1)

    def test_profile_homes_cannot_share(self):
        self.value['profiles']['codex']['personal']['home'] = self.value['profiles']['codex']['team']['home']
        self.config.write_text(json.dumps(self.value))
        with self.assertRaises(ValueError):
            load(self.config)

    def test_root_outside_team_rejected(self):
        with self.assertRaises(ValueError):
            self.call('create','T2','--team','alpha','--root',str(self.root),'--goal-file',str(self.root/'goal.txt'))

    def test_wrong_account_and_api_auth_fail_closed(self):
        profile = self.value['profiles']['codex']['team']
        with self.assertRaises(RuntimeError):
            match_identity(profile, {'email':'personal@example.invalid'})
        with patch('teamkit.providers.subprocess.run') as cmd:
            cmd.return_value.returncode = 0
            cmd.return_value.stdout = json.dumps({'loggedIn':True,'authMethod':'api_key','email':'team@example.invalid'})
            with self.assertRaises(RuntimeError):
                identity('claude', profile)

    def test_capture_stores_only_normalized_quota_not_transcript(self):
        raw = {'rate_limits': {'five_hour':{'used_percentage':3,'resets_at':time.time()+900},
                              'seven_day':{'used_percentage':4,'resets_at':time.time()+900}}, 'transcript':'private text'}
        self.value['profiles']['claude'] = self.value['profiles'].pop('codex')
        self.value['teams']['alpha']['roles']['implementer']['provider'] = 'claude'
        self.config.write_text(json.dumps(self.value))
        raw = self.native_statusline()
        raw['transcript'] = 'private text'
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR':str(self.root/'team')}), patch('teamkit.providers.identity'), patch('sys.stdin',io.StringIO(json.dumps(raw))):
            self.call('capture','--provider','claude','--account','team')
        self.assertNotIn(b'private text', self.state.read_bytes())
        self.assertEqual(self.call('route','--provider','claude')['account'],'team')


    def test_claude_capture_cannot_revive_an_expired_native_response(self):
        self.value['profiles']['claude'] = self.value['profiles'].pop('codex')
        self.value['teams']['alpha']['roles']['implementer']['provider'] = 'claude'
        self.config.write_text(json.dumps(self.value))
        raw = self.native_statusline(time.time()-600)
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR':str(self.root/'team')}), patch('teamkit.providers.identity'), patch('sys.stdin',io.StringIO(json.dumps(raw))):
            self.call('capture','--provider','claude','--account','team')
        self.assertEqual(self.call('route','--provider','claude')['action'],'wait')


class StoreContext:
    def __init__(self,path): self.store = Store(path)
    def __enter__(self): return self.store
    def __exit__(self,*args): self.store.close()


class ReviewRegressionTests(Fixture):
    def seed_quota(self):
        now = time.time()
        with StoreContext(self.state) as db:
            db.quota('codex/team', {'binding':binding(self.value['profiles']['codex']['team']), 'observed_at':now,
                                  'five_hour':{'used':1,'resets_at':now+900},'weekly':{'used':1,'resets_at':now+900}})

    def test_confirmed_receipt_survives_transport_timeout(self):
        self.create(); self.seed_quota()
        def received_then_timeout(*args):
            with StoreContext(self.state) as db:
                task = db.get('T1')
                db.update('T1', task['owner'], 'running', {'identity_checked':True})
            raise RuntimeError('wait timed out after receipt')
        with patch.dict(os.environ, {'HERDR_ENV':'1'}), patch('teamkit.providers.identity'), patch('teamkit.__main__.Herdr') as h:
            h.return_value.launch.return_value = {'name':'fixture','pane':'w1:p1'}
            h.return_value.prompt.side_effect = received_then_timeout
            with self.assertRaises(RuntimeError): self.call('dispatch','T1')
        self.assertEqual(self.call('show','T1')['state'],'running')

    def test_quoted_tilde_is_normalized_in_receipt_and_environment(self):
        self.create(); self.seed_quota()
        with patch.dict(os.environ, {'HERDR_ENV':'1', 'HOME':str(self.root)}), patch('teamkit.providers.identity'), patch('teamkit.__main__.Herdr') as h:
            h.return_value.launch.return_value = {'name':'fixture','pane':'w1:p1'}
            args = parser().parse_args(['--config','~/config.json','--state','~/ledger/state.sqlite','dispatch','T1'])
            run(args)
            env = h.return_value.launch.call_args.args[1]['env']
            self.assertEqual(env['TEAMKIT_CONFIG'],str(self.config.resolve()))
            self.assertEqual(env['TEAMKIT_STATE'],str(self.state.resolve()))
            prompt = h.return_value.prompt.call_args.args[1]
            self.assertNotIn('/~/',prompt)

if __name__ == '__main__': unittest.main()
