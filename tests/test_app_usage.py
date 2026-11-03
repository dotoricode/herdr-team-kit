"""Account coverage is display metadata, not an admission override."""
import json
import subprocess
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
        self.ready=True;self.timeout_run=False;self.queried=False;self.foreground_name='claude';self.status_app='claude';self.foreground_missing_once=False;self.gets=0;self.wrap_sends=False;self.received_text=None
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
        elif op=='send':
            self.received_text='<hc from="tcontroller">'+args[-1]+'</hc>' if self.wrap_sends else args[-1]
            self.queried=self.received_text=='/usage';value={'started':not self.queried,'status':'idle','turn':0}
        elif op=='keys':
            self.received_text=''.join(args[3:-1]) if args[1]=='--json' else ''.join(args[2:-1])
            self.queried=self.received_text=='/usage';value={'status':'idle','turn':0}
        elif op=='read':return subprocess.CompletedProcess(argv,0,self.screen if self.queried or not self.ready else '❯ ','')
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
        self.assertEqual(native.received_text,'/usage')
        with StoreContext(self.state) as db:self.assertIsNone(db.quota('claude/personal'))

    def test_initial_missing_status_and_foreground_wait_for_same_native_startup(self):
        from teamkit.app_usage import refresh_native_usage
        native=NativeHc()
        def starting(argv, **opts):
            result=native(argv, **opts)
            if argv[1]=='get' and native.gets<=3:
                value=json.loads(result.stdout);value.update(status=None,foreground=None)
                return subprocess.CompletedProcess(argv,0,json.dumps(value),'')
            return result
        with patch('teamkit.app_usage.claude_identity'),patch('teamkit.app_usage.subprocess.run',side_effect=starting),patch('teamkit.app_usage.time.sleep'):
            value=refresh_native_usage(self.value,self.state,'personal')
        self.assertEqual(value['weekly'],32)
        self.assertEqual(sum(a[1]=='run' for a in native.calls),1)
        self.assertEqual(sum(a[1]=='keys' for a in native.calls),1)
        self.assertFalse(any(a[1]=='send' for a in native.calls))
        attempt=json.loads(Path(str(self.state)+'.native-usage-personal.json').read_text())
        self.assertTrue(attempt['zero_token_usage']);self.assertTrue(native.missing)

    def test_missing_startup_metadata_times_out_without_input_or_relaunch(self):
        from teamkit.app_usage import refresh_native_usage,UsageUnavailable
        native=NativeHc();ticks=iter(range(100))
        def starting(argv, **opts):
            result=native(argv, **opts)
            if argv[1]=='get' and native.state=='live':
                value=json.loads(result.stdout);value.update(status=None,foreground=None)
                return subprocess.CompletedProcess(argv,0,json.dumps(value),'')
            return result
        with patch('teamkit.app_usage.claude_identity'),patch('teamkit.app_usage.subprocess.run',side_effect=starting),patch('teamkit.app_usage.time.sleep'),patch('teamkit.app_usage.time.monotonic',side_effect=lambda:next(ticks)):
            with self.assertRaisesRegex(UsageUnavailable,'native_not_ready'):
                refresh_native_usage(self.value,self.state,'team')
        self.assertGreater(native.gets,3)
        self.assertEqual(sum(a[1]=='run' for a in native.calls),1)
        self.assertFalse(any(a[1] in ('send','keys') for a in native.calls))
        self.assertTrue(native.missing)

    def test_idle_metadata_without_an_empty_prompt_never_sends_usage(self):
        from teamkit.app_usage import refresh_native_usage,UsageUnavailable
        native=NativeHc();ticks=iter(range(100))
        def starting(argv, **opts):
            result=native(argv, **opts)
            if argv[1]=='read' and not native.queried:
                return subprocess.CompletedProcess(argv,0,'❯ unfinished draft','')
            return result
        with patch('teamkit.app_usage.claude_identity'),patch('teamkit.app_usage.subprocess.run',side_effect=starting),patch('teamkit.app_usage.time.sleep'),patch('teamkit.app_usage.time.monotonic',side_effect=lambda:next(ticks)):
            with self.assertRaisesRegex(UsageUnavailable,'native_not_ready'):
                refresh_native_usage(self.value,self.state,'team')
        self.assertFalse(any(a[1] in ('send','keys') for a in native.calls))
        self.assertTrue(native.missing)

    def test_idle_metadata_waits_for_the_actual_empty_prompt(self):
        from teamkit.app_usage import refresh_native_usage
        native=NativeHc();reads=[]
        def starting(argv, **opts):
            result=native(argv, **opts)
            if argv[1]=='read' and not native.queried:
                reads.append(True)
                if len(reads)<=3:return subprocess.CompletedProcess(argv,0,'Initializing CLI','')
            return result
        with patch('teamkit.app_usage.claude_identity'),patch('teamkit.app_usage.subprocess.run',side_effect=starting),patch('teamkit.app_usage.time.sleep'):
            refresh_native_usage(self.value,self.state,'personal')
        self.assertEqual(len(reads),4)
        self.assertEqual(sum(a[1]=='keys' for a in native.calls),1)
        self.assertTrue(native.missing)

    def test_failed_or_finished_startup_is_rejected_without_waiting_or_input(self):
        from teamkit.app_usage import refresh_native_usage,UsageUnavailable
        for state,status in (('live','error'),('live','blocked'),('live','working'),('finished',None)):
            with self.subTest(state=state,status=status):
                native=NativeHc()
                def starting(argv, **opts):
                    result=native(argv, **opts)
                    if argv[1]=='get' and native.gets==1:
                        value=json.loads(result.stdout);value.update(state=state,status={'app':'claude','state':status})
                        return subprocess.CompletedProcess(argv,0,json.dumps(value),'')
                    return result
                with patch('teamkit.app_usage.claude_identity'),patch('teamkit.app_usage.subprocess.run',side_effect=starting),patch('teamkit.app_usage.time.sleep') as sleeping:
                    with self.assertRaisesRegex(UsageUnavailable,'native_not_ready'):
                        refresh_native_usage(self.value,self.state,'team')
                sleeping.assert_not_called()
                self.assertFalse(any(a[1] in ('send','keys') for a in native.calls))
                self.assertTrue(native.missing)

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

    def test_controller_context_uses_literal_keys_instead_of_agent_message_send(self):
        import os
        from teamkit.app_usage import refresh_native_usage
        native=NativeHc();native.wrap_sends=True
        with patch.dict(os.environ,{'HC_TERMINAL_ID':'tcontroller','HC_SCOPE':'scope-fixture'}),patch('teamkit.app_usage.claude_identity'),patch('teamkit.app_usage.subprocess.run',side_effect=native):
            value=refresh_native_usage(self.value,self.state,'personal')
        self.assertEqual(value['weekly'],32)
        self.assertEqual(native.received_text,'/usage')

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


class NativeAdmissionTests(UsageFixture):
    def probe(self, account='team', weekly=32):
        from teamkit.app_usage import refresh_native_usage
        native = NativeHc()
        native.screen = native.screen.replace('32% used', str(weekly) + '% used')
        with patch('teamkit.app_usage.claude_identity'), patch('teamkit.app_usage.subprocess.run', side_effect=native):
            refresh_native_usage(self.value, self.state, account)
        return native

    def attempt_path(self, account='team'):
        return Path(str(self.state) + '.native-usage-' + account + '.json')

    def test_verified_official_refresh_enables_company_route_without_quota_injection(self):
        self.assertEqual(self.call('route', '--provider', 'claude')['action'], 'wait')
        native = self.probe()
        self.assertEqual(self.call('route', '--provider', 'claude'), {'account':'team', 'action':'dispatch'})
        self.assertTrue(native.missing)
        self.assertEqual(native.received_text, '/usage')
        with StoreContext(self.state) as db:
            self.assertIsNone(db.quota('claude/team'))

    def test_company_then_personal_then_wait_uses_the_same_ninety_percent_limit(self):
        self.probe(weekly=90)
        self.probe('personal', weekly=38)
        self.assertEqual(self.call('route', '--provider', 'claude')['account'], 'personal')
        self.probe('personal', weekly=95)
        self.assertEqual(self.call('route', '--provider', 'claude')['action'], 'wait')

    def test_unverified_manual_display_alone_does_not_enable_assignment(self):
        with patch('teamkit.app_usage.claude_identity'):
            record_native_observation(self.value, self.state, 'team',
                'Current session\n0% used\nCurrent week (all models)\n0% used', self.now)
        self.assertEqual(self.call('route', '--provider', 'claude')['action'], 'wait')

    def test_failed_incomplete_mismatched_or_expired_probe_never_authorizes(self):
        import copy
        self.probe()
        path = self.attempt_path()
        original = json.loads(path.read_text())
        changes = [
            {'cleaned':False}, {'zero_token_usage':False}, {'error':'native_usage_failed'},
            {'binding':'wrong'}, {'nonce':'wrong'}, {'terminal':'other'},
            {'observed_at':time.time()-301}, {'observed_at':time.time()+30},
            {'attempt_at':time.time()+30}, {'values':{'five_hour':0}},
            {'values':{'five_hour':False, 'weekly':0}},
            {'native':{'foreground_name':'zsh', 'foreground_pid':99, 'status_app':'claude', 'status_state':'idle'}},
        ]
        for change in changes:
            with self.subTest(change=change):
                path.write_text(json.dumps({**copy.deepcopy(original), **change}))
                self.assertEqual(self.call('route', '--provider', 'claude')['action'], 'wait')

    def test_changed_measurement_or_profile_cannot_reuse_probe_attestation(self):
        self.probe()
        observations = Path(str(self.state) + '.usage-observations.json')
        data = json.loads(observations.read_text())
        data['claude/team']['weekly'] = 1
        observations.write_text(json.dumps(data))
        self.assertEqual(self.call('route', '--provider', 'claude')['action'], 'wait')
        self.probe()
        self.value['profiles']['claude']['team']['identity']['email'] = 'changed@example.invalid'
        self.config.write_text(json.dumps(self.value))
        self.assertEqual(self.call('route', '--provider', 'claude')['action'], 'wait')

    def test_latest_response_limits_override_older_native_zero(self):
        self.probe(weekly=0)
        from teamkit.policy import CLAUDE_QUOTA_SOURCE
        now = time.time()
        with StoreContext(self.state) as db:
            db.quota('claude/team', {'source':CLAUDE_QUOTA_SOURCE,
                'binding':binding(self.value['profiles']['claude']['team']), 'observed_at':now,
                'five_hour':{'used':95, 'resets_at':now+300}, 'weekly':{'used':0, 'resets_at':now+600}})
        self.assertEqual(self.call('route', '--provider', 'claude')['action'], 'wait')

    def test_equal_timestamps_use_the_more_restrictive_usage(self):
        self.probe(weekly=95)
        from teamkit.policy import CLAUDE_QUOTA_SOURCE
        at = json.loads(self.attempt_path().read_text())['observed_at']
        with StoreContext(self.state) as db:
            db.quota('claude/team', {'source':CLAUDE_QUOTA_SOURCE,
                'binding':binding(self.value['profiles']['claude']['team']), 'observed_at':at,
                'five_hour':{'used':0, 'resets_at':at+300}, 'weekly':{'used':0, 'resets_at':at+600}})
        self.assertEqual(self.call('route', '--provider', 'claude')['action'], 'wait')

    def test_equal_timestamps_display_matches_the_routed_composition(self):
        self.probe(weekly=95)
        from teamkit.policy import CLAUDE_QUOTA_SOURCE
        from teamkit.config import cached_quota
        at = json.loads(self.attempt_path().read_text())['observed_at']
        with StoreContext(self.state) as db:
            db.quota('claude/team', {'source':CLAUDE_QUOTA_SOURCE,
                'binding':binding(self.value['profiles']['claude']['team']), 'observed_at':at,
                'five_hour':{'used':0, 'resets_at':at+300}, 'weekly':{'used':0, 'resets_at':at+600}})
        self.assertEqual(self.call('route', '--provider', 'claude')['action'], 'wait')
        with patch('teamkit.app_usage.providers.codex_quota', return_value=self.raw), patch('teamkit.app_usage.claude_identity'):
            row = collect(self.value, self.state)['accounts']['claude/team']
        with StoreContext(self.state) as db:
            routed = cached_quota(self.value, db, 'claude', 'team')
        self.assertFalse(row['display_only'])
        self.assertEqual(row['source'], routed['source'])
        self.assertEqual(row['windows']['weekly']['used'], routed['weekly']['used'])
        self.assertEqual(row['windows']['weekly']['used'], 95)
        self.assertEqual(row['windows']['five_hour']['used'], routed['five_hour']['used'])

    def test_equal_timestamp_display_keeps_a_rejected_tied_response(self):
        self.probe(weekly=95)
        from teamkit.policy import CLAUDE_QUOTA_SOURCE
        from teamkit.config import cached_quota
        at = json.loads(self.attempt_path().read_text())['observed_at']
        with StoreContext(self.state) as db:
            db.quota('claude/team', {'source':CLAUDE_QUOTA_SOURCE, 'unavailable':True,
                'binding':binding(self.value['profiles']['claude']['team']), 'observed_at':at,
                'five_hour':{'used':1, 'resets_at':at+300}, 'weekly':{'used':2, 'resets_at':at+600}})
        with patch('teamkit.app_usage.providers.codex_quota', return_value=self.raw), patch('teamkit.app_usage.claude_identity'):
            row = collect(self.value, self.state)['accounts']['claude/team']
        with StoreContext(self.state) as db:
            routed = cached_quota(self.value, db, 'claude', 'team')
        self.assertEqual(row['source'], routed['source'])
        self.assertNotEqual(row['status'], 'query_succeeded')

    def test_private_probe_file_is_required_and_tui_does_not_consume_it(self):
        self.probe()
        path = self.attempt_path()
        path.chmod(0o644)
        self.assertEqual(self.call('route', '--provider', 'claude')['action'], 'wait')
        path.chmod(0o600)
        self.value['runtime_backend'] = 'tui'
        self.config.write_text(json.dumps(self.value))
        self.assertEqual(self.call('route', '--provider', 'claude')['action'], 'wait')


    def test_verified_probe_has_admission_source_in_account_coverage(self):
        self.probe()
        with patch('teamkit.app_usage.providers.codex_quota', return_value=self.raw), patch('teamkit.app_usage.claude_identity'):
            row = collect(self.value, self.state)['accounts']['claude/team']
        self.assertFalse(row['display_only'])
        self.assertEqual(row['source'], 'claude-native-usage')
        self.assertEqual(row['windows']['weekly']['status'], 'provided')
        self.assertNotIn('resets_at', row['windows']['weekly'])

    def test_probe_read_does_not_renew_timestamp_and_rejects_symlink(self):
        self.probe()
        original = self.attempt_path().read_bytes()
        with patch('teamkit.app_usage.time.time', return_value=time.time()+301):
            self.assertEqual(self.call('route', '--provider', 'claude')['action'], 'wait')
        self.assertEqual(self.attempt_path().read_bytes(), original)
        other = self.root/'outside.json'
        other.write_bytes(original)
        other.chmod(0o600)
        self.attempt_path().unlink()
        self.attempt_path().symlink_to(other)
        self.assertEqual(self.call('route', '--provider', 'claude')['action'], 'wait')


    def test_native_source_cannot_bypass_codex_reset_checks(self):
        with StoreContext(self.state) as db:
            db.quota('codex/team', {'source':'claude-native-usage',
                'binding':binding(self.value['profiles']['codex']['team']), 'observed_at':time.time(),
                'five_hour':{'used':0}, 'weekly':{'used':0}})
        self.assertEqual(self.call('route', '--provider', 'codex')['action'], 'wait')

    def test_malformed_tied_response_fails_closed(self):
        self.probe()
        from teamkit.policy import CLAUDE_QUOTA_SOURCE
        at = json.loads(self.attempt_path().read_text())['observed_at']
        with StoreContext(self.state) as db:
            db.quota('claude/team', {'source':CLAUDE_QUOTA_SOURCE,
                'binding':binding(self.value['profiles']['claude']['team']), 'observed_at':at,
                'five_hour':None, 'weekly':None})
        self.assertEqual(self.call('route', '--provider', 'claude')['action'], 'wait')


    def test_tied_response_reset_failure_or_unavailability_is_not_erased(self):
        from teamkit.policy import CLAUDE_QUOTA_SOURCE
        self.probe()
        at = json.loads(self.attempt_path().read_text())['observed_at']
        base = {'source':CLAUDE_QUOTA_SOURCE,
                'binding':binding(self.value['profiles']['claude']['team']), 'observed_at':at,
                'five_hour':{'used':1, 'resets_at':at+300}, 'weekly':{'used':2, 'resets_at':at+600}}
        import copy
        for variant in ('expired_reset', 'missing_reset', 'unavailable'):
            value = copy.deepcopy(base)
            if variant == 'expired_reset': value['five_hour']['resets_at'] = at-1
            elif variant == 'missing_reset': value['weekly'].pop('resets_at')
            else: value['unavailable'] = True
            with self.subTest(variant=variant):
                with StoreContext(self.state) as db: db.quota('claude/team', value)
                self.assertEqual(self.call('route', '--provider', 'claude')['action'], 'wait')

    def test_failed_identity_recheck_invalidates_previous_native_success(self):
        from teamkit.app_usage import refresh_native_usage, UsageUnavailable
        for reason in ('query_failed', 'profile_mismatch'):
            with self.subTest(reason=reason):
                self.probe()
                observation = Path(str(self.state) + '.usage-observations.json').read_bytes()
                with patch('teamkit.app_usage.claude_identity', side_effect=UsageUnavailable(reason)):
                    with self.assertRaisesRegex(UsageUnavailable, reason):
                        refresh_native_usage(self.value, self.state, 'team')
                self.assertEqual(json.loads(self.attempt_path().read_text())['error'], reason)
                self.assertEqual(Path(str(self.state) + '.usage-observations.json').read_bytes(), observation)
                self.assertEqual(self.call('route', '--provider', 'claude')['action'], 'wait')
