"""Behavior checks: presentation changes never authorize or complete work."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from teamkit import intake
from teamkit.queue_view import board
from teamkit import queue_view


def organize(*args,**kwargs):
    return queue_view.organize(*args,**kwargs)
from teamkit.tickets import TicketStore, digest


class QueueOrganizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / 'state.sqlite'
        self.queue = TicketStore(self.state)
        self.addCleanup(self.queue.close)
        self.payload = dict(schema_version=1, observed_at=100,
            tickets=[dict(id='ONE', product='alpha', state='running', goal='Implement fixture')],
            intake_pending=[dict(id='CHAT', state='needs_clarification', text='What is this?',
                                 reason='question', next_action='answer', created_at=1)],
            task_actors=[], native_observation=None, capacity={}, supervisor={}, profiles={},
            candidate_slots=0, account_profiles=4, ready_count=0, ready_count_boundary='policy')
        self.addCleanup(patch.stopall)
        patch('teamkit.queue_view.load', return_value={'teams':{'alpha':{}}}).start()
        patch('teamkit.queue_view.view', return_value=self.payload).start()

    def test_conversation_inbox_is_not_a_work_card(self):
        self.assertEqual(['ONE'], [job['id'] for job in board('config', self.state)['jobs']])

    def test_manual_complete_archive_restore_preserves_execution_and_requires_cas(self):
        original = copy.deepcopy(self.payload)
        job = board('config', self.state)['jobs'][0]
        result = organize('config', self.state, 'ONE', 'done', 'request-one', job['board_state']['revision'])
        self.assertEqual('complete', result['manual_status'])
        job = board('config', self.state)['jobs'][0]
        self.assertEqual('running', job['phase'])
        self.assertFalse(job['work_completion_confirmed'])
        self.assertEqual(result, organize('config', self.state, 'ONE', 'done', 'request-one', 'old'))
        with self.assertRaises(ValueError):
            organize('config', self.state, 'ONE', 'archive', 'request-two', 'old')
        organize('config', self.state, 'ONE', 'archive', 'request-two', result['revision'])
        job = board('config', self.state)['jobs'][0]
        self.assertEqual('archived', job['category'])
        self.assertEqual(0, board('config', self.state)['counts']['active'])
        organize('config', self.state, 'ONE', 'restore', 'request-three', job['board_state']['revision'])
        self.assertIsNone(board('config', self.state)['jobs'][0]['board_state']['manual_status'])
        self.assertEqual(original, self.payload)

    def test_reorder_rank_only_changes_private_board_journal(self):
        job=board('config',self.state)['jobs'][0]
        original=self.state.read_bytes()
        result=organize('config',self.state,'ONE','reorder','request-rank-one',job['board_state']['revision'],rank=4.25)
        self.assertEqual(4.25,result['rank'])
        self.assertEqual(original,self.state.read_bytes())
        self.assertEqual('running',board('config',self.state)['jobs'][0]['phase'])
        with self.assertRaisesRegex(ValueError,'같은 요청 ID'):
            organize('config',self.state,'ONE','reorder','request-rank-one',result['revision'],rank=5)

    def test_attachment_summary_is_one_line_but_original_survives(self):
        text='<image name=[Image #1] path="/fixture/paste.png">\n</image>\n[Image #1]\n\n각 스페이스별로 역할이 뭐야?'
        self.payload['tickets'][0]['goal']=text
        job=board('config', self.state)['jobs'][0]
        self.assertEqual('각 스페이스별로 역할이 뭐야?', job['title'])
        self.assertEqual(text, job['request'])

    def test_nonwork_disposition_preserves_evidence_and_does_not_create_ticket(self):
        original=intake.register(self.queue.db, 'What does this mean?', {'key':'chat-one'})
        signature=digest({'text':original['text'], 'source':original['source']})
        with self.queue.transaction():
            item=intake.dismiss(self.queue.db, original['id'], '정보 질문', signature, clock=lambda:5)
        self.assertEqual(original['text'], item['text'])
        self.assertEqual('awaiting_classification', item['dispositions'][0]['previous_state'])
        self.assertEqual('dismissed', item['state'])
        self.assertEqual([], intake.pending(self.queue.db))
        self.assertEqual(0, self.queue.db.execute('SELECT count(*) FROM operation_tickets').fetchone()[0])
        with self.assertRaises(ValueError):
            intake.dismiss(self.queue.db, original['id'], '정보 질문', 'wrong')

    def test_exclusion_refuses_existing_ticket_and_excluded_source_cannot_enqueue(self):
        original=intake.register(self.queue.db, 'Explain fixture', {'key':'source-two'})
        spec=dict(id='TWO', instruction_id=original['id'], product='alpha', role='investigator',
            kind='investigation', goal='Inspect fixture', completion=['report'], checks=['report'],
            profiles=['codex/team'], scope={'paths':[self.temp.name],'actions':['read'],'data':['fixture']},
            resources=[{'kind':'path','name':self.temp.name,'mode':'read'}])
        signature=digest({'text':original['text'], 'source':original['source']})
        with self.queue.transaction():
            intake.dismiss(self.queue.db, original['id'], '정보 질문', signature)
        with self.assertRaisesRegex(ValueError,'non-work'):
            self.queue.enqueue(spec,'source-two-ticket')
        other=intake.register(self.queue.db,'Inspect fixture',{'key':'source-three'})
        spec.update(id='THREE',instruction_id=other['id'])
        self.queue.enqueue(spec,'source-three-ticket')
        with self.assertRaisesRegex(ValueError,'업무 티켓'):
            with self.queue.transaction():
                intake.dismiss(self.queue.db,other['id'],'정보 질문',digest({'text':other['text'],'source':other['source']}))
        self.assertEqual(1,self.queue.db.execute('SELECT count(*) FROM operation_tickets').fetchone()[0])
