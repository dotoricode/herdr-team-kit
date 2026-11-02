import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from teamkit import intake
from teamkit.tickets import TicketStore


class IntakeTest(unittest.TestCase):
    def test_original_message_is_durable_and_never_implicitly_approved(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'state.sqlite'
            queue=TicketStore(path)
            try:
                first=intake.register(queue.db,'Inspect this fixture',{'key':'turn-one'})
                self.assertEqual(first,intake.register(queue.db,'Inspect this fixture',{'key':'turn-one'}))
                self.assertEqual('awaiting_classification',intake.pending(queue.db)[0]['state'])
                self.assertEqual([],queue.ready())
                with self.assertRaises(ValueError):
                    intake.register(queue.db,'Changed message',{'key':'turn-one'})
            finally:
                queue.close()

    def test_capture_restart_partial_line_and_agent_message_exclusion(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); state=root/'state.sqlite'; journal=root/'journal.jsonl'
            TicketStore(state).close()
            def user(text):
                return dict(type='response_item',payload=dict(role='user',content=[dict(type='input_text',text=text)]))
            journal.write_text(json.dumps(user('old history'))+'\n')
            config=dict(coordinator={'provider':'codex'},profiles={'codex':{'personal':{'home':temp}}},operations={'intake':{'enabled':True}})
            record=dict(context_session_id='fixture-session',account='personal',terminal='t1')
            with patch('teamkit.app_coordinator.read_record',return_value=record), patch('teamkit.context_observation._rollout_path',return_value=journal):
                self.assertEqual([],intake.capture(config,state)['captured'])
                message=json.dumps(user('new instruction'))
                with journal.open('a') as output: output.write(message)
                self.assertEqual([],intake.capture(config,state)['captured'])
                with journal.open('a') as output:
                    output.write('\n'+json.dumps(user('<hc from="worker">reply</hc>'))+'\n')
                    output.write(json.dumps(user('<herdr-intake-classify instruction="one">automatic classification</herdr-intake-classify>'))+'\n')
                    output.write(json.dumps(dict(type='event_msg',payload=dict(type='user_message',message='duplicate alternate format')))+'\n')
                self.assertEqual(1,len(intake.capture(config,state)['captured']))
                self.assertEqual([],intake.capture(config,state)['captured'])
            queue=TicketStore(state)
            try:
                self.assertEqual(['new instruction'],[x['text'] for x in intake.pending(queue.db)])
            finally: queue.close()


if __name__=='__main__': unittest.main()
