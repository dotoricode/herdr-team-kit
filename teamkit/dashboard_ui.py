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
                    meta={'action':action} if action else {}))

    def table(headers):
        return Table(*headers,box=box.SQUARE,expand=True,padding=(0,1),
                     header_style='',border_style='dim',highlight=False,show_lines=False)

    def marker(action):return '▼ ' if action in ui.expanded else '▶ '

    if ui.document:
        ident,title,body=ui.document
        items.extend([text('◀ 요약으로 돌아가기 · Esc / v',('back',ident)),Text(''),
                      text('설계 문서 원문 · '+title),
                      text('읽은 뒤 총괄 대화에서 원하는 방식과 작업 시작 여부를 알려 주세요. 여기서는 읽기만 합니다.'),
                      Text(''),Markdown(body,code_theme='ansi_light')])
    else:
        items.extend([text('팀 운영 현황'),text('● 지금 작업 중인지와 끝낸 업무 수를 나눠 보여줍니다 · 화면을 새로 그릴 때 AI를 부르지 않습니다'),Text('')])
        pending=[r for r in view['rows'] if r['phase']=='awaiting_approval']
        held=[r for r in view['rows'] if r['phase'] in ('inspect','quota','environment')]
        inputs=[team for team,a in view.get('activity',{}).items() if a.get('blocked')]
        if pending or held or inputs:
            items.append(text('⚑ 확인할 항목 '+str(len(pending)+len(held)+len(inputs))+'개',attention=True))
            for row in pending:
                items.append(text('  [설계 읽고 선택하기] '+row['id'],('review',row['id']),True))
            for row in held:
                items.append(text('  [업무 확인] '+row['id']+' · '+LABELS[row['phase']],('work',row['id']),True))
            for team in inputs:
                items.append(text('  [답변 필요 확인] '+view['team_labels'].get(team,team)+' · 실제 입력은 해당 팀장 창에서',('team',team),True))
            if pending:items.append(text('팀장 창은 실행 기록입니다. 설계 문서는 위 항목을 클릭하거나 v로 읽으세요.'))
            items.append(Text(''))
        else:items.extend([text('✓ 사용자 확인 사항 없음'),Text('')])

        items.append(text('계정 사용률 · 행 클릭 / Enter로 확인 방법 펼치기'))
        narrow=width<78
        accounts=table(['계정','5h / 주간','확인 상태'] if narrow else ['계정','5h','주간','확인 상태','마지막 확인'])
        for q in view.get('accounts',[]):
            action=('account',q['provider']+'/'+q['account'])
            def value(key):
                val=q[key]
                if val is None:return '제공 안 함' if q['current'] else '아직 확인 못 함' if q['age'] is None else '오래됨 · 다시 확인'
                return usage_bar(val,q['current'],width if width>=120 else 80)
            state=q['status']
            if action in ui.expanded:
                state+='\n'+('공식 계정 정보에서 약 30초마다 읽음' if q['provider']=='codex' else '공식 /usage에서 직접 확인한 값 · 자동으로 작업을 맡기는 데 쓰지 않음' if q.get('display_only') else '마지막 답변 때 받은 사용량 · 따로 자동으로 읽지 않음')
                if q['five_hour'] is None:state+='\n5h 수치 없음 · 0%로 표시하지 않음'
                if q['weekly'] is None:state+='\n주간 수치 없음 · 0%로 표시하지 않음'
                if q['provider']=='claude':state+='\nClaude의 /usage로 직접 확인할 수 있음 · 이 화면의 값과 다를 수 있음'
                if narrow:state+='\n마지막 확인 '+age_text(q['age'])
            cells=[marker(action)+q['label'],value('five_hour')+' / '+value('weekly'),state] if narrow else [marker(action)+q['label'],value('five_hour'),value('weekly'),state,age_text(q['age'])]
            accounts.add_row(*[text(c,action) for c in cells])
        items.extend([accounts,text('*는 전에 확인한 값입니다. 없거나 오래된 값으로 지금 얼마나 남았는지 알 수는 없습니다.'),Text('')])

        team_start=len(items)
        items.append(text('팀 운영 · 행 클릭 / Enter로 설명 펼치기'))
        teams=table(['팀','현재 상태','업무 / 완료','사용자 확인'] if narrow else ['팀','현재 상태','실행','업무','완료','사용자 확인'])
        for team,counts in view['teams'].items():
            action=('team',team);a=view.get('activity',{}).get(team,{'label':'지금 상태 확인 안 됨','working':None,'blocked':None})
            waiting=counts.get('awaiting_approval',0);blocked=sum(counts.get(k,0) for k in ('inspect','quota','environment'))
            attention=waiting or blocked or a.get('blocked')
            status=('● ' if a['working'] else '○ ')+a['label']
            if action in ui.expanded:status+='\n실행 '+count_text(a['working'])+' · 답변 필요 '+count_text(a['blocked'])+'\n팀장 창: 실행 기록 / 설계 선택: 이 화면 상단'
            count=str(sum(counts.values()));done=str(counts.get('complete',0))
            check=' · '.join(label for label in ('설계 선택 '+str(waiting) if waiting else '', '확인 '+str(blocked) if blocked else '', '답변 필요 '+str(a['blocked']) if a.get('blocked') else '') if label)
            check=check.strip(' ·') or '—'
            cells=[marker(action)+view['team_labels'].get(team,team),status,count+' / '+done,check] if narrow else [marker(action)+view['team_labels'].get(team,team),status,count_text(a['working']),count,done,check]
            teams.add_row(*[text(c,action, bool(attention) and i==len(cells)-1) for i,c in enumerate(cells)])
        items.extend([teams,Text('')])
        team_items=items[team_start:];del items[team_start:]

        remaining=sum(r['phase']!='complete' for r in view['rows'])
        items.append(text('제품 업무 · 남은 작업 '+str(remaining)+'건 · 상세는 기본 접힘'))
        works=table(['업무','단계 / 진행','마지막 기록'] if narrow else ['업무','단계','진행','마지막 기록'])
        if narrow:works.columns[1].min_width=8
        for row in view['rows']:
            action=('work',row['id']);label=LABELS.get(row['phase'],row['phase'])
            progress='작업 시작 허락 필요' if row['phase']=='awaiting_approval' else '아직 정하지 않음 · '+str(row['steps_done'])+'/'+str(row['steps_total']) if row['percent'] is None else str(row['percent'])+'% '+str(row['steps_done'])+'/'+str(row['steps_total'])
            cells=[marker(action)+row['id'],label+'\n'+progress,age_text(now-row['updated_at'])] if narrow else [marker(action)+row['id'],label,progress,age_text(now-row['updated_at'])]
            works.add_row(*[text(c,action,row['phase'] in ('awaiting_approval','inspect','quota','environment') and i==1) for i,c in enumerate(cells)])
        if not view['rows']:items.append(text('등록된 제품 작업 없음'))
        else:items.append(works)
        for row in view['rows']:
            if ('work',row['id']) not in ui.expanded:continue
            details=[text('▼ '+row['id']+' · '+row['title'],('work',row['id'])),
                     text('현재 실행: '+row.get('native_state','지금 상태 확인 안 됨')),
                     text('요청한 모델: '+str(row['model_requested'] or '—')+'/'+str(row['effort_requested'] or '—'))]
            if row['percent'] is not None:
                suffix=f"{row['percent']:>3}% {row['steps_done']}/{row['steps_total']}";length=max(0,min(20,width-len(suffix)-8));n=row['percent']*length//100
                details.append(text('['+'█'*n+' '*(length-n)+'] '+suffix))
            if row['phase']=='awaiting_approval':details.extend([text('정할 내용: 어떤 방식으로 어디까지 바꿀지 · 어떻게 확인하고 통과로 볼지'),text('[설계 문서 읽기]',('review',row['id']),True),text('선택한 내용과 작업 시작 허락은 총괄 대화에서 알려 주세요. 팀장 창에 권한을 묻는 팝업이 뜨는 것은 아닙니다.')])
            execution={'handed_off':'작업을 멈추고 이어갈 내용 저장함','ready':'대기','running':'요청 받음','submitted':'요청을 받았는지 확인 중','awaiting_review':'결과와 확인 내용 저장됨','uncertain':'확인 필요'}.get(row['execution_state'],row['execution_state'] or '담당자 정하기 전')
            prefix='앞으로 할 일(아직 허락받지 않음): ' if row['phase']=='awaiting_approval' else '남은 단계: '
            details.extend([text(prefix+(' / '.join(row.get('remaining_steps',[])) or '마지막 동작 확인 · 다른 담당자의 코드 확인 · 완료 확인')),text('실행 기록: '+str(execution)+' · 작업 시작 시도 '+str(row['attempts'])+'회')])
            if row['reason']:details.append(text('확인 사항: '+row['reason'],attention=True))
            if row['next']:details.append(text('다음: '+row['next']))
            items.extend([Padding(Group(*details),(0,1)),Text('')])
        items.extend([Text(''),*team_items])
        if not view.get('native_known'):items.append(text('지금 작업 중인지 확인할 수 없습니다. 상태를 읽는 프로그램이 켜져 있는지 확인하세요.',attention=True))
        items.extend([Text(''),text('진행률은 허락받은 작업 중 끝난 단계로 계산합니다. 설계가 끝나도 제품 작업이 끝난 것은 아닙니다.')])
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
