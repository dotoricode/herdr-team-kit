"""Read-only product progress TUI. No provider clients or agent prompts."""
from collections import Counter
import curses
import fcntl
import json
import os
from pathlib import Path
import shutil
import sys
import time
import unicodedata

from .runtime import Herdr, RuntimeError as KitError
from .store import ACTIVE
from .workflow import snapshot

LABELS={'planning':'설계 중','awaiting_approval':'승인 대기','approved':'배정 대기',
        'in_progress':'진행 중','awaiting_review':'검증·리뷰 대기','complete':'검증 완료',
        'inspect':'확인 필요','quota':'한도 대기','environment':'환경 대기'}
COLORS={'planning':'34','awaiting_approval':'33','approved':'36','in_progress':'34',
        'awaiting_review':'33','complete':'32','inspect':'31','quota':'33','environment':'33'}


def clean(value):
    # Persisted titles/reasons must not inject terminal control sequences.
    return ''.join(c if c.isprintable() else ' ' for c in str(value))


def columns(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W','F') else 1 for c in text)


def model(data,config):
    rows=[]
    for work in sorted(data['works'],key=lambda w:(w['team'],w['id'])):
        tasks=[t for t in data['tasks'] if t.get('work_id')==work['id']]
        active=[t for t in tasks if t['state'] in ACTIVE]
        task=active[-1] if active else tasks[-1] if tasks else None
        phase='inspect' if any(t['state']=='uncertain' for t in active) else work.get('hold',{}).get('kind',work['phase'])
        approved=bool(work.get('approval'))
        percent=None
        if approved:
            percent=min(99,sum(s['weight'] for s in work.get('steps',[]) if s['id'] in work['milestones']))
            if work['phase']=='complete':percent=100
        rows.append({'id':work['id'],'team':work['team'],'title':work['title'],'phase':phase,
                     'percent':percent,'steps_done':len(work['milestones']),'steps_total':len(work.get('steps',[])),
                     'revision':work['revision'],'approved':approved,'updated_at':work['updated_at'],
                     'model_requested':task.get('model') if task else None,'effort_requested':task.get('effort') if task else None,
                     'execution_state':task['state'] if task else None,'attempts':len(tasks),
                     'reason':work.get('hold',{}).get('reason',''),
                     'next':work.get('checkpoint',{}).get('next',''),
                     'active_model_attempts':len(active)})
    teams={team:dict(Counter(row['phase'] for row in rows if row['team']==team)) for team in config['teams']}
    return {'rows':rows,'teams':teams,'team_labels':{team:config.get('dashboard_workspaces',{}).get(team,{}).get('label',team) for team in teams},'observed_at':data['observed_at'],'event_sequence':data['event_sequence']}


def render(view,color=False,now=None):
    now=time.time() if now is None else now
    lines=['팀 운영 현황  ·  '+time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(now)),
           '제품 작업 건수 기준 · 요청 모델/effort 표시 · 화면 갱신은 모델을 호출하지 않음','']
    for team,counts in view['teams'].items():
        total=sum(counts.values());done=counts.get('complete',0);waiting=counts.get('awaiting_approval',0)
        blocked=sum(counts.get(x,0) for x in ('inspect','quota','environment'))
        lines.append(f"{clean(view['team_labels'].get(team,team))}  작업 {total}건  완료 {done}  승인 대기 {waiting}  차단·대기 {blocked}")
    lines+=['','작업 / 단계 / 진행률 / 요청 모델·effort / 기록 갱신 경과','─'*66]
    for row in view['rows']:
        label=LABELS.get(row['phase'],row['phase'])
        if color:label='\x1b['+COLORS.get(row['phase'],'37')+'m'+label+'\x1b[0m'
        percent='미정' if row['percent'] is None else str(row['percent'])+'%'
        seconds=max(0,int(now-row['updated_at']))
        actor='—' if row['model_requested'] is None else clean(row['model_requested'])+'/'+clean(row['effort_requested'])
        lines.append(f"{clean(row['id'])}  {label}  {percent}  {actor}  {seconds}초 전")
        lines.append('  '+clean(row['title'])+f"  [설계 v{row['revision']}, 단계 {row['steps_done']}/{row['steps_total']}]")
        if row['reason']:lines.append('  주의: '+clean(row['reason']))
        if row['next']:lines.append('  다음: '+clean(row['next']))
    if not view['rows']:lines.append('등록된 제품 작업 없음 · 팀장 대기 세션은 작업 건수에서 제외')
    lines+=['','진행률은 승인된 단계의 완료 기록 기준입니다. 100%는 필수 단계와 최신 독립 리뷰 확인 후 표시합니다.',
            '기록 갱신 경과는 마지막 업무 기록 시각이며, 프로세스 생존이나 작업 완료를 뜻하지 않습니다.',
            '이동: ↑↓ / PgUp·PgDn · 종료: q / Ctrl+C · 업무 기록과 AI 세션은 유지됩니다.']
    return '\n'.join(lines)


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
        summary=f"작업 {total} · 완료 {counts.get('complete',0)}"
        wait=counts.get('awaiting_approval',0)
        blocked=sum(counts.get(x,0) for x in ('inspect','quota','environment'))
        runtime.call(*prefix,'workspace','report-metadata',target['id'],'--source','teamkit-progress',
                     '--token','work_summary='+summary,
                     '--token','approval_wait='+('승인 대기 '+str(wait) if wait else ''),
                     '--token','work_blocked='+('확인·차단 '+str(blocked) if blocked else ''),'--ttl-ms','30000')


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
    next_read=0;last_publish=0;offset=0;lines=[];warning=''
    while True:
        now=time.monotonic()
        if now>=next_read:
            view=model(snapshot(args.state),config)
            lines=render(view).splitlines()
            next_read=now+args.interval
            if args.publish and now-last_publish>=10:
                try:publish(view,config);warning=''
                except (KitError,ValueError):warning='좌측 요약 갱신 실패 · 세션과 workspace 연결을 확인해 주세요.'
                last_publish=now
        height,width=screen.getmaxyx();visible=max(1,height-2)
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
        footer=warning or f'기록 {offset+1}–{min(len(lines),offset+visible)}/{len(lines)} · ↑↓ / PgUp·PgDn · q 종료 · 모델 호출 없음'
        try:screen.addnstr(height-1,0,footer,max(1,width-1),curses.A_REVERSE)
        except curses.error:pass
        screen.refresh()
        key=screen.getch()
        if key in (ord('q'),ord('Q')):return
        if key in (curses.KEY_DOWN,ord('j')):offset+=1
        if key in (curses.KEY_UP,ord('k')):offset-=1
        if key==curses.KEY_NPAGE:offset+=visible
        if key==curses.KEY_PPAGE:offset-=visible
