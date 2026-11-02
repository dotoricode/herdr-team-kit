"""Local app boundary: independent GUI ledger, read-only views, explicit actions."""
import argparse
from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from .config import load
from .dashboard import model, clean
from .workflow import document, snapshot
from .app_feedback import card_detail as feedback_card_detail, card_answer, decorate_feedback, question_set, validate_send


def core_read():
    result = subprocess.run(['hc', 'ls', '--json'], capture_output=True, text=True, timeout=5, check=True)
    value = json.loads(result.stdout)
    if not isinstance(value.get('terminals'), list):
        raise ValueError('앱 실행 상태를 읽지 못했습니다.')
    return value


def bindings(path, config):
    if config.get('runtime_backend') != 'core':
        raise ValueError('앱은 core 전용 설정과 GUI 전용 작업 기록을 사용해야 합니다.')
    value = json.loads(Path(path).read_text())
    if value.get('version') != 1 or not isinstance(value.get('teams'), dict):
        raise ValueError('앱 팀장 연결 설정을 확인하세요.')
    if set(value['teams']) != set(config['teams']):
        raise ValueError('설정된 모든 팀의 연결 정보가 필요합니다.')
    return value


def readonly_snapshot(state):
    path = Path(state)
    if not path.is_file():
        raise ValueError('GUI 전용 작업 기록을 먼저 준비하세요.')
    with path.open('rb') as source:
        header = source.read(20)
    if 2 in header[18:20] or Path(str(path)+'-wal').exists() or Path(str(path)+'-shm').exists():
        raise ValueError('보조 파일을 쓰지 않는 rollback 작업 기록만 조회할 수 있습니다.')
    return snapshot(path)


def inventory(path):
    if not path:
        return [], []
    rows, holds = [], []
    for line in Path(path).read_text().splitlines():
        if re.match(r'^\| W\d+ \|', line):
            cells = [x.strip() for x in line.strip('|').split('|')]
            if len(cells) != 4:
                raise ValueError('고정 작업 목록 형식이 바뀌었습니다.')
            rows.append(dict(zip(('id', 'name', 'status', 'condition'), cells)))
        match = re.match(r'^(D\d+): (.+)$', line)
        if match:
            holds.append({'id':match[1], 'reason':match[2]})
    return rows, holds


def card_detail(config_path,state,binding_path,kind,ident,*,_source=False):
    from .app_board import presentation,board_state
    from .app_reading import reading_view
    card=feedback_card_detail(config_path,state,binding_path,kind,ident,_include_signature=True)
    config=load(config_path);linked=bindings(binding_path,config)
    profile=linked.get('card_briefs',{}).get(kind+':'+ident)
    detail=work_detail(config_path,state,binding_path,ident) if kind=='work' else None
    contract={'title':card['title'],'allowed_paths':[],'requirements':[],'steps':[]}
    if kind=='work':
        source=next(w for w in readonly_snapshot(state)['works'] if w['id']==ident)
        try:contract=document(config,source)[1]
        except (OSError,ValueError):pass
    reading=reading_view('',contract,card,profile)
    card['brief']=reading['brief'];card['brief_state']=reading['brief_state']
    authored=reading['brief'] if reading['brief_state']=='current' else None
    if kind=='inventory' and isinstance(profile,dict):authored=profile
    card['presentation']=presentation(card,detail,authored)
    if authored and isinstance(profile.get('title'),str):card['presentation']['title']=profile['title']
    card['board']=board_state(state,card)
    if _source and detail is not None:card['_source_row']=detail
    if not _source:card.pop('_view_signature',None)
    return card


def card_board_action(config_path,state,binding_path,kind,ident,action,rid,revision):
    from .app_board import board_action
    if bindings(binding_path,load(config_path)).get('actions_enabled') is not True:
        raise ValueError('보드 정리 기능이 비활성화돼 있습니다.')
    card=card_detail(config_path,state,binding_path,kind,ident)
    result=board_action(state,card,action,rid,revision)
    return {'board':result,'message':'보드 정리 상태를 변경했습니다. 실제 업무 완료·승인·전달은 변경하지 않습니다.'}


def view(config_path, state, binding_path):
    config = load(config_path)
    linked = bindings(binding_path, config)
    data = readonly_snapshot(state)
    value = model(data, config)
    error = ''
    try:
        native = {t['address']:t for t in core_read()['terminals']}
    except (OSError, ValueError, subprocess.SubprocessError):
        native = {}
        error = '앱의 현재 실행 상태를 확인하지 못했습니다.'
    teams = []
    for ident in config['teams']:
        setting = linked['teams'][ident]
        terminal = setting.get('terminal')
        observed = native.get(terminal, {})
        tasks = [t for t in data['tasks'] if t['team'] == ident and t['role'] == 'leader']
        bound = next((t for t in tasks if t.get('runtime', {}).get('backend') == 'core'
                      and t['runtime'].get('terminal') == terminal and t.get('owner')), None)
        verified=False;app_state='unknown'
        if bound and observed.get('state')=='live':
            try:
                verified,status=verified_session(bound,terminal)
                if verified:app_state=status.get('state','unknown')
            except (OSError,ValueError,subprocess.SubprocessError):pass
        teams.append({'id':ident, 'label':setting.get('label', ident), 'terminal':terminal,
                      'space':setting.get('space'),
                      'app_state':app_state,'identity_verified':verified,
                      'ledger_state':bound['state'] if bound else '새 요청 대기 · 수신 확인 전',
                      'binding_status':'GUI 작업 기록 연결됨' if bound else '앱 대화 확인 · 작업 소유권 미등록',
                      'reason':error or ('현재 팀장의 실행 신원을 확인하지 못했습니다.' if not verified else '')})
    from .session_pool import status as pool_status
    pool_data=pool_status(config,state)
    for team in teams:
        team['sessions']=[slot for slot in pool_data.get('slots',[]) if slot['team']==team['id']]
    from .context_handoff import summary as context_summary
    context_status=context_summary(config,state)
    for team in teams:
        team['context_handoff']=next((j for j in context_status['active'] if j['terminal']==team['terminal']),None)
    rows, holds = inventory(linked.get('inventory'))
    feedback_error=''
    try:
        if not decorate_feedback(config_path,state,binding_path,rows,value['rows']):raise ValueError('incoherent cards')
    except (OSError,ValueError):
        feedback_error='카드 상태·답변·전달 기록을 함께 확인하지 못했습니다. 다음 갱신에서 다시 확인합니다.'
        for row in [*rows,*value['rows']]:
            row['interaction']={'state':'unknown','column':'review','label':'카드 동기화 확인 필요','reason':feedback_error,'action':'기록 확인'}
    try:
        for kind,collection in (('inventory',rows),('work',value['rows'])):
            for row in collection:
                projected=card_detail(config_path,state,binding_path,kind,row['id'],_source=True)
                if kind=='work':
                    from .app_feedback import _work_view_signature
                    if _work_view_signature(row)!=projected['_view_signature']:raise ValueError('work snapshot changed')
                    source=projected['_source_row']
                    if any(source.get(k)!=v for k,v in row.items() if k not in ('native_state','feedback','previous_feedback','interaction','presentation','board')):raise ValueError('execution snapshot changed')
                elif (row['name'],row['status'],row['condition'])!=(projected['title'],projected['status'],projected['condition']):raise ValueError('inventory snapshot changed')
                row['presentation']=projected['presentation'];row['board']=projected['board']
    except (OSError,ValueError):
        feedback_error='카드의 현재 세션·정리 상태를 함께 확인하지 못했습니다. 다시 갱신해 주세요.'
        for row in [*rows,*value['rows']]:
            row['presentation']={'summary':row.get('title') or row.get('name'),'current_activity':'최근 상태를 확인하지 못했습니다.','owner_label':'담당자 미확인','sessions':[],'waiting_on':'상태 갱신 대기','attention':[{'label':'동기화 미확인','reason':feedback_error}],'decision_required':False,'decision_label':'','column':'waiting'}
            row.pop('board',None)
    from .app_usage import read_coverage
    coverage=read_coverage(config,state).get('accounts',{})
    for account in value['accounts']:
        measured=coverage.get(account['provider']+'/'+account['account'])
        if not measured:continue
        account['coverage']=measured.get('status_label','확인 필요')+(' · 표시 전용' if measured.get('display_only') else '')
        missing=[w.get('reason_label','제공 안 함') for w in measured.get('windows',{}).values() if w.get('status')=='not_provided']
        account['reason']=' '.join([measured.get('reason_label',''),*missing]).strip()
        account['action']=measured.get('action',{}).get('label','사용량 다시 확인')
        account['windows']=measured.get('windows',{})
    refresh_record=Path(state).resolve().with_name('usage-refresh.json')
    refresh={}
    if refresh_record.exists():refresh=json.loads(refresh_record.read_text())
    actions=[]
    action_root=Path(state).resolve().parent/'app-actions'
    if action_root.is_dir():
        for folder in sorted(action_root.iterdir()):
            if not folder.is_dir() or not (folder/'request.json').is_file():continue
            request=json.loads((folder/'request.json').read_text())
            status='접수 · 실행 확인 대기';message=''
            if (folder/'started.json').exists():status='재개 확인 중'
            for filename, label in [('result.json','재개 요청 처리됨'),('error.json','재개 실패 · 확인 필요'),('uncertain.json','실행 결과 불명 · 확인 필요')]:
                if (folder/filename).exists():
                    status=label;message=json.loads((folder/filename).read_text()).get('message','')
            actions.append({'id':folder.name,'task':request['task'],'status':status,'message':message})
    from .app_coordinator import status as coordinator_status
    return {'synchronization':{'state':'partial' if error or feedback_error else 'ok','sources':{'native':not bool(error),'cards':not bool(feedback_error)},'reason':' · '.join(x for x in (error,feedback_error) if x)},'observed_at':time.time(), 'teams':teams, 'accounts':value['accounts'],
            'works':value['rows'], 'inventory':rows, 'holds':holds,
            'actions':actions,'feedback_error':feedback_error,'context_handoff':context_status,
            'usage_refreshing':refresh.get('phase') in ('queued','running'),
            'usage_message':refresh.get('message',''),
            'actions_enabled':linked.get('actions_enabled') is True and not error and not feedback_error,
            'native_error':error, 'independent':True,
            'coordinator':coordinator_status(state,linked,list(native.values()),bool(error))}


def document_view(config_path, state, ident, binding_path=None):
    config = load(config_path)
    data = readonly_snapshot(state)
    work = next((w for w in data['works'] if w['id'] == ident), None)
    if work is None:
        raise ValueError('GUI 작업 기록에 없는 작업입니다.')
    folder, contract, digest = document(config, work)
    if work['phase'] == 'awaiting_approval' and digest != work.get('submitted_digest'):
        raise ValueError('확인 요청한 문서가 바뀌었습니다. 새 버전을 제출하세요.')
    if work.get('approval') and digest != work['approval']['digest']:
        raise ValueError('승인한 설계 내용이 바뀌었습니다.')
    path = folder/'plan.md'
    if path.stat().st_size > 1024*1024:
        raise ValueError('설계 문서가 너무 큽니다.')
    raw = path.read_bytes()
    import hashlib
    check = hashlib.sha256(str(work['revision']).encode()+b'\0'+raw+b'\0'+
                           json.dumps(contract,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    if check != digest:
        raise ValueError('읽는 동안 문서 내용이 바뀌었습니다.')
    body=raw.decode()
    # A second committed snapshot detects revisions changed while reading.
    if next((w for w in readonly_snapshot(state)['works'] if w['id'] == ident), None) != work:
        raise ValueError('문서가 바뀌었습니다. 다시 읽어 주세요.')
    resumable = [{'id':t['id'], 'owner':t['owner'], 'attempt':t.get('attempt_count',0)} for t in data['tasks']
                 if t.get('work_id') == ident and t['state'] == 'ready' and t.get('owner')
                 and t.get('runtime', {}).get('backend') == 'core' and t['role'] == 'leader']
    if work.get('approval'):
        resumable += [{'id':t['id'], 'owner':'-', 'attempt':t.get('attempt_count',0)} for t in data['tasks']
                      if t.get('work_id') == ident and t['state'] == 'handed_off' and not t.get('owner')
                      and t.get('runtime',{}).get('backend') == 'core' and t.get('checkpoint')
                      and t['role'] != 'leader']
    from .app_reading import reading_view
    profile=None
    card={'title':work['title'],'document_revision':work['revision'],'document_digest':digest}
    if binding_path:
        linked=bindings(binding_path,config);profile=linked.get('card_briefs',{}).get('work:'+ident)
        card=feedback_card_detail(config_path,state,binding_path,'work',ident)
        if (card.get('document_revision'),card.get('document_digest'),card.get('status'),card.get('document_state'))!=(work['revision'],digest,work['phase'],'available'):
            raise ValueError('문서가 바뀌었습니다. 다시 읽어 주세요.')
    reading=reading_view(body,contract,card,profile)
    if next((w for w in readonly_snapshot(state)['works'] if w['id']==ident),None)!=work:
        raise ValueError('문서가 바뀌었습니다. 다시 읽어 주세요.')
    return {'id':ident,'title':work['title'],'revision':work['revision'],'digest':digest,
            **reading,'phase':work['phase'],'can_approve':work['phase']=='awaiting_approval','resumable':resumable}



def session_read(address):
    result=subprocess.run(['hc','get','--json',address],capture_output=True,text=True,timeout=5,check=True)
    value=json.loads(result.stdout)
    if not isinstance(value,dict):raise ValueError('invalid native session')
    return value



def verified_session(task,terminal):
    from .core_runtime import _process_matches,_detector_matches
    runtime=task.get('runtime',{})
    current=session_read(terminal);labels=current.get('labels',{});fg=current.get('foreground') or {}
    status=current.get('status') or {};pid=runtime.get('native_pid')
    verified=(current.get('address')==terminal and current.get('state')=='live'
        and current.get('name')==runtime.get('native_name',runtime.get('name'))
        and labels.get('teamkit.owner')==task['owner'] and labels.get('teamkit.task')==task['id']
        and labels.get('teamkit.name')==runtime.get('name')
        and type(pid) is int and pid>0 and type(fg.get('pid')) is int and fg['pid']==pid
        and _process_matches(task.get('provider'),fg.get('name'))
        and _detector_matches(task.get('provider'),status.get('app')))
    return verified,status


def work_detail(config_path, state, binding_path, ident):
    """Read one GUI work's purpose and current/historical execution sessions."""
    config=load(config_path);linked=bindings(binding_path,config);data=readonly_snapshot(state)
    row=next((w for w in model(data,config)['rows'] if w['id']==ident),None)
    if row is None:raise ValueError('GUI 작업 기록에 없는 작업입니다.')
    source=next(w for w in data['works'] if w['id']==ident)
    try:current_digest=document(config,source)[2]
    except (OSError,ValueError):current_digest=None
    native_error=''
    try:native={t['address']:t for t in core_read()['terminals']}
    except (OSError,ValueError,subprocess.SubprocessError):
        native={};native_error='앱 실행 상태를 확인하지 못했습니다. 종료로 추정하지 않습니다.'
    from .core_runtime import ADDRESS, _process_matches, _detector_matches
    tasks=[]
    for task in data['tasks']:
        if task.get('work_id')!=ident:continue
        runtime=task.get('runtime',{});terminal=runtime.get('terminal') if runtime.get('backend')=='core' else None
        observed=native.get(terminal,{})
        present=None if native_error else observed.get('state')=='live'
        verified=False;app_state='unknown'
        if present and task.get('owner') and isinstance(terminal,str) and ADDRESS.fullmatch(terminal):
            try:
                verified,status=verified_session(task,terminal)
                if verified:app_state=status.get('state','unknown')
            except (OSError,ValueError,subprocess.SubprocessError):pass
        current_design=bool(current_digest and task.get('design_digest')==current_digest and task.get('design_revision')==source['revision'])
        tasks.append({'current_design':current_design,'design_revision':task.get('design_revision'),'design_digest':task.get('design_digest'),
            'id':task['id'],'goal':'\n'.join(clean(line) for line in task.get('goal','').split('\n')),
            'state':task['state'],'role':task['role'],'owner':task.get('owner'),'current_owner':bool(task.get('owner')),
            'attempt':task.get('attempt_count',0),'provider':task.get('provider'),'account':task.get('account'),
            'model_requested':task.get('model'),'effort_requested':task.get('effort'),'root':task['root'],
            'terminal':terminal,'session_name':observed.get('name') or runtime.get('name'),
            'session_location':observed.get('where'),'native_state':app_state,'session_present':present,
            'identity_verified':verified,'can_focus':verified})
    leader=linked['teams'][row['team']]
    return {**row,'observed_at':time.time(),'description':row['title'],'team_label':leader.get('label',row['team']),
            'tasks':tasks,'leader':{k:leader.get(k) for k in ('label','terminal','space')},'native_error':native_error}


def selected(config_path, state, binding_path, ident, revision, digest, confirmed):
    config = load(config_path)
    if not confirmed or bindings(binding_path, config).get('actions_enabled') is not True:
        raise ValueError('문서를 확인한 사용자의 명시적인 실행 확인이 필요합니다.')
    shown = document_view(config_path, state, ident)
    if shown['revision'] != revision or shown['digest'] != digest:
        raise ValueError('확인한 문서가 현재 버전과 다릅니다. 다시 읽어 주세요.')
    return shown


def approve(config_path, state, binding_path, ident, revision, digest, confirmed):
    selected(config_path, state, binding_path, ident, revision, digest, confirmed)
    from .__main__ import parser, run
    result = run(parser().parse_args(['--config', str(config_path), '--state', str(state),
                 'work', 'approve', ident, '--expected-revision', str(revision),
                 '--expected-digest', digest, '--user-approved', '--approval-reference',
                 'Herdr app user confirmed revision '+str(revision)+' digest '+digest]))
    return {'message':'설계 v'+str(result['revision'])+' 승인 기록을 저장했습니다. 작업을 자동 배정하지 않습니다.'}


def resume(config_path, state, binding_path, ident, revision, digest, task, owner, confirmed, attempt=None):
    shown = selected(config_path, state, binding_path, ident, revision, digest, confirmed)
    if {'id':task, 'owner':owner, 'attempt':attempt} not in shown['resumable']:
        raise ValueError('재개할 GUI 작업과 현재 소유권을 다시 확인하세요.')
    return start_resume(config_path, state, binding_path, ident, revision, digest, task, owner, attempt)


def private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(value, out, ensure_ascii=False, indent=2)


def start_resume(config_path, state, binding_path, ident, revision, digest, task, owner, attempt=None):
    folder = Path(state).resolve().parent/'app-actions'
    folder.mkdir(mode=0o700, exist_ok=True)
    lock = os.open(folder/'.enqueue.lock', os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        for previous in folder.iterdir():
            request=previous/'request.json'
            if previous.is_dir() and request.exists() and not (previous/'result.json').exists():
                if json.loads(request.read_text()).get('task') == task:
                    raise ValueError('이전 재개 결과를 확인한 뒤 진행하세요.')
        return enqueue_resume(folder, config_path, state, binding_path, ident, revision, digest, task, owner, attempt)
    except BlockingIOError as error:
        raise ValueError('다른 재개 요청을 기록 중입니다. 상태를 먼저 확인하세요.') from error
    finally:
        os.close(lock)


def enqueue_resume(folder, config_path, state, binding_path, ident, revision, digest, task, owner, attempt):
    job = folder/uuid.uuid4().hex
    job.mkdir(mode=0o700)
    packet = {'config':str(Path(config_path).resolve()), 'state':str(Path(state).resolve()),
              'bindings':str(Path(binding_path).resolve()), 'work':ident, 'revision':revision,
              'digest':digest, 'task':task, 'owner':owner, 'attempt':attempt}
    request = job/'request.json'
    private_json(request, packet)
    script = str(Path(__file__).resolve().parent.parent/'teamkit_app.py')
    # hc creates the real controller environment. Never manufacture its address.
    try:
        created = subprocess.run(['hc', 'run', '--json', '--bg', '--no-parent', '--no-notify',
                    '--name', 'app-resume-'+job.name[:12], '--', sys.executable, script,
                    'resume-job', '--request', str(request)], capture_output=True, text=True,
                    timeout=10, check=True)
        value = json.loads(created.stdout)
        if not re.fullmatch(r't[0-9a-z]+', value.get('address', '')):
            raise ValueError('controller identity unavailable')
        private_json(job/'controller.json', value)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        private_json(job/'uncertain.json', {'message':'시작 결과 불명. 재실행 전에 작업과 실행 상태를 확인하세요.'})
        raise ValueError('재개 제어 실행 결과가 불명입니다. 자동으로 반복하지 않습니다.') from error
    return {'message':'재개 요청을 접수했습니다. 수신·실행 결과는 작업 기록에서 별도로 확인하세요.', 'job':job.name}


def resume_job(path):
    path = Path(path)
    private_json(path.parent/'started.json', {'at':time.time()})
    try:
        packet = json.loads(path.read_text())
        resume_check = selected(packet['config'], packet['state'], packet['bindings'],
                                packet['work'], packet['revision'], packet['digest'], True)
        if {'id':packet['task'], 'owner':packet['owner'], 'attempt':packet['attempt']} not in resume_check['resumable']:
            raise ValueError('재개 소유권이 바뀌었습니다.')
        from .__main__ import parser, run
        message = path.parent/'message.txt'
        message.write_text('GUI 재개 요청입니다. 원래 배정된 목표의 후속 동작을 이어가세요. 기존 수신이 확인된 같은 owner이므로 receipt를 다시 호출하지 않습니다. 새 계약 변경이나 제품 구현 승인을 추정하지 말고 기존 체크포인트를 유지하세요.')
        message.chmod(0o600)
        command = ['dispatch',packet['task']] if packet['owner'] == '-' else ['tell',packet['task'],'--message-file',str(message),'--expected-owner',packet['owner']]
        command += ['--expected-attempt',str(packet['attempt'])]
        result = run(parser().parse_args(['--config',packet['config'],'--state',packet['state'], *command]))
        private_json(path.parent/'result.json', {'state':result['state'], 'at':time.time()})
    except Exception as error:
        private_json(path.parent/'error.json', {'message':str(error), 'at':time.time()})
        raise


class Refusal(ValueError):
    """A known refusal before anything reached the terminal: recorded as not_sent."""


# RuntimeError texts raised by tell/CoreRuntime strictly before `hc send`; anything else is unknown.
PRE_SEND = ('leader is not idle', 'Core agent is not idle and ready', 'Core terminal owner/process changed',
            'no matching terminal identity', 'requires a genuine HC_TERMINAL_ID', 'Core controller terminal is unavailable',
            'task runtime differs from configured backend')


def leader_binding(linked, data, team):
    """The one current GUI-ledger leader task bound to the team's official terminal."""
    setting = linked['teams'].get(team)
    if not isinstance(setting, dict) or not setting.get('terminal'):
        raise Refusal('담당 팀의 공식 터미널 연결이 없습니다.')
    found = [t for t in data['tasks'] if t.get('team') == team and t.get('role') == 'leader' and t.get('owner')
             and t.get('runtime', {}).get('backend') == 'core' and t['runtime'].get('terminal') == setting['terminal']]
    if len(found) != 1:
        raise Refusal('공식 담당 터미널과 일치하는 현재 담당 팀장 작업이 하나가 아닙니다(' + str(len(found)) + '건). 목적지를 추정하지 않습니다.')
    return setting, found[0]


def _card_team(data, kind, ident, task=None):
    if kind == 'work':
        work = next((w for w in data['works'] if w['id'] == ident), None)
        if work is None:
            raise Refusal('GUI 작업 기록에 없는 작업입니다.')
        return work['team']
    if task is None:
        raise Refusal('운영 환경 카드는 공식 담당 작업 연결이 없어 보내지 않습니다.')
    return task['team']


def destination(config_path, state, binding_path, kind, ident):
    """Resolve and verify the single official recipient right now, or refuse."""
    config = load(config_path)
    linked = bindings(binding_path, config)
    if linked.get('actions_enabled') is not True:
        raise Refusal('답변·전달 권한이 꺼져 있습니다.')
    data = readonly_snapshot(state)
    team = _card_team(data, kind, ident)
    setting, task = leader_binding(linked, data, team)
    runtime = task['runtime']
    if task['state'] != 'ready' or runtime.get('retire_requested') or runtime.get('identity_checked') is not True:
        raise Refusal('담당 팀장이 수신 가능한 대기 상태가 아닙니다(작업 상태: ' + str(task['state']) + '). 새 요청이나 강제 전달은 하지 않습니다.')
    from .core_runtime import CoreRuntime
    from .runtime import RuntimeError as KitError
    try:
        CoreRuntime(task)._native(runtime.get('name'), ready=True)
    except (KitError, OSError, ValueError) as error:
        raise Refusal('담당 팀장의 실행 신원이나 대기 상태를 확인하지 못했습니다: ' + str(error)) from error
    return {'team': team, 'label': setting.get('label', team), 'space':setting.get('space'), 'task': task['id'], 'owner': task['owner'],
            'attempt': task.get('attempt_count', 0), 'terminal': runtime['terminal'], 'name': runtime.get('name'),
            'native_pid': runtime.get('native_pid')}


def verify_author(config_path, state, binding_path, kind, ident, task_id, owner, attempt):
    """Only the current official leader, running in its own genuine managed terminal, may write a question."""
    config = load(config_path)
    linked = bindings(binding_path, config)
    data = readonly_snapshot(state)
    task = next((t for t in data['tasks'] if t['id'] == task_id), None)
    if task is None:
        raise ValueError('작성자 작업을 GUI 작업 기록에서 찾지 못했습니다.')
    team = _card_team(data, kind, ident, task)
    _, leader = leader_binding(linked, data, team)
    if (leader['id'], leader['owner'], leader.get('attempt_count', 0)) != (task_id, owner, attempt) or leader['state'] not in ('ready', 'running'):
        raise ValueError('질문은 현재 담당 팀장 작업의 소유자·시도 번호로만 작성할 수 있습니다.')
    from .core_runtime import CoreRuntime, require_managed
    if require_managed() != leader['runtime']['terminal']:
        raise ValueError('질문은 담당 팀장 자신의 관리 터미널에서만 작성할 수 있습니다.')
    CoreRuntime(leader)._native(leader['runtime'].get('name'))
    return {'task': task_id, 'owner': owner, 'attempt': attempt, 'terminal': leader['runtime']['terminal']}


def card_question_set(config_path, state, binding_path, kind, ident, task, owner, attempt, question_file, expected):
    if not all((task, owner)) or attempt is None or not question_file:
        raise ValueError('작성 task·owner·attempt와 질문 파일이 필요합니다.')
    path = Path(question_file)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
        raise ValueError('질문 파일은 16KB 이하의 일반 파일이어야 합니다.')
    author = verify_author(config_path, state, binding_path, kind, ident, task, owner, attempt)
    return question_set(config_path, state, binding_path, kind, ident, author, json.loads(path.read_text()), expected)


def card_receipt(config_path,state,binding_path,rid,task,owner,attempt,outcome,text):
    from .app_delivery import read_request,request_dir,record_receipt
    stored=read_request(request_dir(state,rid));payload=stored['payload']
    actor=verify_author(config_path,state,binding_path,payload['kind'],payload['work_id'],task,owner,attempt)
    dest=stored.get('destination') or {}
    if any(actor.get(k)!=dest.get(k) for k in ('task','owner','attempt','terminal')):
        raise ValueError('이 요청을 받은 소유자·시도·터미널만 응답을 기록할 수 있습니다.')
    return {'receipt':record_receipt(state,rid,outcome,text,'요청 ID '+rid+'\n'+text,actor)}


def _message(request, shown, question, receipt_command=None):
    payload = request['payload']
    lines = ['GUI 사용자 의견 전달입니다. 요청 ID ' + request['client_request_id'],
             '이 메시지는 사용자가 화면에서 남긴 수정 의견이며 설계 승인이나 구현 허가가 아닙니다.',
             '카드 ' + payload['kind'] + ':' + payload['work_id'] + ' · 표시 버전 ' + payload['card_revision'][:12]
             + ' · 문서 v' + str(payload['document_revision']) + ' ' + str(payload['document_digest'])[:12]]
    if question:
        option = next((o for o in question['options'] if o['option_id'] == payload['option_id']), None)
        lines += ['질문 ' + question['question_id'] + ' v' + str(question['question_revision']) + ': ' + question['prompt'],
                  '선택: ' + (option['label'] + ' (' + option['option_id'] + ')' if option else '직접 입력')]
        if payload['other_text']:
            lines.append('직접 입력: ' + payload['other_text'])
    else:
        lines.append('의견 유형: ' + {'changes': '수정이 필요합니다', 'defer': '나중에 확인합니다'}[payload['choice']])
    if payload['comment']:
        lines.append('의견: ' + payload['comment'])
    lines.append('이 요청은 한 번만 전달됩니다. 자동 재전송은 없습니다.')
    if receipt_command:
        lines += ['처리 뒤 아래 명령으로 응답 근거를 기록하세요. --outcome은 responded 또는 deferred이며, 업무 완료나 설계 승인을 대신하지 않습니다.',receipt_command]
    return '\n'.join(lines)


def _stored_matches(stored, request):
    """Same-ID lookup: every supplied field must equal the stored normalized payload."""
    for key, value in request.items():
        have = stored.get(key)
        if isinstance(value, str) and key in ('other_text', 'comment'):
            value = value.strip()
        if (have or None) != (value or None):
            return False
    return True


def card_send(config_path, state, binding_path, send_json):
    from .app_delivery import digest, record as journal, request_dir, read_request, submit, summary
    try:
        request = json.loads(send_json)
    except ValueError as error:
        raise ValueError('전송 요청 형식을 확인하세요.') from error
    rid = request.get('client_request_id') if isinstance(request, dict) else None
    try:
        existing = request_dir(state, rid)
    except ValueError:
        existing = None
    if existing is not None:
        stored = read_request(existing)
        if not _stored_matches(stored['payload'], request):
            raise ValueError('같은 요청 ID에 다른 내용이 들어왔습니다. 다시 보내지 않았습니다.')
        return {'message': '이미 접수된 요청입니다. 다시 보내지 않고 기록된 결과를 보여 줍니다.', 'request': summary(existing)}
    payload, _, _ = validate_send(config_path, state, binding_path, request)
    refusal, dest = None, None
    try:
        dest = destination(config_path, state, binding_path, payload['kind'], payload['work_id'])
    except Refusal as error:
        refusal = str(error)
    content = {k: v for k, v in payload.items() if k != 'client_request_id'}
    seen, created = submit(state, {'schema_version': 1, 'client_request_id': payload['client_request_id'], 'payload': payload,
                                    'payload_hash': digest(payload), 'content_hash': digest(content), 'destination': dest,
                                    'created_at': time.time()})
    if not created:
        return {'message': '이미 접수된 요청입니다. 다시 보내지 않았습니다.', 'request': seen}
    folder = request_dir(state, payload['client_request_id'])
    if refusal:
        journal(state, payload['client_request_id'], 'not_sent', {'reason': refusal})
        return {'message': '보내지 않았습니다. ' + refusal, 'request': summary(folder)}
    try:
        created = subprocess.run(['hc', 'run', '--json', '--bg', '--no-parent', '--no-notify', '--name', 'card-send-' + hashlib.sha256(payload['client_request_id'].encode()).hexdigest()[:16],
                                  '--', sys.executable, str(Path(__file__).resolve().parent.parent / 'teamkit_app.py'),
                                  '--config', str(Path(config_path).resolve()), '--state', str(Path(state).resolve()),
                                  '--bindings', str(Path(binding_path).resolve()), 'card-send-job', '--request', str(folder / 'request.json')],
                                 capture_output=True, text=True, timeout=10, check=True)
        if not re.fullmatch(r't[0-9a-z]+', json.loads(created.stdout).get('address', '')):
            raise ValueError('controller identity unavailable')
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        reason = '전송 제어 프로세스 시작 결과를 확인하지 못했습니다. 자동으로 다시 보내지 않습니다.'
        if isinstance(error, subprocess.CalledProcessError) and isinstance(error.stderr, str):
            detail = clean(error.stderr)[:300]
            if detail:
                reason += ' hc: ' + detail
        journal(state, payload['client_request_id'], 'unknown', {'reason': reason})
        return {'message': '전송 시작 결과가 불명입니다. 확인 전에는 다시 보내지 않습니다.', 'request': summary(folder)}
    return {'message': '전송 요청을 접수했습니다. 전송 확인은 승인·실행·완료와 별개입니다.', 'request': summary(folder)}


def card_send_status(state, rid):
    from .app_delivery import request_dir, summary
    return {'request': summary(request_dir(state, rid))}


def card_send_job(config_path, state, binding_path, request):
    from .app_delivery import claim, digest, record as journal, request_dir
    path = Path(request).resolve()
    rid = path.parent.name
    if path.name != 'request.json' or request_dir(state, rid) / 'request.json' != path:
        raise ValueError('전송 요청 위치가 기록과 다릅니다.')
    stored = claim(state, rid, os.environ.get('HC_TERMINAL_ID', ''))
    folder = path.parent
    try:
        payload, shown, question = validate_send(config_path, state, binding_path, stored['payload'])
        if digest(payload) != stored['payload_hash']:
            raise Refusal('보내기 직전 카드나 질문이 달라졌습니다.')
        dest = destination(config_path, state, binding_path, payload['kind'], payload['work_id'])
        if dest != stored['destination']:
            raise Refusal('보내기 직전 담당 팀장·소유자·시도·터미널 연결이 달라졌습니다.')
        from .store import Store
        from .workflow import check_task
        try:
            with closing(Store(state)) as store:
                check_task(load(config_path), store, store.get(dest['task']))
        except ValueError as error:
            raise Refusal('기존 전달 조건이 거절했습니다: ' + str(error)) from error
        message = folder / 'message.txt'
        fd = os.open(message, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as out:
            import shlex
            command=shlex.join([sys.executable,str(Path(__file__).resolve().parent.parent/'teamkit_app.py'),'--config',str(config_path),'--state',str(state),'--bindings',str(binding_path),'card-receipt','--request-id',rid,'--task',dest['task'],'--owner',dest['owner'],'--attempt',str(dest['attempt']),'--outcome','responded','--answer-text','처리 결과를 여기에 적으세요'])
            out.write(_message(stored, shown, question,command))
    except ValueError as error:
        journal(state, rid, 'not_sent', {'reason': str(error)})
        return
    except BaseException as error:
        journal(state, rid, 'unknown', {'reason': '전송 전 확인 중 예외가 발생했습니다: ' + type(error).__name__})
        raise
    from .__main__ import parser, run
    from .runtime import RuntimeError as KitError
    try:
        result = run(parser().parse_args(['--config', str(config_path), '--state', str(state), 'tell', dest['task'], '--message-file', str(message),
                                          '--expected-owner', dest['owner'], '--expected-attempt', str(dest['attempt'])]))
    except KitError as error:
        known = any(text in str(error) for text in PRE_SEND)
        journal(state, rid, 'not_sent' if known else 'unknown',
                {'reason': '전송 전에 거절되었습니다: ' + str(error) if known else '호출 뒤 결과를 확인하지 못했습니다: ' + str(error)})
    except ValueError as error:
        journal(state, rid, 'unknown', {'reason': '호출 뒤 결과를 확인하지 못했습니다: ' + str(error)})
    except BaseException as error:
        journal(state, rid, 'unknown', {'reason': '호출 뒤 결과를 확인하지 못했습니다: ' + type(error).__name__})
        raise
    else:
        journal(state, rid, 'sent', {'reason': 'tell 전송 턴이 확인되었습니다. 내용 수신·실행·승인·완료는 별도로 확인해야 합니다.', 'task_state': result.get('state')})


def start_usage_refresh(config_path,state,binding_path):
    config=load(config_path);bindings(binding_path,config)
    state=Path(state).resolve();target=state.with_name('usage-refresh.json')
    lock=os.open(state.with_name('usage-refresh.lock'),os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if target.exists() and json.loads(target.read_text()).get('phase') in ('queued','running'):
            return {'message':'사용량을 이미 확인 중입니다. 완료 상태를 기다려 주세요.'}
        nonce=uuid.uuid4().hex;jobs=state.parent/'usage-jobs';jobs.mkdir(mode=0o700,exist_ok=True)
        folder=jobs/nonce;folder.mkdir(mode=0o700)
        request=folder/'request.json'
        private_json(request,{'config':str(Path(config_path).resolve()),'state':str(state),'nonce':nonce})
        from .app_coordinator import save
        record={'nonce':nonce,'phase':'queued','at':time.time(),'message':'모델 호출 없이 구독 사용량을 확인하고 있습니다.'}
        save(target,record)
        try:
            value=subprocess.run(['hc','run','--json','--bg','--no-parent','--no-notify','--name','gui-usage-'+nonce[:12],
                                  '--',sys.executable,str(Path(__file__).resolve().parent.parent/'teamkit_app.py'),
                                  'usage-job','--request',str(request)],capture_output=True,text=True,timeout=10,check=True)
            result=json.loads(value.stdout)
            if not re.fullmatch(r't[0-9a-z]+',result.get('address','')):raise ValueError('사용량 controller를 확인하지 못했습니다.')
            record['controller']=result['address'];save(target,record)
        except Exception:
            record.update(phase='uncertain',message='사용량 조회 시작 결과를 확인해야 합니다. 중복 시작하지 않았습니다.')
            save(target,record);raise
        return {'message':'네 계정 사용량 갱신을 시작했습니다. Claude는 공식 /usage 관측만 표시합니다.'}
    except BlockingIOError as error:
        raise ValueError('사용량 갱신 요청을 기록 중입니다.') from error
    finally:os.close(lock)


def usage_job(request):
    packet=json.loads(Path(request).read_text());state=Path(packet['state']);config=load(packet['config'])
    from .app_usage import collect,refresh_native_usage,UsageUnavailable
    from .app_coordinator import save
    target=state.with_name('usage-refresh.json')
    lock=os.open(state.with_name('usage-refresh.lock'),os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX)
        record=json.loads(target.read_text())
        if record.get('nonce')!=packet['nonce'] or record.get('phase')!='queued':raise ValueError('사용량 갱신 요청이 바뀌었습니다.')
        record['phase']='running';save(target,record)
    finally:os.close(lock)
    try:
        observations=[]
        for account in config['profiles'].get('claude',{}):
            try:refresh_native_usage(config,state,account)
            except Exception as error:observations.append(str(error))
        for retry in range(40):
            try:
                result=collect(config,state);break
            except UsageUnavailable as error:
                if str(error)!='busy' or retry==39:raise
                time.sleep(.25)
        record.update(phase='done',message='구독 사용량 조회를 마쳤습니다.' if not observations else '일부 CLI 관측을 확인하지 못했습니다. 계정별 사유와 관측 시각을 확인하세요.',at=time.time())
        save(target,record);private_json(Path(request).with_name('result.json'),{'model_requests':0,'observation_errors':len(observations),'coverage':result})
    except Exception as error:
        record.update(phase='failed',message='사용량 갱신 실패: '+str(error),at=time.time());save(target,record)
        private_json(Path(request).with_name('error.json'),{'message':str(error)});raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default=str(Path.home()/'.config/herdr-team-kit-app/config.json'))
    parser.add_argument('--state', default=str(Path.home()/'.local/state/herdr-team-kit-app/state.sqlite'))
    parser.add_argument('--bindings', default=str(Path.home()/'.config/herdr-team-kit-app/bindings.json'))
    parser.add_argument('action', choices=('view','document','work-detail','approve','resume','resume-job','recover-coordinator','coordinator-job','refresh-usage','usage-job','card-detail','card-answer','card-question-set','card-send','card-send-job','card-send-status','card-receipt','card-board-action'))
    parser.add_argument('--work'); parser.add_argument('--revision', type=int); parser.add_argument('--digest')
    parser.add_argument('--task'); parser.add_argument('--owner'); parser.add_argument('--request')
    parser.add_argument('--attempt',type=int)
    parser.add_argument('--kind',choices=('inventory','work'))
    parser.add_argument('--card-revision');parser.add_argument('--choice');parser.add_argument('--answer-text',default='')
    parser.add_argument('--question-file');parser.add_argument('--expected-question-revision',type=int,default=0)
    parser.add_argument('--send-json');parser.add_argument('--request-id')
    parser.add_argument('--board-action',choices=('done','archive','restore'));parser.add_argument('--board-revision')
    parser.add_argument('--outcome',choices=('responded','deferred'))
    parser.add_argument('--confirmed', action='store_true')
    args = parser.parse_args()
    try:
        if args.action == 'view': result = view(args.config, args.state, args.bindings)
        elif args.action == 'document': result = document_view(args.config, args.state, args.work,args.bindings)
        elif args.action == 'work-detail': result = work_detail(args.config,args.state,args.bindings,args.work)
        elif args.action == 'card-detail':result=card_detail(args.config,args.state,args.bindings,args.kind,args.work)
        elif args.action == 'card-board-action':result=card_board_action(args.config,args.state,args.bindings,args.kind,args.work,args.board_action,args.request_id,args.board_revision)
        elif args.action == 'card-answer':result=card_answer(args.config,args.state,args.bindings,args.kind,args.work,args.card_revision,args.choice,args.answer_text)
        elif args.action == 'card-question-set':result=card_question_set(args.config,args.state,args.bindings,args.kind,args.work,args.task,args.owner,args.attempt,args.question_file,args.expected_question_revision)
        elif args.action == 'card-send':result=card_send(args.config,args.state,args.bindings,args.send_json)
        elif args.action == 'card-receipt':result=card_receipt(args.config,args.state,args.bindings,args.request_id,args.task,args.owner,args.attempt,args.outcome,args.answer_text)
        elif args.action == 'card-send-status':result=card_send_status(args.state,args.request_id)
        elif args.action == 'card-send-job':card_send_job(args.config,args.state,args.bindings,args.request);result={'message':'전송 확인을 마쳤습니다.'}
        elif args.action == 'approve': result = approve(args.config, args.state, args.bindings, args.work, args.revision, args.digest, args.confirmed)
        elif args.action == 'resume': result = resume(args.config, args.state, args.bindings, args.work, args.revision, args.digest, args.task, args.owner, args.confirmed, args.attempt)
        elif args.action == 'recover-coordinator':
            config=load(args.config);bindings(args.bindings,config)
            from .app_coordinator import start
            result=start(args.config,args.state,args.bindings,args.confirmed)
        elif args.action == 'coordinator-job':
            from .app_coordinator import run_job
            run_job(args.request);result={'message':'총괄 복구 확인을 마쳤습니다.'}
        elif args.action == 'refresh-usage':result=start_usage_refresh(args.config,args.state,args.bindings)
        elif args.action == 'usage-job':usage_job(args.request);result={'message':'사용량 확인을 마쳤습니다.'}
        else: resume_job(args.request); result = {'message':'재개 확인을 마쳤습니다.'}
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'error':str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0
