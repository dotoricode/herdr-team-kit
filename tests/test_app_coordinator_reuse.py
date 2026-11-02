import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from teamkit import app_coordinator as coordinator


class CoordinatorReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / 'state.sqlite'
        self.config = self.root / 'config.json'
        self.config.write_text('{}')
        self.link = self.root / 'bindings.json'
        self.link.write_text(json.dumps({'inventory': str(self.root / 'inventory.md')}))
        self.request = self.root / 'request.json'
        self.request.write_text(json.dumps({'config': str(self.config), 'state': str(self.state),
            'bindings': str(self.link), 'nonce': 'fixture',
            'config_digest': hashlib.sha256(self.config.read_bytes()).hexdigest()}))
        self.policy = {'runtime_backend': 'core', 'quota_ttl_seconds': 120,
                       'coordinator': {'provider': 'codex', 'root': str(self.root)}}
        self.pr = {'home': str(self.root), 'identity': {}, 'model': 'fixture', 'effort': 'high'}
        self.layout = {'spaces': [{'id': 'sp-existing', 'name': '00 · 운영본부', 'tabs': [
            {'id': 'tb-existing', 'name': '운영본부 · 총괄 · 회사 Codex', 'terminals': []}]}]}
        for target in ('subprocess.Popen', 'subprocess.run', 'subprocess.call',
                       'subprocess.check_output', 'subprocess.check_call',
                       'teamkit.app_coordinator._call', 'teamkit.core_runtime._call',
                       'teamkit.app_coordinator.providers.identity',
                       'teamkit.app_coordinator.CoreRuntime'):
            guard = patch(target, side_effect=AssertionError('unmocked external boundary: ' + target))
            guard.start()
            self.addCleanup(guard.stop)

    def record(self, **extra):
        coordinator.save(coordinator.record_path(self.state),
                         {'phase': 'recovering', 'nonce': 'fixture', **extra})

    def run_fixture(self, error=None):
        def native(*args):
            if args[0] == 'run':
                if error:
                    raise error
                return {'address': 't-new'}
            if args[0] == 'wait':
                return {'ended': True, 'status': 'idle'}
            raise AssertionError('unexpected HC action: ' + repr(args))
        def app(args, **kwargs):
            if args == ['herdr-app', 'ls']:
                return subprocess.CompletedProcess(args, 0, stdout=json.dumps(self.layout))
            if args[:2] == ['herdr-app', 'do']:
                return subprocess.CompletedProcess(args, 0, stdout='{}')
            raise AssertionError('unexpected process: ' + repr(args))
        with patch.object(coordinator, 'require_managed'), \
             patch.object(coordinator, 'load', return_value=self.policy), \
             patch.object(coordinator, 'Store'), \
             patch.object(coordinator, 'cached_quota'), \
             patch.object(coordinator, 'choose_account', return_value='team'), \
             patch.object(coordinator, 'profile', return_value=self.pr), \
             patch.object(coordinator.providers, 'identity'), \
             patch.object(coordinator, 'CoreRuntime') as actor, \
             patch.object(coordinator, 'launch_spec', return_value={'env': {}, 'args': []}), \
             patch.object(coordinator, 'startup_native', return_value={'foreground': {'pid': 123}}), \
             patch('teamkit.workflow.private_root', return_value=self.root), \
             patch('teamkit.context_handoff.settings', return_value={'enabled': False}), \
             patch('teamkit.session_pool.enabled', return_value=False), \
             patch('teamkit.admission.reserve_external', return_value=None), \
             patch('teamkit.admission.bind_external'), \
             patch('teamkit.admission.release_external'), \
             patch.object(coordinator.subprocess, 'run', side_effect=app) as app_call, \
             patch.object(coordinator, '_call', side_effect=native) as hc:
            coordinator.run_job(self.request)
            return hc, app_call, actor

    def test_recovery_reuses_existing_space_and_tab_without_renaming(self):
        self.record(space='sp-existing', tab='tb-existing', terminal='t-old')
        hc, app, _ = self.run_fixture()
        args = hc.call_args_list[0].args
        self.assertIn('herdr.space=sp-existing', args)
        self.assertIn('herdr.tab=tb-existing', args)
        self.assertEqual(app.call_args_list[0].args[0], ['herdr-app', 'ls'])
        self.assertEqual(app.call_count, 1)

    def test_missing_existing_destination_blocks_before_native_launch(self):
        self.record(space='sp-missing', tab='tb-missing', terminal='t-old')
        with self.assertRaisesRegex(ValueError, '복구 대기'):
            self.run_fixture()
        self.assertEqual(coordinator.read_record(self.state)['phase'], 'failed')

    def test_binding_terminal_locates_existing_space_and_tab(self):
        self.record()
        self.link.write_text(json.dumps({'inventory': 'fixture', 'coordinator': {'terminal': 't-old'}}))
        self.layout['spaces'][0]['tabs'][0]['terminals'] = [{'terminal': 't-old'}]
        hc, _, _ = self.run_fixture()
        self.assertIn('herdr.space=sp-existing', hc.call_args_list[0].args)
        self.assertIn('herdr.tab=tb-existing', hc.call_args_list[0].args)

    def test_unknown_native_outcome_preserves_destination_and_blocks_retry(self):
        self.record(space='sp-existing', tab='tb-existing', terminal='t-old')
        with self.assertRaises(TimeoutError):
            self.run_fixture(TimeoutError('unknown'))
        record = coordinator.read_record(self.state)
        self.assertEqual((record['phase'], record['space'], record['tab']),
                         ('uncertain', 'sp-existing', 'tb-existing'))
        with self.assertRaises(ValueError):
            self.run_fixture()

    def test_first_creation_without_any_binding_preserves_explicit_creation_contract(self):
        self.record()
        hc, app, _ = self.run_fixture()
        self.assertTrue(any(arg.startswith('herdr.space=sp') for arg in hc.call_args_list[0].args))
        self.assertEqual(app.call_count, 2)

    def test_start_retains_existing_destination_in_recovery_intent(self):
        self.record(space='sp-existing', tab='tb-existing', terminal='t-old')
        with patch('teamkit.context_handoff.jobs', return_value=[]), \
             patch.object(coordinator, 'status', return_value={'state': 'missing', 'can_recover': True}), \
             patch.object(coordinator, '_call', return_value={'address': 't-controller'}):
            coordinator.start(self.config, self.state, self.link, True)
        record = coordinator.read_record(self.state)
        self.assertEqual((record['space'], record['tab'], record['terminal']),
                         ('sp-existing', 'tb-existing', 't-old'))

    def test_older_recovery_packet_can_reuse_previous_record_without_recreation(self):
        self.record()
        self.request.with_name('previous.json').write_text(json.dumps(
            {'space': 'sp-existing', 'tab': 'tb-existing', 'terminal': 't-old'}))
        hc, _, _ = self.run_fixture()
        self.assertIn('herdr.space=sp-existing', hc.call_args_list[0].args)
        self.assertIn('herdr.tab=tb-existing', hc.call_args_list[0].args)

    def test_ambiguous_conflicting_or_occupied_destination_never_creates_new_ids(self):
        for record, bound, layout in [
            ({'space': None}, {}, self.layout),
            ({'space': 'sp-existing', 'tab': 'tb-existing'}, {'space': 'sp-other'}, self.layout),
            ({'terminal': 't-old'}, {}, {'spaces': []}),
            ({'space': 'sp-existing', 'tab': 'tb-existing'}, {},
             {'spaces': [{'id': 'sp-existing', 'tabs': [{'id': 'tb-existing',
                                                       'terminals': [{'terminal': 't-unrelated'}]}]}]})]:
            with self.subTest(record=record, bound=bound), \
                 patch.object(coordinator.subprocess, 'run', return_value=
                              subprocess.CompletedProcess([], 0, stdout=json.dumps(layout))):
                with self.assertRaisesRegex(ValueError, '복구 대기'):
                    coordinator.destination(record, {'coordinator': bound})

    def test_app_observation_failure_is_recovery_wait_not_first_creation(self):
        with patch.object(coordinator.subprocess, 'run', side_effect=OSError('offline')):
            with self.assertRaisesRegex(ValueError, '복구 대기'):
                coordinator.destination({'space': 'sp-existing', 'tab': 'tb-existing'}, {})


if __name__ == '__main__':
    unittest.main()
