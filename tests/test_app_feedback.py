"""Card replies record feedback; they never approve or deliver product work."""
import copy
import json
import os
from pathlib import Path
from unittest.mock import patch

from test_cli import Fixture
import test_workflow
from teamkit.app_feedback import card_detail, card_answer, feedback_summaries, question_set, validate_send


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
    def test_task_specific_choices_are_bound_to_card_revision(self):
        old=self.detail()['revision']
        decision={'prompt':'어떤 순서로 확인할까요?', 'options':[
            {'id':'first','label':'화면부터 확인','description':'화면의 표시를 먼저 확인해 주세요.','intent':'changes'},
            {'id':'later','label':'다음에 확인','description':'지금은 결정을 보류합니다.','intent':'defer'}]}
        self.linked['card_decisions']={'inventory:W47':decision};self.links.write_text(json.dumps(self.linked))
        value=self.detail();self.assertTrue(value['needs_decision']);self.assertEqual(value['decision'],decision)
        self.assertNotEqual(value['revision'],old)
        with self.assertRaises(ValueError):self.answer(revision=old)
        self.assertIsNone(self.detail('W48')['decision']);self.assertFalse(self.detail('W48')['needs_decision'])

    def test_in_progress_card_has_no_generic_decision_request(self):
        self.setup_work()
        from teamkit.store import Store
        store=Store(self.state)
        try:
            with store.transaction():
                row=json.loads(store.db.execute('SELECT body FROM work_items').fetchone()[0])
                row['phase']='in_progress'
                store.db.execute('UPDATE work_items SET body=?',(json.dumps(row),))
        finally:store.close()
        value=self.detail('W1','work')
        self.assertFalse(value['needs_decision']);self.assertIsNone(value['decision'])

    def test_work_choices_expire_when_the_bound_design_changes(self):
        self.setup_work();self.call('work','submit','W1')
        card=self.detail('W1','work')
        decision={'prompt':'어떻게 진행할까요?', 'document':{'revision':card['document_revision'],'digest':card['document_digest'],'phase':card['status']},'options':[
            {'id':'approve','label':'설계 승인','description':'이 설계를 승인합니다.','intent':'approve'},
            {'id':'later','label':'다음에 결정','description':'다음 지시까지 보류합니다.','intent':'defer'}]}
        self.linked['card_decisions']={'work:W1':decision};self.links.write_text(json.dumps(self.linked))
        self.assertIsNotNone(self.detail('W1','work')['decision'])
        decision['document']['digest']='0'*64;self.links.write_text(json.dumps(self.linked))
        self.assertIsNone(self.detail('W1','work')['decision'])
        self.assertIn('설계',self.detail('W1','work')['decision_reason'])

    def test_invalid_or_unsafe_choice_configuration_is_not_offered(self):
        for intent in ('resume','shell','approve'):
            self.linked['card_decisions']={'inventory:W47':{'prompt':'선택해 주세요.','options':[
                {'id':'one','label':'첫째','description':'첫째 선택','intent':intent},
                {'id':'two','label':'둘째','description':'둘째 선택','intent':'defer'}]}}
            self.links.write_text(json.dumps(self.linked))
            with self.subTest(intent=intent),self.assertRaises(ValueError):self.detail()

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

    def test_cli_saves_free_text_that_starts_with_option_prefix(self):
        import contextlib,io
        from teamkit.app_bridge import main
        selected=self.detail();output=io.StringIO()
        args=['teamkit_app.py','--config',str(self.config),'--state',str(self.state),'--bindings',str(self.links),
              'card-answer','--kind','inventory','--work','W47','--card-revision',selected['revision'],
              '--choice','changes','--answer-text=--no-daemon']
        with patch('sys.argv',args),contextlib.redirect_stdout(output):self.assertEqual(main(),0)
        self.assertEqual(self.detail()['answer']['text'],'--no-daemon')

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


AUTHOR={'task':'L1','owner':'leader-owner','attempt':1,'terminal':'t1'}
OPTIONS=[{'option_id':'proceed','label':'이대로 진행','description':'현재 범위로 진행합니다.'},{'option_id':'narrow','label':'범위 줄이기'}]


class QuestionTests(FeedbackFixture):
    def ask(self,ident='W47',kind='inventory',expected=0,options=None,prompt='이 화면의 저장 결과가 맞나요?',**extra):
        return question_set(self.config,self.state,self.links,kind,ident,AUTHOR,{'prompt':prompt,'options':OPTIONS if options is None else options,**extra},expected)
    def send(self,**fields):
        card=self.detail(fields.pop('ident','W47'),fields.pop('kind','inventory'))
        base={'client_request_id':'req-0001-aaaa','kind':card['kind'],'work_id':card['id'],'card_revision':card['revision'],
              'document_revision':card['document_revision'],'document_digest':card['document_digest']}
        return validate_send(self.config,self.state,self.links,{**base,**fields})
    def qfields(self,option='proceed',**extra):
        q=self.detail()['question']
        return {'question_id':q['question_id'],'question_revision':q['question_revision'],'option_id':option,**extra}

    def test_no_question_by_default_and_nothing_is_invented(self):
        value=self.detail()
        self.assertIsNone(value['question']);self.assertEqual(value['question_state']['state'],'missing')
        self.assertIn('준비하지 않았습니다',value['question_state']['reason'])
        self.assertFalse((self.feedback_root/'questions.jsonl').exists())
        with self.assertRaisesRegex(ValueError,'준비하지 않았습니다'):self.send(question_id='q-whatever',question_revision=1,option_id='proceed')

    def test_typed_question_is_bound_to_card_and_document_and_keeps_legacy_prompt(self):
        saved=self.ask()['question'];value=self.detail()
        self.assertEqual(value['question'],saved);self.assertEqual(value['question_state']['state'],'ready')
        self.assertEqual(saved['question_revision'],1);self.assertTrue(saved['other_allowed'])
        self.assertEqual((saved['card_revision'],saved['document_digest']),(value['revision'],value['document_digest']))
        self.assertEqual(value['prompt'],'화면에서 확인한 결과를 알려 주세요.')
        self.assertEqual((self.feedback_root/'questions.jsonl').stat().st_mode&0o777,0o600)

    def test_question_shape_is_validated_without_partial_write(self):
        bad=[[],[{'option_id':'o'+str(i),'label':str(i)} for i in range(5)],[{'option_id':'a','label':'x'},{'option_id':'a','label':'y'}],
             [{'option_id':'other','label':'x'}],[{'option_id':'Bad Id','label':'x'}],[{'option_id':'a','label':' '}],
             [{'option_id':'a','label':'x','extra':1}]]
        for options in bad:
            with self.subTest(options=options),self.assertRaises(ValueError):self.ask(options=options)
        for prompt in ('',' ','x'*1001):
            with self.subTest(prompt=len(prompt)),self.assertRaises(ValueError):self.ask(prompt=prompt)
        with self.assertRaises(ValueError):self.ask(unknown=True)
        self.assertFalse((self.feedback_root/'questions.jsonl').exists())

    def test_question_versions_need_expected_revision_and_keep_the_same_id(self):
        first=self.ask()['question']
        with self.assertRaises(ValueError):self.ask(expected=0,prompt='다른 질문')
        replay=question_set(self.config,self.state,self.links,'inventory','W47',AUTHOR,{'prompt':first['prompt'],'options':OPTIONS},1)
        self.assertEqual(replay['question'],first);self.assertEqual(len((self.feedback_root/'questions.jsonl').read_text().splitlines()),1)
        second=self.ask(expected=1,prompt='범위를 바꿀까요?')['question']
        self.assertEqual((second['question_id'],second['question_revision']),(first['question_id'],2))
        with self.assertRaises(ValueError):self.ask(expected=2,question_id='q-another-card-id')

    def test_card_change_makes_question_stale_and_blocks_answer(self):
        self.ask();fields=self.qfields();self.inventory.write_text(self.inventory.read_text().replace('실제 화면 필요','화면 재확인 필요'))
        value=self.detail()
        self.assertIsNone(value['question']);self.assertEqual(value['question_state']['state'],'stale');self.assertIsNotNone(value['previous_question'])
        with self.assertRaises(ValueError):self.send(**fields)

    def test_answer_selects_one_existing_option_or_non_empty_other(self):
        self.ask()
        payload,_,question=self.send(**self.qfields('narrow'))
        self.assertEqual(payload['option_id'],'narrow');self.assertEqual(question['question_revision'],1)
        for fields in (self.qfields('missing'),self.qfields('other'),self.qfields('other',other_text='  '),self.qfields('proceed',other_text='추가'),
                       self.qfields('proceed',choice='changes')):
            with self.subTest(fields=fields),self.assertRaises(ValueError):self.send(**fields)
        other,_,_=self.send(**self.qfields('other',other_text='  직접 정한 방향  '))
        self.assertEqual(other['other_text'],'직접 정한 방향')

    def test_new_question_revision_is_never_inherited_by_number_or_option(self):
        self.ask();old=self.qfields('proceed');self.ask(expected=1,prompt='다시 묻습니다',options=[{'option_id':'proceed','label':'다른 뜻'}])
        with self.assertRaisesRegex(ValueError,'질문이 바뀌었습니다'):self.send(**old)
        self.assertEqual(self.send(**self.qfields('proceed'))[0]['question_revision'],2)

    def test_general_feedback_is_separate_from_typed_answers(self):
        payload,_,question=self.send(choice='changes',comment='문구를 고쳐 주세요.')
        self.assertIsNone(question);self.assertIsNone(payload['question_id']);self.assertEqual(payload['choice'],'changes')
        for fields in ({'choice':'changes'},{'choice':'defer','comment':' '},{'choice':'confirmed','comment':'x'},{'comment':'x'},
                       {'choice':'changes','comment':'x'*4001},{'choice':'changes','comment':'x','option_id':'proceed'},
                       {'choice':'changes','comment':'x','unknown':1}):
            with self.subTest(fields=fields),self.assertRaises(ValueError):self.send(**fields)

    def test_stale_card_or_disabled_actions_never_validate(self):
        card=self.detail()
        base={'client_request_id':'req-0001-aaaa','kind':'inventory','work_id':'W47','card_revision':card['revision'],
              'document_revision':0,'document_digest':card['document_digest'],'choice':'changes','comment':'x'}
        self.inventory.write_text(self.inventory.read_text().replace('실제 화면 필요','화면 재확인 필요'))
        with self.assertRaisesRegex(ValueError,'카드나 문서가 바뀌었습니다'):validate_send(self.config,self.state,self.links,base)
        self.linked['actions_enabled']=False;self.links.write_text(json.dumps(self.linked))
        with self.assertRaises(ValueError):validate_send(self.config,self.state,self.links,base)

    def test_work_question_needs_readable_document_and_follows_document_digest(self):
        self.setup_work();self.call('work','submit','W1')
        first=self.ask('W1','work',prompt='설계 범위가 맞나요?')['question']
        self.assertEqual(first['document_revision'],1);self.assertEqual(self.detail('W1','work')['question_state']['state'],'ready')
        (self.root/'private-plans/W1/r1/plan.md').write_text('changed design')
        changed=self.detail('W1','work')
        self.assertEqual(changed['question_state']['state'],'document_unavailable');self.assertIsNone(changed['question'])
        (self.root/'private-plans/W1/r1/plan.md').unlink()
        value=self.detail('W1','work');self.assertEqual(value['question_state']['state'],'document_unavailable')
        with self.assertRaisesRegex(ValueError,'설계 문서'):self.ask('W1','work',expected=1,prompt='문서가 없을 때')

    def test_unreadable_question_journal_is_reported_and_never_repaired(self):
        self.ask();path=self.feedback_root/'questions.jsonl';path.write_bytes(path.read_bytes()+b'{"partial"')
        before=path.read_bytes();value=self.detail()
        self.assertEqual(value['question_state']['state'],'invalid');self.assertIsNone(value['question'])
        with self.assertRaises(ValueError):self.ask(expected=1,prompt='새 질문')
        self.assertEqual(before,path.read_bytes())

    def test_complete_but_unterminated_last_line_is_still_a_damaged_journal(self):
        first=self.ask()['question'];path=self.feedback_root/'questions.jsonl'
        extra=dict(first,question_revision=2,prompt='끝 줄바꿈이 없는 완전한 JSON');path.write_bytes(path.read_bytes()+json.dumps(extra,ensure_ascii=False).encode())
        before=path.read_bytes();self.assertEqual(self.detail()['question_state']['state'],'invalid')
        with self.assertRaises(ValueError):self.ask(expected=1,prompt='새 질문')
        self.assertEqual(before,path.read_bytes())

    def test_recorded_answers_remain_compatible_and_not_delivered(self):
        self.ask();result=self.answer('changes','기록만 합니다.');self.assertEqual(result['answer']['delivery'],'recorded')
        self.assertEqual(self.detail()['deliveries'],[])
