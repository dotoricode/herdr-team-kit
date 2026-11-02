import unittest
from unittest.mock import patch

from teamkit.queue_view import board


class QueueViewTest(unittest.TestCase):
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
