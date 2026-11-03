"""Explicit registration of existing workspace sessions; no inherited ownership."""
import json
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
            result.append(dict(terminal=address, registration_required=True,
                               reason='workspace validation failed', error_type=type(error).__name__))
    return result


def register(config_path, state, team, terminal, approved=False):
    require_managed()
    if approved is not True:
        raise ValueError('explicit operator approval required for workspace registration')
    config = load(config_path)
    proof = observe(config, state, team, terminal)
    queue = TicketStore(state)
    try:
        with queue.transaction():
            previous = next((r for r in rows(queue.db) if r['terminal'] == terminal), None)
            if previous:
                raise ValueError('workspace registration already exists; inspect original receipt, never resend')
            owner, nonce = uuid.uuid4().hex, uuid.uuid4().hex
            task = 'WORKSPACE-' + owner
            actor = {k:proof[k] for k in ('terminal', 'pid', 'provider', 'account', 'model', 'effort', 'profile_binding', 'root', 'product')}
            actor.update(owner=owner, task=task, roles=['leader', 'investigator', 'implementer', 'reviewer'],
                         runtime=dict(backend='core', name=task, native_name=proof['native_name'], terminal=terminal,
                                      native_pid=proof['pid'], session_cwd=proof['root']))
            record = dict(terminal=terminal, actor=actor, phase='registering', nonce=nonce,
                          created_at=time.time(), expires_at=time.time()+TTL, proof=proof)
            save(queue.db, record)
        # Durable intent precedes label/transport changes. No retry after partial failure.
        latest = observe(config, state, team, terminal)
        if latest != proof:
            raise ValueError('workspace changed before registration')
        _call('label', terminal, 'teamkit.owner='+owner, 'teamkit.task='+task, 'teamkit.name='+task)
        receipt = [sys.executable, str(Path(__file__).resolve().parent.parent/'teamkit_cli.py'),
                   '--config', str(config_path), '--state', str(state), 'pool', 'workspace-receipt',
                   '--team', team, '--terminal', terminal, '--expected-owner', owner, '--delivery-id', nonce]
        text = ('Workspace registration only. Do not start product work or alter existing drafts, tasks or files. '
                'Confirm this new registration with the exact command below, then wait. '
                'Previous task ownership is not inherited.\n'+shlex.join(receipt))
        runtime = CoreRuntime(dict(id=task, owner=owner, provider=proof['provider'], runtime=actor['runtime']))
        def guard():
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
