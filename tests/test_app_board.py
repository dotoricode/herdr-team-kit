"""Board organization changes presentation, never source business state."""
import copy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from teamkit.app_board import board_action, board_state, presentation, semantic_token


def card():
    return {'kind':'work','id':'WORK-1','title':'샘플 화면 개선','status':'awaiting_approval',
        'status_label':'설계 확인 필요','document_state':'available','document_revision':1,
        'document_digest':'a'*64,'revision':'b'*64,'prompt':'어떤 범위로 진행할까요?',
        'can_answer':True,'leader':{'label':'샘플 팀장','terminal':'t1','space':'s1'},
        'question':None,'decision':None,'interaction':{'state':'decision','column':'review'}}


class PresentationTests(unittest.TestCase):
    def test_unavailable_native_observation_never_claims_no_session_is_running(self):
        for detail in ({'native_error':'관찰 실패'}, {'tasks':[{'current_design':True,'identity_verified':False}]},
                       {'tasks':[{'current_design':True,'identity_verified':True,'native_state':'unknown'}]}):
            with self.subTest(detail=detail):
                shown=presentation(card(),detail)
                self.assertEqual(shown['current_activity'],'실행 상태 미확인')
                self.assertNotEqual(shown['column'],'progress')

    def test_approval_is_a_user_action_but_operational_attention_is_not(self):
        value=card();self.assertTrue(presentation(value)['decision_required'])
        value.update(status='inspect',status_label='실행 결과 확인 필요',reason='실행 기록을 확인해야 합니다.')
        value['interaction']={'state':'execution_unknown','label':'진행 상태 확인 필요','reason':'현재 실행 기록이 없습니다.'}
        shown=presentation(value)
        self.assertFalse(shown['decision_required']);self.assertEqual(shown['column'],'waiting')
        self.assertTrue(shown['attention']);self.assertIn('없음',shown['current_activity'])

    def test_unknown_delivery_never_offers_another_user_decision(self):
        value=card();value['interaction']={'state':'unknown','label':'전달 결과 확인 필요','reason':'호출 결과 불명'}
        value['decision']={'options':[{'id':'retry','label':'의견 전달'}]}
        shown=presentation(value)
        self.assertFalse(shown['decision_required']);self.assertEqual(shown['column'],'waiting')
        self.assertEqual(shown['attention'][0]['label'],'전달 결과 확인 필요')

    def test_only_current_design_verified_native_working_counts_as_execution(self):
        value=card();value.update(status='in_progress',status_label='진행 중');value['interaction']={'state':'progress'}
        task={'id':'I1','role':'implementer','current_design':True,'terminal':'t1','identity_verified':True,
              'native_state':'working','can_focus':True,'goal':'범위 내 구현','session_location':'샘플 공간'}
        shown=presentation(value,{'tasks':[task]})
        self.assertEqual(shown['column'],'progress');self.assertTrue(shown['current_activity'].startswith('실행 중'))
        for changed in ({'current_design':False},{'identity_verified':False},{'native_state':'done'}):
            with self.subTest(changed=changed):
                shown=presentation(value,{'tasks':[{**task,**changed}]})
                self.assertEqual(shown['column'],'waiting')
                self.assertIn('실행 상태 미확인' if changed.get('identity_verified') is False else '확인된 실행 세션 없음',shown['current_activity'])
                if changed.get('current_design') is False:self.assertEqual(shown['sessions'],[])
                if changed.get('identity_verified') is False:self.assertFalse(shown['sessions'][0]['can_focus'])

    def test_authored_brief_is_not_mistaken_for_a_live_process(self):
        shown=presentation(card(),profile={'summary':'선택한 범위만 화면에 반영합니다.','current_activity':'구현 및 검증'})
        self.assertEqual(shown['summary'],'선택한 범위만 화면에 반영합니다.')
        self.assertIn('확인된 실행 세션 없음',shown['current_activity'])
        self.assertIn('작업 내용: 구현 및 검증',shown['current_activity'])

    def test_actionable_question_and_operations_are_separate(self):
        value=card();value.update(status='environment',status_label='실행 준비 필요',reason='환경 준비가 필요합니다.')
        value['interaction']={'state':'decision'};value['question']={'options':[{'option_id':'local','label':'로컬 준비'}]}
        shown=presentation(value)
        self.assertTrue(shown['decision_required']);self.assertEqual(shown['column'],'review');self.assertTrue(shown['attention'])

    def test_receipt_and_recorded_ack_do_not_complete_source_work(self):
        for status in ('sent','responded','deferred','recorded'):
            value=card();value['interaction']={'state':status}
            shown=presentation(value)
            self.assertFalse(shown['decision_required']);self.assertEqual(shown['column'],'waiting')
            self.assertEqual(value['status'],'awaiting_approval')

    def test_completed_source_and_missing_document_are_not_approvable(self):
        value=card();value['document_state']='missing'
        self.assertFalse(presentation(value)['decision_required'])
        value.update(status='complete',status_label='확인 완료')
        self.assertEqual(presentation(value)['column'],'complete')
        value={'kind':'inventory','id':'W1','status':'완료 · 근거 확인','status_label':'완료','title':'운영 구성'}
        self.assertEqual(presentation(value)['column'],'complete')


class BoardJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.state=self.root/'state.sqlite';self.state.write_bytes(b'unchanged business state')
        self.card=card()

    def action(self,action,rid='request-0001',revision=None,value=None):
        value=value or self.card
        return board_action(self.state,value,action,rid,revision or board_state(self.state,value)['revision'])

    def test_read_does_not_create_files_and_done_restore_leave_business_state_unchanged(self):
        before=set(self.root.iterdir());body=self.state.read_bytes()
        initial=board_state(self.state,self.card)
        self.assertEqual(initial['disposition'],'visible');self.assertEqual(set(self.root.iterdir()),before)
        done=self.action('done');self.assertEqual(done['disposition'],'done');self.assertTrue(done['can_restore'])
        restored=self.action('restore','request-0002',done['revision'])
        self.assertEqual(restored['disposition'],'visible');self.assertFalse(restored['can_restore'])
        self.assertEqual(self.state.read_bytes(),body);self.assertEqual(self.card['status'],'awaiting_approval')

    def test_idempotent_old_request_does_not_undo_a_newer_action(self):
        initial=board_state(self.state,self.card)
        self.action('done',revision=initial['revision'])
        latest=self.action('archive','request-0002')
        repeated=self.action('done',revision=initial['revision'])
        self.assertEqual(repeated,latest)
        with self.assertRaisesRegex(ValueError,'같은 요청 ID'):self.action('archive')

    def test_stale_revision_cannot_restore_newer_user_action(self):
        old=self.action('done');self.action('archive','request-0002')
        with self.assertRaisesRegex(ValueError,'바뀌었습니다'):self.action('restore','request-0003',old['revision'])
        self.assertEqual(board_state(self.state,self.card)['disposition'],'archived')

    def test_restore_never_reopens_completed_source_work(self):
        self.card.update(status='complete',status_label='확인 완료')
        self.action('done');self.action('archive','request-0002')
        restored=self.action('restore','request-0003')
        self.assertEqual(restored['disposition'],'visible')
        self.assertEqual(presentation(self.card)['column'],'complete')
        self.assertEqual(self.card['status'],'complete')

    def test_competing_actions_use_compare_and_swap(self):
        revision=board_state(self.state,self.card)['revision'];barrier=threading.Barrier(2);outcomes=[]
        def write(action,rid):
            barrier.wait()
            try:outcomes.append(('ok',board_action(self.state,self.card,action,rid,revision)))
            except ValueError as error:outcomes.append(('rejected',str(error)))
        threads=[threading.Thread(target=write,args=('done','request-0001')),threading.Thread(target=write,args=('archive','request-0002'))]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=5);self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(item[0] for item in outcomes),['ok','rejected'])

    def test_route_and_observation_changes_keep_archive_but_new_question_reopens(self):
        self.action('archive')
        changed=copy.deepcopy(self.card);changed.update(revision='c'*64,status='in_progress',observed_at=123)
        changed['leader'].update(terminal='t9',space='s2',owner='new-owner')
        changed['interaction']={'state':'progress'}
        self.assertEqual(semantic_token(self.card),semantic_token(changed))
        self.assertEqual(board_state(self.state,changed)['disposition'],'archived')
        changed['question']={'question_id':'question-1','question_revision':1,'document_revision':1,'document_digest':'a'*64,
                             'prompt':'추가 범위도 포함할까요?','options':[{'option_id':'yes','label':'포함'}]}
        current=board_state(self.state,changed)
        self.assertEqual(current['disposition'],'visible');self.assertTrue(current['stale']);self.assertFalse(current['can_restore'])

    def test_new_document_reopens_and_stale_question_route_preserves_identity(self):
        q={'question_id':'question-1','question_revision':1,'document_revision':1,'document_digest':'a'*64,'prompt':'범위 선택','options':[]}
        self.card['question']=q;self.action('done')
        changed=copy.deepcopy(self.card);changed['question']=None;changed['previous_question']=q;changed['revision']='c'*64
        self.assertEqual(board_state(self.state,changed)['disposition'],'done')
        changed['document_revision']=2;changed['document_digest']='d'*64
        self.assertEqual(board_state(self.state,changed)['disposition'],'visible')

    def test_new_version_of_same_question_reopens_without_inheriting_old_undo(self):
        question={'question_id':'question-1','question_revision':1,'document_revision':1,'document_digest':'a'*64,
                  'prompt':'범위 선택','options':[{'option_id':'a','label':'선택'}]}
        self.card['question']=question;hidden=self.action('done')
        changed=copy.deepcopy(self.card);changed['question']['question_revision']=2
        latest=board_state(self.state,changed)
        self.assertEqual(latest['disposition'],'visible');self.assertTrue(latest['stale'])
        with self.assertRaisesRegex(ValueError,'바뀌었습니다'):
            board_action(self.state,changed,'restore','request-0002',hidden['revision'])

    def test_interrupted_write_is_retryable_without_replaying_business_work(self):
        original=self.state.read_bytes();revision=board_state(self.state,self.card)['revision']
        with patch('teamkit.app_board.os.replace',side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):board_action(self.state,self.card,'done','request-0001',revision)
        self.assertEqual(board_state(self.state,self.card)['disposition'],'visible')
        self.assertEqual(self.action('done',revision=revision)['disposition'],'done')
        self.assertEqual(self.state.read_bytes(),original)

    def test_journal_is_private_and_corruption_is_not_overwritten(self):
        self.action('done');base=self.root/'app-board'
        self.assertEqual(base.stat().st_mode&0o777,0o700)
        path=next(base.glob('*.json'));self.assertEqual(path.stat().st_mode&0o777,0o600)
        path.write_text('{invalid')
        with self.assertRaisesRegex(ValueError,'정리 기록'):board_state(self.state,self.card)
        with self.assertRaisesRegex(ValueError,'정리 기록'):self.action('archive','request-0002')
        self.assertEqual(path.read_text(),'{invalid')

    def test_inventory_runtime_status_does_not_change_content_identity(self):
        value={'kind':'inventory','id':'W1','title':'운영 구성','prompt':'결과 확인','condition':'화면 확인',
               'status':'진행 중','document_revision':0,'document_digest':'a'*64}
        changed={**value,'status':'완료','document_digest':'b'*64}
        self.assertEqual(semantic_token(value),semantic_token(changed))
        changed['condition']='다른 완료 조건'
        self.assertNotEqual(semantic_token(value),semantic_token(changed))


if __name__=='__main__':unittest.main()
