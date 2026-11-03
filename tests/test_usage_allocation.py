import json
import time
import unittest
from unittest.mock import patch

import test_queue_override as override_fixtures
import test_workers as worker_fixtures
from teamkit import workers, allocation
from teamkit.tickets import TicketStore


class UsageAdmissionTests(unittest.TestCase):
    setUp = override_fixtures.RealAdmitOverrideTest.setUp
    enqueue = override_fixtures.RealAdmitOverrideTest.enqueue
    runtime_for = override_fixtures.RealAdmitOverrideTest.runtime_for

    def test_opt_in_ignores_role_and_provider_policy_but_keeps_quota_and_native(self):
        self.enqueue('ONE')
        runtime = self.runtime_for({'product': 'other', 'roles': []}, {'allocation_policy': 'usage'})
        quota = {'observed_at': time.time(), 'five_hour': {'used': 20, 'resets_at': time.time()+300},
                 'weekly': {'used': 30, 'resets_at': time.time()+300}}
        queue = TicketStore(self.state)
        try:
            ticket = queue.get('ONE')
        finally:
            queue.close()
        with patch('teamkit.providers.identity'), patch('teamkit.operations.cached_quota', return_value=quota):
            self.assertEqual([], runtime.admit_report(ticket, runtime.executors()[0]))
            quota['weekly']['used'] = 90
            self.assertIn('quota', [x['code'] for x in runtime.admit_report(ticket, runtime.executors()[0])])
            quota['weekly']['used'] = 30
            runtime.native = lambda *a: (_ for _ in ()).throw(ValueError('busy'))
            self.assertIn('native', [x['code'] for x in runtime.admit_report(ticket, runtime.executors()[0])])


class UsageWorkerTests(unittest.TestCase):
    setUp = worker_fixtures.WorkerTests.setUp
    ticket = worker_fixtures.WorkerTests.ticket

    def test_usage_mode_offers_both_providers_even_when_role_requires_decision(self):
        self.config['operations']['allocation_policy'] = 'usage'
        ticket = self.ticket(failures=[{'kind':'insufficient_evidence','cause':'fixture','effort':'high'}])
        with patch.object(workers.providers, 'identity'):
            plans = workers.plans(self.config, self.state, ticket)
        self.assertEqual({('claude','team'),('claude','personal'),('codex','team'),('codex','personal')},
                         {(p['provider'], p['account']) for p in plans})
        self.assertTrue(all(p['model']=='profile-model' for p in plans))


    def test_real_executor_discovery_does_not_invoke_missing_role_routing(self):
        from teamkit.operations import NativeRuntime
        self.config['operations']['allocation_policy'] = 'usage'
        ticket = self.ticket(classification={'kind':'design','risks':[]})
        runtime = NativeRuntime(self.config, self.state)
        with patch('teamkit.session_pool._read', return_value={'slots':{}}), \
             patch('teamkit.staffbootstrap.executors', return_value=[]), \
             patch.object(workers.providers, 'identity'):
            candidates = runtime.executors(ticket)
        self.assertEqual(4, len(candidates))


class UsageRefreshTests(unittest.TestCase):
    setUp = worker_fixtures.WorkerTests.setUp

    def test_failed_refresh_is_recorded_and_not_repeated_each_tick(self):
        self.config['operations']['allocation_policy'] = 'usage'
        self.config_path.write_text(json.dumps(self.config))
        with patch('teamkit.config.cached_quota', return_value=None), \
             patch('teamkit.app_usage.refresh_native_usage', side_effect=ValueError('fixture unavailable')) as claude, \
             patch('teamkit.monitor.collect_and_save', side_effect=ValueError('fixture unavailable')) as codex:
            first = allocation.refresh_usage(self.config_path, self.state)
            second = allocation.refresh_usage(self.config_path, self.state)
        self.assertEqual(2, claude.call_count)
        self.assertEqual(2, codex.call_count)
        self.assertEqual(first, second)
        self.assertTrue(all(row['status']=='unavailable' and row['reason']=='fixture unavailable' for row in first.values()))


class UsagePromptTests(unittest.TestCase):
    def test_latest_allocation_instruction_preserves_original_scope_and_goal(self):
        import tempfile
        from pathlib import Path
        from teamkit.operations import prompt
        ticket = {key: {} for key in ('scope', 'approval', 'responsible')}
        ticket.update(id='T1', origin_id='O1', parent_id=None, role='leader', kind='review',
                      goal='Ask designated provider for a read-only review', completion=['Review exists'],
                      checks=['report'], resources=[], profiles=[])
        attempt = {'id':'A1','executor':{'owner':'owner','provider':'codex'}}
        with tempfile.TemporaryDirectory() as directory:
            text = prompt(Path(directory)/'config.json', Path(directory)/'state.sqlite', ticket, attempt,
                          allocation_policy='usage')
        self.assertIn('이 실행자가 원 업무를 직접 수행', text)
        self.assertIn('읽기 전용 제약', text)
        self.assertIn(ticket['goal'], text)
