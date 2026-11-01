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
        raise ValueError('앱은 core 전용 설정과 GUI 전용 장부를 사용해야 합니다.')
    value = json.loads(Path(path).read_text())
    if value.get('version') != 1 or not isinstance(value.get('teams'), dict):
        raise ValueError('앱 팀장 연결 설정을 확인하세요.')
    if set(value['teams']) != set(config['teams']):
        raise ValueError('설정된 모든 팀의 연결 정보가 필요합니다.')
    return value


def readonly_snapshot(state):
    path = Path(state)
    if not path.is_file():
        raise ValueError('GUI 전용 장부를 먼저 준비하세요.')
    with path.open('rb') as source:
        header = source.read(20)
    if 2 in header[18:20] or Path(str(path)+'-wal').exists() or Path(str(path)+'-shm').exists():
        raise ValueError('보조 파일을 쓰지 않는 rollback 장부만 조회할 수 있습니다.')
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
                      'ledger_state':bound['state'] if bound else '새 요청 대기 · 장부 수신 확인 전',
                      'binding_status':'GUI 장부 연결됨' if bound else '앱 대화 확인 · 작업 소유권 미등록',
                      'reason':error or ('' if bound else 'TUI 작업·소유권을 가져오지 않습니다.')})
    rows, holds = inventory(linked.get('inventory'))
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
    return {'observed_at':time.time(), 'teams':teams, 'accounts':value['accounts'],
            'works':value['rows'], 'inventory':rows, 'holds':holds,
            'actions':actions,
            'actions_enabled':linked.get('actions_enabled') is True and not error,
            'native_error':error, 'independent':True}


def document_view(config_path, state, ident):
    config = load(config_path)
    data = readonly_snapshot(state)
    work = next((w for w in data['works'] if w['id'] == ident), None)
    if work is None:
        raise ValueError('GUI 장부에 없는 작업입니다.')
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
    resumable = [{'id':t['id'], 'owner':t['owner']} for t in data['tasks']
                 if t.get('work_id') == ident and t['state'] == 'ready' and t.get('owner')
                 and t.get('runtime', {}).get('backend') == 'core' and t['role'] == 'leader']
    if work.get('approval'):
        resumable += [{'id':t['id'], 'owner':'-'} for t in data['tasks']
                      if t.get('work_id') == ident and t['state'] == 'handed_off' and not t.get('owner')
                      and t.get('runtime',{}).get('backend') == 'core' and t.get('checkpoint')
                      and t['role'] != 'leader']
    return {'id':ident, 'title':work['title'], 'revision':work['revision'], 'digest':digest,
            'body':body, 'phase':work['phase'], 'can_approve':work['phase'] == 'awaiting_approval',
            'resumable':resumable}


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


def resume(config_path, state, binding_path, ident, revision, digest, task, owner, confirmed):
    shown = selected(config_path, state, binding_path, ident, revision, digest, confirmed)
    if {'id':task, 'owner':owner} not in shown['resumable']:
        raise ValueError('재개할 GUI 작업과 현재 소유권을 다시 확인하세요.')
    return start_resume(config_path, state, binding_path, ident, revision, digest, task, owner)


def private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(value, out, ensure_ascii=False, indent=2)


def start_resume(config_path, state, binding_path, ident, revision, digest, task, owner):
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
        return enqueue_resume(folder, config_path, state, binding_path, ident, revision, digest, task, owner)
    except BlockingIOError as error:
        raise ValueError('다른 재개 요청을 기록 중입니다. 상태를 먼저 확인하세요.') from error
    finally:
        os.close(lock)


def enqueue_resume(folder, config_path, state, binding_path, ident, revision, digest, task, owner):
    job = folder/uuid.uuid4().hex
    job.mkdir(mode=0o700)
    packet = {'config':str(Path(config_path).resolve()), 'state':str(Path(state).resolve()),
              'bindings':str(Path(binding_path).resolve()), 'work':ident, 'revision':revision,
              'digest':digest, 'task':task, 'owner':owner}
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
    return {'message':'재개 요청을 접수했습니다. 수신·실행 결과는 장부에서 별도로 확인하세요.', 'job':job.name}


def resume_job(path):
    path = Path(path)
    private_json(path.parent/'started.json', {'at':time.time()})
    try:
        packet = json.loads(path.read_text())
        resume_check = selected(packet['config'], packet['state'], packet['bindings'],
                                packet['work'], packet['revision'], packet['digest'], True)
        if {'id':packet['task'], 'owner':packet['owner']} not in resume_check['resumable']:
            raise ValueError('재개 소유권이 바뀌었습니다.')
        from .__main__ import parser, run
        message = path.parent/'message.txt'
        message.write_text('사용자가 확인한 동일 설계의 허용 범위에서 작업을 이어가세요. 새 계약 변경이나 구현 승인을 추정하지 마세요. 기존 owner와 체크포인트를 유지하세요.')
        message.chmod(0o600)
        command = ['dispatch',packet['task']] if packet['owner'] == '-' else ['tell',packet['task'],'--message-file',str(message)]
        result = run(parser().parse_args(['--config',packet['config'],'--state',packet['state'], *command]))
        private_json(path.parent/'result.json', {'state':result['state'], 'at':time.time()})
    except Exception as error:
        private_json(path.parent/'error.json', {'message':str(error), 'at':time.time()})
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default=str(Path.home()/'.config/herdr-team-kit-app/config.json'))
    parser.add_argument('--state', default=str(Path.home()/'.local/state/herdr-team-kit-app/state.sqlite'))
    parser.add_argument('--bindings', default=str(Path.home()/'.config/herdr-team-kit-app/bindings.json'))
    parser.add_argument('action', choices=('view','document','approve','resume','resume-job'))
    parser.add_argument('--work'); parser.add_argument('--revision', type=int); parser.add_argument('--digest')
    parser.add_argument('--task'); parser.add_argument('--owner'); parser.add_argument('--request')
    parser.add_argument('--confirmed', action='store_true')
    args = parser.parse_args()
    try:
        if args.action == 'view': result = view(args.config, args.state, args.bindings)
        elif args.action == 'document': result = document_view(args.config, args.state, args.work)
        elif args.action == 'approve': result = approve(args.config, args.state, args.bindings, args.work, args.revision, args.digest, args.confirmed)
        elif args.action == 'resume': result = resume(args.config, args.state, args.bindings, args.work, args.revision, args.digest, args.task, args.owner, args.confirmed)
        else: resume_job(args.request); result = {'message':'재개 확인을 마쳤습니다.'}
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'error':str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0
