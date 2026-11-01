"""Account coverage is display metadata, not an admission override."""
import json
import time
from pathlib import Path
from unittest.mock import patch

from test_cli import Fixture, StoreContext
from teamkit.config import binding
from teamkit.policy import eligible
from teamkit.app_usage import collect, read_coverage, record_native_observation, parse_native_usage


class UsageFixture(Fixture):
    def setUp(self):
        super().setUp();self.value['runtime_backend']='core';self.now=time.time()
        self.value['profiles']['claude']={a:{'home':str(self.root/('claude-'+a)),'model':'fixture','identity':{'email':a+'@example.invalid'}} for a in ('team','personal')}
        self.config.write_text(json.dumps(self.value));self.create()
        self.raw={'rateLimits':{'primary':{'usedPercent':21,'windowDurationMins':10080,'resetsAt':self.now+900},'secondary':None}}


class UsageTests(UsageFixture):
    def test_weekly_primary_is_not_a_missing_or_zero_five_hour(self):
        with patch('teamkit.app_usage.providers.codex_quota',return_value=self.raw),patch('teamkit.app_usage.claude_identity'):
            value=collect(self.value,self.state)
        row=value['accounts']['codex/team']
        self.assertEqual(row['status'],'query_succeeded')
        self.assertEqual(row['windows']['five_hour']['status'],'not_provided')
        self.assertNotIn('used',row['windows']['five_hour'])
        self.assertEqual(row['windows']['weekly']['used'],21)
        with StoreContext(self.state) as db:self.assertFalse(eligible(db.quota('codex/team'),time.time()))
    def test_successful_numeric_windows_are_bound_and_do_not_leak_raw_response(self):
        self.raw['secret']='PRIVATE-FIXTURE'
        self.raw['rateLimits']['secondary']={'usedPercent':4,'windowDurationMins':300,'resetsAt':self.now+900}
        with patch('teamkit.app_usage.providers.codex_quota',return_value=self.raw),patch('teamkit.app_usage.claude_identity'):
            value=collect(self.value,self.state)
        self.assertEqual(value['accounts']['codex/team']['windows']['five_hour']['used'],4)
        self.assertNotIn('PRIVATE-FIXTURE',json.dumps(value))
        self.assertEqual(value['accounts']['codex/team']['binding'],binding(self.value['profiles']['codex']['team']))
    def test_claude_auth_success_without_usage_is_not_zero_or_quota(self):
        with patch('teamkit.app_usage.providers.codex_quota',return_value=self.raw),patch('teamkit.app_usage.claude_identity'):
            row=collect(self.value,self.state)['accounts']['claude/team']
        self.assertEqual(row['status'],'not_collected');self.assertEqual(row['action']['kind'],'read_native_usage')
        with StoreContext(self.state) as db:self.assertIsNone(db.quota('claude/team'))
    def test_claude_login_and_query_failure_have_distinct_actions(self):
        from teamkit.app_usage import UsageUnavailable
        def identity(pr):
            raise UsageUnavailable('login_required' if pr['home'].endswith('team') else 'query_failed')
        with patch('teamkit.app_usage.providers.codex_quota',side_effect=TimeoutError),patch('teamkit.app_usage.claude_identity',side_effect=identity):
            accounts=collect(self.value,self.state)['accounts']
        self.assertEqual(accounts['claude/team']['status'],'login_required')
        self.assertEqual(accounts['claude/team']['action']['kind'],'login')
        self.assertEqual(accounts['claude/personal']['status'],'query_failed')
    def test_display_observation_never_creates_claude_admission_quota(self):
        text='Current session\n0% used\nCurrent week (all models)\n100% used\nResets tomorrow'
        with patch('teamkit.app_usage.claude_identity'):
            row=record_native_observation(self.value,self.state,'team',text,self.now)
        self.assertEqual(row['five_hour'],0);self.assertEqual(row['weekly'],100)
        with StoreContext(self.state) as db:self.assertIsNone(db.quota('claude/team'))
        observation=json.loads(Path(str(self.state)+'.usage-observations.json').read_text())['claude/team']
        self.assertEqual(observation['source'],'native-cli-manual-observation')
        with patch('teamkit.app_usage.providers.codex_quota',return_value=self.raw),patch('teamkit.app_usage.claude_identity'):
            row=collect(self.value,self.state)['accounts']['claude/team']
        self.assertTrue(row['display_only']);self.assertEqual(row['windows']['weekly']['used'],100)
    def test_cached_native_bars_cannot_become_a_fresh_observation(self):
        text='Current session\n5% used\nCurrent week (all models)\n40% used\nShowing last-known usage from 10 minutes ago'
        with patch('teamkit.app_usage.claude_identity'):
            with self.assertRaises(ValueError):record_native_observation(self.value,self.state,'team',text,self.now)
        self.assertFalse(Path(str(self.state)+'.usage-observations.json').exists())
    def test_invalid_or_ambiguous_usage_cannot_be_recorded(self):
        for text in ('', 'Current session\n-1% used\nCurrent week (all models)\n2% used', 'Current session\n101% used\nCurrent week (all models)\n2% used',
                     'Current session\n0% used\nCurrent week (Sonnet only)\n9% used'):
            with self.subTest(text=text),self.assertRaises(ValueError):parse_native_usage(text)
    def test_profile_change_and_expiry_do_not_renew_coverage(self):
        with patch('teamkit.app_usage.providers.codex_quota',return_value=self.raw),patch('teamkit.app_usage.claude_identity'):
            value=collect(self.value,self.state)
        later=read_coverage(self.value,self.state,now=time.time()+301)
        self.assertEqual(later['accounts']['codex/team']['status'],'stale')
        self.assertEqual(later['accounts']['codex/team']['observed_at'],value['accounts']['codex/team']['observed_at'])
        self.value['profiles']['codex']['team']['home']=str(self.root/'changed')
        self.assertNotIn('codex/team',read_coverage(self.value,self.state)['accounts'])
    def test_failed_refresh_keeps_old_timestamp_and_invalidates_admission(self):
        self.raw['rateLimits']['secondary']={'usedPercent':4,'windowDurationMins':300,'resetsAt':self.now+900}
        with patch('teamkit.app_usage.providers.codex_quota',return_value=self.raw),patch('teamkit.app_usage.claude_identity'):
            old=collect(self.value,self.state)['accounts']['codex/team']
        with patch('teamkit.app_usage.providers.codex_quota',side_effect=TimeoutError),patch('teamkit.app_usage.claude_identity'):
            row=collect(self.value,self.state)['accounts']['codex/team']
        self.assertEqual(row['status'],'query_failed');self.assertEqual(row['observed_at'],old['observed_at'])
        with StoreContext(self.state) as db:self.assertFalse(eligible(db.quota('codex/team'),time.time()))
    def test_window_reset_expiry_invalidates_display_before_ttl(self):
        self.raw['rateLimits']['primary']['resetsAt']=self.now+5
        with patch('teamkit.app_usage.providers.codex_quota',return_value=self.raw),patch('teamkit.app_usage.claude_identity'):
            collect(self.value,self.state)
        row=read_coverage(self.value,self.state,now=self.now+6)['accounts']['codex/team']
        self.assertEqual(row['status'],'stale');self.assertEqual(row['windows']['weekly']['status'],'stale')

    def test_corrupt_display_observations_do_not_block_other_accounts(self):
        Path(str(self.state)+'.usage-observations.json').write_text('not json')
        with patch('teamkit.app_usage.providers.codex_quota',return_value=self.raw),patch('teamkit.app_usage.claude_identity'):
            accounts=collect(self.value,self.state)['accounts']
        self.assertEqual(accounts['codex/team']['status'],'query_succeeded')
        self.assertEqual(accounts['claude/team']['reason'],'observation_unreadable')
        self.assertEqual(Path(str(self.state)+'.usage-observations.json').read_text(),'not json')

    def test_tui_config_is_rejected_before_any_provider_or_write(self):
        self.value['runtime_backend']='tui'
        with patch('teamkit.app_usage.providers.codex_quota') as provider,self.assertRaises(ValueError):collect(self.value,self.state)
        provider.assert_not_called()


class NativeHc:
    def __init__(self):
        self.calls=[];self.labels={};self.state='live';self.missing=False
        self.screen='Session\nUsage: 0 input, 0 output, 0 cache read, 0 cache write\nCurrent session\n0% used\nCurrent week (all models)\n32% used'
        self.ready=True;self.timeout_run=False;self.queried=False;self.foreground_name='claude';self.status_app='claude';self.foreground_missing_once=False;self.gets=0
    def __call__(self,argv,**opts):
        import subprocess
        self.calls.append(argv);args=argv[1:];op=args[0]
        if op=='run':
            self.labels={args[i+1].split('=',1)[0]:args[i+1].split('=',1)[1] for i,a in enumerate(args) if a=='--label'}
            if self.timeout_run:raise subprocess.TimeoutExpired(argv,opts.get('timeout'))
            value={'address':'t9','state':'live'}
        elif op=='get':
            self.gets+=1
            if self.missing:return subprocess.CompletedProcess(argv,3,'','not_found')
            value={'address':'t9','state':self.state,'labels':self.labels,'name':'usage-test',
                   'foreground':None if self.foreground_missing_once and self.gets==1 else {'pid':99,'name':self.foreground_name},'status':{'app':self.status_app,'state':'idle'} if self.ready else None}
        elif op=='wait':value={'ended':self.ready,'reason':'status' if self.ready else 'timeout','status':'idle' if self.ready else None}
        elif op=='send':self.queried=True;value={'started':False,'status':'idle','turn':0}
        elif op=='read':return subprocess.CompletedProcess(argv,0,self.screen if self.queried or not self.ready else 'Ready for input','')
        elif op=='ls':value={'terminals':[{'address':'t9','state':self.state}]} if self.labels and not self.missing else {'terminals':[]}
        elif op=='kill':self.state='finished';value={'ok':True}
        elif op=='rm':self.missing=True;value={'ok':True}
        else:raise AssertionError(argv)
        return subprocess.CompletedProcess(argv,0,json.dumps(value),'')


class NativeUsageTests(UsageFixture):
    def test_explicit_native_refresh_records_builtin_bars_and_cleans_owned_terminal(self):
        from teamkit.app_usage import refresh_native_usage
        native=NativeHc()
        with patch('teamkit.app_usage.claude_identity'),patch('teamkit.app_usage.subprocess.run',side_effect=native):
            value=refresh_native_usage(self.value,self.state,'personal')
        self.assertEqual(value['weekly'],32);self.assertTrue(native.missing)
        launch=next(a for a in native.calls if a[1]=='run');self.assertIn('--safe-mode',launch);self.assertNotIn('-p',launch)
        sends=[a for a in native.calls if a[1]=='send'];self.assertEqual(len(sends),1);self.assertEqual(sends[0][-1],'/usage')
        with StoreContext(self.state) as db:self.assertIsNone(db.quota('claude/personal'))

    def test_os_process_name_is_distinct_from_reported_claude_program_id(self):
        from teamkit.app_usage import refresh_native_usage
        native=NativeHc();native.foreground_name='claude.exe';native.status_app='claude-code'
        with patch('teamkit.app_usage.claude_identity'),patch('teamkit.app_usage.subprocess.run',side_effect=native):
            value=refresh_native_usage(self.value,self.state,'personal')
        self.assertEqual(value['weekly'],32);self.assertTrue(native.missing)

    def test_idle_report_waits_for_missing_os_foreground_on_the_same_terminal(self):
        from teamkit.app_usage import refresh_native_usage
        native=NativeHc();native.foreground_missing_once=True;native.foreground_name='claude.exe';native.status_app='claude-code'
        with patch('teamkit.app_usage.claude_identity'),patch('teamkit.app_usage.subprocess.run',side_effect=native):
            value=refresh_native_usage(self.value,self.state,'team')
        self.assertEqual(value['weekly'],32);self.assertTrue(native.missing)
        self.assertEqual(sum(a[1]=='run' for a in native.calls),1)

    def test_os_name_fix_does_not_accept_an_unrelated_process_or_status(self):
        from teamkit.app_usage import refresh_native_usage,UsageUnavailable
        for name,app in (('zsh','claude-code'),('claude.exe','other')):
            with self.subTest(name=name,app=app):
                native=NativeHc();native.foreground_name=name;native.status_app=app
                with patch('teamkit.app_usage.claude_identity'),patch('teamkit.app_usage.subprocess.run',side_effect=native):
                    with self.assertRaisesRegex(UsageUnavailable,'native_not_ready'):refresh_native_usage(self.value,self.state,'team')
                self.assertTrue(native.missing);self.assertFalse(any(a[1]=='send' for a in native.calls))

    def test_native_trust_dialog_is_not_approved_or_reported_as_usage(self):
        from teamkit.app_usage import refresh_native_usage,UsageUnavailable
        native=NativeHc();native.ready=False;native.screen='Allow external CLAUDE.md file imports?'
        with patch('teamkit.app_usage.claude_identity'),patch('teamkit.app_usage.subprocess.run',side_effect=native):
            with self.assertRaisesRegex(UsageUnavailable,'trust'):refresh_native_usage(self.value,self.state,'team')
        self.assertTrue(native.missing);self.assertFalse(any(a[1] in ('send','keys') for a in native.calls))
        self.assertFalse(Path(str(self.state)+'.usage-observations.json').exists())

    def test_native_cached_usage_keeps_old_observation_and_reports_failure(self):
        from teamkit.app_usage import refresh_native_usage,UsageUnavailable
        with patch('teamkit.app_usage.claude_identity'):
            old=record_native_observation(self.value,self.state,'team','Current session\n1% used\nCurrent week (all models)\n10% used',self.now)
        native=NativeHc();native.screen+='\nShowing last-known usage from 5 minutes ago'
        with patch('teamkit.app_usage.claude_identity'),patch('teamkit.app_usage.subprocess.run',side_effect=native):
            with self.assertRaises(UsageUnavailable):refresh_native_usage(self.value,self.state,'team')
        self.assertEqual(json.loads(Path(str(self.state)+'.usage-observations.json').read_text())['claude/team'],old)
        with patch('teamkit.app_usage.providers.codex_quota',return_value=self.raw),patch('teamkit.app_usage.claude_identity'):
            row=collect(self.value,self.state)['accounts']['claude/team']
        self.assertEqual(row['status'],'query_failed');self.assertEqual(row['observed_at'],old['observed_at'])

    def test_native_launch_timeout_never_relaunches_and_recovers_only_nonce_owned_terminal(self):
        from teamkit.app_usage import refresh_native_usage,UsageUnavailable
        native=NativeHc();native.timeout_run=True
        with patch('teamkit.app_usage.claude_identity'),patch('teamkit.app_usage.subprocess.run',side_effect=native):
            with self.assertRaises(UsageUnavailable):refresh_native_usage(self.value,self.state,'team')
        self.assertEqual(sum(a[1]=='run' for a in native.calls),1);self.assertTrue(native.missing)
