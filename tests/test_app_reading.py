"""Reading views keep source bytes and current user explanations separate."""
import copy
import unittest

from teamkit.app_reading import reading_view


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.card={'title':'파일 목록 읽기 개선','document_revision':3,'document_digest':'a'*64,
                   'needs_decision':False,'interaction':{'label':'검토 결과 대기','reason':'담당자가 검토 결과를 정리하고 있습니다.'}}
        self.contract={'title':'파일 목록을 쉽게 읽기','allowed_paths':['feature/','tests/'],
                       'requirements':['목록에서 `file_id`를 유지한다.','기존 선택 동작을 유지한다.'],
                       'steps':[{'id':'render','title':'긴 제목과 코드 표시 확인','weight':100}]}
        self.source='  # 설계 r1\r\n\r\n`file_id`와 한글 설명\r\n```text\r\n\tvalue = "한글"  \r\n```\r\n\x1b[31m표시\x1b[0m\x00\u200b\n'

    def test_source_is_exact_in_both_modes_without_contract_append(self):
        original_contract=copy.deepcopy(self.contract)
        result=reading_view(self.source,self.contract,self.card)
        self.assertEqual(result['body'],self.source)
        self.assertEqual(result['source_body'],self.source)
        self.assertEqual(result['body'].encode(),self.source.encode())
        self.assertNotIn('승인 대상 범위',result['body'])
        self.assertEqual(result['contract'],{k:original_contract[k] for k in ('allowed_paths','requirements','steps')})
        self.assertEqual(self.contract,original_contract)
        result['contract']['steps'][0]['title']='다른 제목'
        self.assertEqual(self.contract,original_contract)

    def test_current_profile_replaces_only_authored_brief_fields(self):
        profile={'document':{'revision':3,'digest':'a'*64},'summary':'파일 목록을 더 쉽게 읽습니다.',
                 'purpose':'긴 파일명을 잘리지 않게 봅니다.','changes':['제목을 줄바꿈합니다.'],
                 'checks':['코드 `file_id`를 확인합니다.'],'impact':'목록 화면만 바뀝니다.',
                 'decision_explanation':'검토를 마친 뒤 승인 여부를 결정합니다.',
                 'current_activity':'변경된 목록을 검토하고 있습니다.'}
        result=reading_view(self.source,self.contract,self.card,profile)
        self.assertEqual(result['brief_state'],'current')
        self.assertEqual(result['brief'],{k:v for k,v in profile.items() if k!='document'})
        self.assertEqual(result['source_body'],self.source)
        self.assertEqual(profile['changes'],['제목을 줄바꿈합니다.'])

    def test_old_revision_or_digest_never_supplies_a_summary(self):
        for document in ({'revision':2,'digest':'a'*64},{'revision':3,'digest':'b'*64},
                         {'revision':'3','digest':'a'*64},{'revision':True,'digest':'a'*64}):
            with self.subTest(document=document):
                result=reading_view(self.source,self.contract,self.card,{
                    'document':document,'summary':'지난 작업은 구현 완료입니다.',
                    'current_activity':'이전 세션이 실행 중입니다.','changes':['지난 변경만 표시']})
                self.assertEqual(result['brief_state'],'stale')
                self.assertEqual(result['brief']['summary'],self.contract['title'])
                self.assertEqual(result['brief']['changes'],self.contract['requirements'])
                self.assertNotIn('이전 세션',result['brief']['current_activity'])
                self.assertNotIn('설계 r1',result['brief']['summary'])

    def test_missing_profile_has_contract_fallback_and_current_card_activity(self):
        result=reading_view(self.source,self.contract,self.card)
        self.assertEqual(result['brief_state'],'missing')
        self.assertEqual(result['brief']['summary'],self.contract['title'])
        self.assertEqual(result['brief']['changes'],self.contract['requirements'])
        self.assertEqual(result['brief']['checks'],['긴 제목과 코드 표시 확인'])
        self.assertIn('검토 결과 대기',result['brief']['current_activity'])
        self.assertIn('현재 사용자 결정 요청은 없습니다.',result['brief']['decision_explanation'])

    def test_display_controls_are_sanitized_without_touching_source(self):
        profile={'document':{'revision':3,'digest':'a'*64},'summary':'\x1b[31m목록\x1b[0m\x00 확인',
                 'changes':['`file_id` 유지\x07']}
        result=reading_view(self.source,self.contract,self.card,profile)
        self.assertEqual(result['brief']['summary'],'목록 확인')
        self.assertEqual(result['brief']['changes'],['`file_id` 유지'])
        self.assertEqual(result['source_body'],self.source)

    def test_document_view_card_shape_can_bind_the_profile(self):
        result=reading_view(self.source,self.contract,{'revision':3,'digest':'a'*64},
                            {'document':{'revision':3,'digest':'a'*64},'summary':'현재 설명'})
        self.assertEqual(result['brief_state'],'current')
        self.assertEqual(result['brief']['summary'],'현재 설명')

    def test_conflicting_or_unverified_card_binding_does_not_reuse_a_profile(self):
        for card in ({**self.card,'revision':4,'digest':'b'*64},
                     {'document_revision':3,'document_digest':None},{'revision':True,'digest':'a'*64},{}):
            with self.subTest(card=card):
                result=reading_view(self.source,self.contract,card,{'document':{'revision':3,'digest':'a'*64},'summary':'과거 설명'})
                self.assertEqual(result['brief_state'],'stale')
                self.assertNotEqual(result['brief']['summary'],'과거 설명')

    def test_partial_profile_uses_fallback_for_missing_or_malformed_fields(self):
        result=reading_view(self.source,self.contract,self.card,{'document':{'revision':3,'digest':'a'*64},
            'summary':'현재 설명','changes':['정상',3],'checks':None,'purpose':''})
        self.assertEqual(result['brief']['summary'],'현재 설명')
        self.assertEqual(result['brief']['changes'],self.contract['requirements'])
        self.assertEqual(result['brief']['checks'],['긴 제목과 코드 표시 확인'])
        self.assertTrue(result['brief']['purpose'])

    def test_unknown_input_is_not_replaced_with_fabricated_work_claims(self):
        result=reading_view('# Old heading\n',{}, {})
        self.assertEqual(result['brief_state'],'missing')
        self.assertEqual(result['brief']['changes'],[])
        self.assertEqual(result['brief']['checks'],[])
        self.assertNotIn('Old heading',result['brief']['summary'])
        self.assertIn('현재 작업 상태 정보가 없습니다.',result['brief']['current_activity'])

    def test_non_text_source_is_rejected_instead_of_reencoded(self):
        with self.assertRaises(TypeError):reading_view(b'raw bytes',self.contract,self.card)
