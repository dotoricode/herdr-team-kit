"""Single-use native generations for canonical team leaders; no task work at warmup."""
from contextlib import closing, contextmanager
from collections import Counter
import copy
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid

from .config import binding, load, profile
from .core_runtime import CoreRuntime, _call, _model_effort, claude_gui_settings, require_managed
from .routing import execution_policy, execution_candidates
from .runtime import launch_spec
from .staff import display_name

SAFE_FIELDS = ('team', 'task', 'provider', 'account', 'model', 'effort', 'phase', 'generation',
               'owner', 'name', 'terminal', 'native_pid', 'reason', 'updated_at', 'native_verified_at')


def enabled(config):
    settings = config.get('session_pool')
    if settings is None:
        return False
    if not isinstance(settings, dict) or type(settings.get('enabled')) is not bool:
        raise ValueError('session_pool requires boolean enabled')
    if not settings['enabled']:
        return False
    tasks = settings.get('tasks')
    if (set(settings) != {'enabled', 'bindings', 'tasks'} or config.get('runtime_backend') != 'core'
            or set(config.get('profiles', {})) != {'codex', 'claude'}
            or not isinstance(settings['bindings'], str) or not Path(settings['bindings']).is_absolute()
            or not isinstance(tasks, dict) or set(tasks) != set(config['teams'])
            or not all(isinstance(t, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', t) for t in tasks.values())
            or len(set(tasks.values())) != len(tasks)):
        raise ValueError('session_pool requires core, absolute bindings and distinct canonical tasks for every team')
    return True


def task_id(config, team):
    return config['session_pool']['tasks'][team] if enabled(config) else None


def managed(config, task):
    return (enabled(config) and isinstance(task, dict) and task.get('team') in config['teams']
            and task.get('id') == task_id(config, task['team']) and task.get('role') == 'leader'
            and not task.get('work_id') and not task.get('user_decision')
            and task.get('classification', {'kind': 'general', 'risks': []}) == {'kind': 'general', 'risks': []})


def _path(state):
    return Path(state).absolute().with_name('team-session-pool.json')


@contextmanager
def _locked(state, create=True):
    flags = os.O_CREAT | os.O_WRONLY if create else os.O_RDONLY
    fd = os.open(str(_path(state))+'.lock', flags | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read(state):
    path = _path(state)
    if not path.exists() and not path.is_symlink():
        return {'version': 1, 'slots': {}}
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_size > 1024*1024
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError('private regular pool manifest required')
        value = json.load(source)
    if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('slots'), dict):
        raise ValueError('invalid pool manifest')
    for slot in value['slots'].values():
        if (not isinstance(slot, dict) or slot.get('phase') not in ('preparing', 'prepared', 'reserved', 'leased', 'uncertain')
                or not re.fullmatch(r'[0-9a-f]{32}', str(slot.get('owner', '')))
                or type(slot.get('generation')) is not int or slot['generation'] < 1):
            raise ValueError('invalid pool generation; preserve manifest before recovery')
    return value


def _write(state, value):
    fd, temporary = tempfile.mkstemp(prefix='.session-pool-', dir=_path(state).parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, ensure_ascii=False)
            out.flush(); os.fsync(out.fileno())
        os.replace(temporary, _path(state))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _task(state, ident):
    with closing(sqlite3.connect(Path(state).absolute().as_uri()+'?mode=ro', uri=True, timeout=1)) as db:
        row = db.execute('SELECT body FROM tasks WHERE id=?', (ident,)).fetchone()
    if row is None:
        raise ValueError('canonical task must already exist')
    return json.loads(row[0])


def _key(team, provider, account):
    return '/'.join((team, provider, account))


def _spec(config_path, state, config, task, role, owner):
    pr = profile(config, role['provider'], role['account'], role)
    spec = launch_spec(role['provider'], pr, None)
    if role['provider'] == 'claude':
        spec = claude_gui_settings(spec, str(config_path), str(state), role['account'],
                                  config.get('claude_display_commands', {}).get(role['account'], ''))
    spec['args'] += ['--add-dir', str(Path(state).parent)]
    spec['env'].update(TEAMKIT_TASK=task['id'], TEAMKIT_OWNER=owner,
                       TEAMKIT_CONFIG=str(config_path), TEAMKIT_STATE=str(state))
    return spec


def _hint_guard(slot, native):
    labels = native.get('labels') or {}
    return (slot['provider'] == 'claude' and slot['phase'] in ('preparing', 'uncertain')
            and native.get('turn') == 0 and type(native.get('turn')) is int
            and not any(key in labels for key in ('herdr.space', 'herdr.tab')))


def _verify(slot, task, spec, pristine=True, learn_hint=False):
    if slot['task'] != task['id'] or slot['team'] != task['team'] or slot['root'] != task['root'] or slot['spec'] != spec:
        raise ValueError('pool task or immutable launch specification changed')
    if (not isinstance(slot.get('runtime'), dict)
            or slot['runtime'].get('terminal') != slot.get('terminal')
            or slot['runtime'].get('native_pid') != slot.get('native_pid')):
        raise ValueError('pool recorded native identity is incomplete or ambiguous')
    actor = {**task, 'provider': slot['provider'], 'owner': slot['owner'], 'runtime': slot['runtime']}
    native = CoreRuntime(actor)._native(slot['name'], ready=True)
    foreground = native.get('foreground') or {}
    cwd = foreground.get('cwd') or native.get('cwd')
    argv = foreground.get('argv')
    if (not isinstance(cwd, str) or Path(cwd).resolve() != Path(task['root']).resolve()
            or not isinstance(argv, list) or len(argv) != len(spec['args'])+1 or argv[1:] != spec['args']
            or _model_effort(slot['provider'], argv) != (slot['model'], slot['effort'])
            or type(native.get('turn')) is not int or native['turn'] < 0
            or pristine and slot.get('turn_baseline') is not None and native['turn'] != slot['turn_baseline']):
        raise ValueError('pool native generation, cwd, argv or turn changed')
    text = subprocess.run(['hc', 'read', '--lines', '24', slot['terminal']], capture_output=True,
                          text=True, timeout=5, check=True).stdout
    prompts = list(re.finditer(r'^[ \t]*[›❯][^\S\r\n]*(.*)$', text, re.M))
    prompt = prompts[-1].group(1).strip() if prompts else None
    from .prompt_state import empty_input
    hint=slot.get('empty_prompt')
    if empty_input(text,hint):return native
    if (hint is None and learn_hint and _hint_guard(slot, native) and empty_input(text,prompt)
            and isinstance(prompt, str) and re.fullmatch(r'Try "[^"\r\n]{1,200}"', prompt)
            and prompt.isprintable()):
        # A read must not bless a turn or App attachment that raced that read.
        after = CoreRuntime(actor)._native(slot['name'], ready=True)
        if not _hint_guard(slot, after) or after.get('foreground') != foreground:
            raise ValueError('native changed while reading initial hint')
        slot['empty_prompt'] = prompt
        return after
    raise ValueError('pool terminal has a draft or unknown prompt')


def status(config, state):
    active = enabled(config)
    rows = [{key: slot.get(key) for key in SAFE_FIELDS} for slot in _read(state)['slots'].values()] if active else []
    return {'enabled': active, 'slots': rows, 'counts': dict(Counter(row['phase'] for row in rows))}


def _finished(slot, state):
    native = _call('get', slot['terminal'])
    return (native.get('state') == 'finished' and native.get('exit_code') == 0 and native.get('signal', 'unknown') is None
            and native.get('pid') == slot['native_pid'] and native.get('labels', {}).get('teamkit.owner') == slot['owner']
            and (slot['phase']!='prepared' or native.get('turn')==slot.get('turn_baseline'))
            and _task(state, slot['task']).get('owner') != slot['owner'])


def prepare(config_path, state, only=None):
    config_path, state = Path(config_path).resolve(), Path(state).resolve()
    config = load(config_path)
    if not enabled(config):
        return {**status(config, state), 'created': 0}
    if only is not None and (not isinstance(only,tuple) or len(only)!=3 or only[0] not in config['teams'] or only[1] not in ('codex','claude') or only[2] not in ('team','personal')):
        raise ValueError('invalid selected pool slot')
    require_managed()
    created = 0
    with _locked(state):
        manifest = _read(state)
        for team in config['teams']:
            if only and team!=only[0]:continue
            task = _task(state, task_id(config, team))
            if not managed(config, task):
                raise ValueError('pool requires plain canonical leader tasks')
            policy = execution_policy(config, task)
            if policy['action'] != 'dispatch':
                raise ValueError('canonical task requires an explicit user decision')
            for provider in ('codex', 'claude'):
                for account in ('team', 'personal'):
                    if only and (team,provider,account)!=only:continue
                    key = _key(team, provider, account)
                    old = manifest['slots'].get(key)
                    if old:
                        if old['phase'] not in ('leased','prepared') or not _finished(old, state):
                            continue
                    role = {**policy, 'provider': provider, 'account': account,
                        'model': policy['model'] if provider == policy['provider'] else profile(config, provider, account)['model'],
                        'effort': policy.get('effort', 'medium')}
                    candidates=execution_candidates(config,task,policy,time.time())
                    if candidates is not None:
                        role=next((candidate for candidate in candidates if candidate['provider']==provider and candidate['account']==account),role)
                    else:
                        role['effort']=policy.get('effort',profile(config,provider,account).get('effort','medium'))
                    owner = uuid.uuid4().hex
                    slot = {key: role[key] for key in ('provider', 'account', 'model', 'effort')}
                    slot.update(team=team, task=task['id'], root=task['root'], owner=owner, name='tk-'+owner[:20],
                        generation=old['generation']+1 if old else 1, phase='preparing', updated_at=time.time(),
                        spec=_spec(config_path, state, config, task, role, owner),
                        binding=binding(profile(config, provider, account)))
                    if old:
                        slot['previous'] = {k: old[k] for k in ('owner', 'terminal', 'native_pid', 'generation')}
                    manifest['slots'][key] = slot
                    _write(state, manifest)  # Persist intent before any native creation.
                    try:
                        subprocess.run([str(Path.home()/'.local/bin/ensure-atlassian-mcp'), '--provider', provider,
                            '--home', profile(config, provider, account)['home']], capture_output=True, timeout=15, check=True)
                        def record(detail):
                            slot.update(detail); slot['runtime'] = detail
                            _write(state, manifest)
                        runtime = CoreRuntime().launch(task, slot['spec'], slot['name'], record)
                        slot.update(runtime); slot['runtime'] = runtime
                        if provider=='codex':
                            deadline=time.monotonic()+3
                            actor={**task,'provider':provider,'owner':owner,'runtime':runtime}
                            while True:
                                remaining=deadline-time.monotonic()
                                if remaining<=0:raise ValueError('Codex initial status has not settled')
                                initial=CoreRuntime(actor)._native(slot['name'],ready=True,timeout=remaining)
                                if time.monotonic()>=deadline:raise ValueError('Codex initial status has not settled')
                                if initial.get('turn',0)>=1:break
                                time.sleep(.1)
                        native = _verify(slot, task, slot['spec'], learn_hint=True)
                        slot.update(phase='prepared', turn_baseline=native['turn'], native_verified_at=time.time())
                        created += 1
                    except Exception as error:
                        slot.update(phase='uncertain', reason=type(error).__name__)
                    slot['updated_at'] = time.time()
                    _write(state, manifest)
    return {**status(config, state), 'created': created}


def recheck(config_path, state):
    """Revalidate an uncertain pristine native in place; never create or prompt."""
    config_path, state = Path(config_path).resolve(), Path(state).resolve()
    config = load(config_path)
    if not enabled(config):
        return {**status(config, state), 'rechecked': 0}
    require_managed()
    repaired = 0
    with _locked(state):
        manifest = _read(state)
        for slot in manifest['slots'].values():
            if (slot['phase'] != 'uncertain' or slot.get('reason') not in ('ValueError','TimeoutExpired')
                    or not isinstance(slot.get('runtime'), dict) or type(slot.get('native_pid')) is not int):
                continue
            try:
                task = _task(state, slot['task'])
                if not managed(config, task) or task.get('owner') or task['state'] not in ('queued', 'handed_off'):
                    continue
                if slot['binding'] != binding(profile(config, slot['provider'], slot['account'])):
                    raise ValueError('pool profile changed')
                role = {key: slot[key] for key in ('provider', 'account', 'model', 'effort')}
                spec = _spec(config_path, state, config, task, role, slot['owner'])
                native = _verify(slot, task, spec, learn_hint=True)
                if not _hint_guard(slot, native):
                    raise ValueError('uncertain native is no longer pristine and unattached')
                slot.update(phase='prepared', turn_baseline=0, native_verified_at=time.time())
                slot.pop('reason', None)
                repaired += 1
            except Exception as error:
                slot['reason'] = type(error).__name__
            slot['updated_at'] = time.time()
            _write(state, manifest)
    return {**status(config, state), 'rechecked': repaired}


def _candidate(config, state, manifest, task, role):
    if not managed(config, task) or role.get('provider') not in ('codex', 'claude') or role.get('account') not in ('team', 'personal'):
        raise ValueError('pool requires a selected canonical leader identity')
    key = _key(task['team'], role['provider'], role['account'])
    slot = manifest['slots'].get(key)
    if not slot or slot['phase'] != 'prepared':
        raise ValueError('selected pool slot is not prepared; do not use another account')
    if (any(slot[k] != role.get(k, 'medium' if k == 'effort' else None) for k in ('provider', 'account', 'model', 'effort'))
            or slot['binding'] != binding(profile(config, role['provider'], role['account']))):
        raise ValueError('selected pool policy/profile differs from prepared native')
    spec = _spec(slot['spec']['env']['TEAMKIT_CONFIG'], Path(state).resolve(), config, task, role, slot['owner'])
    _verify(slot, task, spec)
    return slot


def preview(config, state, task, role):
    if not enabled(config):
        raise ValueError('session pool disabled')
    with _locked(state, create=False):
        return copy.deepcopy(_candidate(config, state, _read(state), task, role))


def reserve(config, state, task, role):
    if not enabled(config):
        raise ValueError('session pool disabled')
    with _locked(state):
        manifest = _read(state)
        if _task(state, task['id']) != task or task.get('owner') or task['state'] not in ('queued', 'handed_off'):
            raise ValueError('canonical task is owned or changed; preserve current execution')
        slot = _candidate(config, state, manifest, task, role)
        slot.update(phase='reserved', expected_attempt=task.get('attempt_count', 0)+1, updated_at=time.time())
        _write(state, manifest)
        return copy.deepcopy(slot)


def activate(config, state, slot, task, spec):
    if not enabled(config):
        raise ValueError('session pool disabled')
    with _locked(state):
        manifest = _read(state)
        current = manifest['slots'].get(_key(slot['team'], slot['provider'], slot['account']))
        actual = _task(state, task['id'])
        if (current != slot or current['phase'] != 'reserved' or actual != task
                or actual.get('owner') != slot['owner'] or actual['state'] not in ('claimed', 'launching')
                or actual.get('attempt_count') != slot['expected_attempt']
                or any(actual.get(k) != slot[k] for k in ('provider', 'account', 'model', 'effort'))):
            raise ValueError('reserved pool lease does not match official task claim')
        _verify(slot, task, spec)
        current.update(phase='leased', updated_at=time.time(), native_verified_at=time.time())
        _write(state, manifest)
        return {**slot['runtime'], 'pool_slot': _key(slot['team'], slot['provider'], slot['account']),
                'pool_generation': slot['generation'], 'pool_owner': slot['owner'],
                'pool_empty_prompt': slot.get('empty_prompt')}


def mark_uncertain(state, slot, reason):
    with _locked(state):
        manifest = _read(state)
        current = manifest['slots'].get(_key(slot['team'], slot['provider'], slot['account']))
        if not current or current['owner'] != slot['owner'] or current['generation'] != slot['generation']:
            raise ValueError('pool generation changed')
        current.update(phase='uncertain', reason=str(reason)[:160], updated_at=time.time())
        _write(state, manifest)


def _wait_for_tab(space,tab,terminal):
    deadline=time.monotonic()+3
    while True:
        remaining=deadline-time.monotonic()
        if remaining<=0:raise ValueError('prepared native tab adoption is pending; keep the existing native')
        reply=subprocess.run(['herdr-app','ls'],capture_output=True,text=True,timeout=remaining,check=True)
        data=json.loads(reply.stdout)
        if any(s.get('id')==space and t.get('id')==tab and any(r.get('terminal')==terminal for r in t.get('terminals',[]))
               for s in data.get('spaces',[]) for t in s.get('tabs',[])):
            return
        if time.monotonic()>=deadline:raise ValueError('prepared native tab adoption is pending; keep the existing native')
        time.sleep(.05)


def present(config, state, only=None):
    """Label only pool-created terminals and rename their tabs without selecting them."""
    if not enabled(config):
        return 0
    linked = json.loads(Path(config['session_pool']['bindings']).read_text())
    count = 0
    with _locked(state):
        for slot in _read(state)['slots'].values():
            if only and (slot['team'],slot['provider'],slot['account'])!=only:continue
            space = linked['teams'][slot['team']].get('space')
            if not space or slot['phase'] not in ('prepared', 'reserved', 'leased'):
                continue
            actor = {'id': slot['task'], 'team': slot['team'], 'root': slot['root'], 'provider': slot['provider'],
                     'owner': slot['owner'], 'runtime': slot['runtime']}
            CoreRuntime(actor)._native(slot['name'])
            tab = 'tbpool'+slot['owner'][:16]
            label = display_name(slot['provider'], slot['account'], product=slot['team'],
                                 aliases=config.get('operations', {}).get('staff_aliases'))
            _call('label', slot['terminal'], 'herdr.space='+space, 'herdr.tab='+tab, 'ui.display-name='+label)
            _wait_for_tab(space,tab,slot['terminal'])
            subprocess.run(['herdr-app', 'do', 'tab.rename', json.dumps({'id': tab, 'name': label})],
                           capture_output=True, text=True, timeout=5, check=True)
            count += 1
    return count


def verify_leased(config,state,task):
    """A fresh request must still match the immutable native profile generation."""
    if not managed(config,task):raise ValueError('canonical pool leader required')
    with _locked(state,create=False):
        slot=_read(state)['slots'].get(task.get('runtime',{}).get('pool_slot'))
        if (not slot or slot['phase']!='leased' or slot['owner']!=task.get('owner')
                or slot['generation']!=task['runtime'].get('pool_generation')
                or slot['binding']!=binding(profile(config,task['provider'],task['account']))):
            raise ValueError('active pool generation or profile changed')
        role={k:task[k] for k in ('provider','account','model','effort')}
        spec=_spec(slot['spec']['env']['TEAMKIT_CONFIG'],Path(state).resolve(),config,task,role,slot['owner'])
        return _verify(slot,task,spec,pristine=False)
