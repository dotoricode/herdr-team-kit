"""Rich layout and local disclosure state; no ledger mutations or model calls."""
from dataclasses import dataclass, field
from io import StringIO

from rich import box
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.style import Style
from rich.table import Table
from rich.text import Text

from .dashboard import LABELS, age_text, clean, count_text, usage_bar


@dataclass
class UIState:
    expanded: set = field(default_factory=set)
    focus: tuple | None = None
    document: tuple | None = None

    def toggle(self, action):
        if action in self.expanded:self.expanded.remove(action)
        else:self.expanded.add(action)
        self.focus=action


@dataclass
class Hit:
    y: int
    x: int
    end: int
    action: tuple


@dataclass
class Frame:
    lines: list
    hits: list

    def text(self):
        return '\n'.join(''.join(s.text for s in line) for line in self.lines)

    def hit(self, y, x):
        return next((h.action for h in self.hits if h.y==y and h.x<=x<h.end),None)

    def actions(self):
        return list(dict.fromkeys(h.action for h in self.hits))


def frame(view, ui, width=100, now=0):
    width=max(1,width)
    items=[]

    def text(value, action=None, attention=False):
        return Text('\n'.join(clean(line) for line in str(value).split('\n')),style=Style(color='yellow' if attention else None,bold=attention,
                    reverse=bool(action and ui.focus==action),meta={'action':action} if action else {}))

    def table(headers):
        return Table(*headers,box=box.SQUARE,expand=True,padding=(0,1),
                     header_style='',border_style='dim',highlight=False,show_lines=False)

    def marker(action):return '▼ ' if action in ui.expanded else '▶ '

    if ui.document:
        ident,title,body=ui.document
        items.extend([text('◀ 요약으로 돌아가기 · Esc / v',('back',ident)),Text(''),
                      text('설계 문서 · '+title),
                      text('읽은 뒤 총괄 대화에서 선택·승인을 전달하세요. 이 화면은 승인하지 않습니다.'),
                      Text(''),Markdown(body,code_theme='ansi_light')])
    else:
        items.extend([text('팀 운영 현황'),text('● 현재 실행과 제품 진행을 구분합니다 · 화면 갱신은 모델을 호출하지 않습니다'),Text('')])
        pending=[r for r in view['rows'] if r['phase']=='awaiting_approval']
        if pending:
            items.append(text('⚑ 사용자 확인 '+str(len(pending))+'건',attention=True))
            for row in pending:
                items.append(text('  [설계 읽고 선택하기] '+row['id'],('review',row['id']),True))
            items.extend([text('팀장 창은 실행 기록입니다. 설계 문서는 위 항목을 클릭하거나 v로 읽으세요.'),Text('')])
        else:items.extend([text('✓ 사용자 확인 사항 없음'),Text('')])

        items.append(text('계정 사용률 · 행 클릭 / Enter로 수집 정보 펼치기'))
        narrow=width<78
        accounts=table(['계정','5h / 주간','수집 상태'] if narrow else ['계정','5h','주간','수집 상태','마지막 수집'])
        for q in view.get('accounts',[]):
            action=('account',q['provider']+'/'+q['account'])
            def value(key):
                val=q[key]
                if val is None:return '미제공' if q['current'] else '미수집' if q['age'] is None else '만료·확인 필요'
                return usage_bar(val,q['current'],width if width>=120 else 80)
            state=q['status']
            if action in ui.expanded:
                state+='\n'+('공식 App Server 조회 · 약 30초 간격' if q['provider']=='codex' else '공식 /usage 수동 확인 · 표시용, 자동 배정 근거 아님' if q.get('display_only') else '실제 응답 기준 statusline · 자동 조회 없음')
                if q['five_hour'] is None:state+='\n5h 수치 없음 · 0%로 추정하지 않음'
                if q['weekly'] is None:state+='\n주간 수치 없음 · 0%로 추정하지 않음'
                if q['provider']=='claude':state+='\nClaude에서 /usage로 직접 확인 가능 · 이 화면의 수집 자료와 별도'
                if narrow:state+='\n마지막 수집 '+age_text(q['age'])
            cells=[marker(action)+q['label'],value('five_hour')+' / '+value('weekly'),state] if narrow else [marker(action)+q['label'],value('five_hour'),value('weekly'),state,age_text(q['age'])]
            accounts.add_row(*[text(c,action) for c in cells])
        items.extend([accounts,text('*는 이전 수집값입니다. 누락·만료는 현재 잔량을 뜻하지 않습니다.'),Text('')])

        team_start=len(items)
        items.append(text('팀 운영 · 행 클릭 / Enter로 설명 펼치기'))
        teams=table(['팀','현재 상태','업무 / 완료','사용자 확인'] if narrow else ['팀','현재 상태','실행','업무','완료','사용자 확인'])
        for team,counts in view['teams'].items():
            action=('team',team);a=view.get('activity',{}).get(team,{'label':'실행 상태 미확인','working':None,'blocked':None})
            waiting=counts.get('awaiting_approval',0);blocked=sum(counts.get(k,0) for k in ('inspect','quota','environment'))
            attention=waiting or blocked or a.get('blocked')
            status=('● ' if a['working'] else '○ ')+a['label']
            if action in ui.expanded:status+='\n실행 '+count_text(a['working'])+' · 입력 요청 '+count_text(a['blocked'])+'\n팀장 창: 실행 기록 / 설계 선택: 이 화면 상단'
            count=str(sum(counts.values()));done=str(counts.get('complete',0))
            check=('설계 선택 '+str(waiting) if waiting else '')+(' · 확인 '+str(blocked) if blocked else '')
            check=check.strip(' ·') or '—'
            cells=[marker(action)+view['team_labels'].get(team,team),status,count+' / '+done,check] if narrow else [marker(action)+view['team_labels'].get(team,team),status,count_text(a['working']),count,done,check]
            teams.add_row(*[text(c,action, bool(attention) and i==len(cells)-1) for i,c in enumerate(cells)])
        items.extend([teams,Text('')])
        team_items=items[team_start:];del items[team_start:]

        remaining=sum(r['phase']!='complete' for r in view['rows'])
        items.append(text('제품 업무 · 남은 작업 '+str(remaining)+'건 · 상세는 기본 접힘'))
        works=table(['업무','단계 / 진행','기록 갱신'] if narrow else ['업무','단계','진행','기록 갱신'])
        if narrow:works.columns[1].min_width=8
        for row in view['rows']:
            action=('work',row['id']);label=LABELS.get(row['phase'],row['phase'])
            progress='구현 미승인' if row['phase']=='awaiting_approval' else '미정 · '+str(row['steps_done'])+'/'+str(row['steps_total']) if row['percent'] is None else str(row['percent'])+'% '+str(row['steps_done'])+'/'+str(row['steps_total'])
            cells=[marker(action)+row['id'],label+'\n'+progress,age_text(now-row['updated_at'])] if narrow else [marker(action)+row['id'],label,progress,age_text(now-row['updated_at'])]
            works.add_row(*[text(c,action,row['phase'] in ('awaiting_approval','inspect','quota','environment') and i==1) for i,c in enumerate(cells)])
        if not view['rows']:items.append(text('등록된 제품 작업 없음'))
        else:items.append(works)
        for row in view['rows']:
            if ('work',row['id']) not in ui.expanded:continue
            details=[text('▼ '+row['id']+' · '+row['title'],('work',row['id'])),
                     text('현재 실행: '+row.get('native_state','실행 상태 미확인')),
                     text('요청 모델: '+str(row['model_requested'] or '—')+'/'+str(row['effort_requested'] or '—'))]
            if row['percent'] is not None:
                suffix=f"{row['percent']:>3}% {row['steps_done']}/{row['steps_total']}";length=max(0,min(20,width-len(suffix)-8));n=row['percent']*length//100
                details.append(text('['+'█'*n+' '*(length-n)+'] '+suffix))
            if row['phase']=='awaiting_approval':details.extend([text('필요한 선택: 설계 대안 · 적용 범위 · 검증 합격 기준'),text('[설계 문서 읽기]',('review',row['id']),True),text('선택·승인은 총괄 대화에서 전달합니다. 팀장에게 권한 팝업이 생기는 상태가 아닙니다.')])
            execution={'handed_off':'인계 완료','ready':'대기','running':'수신 확인됨','submitted':'수신 확인 대기','awaiting_review':'완료 근거 기록됨','uncertain':'확인 필요'}.get(row['execution_state'],row['execution_state'] or '미배정')
            prefix='제안 단계(미승인): ' if row['phase']=='awaiting_approval' else '남은 단계: '
            details.extend([text(prefix+(' / '.join(row.get('remaining_steps',[])) or '최종 검증·독립 리뷰·완료 확인')),text('실행 기록: '+str(execution)+' · 배정 시도 '+str(row['attempts'])+'회')])
            if row['reason']:details.append(text('확인 사항: '+row['reason'],attention=True))
            if row['next']:details.append(text('다음: '+row['next']))
            items.extend([Padding(Group(*details),(0,1)),Text('')])
        items.extend([Text(''),*team_items])
        if not view.get('native_known'):items.append(text('실행 상태 관측이 없거나 오래됐습니다. monitor 갱신을 확인하세요.',attention=True))
        items.extend([Text(''),text('진행률: 승인된 단계의 완료 기록 기준 · 설계 완료와 제품 완료는 별도입니다.')])
    console=Console(file=StringIO(),width=width,height=100000,color_system='standard',force_terminal=True,highlight=False,markup=False)
    lines=console.render_lines(Group(*items),console.options,pad=False)
    hits=[]
    for y,line in enumerate(lines):
        x=0
        for segment in line:
            end=x+segment.cell_length
            action=segment.style.meta.get('action') if segment.style else None
            if action and end>x:hits.append(Hit(y,x,end,tuple(action)))
            x=end
    return Frame(lines,hits)
