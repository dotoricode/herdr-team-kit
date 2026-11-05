import datetime
import json
from pathlib import Path
import tempfile
import unittest

from teamkit import hq
from teamkit.knowledge import add_entry


class BountyTests(unittest.TestCase):
    def test_bounty_lifecycle_open_claim_release_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = hq.add_bounty(tmp, '간헐 타임아웃 원인', '재현 조건 정리', 'infra', 300, '사용자')
            self.assertEqual(item['status'], 'open')
            self.assertEqual(hq.claim_bounty(tmp, item['id'], '민과장')['claimed_by'], '민과장')
            with self.assertRaises(ValueError):
                hq.claim_bounty(tmp, item['id'], '배사원')
            self.assertNotIn('claimed_by', hq.release_bounty(tmp, item['id']))
            hq.claim_bounty(tmp, item['id'], '배사원')
            done = hq.resolve_bounty(tmp, item['id'], '배사원', 'DNS 캐시 만료가 원인')
            self.assertEqual((done['status'], done['resolved_by']), ('resolved', '배사원'))
            with self.assertRaises(ValueError):
                hq.resolve_bounty(tmp, item['id'], '배사원', '다시')
            stored = json.loads((Path(tmp) / 'bounties.json').read_text(encoding='utf-8'))
            self.assertEqual(stored[0]['resolution'], 'DNS 캐시 만료가 원인')

    def test_rejects_unknown_id_bad_team_and_points(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                hq.claim_bounty(tmp, 'missing', '누구')
            with self.assertRaises(ValueError):
                hq.add_bounty(tmp, '제목', team='../etc')
            with self.assertRaises(ValueError):
                hq.add_bounty(tmp, '제목', points=-1)
            with self.assertRaises(ValueError):
                hq.add_bounty(tmp, '   ')


class ProposalTests(unittest.TestCase):
    def test_proposal_starts_pending_and_only_known_states_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = hq.add_proposal(tmp, '빌드 캐시 공유', '공통 캐시로 빌드 시간 단축', '빌드 시간', '캐시 적중이 높다', 'infra', '안대리')
            self.assertEqual(item['status'], '대기')
            self.assertEqual(hq.decide_proposal(tmp, item['id'], '채택', '이번 주 진행')['status'], '채택')
            with self.assertRaises(ValueError):
                hq.decide_proposal(tmp, item['id'], '승인')

    def test_agent_cli_cannot_decide_proposals(self):
        with self.assertRaises(SystemExit):
            hq.main(['proposal-decide', 'x', '채택'])


class CoffeeTests(unittest.TestCase):
    def test_coffee_keeps_recent_notes_and_view_hides_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'state.sqlite'
            root = hq.board_root(state)
            hq.post_coffee(root, '오래된 근황', by='a', now=1000)
            for index in range(hq.COFFEE_KEEP + 3):
                hq.post_coffee(root, f'근황 {index}', 'qa', '나사원', now=10**6 + index)
            stored = json.loads((root / 'coffee.json').read_text(encoding='utf-8'))
            self.assertEqual(len(stored), hq.COFFEE_KEEP)
            shown = hq.view(state, now=10**6 + 100)['coffee']
            self.assertEqual(shown[0]['text'], f'근황 {hq.COFFEE_KEEP + 2}')
            self.assertNotIn('오래된 근황', [item['text'] for item in shown])

    def test_coffee_text_is_one_short_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(hq.post_coffee(tmp, '줄\n바꿈', by='a')['text'], '줄 바꿈')
            with self.assertRaises(ValueError):
                hq.post_coffee(tmp, 'x' * 201, by='a')


class GoalTests(unittest.TestCase):
    BRIEF = """## 목표 브리프: 리뷰 처리량

**목표 문장**: 리뷰 요청이 하루 안에 판정될 때 우리는 이긴다.

### 성공 지표 (결과)
| 지표 | 정의 (계산식) | 기준선 | 목표 | 기한 | 데이터 출처 |
|---|---|---|---|---|---|
| 하루 내 판정률 | 24시간 내 판정 ÷ 요청 | 미확인 | 80% | 분기 말 | PR 기록 |

### 가드레일 (나빠지면 안 되는 것)
| 지표 | 허용 한계 | 넘으면 할 일 |
|---|---|---|
| 머지 후 결함 | 늘면 안 됨 | 사람 검토 |

### 하지 말 것
- 테스트 없이 승인
"""

    def test_reads_goal_brief_from_skill_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(hq.read_goal(tmp))
            (Path(tmp) / 'GOAL-BRIEF.md').write_text(self.BRIEF, encoding='utf-8')
            goal = hq.read_goal(tmp)
            self.assertEqual(goal['name'], '리뷰 처리량')
            self.assertTrue(goal['sentence'].startswith('리뷰 요청이'))
            self.assertEqual(goal['metrics'][0]['목표'], '80%')
            self.assertEqual(goal['metrics'][0]['기준선'], '미확인')
            self.assertEqual(goal['guardrails'][0]['지표'], '머지 후 결함')
            self.assertEqual(goal['donts'], ['테스트 없이 승인'])


class TipTests(unittest.TestCase):
    def test_daily_tip_is_stable_within_a_day_and_uses_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            add_entry(tmp, 'alpha', '첫 주의', '본문 하나')
            add_entry(tmp, 'alpha', '둘째 주의', '본문 둘')
            from teamkit.knowledge import search
            entries = search(tmp)
            day = datetime.date(2026, 11, 5)
            first = hq.daily_tip(entries, day)
            self.assertEqual(first, hq.daily_tip(entries, day))
            titles = {hq.daily_tip(entries, day + datetime.timedelta(days=n))['title'] for n in range(4)}
            self.assertEqual(len(titles), 2)
            self.assertIsNone(hq.daily_tip([]))


class ApplyTests(unittest.TestCase):
    def test_apply_routes_app_actions_and_rejects_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'state.sqlite'
            item = hq.apply(state, {'op': 'bounty-add', 'title': '재현 필요', 'points': 100})
            self.assertEqual(hq.view(state)['bounties'][0]['id'], item['id'])
            with self.assertRaises(ValueError):
                hq.apply(state, {'op': 'rm -rf'})


class KnowledgeEntryTests(unittest.TestCase):
    def test_add_entry_rejects_path_escape_and_multiline_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                add_entry(tmp, '../../outside', '제목', '내용')
            entry = add_entry(tmp, 'alpha', '제목\n# 가짜 제목', '내용')
            self.assertEqual(entry['title'], '제목 # 가짜 제목')
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir() if not p.name.startswith('.')), ['alpha.md'])


class ReviewFindingTests(unittest.TestCase):
    def test_concurrent_knowledge_adds_keep_both_entries(self):
        import threading
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            add_entry(tmp, 'alpha', '처음', '본문')
            barrier = threading.Barrier(2, timeout=2)
            real = Path.read_text
            def slow_read(self, *a, **k):
                text = real(self, *a, **k)
                if self.name == 'alpha.md':
                    try:
                        barrier.wait()
                    except threading.BrokenBarrierError:
                        pass
                return text
            with patch.object(Path, 'read_text', slow_read):
                threads = [threading.Thread(target=add_entry, args=(tmp, 'alpha', f'동시 {n}', '내용')) for n in (1, 2)]
                [t.start() for t in threads]
                [t.join() for t in threads]
            text = (Path(tmp) / 'alpha.md').read_text(encoding='utf-8')
            self.assertIn('동시 1', text)
            self.assertIn('동시 2', text)

    def test_knowledge_add_refuses_symlinked_team_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            wiki = Path(tmp) / 'wiki'
            wiki.mkdir()
            outside = Path(tmp) / 'outside.md'
            outside.write_text('# 바깥\n', encoding='utf-8')
            (wiki / 'alpha.md').symlink_to(outside)
            with self.assertRaises(ValueError):
                add_entry(wiki, 'alpha', '제목', '내용')
            self.assertEqual(outside.read_text(encoding='utf-8'), '# 바깥\n')

    def test_same_request_id_never_adds_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'state.sqlite'
            for op, fields in (('bounty-add', {'title': '난제'}), ('propose', {'title': '제안', 'content': '내용', 'by': 'a'}),
                               ('coffee', {'text': '근황', 'by': 'a'})):
                first = hq.apply(state, {'op': op, 'request_id': 'r1', **fields})
                again = hq.apply(state, {'op': op, 'request_id': 'r1', **fields})
                self.assertEqual(first['id'], again['id'])
            board = hq.view(state)
            self.assertEqual([len(board[k]) for k in ('bounties', 'proposals', 'coffee')], [1, 1, 1])
            wiki = Path(tmp) / 'wiki'
            add_entry(wiki, 'alpha', '제목', '내용', request_id='k1')
            add_entry(wiki, 'alpha', '제목', '내용', request_id='k1')
            self.assertEqual((wiki / 'alpha.md').read_text(encoding='utf-8').count('제목'), 1)


class ReReviewFindingTests(unittest.TestCase):
    def test_search_ignores_in_flight_temp_files_and_vanished_documents(self):
        from unittest.mock import patch
        from teamkit.knowledge import search
        with tempfile.TemporaryDirectory() as tmp:
            add_entry(tmp, 'alpha', '제목', '내용')
            (Path(tmp) / '.alpha-tmp123.md').write_text('# 임시\n', encoding='utf-8')
            self.assertEqual([x['team'] for x in search(tmp)], ['alpha'])
            ghost = Path(tmp) / 'ghost.md'
            real_glob = Path.glob
            with patch.object(Path, 'glob', lambda self, pattern: list(real_glob(self, pattern)) + [ghost]):
                self.assertEqual([x['team'] for x in search(tmp)], ['alpha'])

    def test_same_request_id_with_changed_content_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'state.sqlite'
            hq.apply(state, {'op': 'bounty-add', 'request_id': 'r1', 'title': '원래'})
            with self.assertRaises(ValueError):
                hq.apply(state, {'op': 'bounty-add', 'request_id': 'r1', 'title': '고친 제목'})
            wiki = Path(tmp) / 'wiki'
            add_entry(wiki, 'alpha', '원래', '내용', request_id='k1')
            with self.assertRaises(ValueError):
                add_entry(wiki, 'alpha', '고친 제목', '내용', request_id='k1')


if __name__ == '__main__':
    unittest.main()
