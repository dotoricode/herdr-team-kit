"""Opt-in, durable context rollover. Native uncertainty never authorizes a retry."""
import fcntl
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager, closing, nullcontext
from concurrent.futures import ThreadPoolExecutor

from .config import binding, load, profile, cached_quota
from .context_observation import context_percent, codex_observation
from .core_runtime import CoreRuntime, _call, _process_matches
from .policy import account_order, choose_account, number
from .runtime import profile_home_matches
from .store import Store, ACTIVE

def source_fingerprint():
    digest=hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob('*.py')):
        digest.update(path.name.encode());digest.update(path.read_bytes())
    return digest.hexdigest()


LOADED_SOURCE_FINGERPRINT=source_fingerprint()


TERMINAL = re.compile(r't[0-9a-z]+\Z')
SESSION = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
JOB = re.compile(r'[a-f0-9]{32}\Z')
FINISHED = {'done', 'uncertain', 'cancelled'}


@contextmanager
def locked(path):
    fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def settings(config):
    value = config.get('context_handoff', {'enabled': False, 'threshold': 90, 'interval': 10})
    if (not isinstance(value, dict) or set(value) != {'enabled', 'threshold', 'interval'}
            or type(value['enabled']) is not bool or value['threshold'] != 90
            or type(value['interval']) is not int or not 5 <= value['interval'] <= 60):
        raise ValueError('context_handoff requires enabled, threshold=90 and interval=5..60')
    return value


def directory(state):
    path = Path(state).resolve().with_name('context-handoffs')
    path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink():
        raise ValueError('context storage must not be a symlink')
    return path


def read(path):
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 256 * 1024:
        raise ValueError('invalid context record')
    result = json.loads(path.read_text())
    if not isinstance(result, dict):
        raise ValueError('invalid context record')
    return result


def write(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.context-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def job_path(state, nonce):
    if not isinstance(nonce, str) or not JOB.fullmatch(nonce):
        raise ValueError('invalid context job')
    return directory(state) / (nonce + '.json')


def targets(config, state, starting=False):
    path=Path(state).resolve()
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        if db.execute('PRAGMA journal_mode').fetchone()[0] != 'delete':
            raise ValueError('context supervision requires rollback journal mode')
        task_rows=[json.loads(row[0]) for row in db.execute('SELECT body FROM tasks')]
        result = []
        for task in task_rows:
            from . import session_pool
            from .team_dispatch import settings as dispatch_settings
            dispatch=dispatch_settings(config)
            observer=(dispatch['enabled'] and dispatch['tasks'].get(task['team'])==task['id']
                      and task.get('team_dispatch_team')==task['team']
                      and task.get('execution_constraint')=={'provider':'codex','account':'personal'})
            if (session_pool.enabled(config) and task.get('team') in config['teams']
                    and task.get('role')=='leader' and not task.get('work_id')
                    and task.get('classification',{}).get('kind','general')=='general'
                    and task['id']!=session_pool.task_id(config,task['team']) and not observer):
                continue
            runtime = task.get('runtime', {})
            if (task['state'] not in ('running', 'ready') or not task.get('owner')
                    or runtime.get('backend') != 'core' or runtime.get('identity_checked') is not True):
                continue
            result.append({'kind':'task', 'id':task['id'], 'owner':task['owner'],
                'provider':task['provider'], 'account':task['account'], 'root':task['root'],
                'terminal':runtime['terminal'], 'pid':runtime.get('native_pid'),
                'session_cwd':runtime.get('session_cwd',task['root']),
                'binding':binding(profile(config, task['provider'], task['account'])),
                'session_id':runtime.get('context_session_id'), 'was_ready':task['state'] == 'ready',
                'empty_prompt':runtime.get('pool_empty_prompt')})
    from .app_coordinator import read_record
    record = read_record(state)
    if (record.get('phase') == 'ready' or starting and record.get('phase') == 'launching') and record.get('terminal') and record.get('native_pid'):
        result.append({'kind':'coordinator', 'id':'COORDINATOR', 'owner':record['nonce'],
            'provider':config['coordinator']['provider'], 'account':record['account'],
            'root':config['coordinator']['root'], 'terminal':record['terminal'],
            'binding':binding(profile(config, config['coordinator']['provider'], record['account'])),
            'pid':record['native_pid'], 'session_id':record.get('context_session_id'), 'was_ready':True})
    return result


def native(target, state):
    if target['kind'] == 'task':
        store = Store(state)
        try:
            task = store.get(target['id'])
        finally:
            store.close()
        if task.get('owner') != target['owner'] or task.get('runtime', {}).get('terminal') != target['terminal'] or task.get('runtime', {}).get('native_pid') != target['pid']:
            raise ValueError('context owner changed')
        return CoreRuntime(task)._native(task['runtime']['name'])
    from .app_coordinator import read_record
    record = read_record(state)
    value = _call('get', target['terminal'])
    foreground = value.get('foreground') or {}
    if (record.get('nonce') != target['owner'] or record.get('terminal') != target['terminal']
            or record.get('native_pid') != target['pid'] or value.get('state') != 'live'
            or foreground.get('pid') != target['pid']
            or not _process_matches(target['provider'], foreground.get('name'))
            or value.get('labels', {}).get('teamkit.owner') != target['owner']):
        raise ValueError('coordinator native ownership changed')
    return value


def current_target(config, state, expected):
    return next((t for t in targets(config, state) if all(t.get(k) == expected.get(k)
        for k in ('kind', 'id', 'owner', 'terminal', 'pid', 'provider', 'account', 'binding', 'root'))), None)


def register(config, state, session_id=None):
    """Runs in the actual model tool environment, not a fabricated pane."""
    address = os.environ.get('HC_TERMINAL_ID', '')
    session_id = session_id or os.environ.get('CODEX_THREAD_ID', '')
    if not TERMINAL.fullmatch(address) or not SESSION.fullmatch(session_id):
        raise ValueError('native terminal and session identity required')
    candidates = [t for t in targets(config, state, starting=True) if t['terminal'] == address]
    if len(candidates) != 1:
        raise ValueError('managed context target unavailable')
    target = candidates[0]
    if target['provider'] != 'codex' or not profile_home_matches('codex', profile(config, 'codex', target['account'])['home']):
        raise ValueError('context profile mismatch')
    native(target, state)
    save_registration(state, target, session_id)
    return {'registered':True, 'terminal':address}


def save_registration(state, target, session_id):
    if not SESSION.fullmatch(session_id):
        raise ValueError('invalid native session')
    if target['kind'] == 'task':
        store = Store(state)
        try:
            with store.transaction():
                value = store.get(target['id'])
                if value.get('owner') != target['owner'] or value['runtime'].get('native_pid') != target['pid']:
                    raise ValueError('context registration owner changed')
                value['runtime']['context_session_id'] = session_id
                store._save(value, 'context_registered', {'session_id':session_id})
        finally:
            store.close()
    else:
        from .app_coordinator import read_record, record_path
        value = read_record(state)
        if value.get('nonce') != target['owner'] or value.get('native_pid') != target['pid']:
            raise ValueError('coordinator changed')
        value['context_session_id'] = session_id
        write(record_path(state), value)


def capture_claude(config, state, account, raw):
    address,owner=os.environ.get('HC_TERMINAL_ID',''),os.environ.get('TEAMKIT_OWNER','')
    if not settings(config)['enabled'] or not TERMINAL.fullmatch(address) or not owner:
        return
    with locked(directory(state)/('observation-'+address+'.json')):
        return _capture_claude(config,state,account,raw)


def _capture_claude(config, state, account, raw):
    if not settings(config)['enabled'] or config.get('runtime_backend') != 'core':
        return
    from .statusline import observation
    pr = profile(config, 'claude', account)
    if not profile_home_matches('claude', pr['home']):
        return
    address, owner = os.environ.get('HC_TERMINAL_ID', ''), os.environ.get('TEAMKIT_OWNER', '')
    if not TERMINAL.fullmatch(address) or not owner:
        return
    path=directory(state)/('observation-'+address+'.json')
    previous=read(path)
    session=raw.get('session_id') if isinstance(raw,dict) else None
    if previous and previous.get('owner')==owner and isinstance(session,str) and SESSION.fullmatch(session) and previous.get('session_id')!=session:
        write(path,{'terminal':address,'owner':owner,'session_id':session,'used':None,
                    'binding':binding(pr),'source':'claude-statusline-response','unavailable':True})
    value = observation(raw, pr, time.time(), config['quota_ttl_seconds'])
    used = context_percent(raw)
    if value is None or used is None:
        return
    packet = {'terminal':address, 'owner':owner, 'session_id':raw['session_id'],
              'used':used, 'observed_at':value['observed_at'], 'source':'claude-statusline-response',
              'binding':binding(pr)}
    path = directory(state) / ('observation-' + address + '.json')
    old = read(path)
    if old and old.get('owner') == owner and old.get('session_id') == packet['session_id']:
        if old.get('observed_at', 0) > packet['observed_at']:
            return
        if number(old.get('used')) and old['used'] >= 90 and 0 <= time.time()-old.get('observed_at', 0) <= 120 and packet['used'] < 90:
            return  # Keep the actual crossing through fast native compaction.
    write(path, packet)


def observe(config, state, target):
    pr = profile(config, target['provider'], target['account'])
    if target['provider'] == 'codex':
        return codex_observation(pr['home'], target.get('session_id'), target.get('session_cwd',target['root']), since=time.time()-120)
    result = read(directory(state) / ('observation-' + target['terminal'] + '.json'))
    if not result or result.get('source') != 'claude-statusline-response' or result.get('owner') != target['owner'] or result.get('binding') != binding(pr):
        return None
    return result


def clean_prompt(value, target):
    if (value.get('status') or {}).get('state') not in ('idle', 'done'):
        return False
    text = subprocess.run(['hc', 'read', '--lines', '24', target['terminal']],
                          capture_output=True, text=True, timeout=5, check=True).stdout
    from .prompt_state import empty_input
    return empty_input(text,target.get('empty_prompt'))


def cli(config_path, state, *args):
    return [sys.executable, str(Path(__file__).resolve().parent.parent / 'teamkit_cli.py'),
            '--config', str(config_path), '--state', str(state), *args]


def checkpoint(config_path, state, nonce, evidence_file, stopped):
    with locked(job_path(state, nonce)):
        return _checkpoint(config_path, state, nonce, evidence_file, stopped)


def _checkpoint(config_path, state, nonce, evidence_file, stopped):
    path = job_path(state, nonce)
    job = read(path)
    if not job or job['phase'] not in ('requesting', 'requested') or not stopped:
        raise ValueError('context checkpoint is not requested')
    target = current_target(load(config_path), state, job['target'])
    if target is None or os.environ.get('HC_TERMINAL_ID') != target['terminal']:
        raise ValueError('checkpoint requires the original managed terminal')
    if target['kind'] == 'task' and os.environ.get('TEAMKIT_OWNER') != target['owner']:
        raise ValueError('checkpoint requires the current owner')
    if target['kind'] == 'coordinator' and os.environ.get('TEAMKIT_GUI_COORDINATOR') != target['owner']:
        raise ValueError('checkpoint requires the current coordinator')
    native(target, state)
    actual_session=None
    if target['provider'] == 'codex':
        actual_session=os.environ.get('CODEX_THREAD_ID')
        if not isinstance(actual_session,str) or not SESSION.fullmatch(actual_session):
            raise ValueError('checkpoint requires native session identity')
        if job.get('trigger') and job['trigger'].get('metric','context')=='context' and actual_session != job['trigger']['session_id']:
            if actual_session and SESSION.fullmatch(actual_session):
                save_registration(state, target, actual_session)
            set_phase(state,job,'cancelled','새 대화가 열려 이전 인계를 취소했습니다.')
            raise ValueError('native thread changed since context observation; do not exit')
        save_registration(state,target,actual_session)
    if target['provider'] == 'claude':
        current=observe(load(config_path),state,target)
        actual_session=(current or {}).get('session_id')
        if not isinstance(actual_session,str) or not SESSION.fullmatch(actual_session):
            raise ValueError('checkpoint requires native Claude session observation')
        if job.get('trigger') and job['trigger'].get('metric','context')=='context' and actual_session != job['trigger']['session_id']:
            set_phase(state,job,'cancelled','새 대화가 열려 이전 인계를 취소했습니다.')
            raise ValueError('native Claude session changed since observation')
        reported_session=os.environ.get('CLAUDE_SESSION_ID')
        if reported_session and reported_session != actual_session:
            raise ValueError('native Claude session changed since observation')
    expected = directory(state) / (nonce + '-evidence.json')
    if Path(evidence_file).absolute() != expected:
        raise ValueError('checkpoint evidence must use the job path')
    evidence = read(expected)
    required = {'verification', 'next', 'pending_approvals', 'background_writers_stopped'}
    if (not evidence or set(evidence) != required or evidence['background_writers_stopped'] is not True
            or not isinstance(evidence['next'], str) or not evidence['next'].strip()
            or not isinstance(evidence['verification'], list) or not evidence['verification']
            or not all(isinstance(s, str) and s.strip() for s in evidence['verification'])
            or not isinstance(evidence['pending_approvals'], list)
            or not all(isinstance(s, str) for s in evidence['pending_approvals'])):
        raise ValueError('complete checkpoint and stopped background writers required')
    job['checkpoint_session_id']=actual_session
    return save_checkpoint(load(config_path),state,job,evidence,'agent')


def exit_session_matches(config,state,job):
    target=current_target(config,state,job['target'])
    expected=job.get('checkpoint_session_id')
    if target is None or not isinstance(expected,str) or not SESSION.fullmatch(expected):
        return False
    if target['provider']=='claude':
        packet=observe(config,state,target)
        if not packet or packet.get('session_id')!=expected or packet.get('unavailable'):return False
    elif target.get('session_id')!=expected:return False
    # Native /status is local UI state; it cannot start a model turn.
    def screen():
        return subprocess.run(['hc','read','--lines','60',target['terminal']],capture_output=True,text=True,timeout=5,check=True).stdout.splitlines()
    before=screen()
    _call('send',target['terminal'],'/status')
    after=screen()
    changed=[]
    for tag,_,__,start,end in difflib.SequenceMatcher(a=before,b=after,autojunk=False).get_opcodes():
        if tag in ('insert','replace'):changed.extend(after[start:end])
    label='Session ID' if target['provider']=='claude' else 'Session'
    found=re.findall(r'^\s*'+label+r':\s+([a-f0-9-]{36})\s*$','\n'.join(changed),re.M)
    if target['provider']=='claude':
        full='\n'.join(after)
        if not re.search(r'^\s*Settings\s+Status\s+Config',full,re.M) or 'Esc to cancel' not in full:return False
        _call('keys',target['terminal'],'escape')
    if not found or found[-1]!=expected:return False
    latest=current_target(config,state,target)
    return bool(latest and (target['provider']=='claude' or latest.get('session_id')==expected))


def save_checkpoint(config,state,job,evidence,source):
    target=job['target'];nonce=job['id']
    path=job_path(state,nonce)
    if target['kind'] == 'task':
        from .__main__ import task_head, task_fingerprint
        store = Store(state)
        try:
            task = store.get(target['id'])
            evidence['head'] = task_head(task, config)
            evidence['worktree_fingerprint'] = task_fingerprint(task, config)
        finally:
            store.close()
    if target['kind']=='task':
        write(directory(state)/(nonce+'-handoff.json'),
              {'verification':evidence['verification'],'next':evidence['next'],
               'context_continuation':{'was_ready':job['target']['was_ready'],
                                       'pending_approvals':evidence['pending_approvals']}})
    job.update(checkpoint=evidence, checkpoint_source=source, phase='checkpoint_ready', updated_at=time.time())
    write(path, job)
    return {'checkpoint':'saved', 'next':'Return without further tools; supervisor verifies exit before releasing ownership.'}


def jobs(state):
    root=Path(state).resolve().with_name('context-handoffs')
    if not root.exists():
        return []
    if root.is_symlink():
        raise ValueError('context storage must not be a symlink')
    return [read(p) for p in sorted(root.glob('*.json')) if JOB.fullmatch(p.stem)]


def request(config, state, target, observation, manual=False, policy_rotation=False, pool_request=None):
    with locked(directory(state) / 'requests'):
        return _request(config, state, target, observation, manual, policy_rotation, pool_request)


def _request(config, state, target, observation, manual=False, policy_rotation=False, pool_request=None):
    """manual is reserved for explicit isolated lifecycle verification."""
    if not settings(config)['enabled']:
        raise ValueError('context handoff disabled')
    if manual and (target['kind'] != 'task' or not target['id'].startswith('CONTEXT-SMOKE-')
                   or not Path(target['root']).resolve().is_relative_to(Path(config['private_plan_root']).resolve())):
        raise ValueError('manual rehearsal is limited to an isolated context smoke task')
    if policy_rotation:
        from . import session_pool
        store=Store(state)
        try:task=store.get(target['id'])
        finally:store.close()
        if (manual or not session_pool.managed(config,task) or task['state']!='ready'
                or task.get('owner')!=target['owner'] or task.get('runtime',{}).get('terminal')!=target['terminal']):
            raise ValueError('policy rotation requires the current idle pool leader')
        from .handoff_trigger import trigger
        store=Store(state)
        try:observation=trigger(config,store,target,observe(config,state,target),time.time())
        finally:store.close()
    metric=(observation or {}).get('metric','context')
    if not manual and not policy_rotation:
        valid=(isinstance(observation,dict) and number(observation.get('observed_at'))
               and 0 <= time.time()-observation['observed_at'] <= 120)
        if metric=='account_blocked':
            valid=valid and observation.get('blocked') is True
        else:
            valid=valid and number(observation.get('used')) and 90 <= observation['used'] <= 100
        if metric=='context':
            valid=valid and isinstance(observation.get('session_id'),str) and SESSION.fullmatch(observation['session_id'])
        if not valid:return None
    for previous in jobs(state):
        if previous['target']['owner']!=target['owner'] or previous['target']['terminal']!=target['terminal']:
            continue
        with locked(job_path(state,previous['id'])):
            previous=read(job_path(state,previous['id']))
            # Only a new durable explicit pool request may bypass an unsent cancellation.
            if (policy_rotation and pool_request and JOB.fullmatch(pool_request)
                    and previous['phase']=='cancelled' and previous.get('policy_rotation')
                    and not previous.get('checkpoint') and not previous.get('pool_request')==pool_request):
                assignment=read(Path(state).resolve().parent/'pool-requests'/(pool_request+'.json'))
                store=Store(state)
                try:
                    current=store.get(target['id'])
                    unsent=not any(e['event']=='woken' and e['at']>=previous['at'] for e in store.events(target['id']))
                finally:store.close()
                if (assignment and assignment.get('task')==target['id'] and assignment.get('phase')=='pending'
                        and current['state']=='ready' and current.get('owner')==target['owner'] and unsent):
                    continue
            changed_thread=(metric=='context' and (previous.get('trigger') or {}).get('metric','context')=='context'
                            and (previous.get('trigger') or {}).get('session_id')!=(observation or {}).get('session_id'))
            if changed_thread and previous['phase'] in ('pending','cancelled'):
                set_phase(state,previous,'cancelled','새 대화의 컨텍스트를 별도로 확인합니다.')
            else:return None
    nonce = uuid.uuid4().hex
    job = {'id':nonce, 'phase':'pending', 'target':target, 'trigger':observation,
           'verification_only':manual, 'policy_rotation':policy_rotation, 'pool_request':pool_request, 'at':time.time(), 'updated_at':time.time()}
    update_next_session(config,state,job)
    write(job_path(state, nonce), job)
    return job


def set_phase(state, job, phase, reason=''):
    job.update(phase=phase, reason=reason, updated_at=time.time())
    write(job_path(state, job['id']), job)


def checkpoint_prompt(config_path, state, job):
    nonce = job['id']
    evidence = directory(state) / (nonce + '-evidence.json')
    command = shlex.join(cli(config_path, state, 'auto-context', 'checkpoint', '--job', nonce,
                             '--evidence-file', str(evidence), '--writer-stopped'))
    metric=(job.get('trigger') or {}).get('metric','context')
    reason=('Explicit isolated lifecycle rehearsal (not a measured 90% crossing). ' if job['verification_only'] else
            ('Policy-based idle leader handoff. ' if job.get('policy_rotation') else
             'Context90% automatic handoff. ' if metric=='context' else 'Subscription limit handoff ('+metric+'). '))
    reason+=' Planned next receiver (rechecked before activation): '+json.dumps(job.get('next_session'),ensure_ascii=False)+'. '
    return (reason + 'Stop starting product work; finish or stop any background writers safely. '
            'Save a concise handoff as JSON at ' + str(evidence) + ' with exactly these keys: '
            'verification (nonempty string list), next (remaining approved work or standby), '
            'pending_approvals (string list; never infer approval), background_writers_stopped (true only after checking). '
            'Do not approve or expand work, do not start agents. Then run ' + command +
            '. After success, report briefly and return without any further tool calls. Do not exit yourself.')


def advance(config_path, state, job):
    with locked(job_path(state, job['id'])):
        _advance(config_path, state, read(job_path(state, job['id'])))


def _advance(config_path, state, job):
    config = load(config_path)
    if not settings(config)['enabled'] or job['phase'] in FINISHED:
        return
    target = job['target']
    try:
        if job['phase'] in ('pending', 'requesting', 'requested', 'checkpoint_ready'):
            if current_target(config, state, target) is None:
                return set_phase(state, job, 'cancelled', '담당 소유권이 바뀌어 인계를 취소했습니다.')
            value = native(target, state)
            if job['phase'] == 'requesting':
                return set_phase(state, job, 'uncertain', '인계 요청 전송 결과를 확인해야 합니다. 중복 전송하지 않습니다.')
            if job['phase'] == 'pending':
                if not clean_prompt(value, target):
                    job['needs_you'] = (value.get('status') or {}).get('state') in ('idle','done','blocked')
                    return set_phase(state, job, 'pending', '입력·확인 창을 정리해 주세요.' if job['needs_you'] else '현재 작업이 끝나면 인계합니다. 입력 초안은 보존합니다.')
                job['needs_you'] = False
                trigger=job.get('trigger') or {}
                if trigger.get('metric')=='account_blocked' or trigger.get('metric') in ('five_hour','weekly') and trigger.get('used')==100:
                    job['needs_you']=True
                    return set_phase(state,job,'pending','한도가 소진되어 인계 기록을 작성할 수 없습니다. 마지막 작업과 쓰기 중단 확인이 필요합니다.')
                if target['provider']=='claude' and job.get('trigger') and job['trigger'].get('metric','context')=='context':
                    measured=observe(config,state,target)
                    if measured and measured.get('session_id') != job['trigger']['session_id']:
                        return set_phase(state,job,'cancelled','새 대화가 열려 이전 컨텍스트 인계를 취소했습니다.')
                with locked(directory(state)/'model-start'):
                    if not capacity_available(state,target,job['id']):
                        return set_phase(state,job,'pending','동시 실행 여유가 생기면 인계 기록을 작성합니다.')
                    set_phase(state,job,'requesting')
                    if target['kind']=='task':
                        store=Store(state)
                        try:
                            task=store.get(target['id'])
                            if task['state']=='ready':store.wake(target['id'],target['owner'],approval_config=config)
                        finally:store.close()
                    _call('send',target['terminal'],checkpoint_prompt(config_path,state,job))
                return set_phase(state,job,'requested','기존 담당이 인계 기록을 작성하고 있습니다.')
            if job['phase'] == 'requested':
                if time.time() - job['updated_at'] > 300:
                    return set_phase(state, job, 'uncertain', '인계 기록 확인이 필요합니다. 재전송하지 않습니다.')
                return
            if not clean_prompt(value, target):
                return
            if not exit_session_matches(config,state,job):
                return set_phase(state,job,'uncertain','인계 기록 이후 대화가 변경됐거나 현재 대화를 확인할 수 없습니다. 기존 실행을 보존했습니다.')
            if not clean_prompt(native(target,state),target):return
            set_phase(state, job, 'stopping', '인계 기록을 저장했습니다. 기존 실행 종료를 확인합니다.')
            _call('send', target['terminal'], '/exit')
            return
        if job['phase'] == 'stopping':
            value = _call('get', target['terminal'])
            if value.get('state') != 'finished':
                if time.time() - job['updated_at'] > 30:
                    return set_phase(state, job, 'uncertain', '기존 실행 종료를 확인하지 못했습니다. 새 담당을 시작하지 않습니다.')
                return
            if value.get('exit_code') != 0 or value.get('signal') is not None:
                return set_phase(state, job, 'uncertain', '기존 담당이 정상 종료되지 않았습니다.')
            if value.get('pid') != target['pid'] or value.get('labels',{}).get('teamkit.owner') != target['owner']:
                return set_phase(state,job,'uncertain','종료된 실행의 소유권이 일치하지 않습니다.')
            if target['kind'] == 'task':
                from .__main__ import task_head, task_fingerprint
                store = Store(state)
                try:
                    task = store.get(target['id'])
                    cp = job['checkpoint']
                    if task_head(task, config) != cp['head'] or task_fingerprint(task, config) != cp['worktree_fingerprint']:
                        return set_phase(state, job, 'uncertain', '인계 기록 이후 작업 파일이 바뀌었습니다.')
                    result = subprocess.run(cli(config_path, state, 'handoff', target['id'], '--owner', target['owner'],
                        '--writer-stopped', '--evidence-file', str(directory(state) / (job['id'] + '-handoff.json'))),
                        capture_output=True, text=True, timeout=15)
                    if result.returncode:
                        return set_phase(state, job, 'uncertain', '공식 인계가 거절됐습니다. 실행을 반복하지 않습니다.')
                finally:
                    store.close()
            else:
                from .app_coordinator import read_record, record_path
                record = read_record(state)
                if record.get('nonce') != target['owner']:
                    return set_phase(state, job, 'cancelled', '총괄 소유권이 바뀌었습니다.')
                record.update(phase='closed', context_checkpoint=str(job_path(state, job['id'])))
                write(record_path(state), record)
            return set_phase(state, job, 'waiting_dispatch', '새 담당의 유효 한도와 승인을 확인합니다.')
        if job['phase'] == 'waiting_dispatch':
            from .routing import execution_policy, execution_candidates
            from .__main__ import select_execution
            store=Store(state)
            try:
                if target['kind']=='task':
                    task=store.get(target['id'])
                    if task.get('owner') and task['owner']!=target['owner']:
                        return set_phase(state,job,'dispatching','다른 정식 배정의 수신을 확인합니다.')
                    role=execution_policy(config,task)
                    if role['action']!='dispatch':
                        return set_phase(state,job,'uncertain','명시 모델 결정이 필요합니다. 승인 대기를 보존했습니다.')
                    selection=select_execution(config,store,task,time.time())
                    candidates=execution_candidates(config,task,role,time.time())
                    pairs=[(c['provider'],c['account']) for c in candidates] if candidates is not None else [(role['provider'],a) for a in account_order(config,time.time())]
                else:
                    role=config['coordinator']
                    account=choose_account(cached_quota(config,store,role['provider'],'team'),cached_quota(config,store,role['provider'],'personal'),
                        time.time(),config['quota_ttl_seconds'],order=account_order(config,time.time()))
                    selection={'action':'dispatch' if account else 'wait','account':account}
                    pairs=[(role['provider'],a) for a in account_order(config,time.time())]
            finally:store.close()
            from . import session_pool
            if target['kind']=='task' and session_pool.managed(config,task) and time.time()-job.get('last_refresh',0)>=300:
                refresh_candidates(config_path,state,pairs)
                job['last_refresh']=time.time()
                store=Store(state)
                try:selection=select_execution(config,store,store.get(target['id']),time.time())
                finally:store.close()
            if selection['action']!='dispatch':
                if time.time()-job.get('last_refresh',0)>=300:
                    refresh_candidates(config_path,state,pairs)
                    job['last_refresh']=time.time()
                return set_phase(state,job,'waiting_dispatch','사용 가능한 계정을 기다립니다. 새 한도 확인은 최대 5분에 한 번 수행합니다.')
            from . import session_pool
            if target['kind']=='task' and session_pool.managed(config,task):
                chosen=(task['team'],selection['provider'],selection['account'])
                session_pool.prepare(config_path,state,only=chosen)
                session_pool.present(config,state,only=chosen)
                update_next_session(config,state,job)
            with locked(directory(state)/'model-start'):
                if not capacity_available(state,None,job['id']):
                    return set_phase(state,job,'waiting_dispatch','동시 실행 여유가 생기면 새 담당을 연결합니다.')
                set_phase(state,job,'dispatching','공식 배정과 새 담당의 수신 확인을 진행합니다.')
            if target['kind'] == 'coordinator':
                from .app_coordinator import start
                result = start(config_path, state, config['context_bindings'], True, context_job=job['id'])
                job['dispatch_result'] = result
                write(job_path(state, job['id']), job)
            else:
                store = Store(state)
                try:
                    task = store.get(target['id'])
                finally:
                    store.close()
                launched = _call('run', '--json', '--bg', '--no-parent', '--no-notify',
                    '--name', 'context-dispatch-' + job['id'][:12], '--',
                    *cli(config_path, state, 'dispatch', target['id'], '--expected-attempt', str(task['attempt_count'])))
                job['controller'] = launched['address']
                write(job_path(state, job['id']), job)
            return
        if job['phase'] == 'dispatching':
            if time.time()-job['updated_at'] > 300:
                return set_phase(state,job,'uncertain','새 담당의 수신·인증을 확인해 주세요. 추가 실행하지 않습니다.')
            if target['kind'] == 'coordinator':
                from .app_coordinator import read_record
                record = read_record(state)
                if record.get('terminal') and record.get('nonce') != target['owner']:
                    job['new_terminal']=record['terminal']
                    write(job_path(state,job['id']),job)
                if record.get('phase') != 'ready' or record.get('nonce') == target['owner']:
                    return
                address = record['terminal']
            else:
                store = Store(state)
                try:
                    task = store.get(target['id'])
                finally:
                    store.close()
                if task.get('runtime',{}).get('terminal') and task.get('owner') != target['owner']:
                    job['new_terminal']=task['runtime']['terminal']
                    write(job_path(state,job['id']),job)
                if task.get('owner') == target['owner']:
                    return set_phase(state, job, 'uncertain', '이전 소유권이 남아 있습니다.')
                if task['state'] in ('queued', 'handed_off'):
                    if time.time() - job['updated_at'] > 60:
                        return set_phase(state, job, 'uncertain', '배정 실행 결과를 확인해야 합니다.')
                    return
                if task['state'] not in ('running', 'ready', 'awaiting_review') or task.get('runtime', {}).get('identity_checked') is not True:
                    if job.get('new_terminal'):
                        state_now=(_call('get',job['new_terminal']).get('status') or {}).get('state')
                        if state_now in ('blocked','error'):
                            job['needs_you']=True
                            job['reason']='새 담당의 확인 창이나 인증을 완료해 주세요.'
                            write(job_path(state,job['id']),job)
                    return
                address = task['runtime']['terminal']
            attach(config, state, target, address)
            job['new_terminal'] = address
            return set_phase(state, job, 'done', '새 담당의 수신을 확인했습니다. 기존 승인 범위에서 이어갑니다.')
    except Exception as error:
        job['error_type'] = type(error).__name__
        set_phase(state, job, 'uncertain', '인계 상태를 확인해 주세요. 중복 실행하지 않고 기록을 보존했습니다.')


def refresh_candidates(config_path, state, pairs):
    config=load(config_path)
    from .policy import eligible
    for provider,account in pairs:
        store=Store(state)
        try:snapshot=cached_quota(config,store,provider,account)
        finally:store.close()
        if eligible(snapshot,time.time(),config['quota_ttl_seconds']):return
        if snapshot and number(snapshot.get('observed_at')) and 0 <= time.time()-snapshot['observed_at'] <= config['quota_ttl_seconds']:
            continue
        try:
            if provider=='claude':
                from .app_usage import refresh_native_usage
                refresh_native_usage(config,state,account)
            else:
                from .monitor import collect_and_save
                collect_and_save(state,config_path,account,profile(config,provider,account))
        except Exception:pass
        store=Store(state)
        try:snapshot=cached_quota(config,store,provider,account)
        finally:store.close()
        if eligible(snapshot,time.time(),config['quota_ttl_seconds']):return


def attach(config, state, previous, address):
    binding_path = Path(config['context_bindings'])
    lock = os.open(str(binding_path) + '.context.lock', os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        value = read(binding_path)
        if previous['kind'] == 'task':
            store = Store(state)
            try:
                task = store.get(previous['id'])
            finally:
                store.close()
            row = value['teams'].get(task['team'])
            if row and row.get('terminal') == previous['terminal'] and task['role'] == 'leader':
                row['terminal'] = address
                _call('label', address, 'herdr.space=' + row['space'])
                write(binding_path, value)
        # Expose the replacement without leaving the person in another tab.
        try:
            app = json.loads(subprocess.check_output(['herdr-app', 'ls'], timeout=5))
            active = [(sp, tab) for sp in app['spaces'] if sp.get('active') for tab in sp['tabs'] if tab.get('active')]
            restore = active[0] if active and all(t['terminal'] != previous['terminal'] for t in active[0][1]['terminals']) else None
            subprocess.run(['herdr-app', 'do', 'agent.focus', '--terminal', address], capture_output=True, timeout=5, check=True)
            if restore:
                subprocess.run(['herdr-app', 'do', 'space.select', '--id', restore[0]['id']], capture_output=True, timeout=5)
                subprocess.run(['herdr-app', 'do', 'tab.select', '--id', restore[1]['id']], capture_output=True, timeout=5)
        except (subprocess.SubprocessError, ValueError, OSError):
            pass  # Durable bindings are also used when the window next opens.
    finally:
        os.close(lock)


def _operations_tick(config_path, state):
    config = load(config_path)
    from . import operations, admission
    if (not settings(config)['enabled'] or config.get('runtime_backend') != 'core'
            or not operations.enabled(config)):
        return {'enabled':False}
    admission.configure(state, operations.policy(config))
    admission.reconcile_stopped(state)
    from .queue_actions import consume
    actions = consume(config_path, state)
    # A sync request already performed this consumer's full reconcile/dispatch pass.
    synced = next((action for action in reversed(actions) if action['kind']=='sync'
                   and action.get('result') is not None), None)
    result = dict(synced['result']['dispatch']) if synced else operations.tick(config_path, state)
    result['actions'] = actions
    return result


def dispatch_status(state):
    """Read the independent dispatcher heartbeat without querying native actors."""
    path = Path(state).resolve().with_name('context-handoffs') / 'dispatch-consumer.json'
    value = read(path) or {'phase':'missing', 'enabled':False,
                           'reason':'dispatch consumer heartbeat missing'}
    return {**value, 'heartbeat_age_seconds':max(0, time.time()-value.get('observed_at',0))}


@contextmanager
def dispatch_consumer(config_path, state):
    """A single sequential dispatcher, inside the supervisor's lifetime flock."""
    from .wake import listener, notify, wait
    stopped = threading.Event()
    path = directory(state) / 'dispatch-consumer.json'
    value = {'schema_version':1, 'pid':os.getpid(), 'phase':'starting', 'enabled':False,
             'observed_at':time.time(), 'source_fingerprint':LOADED_SOURCE_FINGERPRINT,
             'last_success_at':None, 'consecutive_errors':0}
    failure = []
    with listener(state, channel='dispatch') as wake:
        def publish(**changes):
            value.update(changes, observed_at=time.time())
            write(path, value)
        def reload_required():
            if source_fingerprint() == LOADED_SOURCE_FINGERPRINT:
                return False
            publish(phase='reload_required', enabled=False, reload_required=True,
                    reason='dispatch source version changed; current attempt drained without retry')
            notify(state)
            return True
        def consume():
            try:
                publish(thread_id=threading.get_ident(), thread_name=threading.current_thread().name)
                while not stopped.is_set():
                    if reload_required():
                        break
                    publish(phase='working', enabled=False, tick_started_at=time.time())
                    try:
                        result = _operations_tick(config_path, state)
                    except Exception as error:
                        publish(phase='error', enabled=False, consecutive_errors=value['consecutive_errors']+1,
                                reason=type(error).__name__+': '+str(error),
                                last_error={'type':type(error).__name__, 'reason':str(error), 'at':time.time()})
                    else:
                        if reload_required():
                            break
                        publish(phase='idle' if result.get('enabled') is True else 'disabled',
                                enabled=result.get('enabled') is True, last_success_at=time.time(),
                                consecutive_errors=0, reason='')
                    if not stopped.is_set():
                        wait(wake, 5)
            except BaseException as error:
                failure.append(error)
                publish(phase='error', enabled=False, reason=type(error).__name__+': '+str(error),
                        last_error={'type':type(error).__name__, 'reason':str(error), 'at':time.time()})
            finally:
                publish(phase=value['phase'] if value['phase'] in ('error','reload_required') else 'stopped',
                        enabled=False, stopped_at=time.time())
        scope = hashlib.sha256(str(Path(state).resolve()).encode()).hexdigest()[:12]
        thread = threading.Thread(target=consume, name='teamkit-dispatch-'+scope)
        thread.start()
        try:
            yield thread
        finally:
            stopped.set()
            notify(state)
            # Keep the lifetime flock until the in-flight tick drains; no second
            # supervisor may start merely because a shutdown timeout elapsed.
            thread.join()
            if failure:
                raise RuntimeError('dispatch consumer failed; inspect its error heartbeat') from failure[0]


def tick(config_path, state, pool=None, futures=None, operations_consumer=True):
    config = load(config_path)
    if not settings(config)['enabled'] or config.get('runtime_backend') != 'core':
        return {'enabled':False}
    if futures is not None:
        for nonce,future in list(futures.items()):
            if future.done():
                future.result()
                del futures[nonce]
    from . import operations, admission
    if operations.enabled(config):
        admission.configure(state, operations.policy(config))
        if operations_consumer:
            if pool is None:
                _operations_tick(config_path, state)
            elif 'operation-tickets' not in futures:
                futures['operation-tickets'] = pool.submit(_operations_tick, config_path, state)
    for target in targets(config, state):
        try:
            native(target, state)
            measured = observe(config, state, target)
            from .handoff_trigger import trigger
            store=Store(state)
            try:measured=trigger(config,store,target,measured,time.time())
            finally:store.close()
            request(config, state, target, measured)
        except Exception:
            continue
    for job in jobs(state):
        if pool is None:
            advance(config_path, state, job)
        elif job['phase'] not in FINISHED:
            future=futures.get(job['id'])
            if future is not None and future.done():
                future.result()
                del futures[job['id']]
            if job['id'] not in futures:
                futures[job['id']]=pool.submit(advance,config_path,state,job)
    # Historical pool JSON is display/reconciliation evidence only. All new work
    # is consumed by the single operation_tickets dispatcher above.
    from . import team_dispatch
    dispatch_state=team_dispatch.tick(config_path,state)
    for team in dispatch_state.get('teams',[]):
        key='team-dispatch-'+team
        if pool is None:team_dispatch.advance(config_path,state,team)
        elif key not in futures:futures[key]=pool.submit(team_dispatch.advance,config_path,state,team)
    return {'enabled':True, 'jobs':[{k:j.get(k) for k in ('id', 'phase', 'reason')} for j in jobs(state)]}


def serve(config_path, state, once=False):
    if not Path(state).is_file():
        raise ValueError('context supervisor requires an existing task store')
    path = directory(state) / 'supervisor.lock'
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        from .wake import listener, wait
        if source_fingerprint()!=LOADED_SOURCE_FINGERPRINT:
            write(directory(state)/'supervisor.json', {'observed_at':time.time(), 'pid':os.getpid(),
                  'enabled':False, 'reload_required':True, 'terminal':os.environ.get('HC_TERMINAL_ID'),
                  'source_fingerprint':LOADED_SOURCE_FINGERPRINT})
            return {'enabled':False, 'reload_required':True}
        workers = load(config_path).get('operations', {}).get('consumer_workers', 2)
        if type(workers) is not int or not 2 <= workers <= 16:
            raise ValueError('consumer_workers must be an integer in 2..16')
        with listener(state) as wake, ThreadPoolExecutor(max_workers=workers) as pool:
            with (nullcontext() if once else dispatch_consumer(config_path, state)):
                futures={}
                while True:
                    if source_fingerprint()!=LOADED_SOURCE_FINGERPRINT:
                        write(directory(state)/'supervisor.json',{'observed_at':time.time(),'pid':os.getpid(),
                            'enabled':False,'reload_required':True,'terminal':os.environ.get('HC_TERMINAL_ID'),'source_fingerprint':LOADED_SOURCE_FINGERPRINT})
                        return {'enabled':False,'reload_required':True}
                    value = (tick(config_path, state, None, futures) if once
                             else tick(config_path, state, pool, futures, operations_consumer=False))
                    heartbeat = {'observed_at':time.time(), 'pid':os.getpid(), 'enabled':value['enabled'],
                                 'terminal':os.environ.get('HC_TERMINAL_ID'), 'source_fingerprint':LOADED_SOURCE_FINGERPRINT}
                    if not once:
                        heartbeat['dispatch'] = dispatch_status(state)
                    write(directory(state) / 'supervisor.json', heartbeat)
                    if once:
                        return value
                    wait(wake, min(5, settings(load(config_path))['interval']))
    finally:
        os.close(fd)


def maybe_register(config, state):
    if not settings(config)['enabled']:
        return
    try:
        register(config, state)
    except Exception:
        pass  # Unregistered sessions remain ineligible for automatic rollover.


def enroll(config, state, task_id=None):
    """One-time native /status read; never submits a model prompt or a draft."""
    results = []
    for target in targets(config, state):
        if task_id is not None and target['id'] != task_id:
            continue
        if target['provider'] != 'codex' or target.get('session_id'):
            continue
        value = native(target, state)
        if not clean_prompt(value, target):
            results.append({'id':target['id'], 'registered':False, 'reason':'working, draft or dialog'})
            continue
        _call('send', target['terminal'], '/status')
        text = subprocess.run(['hc', 'read', '--lines', '60', target['terminal']],
                              capture_output=True, text=True, timeout=5, check=True).stdout
        found = re.findall(r'^\s*Session:\s+([a-f0-9-]{36})\s*$', text, re.M)
        if not found:
            results.append({'id':target['id'], 'registered':False, 'reason':'native session unavailable'})
            continue
        pr = profile(config, 'codex', target['account'])
        measured = codex_observation(pr['home'], found[-1], target['root'])
        if measured is None:
            results.append({'id':target['id'], 'registered':False, 'reason':'native session record mismatch'})
            continue
        native(target, state)
        save_registration(state, target, found[-1])
        results.append({'id':target['id'], 'registered':True, 'used':measured['used']})
    return {'registrations':results, 'model_requests':0}


def ensure(config_path, state):
    config = load(config_path)
    if not settings(config)['enabled']:
        return
    scope = hashlib.sha256(str(Path(state).resolve()).encode()).hexdigest()[:12]
    name = 'context-supervisor-' + scope
    path = directory(state) / 'supervisor-start.json'
    with locked(path):
        candidates = _call('ls', '--json', '--label', 'teamkit.context-scope=' + scope)['terminals']
        live = [t for t in candidates if t.get('state') == 'live']
        if live:
            if len(live) != 1 or live[0].get('name') != name:
                raise ValueError('context supervisor ownership conflict')
            value = _call('get', live[0]['address'])
            if 'auto-context' not in value.get('argv', []) or 'serve' not in value.get('argv', []):
                raise ValueError('context supervisor program changed')
            heartbeat=read(directory(state)/'supervisor.json') or {}
            started=read(path) or {}
            running=(heartbeat.get('source_fingerprint') if heartbeat.get('terminal')==live[0]['address']
                     else started.get('source_fingerprint') if started.get('terminal')==live[0]['address'] else None)
            if running!=source_fingerprint():
                raise ValueError('context supervisor source version differs; inspect active jobs and restart only the supervisor safely')
            return
        previous = read(path)
        if previous and previous.get('phase') == 'starting':
            raise ValueError('context supervisor launch outcome unknown; inspect before retry')
        write(path, {'phase':'starting', 'at':time.time()})
        created = _call('run', '--json', '--bg', '--outlive-parent', '--no-notify',
                        '--name', name, '--label', 'teamkit.context-scope=' + scope,
                        '--', *cli(config_path, state, 'auto-context', 'serve'))
        write(path, {'phase':'started', 'terminal':created['address'], 'at':time.time(), 'source_fingerprint':LOADED_SOURCE_FINGERPRINT})


def summary(config, state):
    if not settings(config)['enabled']:
        return {'enabled':False,'label':'자동 인계 꺼짐','active':[]}
    root=Path(state).resolve().with_name('context-handoffs')
    heartbeat=read(root/'supervisor.json') or {}
    healthy=heartbeat.get('enabled') is True and 0 <= time.time()-heartbeat.get('observed_at',0) < 120
    active=[]
    if root.is_dir():
        for job in jobs(state):
            phase=job['phase']
            if phase in ('done','cancelled'):
                continue
            needs_you=phase=='uncertain' or job.get('needs_you',False)
            title=('인계 확인 필요' if needs_you else '한도 확인 대기' if phase=='waiting_dispatch' else '인계 중')
            active.append({'id':job['id'],'terminal':job['target']['terminal'],
                'new_terminal':job.get('new_terminal'),
                'task':job['target']['id'],'kind':job['target']['kind'],'phase':phase,
                'status':'blocked' if needs_you or phase=='waiting_dispatch' else 'working',
                'title':title,'next_session':job.get('next_session'),'detail':job.get('reason') or 'Context 90%에서 담당을 이어 붙이고 있어요'})
    return {'enabled':True,'healthy':healthy,'label':'자동 인계 · 90%' if healthy else '자동 인계 · 감시 확인 필요','active':active}


def capacity_available(state, target=None, current_job=None, config=None):
    store=Store(state)
    try:
        tasks=store.list()
        from . import admission
        enabled, policy = admission.policy_for(config, store.db)
        if enabled:
            try:
                observation = admission.observe_native()
                snapshot = admission.snapshot(store.db, config, observation)
                included = False
                if target:
                    runtime = next((t.get('runtime', {}) for t in tasks if t['id'] == target.get('id')), {})
                    attempt = runtime.get('resource_attempt')
                    if target.get('kind') == 'coordinator':
                        from .app_coordinator import read_record
                        attempt = read_record(state).get('resource_attempt')
                    from .resources import ResourceLedger
                    included = any(r['attempt'] == attempt and r['execution_reserved']
                                   for r in ResourceLedger._rows(store.db))
                    included |= any(r.get('terminal') == target.get('terminal')
                                    and r.get('status') not in ('idle', 'done', 'blocked', 'finished')
                                    for r in observation['terminals'])
                if policy['global_limit'] is None:
                    return True
                return snapshot['used'] <= policy['global_limit'] if included else snapshot['available'] > 0
            except Exception:
                return False
    finally:store.close()
    aliases={t.get('runtime',{}).get('terminal'):'task:'+t['id'] for t in tasks}
    from .app_coordinator import read_record
    coord=read_record(state)
    if coord.get('terminal'):aliases[coord['terminal']]='coordinator'
    active={'task:'+t['id'] for t in tasks if t['state'] in ACTIVE}
    for terminal in _call('ls','--json')['terminals']:
        if terminal.get('agent') in ('codex','claude-code') and terminal.get('status')=='working':
            active.add(aliases.get(terminal['address'],'native:'+terminal['address']))
    for job in jobs(state):
        if job['id']==current_job:continue
        if job['phase'] in ('requesting','requested','dispatching'):
            t=job['target'];active.add('task:'+t['id'] if t['kind']=='task' else 'coordinator')
    # Sends committed but not yet started also hold a slot; the model-start lock is released before they run.
    try:
        from .pool_control import requests as pool_requests
        from .team_dispatch import _events as dispatch_events
        for job in pool_requests(state):
            if job.get('phase') in ('sending','dispatching') and isinstance(job.get('task'),str):
                active.add('task:'+job['task'])
        for event in dispatch_events(state):
            receiver=event.get('receiver') or {}
            if event.get('phase')=='sending' and isinstance(receiver.get('task'),str):
                active.add('task:'+receiver['task'])
    except (OSError,ValueError):
        return False
    key=('task:'+target['id'] if target['kind']=='task' else 'coordinator') if target else None
    return len(active)<=3 if key in active else len(active)<3


def update_next_session(config,state,job):
    from . import session_pool
    if job['target']['kind']!='task' or not session_pool.enabled(config):return
    store=Store(state)
    try:
        task=store.get(job['target']['id'])
        if not session_pool.managed(config,task):return
        from .__main__ import select_execution
        selected=select_execution(config,store,task,time.time())
    finally:store.close()
    hint={k:selected.get(k) for k in ('action','provider','account','model','effort','reason') if k in selected}
    if selected['action']=='dispatch':
        try:
            slot=session_pool.preview(config,state,task,selected)
            hint.update({k:slot.get(k) for k in ('terminal','generation')})
        except ValueError:
            hint['reason']='preferred slot is occupied or unavailable; replacement requires verified exit'
    if job.get('next_session')!=hint:
        job.setdefault('receiver_changes',[]).append({'at':time.time(),'previous':job.get('next_session'),'next':hint})
        job['next_session']=hint
