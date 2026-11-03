import unittest
import copy
from unittest.mock import patch

from teamkit.queue_view import board


class QueueViewTest(unittest.TestCase):
    def setUp(self):
        for target in ('subprocess.run', 'subprocess.Popen', 'subprocess.call', 'subprocess.check_output',
                       'subprocess.check_call', 'os.kill', 'teamkit.core_runtime._call'):
            guard = patch(target, side_effect=AssertionError('real HC/UI/process call forbidden: '+target))
            guard.start()
            self.addCleanup(guard.stop)

    def project(self, ticket, **extra):
        payload = dict(schema_version=1, observed_at=100, tickets=[ticket], task_actors=[],
                       native_observation=None, capacity={'available':None}, supervisor={}, profiles={},
                       candidate_slots=16, account_profiles=4, ready_count=0,
                       ready_count_boundary='approval/dependency only')
        payload.update(extra)
        with patch('teamkit.queue_view.load', return_value={'teams':{'example':{}}}), \
             patch('teamkit.queue_view.view', return_value=payload):
            return board('config','state')

    def test_unlimited_capacity_round_trips_without_becoming_unknown_or_numeric(self):
        capacity = dict(global_limit=None, available=None, used=4,
                        profile_limits={'codex/team': 3}, profile_used={'codex/team': 1})
        result = self.project(dict(id='ONE', product='example', state='pending'), capacity=capacity)
        self.assertEqual(result['capacity'], capacity)

    def test_internal_next_action_is_operational_korean_and_raw_value_is_preserved(self):
        ticket = dict(id='ONE', product='example', state='waiting_approval', next_action='check admission',
                      reason='approval missing or changed', role='investigator', origin_id='ORIGINAL')
        original = copy.deepcopy(ticket)
        job = self.project(ticket)['jobs'][0]
        self.assertNotEqual(job['next_action'], ticket['next_action'])
        self.assertIn('승인 범위',job['next_action'])
        self.assertEqual(job['technical_next_action'],'check admission')
        self.assertEqual(job['technical_reason'],'approval missing or changed')
        self.assertEqual(job['phase'],'waiting_approval')
        self.assertEqual(job['role'],'investigator')
        self.assertEqual(job['origin_id'],'ORIGINAL')
        self.assertEqual(ticket, original)

    def test_transport_received_is_not_actor_receipt_even_when_native_is_working(self):
        ticket, observation, actors = self.receiver('received','working')
        job = self.project(ticket, native_observation=observation, task_actors=actors)['jobs'][0]
        self.assertEqual(job['state_label'],'입력 전달 확인 · 실행 수신 대기')
        self.assertFalse(job['verified_working'])
        self.assertIn('재전송', job['next_action'])

    def receiver(self, phase, status):
        executor = dict(terminal='t1', task='ACTOR', owner='original', pid=42, provider='codex')
        ticket = dict(id='ONE',product='example',state=phase,executor=executor,next_action='submit result')
        observation = {'complete':True,'observed_at':100,'terminals':[
            dict(terminal='t1',owner='original',pid=42,provider='codex',status=status)]}
        actors = [dict(id='ACTOR',owner='original',runtime=dict(terminal='t1',native_pid=42))]
        return ticket,observation,actors

    def test_only_matching_fresh_native_working_is_shown_in_progress(self):
        for status in ('idle','done','blocked','unknown','working'):
            with self.subTest(status=status):
                ticket,observation,actors = self.receiver('running',status)
                job = self.project(ticket,native_observation=observation,task_actors=actors)['jobs'][0]
                self.assertEqual(job['state_label'],'진행 중' if status=='working' else '결과 제출 대기')
                self.assertEqual(job['verified_working'],status=='working')
        for changed in ('owner','stale','incomplete'):
            with self.subTest(changed=changed):
                ticket,observation,actors = self.receiver('running','working')
                if changed=='owner': observation['terminals'][0]['owner']='replacement'
                if changed=='stale': observation['observed_at']=1
                if changed=='incomplete': observation['complete']=False
                job = self.project(ticket,native_observation=observation,task_actors=actors)['jobs'][0]
                self.assertEqual(job['state_label'],'결과 제출 대기')
                self.assertFalse(job['verified_working'])

    def test_unknown_and_clarification_preserve_diagnosis_without_success_label(self):
        for phase,expected in [('classification_uncertain','분류 전달 불명'),
                               ('needs_clarification','원 지시 확인 필요'), ('unknown','상태 확인 필요')]:
            with self.subTest(phase=phase):
                job = self.project(dict(id='I',product='operations',state=phase,
                                        reason='unknown delivery outcome',next_action='reconcile original evidence'))['jobs'][0]
                self.assertIn(expected,job['state_label'])
                self.assertIn('원',job['next_action'])
                self.assertEqual(job['technical_reason'],'unknown delivery outcome')
                self.assertFalse(job['work_completion_confirmed'])

    def test_capacity_unknown_and_percentage_data_are_not_invented_or_rescaled(self):
        profiles={'codex/team':{'five_hour':{'used':93},'weekly':{'used':12},'observed_at':100,'eligible':False}}
        result=self.project(dict(id='ONE',product='example',state='pending'),profiles=profiles)
        self.assertIsNone(result['capacity']['available'])
        self.assertEqual(result['profiles'],profiles)

    def test_changed_owner_or_pid_never_looks_like_the_current_receiver(self):
        executor=dict(terminal='t1',task='ACTOR',owner='original',pid=42,provider='codex')
        payload=dict(schema_version=1,tickets=[dict(id='ONE',product='example',state='received',executor=executor)],
                     task_actors=[dict(id='ACTOR',owner='replacement',runtime=dict(terminal='t1',native_pid=99))],
                     native_observation={'terminals':[dict(terminal='t1',owner='replacement',pid=99,provider='codex')]},
                     capacity={},supervisor={},profiles={},candidate_slots=0,account_profiles=4,
                     ready_count=0,ready_count_boundary='approval/dependency only')
        with patch('teamkit.queue_view.load',return_value={'teams':{'example':{}}}), patch('teamkit.queue_view.view',return_value=payload):
            self.assertFalse(board('config','state')['jobs'][0]['receiver_matches_current'])
            payload['task_actors'][0].update(owner='original',runtime=dict(terminal='t1',native_pid=42))
            payload['native_observation']['terminals'][0].update(owner='original',pid=42)
            self.assertTrue(board('config','state')['jobs'][0]['receiver_matches_current'])
