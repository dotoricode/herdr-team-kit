"""Prepared generations are single-use, exact-profile canonical leader slots."""
import copy
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

from test_cli import Fixture
from teamkit import session_pool as pool
from teamkit.store import Store


class FakeNative:
    def __init__(self):
        self.rows = {'tcontroller': {'address': 'tcontroller', 'state': 'live'}}
        self.calls = []
        self.commands = []
        self.drafts = {}
        self.fail_run = False
        self.claude_hint = None

    def call(self, *args, timeout=45):
        if timeout <= 0:raise AssertionError("positive native deadline required")
        self.calls.append(args)
        if args[0] == 'get':
            return copy.deepcopy(self.rows[args[1]])
        if args[0] == 'run':
            if self.fail_run:
                raise OSError('creation outcome unknown')
            address = 't'+str(len(self.rows))
            start = args.index('--')+1
            command = list(args[start:])
            provider_index = next(i for i, arg in enumerate(command) if arg in ('codex', 'claude'))
            provider = command[provider_index]
            labels = dict(args[i+1].split('=', 1) for i, value in enumerate(args) if value == '--label')
            cwd = args[args.index('--cwd')+1]
            self.rows[address] = {'address': address, 'state': 'live', 'name': args[args.index('--name')+1],
                'labels': labels, 'turn': 1, 'cwd': cwd,
                'status': {'state': 'idle', 'app': 'claude-code' if provider == 'claude' else provider},
                'foreground': {'name': provider, 'pid': 100+len(self.rows), 'cwd': cwd,
                               'argv': command[provider_index:]}}
            if provider == 'claude' and self.claude_hint is not None:
                self.rows[address]['turn'] = 0
                self.drafts[address] = '❯ '+self.claude_hint+'\n────────────────\nContext 0% used\n'
            return {'address': address}
        if args[0] == 'wait':
            return {'terminal': args[-1], 'ended': True, 'reason': 'status', 'status': 'idle'}
        if args[0] == 'label':
            self.rows[args[1]]['labels'].update(value.split('=', 1) for value in args[2:])
            return {'ok': True}
        raise AssertionError(args)

    def run(self, args, **kwargs):
        self.commands.append(args)
        if args[:2] == ['hc', 'read']:
            return subprocess.CompletedProcess(args, 0, self.drafts.get(args[-1], '❯ \n'), '')
        if args == ['herdr-app','ls']:
            self.app_reads=getattr(self,'app_reads',0)+1
            if self.app_reads<=getattr(self,'adoption_delay_reads',0):
                return subprocess.CompletedProcess(args,0,json.dumps({'spaces':[]}), '')
            spaces={}
            for row in self.rows.values():
                labels=row.get('labels',{})
                if labels.get('herdr.space') and labels.get('herdr.tab'):
                    spaces.setdefault(labels['herdr.space'],[]).append({'id':labels['herdr.tab'],'terminals':[{'terminal':row['address']}]})
            return subprocess.CompletedProcess(args,0,json.dumps({'spaces':[{'id':key,'tabs':tabs} for key,tabs in spaces.items()]}), '')
        if Path(args[0]).name == 'ensure-atlassian-mcp' or args[:3] == ['herdr-app', 'do', 'tab.rename']:
            return subprocess.CompletedProcess(args, 0, '', '')
        raise AssertionError('unexpected external command: '+repr(args))


class SessionPoolTests(Fixture):
    def setUp(self):
        super().setUp()
        self.value['runtime_backend'] = 'core'
        self.value['profiles']['claude'] = {account: {'home': str(self.root/('claude-'+account)),
            'identity': {'email': account+'@example.invalid'}, 'model': 'claude-fixture', 'effort': 'medium'}
            for account in ('team', 'personal')}
        self.value['teams'] = {team: {'root': str(self.repo), 'roles': {'leader': {
            'provider': 'codex', 'model': 'leader-fixture', 'effort': 'medium'}}}
            for team in ('alpha', 'beta', 'gamma', 'delta')}
        self.bindings = self.root/'bindings.json'
        self.bindings.write_text(json.dumps({'teams': {team: {'space': 'space-'+team} for team in self.value['teams']}}))
        self.value['session_pool'] = {'enabled': True, 'bindings': str(self.bindings),
            'tasks': {team: 'LEADER-'+team.upper() for team in self.value['teams']}}
        self.config.write_text(json.dumps(self.value))
        self.store = Store(self.state)
        self.addCleanup(self.store.close)
        for team, ident in self.value['session_pool']['tasks'].items():
            self.store.create(ident, team, 'leader', str(self.repo), 'a'*40, 'Stand by; no product work.')
        self.fake = FakeNative()
        for target, replacement in (('teamkit.core_runtime._call', self.fake.call),
                ('teamkit.session_pool._call', self.fake.call), ('teamkit.session_pool.subprocess.run', self.fake.run)):
            patched = patch(target, side_effect=replacement)
            patched.start(); self.addCleanup(patched.stop)
        environment = patch.dict(os.environ, {'HC_TERMINAL_ID': 'tcontroller'})
        environment.start(); self.addCleanup(environment.stop)
        identity = patch('teamkit.providers.identity', side_effect=AssertionError('no provider identity during preparation'))
        identity.start(); self.addCleanup(identity.stop)
        self.task = self.store.get('LEADER-ALPHA')
        self.role = {'action': 'dispatch', 'provider': 'codex', 'account': 'personal',
                     'model': 'leader-fixture', 'effort': 'medium'}

    def prepare(self):
        return pool.prepare(self.config, self.state)

    def slot(self):
        return pool.preview(self.value, self.state, self.task, self.role)

    def claimed(self, slot):
        return self.store.claim(self.task['id'], slot['owner'], slot['provider'], slot['account'],
                                slot['model'], slot['effort'], expected_task=self.task)

    def test_prepare_sixteen_native_slots_without_claim_or_model_prompt(self):
        result = self.prepare()
        self.assertEqual(result['counts'], {'prepared': 16})
        self.assertEqual(result['created'], 16)
        self.assertEqual(sum(call[0] == 'run' for call in self.fake.calls), 16)
        self.assertFalse(any(call[0] == 'send' for call in self.fake.calls))
        self.assertTrue(all(task['state'] == 'queued' and task.get('owner') is None for task in self.store.list()))
        for slot in pool._read(self.state)['slots'].values():
            self.assertEqual(slot['spec']['env']['TEAMKIT_OWNER'], slot['owner'])
            self.assertEqual(slot['spec']['env']['TEAMKIT_TASK'], slot['task'])
            self.assertEqual(slot['spec']['args'][-2:], ['--add-dir', str(self.state.resolve().parent)])
            if slot['provider'] == 'claude':
                self.assertIn('--settings', slot['spec']['args'])
        status = json.dumps(pool.status(self.value, self.state))
        for forbidden in ('CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'identity', str(self.root)):
            self.assertNotIn(forbidden, status)

    def test_repeated_prepare_does_not_duplicate_prepared_reserved_or_uncertain(self):
        self.prepare(); slot = pool.reserve(self.value, self.state, self.task, self.role)
        pool.mark_uncertain(self.state, slot, 'inspect')
        self.assertEqual(self.prepare()['created'], 0)
        self.assertEqual(sum(call[0] == 'run' for call in self.fake.calls), 16)
        self.assertEqual(pool.status(self.value, self.state)['counts']['uncertain'], 1)

    def test_preview_is_read_only_and_reserve_requires_official_unowned_task(self):
        self.prepare()
        before = pool._path(self.state).read_bytes()
        slot = self.slot()
        self.assertEqual(pool._path(self.state).read_bytes(), before)
        reserved = pool.reserve(self.value, self.state, self.task, self.role)
        self.assertEqual(reserved['owner'], slot['owner'])
        self.assertEqual(reserved['phase'], 'reserved')
        with self.assertRaises(ValueError): pool.reserve(self.value, self.state, self.task, self.role)

    def test_activate_requires_exact_official_claim_and_does_not_mutate_native_labels(self):
        self.prepare(); slot = pool.reserve(self.value, self.state, self.task, self.role)
        with self.assertRaises(ValueError): pool.activate(self.value, self.state, slot, self.task, slot['spec'])
        task = self.claimed(slot)
        detail = pool.activate(self.value, self.state, slot, task, slot['spec'])
        self.assertEqual(detail['terminal'], slot['terminal'])
        self.assertEqual(detail['native_pid'], slot['native_pid'])
        self.assertEqual(detail['pool_owner'], task['owner'])
        self.assertFalse(any(call[0] == 'label' for call in self.fake.calls))
        with self.assertRaises(ValueError): pool.activate(self.value, self.state, slot, task, slot['spec'])

    def test_matching_owner_claim_with_wrong_account_cannot_activate(self):
        self.prepare(); slot = pool.reserve(self.value, self.state, self.task, self.role)
        wrong = self.store.claim(self.task['id'], slot['owner'], slot['provider'], 'team',
                                 slot['model'], slot['effort'], expected_task=self.task)
        with self.assertRaises(ValueError): pool.activate(self.value, self.state, slot, wrong, slot['spec'])
        self.assertEqual(pool.status(self.value, self.state)['counts']['reserved'], 1)

    def test_corrupt_manifest_never_permits_duplicate_generation(self):
        self.prepare()
        value = pool._read(self.state)
        value['slots'][pool._key('alpha', 'codex', 'personal')] = None
        pool._write(self.state, value)
        calls = len(self.fake.calls)
        with self.assertRaises(ValueError): self.prepare()
        self.assertEqual(len(self.fake.calls), calls+1)
        self.assertEqual(sum(call[0] == 'run' for call in self.fake.calls), 16)

    def test_exact_selected_model_account_profile_no_fallback(self):
        self.prepare()
        for change in ({'model': 'other-model'}, {'effort': 'high'}, {'account': 'unknown'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                pool.reserve(self.value, self.state, self.task, dict(self.role, **change))
        self.value['profiles']['codex']['personal']['home'] = str(self.root/'changed')
        with self.assertRaises(ValueError): self.slot()
        self.assertTrue(all(row['phase'] == 'prepared' for row in pool.status(self.value, self.state)['slots']))

    def test_fresh_claude_ghost_hint_is_saved_exactly_without_model_turn(self):
        self.fake.claude_hint = 'Try "write a fixture helper"'
        self.assertEqual(self.prepare()['counts'], {'prepared': 16})
        role = dict(self.role, provider='claude', model='claude-fixture')
        slot = pool.preview(self.value, self.state, self.task, role)
        self.assertEqual(slot['empty_prompt'], self.fake.claude_hint)
        reserved = pool.reserve(self.value, self.state, self.task, role)
        claimed = self.claimed(reserved)
        detail = pool.activate(self.value, self.state, reserved, claimed, reserved['spec'])
        self.assertEqual(detail['pool_empty_prompt'], self.fake.claude_hint)
        self.assertFalse(any(call[0] == 'send' for call in self.fake.calls))

    def test_saved_hint_does_not_authorize_different_quoted_try_draft(self):
        self.fake.claude_hint = 'Try "write a fixture helper"'; self.prepare()
        role = dict(self.role, provider='claude', model='claude-fixture')
        slot = pool.preview(self.value, self.state, self.task, role)
        self.fake.drafts[slot['terminal']] = '❯ Try "different unsent user draft"\n'
        with self.assertRaises(ValueError): pool.reserve(self.value, self.state, self.task, role)
        self.assertEqual(pool._read(self.state)['slots']['alpha/claude/personal']['empty_prompt'], self.fake.claude_hint)

    def uncertain_claude(self, hint='Try "write a fixture helper"'):
        self.prepare()
        manifest = pool._read(self.state)
        slot = manifest['slots']['alpha/claude/personal']
        slot.update(phase='uncertain', reason='ValueError')
        slot.pop('turn_baseline', None)
        self.fake.rows[slot['terminal']]['turn'] = 0
        self.fake.drafts[slot['terminal']] = '❯ '+hint+'\n'
        pool._write(self.state, manifest)
        return slot

    def test_recheck_repairs_only_recorded_native_without_run_prompt_or_mcp(self):
        old = self.uncertain_claude()
        self.assertEqual(self.prepare()['created'], 0)  # Unknown hint never causes recreation.
        self.fake.calls.clear(); self.fake.commands.clear()
        result = pool.recheck(self.config, self.state)
        slot = pool._read(self.state)['slots']['alpha/claude/personal']
        self.assertEqual(result['rechecked'], 1)
        self.assertEqual(slot['phase'], 'prepared')
        self.assertEqual(slot['owner'], old['owner'])
        self.assertEqual(slot['terminal'], old['terminal'])
        self.assertEqual(slot['turn_baseline'], 0)
        self.assertEqual(slot['empty_prompt'], 'Try "write a fixture helper"')
        self.assertTrue(all(call[0] == 'get' for call in self.fake.calls))
        self.assertTrue(all(args[:2] == ['hc', 'read'] for args in self.fake.commands))

    def test_user_turn_racing_hint_read_is_not_blessed_as_empty(self):
        slot = self.uncertain_claude()
        original_run = self.fake.run
        def read_and_type(args, **kwargs):
            result = original_run(args, **kwargs)
            if args[:2] == ['hc', 'read'] and args[-1] == slot['terminal']:
                self.fake.rows[slot['terminal']]['turn'] = 1
            return result
        with patch('teamkit.session_pool.subprocess.run', side_effect=read_and_type):
            self.assertEqual(pool.recheck(self.config, self.state)['rechecked'], 0)
        row = pool._read(self.state)['slots']['alpha/claude/personal']
        self.assertEqual(row['phase'], 'uncertain')
        self.assertNotIn('empty_prompt', row)

    def test_recheck_preserves_user_turn_app_label_pid_dialog_and_unknown_run(self):
        original = self.uncertain_claude()
        pristine = copy.deepcopy(self.fake.rows[original['terminal']])
        for changed in ('turn', 'space', 'tab', 'pid', 'dialog', 'missing-runtime', 'claimed-owner', 'unknown-run'):
            manifest = pool._read(self.state); slot = manifest['slots']['alpha/claude/personal'] = copy.deepcopy(original)
            native = self.fake.rows[original['terminal']] = copy.deepcopy(pristine)
            self.fake.drafts[original['terminal']] = '❯ Try "write a fixture helper"\n'
            if changed == 'turn': native['turn'] = 1
            elif changed == 'space': native['labels']['herdr.space'] = 'space-alpha'
            elif changed == 'tab': native['labels']['herdr.tab'] = 'tab-alpha'
            elif changed == 'pid': native['foreground']['pid'] += 1
            elif changed == 'dialog': native['status']['state'] = 'blocked'
            elif changed == 'missing-runtime': slot.pop('runtime')
            elif changed == 'unknown-run': slot['reason'] = 'OSError'
            else: self.claimed(slot)
            pool._write(self.state, manifest)
            with self.subTest(changed=changed):
                self.assertEqual(pool.recheck(self.config, self.state)['rechecked'], 0)
                row = pool._read(self.state)['slots']['alpha/claude/personal']
                self.assertEqual(row['phase'], 'uncertain')
                self.assertNotIn('empty_prompt', row)
            if changed == 'claimed-owner':
                task = self.store.get(self.task['id']); task['state'] = 'queued'; task['owner'] = None
                self.store._save(task, 'fixture_reset', {})

    def test_hint_learning_requires_bounded_single_quoted_line_and_no_reserved_learning(self):
        for hint in ('Try unquoted input', 'Try ""', 'Try "'+('x'*201)+'"', 'Try "first" trailing', 'Try "first"\nuser draft'):
            slot = self.uncertain_claude(hint)
            with self.subTest(hint=hint):
                self.assertEqual(pool.recheck(self.config, self.state)['rechecked'], 0)
        for phase in ('prepared', 'reserved', 'leased'):
            slot = self.uncertain_claude()
            slot['phase'] = phase
            self.fake.rows[slot['terminal']]['turn'] = 0
            with self.subTest(phase=phase), self.assertRaises(ValueError):
                pool._verify(slot, self.task, slot['spec'], learn_hint=True)
            self.assertNotIn('empty_prompt', slot)

    def test_empty_claude_prompt_does_not_consume_following_footer_lines(self):
        footer = '  ❯ \u00a0\n────────────────\nContext 18% used\n'
        self.fake.drafts.update({'t'+str(index): footer for index in range(1, 17)})
        self.assertEqual(self.prepare()['counts'], {'prepared': 16})
        self.assertEqual(self.slot()['phase'], 'prepared')
        slot = self.slot()
        self.fake.drafts[slot['terminal']] = '  ❯ unfinished user draft\n────────────────\nContext 18% used\n'
        with self.assertRaises(ValueError): self.slot()

    def test_native_pid_labels_turn_argv_and_draft_mismatch_are_not_reservable(self):
        self.prepare(); slot = self.slot()
        original = copy.deepcopy(self.fake.rows[slot['terminal']])
        for changed in ('pid', 'owner', 'turn', 'argv', 'draft', 'status'):
            native = self.fake.rows[slot['terminal']] = copy.deepcopy(original)
            if changed == 'pid': native['foreground']['pid'] += 1
            elif changed == 'owner': native['labels']['teamkit.owner'] = 'other'
            elif changed == 'turn': native['turn'] += 1
            elif changed == 'argv': native['foreground']['argv'] += ['--add-dir', '/example/extra']
            elif changed == 'draft': self.fake.drafts[slot['terminal']] = '❯ unfinished user draft\n'
            else: native['status']['state'] = 'blocked'
            with self.subTest(changed=changed), self.assertRaises(Exception): self.slot()
            self.fake.drafts.clear()
        self.assertEqual(pool._read(self.state)['slots'][pool._key('alpha', 'codex', 'personal')]['phase'], 'prepared')

    def test_creation_uncertainty_is_durable_and_never_retried(self):
        self.fake.fail_run = True
        self.assertEqual(self.prepare()['counts'], {'uncertain': 16})
        attempts = len(self.fake.calls)
        self.assertEqual(self.prepare()['created'], 0)
        self.assertEqual(len(self.fake.calls), attempts+1)  # Controller liveness only.
        self.assertEqual(sum(call[0] == 'run' for call in self.fake.calls), 16)

    def test_leased_ready_or_awaiting_review_owner_is_not_free_or_replenished(self):
        self.prepare(); slot = pool.reserve(self.value, self.state, self.task, self.role)
        claimed = self.claimed(slot); pool.activate(self.value, self.state, slot, claimed, slot['spec'])
        for state in ('ready', 'awaiting_review'):
            current = self.store.get(self.task['id']); current['state'] = state
            self.store._save(current, 'fixture', {})
            self.assertEqual(self.prepare()['created'], 0)
            with self.assertRaises(ValueError): pool.reserve(self.value, self.state, current, self.role)

    def test_replenish_only_after_clean_native_exit_and_revoked_old_owner(self):
        self.prepare(); slot = pool.reserve(self.value, self.state, self.task, self.role)
        claimed = self.claimed(slot); pool.activate(self.value, self.state, slot, claimed, slot['spec'])
        native = self.fake.rows[slot['terminal']]
        native.update(state='finished', pid=slot['native_pid'], exit_code=0, signal=None)
        self.assertEqual(self.prepare()['created'], 0)  # Original task still owns it.
        self.store.handoff(self.task['id'], slot['owner'], {'head': 'a'*40, 'dirty': False,
            'verification': ['fixture writer stopped'], 'next': 'standby'}, True)
        native['exit_code'] = 1
        self.assertEqual(self.prepare()['created'], 0)
        native['exit_code'] = 0
        self.assertEqual(self.prepare()['created'], 1)
        new = pool.preview(self.value, self.state, self.store.get(self.task['id']), self.role)
        self.assertNotEqual(new['owner'], slot['owner'])
        self.assertNotEqual(new['terminal'], slot['terminal'])
        self.assertEqual(new['generation'], 2)

    def test_claude_settings_spec_tampering_and_noncanonical_tasks_are_rejected(self):
        self.prepare()
        role = dict(self.role, provider='claude', model='claude-fixture')
        slot = pool.reserve(self.value, self.state, self.task, role)
        claimed = self.claimed(slot)
        bad = copy.deepcopy(slot['spec']); bad['args'].remove('--settings')
        with self.assertRaises(ValueError): pool.activate(self.value, self.state, slot, claimed, bad)
        for task in (dict(self.task, id='other'), dict(self.task, role='reviewer'),
                     dict(self.task, work_id='W1'), dict(self.task, classification={'kind': 'design', 'risks': []})):
            self.assertFalse(pool.managed(self.value, task))

    def test_present_labels_only_owned_pool_slots_and_never_focuses(self):
        self.prepare()
        self.assertEqual(pool.present(self.value, self.state), 16)
        self.assertFalse(any('focus' in ' '.join(args) or 'select' in ' '.join(args) for args in self.fake.commands))
        labels = [args for args in self.fake.calls if args[0] == 'label']
        self.assertEqual(len(labels), 16)
        self.assertTrue(all(any(value.startswith('herdr.space=space-') for value in args) for args in labels))

    def test_disabled_status_is_readonly_and_does_not_create_manifest(self):
        self.value.pop('session_pool')
        self.assertEqual(pool.status(self.value, self.state), {'enabled': False, 'slots': [], 'counts': {}})
        self.assertFalse(pool._path(self.state).exists())

    def test_selected_preparation_does_not_touch_other_team_slots(self):
        result=pool.prepare(self.config,self.state,only=('alpha','codex','personal'))
        self.assertEqual(result['created'],1)
        self.assertEqual(len(pool.status(self.value,self.state)['slots']),1)
        self.assertEqual(pool.present(self.value,self.state,only=('alpha','codex','personal')),1)
        self.assertEqual(sum(c[0]=='run' for c in self.fake.calls),1)

    def test_closed_pristine_prepared_slot_can_be_replaced_but_user_turn_cannot(self):
        self.prepare();slot=self.slot();native=self.fake.rows[slot['terminal']]
        native.update(state='finished',pid=slot['native_pid'],exit_code=0,signal=None)
        native['turn']+=1
        self.assertEqual(self.prepare()['created'],0)
        native['turn']=slot['turn_baseline']
        self.assertEqual(pool.prepare(self.config,self.state,only=('alpha','codex','personal'))['created'],1)
        new=self.slot();self.assertEqual(new['generation'],2);self.assertNotEqual(new['owner'],slot['owner'])

    def test_omitted_role_effort_uses_the_same_profile_default_as_dispatch(self):
        from teamkit.__main__ import select_execution
        import time
        for team in self.value['teams'].values():team['roles']['leader'].pop('effort')
        self.value['profiles']['codex']['team']['effort']='low'
        self.config.write_text(json.dumps(self.value));self.prepare()
        now=time.time()
        from teamkit.config import binding,load
        self.store.quota('codex/team',{'binding':binding(self.value['profiles']['codex']['team']),'observed_at':now,
            'five_hour':{'used':1,'resets_at':now+3600},'weekly':{'used':1,'resets_at':now+86400}})
        chosen=select_execution(load(self.config),self.store,self.task,now)
        self.assertEqual(chosen['effort'],'low')
        self.assertEqual(pool.preview(load(self.config),self.state,self.task,chosen)['effort'],'low')

    def test_partial_provider_preference_still_prepares_all_requested_sessions(self):
        self.value['execution_preference']={'provider_order':['codex'],'weekend_account_first':True}
        self.config.write_text(json.dumps(self.value))
        self.assertEqual(self.prepare()['counts'],{'prepared':16})

    def test_present_waits_for_exact_app_adoption_without_new_native_or_focus(self):
        self.prepare();self.fake.adoption_delay_reads=1
        self.assertEqual(pool.present(self.value,self.state,only=('alpha','codex','personal')),1)
        self.assertEqual(self.fake.app_reads,2)
        self.assertEqual(sum(c[0]=='run' for c in self.fake.calls),16)
        self.assertFalse(any('focus' in ' '.join(a) for a in self.fake.commands))

    def test_recheck_retries_read_timeout_without_creating_or_prompting(self):
        original=self.fake.call
        def zero(*args,**kwargs):
            out=original(*args,**kwargs)
            if args[0]=='run':self.fake.rows[out['address']]['turn']=0 if self.fake.rows[out['address']]['foreground']['name']=='claude' else 1
            return out
        with patch('teamkit.core_runtime._call',side_effect=zero),patch('teamkit.session_pool._call',side_effect=zero):self.prepare()
        manifest=pool._read(self.state);slot=manifest['slots'][pool._key('alpha','claude','personal')]
        slot.update(phase='uncertain',reason='TimeoutExpired');pool._write(self.state,manifest)
        self.assertEqual(pool.recheck(self.config,self.state)['rechecked'],1)
        self.assertEqual(sum(c[0]=='run' for c in self.fake.calls),16)
        self.assertFalse(any(c[0]=='send' for c in self.fake.calls))

    def test_codex_late_initial_status_cannot_become_prepared(self):
        from teamkit.core_runtime import CoreRuntime
        clock=[0.0];seen=[];counts={};original=CoreRuntime._native
        def late(actor,name,ready=False,timeout=45):
            native=original(actor,name,ready=ready,timeout=timeout)
            counts[name]=counts.get(name,0)+1
            if actor.task.get('provider')=='codex' and counts[name]==2:
                seen.append(timeout);clock[0]+=4
            return native
        with patch.object(CoreRuntime,'_native',new=late),patch.object(pool.time,'monotonic',side_effect=lambda:clock[0]):
            result=self.prepare()
        self.assertEqual(result['counts'],{'uncertain':8,'prepared':8})
        self.assertTrue(seen)
        self.assertTrue(all(0<t<=3 for t in seen))
