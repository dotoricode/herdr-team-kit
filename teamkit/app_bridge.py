"""Local app boundary: independent GUI ledger, read-only views, explicit actions."""
import argparse
import fcntl
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
        teams.append({'id':ident, 'label':setting.get('label', ident), 'terminal':terminal,
                      'space':setting.get('space'),
                      'app_state':observed.get('status', '미확인') if observed.get('state') == 'live' else '미확인',
                      'ledger_state':bound['state'] if bound else '새 요청 대기 · 수신 확인 전',
                      'binding_status':'GUI 작업 기록 연결됨' if bound else '앱 대화 확인 · 작업 소유권 미등록',
                      'reason':error or ('' if bound else 'TUI 작업·소유권을 가져오지 않습니다.')})
    rows, holds = inventory(linked.get('inventory'))
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
    return {'observed_at':time.time(), 'teams':teams, 'accounts':value['accounts'],
            'works':value['rows'], 'inventory':rows, 'holds':holds,
            'actions':actions,
            'usage_refreshing':refresh.get('phase') in ('queued','running'),
            'usage_message':refresh.get('message',''),
            'actions_enabled':linked.get('actions_enabled') is True and not error,
            'native_error':error, 'independent':True,
            'coordinator':coordinator_status(state,linked,list(native.values()),bool(error))}


def document_view(config_path, state, ident):
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
    body = '\n'.join(clean(line) for line in raw.decode().splitlines())
    body += '\n\n승인 대상 범위\n'+ '\n'.join('- '+clean(x) for x in contract['allowed_paths'])
    body += '\n\n필수 조건\n'+ '\n'.join('- '+clean(x) for x in contract['requirements'])
    body += '\n\n완료 단계\n'+ '\n'.join('- '+clean(s['title'])+' ('+str(s['weight'])+'%)' for s in contract['steps'])
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
    return {'id':ident, 'title':work['title'], 'revision':work['revision'], 'digest':digest,
            'body':body, 'phase':work['phase'], 'can_approve':work['phase'] == 'awaiting_approval',
            'resumable':resumable}


def session_read(address):
    result=subprocess.run(['hc','get','--json',address],capture_output=True,text=True,timeout=5,check=True)
    value=json.loads(result.stdout)
    if not isinstance(value,dict):raise ValueError('invalid native session')
    return value


def work_detail(config_path, state, binding_path, ident):
    """Read one GUI work's purpose and current/historical execution sessions."""
    config=load(config_path);linked=bindings(binding_path,config);data=readonly_snapshot(state)
    row=next((w for w in model(data,config)['rows'] if w['id']==ident),None)
    if row is None:raise ValueError('GUI 작업 기록에 없는 작업입니다.')
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
                current=session_read(terminal);labels=current.get('labels',{});fg=current.get('foreground') or {}
                status=current.get('status') or {};pid=runtime.get('native_pid')
                verified=(current.get('address')==terminal and current.get('state')=='live'
                    and current.get('name')==runtime.get('native_name',runtime.get('name'))
                    and labels.get('teamkit.owner')==task['owner'] and labels.get('teamkit.task')==task['id']
                    and labels.get('teamkit.name')==runtime.get('name')
                    and type(pid) is int and pid>0 and type(fg.get('pid')) is int and fg['pid']==pid
                    and _process_matches(task.get('provider'),fg.get('name'))
                    and _detector_matches(task.get('provider'),status.get('app')))
                if verified:app_state=status.get('state','unknown')
            except (OSError,ValueError,subprocess.SubprocessError):pass
        tasks.append({'id':task['id'],'goal':'\n'.join(clean(line) for line in task.get('goal','').split('\n')),
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
    parser.add_argument('action', choices=('view','document','work-detail','approve','resume','resume-job','recover-coordinator','coordinator-job','refresh-usage','usage-job'))
    parser.add_argument('--work'); parser.add_argument('--revision', type=int); parser.add_argument('--digest')
    parser.add_argument('--task'); parser.add_argument('--owner'); parser.add_argument('--request')
    parser.add_argument('--attempt',type=int)
    parser.add_argument('--confirmed', action='store_true')
    args = parser.parse_args()
    try:
        if args.action == 'view': result = view(args.config, args.state, args.bindings)
        elif args.action == 'document': result = document_view(args.config, args.state, args.work)
        elif args.action == 'work-detail': result = work_detail(args.config,args.state,args.bindings,args.work)
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
