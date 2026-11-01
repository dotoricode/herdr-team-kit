import unittest
from teamkit.dashboard import age_text


class DashboardUITests(unittest.TestCase):
    def test_age_uses_hours_minutes_and_seconds(self):
        self.assertEqual(age_text(33547),'9시간 19분 7초 전')
        self.assertEqual(age_text(65),'0시간 1분 5초 전')
        self.assertEqual(age_text(0),'0시간 0분 0초 전')
        self.assertEqual(age_text(None),'기록 없음')
        self.assertEqual(age_text(-1),'시각 확인 필요')

    def view(self):
        return {'rows':[{'id':'W1','team':'alpha','title':'A [bold]literal[/bold] 긴 설계',
            'phase':'awaiting_approval','percent':None,'steps_done':0,'steps_total':1,'revision':2,
            'updated_at':0,'model_requested':'model','effort_requested':'high','native_state':'작업 창 닫힘',
            'review_path':'/srv/private/W1/r2/plan.md','remaining_steps':['검증 단계'],
            'execution_state':'handed_off','attempts':1,'reason':'','next':''}],
            'teams':{'alpha':{'awaiting_approval':1},'beta':{}},'team_labels':{'alpha':'한글 팀장','beta':'다른 팀장'},
            'activity':{'alpha':{'label':'대기 중','working':0,'blocked':0},'beta':{'label':'대기 중','working':0,'blocked':0}},
            'accounts':[{'label':'Codex 개인','provider':'codex','account':'personal','current':True,
                         'five_hour':None,'weekly':3,'status':'일부 사용량 알 수 없음','age':65}], 'native_known':True}

    def test_default_is_collapsed_and_literal_markup_is_preserved(self):
        from teamkit.dashboard_ui import UIState,frame
        ui=UIState();result=frame(self.view(),ui,width=120,now=33547)
        text=result.text()
        self.assertIn('▶ W1',text);self.assertNotIn('model/high',text)
        self.assertIn('3%',text);self.assertIn('제공 안 함',text)
        self.assertIn('9시간 19분 7초 전',text)
        self.assertIn('팀장 창은 실행 기록',text)
        self.assertNotIn('/srv/private',text)
        self.assertFalse(any(s.style and s.style.bold for line in result.lines for s in line if '—'==s.text))
        ui.toggle(('work','W1'));expanded=frame(self.view(),ui,width=120,now=33547).text()
        self.assertIn('model/high',expanded);self.assertIn('[bold]literal[/bold]',expanded)
        self.assertIn('검증 단계',expanded)

    def test_click_hitbox_and_keyboard_actions_survive_wrap_and_collapse(self):
        from teamkit.dashboard_ui import UIState,frame
        ui=UIState()
        for width in (24,60,120):
            result=frame(self.view(),ui,width=width,now=33547)
            hit=next(h for h in result.hits if h.action==('work','W1'))
            self.assertEqual(result.hit(hit.y,hit.x),('work','W1'))
            self.assertIsNone(result.hit(-1,0))
            self.assertTrue(all(sum(s.cell_length for s in line)<=width for line in result.lines))
        ui.toggle(('work','W1'));self.assertIn(('work','W1'),ui.expanded)
        ui.toggle(('work','W1'));self.assertNotIn(('work','W1'),ui.expanded)

    def test_only_nonzero_actions_have_bold_semantics(self):
        from teamkit.dashboard_ui import UIState,frame
        v=self.view();v['rows']=[];v['teams']={'beta':{}};v['accounts']=[]
        result=frame(v,UIState(),width=120,now=33547)
        self.assertFalse(any(s.style and s.style.bold for line in result.lines for s in line))
        result=frame(self.view(),UIState(),width=120,now=33547)
        self.assertTrue(any(s.style and s.style.bold and '설계' in s.text for line in result.lines for s in line))

    def test_mouse_and_enter_use_the_rendered_targets(self):
        from argparse import Namespace
        from unittest.mock import MagicMock,patch
        import curses
        from teamkit.dashboard import live
        from teamkit.dashboard_ui import UIState,frame
        ui=UIState();view=self.view();result=frame(view,ui,width=120,now=33547)
        hit=next(h for h in result.hits if h.action==('work','W1'))
        screen=MagicMock();screen.getmaxyx.return_value=(120,121)
        screen.getch.side_effect=[curses.KEY_MOUSE,32,hit.x+33,hit.y+33,10,ord('q')]
        with patch('teamkit.dashboard_ui.UIState',return_value=ui),patch('teamkit.dashboard.snapshot',return_value={}),patch('teamkit.dashboard.model',return_value=view),patch('teamkit.dashboard.time.time',return_value=33547),patch('teamkit.dashboard.curses.curs_set'),patch('teamkit.dashboard.curses.mousemask'),patch('teamkit.dashboard.curses.tigetstr',return_value=b'\x1b[M'):
            live(screen,Namespace(state='unused',publish=False,color='never',interval=60),{})
        self.assertNotIn(('work','W1'),ui.expanded,'click expands and Enter collapses the same focused row')
        self.assertTrue(any('model/high' in c.args[2] for c in screen.addnstr.call_args_list))

    def test_native_input_request_has_visible_action_text(self):
        from teamkit.dashboard_ui import UIState,frame
        v=self.view();v['rows']=[];v['teams']={'beta':{}};v['accounts']=[]
        v['activity']['beta']['blocked']=1
        result=frame(v,UIState(),width=120,now=33547)
        self.assertIn('답변 필요 1',result.text())
        self.assertNotIn('사용자 확인 사항 없음',result.text())
        self.assertFalse(any(s.style and s.style.bold and s.text.strip()=='—' for line in result.lines for s in line))

    def test_footer_click_cannot_toggle_the_hidden_row_behind_it(self):
        from argparse import Namespace
        from unittest.mock import MagicMock,patch
        import curses
        from teamkit.dashboard import live
        from teamkit.dashboard_ui import UIState,frame
        ui=UIState();view=self.view();result=frame(view,ui,width=120,now=33547)
        hit=next(h for h in result.hits if h.action[0]=='account')
        screen=MagicMock();screen.getmaxyx.return_value=(hit.y+1,121)
        screen.getch.side_effect=[curses.KEY_MOUSE,32,hit.x+33,hit.y+33,ord('q')]
        with patch('teamkit.dashboard_ui.UIState',return_value=ui),patch('teamkit.dashboard.snapshot',return_value={}),patch('teamkit.dashboard.model',return_value=view),patch('teamkit.dashboard.time.time',return_value=33547),patch('teamkit.dashboard.curses.curs_set'),patch('teamkit.dashboard.curses.mousemask'),patch('teamkit.dashboard.curses.tigetstr',return_value=b'\x1b[M'):
            live(screen,Namespace(state='unused',publish=False,color='never',interval=60),{})
        self.assertEqual(ui.expanded,set())

    def test_wheel_packets_scroll_the_document_without_treating_them_as_escape(self):
        from argparse import Namespace
        from unittest.mock import MagicMock,patch
        import curses
        from teamkit.dashboard import live
        from teamkit.dashboard_ui import UIState
        packets=[list(map(ord,'\x1b[<65;10;5M')),[curses.KEY_MOUSE,97,42,37]]
        for packet in packets:
            with self.subTest(packet=packet):
                ui=UIState();ui.document=('W1','example','\n\n'.join('line '+str(i) for i in range(100)))
                screen=MagicMock();screen.getmaxyx.return_value=(12,121);screen.getch.side_effect=[*packet,ord('q')]
                with patch('teamkit.dashboard_ui.UIState',return_value=ui),patch('teamkit.dashboard.snapshot',return_value={}),patch('teamkit.dashboard.model',return_value=self.view()),patch('teamkit.dashboard.curses.curs_set'),patch('teamkit.dashboard.curses.mousemask'),patch('teamkit.dashboard.curses.tigetstr',return_value=b'\x1b[M'):
                    live(screen,Namespace(state='unused',publish=False,color='never',interval=60),{})
                self.assertIsNotNone(ui.document)
                first_rows=[c.args[2] for c in screen.addnstr.call_args_list if c.args[0]==0]
                self.assertTrue(any('요약으로' in s for s in first_rows))
                self.assertTrue(any('요약으로' not in s and s.strip() for s in first_rows))
