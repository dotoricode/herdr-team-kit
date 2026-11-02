"""The operator board and document API expose useful, version-bound information."""
from unittest.mock import patch
from pathlib import Path
from test_app_feedback import FeedbackFixture
from teamkit import app_bridge


class WorkspaceTests(FeedbackFixture):
    def test_document_source_is_exact_and_contract_is_separate(self):
        self.setup_work();self.call('work','submit','W1')
        raw=(self.root/'private-plans/W1/r1/plan.md').read_text()
        document=app_bridge.document_view(self.config,self.state,'W1')
        self.assertEqual(document['body'],raw)
        self.assertEqual(document['source_body'],raw)
        self.assertIn('allowed_paths',document['contract'])
        self.assertIn('summary',document['brief'])

    def test_operational_issue_does_not_ask_the_user_for_an_absent_choice(self):
        self.inventory.write_text('| W47 | 実行状態 | 진행 상태 확인 필요 | 담당자가 실행 기록 확인 |\n')
        with patch('teamkit.app_bridge.core_read',return_value={'terminals':[]}):
            data=app_bridge.view(self.config,self.state,self.links)
        card=data['inventory'][0]
        self.assertFalse(card['presentation']['decision_required'])
        self.assertEqual(card['presentation']['column'],'waiting')
        self.assertIn('current_activity',card['presentation'])
        self.assertEqual(card['board']['disposition'],'visible')

    def test_board_organization_round_trip_never_completes_work_or_sends(self):
        before=self.state.read_bytes()
        card=app_bridge.card_detail(self.config,self.state,self.links,'inventory','W47')
        with patch('teamkit.app_bridge.subprocess.run') as runtime:
            result=app_bridge.card_board_action(self.config,self.state,self.links,'inventory','W47','done','board-request-01',card['board']['revision'])
            self.assertEqual(result['board']['disposition'],'done')
            runtime.assert_not_called()
        current=app_bridge.card_detail(self.config,self.state,self.links,'inventory','W47')
        self.assertFalse(current['status'].startswith('완료'))
        self.assertEqual(self.state.read_bytes(),before)
        app_bridge.card_board_action(self.config,self.state,self.links,'inventory','W47','restore','board-request-02',current['board']['revision'])
        self.assertEqual(app_bridge.card_detail(self.config,self.state,self.links,'inventory','W47')['board']['disposition'],'visible')

    def test_work_detail_marks_only_current_design_tasks_and_exports_no_private_basis(self):
        from test_cli import StoreContext
        self.setup_work()
        self.call('create','NEW','--team','alpha','--role','implementer','--root',str(self.repo),'--goal-file',str(self.root/'goal.txt'),'--work','W1')
        with StoreContext(self.state) as store:
            with store.transaction():
                old=dict(store.get('NEW'),id='OLD',design_revision=0,design_digest='0'*64);store._save(old,'fixture')
        with patch('teamkit.app_bridge.core_read',return_value={'terminals':[]}):
            result=app_bridge.work_detail(self.config,self.state,self.links,'W1')
            card=app_bridge.card_detail(self.config,self.state,self.links,'work','W1')
        self.assertEqual({t['id']:t['current_design'] for t in result['tasks']},{'NEW':True,'OLD':False})
        self.assertEqual([s['task'] for s in card['presentation']['sessions']],['NEW'])
        self.assertNotIn('_view_signature',card);self.assertNotIn('_revision_basis',card)

    def test_board_action_consumed_cli_uses_same_current_card_and_revision(self):
        import contextlib,io,json,sys
        card=app_bridge.card_detail(self.config,self.state,self.links,'inventory','W47')
        argv=['app','--config',str(self.config),'--state',str(self.state),'--bindings',str(self.links),'card-board-action',
              '--kind','inventory','--work','W47','--board-action','archive','--board-revision',card['board']['revision'],'--request-id','board-cli-request']
        output=io.StringIO()
        with patch.object(sys,'argv',argv),contextlib.redirect_stdout(output):self.assertEqual(app_bridge.main(),0)
        self.assertEqual(json.loads(output.getvalue())['board']['disposition'],'archived')
