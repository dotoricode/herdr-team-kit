"""Durable request/event journal: one claim, no repair, no automatic resend."""
import json
import os
from pathlib import Path
import threading
import time
from unittest.mock import patch

from test_app_feedback import FeedbackFixture
from teamkit import app_delivery as delivery
from teamkit.app_delivery import canonical, digest

PAYLOAD={'client_request_id':'req-0001-aaaa','kind':'work','work_id':'W1','card_revision':'a'*64,'document_revision':1,
         'document_digest':'b'*64,'question_id':None,'question_revision':None,'option_id':None,'other_text':'','choice':'changes','comment':'수정해 주세요.'}
DEST={'team':'alpha','task':'L1','owner':'leader-owner','attempt':1,'terminal':'t1','name':'tk-leader','native_pid':77}


def request(rid='req-0001-aaaa',comment='수정해 주세요.',dest=DEST):
    payload={**PAYLOAD,'client_request_id':rid,'comment':comment}
    content={k:v for k,v in payload.items() if k!='client_request_id'}
    return {'schema_version':1,'client_request_id':rid,'payload':payload,'payload_hash':digest(payload),
            'content_hash':digest(content),'destination':dest,'created_at':time.time()}


class DeliveryTests(FeedbackFixture):
    def submit(self,**kw):return delivery.submit(self.state,request(**kw))
    def folder(self,rid='req-0001-aaaa'):return self.feedback_root/'delivery'/rid

    def test_request_and_events_are_private_and_synced_before_returning(self):
        with patch('teamkit.app_delivery.os.fsync',wraps=os.fsync) as sync:
            seen,created=self.submit()
        self.assertTrue(created);self.assertEqual((seen['state'],seen['label']),('pending','전달 준비 중'))
        self.assertGreaterEqual(sync.call_count,4)  # request, events, request folder, delivery folder
        self.assertEqual((self.feedback_root/'delivery').stat().st_mode&0o777,0o700);self.assertEqual(self.folder().stat().st_mode&0o777,0o700)
        for name in ('request.json','events.jsonl'):self.assertEqual((self.folder()/name).stat().st_mode&0o777,0o600)

    def test_same_id_returns_original_without_new_event_and_other_payload_is_rejected(self):
        self.submit();journal=(self.folder()/'events.jsonl').read_bytes()
        seen,created=self.submit();self.assertFalse(created);self.assertEqual(seen['state'],'pending')
        with self.assertRaisesRegex(ValueError,'다른 내용'):self.submit(comment='다른 의견')
        self.assertEqual(journal,(self.folder()/'events.jsonl').read_bytes())

    def test_unresolved_duplicate_content_is_blocked_until_the_first_is_resolved(self):
        self.submit();delivery.claim(self.state,'req-0001-aaaa','t9')
        with self.assertRaisesRegex(ValueError,'확인되지 않았습니다'):self.submit(rid='req-0002-bbbb')
        delivery.record(self.state,'req-0001-aaaa','unknown',{'reason':'timeout'})
        with self.assertRaises(ValueError):self.submit(rid='req-0002-bbbb')
        self.assertFalse(self.folder('req-0002-bbbb').exists())
        self.submit(rid='req-0003-cccc',comment='다른 내용')  # different content is independent
        delivery.record(self.state,'req-0003-cccc','not_sent',{'reason':'거절'})

    def test_resolved_not_sent_allows_the_same_content_under_a_new_id(self):
        self.submit();delivery.record(self.state,'req-0001-aaaa','not_sent',{'reason':'대기 아님'})
        seen,created=self.submit(rid='req-0002-bbbb');self.assertTrue(created);self.assertEqual(seen['state'],'pending')

    def test_only_one_controller_can_claim_even_when_concurrent(self):
        self.submit();wins,errors=[],[]
        def take(n):
            try:wins.append(delivery.claim(self.state,'req-0001-aaaa','t'+str(n)))
            except ValueError as error:errors.append(str(error))
        threads=[threading.Thread(target=take,args=(n,)) for n in range(8)]
        [t.start() for t in threads];[t.join() for t in threads]
        self.assertEqual(len(wins),1);self.assertEqual(len(errors),7)
        events=delivery.read_events(self.folder());self.assertEqual([e['state'] for e in events],['pending','sending'])

    def test_transitions_are_one_way_and_final_states_cannot_change(self):
        self.submit()
        with self.assertRaises(ValueError):delivery.record(self.state,'req-0001-aaaa','sent',{})
        delivery.claim(self.state,'req-0001-aaaa','t9');delivery.record(self.state,'req-0001-aaaa','sent',{'reason':'확인'})
        for state in ('sending','sent','not_sent','unknown'):
            with self.subTest(state=state),self.assertRaises(ValueError):delivery.record(self.state,'req-0001-aaaa',state,{})
        with self.assertRaises(ValueError):delivery.claim(self.state,'req-0001-aaaa','t9')
        self.assertEqual(delivery.summary(self.folder())['label'],'전송 확인됨')

    def test_orphaned_requests_are_shown_unknown_but_never_rewritten_or_repeated(self):
        self.submit();delivery.claim(self.state,'req-0001-aaaa','t9');before=(self.folder()/'events.jsonl').read_bytes()
        later=time.time()+delivery.SENDING_ORPHAN_AFTER+5
        seen=delivery.summary(self.folder(),now=later)
        self.assertEqual((seen['state'],seen['effective_state'],seen['orphaned']),('sending','unknown',True))
        self.assertIn('자동 재전송 없이',seen['reason']);self.assertEqual(before,(self.folder()/'events.jsonl').read_bytes())
        with self.assertRaises(ValueError):delivery.claim(self.state,'req-0001-aaaa','t10')

    def test_damaged_records_are_inspected_never_repaired_or_resent(self):
        self.submit();path=self.folder()/'events.jsonl';good=path.read_bytes()
        for broken in (good+b'{"seq":2',good+canonical({'seq':2,'state':'sending','at':1,'detail':{}}).encode(),good+canonical({'seq':3,'state':'sending','at':1,'detail':{}}).encode()+b'\n',
                       good+canonical({'seq':2,'state':'sent','at':1,'detail':{}}).encode()+b'\n',b''):
            with self.subTest(tail=broken[len(good):][:12]):
                path.write_bytes(broken)
                with self.assertRaises(ValueError):delivery.summary(self.folder())
                with self.assertRaises(ValueError):delivery.claim(self.state,'req-0001-aaaa','t9')
                self.assertEqual(broken,path.read_bytes())
        path.write_bytes(good);req=json.loads((self.folder()/'request.json').read_text());req['payload']['comment']='바꿈'
        (self.folder()/'request.json').write_text(json.dumps(req))
        with self.assertRaisesRegex(ValueError,'변조'):delivery.summary(self.folder())

    def test_card_listing_flags_unreadable_records_without_deleting_them(self):
        self.submit();self.submit(rid='req-0002-bbbb',comment='다른 카드',dest=None)
        (self.folder('req-0002-bbbb')/'events.jsonl').write_bytes(b'{"seq":1')
        found=delivery.card_requests(self.state,'work:W1');states={item['request_id']:item['effective_state'] for item in found}
        self.assertEqual(states,{'req-0001-aaaa':'pending','req-0002-bbbb':'unknown'})
        self.assertEqual(delivery.card_requests(self.state,'work:OTHER')[0]['label'],'기록 확인 필요')
        self.assertTrue(self.folder('req-0002-bbbb').exists())

    def test_unsafe_folders_and_ids_are_refused(self):
        with self.assertRaises(ValueError):delivery.request_dir(self.state,'../escape')
        self.submit();base=self.feedback_root/'delivery';os.chmod(base,0o755)
        with self.assertRaises(ValueError):self.submit(rid='req-0002-bbbb',comment='x')
        os.chmod(base,0o700);target=self.root/'elsewhere';target.mkdir();base.rename(self.root/'moved');base.symlink_to(target)
        with self.assertRaises(ValueError):self.submit(rid='req-0003-cccc',comment='y')
        self.assertEqual(list(target.iterdir()),[])
