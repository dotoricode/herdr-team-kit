import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from teamkit.providers import profile_env, identity
from teamkit.runtime import launch_spec
from test_cli import Fixture


class LaunchEnvironmentTests(unittest.TestCase):
    def profile(self, home):
        return {'home':str(home),'identity':{'email':'personal@example.invalid'},'model':'fixture','effort':'medium'}

    def test_codex_new_and_resume_bypass_shared_daemon(self):
        for session in (None,'saved-session'):
            spec=launch_spec('codex',self.profile('/private/profile'),session)
            self.assertIn('--no-daemon',spec['args'])

    def test_default_claude_home_keeps_default_auth_namespace(self):
        p=self.profile(Path.home()/'.claude')
        with patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':'/private/other-profile'}):
            self.assertFalse('CLAUDE_CONFIG_DIR' in profile_env('claude',p))
        self.assertEqual(launch_spec('claude',p,None)['env']['CLAUDE_CONFIG_DIR'],'')

    def test_custom_claude_home_stays_explicit(self):
        p=self.profile('/private/custom')
        self.assertEqual(profile_env('claude',p)['CLAUDE_CONFIG_DIR'],'/private/custom')
        self.assertEqual(launch_spec('claude',p,None)['env']['CLAUDE_CONFIG_DIR'],'/private/custom')

    def test_default_identity_read_uses_default_namespace(self):
        p=self.profile(Path.home()/'.claude')
        def auth(command,**kwargs):
            default='CLAUDE_CONFIG_DIR' not in kwargs['env']
            value={'loggedIn':default,'authMethod':'claude.ai','email':'personal@example.invalid'}
            import subprocess
            return subprocess.CompletedProcess(command,0 if default else 1,json.dumps(value),'')
        with patch('teamkit.providers.subprocess.run',side_effect=auth):
            self.assertEqual(identity('claude',p)['email'],'personal@example.invalid')


class DefaultReceiptTests(Fixture):
    def test_receipt_accepts_unset_default_home_after_identity_check(self):
        default=str(Path.home()/'.claude')
        self.value['profiles']['claude']=self.value['profiles'].pop('codex')
        self.value['profiles']['claude']['personal']['home']=default
        self.value['teams']['alpha']['roles']['implementer']['provider']='claude'
        self.config.write_text(json.dumps(self.value))
        self.create()
        from teamkit.store import Store
        db=Store(self.state)
        try:
            db.claim('T1','owner','claude','personal','fixture','medium')
            db.update('T1','owner','launching',{})
            db.update('T1','owner','submitted',{})
        finally:db.close()
        with patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':''}),patch('teamkit.providers.identity') as check:
            result=self.call('receipt','T1','--owner','owner')
            check.assert_called_once()
        self.assertEqual(result['state'],'running')

    def test_capture_accepts_default_namespace_without_exposing_input(self):
        import io,time
        default=str(Path.home()/'.claude')
        self.value['profiles']['claude']=self.value['profiles'].pop('codex')
        self.value['profiles']['claude']['personal']['home']=default
        self.value['teams']['alpha']['roles']['implementer']['provider']='claude'
        self.config.write_text(json.dumps(self.value))
        data={'rate_limits':{'five_hour':{'used_percentage':7,'resets_at':time.time()+900},
                             'seven_day':{'used_percentage':8,'resets_at':time.time()+900}}}
        with patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':''}),patch('teamkit.providers.identity'),patch('sys.stdin',io.StringIO(json.dumps(data))):
            result=self.call('capture','--provider','claude','--account','personal')
        self.assertEqual(result['quota']['five_hour']['used'],7)
        self.assertEqual(self.call('route','--provider','claude')['account'],'personal')

    def default_claude_fixture(self):
        default=str(Path.home()/'.claude')
        self.value['profiles']['claude']=self.value['profiles'].pop('codex')
        self.value['profiles']['claude']['personal']['home']=default
        self.value['teams']['alpha']['roles']['implementer']['provider']='claude'
        self.config.write_text(json.dumps(self.value))
        return default

    def test_receipt_rejects_explicit_default_namespace(self):
        default=self.default_claude_fixture()
        self.create()
        from teamkit.store import Store
        db=Store(self.state)
        try:
            db.claim('T1','owner','claude','personal','fixture','medium')
            db.update('T1','owner','launching',{})
            db.update('T1','owner','submitted',{})
        finally:db.close()
        with patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':default}),patch('teamkit.providers.identity'):
            with self.assertRaises(ValueError):self.call('receipt','T1','--owner','owner')
        self.assertEqual(self.call('show','T1')['state'],'submitted')

    def test_capture_rejects_explicit_default_namespace(self):
        import io,time
        default=self.default_claude_fixture()
        data={'rate_limits':{'five_hour':{'used_percentage':1,'resets_at':time.time()+900},
                             'seven_day':{'used_percentage':2,'resets_at':time.time()+900}}}
        with patch.dict(os.environ,{'CLAUDE_CONFIG_DIR':default}),patch('teamkit.providers.identity'),patch('sys.stdin',io.StringIO(json.dumps(data))):
            with self.assertRaises(ValueError):self.call('capture','--provider','claude','--account','personal')
        self.assertEqual(self.call('route','--provider','claude')['action'],'wait')
