"""Read-only product progress TUI. No provider calls or agent prompts."""
from collections import Counter
import curses
import fcntl
import json
import os
from pathlib import Path
import sys
import time
import unicodedata

from .runtime import Herdr, RuntimeError as KitError
from .store import ACTIVE
from .workflow import snapshot
from .config import binding
from .policy import number, CLAUDE_QUOTA_SOURCE
from .monitor import namespace

LABELS={'planning':'설계 중','awaiting_approval':'설계 검토 필요','approved':'배정 대기',
        'in_progress':'진행 중','awaiting_review':'검증·리뷰 대기','complete':'검증 완료',
        'inspect':'확인 필요','quota':'한도 대기','environment':'환경 대기'}
COLORS={'planning':'34','awaiting_approval':'33','approved':'36','in_progress':'34',
        'awaiting_review':'33','complete':'32','inspect':'31','quota':'33','environment':'33'}


def clean(value):
    # Persisted titles/reasons must not inject terminal control sequences.
    return ''.join(c if c.isprintable() else ' ' for c in str(value))


def columns(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W','F') else 1 for c in text)


def quota_rows(data, config):
    now=data['observed_at'];rows=[]
    for provider,accounts in config['profiles'].items():
        for account,pr in accounts.items():
            q=data.get('quotas',{}).get(provider+'/'+account)
            trusted=isinstance(q,dict) and q.get('binding')==binding(pr)
            if provider=='claude':trusted=trusted and q.get('source')==CLAUDE_QUOTA_SOURCE
            q=q if trusted else {}
            at=q.get('observed_at');age=now-at if number(at) and 0<=now-at else None
            fresh=age is not None and age<=config.get('quota_ttl_seconds',120) and not q.get('unavailable')
            values={}
            for key in ('five_hour','weekly'):
                window=q.get(key,{})
                if not isinstance(window,dict):window={}
                used,reset=window.get('used'),window.get('resets_at')
                values[key]=used if age is not None and number(used) and 0<=used<=100 and number(reset) and reset>now else None
            status='최신' if fresh and all(v is not None for v in values.values()) else '이전 기록' if age is not None else '미수집'
            if q.get('unavailable'):status='조회 실패'+(' · 이전 기록' if age is not None else '')
            elif age is not None and any(v is None for v in values.values()):status='일부 한도 확인 불가' if fresh else '갱신 필요'
            rows.append({'provider':provider,'account':account,'label':('Codex' if provider=='codex' else 'Claude')+' '+('회사' if account=='team' else '개인'),
                         'age':age,'current':fresh,'status':status,**values})
    return rows


def native_view(data,config):
    value=data.get('monitor')
    valid=isinstance(value,dict) and value.get('namespace')==namespace(config)
    at=value.get('observed_at') if valid else None
    ttl=value.get('native_ttl') if valid else None
    valid=valid and number(at) and number(ttl) and 0<ttl<=180 and 0<=data['observed_at']-at<=ttl and not value.get('error')
    agents=value.get('agents',[]) if valid else []
    if not isinstance(agents,list):agents=[];valid=False
    return {a.get('name'):a for a in agents if isinstance(a,dict) and a.get('name')},bool(valid)


def team_activity(team,tasks,agents,known,config):
    target=config.get('dashboard_workspaces',{}).get(team,{})
    names={t.get('runtime',{}).get('name') for t in tasks if t.get('team')==team and t['state'] in ACTIVE|{'ready','awaiting_review'} and not t.get('runtime',{}).get('workspace_closed')}
    observed=[a for name,a in agents.items() if name in names or a.get('workspace_id')==target.get('id')]
    if not known:return {'label':'실행 상태 미확인','working':None,'blocked':None}
    working=sum(a.get('agent_status')=='working' for a in observed)
    blocked=sum(a.get('agent_status')=='blocked' for a in observed)
    missing=any(t['state'] in ACTIVE|{'ready','awaiting_review'} and not t.get('runtime',{}).get('workspace_closed') and t.get('runtime',{}).get('name') not in agents for t in tasks if t.get('team')==team)
    unknown=missing or bool(target.get('id') and not any(a.get('workspace_id')==target['id'] for a in observed)) or any(a.get('agent_status') not in ('working','blocked','idle','done') for a in observed)
    label='작업 중' if working else '입력 요청 있음' if blocked else '실행 상태 미확인' if unknown else '대기 중'
    return {'label':label,'working':working if not unknown or working else None,'blocked':blocked if not unknown or blocked else None}


def count_text(value):
    return '—' if value is None else str(value)


def padded(value,width):
    return value+' '*max(1,width-columns(value))


def age_text(seconds):
    if seconds is None:return '기록 없음'
    if seconds<60:return str(int(seconds))+'초 전'
    if seconds<3600:return str(int(seconds//60))+'분 전'
    return str(round(seconds/3600,1))+'시간 전'


def usage_bar(value,current,width):
    if value is None:return '—'
    percent=format(value,'.0f')+'%'+('' if current else '*')
    length=8 if width>=100 else 0
    return ('|'+'█'*(int(value)*length//100)+' '*(length-int(value)*length//100)+'| ' if length else '')+percent


def model(data,config,now=None):
    data={**data,'observed_at':time.time() if now is None else now}
    rows=[]
    agents,native_known=native_view(data,config)
    for work in sorted(data['works'],key=lambda w:(w['team'],w['id'])):
        tasks=[t for t in data['tasks'] if t.get('work_id')==work['id']]
        active=[t for t in tasks if t['state'] in ACTIVE]
        task=active[-1] if active else tasks[-1] if tasks else None
        phase='inspect' if any(t['state']=='uncertain' for t in active) else work.get('hold',{}).get('kind',work['phase'])
        reason=work.get('hold',{}).get('reason','')
        if task and task['state']=='handed_off':
            failure=task.get('checkpoint',{}).get('failure',{})
            if failure.get('kind')=='environment':
                phase='environment';reason='환경 실패: '+failure.get('cause','unknown')
        if work['phase']=='complete':
            phase='complete';reason=''
        approved=bool(work.get('approval'))
        percent=None
        if approved:
            percent=min(99,sum(s['weight'] for s in work.get('steps',[]) if s['id'] in work['milestones']))
            if work['phase']=='complete':percent=100
        observed=agents.get(task.get('runtime',{}).get('name')) if task else None
        live='공간 정리됨' if task and task.get('runtime',{}).get('workspace_closed') else {'working':'작업 중','blocked':'입력 요청 있음','idle':'응답 종료·대기','done':'응답 종료·대기'}.get(observed.get('agent_status'),'실행 상태 미확인') if observed else '실행 상태 미확인'
        review_path=str(Path(work['private_root'])/work['id']/('r'+str(work['revision']))/'plan.md') if work.get('private_root') else ''
        rows.append({'id':work['id'],'team':work['team'],'title':work['title'],'phase':phase,
                     'percent':percent,'steps_done':len(work['milestones']),'steps_total':len(work.get('steps',[])),
                     'remaining_steps':[step['title'] for step in work.get('steps',[]) if step['id'] not in work['milestones']],
                     'revision':work['revision'],'approved':approved,'updated_at':work['updated_at'],
                     'model_requested':task.get('model') if task else None,'effort_requested':task.get('effort') if task else None,
                     'execution_state':task['state'] if task else None,'attempts':sum(t.get('attempt_count',0) for t in tasks),
                     'reason':reason,'native_state':live,'review_path':review_path,
                     'next':work.get('checkpoint',{}).get('next',''),
                     'active_model_attempts':len(active)})
    teams={team:dict(Counter(row['phase'] for row in rows if row['team']==team)) for team in config['teams']}
    return {'rows':rows,'teams':teams,'accounts':quota_rows(data,config),'activity':{team:team_activity(team,data['tasks'],agents,native_known,config) for team in teams},'native_known':native_known,'native_deadline':data['monitor']['observed_at']+data['monitor']['native_ttl'] if native_known else None,'team_labels':{team:config.get('dashboard_workspaces',{}).get(team,{}).get('label',team) for team in teams},'observed_at':data['observed_at'],'event_sequence':data['event_sequence']}


def render(view,color=False,now=None,width=80):
    now=time.time() if now is None else now
    lines=['팀 운영 현황  ·  '+time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(now)),
           '제품 작업 건수 기준 · 요청 모델/effort 표시 · 화면 갱신은 모델을 호출하지 않음','']
    lines+=['계정 사용률  ·  5h / Weekly / 마지막 수집','']
    for account in view.get('accounts',[]):
        first=usage_bar(account['five_hour'],account['current'],width)
        second=usage_bar(account['weekly'],account['current'],width)
        span=19 if width>=100 else 9
        lines.append(padded(account['label'],14)+padded('5h '+first,span+3)+padded('주간 '+second,span+5)+account['status']+' · '+age_text(account['age']))
    lines+=['* 이전 사용률이며 현재 잔량을 뜻하지 않습니다. Claude는 실제 응답 때 수집합니다.','', '팀별 현재 실행 상태와 제품 업무']
    for team,counts in view['teams'].items():
        total=sum(counts.values());done=counts.get('complete',0);waiting=counts.get('awaiting_approval',0)
        blocked=sum(counts.get(x,0) for x in ('inspect','quota','environment'))
        activity=view.get('activity',{}).get(team,{'label':'실행 상태 미확인','working':0,'blocked':0})
        lines.append(f"{clean(view['team_labels'].get(team,team))}  현재: {activity['label']} · 실행 {count_text(activity['working'])} · 입력 요청 {count_text(activity['blocked'])}")
        lines.append(f"  제품 업무 {total}건 · 제품 완료 {done} · 설계 검토 필요 {waiting} · 확인·차단 {blocked}")
    if not view.get('native_known'):lines.append('실행 상태 관측이 없거나 오래됐습니다. monitor 갱신을 확인하세요.')
    remaining=sum(row['phase']!='complete' for row in view['rows'])
    lines.append(f'남은 작업 {remaining}건')
    lines+=['','작업 / 단계 / 진행률 / 요청 모델·effort / 기록 갱신 경과','─'*66]
    for row in view['rows']:
        label=LABELS.get(row['phase'],row['phase'])
        if color:label='\x1b['+COLORS.get(row['phase'],'37')+'m'+label+'\x1b[0m'
        percent='미정' if row['percent'] is None else str(row['percent'])+'%'
        seconds=max(0,int(now-row['updated_at']))
        actor='—' if row['model_requested'] is None else clean(row['model_requested'])+'/'+clean(row['effort_requested'])
        lines.append(f"{clean(row['id'])}  {label}  {actor}  {seconds}초 전")
        lines.append('  현재 실행: '+clean(row.get('native_state','실행 상태 미확인')))
        if row['phase']=='awaiting_approval':
            lines+=['  사용자에게 필요한 일: 설계 대안·적용 범위·검증 기준을 검토하고 총괄 대화에서 선택·승인',
                    '  검토 문서: '+clean(row.get('review_path','')),'  [v] 검토 요청·문서 열기 · 권한 팝업이 아닌 설계 선택 요청입니다.']
        counts=f"{row['steps_done']}/{row['steps_total']}"
        if row['percent'] is None:
            lines.append(f'  미정 · {counts}')
        else:
            suffix=f'{percent:>4} {counts}'
            length=max(0,min(20,width-len(suffix)-5))
            filled=row['percent']*length//100
            bar='█'*filled+' '*(length-filled)
            lines.append(f'  [{bar}] {suffix}' if length else '  '+suffix)
        lines.append('  '+clean(row['title'])+f"  [설계 v{row['revision']}, 구현·검증 단계 {row['steps_done']}/{row['steps_total']}]")
        if row['phase']!='complete':
            pending=' / '.join(clean(title) for title in row.get('remaining_steps',[]))
            lines.append('  남은 단계: '+(pending or '최종 검증·독립 리뷰·완료 확인'))
        if row['execution_state']:
            execution={'claimed':'배정 확정','launching':'시작 준비','submitted':'수신 확인 대기','running':'수신 확인됨','uncertain':'실행 결과 확인 필요','awaiting_review':'완료 근거 기록됨','handed_off':'인계됨','queued':'미배정','ready':'대기'}.get(row['execution_state'],row['execution_state'])
            lines.append('  실행 기록: '+clean(execution)+f" · 배정 시도 {row['attempts']}회")
        if row['reason']:lines.append('  주의: '+clean(row['reason']))
        if row['next']:lines.append('  다음: '+clean(row['next']))
    if not view['rows']:lines.append('등록된 제품 작업 없음 · 팀장 대기 세션은 작업 건수에서 제외')
    lines+=['','진행률은 승인된 단계의 완료 기록 기준입니다. 100%는 필수 단계와 최신 독립 리뷰 확인 후 표시합니다.',
            '기록 갱신 경과는 마지막 업무 기록 시각이며, 프로세스 생존이나 작업 완료를 뜻하지 않습니다.',
            '이동: ↑↓ / PgUp·PgDn · 종료: q / Ctrl+C · 업무 기록과 AI 세션은 유지됩니다.']
    return '\n'.join(lines)


def review_lines(view,selected):
    rows=[r for r in view['rows'] if r['phase']=='awaiting_approval']
    lines=['사용자 설계 검토 요청','↑↓ 선택 · Enter 문서 열기 · Esc/v 뒤로','선택·승인은 문서를 읽은 뒤 총괄 대화에서 전달합니다.','']
    for index,row in enumerate(rows):
        lines.append(('▶ ' if index==selected else '  ')+clean(row['id'])+' · 설계 v'+str(row['revision']))
        lines.append('  '+clean(row['title']))
    if not rows:lines.append('현재 사용자 설계 검토 요청이 없습니다.')
    return lines


def open_review(state,config,ident):
    # Re-resolve the current submitted revision, rather than opening stale UI text.
    from .workflow import document
    import subprocess
    work=next((w for w in snapshot(state)['works'] if w['id']==ident),None)
    if not work or work['phase']!='awaiting_approval':raise ValueError('검토 요청이 변경됐습니다. 현황을 새로 확인하세요.')
    path,contract,digest=document(config,work)
    if work.get('submitted_digest')!=digest:raise ValueError('설계가 변경됐습니다. 현재 버전을 다시 제출해야 합니다.')
    latest=next((w for w in snapshot(state)['works'] if w['id']==ident),None)
    if latest!=work:raise ValueError('검토 요청이 변경됐습니다. 새 revision을 확인하세요.')
    command='open' if sys.platform=='darwin' else 'xdg-open'
    subprocess.run([command,str(path/'plan.md')],check=True,timeout=10,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


def publish(view,config):
    """Metadata only. Does not alter native agent lifecycle state."""
    runtime=Herdr()
    prefix=['--session',config['herdr_session']] if config.get('herdr_session') else []
    if prefix:
        sessions=runtime.call('session','list','--json')
        names=[s.get('name') for s in sessions.get('sessions',[]) if s.get('running') is True]
        if config['herdr_session'] not in names:raise ValueError('dashboard target session is not running')
    for team,target in config.get('dashboard_workspaces',{}).items():
        result=runtime.call(*prefix,'workspace','get',target['id'])
        if result['workspace']['label']!=target['label']:
            raise ValueError('dashboard workspace identity changed; do not publish')
        counts=view['teams'][team]
        total=sum(counts.values())
        now=time.time();deadline=view.get('native_deadline')
        fresh=number(deadline) and now<deadline
        activity=view.get('activity',{}).get(team,{'label':'실행 상태 미확인','working':None}) if fresh else {'label':'실행 상태 미확인','working':None}
        ttl=min(30000,max(1,int((deadline-now)*1000))) if fresh else 30000
        summary=f"{activity['label']} · 실행 {count_text(activity['working'])} · 업무 {total} · 완료 {counts.get('complete',0)}"
        wait=counts.get('awaiting_approval',0)
        blocked=sum(counts.get(x,0) for x in ('inspect','quota','environment'))
        runtime.call(*prefix,'workspace','report-metadata',target['id'],'--source','teamkit-progress',
                     '--token','work_summary='+summary,
                     '--token','approval_wait='+('설계 검토 필요 '+str(wait) if wait else ''),
                     '--token','work_blocked='+('확인·차단 '+str(blocked) if blocked else ''),'--ttl-ms',str(ttl))


def run(args,config):
    if not 1<=args.interval<=60 or not isinstance(args.interval,(int,float)):
        raise ValueError('dashboard interval must be 1..60 seconds')
    if args.json and not args.once:raise ValueError('JSON status requires --once')
    if not args.once and not sys.stdout.isatty():raise ValueError('live TUI requires an interactive terminal')
    lock=None
    if args.publish:
        if not Path(args.state).is_file():raise ValueError('publishing requires an existing ledger')
        lock=os.open(Path(args.state).parent/'dashboard-publisher.lock',os.O_CREAT|os.O_WRONLY,0o600)
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock);raise ValueError('another dashboard publisher is active')
    try:
        if args.once:
            view=model(snapshot(args.state),config)
            if args.publish:publish(view,config)
            return view if args.json else {'display':render(view)}
        curses.wrapper(lambda screen: live(screen,args,config))
        return {'action':'dashboard_closed','model_requests':0}
    except KeyboardInterrupt:
        return {'action':'dashboard_closed','model_requests':0}
    finally:
        if lock is not None:os.close(lock)


def live(screen,args,config):
    screen.timeout(200)
    screen.keypad(True)
    curses.curs_set(0)
    colored=args.color=='always' or args.color=='auto' and 'NO_COLOR' not in os.environ
    colors={}
    if colored and curses.has_colors():
        curses.start_color();curses.use_default_colors()
        for index,(phase,code) in enumerate(COLORS.items(),1):
            fg={'31':curses.COLOR_RED,'32':curses.COLOR_GREEN,'33':curses.COLOR_YELLOW,'34':curses.COLOR_BLUE,'36':curses.COLOR_CYAN}.get(code,curses.COLOR_WHITE)
            curses.init_pair(index,fg,-1);colors[LABELS[phase]]=curses.color_pair(index)|curses.A_BOLD
    review_mode=False;selected=0
    next_read=0;next_frame=0;last_publish=float('-inf');offset=0;lines=[];warning='';last_width=None
    while True:
        height,width=screen.getmaxyx()
        resized=width!=last_width;last_width=width
        now=time.monotonic()
        if now>=next_read:
            data=snapshot(args.state)
            next_read=now+args.interval
            resized=True
        if resized or now>=next_frame:
            view=model(data,config);resized=True;next_frame=now+1
        pending=[r for r in view['rows'] if r['phase']=='awaiting_approval']
        selected=max(0,min(selected,len(pending)-1))
        if review_mode:lines=review_lines(view,selected)
        elif resized:lines=render(view,width=max(1,width-1)).splitlines()
        if args.publish and now-last_publish>=5:
            try:publish(view,config);warning=''
            except (KitError,ValueError):warning='좌측 요약 갱신 실패 · 세션과 workspace 연결을 확인해 주세요.'
            last_publish=now
        visible=max(1,height-2)
        offset=max(0,min(offset,max(0,len(lines)-visible)))
        screen.erase()
        for y,line in enumerate(lines[offset:offset+visible]):
            try:
                screen.addnstr(y,0,line,max(1,width-1))
                for label,style in colors.items():
                    where=line.find(label)
                    if where>=0:
                        col=columns(line[:where])
                        if col+columns(label)<width:screen.addstr(y,col,label,style)
            except curses.error:pass
        footer=warning or f'기록 {offset+1}–{min(len(lines),offset+visible)}/{len(lines)} · ↑↓ / PgUp·PgDn · v 검토 문서 · q 종료 · 모델 호출 없음'
        try:screen.addnstr(height-1,0,footer,max(1,width-1),curses.A_REVERSE)
        except curses.error:pass
        screen.refresh()
        key=screen.getch()
        if key in (ord('q'),ord('Q')):return
        if key==ord('v') or key==27 and review_mode:
            review_mode=not review_mode;offset=0;next_read=0
        if review_mode:
            if key in (curses.KEY_DOWN,ord('j')):selected=min(len(pending)-1,selected+1)
            if key in (curses.KEY_UP,ord('k')):selected=max(0,selected-1)
            if key in (10,13,curses.KEY_ENTER) and pending:
                try:open_review(args.state,config,pending[selected]['id']);warning='검토 문서를 열었습니다. 선택 결과는 총괄 대화에 전달하세요.'
                except Exception:warning='문서 열기 실패 · 설계 버전/경로와 기본 Markdown 앱을 확인하세요.'
            continue
        if key in (curses.KEY_DOWN,ord('j')):offset+=1
        if key in (curses.KEY_UP,ord('k')):offset-=1
        if key==curses.KEY_NPAGE:offset+=visible
        if key==curses.KEY_PPAGE:offset-=visible
