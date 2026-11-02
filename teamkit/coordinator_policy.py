"""Narrow resumed-Codex policy observation, never model selection or restart.

Status is a local native command, not a peer message. Busy/final guards only
read owner-private cache/journal evidence. The cache is not attestation against
an uncooperative process that can rewrite the same user's private state.
"""
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
from datetime import datetime

from .config import binding
from .context_observation import _rollout_path
from .core_runtime import _call, _model_effort, require_managed
from .prompt_state import empty_input
from .tickets import digest


FLAGS = {'--no-daemon', '--no-alt-screen', '--approve-for-me'}


def _path(state):
    return Path(state).resolve().parent / 'context-handoffs' / 'coordinator-policy.json'


def _basis(config, record, pr):
    return dict(terminal=record['terminal'], pid=record['native_pid'], owner=record['nonce'],
                session=record.get('context_session_id'), profile_binding=binding(pr),
                account=record['account'], home=str(Path(pr['home']).resolve()),
                root=str(Path(config['coordinator']['root']).resolve()),
                model=pr['model'].casefold(), effort=pr.get('effort', 'low'),
                policy_digest=digest(config['coordinator']))


def _resume(argv, session):
    if (not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', session)
            or not isinstance(argv, list) or not argv or Path(argv[0]).name != 'codex'
            or argv.count('resume') != 1):
        raise ValueError('registered resume identity is unavailable')
    index = argv.index('resume')
    flags = [*argv[1:index], *argv[index+2:]]
    if (index+1 >= len(argv) or argv[index+1] != session
            or any(value not in FLAGS for value in flags) or len(flags) != len(set(flags))
            or not {'--no-daemon', '--no-alt-screen'}.issubset(flags)):
        raise ValueError('resume arguments are ambiguous or differ from the registered session')


def _status(text):
    # Only the final full native status block after a literal local /status.
    chunks = re.split(r'(?m)^/status\s*$', text)
    if len(chunks) < 2:
        raise ValueError('native local status block is missing')
    block = chunks[-1]
    if not re.search(r'(?m)^\s*>_ OpenAI Codex \(v[^\r\n]+\)\s*$', block):
        raise ValueError('native status formatter is unknown')
    def field(name):
        values = re.findall(r'(?m)^\s*'+re.escape(name)+r':\s*([^\r\n]+)$', block)
        if len(values) != 1:
            raise ValueError('native status '+name+' is missing or ambiguous')
        return values[0].strip()
    model = re.fullmatch(r'([A-Za-z0-9._-]+) \(reasoning (low|medium|high|xhigh|max), summaries [A-Za-z]+\)', field('Model'))
    account = re.fullmatch(r'([^\s()]+@[^\s()]+) \([^\r\n()]+\)', field('Account'))
    permissions = field('Permissions')
    if (not model or not account or field('Model provider') != 'openai'
            or not re.fullmatch(r'Workspace(?: \[[^\r\n\]]+\])? \(Approve for me\)', permissions)):
        raise ValueError('native model/account/permissions formatter is unknown or unsupported')
    return dict(model=model[1].casefold(), effort=model[2], account=account[1].casefold(),
                session=field('Session'), permissions='workspace-approve-for-me')


def _process_home(pid, argv, expected):
    # Never log, persist or return the process environment. Only compare allowed
    # HOME/CODEX_HOME values from the exact bound process, not another process.
    result = subprocess.run(['/bin/ps', 'eww', '-p', str(pid), '-o', 'command='],
                            capture_output=True, text=True, timeout=5, check=True)
    raw = result.stdout.strip()
    if len(raw.encode()) > 1024*1024 or not raw.startswith(' '.join(argv)+' '):
        raise ValueError('native process environment formatter is unavailable')
    environment = raw[len(' '.join(argv))+1:]
    values = {}
    for name in ('HOME', 'CODEX_HOME'):
        matches = re.findall(r'(?:^|\s)'+name+r'=([^\s]+)', environment)
        if len(matches) > 1:
            raise ValueError('native profile home is ambiguous')
        values[name] = matches[0] if matches else None
    home = values['CODEX_HOME']
    if home is None and values['HOME']:
        home = str(Path(values['HOME']) / '.codex')
    if not home or not Path(home).is_absolute() or Path(home).resolve() != Path(expected).resolve():
        raise ValueError('native process profile home differs from the expected binding')


def _journal(pr, session):
    path = _rollout_path(Path(pr['home']).resolve(), session)
    if path is None or path.is_symlink():
        raise ValueError('registered session journal is unavailable')
    with path.open('rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('session journal is not a regular file')
        header = stream.readline(1024*1024+1)
        if len(header) > 1024*1024 or not header.endswith(b'\n'):
            raise ValueError('session metadata is incomplete')
        first = json.loads(header)
        if (not isinstance(first, dict) or first.get('type') != 'session_meta'
                or not isinstance(first.get('payload'), dict) or first['payload'].get('id') != session):
            raise ValueError('journal belongs to a different session')
        start = max(stream.tell(), info.st_size-4*1024*1024)
        stream.seek(start)
        if start > len(header):
            stream.readline()  # Partial leading line is not a record.
        rows = stream.read(4*1024*1024).splitlines(keepends=True)
    latest = None
    for line in rows:
        if not line.endswith(b'\n'):
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError('session journal record format is unknown')
        if row.get('type') != 'turn_context':
            continue
        latest = row
    context = None
    if latest:
        data = latest.get('payload', {})
        if not isinstance(data, dict) or not isinstance(latest.get('timestamp'), str):
            raise ValueError('latest turn context formatter is unknown')
        stamp = datetime.fromisoformat(latest['timestamp'].replace('Z', '+00:00'))
        if (stamp.tzinfo is None or not isinstance(data.get('model'), str)
                or not isinstance(data.get('cwd'), str) or not Path(data['cwd']).is_absolute()
                or data.get('effort') not in ('low', 'medium', 'high', 'xhigh', 'max')):
            raise ValueError('latest turn context formatter is unknown')
        context = dict(at=stamp.timestamp(), model=data['model'].casefold(), effort=data['effort'],
                       root=str(Path(data['cwd']).resolve()))
    return dict(device=info.st_dev, inode=info.st_ino, minimum_size=info.st_size), context


def _read(path):
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError('policy cache path changed')
    with path.open('rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 64*1024):
            raise ValueError('policy cache ownership/format is untrusted')
        result = json.load(stream)
    if not isinstance(result, dict):
        raise ValueError('policy cache is not a record')
    return result


def _write(path, value):
    fd, name = tempfile.mkstemp(prefix='.coordinator-policy-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _cache_shape(cached):
    return (isinstance(cached, dict)
            and set(cached) == {'schema', 'phase', 'basis', 'cid', 'observed_at', 'controller', 'journal'}
            and cached['schema'] == 1 and cached['phase'] in ('verified', 'intent', 'uncertain')
            and isinstance(cached['basis'], dict) and cached['cid'] == digest(cached['basis'])
            and type(cached['observed_at']) in (int, float) and math.isfinite(cached['observed_at'])
            and 0 < cached['observed_at'] <= time.time()
            and isinstance(cached['controller'], str) and re.fullmatch(r't[0-9a-z]+', cached['controller']))


def _cached(config, state, record, pr, *, working=False):
    cached = _read(_path(state))
    basis = _basis(config, record, pr)
    if not _cache_shape(cached) or cached['phase'] != 'verified' or cached['basis'] != basis:
        raise ValueError('resumed coordinator policy cache is missing, unknown or mismatched')
    journal, context = _journal(pr, basis['session'])
    old = cached['journal']
    if (not isinstance(old, dict) or set(old) != {'device', 'inode', 'minimum_size'}
            or any(type(old[k]) is not int or old[k] < 0 for k in ('device', 'inode', 'minimum_size'))
            or any(journal[k] != old[k] for k in ('device', 'inode'))
            or journal['minimum_size'] < old['minimum_size']):
        raise ValueError('session journal changed; preserve the prior policy observation')
    # Earlier context may precede an interactive model selection. Only a new
    # actual working turn can corroborate the cached native /status selection.
    if working and (not context or context['at'] < cached['observed_at']):
        raise ValueError('current working turn policy is not yet observed')
    if context and context['at'] >= cached['observed_at']:
        if context['at'] > time.time()+1 or any(context[k] != basis[k] for k in ('model', 'effort', 'root')):
            raise ValueError('new turn context differs from the verified native policy')
    return dict(mode='resumed-status', cid=cached['cid'])


def _observe(config, state, record, pr, actor, native, name):
    path = _path(state)
    if path.parent.is_symlink():
        raise ValueError('policy cache directory changed')
    path.parent.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(path.with_suffix('.lock'), os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        basis = _basis(config, record, pr)
        if path.exists():
            prior = _read(path)
            if not _cache_shape(prior):
                raise ValueError('policy cache format changed; inspect before a new status command')
            if prior['phase'] in ('intent', 'uncertain'):
                raise ValueError('prior native status outcome unknown; inspect before another status command')
        current = actor._native(name, ready=True)
        _resume(current.get('foreground', {}).get('argv'), basis['session'])
        screen = _call('read', '--json', record['terminal'])
        if screen.get('complete') is not True or not empty_input(screen.get('text', '')):
            raise ValueError('native status requires a complete empty input screen')
        if current.get('turn') != native.get('turn'):
            raise ValueError('coordinator changed before native status observation')
        _process_home(basis['pid'], current['foreground']['argv'], basis['home'])
        journal, _ = _journal(pr, basis['session'])
        controller = require_managed()
        after = actor._native(name, ready=True)
        _resume(after.get('foreground', {}).get('argv'), basis['session'])
        if after.get('turn') != current.get('turn'):
            raise ValueError('coordinator changed while verifying the controller')
        screen = _call('read', '--json', record['terminal'])
        if screen.get('complete') is not True or not empty_input(screen.get('text', '')):
            raise ValueError('native input changed immediately before local status command')
        value = dict(schema=1, phase='intent', basis=basis, cid=digest(basis),
                     observed_at=time.time(), controller=controller, journal=journal)
        _write(path, value)
        try:
            env = dict(os.environ)
            env.pop('HC_TERMINAL_ID', None)
            # Fixed whitelist: no public raw-send/text parameter exists.
            condition = ['--if', 'idle'] if after['status']['state'] == 'idle' else []
            sent = subprocess.run(['hc', 'send', '--json', *condition, record['terminal'], '/status'],
                                  env=env, capture_output=True, text=True, timeout=5, check=True)
            ack = json.loads(sent.stdout)
            if (ack.get('started') is not False or ack.get('status') not in ('idle', 'done')
                    or ack.get('turn') != native.get('turn')):
                raise ValueError('native status may have started a model turn; preserve and inspect')
            prior_text = screen.get('text', '')
            for _ in range(10):
                screen = _call('read', '--json', record['terminal'])
                if screen.get('complete') is True and screen.get('text') != prior_text:
                    break
                time.sleep(.02)
            if screen.get('complete') is not True or not empty_input(screen.get('text', '')):
                raise ValueError('native status result or empty prompt is unconfirmed')
            if screen.get('text') == prior_text:
                raise ValueError('native status screen did not advance; prior output is not fresh policy evidence')
            observed = _status(screen['text'])
            expected_email = pr.get('identity', {}).get('email')
            if (not isinstance(expected_email, str) or observed['account'] != expected_email.casefold()
                    or any(observed[k] != basis[k] for k in ('model', 'effort', 'session'))):
                raise ValueError('native model/effort/account/session differs from the configured expected policy')
            final = actor._native(name, ready=True)
            _resume(final.get('foreground', {}).get('argv'), basis['session'])
            if final.get('turn') != native.get('turn'):
                raise ValueError('native turn changed during the status command')
            value.update(phase='verified', observed_at=time.time())
            _write(path, value)
        except BaseException:
            value['phase'] = 'uncertain'
            _write(path, value)
            raise
        return dict(mode='resumed-status', cid=value['cid'])
    finally:
        os.close(fd)


def _verify(config, state, record, pr, actor, native, name, *, allow_probe=False):
    argv = native.get('foreground', {}).get('argv')
    if not isinstance(argv, list) or any(not isinstance(x, str) for x in argv):
        raise ValueError('native argv unavailable')
    explicit = any(x in ('--model', '-m') or x.startswith(('--model=', '-m='))
                   or 'model_reasoning_effort' in x or x.startswith('model=') for x in argv)
    if explicit:
        if _model_effort('codex', argv) != (pr['model'], pr.get('effort', 'low')):
            raise ValueError('explicit native model/effort differs from expected policy')
        return dict(mode='argv', cid=digest(_basis(config, record, pr)))
    _resume(argv, record.get('context_session_id'))
    if (native.get('status') or {}).get('state') not in ('idle', 'done', 'working'):
        raise ValueError('resumed coordinator native state is unknown')
    if allow_probe:
        return _observe(config, state, record, pr, actor, native, name)
    return _cached(config, state, record, pr, working=(native.get('status') or {}).get('state') == 'working')


def verify(config, state, record, pr, actor, native, name, *, allow_probe=False):
    try:
        return _verify(config, state, record, pr, actor, native, name, allow_probe=allow_probe)
    except (KeyError, TypeError, subprocess.SubprocessError) as error:
        raise ValueError('native policy observation format/outcome unavailable; preserve and inspect') from error


def fence(config, state, record, pr, proof):
    """No native/status/process/auth calls: usable immediately before HC send."""
    if not isinstance(proof, dict) or set(proof) != {'mode', 'cid'}:
        raise ValueError('policy snapshot is unavailable')
    expected = digest(_basis(config, record, pr))
    if proof['cid'] != expected or proof['mode'] not in ('argv', 'resumed-status'):
        raise ValueError('coordinator policy snapshot changed')
    if proof['mode'] == 'resumed-status' and _cached(config, state, record, pr) != proof:
        raise ValueError('coordinator policy cache CID changed')
