"""Explicit recovery of the GUI coordinator from durable GUI records."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import closing

from .app_bridge import core_read
from .config import load, profile, binding, cached_quota
from .core_runtime import _call, require_managed, CoreRuntime, startup_native, _model_effort
from .policy import account_order, choose_account
from .runtime import launch_spec, RuntimeError as KitError
from .store import Store
from . import providers


def record_path(state):
    return Path(state).resolve().with_name('coordinator.json')


def scope(state):
    return hashlib.sha256(str(Path(state).resolve()).encode()).hexdigest()[:12]


def save(path, value):
    fd, name=tempfile.mkstemp(prefix='.coordinator-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as out:
            json.dump(value,out,ensure_ascii=False,indent=2);out.flush();os.fsync(out.fileno())
        os.replace(name,path)
        directory=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(name):os.unlink(name)


def read_record(state):
    path=record_path(state)
    if not path.exists():return {}
    if path.stat().st_size>1024*1024:raise ValueError('총괄 복구 기록이 너무 큽니다.')
    value=json.loads(path.read_text())
    if not isinstance(value,dict):raise ValueError('총괄 복구 기록을 확인하세요.')
    return value


def _rebind_json(path, limit=1024*1024):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as source:
        info=os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0<info.st_size<=limit:
            raise ValueError('bounded regular reconciliation evidence required')
        raw=source.read(limit+1)
    if len(raw)>limit:raise ValueError('reconciliation evidence grew during read')
    try:value=json.loads(raw)
    except (UnicodeError,ValueError) as error:raise ValueError('reconciliation evidence must be JSON') from error
    if not isinstance(value,dict):raise ValueError('reconciliation evidence must be an object')
    return value,hashlib.sha256(raw).hexdigest(),raw


def _rebind_environment(pid):
    """Return selected metadata and a credential-presence flag, never credentials."""
    from .runtime import AUTH_ENV
    result=subprocess.run(['ps','eww','-p',str(pid),'-o','command='],capture_output=True,text=True,timeout=5,check=True)
    keys={'HOME','CODEX_HOME','TEAMKIT_CONFIG','TEAMKIT_STATE','TEAMKIT_GUI_COORDINATOR',*AUTH_ENV}
    found={}
    for key,value in re.findall(r'(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=([\s\S]*?)(?=\s[A-Za-z_][A-Za-z0-9_]*=|$)',result.stdout):
        if key not in keys:continue
        if key in AUTH_ENV:
            if value.strip():found['_api_credentials_present']=True
            continue
        if key in found:raise ValueError('ambiguous native profile environment')
        found[key]=value.strip()
    return found


def _rebind_context(config_path,state,record):
    config_path=Path(config_path).resolve();state=Path(state).resolve()
    config,digest,_=_rebind_json(config_path)
    config=load(config_path)
    if _rebind_json(config_path)[1]!=digest:raise ValueError('config changed during policy read')
    policy=config.get('coordinator') or {}
    if (config.get('runtime_backend')!='core' or policy.get('provider')!='codex'
            or not record.get('nonce') or record.get('account') not in ('team','personal')
            or not isinstance(record.get('context_session_id'),str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',record['context_session_id'])
            or not isinstance(policy.get('root'),str) or not Path(policy['root']).is_absolute()
            or not Path(policy['root']).is_dir()):
        raise ValueError('existing core coordinator identity and working root required')
    pr=profile(config,'codex',record['account'],policy)
    if (record.get('profile_binding')!=binding(pr) or record.get('model')!=pr['model']
            or record.get('effort')!=pr.get('effort','low')):
        raise ValueError('existing profile binding/model/effort policy changed; do not replace it')
    binding_path=Path(config.get('context_bindings',''))
    if not binding_path.is_absolute():raise ValueError('existing absolute App bindings required')
    linked,binding_digest,_=_rebind_json(binding_path)
    sp,tab,created=destination(record,linked)
    if created or sp!=record.get('space') or tab!=record.get('tab'):
        raise ValueError('existing coordinator space/tab required; no new destination is allowed')
    return config,pr,digest,str(binding_path.resolve()),binding_digest


def _rebind_native(config_path,state,record,pr,pid,exact_argv=None):
    from .prompt_state import empty_input
    if type(pid) is not int or pid<=0:raise ValueError('positive exact native PID required')
    name='gui-coordinator-'+scope(state)
    actor=CoreRuntime(dict(id='GUI-COORDINATOR',owner=record['nonce'],provider='codex',
        runtime=dict(backend='core',name=name,terminal=record['terminal'],native_pid=pid)))
    native=actor._native(name,ready=True)
    expected={'teamkit.app-scope':scope(state),'teamkit.kind':'coordinator',
              'herdr.space':record['space'],'herdr.tab':record['tab']}
    if any(native.get('labels',{}).get(k)!=v for k,v in expected.items()):
        raise ValueError('coordinator labels or existing destination changed')
    argv=native['foreground'].get('argv') or []
    sessions=[argv[i+1] for i,value in enumerate(argv[:-1]) if value=='resume']
    if sessions!=[record['context_session_id']]:raise ValueError('native must resume the exact existing session')
    if Path(native['foreground'].get('cwd','')).resolve()!=Path(load(config_path)['coordinator']['root']).resolve():
        raise ValueError('coordinator native cwd changed')
    if exact_argv is not None:
        if (not argv or Path(argv[0]).name!='codex' or argv[1:]!=exact_argv[1:]
                or _model_effort('codex',argv)!=(pr['model'],pr.get('effort','low'))):
            raise ValueError('new native argv must match the prepared resume policy exactly')
    env=_rebind_environment(pid)
    if not isinstance(env.get('HOME'),str) or not Path(env['HOME']).is_absolute():
        raise ValueError('native process environment is unavailable; do not infer its default profile')
    effective=env.get('CODEX_HOME',str(Path(env['HOME'])/'.codex'))
    if Path(effective).resolve()!=Path(pr['home']).resolve() or env.get('_api_credentials_present'):
        raise ValueError('native profile environment differs or API credentials are present')
    if exact_argv is not None and any(env.get(k)!=v for k,v in {
            'CODEX_HOME':pr['home'],'TEAMKIT_CONFIG':str(Path(config_path).resolve()),
            'TEAMKIT_STATE':str(Path(state).resolve()),'TEAMKIT_GUI_COORDINATOR':record['nonce']}.items()):
        raise ValueError('new native profile/controller environment is not the prepared one')
    screen=_call('read','--json',record['terminal'])
    if screen.get('complete') is not True or not empty_input(screen.get('text','')):
        raise ValueError('coordinator has a user draft, dialog or incomplete screen; preserve it')
    after=actor._native(name,ready=True)
    if (after.get('turn')!=native.get('turn') or after.get('foreground')!=native.get('foreground')
            or after.get('labels')!=native.get('labels')):
        raise ValueError('coordinator turn/process metadata changed during observation')
    return native


def _rebind_pending(intent):
    return {**intent['original_record'],'phase':'rebinding','policy_rebind_intent':intent['id'],
            'reason':'기존 대화·정책을 보존한 PID 재결합 대기입니다. 자동 종료·실행하지 않습니다.'}


def _rebind_no_pending(state,record):
    from .context_handoff import jobs
    if any(j.get('target',{}).get('kind')=='coordinator' and j.get('phase') not in ('done','cancelled','closed')
           for j in jobs(state)):
        raise ValueError('unresolved coordinator handoff exists; preserve it before policy reconciliation')
    with closing(sqlite3.connect(Path(state).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        tables={x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'operation_intake_attempts' in tables:
            for body, in db.execute('SELECT body FROM operation_intake_attempts'):
                attempt=json.loads(body)
                if attempt.get('owner')==record['nonce'] and attempt.get('phase')!='submitted':
                    raise ValueError('unresolved coordinator classification attempt exists; do not replace its PID')
        if 'resource_reservations' in tables:
            for body, in db.execute('SELECT body FROM resource_reservations'):
                reservation=json.loads(body);session=reservation.get('session') or {}
                if (session.get('owner')==record['nonce'] and session.get('terminal')==record['terminal']
                        and (reservation.get('execution_reserved') or reservation.get('writes_held'))):
                    raise ValueError('coordinator still owns active execution/resource reservations')


def _rebind_lock(state):
    fd=os.open(Path(state).resolve().with_name('coordinator.lock'),os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd);raise ValueError('coordinator lock must be a regular file')
    return fd


def prepare_rebind(config_path,state,checkpoint_file,owner,pid,session_id):
    """Persist a fenced intent and return argv only; never launch/kill/send/auth."""
    require_managed();state=Path(state).resolve();config_path=Path(config_path).resolve()
    lock=_rebind_lock(state)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        record,record_digest,original=_rebind_json(record_path(state))
        config,pr,config_digest,binding_path,binding_digest=_rebind_context(config_path,state,record)
        checkpoint_path=Path(checkpoint_file).absolute()
        checkpoint,checkpoint_digest,_=_rebind_json(checkpoint_path,256*1024)
        ident=hashlib.sha256(json.dumps([owner,pid,session_id,config_digest,checkpoint_digest],sort_keys=True).encode()).hexdigest()[:32]
        root=state.with_name('coordinator-rebinds');root.mkdir(mode=0o700,exist_ok=True)
        if root.is_symlink():raise ValueError('coordinator reconciliation storage must not be a symlink')
        path=root/(ident+'.json')
        previous=_rebind_json(path)[0] if path.exists() else None
        if record.get('policy_rebind_intent') and record['policy_rebind_intent']!=ident and record.get('phase')=='rebinding':
            raise ValueError('another unresolved coordinator intent exists; inspect it without replacement')
        if previous and previous['phase']=='completed':
            return _complete_rebind(config_path,state,path,previous,owner,previous['new_pid'])
        if (record.get('phase') not in ('ready','rebinding') or record.get('nonce')!=owner
                or type(pid) is not int or record.get('native_pid')!=pid or record.get('context_session_id')!=session_id):
            raise ValueError('existing ready owner/PID/session differs; no replacement is allowed')
        required={'task':'GUI-COORDINATOR','owner':owner,'terminal':record['terminal'],'native_pid':pid,
                  'session_id':session_id,'config_sha256':config_digest,'profile_binding':binding(pr)}
        if (any(checkpoint.get(k)!=v for k,v in required.items()) or type(checkpoint.get('native_pid')) is not int
                or checkpoint.get('writer_stopped') is not True or checkpoint.get('background_writers')!=[]
                or not isinstance(checkpoint.get('verification'),list) or not checkpoint['verification']
                or any(not isinstance(v,str) or not v.strip() for v in checkpoint['verification'])
                or type(checkpoint.get('native_turn')) is not int or checkpoint['native_turn']<0
                or type(checkpoint.get('observed_at')) not in (int,float)
                or not 0<=time.time()-checkpoint['observed_at']<=300
                or not checkpoint_path.is_relative_to(Path(config['coordinator']['root']).resolve())):
            raise ValueError('fresh same-actor checkpoint with explicit stopped-writer evidence required')
        _rebind_no_pending(state,record)
        native=_rebind_native(config_path,state,record,pr,pid)
        if native.get('turn')!=checkpoint['native_turn']:
            raise ValueError('checkpoint belongs to another native turn; preserve the current execution')
        if _rebind_json(config_path)[1]!=config_digest or _rebind_json(binding_path)[1]!=binding_digest:
            raise ValueError('config or bindings changed during prepare')
        if previous:
            if previous['phase'] not in ('preparing','prepared') or previous['bindings_sha256']!=binding_digest:
                raise ValueError('existing intent contract changed')
            if _rebind_json(previous['original_record_file'])[1]!=previous['original_record_sha256']:
                raise ValueError('original coordinator backup changed')
            if record!=_rebind_pending(previous) and record_digest!=previous['original_record_sha256']:
                raise ValueError('coordinator changed after the existing intent')
            if previous['phase']=='prepared' and record==_rebind_pending(previous):return previous
            intent=previous
        else:
            if _rebind_json(record_path(state))[1]!=record_digest:raise ValueError('coordinator changed during prepare')
            spec=launch_spec('codex',pr,session_id)
            spec['env'].update(TEAMKIT_CONFIG=str(config_path),TEAMKIT_STATE=str(state),TEAMKIT_GUI_COORDINATOR=owner)
            spec['args']+=['--add-dir',str(state.parent)]
            backup=root/(ident+'.original.json')
            try:fd=os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            except FileExistsError:
                if _rebind_json(backup)[1]!=record_digest:raise ValueError('preserved original coordinator differs')
            else:
                with os.fdopen(fd,'wb') as output:output.write(original);output.flush();os.fsync(output.fileno())
            intent={'id':ident,'phase':'preparing','at':time.time(),'old_pid':pid,'owner':owner,'session_id':session_id,
                'config_path':str(config_path),'config_sha256':config_digest,'bindings_path':binding_path,
                'bindings_sha256':binding_digest,'checkpoint_file':str(checkpoint_path),'checkpoint_sha256':checkpoint_digest,
                'checkpoint':checkpoint,'profile_binding':binding(pr),'original_record':record,
                'original_record_file':str(backup),'original_record_sha256':record_digest,
                'native_argv':['codex',*spec['args']],
                'resume_argv':['env',*(k+'='+v for k,v in spec['env'].items()),'codex',*spec['args']],
                'errors':[],'boundary':'No process is started or stopped. Preserve the current address, conversation, owner, profile and App binding.',
                'next_action':'Inspect this same intent and native terminal before any external process action; repeated prepare never authorizes a retry launch.'}
            save(path,intent)
        save(record_path(state),_rebind_pending(intent))
        try:
            _rebind_no_pending(state,record)
            if _rebind_native(config_path,state,record,pr,pid).get('turn')!=native.get('turn'):
                raise ValueError('coordinator turn changed after the durable intent; preserve pending recovery')
        except Exception as error:
            intent['errors']=[*intent.get('errors',[]),{'at':time.time(),'type':type(error).__name__,'reason':str(error)}]
            save(path,intent)
            raise
        intent['phase']='prepared';save(path,intent)
        return intent
    finally:os.close(lock)


def _complete_rebind(config_path,state,path,intent,owner,pid):
    record=_rebind_json(record_path(state))[0];original=intent['original_record']
    config,pr,config_digest,binding_path,binding_digest=_rebind_context(config_path,state,record)
    if (intent.get('phase') not in ('prepared','completing','completed') or owner!=intent['owner']
            or record.get('nonce')!=owner or record.get('policy_rebind_intent')!=intent['id']
            or str(Path(config_path).resolve())!=intent['config_path'] or config_digest!=intent['config_sha256']
            or binding_path!=intent['bindings_path'] or binding_digest!=intent['bindings_sha256']
            or _rebind_json(intent['checkpoint_file'])[1]!=intent['checkpoint_sha256']
            or _rebind_json(intent['original_record_file'])[1]!=intent['original_record_sha256']
            or type(pid) is not int or pid<=0 or pid==intent['old_pid']):
        raise ValueError('intent/config/checkpoint/owner/PID contract differs; preserve unresolved intent')
    already=record.get('phase')=='ready' and record.get('native_pid')==pid
    if already:
        if any(record.get(k)!=v for k,v in original.items()
               if k not in ('phase','native_pid','policy_rebind_intent','policy_rebind_history')):
            raise ValueError('completed coordinator identity or policy changed after the original intent')
        if not any(x.get('intent')==intent['id'] and x.get('new_pid')==pid for x in record.get('policy_rebind_history',[])):
            raise ValueError('current PID has no matching formal reconciliation audit')
        if intent['phase']=='completed' and _rebind_json(record_path(state))[1]!=intent.get('completed_record_sha256'):
            raise ValueError('completed coordinator record changed; never overwrite a later state')
    elif record!=_rebind_pending(intent):
        raise ValueError('original coordinator record changed; never overwrite a later PID')
    if intent.get('new_pid') not in (None,pid):raise ValueError('late native PID differs from the original completion')
    def process_boundary():
        try:os.kill(intent['old_pid'],0)
        except ProcessLookupError:pass
        else:raise ValueError('original OS PID is still alive or was reused; do not rebind')
        os.kill(pid,0)
    process_boundary()
    native=_rebind_native(config_path,state,original,pr,pid,intent['native_argv'])
    process_boundary()
    _rebind_no_pending(state,record)
    if (_rebind_json(config_path)[1]!=intent['config_sha256'] or _rebind_json(binding_path)[1]!=intent['bindings_sha256']
            or _rebind_json(intent['checkpoint_file'])[1]!=intent['checkpoint_sha256']
            or _rebind_json(record_path(state))[0]!=record):raise ValueError('coordinator/config/bindings changed during completion')
    if intent['phase']=='completed':return intent
    intent.update(phase='completing',new_pid=pid);save(path,intent)
    if not already:
        audit={'intent':intent['id'],'at':time.time(),'old_pid':intent['old_pid'],'new_pid':pid,
               'session_id':intent['session_id'],'owner':owner,'checkpoint_sha256':intent['checkpoint_sha256'],
               'config_sha256':intent['config_sha256'],'native_turn':native.get('turn')}
        updated={**original,'native_pid':pid,'phase':'ready','policy_rebind_intent':intent['id'],
                 'policy_rebind_history':[*original.get('policy_rebind_history',[]),audit]}
        save(record_path(state),updated)
    intent.update(phase='completed',completed_at=time.time(),completed_record_sha256=_rebind_json(record_path(state))[1]);save(path,intent)
    return intent


def complete_rebind(config_path,state,ident,owner,pid):
    """Adopt only an externally resumed, proven same-session native process."""
    require_managed();state=Path(state).resolve()
    if not isinstance(ident,str) or not re.fullmatch(r'[a-f0-9]{32}',ident):raise ValueError('exact reconciliation intent required')
    lock=_rebind_lock(state)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        path=state.with_name('coordinator-rebinds')/(ident+'.json');intent=_rebind_json(path)[0]
        try:return _complete_rebind(config_path,state,path,intent,owner,pid)
        except Exception as error:
            intent['errors']=[*intent.get('errors',[]),{'at':time.time(),'type':type(error).__name__,'reason':str(error)}]
            save(path,intent)
            raise
    finally:os.close(lock)


def destination(record, linked):
    """Reuse a verified App destination; only unbound first creation gets IDs."""
    bound = linked.get('coordinator', {})
    if not isinstance(bound, dict):
        raise ValueError('총괄 연결 구조 확인 후 복구 대기입니다.')
    anchors = [source[key] for source in (record, bound)
               for key in ('space', 'tab', 'terminal') if key in source]
    if not anchors:
        return 'sp'+uuid.uuid4().hex, 'tb'+uuid.uuid4().hex, True
    if any(not isinstance(value, str) or not value for value in anchors):
        raise ValueError('총괄 연결 식별자 확인 후 복구 대기입니다.')
    try:
        result = subprocess.run(['herdr-app', 'ls'], capture_output=True, text=True, timeout=5, check=True)
        layout = json.loads(result.stdout)
        pairs = [(space, tab) for space in layout['spaces'] for tab in space['tabs']]
        for source in (record, bound):
            for field in ('space', 'tab'):
                if source.get(field):
                    pairs = [(sp, tb) for sp, tb in pairs
                             if (sp if field == 'space' else tb)['id'] == source[field]]
        if not any(source.get('tab') for source in (record, bound)):
            terminal = record.get('terminal') or bound.get('terminal')
            pairs = [(sp, tb) for sp, tb in pairs
                     if any(t.get('terminal') == terminal for t in tb['terminals'])]
        if len(pairs) != 1:
            raise ValueError('existing destination missing or ambiguous')
        space, tab = pairs[0]
        old_terminals = {source.get('terminal') for source in (record, bound)} - {None}
        if any(t.get('terminal') not in old_terminals for t in tab['terminals']):
            raise ValueError('destination contains another terminal')
        return space['id'], tab['id'], False
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        raise ValueError('기존 총괄 스페이스·탭 연결을 확인하지 못해 복구 대기입니다. 새 공간을 만들지 않습니다.') from error


def status(state, linked, terminals=None, unavailable=False):
    try:
        if unavailable:raise ValueError('core unavailable')
        record=read_record(state)
        entries=core_read()['terminals'] if terminals is None else terminals
        live=[t for t in entries if t.get('state')=='live']
        name='gui-coordinator-'+scope(state)
        owned=[t for t in live if t.get('name')==name]
        if len(owned)>1:raise ValueError('총괄 후보가 중복돼 있습니다.')
        if owned:
            target=_call('get',owned[0]['address'])
            if target.get('labels',{}).get('teamkit.app-scope')!=scope(state):
                raise ValueError('총괄 이름과 소유권이 일치하지 않습니다.')
            if (target.get('foreground') or {}).get('name')!='codex':
                return {'state':'unknown','terminal':target['address'],'label':'총괄','can_recover':False,
                        'reason':'총괄 자리에 다른 프로그램이나 복구된 쉘이 있습니다. 먼저 확인하고 보존하세요.'}
            return {'state':'live','terminal':target['address'],'label':'총괄','can_recover':False,
                    'agent_state':(target.get('status') or {}).get('state','unknown'),
                    'reason':record.get('reason','GUI 체크포인트를 사용하는 총괄입니다.')}
        terminal=record.get('terminal') or linked.get('coordinator',{}).get('terminal')
        target=next((t for t in live if t['address']==terminal),None)
        if target:
            return {'state':'live','terminal':terminal,'label':'총괄','can_recover':False,
                    'agent_state':target.get('status','unknown'),'reason':record.get('reason','')}
        if record.get('phase') in ('recovering','launching','uncertain'):
            return {'state':'recovering' if record['phase']!='uncertain' else 'unknown',
                    'terminal':None,'label':'총괄','can_recover':False,
                    'reason':record.get('reason','이전 복구 결과를 확인 중입니다. 중복 실행하지 않습니다.')}
        return {'state':'missing','terminal':None,'label':'총괄','can_recover':True,
                'reason':record.get('reason','총괄이 닫혔습니다. GUI 작업 기록에서 새 총괄 대화를 열 수 있습니다.')}
    except (OSError,ValueError,subprocess.SubprocessError,KitError):
        return {'state':'unknown','terminal':None,'label':'총괄','can_recover':False,
                'reason':'core 연결 또는 총괄 소유권을 확인하지 못했습니다. 종료로 추정하지 않습니다.'}


def start(config_path, state, binding_path, confirmed, context_job=None):
    if not confirmed:raise ValueError('GUI 기록에서 총괄을 다시 여는 확인이 필요합니다.')
    state=Path(state).resolve()
    from .context_handoff import jobs as context_jobs, FINISHED
    pending=[j for j in context_jobs(state) if j['target']['kind']=='coordinator' and j['phase'] not in FINISHED]
    if pending and not any(j['id']==context_job and j['phase']=='dispatching' for j in pending):
        return {'message':'총괄 자동 인계를 진행 중입니다. 기존 요청에서 이어갑니다.'}
    lock=os.open(state.with_name('coordinator.lock'),os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        linked=json.loads(Path(binding_path).read_text())
        current=status(state,linked)
        if current['state']=='live':
            return {'message':'이미 실행 중인 GUI 총괄을 사용합니다.','terminal':current['terminal']}
        if not current['can_recover']:raise ValueError(current.get('reason','이전 총괄 복구를 확인하세요.'))
        nonce=uuid.uuid4().hex
        jobs=state.parent/'coordinator-jobs';jobs.mkdir(mode=0o700,exist_ok=True)
        folder=jobs/nonce;folder.mkdir(mode=0o700)
        packet={'config':str(Path(config_path).resolve()),'state':str(state),
                'bindings':str(Path(binding_path).resolve()),'nonce':nonce,
                'config_digest':hashlib.sha256(Path(config_path).read_bytes()).hexdigest()}
        request=folder/'request.json';save(request,packet)
        previous=read_record(state)
        if previous:save(folder/'previous.json',previous)
        record={'phase':'recovering','nonce':nonce,'at':time.time(),
                'reason':'GUI 체크포인트에서 새 총괄을 준비하고 있습니다. 제품 업무는 자동 재개하지 않습니다.'}
        if previous.get('context_checkpoint'):
            record['context_checkpoint']=previous['context_checkpoint']
        for key in ('space', 'tab', 'terminal'):
            if key in previous:
                record[key] = previous[key]
        save(record_path(state),record)
        try:
            child=_call('run','--json','--bg','--no-parent','--no-notify','--name','gui-recover-'+nonce[:12],
                        '--',sys.executable,str(Path(__file__).resolve().parent.parent/'teamkit_app.py'),
                        'coordinator-job','--request',str(request))
            if not isinstance(child.get('address'),str):raise ValueError('복구 controller를 확인하지 못했습니다.')
            record['controller']=child['address'];save(record_path(state),record)
        except BaseException:
            record.update(phase='uncertain',reason='복구 시작 결과가 불명입니다. 새 총괄을 다시 만들기 전에 확인하세요.')
            save(record_path(state),record);raise
        return {'message':'총괄 복구를 시작했습니다. 준비 상태는 운영 바에서 확인하세요.'}
    except BlockingIOError as error:
        raise ValueError('총괄 복구를 이미 처리 중입니다.') from error
    finally:os.close(lock)


def run_job(request):
    packet=json.loads(Path(request).read_text());state=Path(packet['state'])
    require_managed()
    lock=os.open(state.with_name('coordinator.lock'),os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    record=None;launched=False
    try:
        fcntl.flock(lock,fcntl.LOCK_EX)
        record=read_record(state)
        if record.get('nonce')!=packet['nonce'] or record.get('phase')!='recovering':
            raise ValueError('총괄 복구 요청이 바뀌었습니다.')
        if hashlib.sha256(Path(packet['config']).read_bytes()).hexdigest()!=packet['config_digest']:
            raise ValueError('복구할 계정·실행 설정이 바뀌었습니다.')
        config=load(packet['config'])
        from .context_handoff import ensure, settings as context_settings
        if context_settings(config)['enabled']:
            ensure(packet['config'],state)
        policy=config.get('coordinator')
        if config.get('runtime_backend')!='core' or not isinstance(policy,dict) or policy.get('provider')!='codex':
            raise ValueError('GUI 총괄의 core·Codex 정책을 먼저 설정하세요.')
        from .workflow import private_root
        root=Path(policy['root']).resolve()
        if not root.is_dir() or not root.is_relative_to(private_root(config)):
            raise ValueError('GUI 총괄 작업 폴더는 GUI 비공개 기록 안에 있어야 합니다.')
        store=Store(state)
        try:
            account=choose_account(cached_quota(config,store,'codex','team'),cached_quota(config,store,'codex','personal'),time.time(),config['quota_ttl_seconds'], order=account_order(config, time.time()))
        finally:store.close()
        if not account:raise ValueError('최신 구독 사용량을 확인한 뒤 총괄을 열 수 있습니다. 사용량 갱신을 실행하세요.')
        pr=profile(config,'codex',account,policy);providers.identity('codex',pr)
        spec=launch_spec('codex',pr,None)
        spec['env'].update(TEAMKIT_CONFIG=packet['config'],TEAMKIT_STATE=str(state),TEAMKIT_GUI_COORDINATOR=packet['nonce'])
        spec['args']+=['--add-dir',str(state.parent)]
        name='gui-coordinator-'+scope(state)
        linked=json.loads(Path(packet['bindings']).read_text())
        previous_path=Path(request).with_name('previous.json')
        previous=json.loads(previous_path.read_text()) if previous_path.exists() else {}
        sp,tab,new_destination=destination({**previous,**record},linked)
        checkpoints=root/'coordinator-checkpoints';checkpoints.mkdir(mode=0o700,exist_ok=True)
        checkpoint=checkpoints/(packet['nonce']+'.md')
        inventory=Path(linked['inventory'])
        content=('# GUI 총괄 복구 체크포인트\n\nGUI 작업 기록: '+str(state)+
            '\n고정 작업 정본: '+str(inventory)+'\n실행 코드: '+str(Path(__file__).resolve().parent.parent)+
            '\n\nTUI의 작업·승인·소유권을 가져오지 않습니다. 기존 GUI 기록·현재 소스·검증 근거가 재개의 정본입니다. '
            '승인 범위를 새로 추정하거나 제품 업무를 자동 재개하지 마세요. 종료된 대화 전문을 복원했다는 뜻은 아닙니다.\n')
        fd=os.open(checkpoint,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w') as out:out.write(content)
        record.update(phase='launching',account=account,profile_binding=binding(pr),identity_source='host-preflight',
                      model=pr['model'],effort=pr.get('effort','low'),checkpoint=str(checkpoint),space=sp,tab=tab)
        save(record_path(state),record)
        args=['run','--json','--bg','--keep','--no-parent','--no-notify','--name',name,'--cwd',str(root)]
        labels={'herdr.space':sp,'herdr.tab':tab,'teamkit.app-scope':scope(state),'teamkit.kind':'coordinator',
                'teamkit.task':'GUI-COORDINATOR','teamkit.owner':packet['nonce'],'teamkit.name':name}
        for key,value in labels.items():args+=['--label',key+'='+value]
        if hashlib.sha256(Path(packet['config']).read_bytes()).hexdigest()!=packet['config_digest']:
            raise ValueError('총괄 실행 설정이 신원 확인 중 바뀌었습니다.')
        store=Store(state)
        try:
            current=choose_account(cached_quota(config,store,'codex','team'),cached_quota(config,store,'codex','personal'),time.time(),config['quota_ttl_seconds'], order=account_order(config, time.time()))
        finally:store.close()
        if current!=account:
            raise ValueError('총괄 구독 한도가 신원 확인 중 바뀌었습니다. 갱신 후 다시 확인하세요.')
        from . import admission
        resource_attempt = admission.reserve_external(state, 'coordinator:' + packet['nonce'],
                                                      'GUI-COORDINATOR', 'codex/' + account, config)
        if resource_attempt:
            record['resource_attempt'] = resource_attempt
            save(record_path(state), record)
        # Creation may succeed even when its response is lost. Persist uncertainty
        # before crossing the native boundary so a retry cannot duplicate it.
        launched=True
        record['phase']='uncertain';save(record_path(state),record)
        created=_call(*args,'--','env',*(k+'='+v for k,v in spec['env'].items()),'codex',*spec['args'])
        record['phase']='launching';record['terminal']=created['address'];save(record_path(state),record)
        waited=_call('wait','--json','--timeout','30',record['terminal'])
        if waited.get('ended') is not True or waited.get('status') not in ('idle','done'):
            raise ValueError('총괄 터미널은 생성됐지만 준비를 확인하지 못했습니다. 다시 만들지 말고 확인하세요.')
        native=startup_native(record['terminal']);pid=(native.get('foreground') or {}).get('pid')
        runtime={'backend':'core','name':name,'terminal':record['terminal'],'native_pid':pid}
        actor=CoreRuntime({'id':'GUI-COORDINATOR','owner':packet['nonce'],'provider':'codex','runtime':runtime})
        actor._native(name,ready=True)
        admission.bind_external(state, resource_attempt, dict(terminal=record['terminal'], pid=pid,
                                                            owner=packet['nonce']))
        record['native_pid']=pid
        save(record_path(state),record)
        if new_destination:
            for command,values in [('space.rename',{'id':sp,'name':'GUI 총괄'}),('tab.rename',{'id':tab,'name':'총괄'})]:
                subprocess.run(['herdr-app','do',command,json.dumps(values)],capture_output=True,text=True,timeout=5)
        # Read the checkpoint; this is initialization, never a product resume.
        prompt=('GUI 독립 운영 총괄의 복구 확인입니다. '+str(checkpoint)+'와 '+str(inventory)+
                '의 현재 상태, '+str(policy.get('instructions',''))+'의 GUI 지침만 확인하세요. '+
                '제품 코드 조사/수정·작업 재개·다른 agent 생성·기억 전체 조회·외부 게시를 하지 마세요. '+
                'GUI 기록에서 이어갈 준비가 됐는지와 미완료 항목을 짧게 보고하고 사용자 요청을 기다리세요. '+
                '이것은 새 총괄 대화이며 과거 대화 전문 복원이나 제품 구현 승인이 아닙니다.')
        from .session_pool import enabled as pool_enabled
        if pool_enabled(config):
            import shlex
            from .context_handoff import cli as pool_cli
            prompt += (' 팀장 배정은 준비된 세션 풀을 사용하세요. 먼저 '+shlex.join(pool_cli(packet['config'],state,'pool','status'))+
                       '로 네 팀장별 세션과 사용량을 확인하고, pool plan --team 팀ID로 선택을 확인하세요. 승인된 요청 파일은 pool assign --team 팀ID --message-file 경로로 전달하세요. '+
                       '기존 작업·소유권을 덮어쓰지 말고, 인계 예약과 수신 확인을 기다리세요. 준비 세션을 이유로 별도 팀장 대화를 중복 생성하지 마세요.')
        if record.get('context_checkpoint'):
            prompt += (' 자동 컨텍스트 인계 기록 '+record['context_checkpoint']+
                       '의 checkpoint만 추가로 읽고 미완료·승인 대기를 보존하세요. 제품 업무를 자동 재개하지 마세요.')
        from .context_handoff import settings as context_settings, cli as context_cli
        if context_settings(config)['enabled']:
            import shlex
            prompt += ' 먼저 실제 세션 식별자를 등록하세요: '+shlex.join(context_cli(packet['config'],state,'auto-context','register'))
        actor.prompt(name,prompt)
        admission.release_external(state, resource_attempt, writer_stopped=True)
        record.update(phase='ready',native_pid=pid,reason='GUI 체크포인트에서 새 총괄 대화를 열었습니다. 제품 업무는 자동 재개하지 않았습니다.')
        save(record_path(state),record);save(Path(request).with_name('result.json'),{'terminal':record['terminal'],'phase':'ready'})
    except BaseException as error:
        if record and record.get('nonce')==packet['nonce']:
            record.update(phase='uncertain' if launched else 'failed',reason=str(error))
            save(record_path(state),record)
        save(Path(request).with_name('error.json'),{'message':str(error),'terminal':record.get('terminal') if record else None})
        raise
    finally:os.close(lock)
