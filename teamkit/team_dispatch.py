"""Event-driven, personal Codex queue observers; product work stays in its ledger."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import time

from .config import cached_quota, load
from .policy import eligible, number

PIN = {'provider': 'codex', 'account': 'personal'}
TASK_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')
EVENT_ID = re.compile(r'[a-f0-9]{32}\Z')


def settings(config):
    value = config.get('team_dispatch')
    if value is None:
        return {'enabled': False, 'tasks': {}, 'stall_seconds': 900}
    if (not isinstance(value, dict) or set(value) != {'enabled', 'tasks', 'stall_seconds'}
            or type(value['enabled']) is not bool or not isinstance(value['tasks'], dict)
            or type(value['stall_seconds']) is not int or not 60 <= value['stall_seconds'] <= 86400):
        raise ValueError('team_dispatch requires enabled, tasks and stall_seconds=60..86400')
    tasks = value['tasks']
    if (value['enabled'] and (config.get('runtime_backend') != 'core' or set(tasks) != set(config['teams'])
                             or 'codex' not in config['profiles']
                             or config.get('context_handoff', {}).get('enabled') is not True
                             or config.get('session_pool', {}).get('enabled') is not True)):
        raise ValueError('enabled team_dispatch requires core, the existing supervisor/pool and one personal Codex task per team')
    if (any(team not in config['teams'] or not isinstance(ident, str) or not TASK_ID.fullmatch(ident)
            for team, ident in tasks.items()) or len(set(tasks.values())) != len(tasks)
            or set(tasks.values()) & set(config.get('session_pool', {}).get('tasks', {}).values())):
        raise ValueError('observer tasks must be distinct from product leaders')
    return value


def _root(state):
    root = Path(state).resolve().with_name('team-dispatch')
    if root.is_symlink():
        raise ValueError('observer storage must not be a symlink')
    return root


def _directory(state):
    root = _root(state)
    root.mkdir(mode=0o700, exist_ok=True)
    events = root/'events'
    events.mkdir(mode=0o700, exist_ok=True)
    if events.is_symlink():
        raise ValueError('observer events must not be a symlink')
    return root


def _read(path):
    from .context_handoff import read
    return read(path)


def _write(path, value):
    from .pool_control import write_request
    write_request(path, value)


def _snapshot(state):
    from .workflow import snapshot
    return snapshot(state)


def _task(config, state, team):
    setting = settings(config)
    if not setting['enabled'] or team not in setting['tasks']:
        raise ValueError('configured observer team required')
    task = next((t for t in _snapshot(state)['tasks'] if t['id'] == setting['tasks'][team]), None)
    if task is None:
        raise ValueError('observer task is not prepared')
    if (task.get('team_dispatch_team') != team or task.get('team') != team
            or task.get('role') != 'leader' or task.get('work_id')
            or task.get('root') != config['teams'][team]['root'] or task.get('execution_constraint') != PIN
            or task.get('classification') != {'kind': 'general', 'risks': []}):
        raise ValueError('observer task contract changed')
    if task.get('owner') and (task.get('provider'), task.get('account')) != ('codex', 'personal'):
        raise ValueError('observer account changed; preserve current actor')
    return task


def prepare(config_path, state):
    config = load(config_path)
    setting = settings(config)
    if not setting['enabled']:
        raise ValueError('team dispatch is disabled')
    from .store import Store
    from .__main__ import instructions_basis
    store = Store(state)
    try:
        for team, ident in setting['tasks'].items():
            previous = next((t for t in store.list() if t['id'] == ident), None)
            if previous is not None:
                _task(config, state, team)
                continue
            root = config['teams'][team]['root']
            # Observer startup is bound to team instructions, not a moving product HEAD.
            base, kind = instructions_basis(config['teams'][team]), 'instructions'
            goal = ('Observe only this team queue and report evidence; do not write product files, '
                    'approve designs, resolve product work, resend requests, create agents or switch accounts. '
                    'Use the supplied receipt and park commands. A later event message supplies exact event ack commands. '
                    'A queue-event acknowledgement is not product completion. When UI verification is required, '
                    'use the existing ui-lab request path; its personal Codex operator uses test-macbook serially. '
                    'Do not operate the local desktop. Remain on personal Codex; unavailable quota means wait.')
            task = store.create(ident, team, 'leader', root, base, goal, kind,
                                {'kind': 'general', 'risks': []})
            with store.transaction():
                current = store.get(ident)
                if current != task or current.get('owner'):
                    raise ValueError('observer changed during preparation')
                current.update(execution_constraint=dict(PIN), team_dispatch_team=team)
                store._save(current, 'observer_prepared')
    finally:
        store.close()
    return status(config, state)


def _ready(config, state, team):
    task = _task(config, state, team)
    if task['state'] != 'ready' or not task.get('owner'):
        return None
    if task.get('runtime', {}).get('identity_checked') is not True:
        raise ValueError('observer receipt identity is unconfirmed')
    from .core_runtime import CoreRuntime, _model_effort
    from .context_handoff import clean_prompt
    native = CoreRuntime(task)._native(task['runtime']['name'])
    if (native.get('status') or {}).get('state') not in ('idle', 'done'):
        return None
    native = CoreRuntime(task)._native(task['runtime']['name'], ready=True)
    argv = (native.get('foreground') or {}).get('argv', [])
    if ('--approve-for-me' not in argv or '--no-daemon' not in argv
            or any(v in argv for v in ('--yolo', '--dangerously-bypass-approvals-and-sandbox'))
            or _model_effort('codex', argv) != (task['model'], task['effort'])):
        raise ValueError('observer native permission or model does not match')
    target = {'terminal': task['runtime']['terminal'], 'empty_prompt': task['runtime'].get('pool_empty_prompt')}
    return task if clean_prompt(native, target) else None


def bootstrap(config_path, state, team):
    config = load(config_path)
    task = _task(config, state, team)
    if task['state'] == 'ready':
        current = _ready(config, state, team)
        if current is None:
            return {'state': 'waiting', 'reason': 'preserve current observer work and input'}
        return {'state': 'ready', 'task': task['id'], 'terminal': task['runtime']['terminal']}
    if task['state'] not in ('queued', 'handed_off') or task.get('owner'):
        return {'state': 'waiting', 'reason': 'existing observer actor requires inspection'}
    from .pool_control import official
    return official(config_path, state, 'dispatch', task['id'], '--expected-attempt', str(task.get('attempt_count', 0)))


def _events(state):
    root = _root(state)/'events'
    if root.is_symlink():
        raise ValueError('observer events must not be a symlink')
    if not root.exists():
        return []
    result = []
    for p in root.glob('*.json'):
        if not EVENT_ID.fullmatch(p.stem):
            continue
        event = _read(p)
        if not event or event.get('id') != p.stem:
            raise ValueError('invalid observer event identity')
        result.append(event)
    return sorted(result, key=lambda e: (e['created_at'], e['id']))


def status(config, state):
    setting = settings(config)
    if not setting['enabled']:
        return {'enabled': False, 'managers': [], 'events': [], 'heartbeat': None}
    tasks = {t['id']: t for t in _snapshot(state)['tasks']}
    managers = []
    for team, ident in setting['tasks'].items():
        task = tasks.get(ident, {})
        managers.append({'team': team, 'task': ident, 'state': task.get('state', 'unprepared'),
                         'provider': task.get('provider'), 'account': task.get('account'),
                         'terminal': task.get('runtime', {}).get('terminal')})
    return {'enabled': True, 'managers': managers, 'events': _events(state),
            'heartbeat': _read(_root(state)/'heartbeat.json')}


def tick(config_path, state, jobs=None):
    config = load(config_path)
    setting = settings(config)
    if not setting['enabled']:
        return {'enabled': False, 'teams': []}
    from .context_handoff import locked
    from .pool_control import requests
    root = _directory(state)
    jobs = requests(state) if jobs is None else jobs
    now = time.time()
    with locked(root/'observe'):
        for job in jobs:
            if job.get('team') not in setting['tasks']:
                continue
            if not isinstance(job.get('id'), str) or not EVENT_ID.fullmatch(job['id']):
                raise ValueError('bounded queue request identity required')
            observed = root/('observed-'+job['id']+'.json')
            previous = _read(observed) or {}
            semantics = {'team': job['team'], 'request': job['id'], 'work_id': job.get('work_id'),
                         'queue_phase': job['phase'], 'queue_reason': job.get('reason', '')}
            base_signature = hashlib.sha256(json.dumps(semantics, sort_keys=True).encode()).hexdigest()
            supplied_at = job.get('updated_at', job.get('at', now))
            since_at = (previous['since_at'] if previous.get('base_signature') == base_signature
                        else min(now, supplied_at) if number(supplied_at) else now)
            semantics['stalled'] = (job['phase'] not in ('completed', 'deferred', 'cancelled')
                                    and now - since_at >= setting['stall_seconds'])
            signature = hashlib.sha256(json.dumps(semantics, sort_keys=True).encode()).hexdigest()
            if previous.get('signature') == signature:
                continue
            sequence = previous.get('sequence', 0)+1
            ident = hashlib.sha256((job['id']+':'+str(sequence)+':'+signature).encode()).hexdigest()[:32]
            event_path = root/'events'/(ident+'.json')
            if _read(event_path) is None:
                _write(event_path, {**semantics, 'id': ident, 'phase': 'pending', 'created_at': now,
                                    'updated_at': now, 'sequence': sequence})
            # Write the event before its observation; a crash reuses the same event id.
            _write(observed, {'signature': signature, 'sequence': sequence,
                              'base_signature': base_signature, 'since_at': since_at})
        _write(root/'heartbeat.json', {'observed_at': now, 'pid': os.getpid(), 'enabled': True})
    return {'enabled': True, 'teams': list(setting['tasks'])}


def _quota(config, state):
    from types import SimpleNamespace
    snapshot = _snapshot(state)
    store = SimpleNamespace(path=Path(state), quota=snapshot['quotas'].get)
    return cached_quota(config, store, 'codex', 'personal')


def advance(config_path, state, team):
    config = load(config_path)
    setting = settings(config)
    if not setting['enabled'] or team not in setting['tasks']:
        return
    from .context_handoff import locked, capacity_available
    from .pool_control import official, command
    root = _directory(state)
    key = hashlib.sha256(team.encode()).hexdigest()
    with locked(root/('team-'+key)):
        events = [e for e in _events(state) if e['team'] == team]
        for event in events:
            if event['phase'] == 'sending':
                event.update(phase='uncertain', reason='observer delivery outcome requires inspection', updated_at=time.time())
                _write(root/'events'/(event['id']+'.json'), event)
        if any(e['phase'] in ('sending', 'received', 'uncertain') for e in events):
            return
        event = next((e for e in events if e['phase'] == 'pending'), None)
        if event is None:
            return
        path = root/'events'/(event['id']+'.json')
        try:
            if not eligible(_quota(config, state), time.time(), config['quota_ttl_seconds']):
                event.update(reason='personal Codex quota is unavailable or stale', updated_at=time.time())
                _write(path, event)
                return
            task = _ready(config, state, team)
            if task is None or not capacity_available(state):
                return
            event.update(phase='sending', receiver={'task': task['id'], 'owner': task['owner'],
                         'attempt': task['attempt_count'], 'terminal': task['runtime']['terminal']}, updated_at=time.time())
            _write(path, event)
            evidence = root/('ack-'+event['id']+'.json')
            ack_command = command(config_path, state, 'team-dispatch', 'ack', '--team', team,
                                  '--event', event['id'], '--owner', task['owner'], '--attempt', str(task['attempt_count']),
                                  '--evidence-file', str(evidence))
            message = root/('message-'+event['id']+'.txt')
            content = ('Queue observation event, not a product execution approval.\n'+json.dumps(event, ensure_ascii=False)+
                       '\nRead current queue/ledger evidence; do not change products, resend requests or resolve work. '
                       'Write a JSON object with a nonempty verification string array at '+str(evidence)+
                       ', then acknowledge only this event using:\n'+shlex.join(ack_command)+
                       '\nUse the supplied official park after acknowledgement. UI requests go through ui-lab/test-macbook.\n')
            with os.fdopen(os.open(message, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'w') as out:
                out.write(content); out.flush(); os.fsync(out.fileno())
            official(config_path, state, 'tell', task['id'], '--message-file', str(message),
                     '--expected-owner', task['owner'], '--expected-attempt', str(task['attempt_count']))
            with locked(path):
                current = _read(path)
                if current['phase'] == 'sending':
                    current.update(phase='received', reason='transport confirmed; awaiting actor acknowledgement', updated_at=time.time())
                    _write(path, current)
        except Exception:
            with locked(path):
                current = _read(path)
                if current['phase'] == 'acknowledged':
                    return
                current.update(phase='uncertain' if current['phase'] == 'sending' else 'pending',
                               reason='observer boundary unconfirmed; preserve actor and event', updated_at=time.time())
                _write(path, current)


def ack(config_path, state, team, event_id, owner, attempt, evidence_file):
    if not isinstance(event_id, str) or not EVENT_ID.fullmatch(event_id):
        raise ValueError('bounded observer event identity required')
    config = load(config_path)
    task = _task(config, state, team)
    from .core_runtime import require_managed, CoreRuntime
    address = require_managed()
    if (task.get('runtime', {}).get('terminal') != address or task.get('owner') != owner
            or type(attempt) is not int or task.get('attempt_count') != attempt
            or task.get('state') != 'running'):
        raise ValueError('only the current running observer may acknowledge its event')
    CoreRuntime(task)._native(task['runtime']['name'])
    fd = os.open(evidence_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 256*1024:
            raise ValueError('bounded regular observation evidence required')
        raw = source.read(256*1024+1)
    if len(raw) > 256*1024:
        raise ValueError('observation evidence grew during read')
    evidence = json.loads(raw)
    verification = evidence.get('verification') if isinstance(evidence, dict) else None
    if not isinstance(verification, list) or not verification or any(not isinstance(v, str) or not v.strip() for v in verification):
        raise ValueError('nonempty observation verification evidence required')
    from .context_handoff import locked
    path = _root(state)/'events'/(event_id+'.json')
    # Acknowledge without the consumer lock, which may be waiting for this actor's turn.
    with locked(path):
        event = _read(path)
        expected = {'task': task['id'], 'owner': owner, 'attempt': attempt, 'terminal': address}
        if (not event or event['team'] != team or event.get('receiver') != expected
                or event['phase'] not in ('sending', 'received')):
            raise ValueError('event receiver or lifecycle changed')
        event.update(phase='acknowledged', verification=verification,
                     evidence_sha256=hashlib.sha256(raw).hexdigest(), updated_at=time.time())
        _write(path, event)
    return {k: event[k] for k in ('id', 'team', 'phase', 'work_id')}


def handle(args):
    config = load(args.config)
    if args.action == 'status':
        return status(config, args.state)
    from .core_runtime import require_managed
    require_managed()
    if args.action == 'prepare':
        return prepare(args.config, args.state)
    if args.action == 'bootstrap':
        return bootstrap(args.config, args.state, args.team)
    if args.action == 'ack':
        return ack(args.config, args.state, args.team, args.event, args.owner, args.attempt, args.evidence_file)
    raise ValueError('unsupported team dispatch action')
