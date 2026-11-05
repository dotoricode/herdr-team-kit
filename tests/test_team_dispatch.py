import json
import os
import time
from pathlib import Path
from unittest.mock import patch

from test_cli import Fixture
from teamkit.config import binding, load
from teamkit.store import Store
from teamkit import team_dispatch as dispatch


class TeamDispatchTests(Fixture):
    def setUp(self):
        super().setUp()
        self.value['runtime_backend'] = 'core'
        self.value['teams']['alpha']['roles']['leader'] = {'provider': 'codex', 'model': 'fixture', 'effort': 'medium'}
        self.value['profiles']['claude'] = {a: {'home': str(self.root/('claude-'+a)), 'identity': {'email': a+'@example.invalid'}, 'model': 'claude-fixture', 'effort': 'medium'} for a in ('team', 'personal')}
        self.value['session_pool'] = {'enabled': True, 'bindings': str(self.root/'bindings.json'), 'tasks': {'alpha': 'L1'}}
        self.value['context_handoff'] = {'enabled': True, 'threshold': 90, 'interval': 10}
        self.value['context_bindings'] = str(self.root/'bindings.json')
        self.value['team_dispatch'] = {'enabled': True, 'tasks': {'alpha': 'D1'}, 'stall_seconds': 60}
        self.config.write_text(json.dumps(self.value))
        self.job = {'id': 'a'*32, 'team': 'alpha', 'work_id': 'W1', 'phase': 'pending', 'reason': 'waiting', 'updated_at': time.time()}

    def prepared(self):
        dispatch.prepare(self.config, self.state)
        return dispatch.status(load(self.config), self.state)

    def ready(self):
        self.prepared()
        store = Store(self.state)
        task = store.get('D1')
        task.update(state='ready', owner='owner-1', attempt_count=1, provider='codex', account='personal', model='fixture', effort='medium', runtime={'backend': 'core', 'name': 'manager', 'terminal': 'tmanager', 'native_pid': 1234, 'identity_checked': True})
        store._save(task, 'fixture')
        now = time.time()
        store.quota('codex/personal', {'binding': binding(self.value['profiles']['codex']['personal']), 'source': 'codex-native-quota', 'observed_at': now, 'five_hour': {'used': 1, 'resets_at': now+1000}, 'weekly': {'used': 2, 'resets_at': now+1000}})
        store.close()
        return task

    def native(self):
        return {'status': {'state': 'idle'}, 'foreground': {'argv': ['codex', '--model', 'fixture', '-c', 'model_reasoning_effort="medium"', '--approve-for-me', '--no-daemon']}}

    def observe(self, jobs=None):
        return dispatch.tick(self.config, self.state, jobs=[self.job] if jobs is None else jobs)

    def test_disabled_status_does_not_create_state(self):
        config = dict(self.value)
        config.pop('team_dispatch')
        self.assertFalse(dispatch.status(config, self.state)['enabled'])
        self.assertFalse(self.state.exists())

    def test_prepare_pins_personal_codex_and_preserves_existing_contract(self):
        self.prepared()
        store = Store(self.state)
        task = store.get('D1')
        self.assertEqual(task['execution_constraint'], {'provider': 'codex', 'account': 'personal'})
        store.close()
        dispatch.prepare(self.config, self.state)
        self.value['team_dispatch']['tasks']['alpha'] = 'L1'
        self.value['session_pool'] = {'tasks': {'alpha': 'L1'}}
        with self.assertRaises(ValueError):
            dispatch.settings(self.value)

    def test_observation_changes_only_semantically_and_does_not_send(self):
        self.prepared()
        with patch('teamkit.pool_control.official') as sent:
            self.observe()
            self.job['updated_at'] = time.time()+1
            self.observe()
            sent.assert_not_called()
        self.assertEqual(len(dispatch.status(load(self.config), self.state)['events']), 1)
        self.job['phase'] = 'received'
        self.observe()
        self.assertEqual(len(dispatch.status(load(self.config), self.state)['events']), 2)

    def test_stall_transition_is_recorded_once(self):
        self.prepared()
        self.job['updated_at'] = time.time()-61
        self.observe(); self.observe()
        events = dispatch.status(load(self.config), self.state)['events']
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]['stalled'])

    def test_pending_without_prepared_manager_does_not_start_agent(self):
        self.prepared(); self.observe()
        with patch('teamkit.pool_control.official') as sent:
            dispatch.advance(self.config, self.state, 'alpha')
            sent.assert_not_called()
        self.assertEqual(dispatch.status(load(self.config), self.state)['events'][0]['phase'], 'pending')

    def test_timeout_is_uncertain_and_not_retried(self):
        self.ready(); self.observe()
        with patch('teamkit.core_runtime.CoreRuntime._native', return_value=self.native()), patch('teamkit.context_handoff.clean_prompt', return_value=True), patch('teamkit.context_handoff.capacity_available', return_value=True), patch('teamkit.pool_control.official', side_effect=TimeoutError) as sent:
            dispatch.advance(self.config, self.state, 'alpha')
            dispatch.advance(self.config, self.state, 'alpha')
            self.assertEqual(sent.call_count, 1)
        self.assertEqual(dispatch.status(load(self.config), self.state)['events'][0]['phase'], 'uncertain')

    def test_capacity_check_and_sending_save_share_the_model_start_lock(self):
        import fcntl, os
        from teamkit import context_handoff
        self.ready(); self.observe()
        lock = str(context_handoff.directory(self.state)/'model-start') + '.lock'
        held = []
        def capacity(state, *a, **k):
            fd = os.open(lock, os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held.append(False)
                fcntl.flock(fd, fcntl.LOCK_UN)
            except BlockingIOError:
                held.append(True)
            finally:
                os.close(fd)
            return True
        with patch('teamkit.core_runtime.CoreRuntime._native', return_value=self.native()), patch('teamkit.context_handoff.clean_prompt', return_value=True), patch('teamkit.context_handoff.capacity_available', side_effect=capacity), patch('teamkit.pool_control.official', side_effect=TimeoutError):
            dispatch.advance(self.config, self.state, 'alpha')
        self.assertEqual(held, [True])

    def test_quota_binding_and_expiry_block_delivery(self):
        self.ready(); self.observe()
        store = Store(self.state)
        store.quota('codex/personal', {'binding': 'wrong', 'observed_at': time.time(), 'five_hour': {'used': 1, 'resets_at': time.time()+1000}, 'weekly': {'used': 1, 'resets_at': time.time()+1000}})
        store.close()
        with patch('teamkit.pool_control.official') as sent:
            dispatch.advance(self.config, self.state, 'alpha')
            sent.assert_not_called()
        self.assertEqual(dispatch.status(load(self.config), self.state)['events'][0]['phase'], 'pending')

    def test_permission_mismatch_does_not_send(self):
        self.ready(); self.observe()
        native = self.native(); native['foreground']['argv'].remove('--approve-for-me')
        with patch('teamkit.core_runtime.CoreRuntime._native', return_value=native), patch('teamkit.pool_control.official') as sent:
            dispatch.advance(self.config, self.state, 'alpha')
            sent.assert_not_called()

    def delivered(self):
        self.ready(); self.observe()
        with patch('teamkit.core_runtime.CoreRuntime._native', return_value=self.native()), patch('teamkit.context_handoff.clean_prompt', return_value=True), patch('teamkit.context_handoff.capacity_available', return_value=True), patch('teamkit.pool_control.official', return_value={}) as sent:
            dispatch.advance(self.config, self.state, 'alpha')
            self.assertEqual(sent.call_args.args[2:4], ('tell', 'D1'))
        return dispatch.status(load(self.config), self.state)['events'][0]

    def test_successful_transport_waits_for_ack_and_does_not_complete_work(self):
        event = self.delivered()
        self.assertEqual(event['phase'], 'received')
        self.assertEqual(self.job['phase'], 'pending')
        with patch('teamkit.pool_control.official') as sent:
            dispatch.advance(self.config, self.state, 'alpha')
            sent.assert_not_called()

    def test_ack_requires_current_actor_and_owner(self):
        event = self.delivered()
        evidence = self.root/'evidence.json'; evidence.write_text(json.dumps({'verification': ['Queue state read; no product changes.']}))
        with patch('teamkit.core_runtime.require_managed', return_value='tother'):
            with self.assertRaises(ValueError):
                dispatch.ack(self.config, self.state, 'alpha', event['id'], 'owner-1', 1, evidence)
        store = Store(self.state); task = store.get('D1'); task['state'] = 'running'; store._save(task, 'fixture'); store.close()
        with patch('teamkit.core_runtime.require_managed', return_value='tmanager'), patch('teamkit.core_runtime.CoreRuntime._native', return_value=self.native()):
            dispatch.ack(self.config, self.state, 'alpha', event['id'], 'owner-1', 1, evidence)
        self.assertEqual(dispatch.status(load(self.config), self.state)['events'][0]['phase'], 'acknowledged')
        self.assertEqual(self.job['phase'], 'pending')

    def test_status_is_read_only(self):
        self.prepared(); self.observe()
        before = self.state.stat()
        dispatch.status(load(self.config), self.state)
        after = self.state.stat()
        self.assertEqual((before.st_mode, before.st_mtime_ns, before.st_size), (after.st_mode, after.st_mtime_ns, after.st_size))

    def test_stall_uses_semantic_age_not_refreshed_timestamp(self):
        self.prepared()
        start = time.time()
        self.job['updated_at'] = start
        with patch('teamkit.team_dispatch.time.time', return_value=start):
            self.observe()
        self.job['updated_at'] = start+61
        with patch('teamkit.team_dispatch.time.time', return_value=start+61):
            self.observe(); self.observe()
        events = dispatch.status(load(self.config), self.state)['events']
        self.assertEqual(len(events), 2)
        self.assertTrue(events[-1]['stalled'])

    def test_busy_or_dirty_manager_preserves_pending_event(self):
        self.ready(); self.observe()
        with patch('teamkit.core_runtime.CoreRuntime._native', return_value={'status': {'state': 'working'}}), patch('teamkit.pool_control.official') as sent:
            dispatch.advance(self.config, self.state, 'alpha')
            sent.assert_not_called()
        with patch('teamkit.core_runtime.CoreRuntime._native', return_value=self.native()), patch('teamkit.context_handoff.clean_prompt', return_value=False), patch('teamkit.pool_control.official') as sent:
            dispatch.advance(self.config, self.state, 'alpha')
            sent.assert_not_called()
        self.assertEqual(dispatch.status(load(self.config), self.state)['events'][0]['phase'], 'pending')

    def test_quota_expired_or_at_limit_does_not_fall_back(self):
        self.ready(); self.observe()
        for age, used in [(301, 1), (0, 90)]:
            now = time.time(); store = Store(self.state)
            store.quota('codex/personal', {'binding': binding(self.value['profiles']['codex']['personal']), 'source': 'codex-native-quota', 'observed_at': now-age, 'five_hour': {'used': used, 'resets_at': now+1000}, 'weekly': {'used': 1, 'resets_at': now+1000}})
            store.close()
            with patch('teamkit.pool_control.official') as sent:
                dispatch.advance(self.config, self.state, 'alpha')
                sent.assert_not_called()
        self.assertEqual(dispatch.status(load(self.config), self.state)['events'][0]['phase'], 'pending')

    def test_restarted_sending_event_is_uncertain_not_resent(self):
        self.ready(); self.observe()
        event = dispatch.status(load(self.config), self.state)['events'][0]
        path = self.state.with_name('team-dispatch')/'events'/(event['id']+'.json')
        event['phase'] = 'sending'; path.write_text(json.dumps(event))
        with patch('teamkit.pool_control.official') as sent:
            dispatch.advance(self.config, self.state, 'alpha')
            dispatch.advance(self.config, self.state, 'alpha')
            sent.assert_not_called()
        self.assertEqual(dispatch.status(load(self.config), self.state)['events'][0]['phase'], 'uncertain')

    def test_actor_ack_before_transport_return_is_preserved(self):
        self.ready(); self.observe()
        def acknowledge(*args):
            event = dispatch.status(load(self.config), self.state)['events'][0]
            store = Store(self.state); task = store.get('D1'); task['state'] = 'running'; store._save(task, 'fixture'); store.close()
            evidence = self.root/'ack.json'; evidence.write_text(json.dumps({'verification': ['Observed current queue; no product mutation.']}))
            dispatch.ack(self.config, self.state, 'alpha', event['id'], 'owner-1', 1, evidence)
            return {}
        with patch('teamkit.core_runtime.require_managed', return_value='tmanager'), patch('teamkit.core_runtime.CoreRuntime._native', return_value=self.native()), patch('teamkit.context_handoff.clean_prompt', return_value=True), patch('teamkit.context_handoff.capacity_available', return_value=True), patch('teamkit.pool_control.official', side_effect=acknowledge):
            dispatch.advance(self.config, self.state, 'alpha')
        self.assertEqual(dispatch.status(load(self.config), self.state)['events'][0]['phase'], 'acknowledged')

    def test_enabled_requires_existing_pool_and_supervisor(self):
        for key in ['context_handoff', 'session_pool']:
            config = json.loads(json.dumps(self.value)); config[key]['enabled'] = False
            with self.assertRaises(ValueError):
                dispatch.settings(config)

    def test_observer_basis_is_independent_of_product_commit(self):
        self.prepared()
        store = Store(self.state); before = store.get('D1'); store.close()
        self.assertEqual(before['basis_kind'], 'instructions')
        import subprocess
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Fixture', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'unrelated product change', '--allow-empty'], check=True)
        dispatch.prepare(self.config, self.state)
        store = Store(self.state); after = store.get('D1'); store.close()
        self.assertEqual(before['base'], after['base'])

    def test_symlinked_message_is_preserved_without_tell(self):
        self.ready(); self.observe()
        event = dispatch.status(load(self.config), self.state)['events'][0]
        target = self.root/'unrelated.txt'; target.write_text('preserve me')
        message = self.state.with_name('team-dispatch')/('message-'+event['id']+'.txt'); message.symlink_to(target)
        with patch('teamkit.core_runtime.CoreRuntime._native', return_value=self.native()), patch('teamkit.context_handoff.clean_prompt', return_value=True), patch('teamkit.context_handoff.capacity_available', return_value=True), patch('teamkit.pool_control.official') as sent:
            dispatch.advance(self.config, self.state, 'alpha')
            sent.assert_not_called()
        self.assertEqual(target.read_text(), 'preserve me')
        self.assertTrue(message.is_symlink())
        self.assertEqual(dispatch.status(load(self.config), self.state)['events'][0]['phase'], 'uncertain')

    def test_symlinked_ack_evidence_is_not_read(self):
        event = self.delivered()
        store = Store(self.state); task = store.get('D1'); task['state'] = 'running'; store._save(task, 'fixture'); store.close()
        target = self.root/'unrelated.json'; target.write_text(json.dumps({'verification': ['unrelated']}))
        evidence = self.root/'evidence-link.json'; evidence.symlink_to(target)
        with patch('teamkit.core_runtime.require_managed', return_value='tmanager'), patch('teamkit.core_runtime.CoreRuntime._native', return_value=self.native()):
            with self.assertRaises(OSError):
                dispatch.ack(self.config, self.state, 'alpha', event['id'], 'owner-1', 1, evidence)
        self.assertEqual(dispatch.status(load(self.config), self.state)['events'][0]['phase'], 'received')
