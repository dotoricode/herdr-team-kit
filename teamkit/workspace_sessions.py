"""Explicit registration of existing workspace sessions; no inherited ownership."""
import json
import hashlib
import os
from pathlib import Path
import shlex
import sys
import time
import uuid

from .config import load, profile, binding, cached_quota
from .core_runtime import _call, _model_effort, _process_matches, _detector_matches, CoreRuntime, require_managed
from .policy import eligible
from .resources import ResourceLedger
from .runtime import AUTH_ENV, RuntimeError as NativeError
from .store import Store
from .tickets import TicketStore
from .staffbootstrap import _process_environment, _namespace_matches
from .prompt_state import empty_input

TABLE = 'workspace_actors'
TTL = 300


def rows(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return []
    return [json.loads(r[0]) for r in db.execute('SELECT body FROM workspace_actors')]


def save(db, row):
    db.execute('CREATE TABLE IF NOT EXISTS workspace_actors (terminal TEXT PRIMARY KEY, body TEXT NOT NULL)')
    db.execute('INSERT OR REPLACE INTO workspace_actors VALUES (?,?)', (row['terminal'], json.dumps(row)))


def space(config, team):
    from .context_handoff import read
    bindings = read(Path(config['session_pool']['bindings'])) or {}
    value = bindings.get('teams', {}).get(team, {}).get('space')
    if not value:
        raise ValueError('team workspace binding is missing')
    return value


def observe(config, state, team, terminal):
    """Verify one local live session without changing labels or task records."""
    native = _call('get', terminal)
    fg, status, labels = native.get('foreground') or {}, native.get('status') or {}, native.get('labels') or {}
    if (native.get('address') != terminal or native.get('state') != 'live'
            or labels.get('herdr.space') != space(config, team)):
        raise ValueError('session is not live in the requested team workspace')
    provider = next((p for p in ('claude', 'codex') if _process_matches(p, fg.get('name'))
                     and _detector_matches(p, status.get('app'))), None)
    if provider is None or type(fg.get('pid')) is not int or fg['pid'] <= 0:
        raise ValueError('workspace process/provider is unverified')
    if status.get('state') not in ('idle', 'done') or type(native.get('turn')) is not int:
        raise ValueError('workspace session is working or its turn is unknown')
    screen = _call('read', '--json', terminal)
    if screen.get('complete') is not True or not empty_input(screen.get('text', '')):
        raise ValueError('workspace session has a draft, dialog or incomplete screen')
    root = config['teams'][team]['root']
    if Path(fg.get('cwd') or native.get('cwd') or '').resolve() != Path(root).resolve():
        raise ValueError('workspace session working root differs from team root')
    model, effort = _model_effort(provider, fg.get('argv'))
    env = _process_environment(fg['pid'])
    if any(env.get(k) for k in AUTH_ENV):
        raise ValueError('workspace session authentication namespace is unverified')
    accounts = [a for a in config['profiles'][provider]
                if _namespace_matches(provider, profile(config, provider, a), env)]
    if len(accounts) != 1:
        raise ValueError('workspace session profile/account is ambiguous')
    account = accounts[0]
    pr = profile(config, provider, account)
    from .providers import identity
    identity(provider, pr)
    store = Store(state, read_only=True)
    try:
        if not eligible(cached_quota(config, store, provider, account), time.time(), config['quota_ttl_seconds']):
            raise ValueError('workspace session quota is missing, expired or limited')
        active = [t for t in store.list() if (t.get('runtime') or {}).get('terminal') == terminal
                  and t.get('state') not in ('ready', 'queued', 'handed_off', 'completed', 'cancelled', 'retired')]
        if active:
            raise ValueError('workspace session has an existing active task owner')
        reservations = ResourceLedger._rows(store.db) if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='resource_reservations' AND type='table'").fetchone() else []
        if any(r['execution_reserved'] and (r.get('session') or {}).get('terminal') == terminal for r in reservations):
            raise ValueError('workspace session has an active execution reservation')
    finally:
        store.close()
    after = _call('get', terminal)
    if any(after.get(k) != native.get(k) for k in ('address', 'state', 'foreground', 'status', 'turn', 'labels')):
        raise ValueError('workspace session changed during verification')
    return dict(terminal=terminal, pid=fg['pid'], provider=provider, account=account,
                model=model, effort=effort, profile_binding=binding(pr), root=root,
                product=team, native_name=native.get('name'), previous_labels=labels,
                turn=native['turn'])


DISCOVERY_REASONS = {
    'session is not live in the requested team workspace': ('workspace', '해당 팀 워크스페이스의 열린 세션으로 확인되지 않았습니다.'),
    'workspace process/provider is unverified': ('process', '실제 프로세스와 실행 도구를 확인하지 못했습니다.'),
    'workspace session is working or its turn is unknown': ('working', '현재 작업 중이거나 턴 종료 여부를 확인하지 못했습니다.'),
    'workspace session has a draft, dialog or incomplete screen': ('input', '입력 초안·확인 창이 있거나 화면 조회가 불완전합니다.'),
    'workspace session working root differs from team root': ('root', '현재 작업 폴더가 팀의 승인 경로와 다릅니다.'),
    'workspace session authentication namespace is unverified': ('authentication', '실행 중인 세션의 인증 경로를 확인하지 못했습니다.'),
    'workspace session profile/account is ambiguous': ('profile', '세션의 프로필·계정을 하나로 확인하지 못했습니다.'),
    'workspace session quota is missing, expired or limited': ('quota', '최신 사용량이 없거나 만료됐거나 사용 한도에 도달했습니다.'),
    'workspace session has an existing active task owner': ('active_task', '기존 업무의 실행 소유권을 먼저 확인해야 합니다.'),
    'workspace session has an active execution reservation': ('active_reservation', '이 세션에 기존 실행 예약이 남아 있습니다.'),
    'workspace session changed during verification': ('changed', '확인 도중 세션·프로세스·입력 상태가 바뀌었습니다.'),
}


def discovery_failure(error):
    code, reason = DISCOVERY_REASONS.get(str(error),
        ('observation_failed', '공식 신원·실행 관측을 확인하지 못했습니다. 운영본부의 재확인이 필요합니다.'))
    return dict(reason_code=code, reason=reason, error_type=type(error).__name__)


def discover(config, state, team):
    workspace = space(config, team)
    terminals = _call('ls').get('terminals', [])
    result = []
    for item in terminals:
        address = item.get('address')
        if not isinstance(address, str) or '/' in address:
            continue
        native = _call('get', address)
        if (native.get('labels') or {}).get('herdr.space') != workspace:
            continue
        try:
            result.append(dict(observe(config, state, team, address), registration_required=True))
        except (ValueError, RuntimeError, NativeError, OSError) as error:
            # Fixed categories only: provider errors may contain credentials or paths.
            result.append(dict(terminal=address, registration_required=True, **discovery_failure(error)))
    return result


def register(config_path, state, team, terminal, approved=False):
    require_managed()
    from .context_handoff import locked, directory
    # Legacy dispatch saves its receiver under this same lock before transport.
    with locked(directory(state)/'model-start'):
        return _register(config_path, state, team, terminal, approved)


def _register(config_path, state, team, terminal, approved=False):
    require_managed()
    if approved is not True:
        raise ValueError('explicit operator approval required for workspace registration')
    config = load(config_path)
    proof = observe(config, state, team, terminal)
    from .pool_control import requests
    if any((job.get('receiver') or {}).get('terminal') == terminal
           and job['phase'] not in ('pending', 'completed', 'deferred', 'cancelled') for job in requests(state)):
        raise ValueError('workspace session has a live or unconfirmed legacy delivery')
    queue = TicketStore(state)
    try:
        with queue.transaction():
            previous = next((r for r in rows(queue.db) if r['terminal'] == terminal), None)
            if previous and previous['phase'] != 'expired_before_delivery':
                raise ValueError('workspace registration already exists; inspect original receipt, never resend')
            held = ResourceLedger._rows(queue.db) if queue.db.execute("SELECT 1 FROM sqlite_master WHERE name='resource_reservations'").fetchone() else []
            if any(r['execution_reserved'] and (r.get('session') or {}).get('terminal') == terminal for r in held):
                raise ValueError('workspace registration conflicts with an active reservation')
            owner, nonce = uuid.uuid4().hex, uuid.uuid4().hex
            task = 'WORKSPACE-' + owner
            actor = {k:proof[k] for k in ('terminal', 'pid', 'provider', 'account', 'model', 'effort', 'profile_binding', 'root', 'product')}
            actor.update(owner=owner, task=task, workspace_registered=True, roles=['leader', 'investigator', 'implementer', 'reviewer'],
                         runtime=dict(backend='core', name=task, native_name=proof['native_name'], terminal=terminal,
                                      native_pid=proof['pid'], session_cwd=proof['root']))
            record = dict(terminal=terminal, actor=actor, phase='registering', nonce=nonce,
                          created_at=time.time(), expires_at=time.time()+TTL, proof=proof,
                          history=(previous.get('history', [])+[{k:v for k,v in previous.items() if k!='history'}]) if previous else [])
            save(queue.db, record)
        # Durable intent precedes label/transport changes. No retry after partial failure.
        latest = observe(config, state, team, terminal)
        if latest != proof:
            raise ValueError('workspace changed before registration')
        if time.time() > record['expires_at']:
            with queue.transaction():
                record['phase']='expired_before_delivery';save(queue.db,record)
            raise ValueError('workspace registration expired before delivery')
        _call('label', terminal, 'teamkit.owner='+owner, 'teamkit.task='+task, 'teamkit.name='+task)
        receipt = [sys.executable, str(Path(__file__).resolve().parent.parent/'teamkit_cli.py'),
                   '--config', str(config_path), '--state', str(state), 'pool', 'workspace-receipt',
                   '--team', team, '--terminal', terminal, '--expected-owner', owner, '--delivery-id', nonce]
        text = ('Workspace registration only. Do not start product work or alter existing drafts, tasks or files. '
                'Confirm this new registration with the exact command below, then wait. '
                'Previous task ownership is not inherited.\n'+shlex.join(receipt))
        runtime = CoreRuntime(dict(id=task, owner=owner, provider=proof['provider'], runtime=actor['runtime']))
        def guard():
            if time.time() > record['expires_at']:
                with queue.transaction():
                    record['phase']='expired_before_delivery';save(queue.db,record)
                raise ValueError('workspace registration expired before delivery')
            current = runtime._native(task, ready=True)
            screen = _call('read', '--json', terminal)
            if (current.get('turn') != proof['turn'] or screen.get('complete') is not True
                    or not empty_input(screen.get('text', ''))):
                raise ValueError('workspace process/turn/input changed before registration delivery')
        guard()
        accepted = runtime.accept(task, text, before_send=guard)
        with queue.transaction():
            fresh = next(r for r in rows(queue.db) if r['terminal'] == terminal)
            if fresh['phase'] == 'registering':
                fresh.update(phase='awaiting_receipt', delivery=accepted)
                save(queue.db, fresh)
        return {k:v for k,v in fresh.items() if k not in ('nonce','proof')}
    finally:
        queue.close()


def receipt(config_path, state, terminal, owner, nonce):
    require_managed()
    if os.environ.get('HC_TERMINAL_ID') != terminal:
        raise ValueError('workspace receipt must come from its own terminal')
    config = load(config_path)
    queue = TicketStore(state)
    try:
        with queue.transaction():
            record = next((r for r in rows(queue.db) if r['terminal'] == terminal), None)
            if (not record or record['actor']['owner'] != owner or record['nonce'] != nonce
                    or record['phase'] not in ('registering', 'awaiting_receipt') or time.time() > record['expires_at']):
                raise ValueError('workspace receipt is missing, expired or already consumed')
            actor = record['actor']
            native = CoreRuntime(dict(id=actor['task'], owner=owner, provider=actor['provider'], runtime=actor['runtime']))._native(actor['task'])
            env = _process_environment(actor['pid'])
            if (binding(profile(config, actor['provider'], actor['account'])) != actor['profile_binding']
                    or not _namespace_matches(actor['provider'], profile(config, actor['provider'], actor['account']), env)
                    or any(env.get(k) for k in AUTH_ENV)
                    or _model_effort(actor['provider'], native['foreground'].get('argv')) != (actor['model'], actor['effort'])
                    or (native.get('labels') or {}).get('herdr.space') != space(config, actor['product'])
                    or native.get('turn', -1) <= record['proof']['turn']):
                raise ValueError('workspace receipt profile/model/workspace/turn changed')
            store=object.__new__(Store);store.db=queue.db;store.path=Path(state)
            if queue.db.execute('SELECT 1 FROM tasks WHERE id=?',(actor['task'],)).fetchone():
                raise ValueError('workspace actor task already exists')
            runtime=dict(actor['runtime'],identity_checked=True,profile_binding=actor['profile_binding'])
            actor['runtime']=runtime
            value=dict(id=actor['task'],team=actor['product'],role='leader',root=actor['root'],
                base=hashlib.sha256(('workspace-registration:'+actor['root']).encode()).hexdigest(),
                basis_kind='instructions',goal='Receive approved workspace assignments under this fresh registration.',
                state='ready',owner=owner,attempt_count=1,provider=actor['provider'],account=actor['account'],
                model=actor['model'],effort=actor['effort'],runtime=runtime,
                workspace_registered=True,workspace_product=actor['product'])
            store._save(value,'workspace_receipted',{'terminal':terminal,'pid':actor['pid']})
            record.update(phase='ready', received_at=time.time())
            save(queue.db, record)
            return dict(terminal=terminal, phase='ready', owner=owner)
    finally:
        queue.close()


def executors(state):
    queue = TicketStore(state, read_only=True)
    try:
        return [r['actor'] for r in rows(queue.db) if r['phase'] == 'ready']
    finally:
        queue.close()


def verify_actor(config, actor, native):
    """Recheck mutable workspace/process namespace at every admission and send fence."""
    if (native.get('labels') or {}).get('herdr.space') != space(config, actor['product']):
        raise ValueError('registered workspace membership changed')
    foreground = native.get('foreground') or {}
    cwd = foreground.get('cwd') or native.get('cwd')
    if not cwd or Path(cwd).resolve() != Path(actor['root']).resolve():
        raise ValueError('registered workspace root changed')
    pr = profile(config, actor['provider'], actor['account'])
    env = _process_environment(actor['pid'])
    if (binding(pr) != actor['profile_binding'] or not _namespace_matches(actor['provider'], pr, env)
            or any(env.get(k) for k in AUTH_ENV)
            or _model_effort(actor['provider'], foreground.get('argv')) != (actor['model'], actor['effort'])):
        raise ValueError('registered workspace profile/model changed')


def registration_fence(db, terminal, owner):
    record = next((r for r in rows(db) if r['terminal'] == terminal), None)
    if record and (record['phase'] != 'ready' or record['actor']['owner'] != owner):
        raise ValueError('workspace registration owns the session; previous owner cannot dispatch')


def plan(config, state, team):
    """Use a receipted current workspace actor without inheriting the canonical owner."""
    if config.get('operations',{}).get('allocation_policy') != 'usage':return None
    store=Store(state,read_only=True)
    try:
        for actor in executors(state):
            if actor['product']!=team:continue
            task=store.get(actor['task'])
            if task['state']!='ready' or task.get('owner')!=actor['owner']:continue
            try:
                proof=observe(config,state,team,actor['terminal'])
                native=CoreRuntime(dict(id=actor['task'],owner=actor['owner'],provider=actor['provider'],runtime=actor['runtime']))._native(actor['task'],ready=True)
                verify_actor(config,actor,native)
                if proof['pid']!=actor['pid']:continue
            except (ValueError,RuntimeError,NativeError,OSError):continue
            selected={k:actor[k] for k in ('provider','account','model','effort')};selected['action']='dispatch'
            return dict(team=team,task=actor['task'],selection=selected,state='ready',owner=actor['owner'],
                        attempt=task['attempt_count'],terminal=actor['terminal'],action='tell',workspace=True)
    finally:store.close()
    return None


def verify_task(config,state,task):
    queue=TicketStore(state,read_only=True)
    try:
        record=next((r for r in rows(queue.db) if r['actor']['task']==task['id'] and r['phase']=='ready'),None)
    finally:queue.close()
    if not record or record['actor']['owner']!=task['owner']:
        raise ValueError('workspace registration or owner changed')
    actor=record['actor']
    native=CoreRuntime(task)._native(actor['task'],ready=True)
    verify_actor(config,actor,native)
    from .providers import identity
    identity(actor['provider'],profile(config,actor['provider'],actor['account']))
    store=Store(state,read_only=True)
    try:
        if not eligible(cached_quota(config,store,actor['provider'],actor['account']),time.time(),config['quota_ttl_seconds']):
            raise ValueError('workspace quota changed before delivery')
    finally:store.close()
    screen=_call('read','--json',actor['terminal'])
    if screen.get('complete') is not True or not empty_input(screen.get('text','')):
        raise ValueError('workspace input changed before delivery')
