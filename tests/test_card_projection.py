"""Consumer regression: journal outcomes must reach board and detail together."""
from unittest.mock import patch
from test_app_feedback import FeedbackFixture
from teamkit.app_feedback import card_detail
from teamkit.app_bridge import view
from teamkit import app_delivery as delivery
from test_app_delivery import request


class ProjectionTests(FeedbackFixture):
    def setup_delivery(self, outcome='sent', choice='changes', revision=None):
        self.setup_work();self.call('work','submit','W1')
        card=self.detail('W1','work')
        req=request()
        req['payload'].update(card_revision=revision or card['revision'], document_revision=card['document_revision'],
                              document_digest=card['document_digest'],choice=choice)
        req['payload_hash']=delivery.digest(req['payload'])
        delivery.submit(self.state,req);delivery.claim(self.state,req['client_request_id'],'t9')
        if outcome!='sending':delivery.record(self.state,req['client_request_id'],outcome,{'reason':'fixture'})
        return req

    def board(self):
        with patch('teamkit.app_bridge.core_read',return_value={'terminals':[]}):
            return view(self.config,self.state,self.links)

    def test_sent_answer_moves_both_consumers_to_waiting_without_completing_work(self):
        self.setup_delivery()
        card=self.detail('W1','work');row=self.board()['works'][0]
        self.assertFalse(card['needs_decision'])
        self.assertEqual(card['interaction'],row['interaction'])
        self.assertEqual(row['interaction']['column'],'waiting')
        self.assertEqual(row['interaction']['state'],'sent')
        self.assertEqual(row['phase'],'awaiting_approval')
        self.assertIn('처리 결과 미기록',row['interaction']['reason'])

    def test_unknown_is_inspectable_without_repeating_the_decision(self):
        self.setup_delivery('unknown')
        card=self.detail('W1','work')
        self.assertFalse(card['needs_decision'])
        self.assertEqual(card['interaction']['column'],'review')
        self.assertEqual(card['interaction']['action'],'전달 기록 확인')

    def test_old_revision_cannot_hide_a_new_decision(self):
        self.setup_delivery(revision='f'*64)
        self.assertTrue(self.detail('W1','work')['needs_decision'])
        self.assertEqual(self.board()['works'][0]['interaction']['state'],'decision')

    def test_recorded_ack_moves_inventory_to_waiting_not_complete(self):
        self.answer()
        card=self.detail();row=self.board()['inventory'][0]
        self.assertFalse(card['needs_decision'])
        self.assertEqual(card['interaction'],row['interaction'])
        self.assertEqual(row['interaction']['column'],'waiting')
        self.assertFalse(row['status'].startswith('완료'))

    def test_inventory_error_and_mixed_status_use_same_classification(self):
        for status in ('실패','결과 불명','진행 중 · 확인 필요'):
            with self.subTest(status=status):
                self.inventory.write_text(f'| W47 | 화면 확인 | {status} | 확인 |\n')
                card=self.detail();row=self.board()['inventory'][0]
                self.assertTrue(card['needs_decision'])
                self.assertEqual(row['interaction']['column'],'review')

    def test_late_verified_response_resolves_display_but_preserves_timeout(self):
        req=self.setup_delivery('unknown');rid=req['client_request_id']
        delivery.record_receipt(self.state,rid,'responded','기존 반영 내용 대조 완료. 추가 검증 필요.',rid+' response evidence',{'host':'fixture'})
        card=self.detail('W1','work');row=self.board()['works'][0]
        self.assertEqual(card['deliveries'][0]['state'],'unknown')
        self.assertEqual(card['interaction']['state'],'responded')
        self.assertEqual(row['interaction']['column'],'waiting')
        self.assertEqual(row['phase'],'awaiting_approval')

    def test_native_failure_is_partial_and_disables_actions(self):
        with patch('teamkit.app_bridge.core_read',side_effect=OSError('offline')):
            data=view(self.config,self.state,self.links)
        self.assertEqual(data['synchronization']['state'],'partial')
        self.assertFalse(data['actions_enabled'])

    def test_unverified_team_terminal_is_not_counted_as_working(self):
        with patch('teamkit.app_bridge.core_read',return_value={'terminals':[{'address':'t1','state':'live','status':'working'}]}):
            data=view(self.config,self.state,self.links)
        self.assertFalse(data['teams'][0]['identity_verified'])
        self.assertEqual(data['teams'][0]['app_state'],'unknown')

    def test_new_question_revision_is_not_answered_by_old_delivery(self):
        from teamkit.app_feedback import question_set
        from test_app_feedback import AUTHOR,OPTIONS
        self.setup_work();self.call('work','submit','W1')
        first=question_set(self.config,self.state,self.links,'work','W1',AUTHOR,{'prompt':'확인할까요?','options':OPTIONS},0)['question']
        card=self.detail('W1','work');req=request()
        req['payload'].update(card_revision=card['revision'],document_revision=card['document_revision'],document_digest=card['document_digest'],
                              question_id=first['question_id'],question_revision=1,option_id='proceed',choice=None)
        req['payload_hash']=delivery.digest(req['payload']);delivery.submit(self.state,req);delivery.claim(self.state,req['client_request_id'],'t9');delivery.record(self.state,req['client_request_id'],'unknown',{})
        self.assertFalse(self.detail('W1','work')['needs_decision'])
        question_set(self.config,self.state,self.links,'work','W1',AUTHOR,{'prompt':'새 범위도 확인할까요?','options':OPTIONS},1)
        self.assertTrue(self.detail('W1','work')['needs_decision'])
        self.assertEqual(self.board()['works'][0]['interaction']['state'],'decision')

    def test_receipt_rejects_different_destination_and_damaged_evidence(self):
        import json
        req=self.setup_delivery('unknown');rid=req['client_request_id']
        delivery.record_receipt(self.state,rid,'responded','처리 확인',rid,{'host':'fixture'})
        folder=delivery.request_dir(self.state,rid)
        path=folder/'receipt.json';value=json.loads(path.read_text());evidence=value['evidence'];value['evidence']='changed';path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'근거'):delivery.summary(folder)
        value['evidence']=evidence;value['destination']['owner']='another';path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'소유권'):delivery.summary(folder)

    def test_concurrent_inventory_change_is_partial_and_blocks_actions(self):
        from teamkit.app_bridge import inventory
        calls=[]
        def changing(path):
            result=inventory(path)
            if not calls:
                self.inventory.write_text(self.inventory.read_text().replace('실제 화면 필요','새 화면 필요'))
            calls.append(True)
            return result
        with patch('teamkit.app_bridge.inventory',side_effect=changing):data=self.board()
        self.assertEqual(data['synchronization']['state'],'partial')
        self.assertFalse(data['actions_enabled'])
        self.assertTrue(all(r['interaction']['state']=='unknown' for r in data['inventory']))

    def test_receipt_cli_boundary_rejects_foreign_owner_or_unverified_author(self):
        from teamkit.app_bridge import card_receipt
        req=self.setup_delivery('unknown');rid=req['client_request_id']
        with patch('teamkit.app_bridge.verify_author',side_effect=ValueError('not managed')):
            with self.assertRaises(ValueError):card_receipt(self.config,self.state,self.links,rid,'L1','leader-owner',1,'responded','done')
        with patch('teamkit.app_bridge.verify_author',return_value={'task':'L1','owner':'foreign','attempt':1,'terminal':'t1'}):
            with self.assertRaisesRegex(ValueError,'수신|받은'):card_receipt(self.config,self.state,self.links,rid,'L1','foreign',1,'responded','done')
        self.assertIsNone(delivery.summary(delivery.request_dir(self.state,rid))['receipt'])
        actor={k:req['destination'][k] for k in ('task','owner','attempt','terminal')}
        with patch('teamkit.app_bridge.verify_author',return_value=actor):
            card_receipt(self.config,self.state,self.links,rid,'L1','leader-owner',1,'responded','응답 확인')
        self.assertEqual(self.detail('W1','work')['interaction']['state'],'responded')

    def test_interrupted_receipt_can_be_recorded_again_without_resending(self):
        req=self.setup_delivery('unknown');rid=req['client_request_id'];write=delivery._write_new
        def interrupted(path,body):
            if path.name=='receipt.json' or path.name.startswith('.receipt-'):raise OSError('interrupted write')
            return write(path,body)
        with patch('teamkit.app_delivery._write_new',side_effect=interrupted):
            with self.assertRaises(OSError):delivery.record_receipt(self.state,rid,'responded','처리 확인',rid,{'host':'fixture'})
        self.assertIsNone(delivery.summary(delivery.request_dir(self.state,rid))['receipt'])
        delivery.record_receipt(self.state,rid,'responded','처리 확인',rid,{'host':'fixture'})
        self.assertEqual(delivery.summary(delivery.request_dir(self.state,rid))['state'],'unknown')
        self.assertEqual(self.detail('W1','work')['interaction']['state'],'responded')

    def test_reviewing_implementation_is_waiting_not_running_or_a_user_decision(self):
        from test_cli import StoreContext
        self.setup_work()
        self.call('create','IMPL','--team','alpha','--role','implementer','--root',str(self.repo),'--goal-file',str(self.root/'goal.txt'),'--work','W1')
        with StoreContext(self.state) as store:
            with store.transaction():
                import json
                work=json.loads(store.db.execute('SELECT body FROM work_items').fetchone()[0]);work['phase']='in_progress'
                store.db.execute('UPDATE work_items SET body=?',(json.dumps(work),))
                task=store.get('IMPL');task['state']='awaiting_review';store._save(task,'fixture')
        card=self.detail('W1','work');row=self.board()['works'][0]
        self.assertFalse(card['needs_decision'])
        self.assertEqual(card['interaction']['state'],'review_wait')
        self.assertEqual(row['interaction']['column'],'waiting')
        self.assertEqual(row['phase'],'in_progress')
        for task_state,display_state,column in (('queued','assignment_wait','waiting'),('running','progress','progress'),('done','execution_unknown','review')):
            with self.subTest(task_state=task_state),StoreContext(self.state) as store:
                with store.transaction():
                    task=store.get('IMPL');task['state']=task_state;store._save(task,'fixture')
                current=self.detail('W1','work')
                self.assertEqual(current['interaction']['state'],display_state)
                self.assertEqual(current['interaction']['column'],column)
                self.assertFalse(current['needs_decision'])
        with StoreContext(self.state) as store:
            with store.transaction():
                current=store.get('IMPL');current['state']='queued';store._save(current,'fixture')
                old=dict(current,id='OLD',state='awaiting_review',design_revision=0,design_digest='0'*64);store._save(old,'fixture')
        self.assertEqual(self.detail('W1','work')['interaction']['state'],'assignment_wait')

    def test_leader_route_change_does_not_reopen_the_same_answered_decision(self):
        import json
        self.setup_delivery()
        self.linked['teams']['alpha']['terminal']='t8';self.links.write_text(json.dumps(self.linked))
        card=self.detail('W1','work')
        self.assertFalse(card['needs_decision'])
        self.assertEqual(card['interaction']['state'],'sent')
        self.linked['card_prompts']['work:W1']='새로 바뀐 결정 내용';self.links.write_text(json.dumps(self.linked))
        self.assertTrue(self.detail('W1','work')['needs_decision'])
