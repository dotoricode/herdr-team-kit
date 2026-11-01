"""Card replies record feedback; they never approve or deliver product work."""
import copy
import json
import os
from pathlib import Path
from unittest.mock import patch

from test_cli import Fixture
import test_workflow
from teamkit.app_feedback import card_detail, card_answer, feedback_summaries


class FeedbackFixture(Fixture):
    setup_work=test_workflow.WorkflowTests.setup_work
    def setUp(self):
        super().setUp();self.value['runtime_backend']='core';self.config.write_text(json.dumps(self.value));self.create()
        self.inventory=self.root/'WORK_STATUS.md'
        self.inventory.write_text('| W47 | 화면 상호작용 확인 | 사용자 확인 대기 · 실제 화면 필요 | 화면을 열고 저장/조회 결과 확인 |\n| W48 | 이전 완료 항목 | 완료 · 검사 통과 | 기존 완료 근거 |\n')
        self.links=self.root/'bindings.json';self.linked={'version':1,'actions_enabled':True,'inventory':str(self.inventory),'card_prompts':{'inventory:W47':'화면에서 확인한 결과를 알려 주세요.'},'teams':{'alpha':{'label':'Alpha 팀장','terminal':'t1','space':'s1'}}};self.links.write_text(json.dumps(self.linked))
    def detail(self,ident='W47',kind='inventory'):
        return card_detail(self.config,self.state,self.links,kind,ident)
    def answer(self,choice='confirmed',text='',revision=None,ident='W47',kind='inventory'):
        revision=revision or self.detail(ident,kind)['revision']
        return card_answer(self.config,self.state,self.links,kind,ident,revision,choice,text)
    @property
    def feedback_root(self):return self.state.parent/'app-feedback'


class FeedbackTests(FeedbackFixture):
    def test_inventory_detail_uses_real_status_condition_and_coordinator_without_product_session(self):
        before=self.state.read_bytes();files=set(self.root.rglob('*'))
        value=self.detail()
        self.assertEqual(value['status'],'사용자 확인 대기 · 실제 화면 필요');self.assertEqual(value['status_label'],'확인 필요')
        self.assertEqual(value['condition'],'화면을 열고 저장/조회 결과 확인')
        self.assertEqual(value['team_label'],'팀 총괄');self.assertEqual(value['leader']['label'],'팀 총괄')
        self.assertIsNone(value['leader']['terminal']);self.assertIn('제품',value['leader']['reason'])
        self.assertEqual(value['prompt'],'화면에서 확인한 결과를 알려 주세요.')
        self.assertEqual(len(value['revision']),64);self.assertTrue(value['can_answer']);self.assertIsNone(value['answer'])
        self.assertEqual(before,self.state.read_bytes());self.assertEqual(files,set(self.root.rglob('*')))
    def test_confirmed_reply_persists_without_approving_or_prompting(self):
        before=self.state.read_bytes();source=self.inventory.read_bytes()
        with patch('teamkit.providers.identity') as provider,patch('teamkit.app_bridge.core_read') as native:
            result=self.answer(text='직접 저장과 다시 열기를 확인했습니다.')
            provider.assert_not_called();native.assert_not_called()
        self.assertEqual(result['answer']['delivery'],'recorded');self.assertIn('총괄 검토 대기',result['message'])
        self.assertEqual(self.detail()['answer'],result['answer'])
        self.assertEqual(before,self.state.read_bytes());self.assertEqual(source,self.inventory.read_bytes())
        self.assertEqual(self.feedback_root.stat().st_mode&0o777,0o700)
        for name in ('answers.jsonl','latest.json'):self.assertEqual((self.feedback_root/name).stat().st_mode&0o777,0o600)
    def test_changes_and_defer_require_meaningful_text(self):
        for choice,text in (('changes',''),('changes',' \n '),('defer',''),('unknown','text'),('', 'text'),('confirmed','x'*4001)):
            with self.subTest(choice=choice,text=len(text)),self.assertRaises(ValueError):self.answer(choice,text)
        self.assertFalse(self.feedback_root.exists())
    def test_stale_source_rejects_without_recording(self):
        old=self.detail()['revision'];self.inventory.write_text(self.inventory.read_text().replace('실제 화면 필요','화면 재확인 필요'))
        with self.assertRaises(ValueError):self.answer(revision=old)
        self.assertFalse(self.feedback_root.exists())
    def test_disabled_actions_and_completed_cards_reject_write(self):
        old=self.detail()['revision'];self.linked['actions_enabled']=False;self.links.write_text(json.dumps(self.linked))
        self.assertFalse(self.detail()['can_answer'])
        with self.assertRaises(ValueError):self.answer(revision=old)
        self.linked['actions_enabled']=True;self.links.write_text(json.dumps(self.linked))
        self.assertFalse(self.detail('W48')['can_answer'])
        with self.assertRaises(ValueError):self.answer(ident='W48')
        self.assertFalse(self.feedback_root.exists())
    def test_identical_replay_returns_original_answer_without_second_append(self):
        first=self.answer('changes','설명 부분을 다시 확인해 주세요.');journal=(self.feedback_root/'answers.jsonl').read_bytes()
        second=self.answer('changes','설명 부분을 다시 확인해 주세요.')
        self.assertEqual(first,second);self.assertEqual(journal,(self.feedback_root/'answers.jsonl').read_bytes())
    def test_changed_source_keeps_previous_reply_separate_from_current_answer(self):
        saved=self.answer()['answer'];self.inventory.write_text(self.inventory.read_text().replace('사용자 확인 대기 · 실제 화면 필요','완료 · 사용자 확인 반영'))
        value=self.detail();self.assertIsNone(value['answer']);self.assertEqual(value['previous_answer'],saved);self.assertFalse(value['can_answer'])
        summaries=feedback_summaries(self.state)
        self.assertEqual(summaries['inventory:W47']['answer'],saved)
    def test_append_failure_does_not_report_saved_or_change_source(self):
        source=self.inventory.read_bytes();before=self.state.read_bytes()
        with patch('teamkit.app_feedback._append_record',side_effect=OSError('disk full')):
            with self.assertRaises(ValueError):self.answer()
        self.assertIsNone(self.detail()['answer']);self.assertEqual(source,self.inventory.read_bytes());self.assertEqual(before,self.state.read_bytes())
    def test_latest_write_failure_replays_durable_history_without_duplicate(self):
        with patch('teamkit.app_feedback._atomic_latest',side_effect=OSError('cache failed')):
            with self.assertRaisesRegex(ValueError,'이력'):self.answer('defer','다음 사용 때 확인합니다.')
        saved=self.detail()['answer'];self.assertEqual(saved['choice'],'defer')
        retry=self.answer('defer','다음 사용 때 확인합니다.')
        self.assertEqual(retry['answer'],saved)
        self.assertEqual(len((self.feedback_root/'answers.jsonl').read_text().splitlines()),1)
    def test_source_change_during_save_is_rechecked_before_append(self):
        old=self.detail()['revision']
        def changed_time():
            self.inventory.write_text(self.inventory.read_text().replace('실제 화면 필요','새 질문 확인 필요'));return 1234567890
        with patch('teamkit.app_feedback.time.time',side_effect=changed_time),self.assertRaises(ValueError):self.answer(revision=old)
        self.assertFalse((self.feedback_root/'answers.jsonl').exists())
    def test_work_feedback_uses_product_team_and_preserves_phase_and_approval(self):
        self.setup_work();self.call('work','submit','W1');before=self.state.read_bytes();value=self.detail('W1','work')
        self.assertEqual(value['team_label'],'Alpha 팀장');self.assertEqual(value['leader']['terminal'],'t1')
        result=self.answer('confirmed','문서를 읽었습니다.',value['revision'],'W1','work')
        self.assertEqual(result['answer']['delivery'],'recorded');self.assertEqual(before,self.state.read_bytes())
        self.assertEqual(self.call('work','show','W1')['phase'],'awaiting_approval')
        self.assertNotIn('approval',self.call('work','show','W1'))
    def test_missing_document_allows_generic_feedback_but_not_design_approval(self):
        from teamkit.app_bridge import document_view
        self.setup_work();self.call('work','submit','W1');old=self.detail('W1','work')['revision']
        plan=self.root/'private-plans/W1/r1/plan.md';plan.unlink();before=self.state.read_bytes()
        value=self.detail('W1','work')
        self.assertEqual(value['document_state'],'missing');self.assertTrue(value['can_answer']);self.assertNotEqual(value['revision'],old)
        with self.assertRaises(ValueError):self.answer('confirmed','old draft',old,'W1','work')
        result=self.answer('changes','문서 파일이 없어 내용을 확인하지 못했습니다.',value['revision'],'W1','work')
        self.assertEqual(result['answer']['delivery'],'recorded');self.assertEqual(before,self.state.read_bytes())
        with self.assertRaises(OSError):document_view(self.config,self.state,'W1')
        self.assertEqual(self.call('work','show','W1')['phase'],'awaiting_approval')
    def test_question_override_is_bound_to_the_open_card_revision(self):
        old=self.detail()['revision'];self.linked['card_prompts']['inventory:W47']='새 화면에서 확인한 결과를 알려 주세요.'
        self.links.write_text(json.dumps(self.linked))
        self.assertNotEqual(self.detail()['revision'],old)
        with self.assertRaises(ValueError):self.answer(revision=old)
        self.assertFalse(self.feedback_root.exists())
    def test_product_document_change_invalidates_the_open_card(self):
        self.setup_work();self.call('work','submit','W1');old=self.detail('W1','work')['revision']
        (self.root/'private-plans/W1/r1/plan.md').write_text('Changed design body')
        with self.assertRaises(ValueError):self.answer('confirmed','확인',old,'W1','work')
        self.assertFalse(self.feedback_root.exists())
    def test_tui_binding_and_unsafe_latest_do_not_write_feedback(self):
        old=self.detail()['revision'];self.value['runtime_backend']='tui';self.config.write_text(json.dumps(self.value))
        with self.assertRaises(ValueError):self.answer(revision=old)
        self.assertFalse(self.feedback_root.exists())
        self.value['runtime_backend']='core';self.config.write_text(json.dumps(self.value));self.feedback_root.mkdir(mode=0o700)
        target=self.root/'other.json';target.write_text('original');(self.feedback_root/'latest.json').symlink_to(target)
        with self.assertRaises(ValueError):self.answer(revision=old)
        self.assertEqual(target.read_text(),'original');self.assertFalse((self.feedback_root/'answers.jsonl').exists())
    def test_corrupt_or_partial_history_is_not_silently_replaced(self):
        self.answer();history=self.feedback_root/'answers.jsonl';history.write_text(history.read_text()+'{"partial":')
        before=history.read_bytes()
        with self.assertRaises(ValueError):self.detail()
        with self.assertRaises(ValueError):self.answer(revision='a'*64)
        self.assertEqual(history.read_bytes(),before)
    def test_unknown_kind_id_and_symlink_store_are_rejected(self):
        for kind,ident in (('other','W47'),('inventory','missing'),('inventory','../W47')):
            with self.subTest(kind=kind,ident=ident),self.assertRaises(ValueError):self.detail(ident,kind)
        target=self.root/'elsewhere';target.mkdir();self.feedback_root.symlink_to(target,target_is_directory=True)
        with self.assertRaises(ValueError):self.answer()
        self.assertEqual(list(target.iterdir()),[])


class FeedbackBridgeTests(FeedbackFixture):
    def test_view_attaches_only_revision_current_feedback_and_preserves_question(self):
        from teamkit.app_bridge import view
        saved=self.answer('changes','화면에서 버튼을 확인해 주세요.')['answer']
        with patch('teamkit.app_bridge.core_read',return_value={'terminals':[]}):
            current=view(self.config,self.state,self.links)
        row=next(r for r in current['inventory'] if r['id']=='W47')
        self.assertEqual(row['feedback'],saved);self.assertEqual(row['condition'],'화면을 열고 저장/조회 결과 확인')
        self.inventory.write_text(self.inventory.read_text().replace('실제 화면 필요','추가 결과 필요'))
        with patch('teamkit.app_bridge.core_read',return_value={'terminals':[]}):
            changed=view(self.config,self.state,self.links)
        row=next(r for r in changed['inventory'] if r['id']=='W47')
        self.assertNotIn('feedback',row);self.assertEqual(row['previous_feedback'],saved)
    def test_a_new_reply_is_not_attached_as_current_to_an_older_display_snapshot(self):
        from teamkit.app_bridge import inventory
        from teamkit.app_feedback import decorate_feedback
        old_rows,_=inventory(self.inventory)
        self.inventory.write_text(self.inventory.read_text().replace('실제 화면 필요','새 화면 필요'))
        saved=self.answer('changes','새 화면 기준의 답변입니다.')['answer']
        decorate_feedback(self.config,self.state,self.links,old_rows,[])
        row=next(r for r in old_rows if r['id']=='W47')
        self.assertNotIn('feedback',row);self.assertEqual(row['previous_feedback'],saved)

    def test_work_progress_snapshot_does_not_inherit_a_newer_reply(self):
        from test_cli import StoreContext
        from teamkit.dashboard import model
        from teamkit.workflow import snapshot
        from teamkit.app_feedback import decorate_feedback
        self.setup_work()
        contract=self.root/'private-plans/W1/r1/contract.json'
        value=json.loads(contract.read_text())
        value['steps']=[{'id':'first','title':'First check','weight':50},{'id':'second','title':'Second check','weight':50}]
        contract.write_text(json.dumps(value))
        self.call('work','submit','W1')
        self.call('work','approve','W1','--user-approved','--approval-reference','Fixture selected design')
        self.call('create','TW','--team','alpha','--role','implementer','--root',str(self.repo),'--goal-file',str(self.root/'goal.txt'),'--work','W1')
        with StoreContext(self.state) as store:
            store.claim('TW','fixture-owner','codex','team','fixture','high',approval_config=self.value)
        old_rows=model(snapshot(self.state),self.value)['rows']
        self.assertEqual(old_rows[0]['percent'],0)
        evidence=self.root/'step.json';evidence.write_text(json.dumps({'verification':['Fixture step assertion']}))
        self.call('work','record','W1','--step','first','--evidence-file',str(evidence))
        current_rows=model(snapshot(self.state),self.value)['rows']
        self.assertEqual(current_rows[0]['percent'],50)
        self.assertEqual(current_rows[0]['phase'],old_rows[0]['phase'])
        saved=self.answer(kind='work',ident='W1')['answer']
        decorate_feedback(self.config,self.state,self.links,[],old_rows)
        self.assertNotIn('feedback',old_rows[0]);self.assertEqual(old_rows[0]['previous_feedback'],saved)
        decorate_feedback(self.config,self.state,self.links,[],current_rows)
        self.assertEqual(current_rows[0]['feedback'],saved)

    def test_card_cli_accepts_opaque_revision_and_preserves_existing_revision_parser(self):
        import contextlib,io
        from teamkit.app_bridge import main
        args=['teamkit_app.py','--config',str(self.config),'--state',str(self.state),'--bindings',str(self.links)]
        output=io.StringIO()
        with patch('sys.argv',args+['card-detail','--kind','inventory','--work','W47']),contextlib.redirect_stdout(output):
            self.assertEqual(main(),0)
        selected=json.loads(output.getvalue());output=io.StringIO()
        with patch('sys.argv',args+['card-answer','--kind','inventory','--work','W47','--card-revision',selected['revision'],'--choice','confirmed','--answer-text','원문을 확인했습니다.']),contextlib.redirect_stdout(output):
            self.assertEqual(main(),0)
        self.assertEqual(json.loads(output.getvalue())['answer']['delivery'],'recorded')
        self.setup_work();self.call('work','submit','W1')
        from teamkit.app_bridge import document_view
        doc=document_view(self.config,self.state,'W1');output=io.StringIO()
        with patch('sys.argv',args+['approve','--work','W1','--revision','1','--digest',doc['digest'],'--confirmed']),contextlib.redirect_stdout(output):
            self.assertEqual(main(),0)
        self.assertEqual(self.call('work','show','W1')['phase'],'approved')
