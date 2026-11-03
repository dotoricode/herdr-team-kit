"""Explicit staff startup and receipt, separate from product tickets and UI work."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import uuid

from . import admission, docsstaff, providers
from .config import binding, cached_quota, load, profile
from .core_runtime import CoreRuntime, _call, _model_effort, _process_matches, _detector_matches, claude_gui_settings, require_managed
from .policy import eligible
from .resources import ResourceLedger, observe_native
from .runtime import AUTH_ENV, launch_spec, profile_home_matches
from .store import Store
from .tickets import TicketStore, file_digest
from .workers import _atlassian_registered, _hint, _policy_task


TABLE = 'staff_actors'


def _schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS staff_actors (seat TEXT PRIMARY KEY, body TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS staff_resume_proofs (seat TEXT PRIMARY KEY, body TEXT NOT NULL)')


def records(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return []
    return [json.loads(row[0]) for row in db.execute('SELECT body FROM staff_actors ORDER BY seat')]


def _write(db, record):
    db.execute('INSERT OR REPLACE INTO staff_actors VALUES (?,?)',
               (record['seat'], json.dumps(record)))


def _get(db, seat):
    return next((r for r in records(db) if r['seat'] == seat), None)


def _contract(config, department, provider):
    ticket = dict(product=department, role='documenter')
    staff = docsstaff.policy(config, ticket)
    if staff is None:
        ticket['role'] = 'investigator'
        staff = docsstaff.policy(config, ticket)
    if staff is None or provider + '/personal' not in staff['profiles']:
        raise ValueError('staff seat is not explicitly configured; UI and specialist review are separate')
    from .departments import execution_config
    from .routing import execution_candidates, execution_policy
    effective = execution_config(config)
    task = _policy_task(effective, ticket)
    if task is None:
        raise ValueError('staff requires an existing department execution role')
    role = execution_policy(effective, task)
    candidates = execution_candidates(docsstaff.routing_config(effective, role), task, role, time.time())
    selected = next((c for c in candidates or [] if c['provider'] == provider and c['account'] == 'personal'), None)
    if selected is None or role.get('action') != 'dispatch':
        raise ValueError('staff provider is outside existing model policy')
    root = effective['teams'][department]['root']
    if not Path(root).is_dir():
        raise ValueError('staff working root must exist')
    return ticket, task['role'], root, selected


def _preflight(config, state, department, provider):
    ticket, role, root, selected = _contract(config, department, provider)
    pr = profile(config, provider, 'personal', selected)
    if not _atlassian_registered(provider, pr):
        raise ValueError('staff profile requires registered enabled Atlassian MCP')
    providers.identity(provider, pr)
    store = Store(state)
    try:
        quota = cached_quota(config, store, provider, 'personal')
    finally:
        store.close()
    if not eligible(quota, time.time(), config['quota_ttl_seconds']):
        raise ValueError('staff personal quota is unavailable, expired or limited')
    return ticket, role, root, selected, pr, dict(identity_checked_at=time.time(),
        identity_source='host-fresh-provider', quota_observed_at=quota['observed_at'], mcp_registered=True)


def _native(record, idle=True):
    actor = record['actor']
    task = dict(id=actor['task'], owner=actor['owner'], provider=actor['provider'], runtime=actor['runtime'])
    native = CoreRuntime(task)._native(actor['runtime']['name'], ready=idle)
    if (native['foreground']['pid'] != actor['pid'] or
            _model_effort(actor['provider'], native['foreground'].get('argv')) != (actor['model'], actor['effort'])):
        raise ValueError('staff native PID/model/effort changed')
    if idle:
        from .prompt_state import empty_input
        screen = _call('read', '--json', actor['terminal'])
        if screen.get('complete') is not True or not empty_input(screen.get('text', ''), actor.get('empty_prompt')):
            raise ValueError('staff input is not empty; preserve the existing draft or dialog')
    return native


def _journal(pr, provider, session, root, filename):
    if not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', session):
        raise ValueError('exact native session ID required')
    path = Path(filename)
    home = Path(pr['home']).resolve()
    if (not path.is_absolute() or path.is_symlink() or not path.resolve().is_relative_to(home)
            or not path.is_file() or session not in path.name):
        raise ValueError('session journal must belong to the exact configured profile')
    found = False
    with path.open() as stream:
        # Only metadata is retained. No transcript text is stored in the staff ledger.
        for line in stream:
            row = json.loads(line)
            metadata = row.get('payload', {}) if provider == 'codex' and row.get('type') == 'session_meta' else row
            sid = metadata.get('id') if provider == 'codex' else metadata.get('sessionId')
            if sid == session and metadata.get('cwd') == root and metadata.get('isSidechain') is not True:
                found = True
                break
    if not found:
        raise ValueError('session journal does not prove the requested SID and working root')
    return dict(path=str(path.resolve()), digest=file_digest(path))


def _pid_stopped(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    raise ValueError('previous native PID still exists; preserve it before resume')


def checkpoint(config_path, state, department, provider, terminal, pid, session, journal):
    """Capture an idle, empty original native before a separately authorized normal stop."""
    require_managed()
    config = load(config_path)
    _, _, root, _, pr, checks = _preflight(config, state, department, provider)
    from .prompt_state import empty_input
    native = _call('get', terminal)
    screen = _call('read', '--json', terminal)
    foreground = native.get('foreground') or {}
    if (type(pid) is not int or pid <= 0 or native.get('address') != terminal or native.get('state') != 'live'
            or foreground.get('pid') != pid or (native.get('status') or {}).get('state') not in ('idle', 'done')
            or not _process_matches(provider, foreground.get('name'))
            or not _detector_matches(provider, (native.get('status') or {}).get('app'))
            or foreground.get('cwd', native.get('cwd')) != root
            or screen.get('complete') is not True or not empty_input(screen.get('text', ''))):
        raise ValueError('resume checkpoint requires the exact idle PID, working root and empty input')
    # Inspect only selected environment values. Never return the raw ps output.
    result = subprocess.run(['ps', 'eww', '-p', str(pid), '-o', 'command='], capture_output=True,
                            text=True, timeout=5, check=True)
    env = dict(re.findall(r'(?:^|\s)([A-Z_][A-Z0-9_]*)=([^\s]*)', result.stdout))
    key = 'CODEX_HOME' if provider == 'codex' else 'CLAUDE_CONFIG_DIR'
    actual_home = env.get(key) or str(Path.home() / ('.codex' if provider == 'codex' else '.claude'))
    if Path(actual_home).resolve() != Path(pr['home']).resolve() or any(env.get(key) for key in AUTH_ENV):
        raise ValueError('original native profile or authentication namespace is unverified')
    session_record = None
    if provider == 'claude':
        path = Path(pr['home']) / 'sessions' / (str(pid) + '.json')
        if path.is_symlink():
            raise ValueError('native session record must not be a symlink')
        original = json.loads(path.read_text())
        if original.get('sessionId') != session or original.get('cwd') != root:
            raise ValueError('official PID session record does not match the original conversation')
        session_record = dict(sessionId=session, cwd=root, path=str(path), digest=file_digest(path))
    proof = dict(seat=department + '/' + provider, terminal=terminal, pid=pid, session=session,
                 profile_binding=binding(pr), journal=_journal(pr, provider, session, root, journal),
                 captured_at=time.time(), native_turn=native.get('turn'), checks=checks,
                 native_session_record=session_record)
    after = _call('get', terminal)
    after_screen = _call('read', '--json', terminal)
    if (any(after.get(key) != native.get(key) for key in ('address', 'state', 'foreground', 'status', 'turn'))
            or after_screen.get('complete') is not True or after_screen.get('text') != screen.get('text')):
        raise ValueError('original native changed during checkpoint')
    store = Store(state)
    try:
        _schema(store.db)
        with store.transaction():
            if _get(store.db, proof['seat']):
                raise ValueError('staff seat already exists; do not create a second native')
            store.db.execute('INSERT OR REPLACE INTO staff_resume_proofs VALUES (?,?)',
                             (proof['seat'], json.dumps(proof)))
        return proof
    finally:
        store.close()


def _resume(db, seat, session, pr, provider, root):
    row = db.execute('SELECT body FROM staff_resume_proofs WHERE seat=?', (seat,)).fetchone()
    if session is None:
        if row:
            raise ValueError('original conversation checkpoint requires the exact --resume SID')
        return None
    if not row:
        raise ValueError('resume requires an official original-native checkpoint')
    proof = json.loads(row[0])
    if proof['session'] != session or proof['profile_binding'] != binding(pr):
        raise ValueError('resume SID or personal profile differs from checkpoint')
    _pid_stopped(proof['pid'])
    native = _call('get', proof['terminal'])
    if (native.get('foreground') or {}).get('pid') == proof['pid']:
        raise ValueError('original native is still attached; do not duplicate the session')
    if _journal(pr, provider, session, root, proof['journal']['path']) != proof['journal']:
        raise ValueError('original journal changed since empty-input checkpoint; inspect before resume')
    # A separately started resume is not permission to start another copy.
    listing = _call('ls', '--json')
    for terminal in listing['terminals']:
        if terminal.get('state') == 'live':
            observed = _call('get', terminal['address'])
            if session in ((observed.get('foreground') or {}).get('argv') or []):
                raise ValueError('requested session is already resumed in another native')
    return proof


def prepare(config_path, state, department, provider, resume=None):
    require_managed()
    config_bytes = Path(config_path).read_bytes()
    config = load(config_path)
    if config.get('operations', {}).get('enabled') is not True or config.get('runtime_backend') != 'core':
        raise ValueError('staff preparation requires enabled core operations')
    ticket, policy_role, root, selected, pr, checks = _preflight(config, state, department, provider)
    seat = department + '/' + provider
    store = Store(state)
    queue = TicketStore(state)
    intent = False
    try:
        _schema(store.db)
        previous = _get(store.db, seat)
        if previous:
            if resume is not None and previous.get('session') != resume:
                raise ValueError('existing staff seat belongs to a different session; preserve it')
            return {**previous, 'next_action': 'inspect existing actor; never relaunch or resend preparation'}
        _resume(store.db, seat, resume, pr, provider, root)
        if docsstaff.actors(config, queue.db, ticket, provider):
            raise ValueError('an official staff actor already exists; preserve its session')
        owner = uuid.uuid4().hex
        ident = 'STAFF-' + hashlib.sha256(seat.encode()).hexdigest()[:24]
        name = 'tk-' + owner[:20]
        from .__main__ import instructions_basis
        from .departments import organization
        staged = store.create(ident, department, policy_role, root,
            instructions_basis(organization(config, department)),
            'Wait as an explicitly configured staff actor. No product work is authorized by preparation.',
            'instructions', {'kind': 'general', 'risks': []})
        executor = dict(task=ident, owner=owner, provider=provider, account='personal',
            product=department, roles=[ticket['role']], root=root, model=selected['model'],
            effort=selected.get('effort', 'medium'), profile_binding=binding(pr), operation_actor=True,
            terminal=None, pid=None, empty_prompt=None, runtime=dict(backend='core', name=name,
                profile_binding=binding(pr), identity_checked=False))
        attempt = 'staff:' + owner
        record = dict(seat=seat, phase='launching', actor=executor, resource_attempt=attempt,
                      session=resume, checks=checks, config_digest=hashlib.sha256(config_bytes).hexdigest(),
                      receipt_nonce=uuid.uuid4().hex, created_at=time.time())
        observation = observe_native()
        with store.transaction():
            if _get(store.db, seat) or store.get(ident) != staged or staged.get('owner'):
                raise ValueError('staff seat changed before launch intent')
            docsstaff.check(config, store.db, ticket, executor)
            if resume and any(r.get('session') == resume for r in records(store.db)):
                raise ValueError('another staff seat already reserved this original session')
            if Path(config_path).read_bytes() != config_bytes:
                raise ValueError('staff configuration changed before launch')
            admission.configure(store.db, config['operations'].get('resource_policy'))
            admission.reserve_in_transaction(store.db, attempt, ident, provider + '/personal', [],
                config['operations'].get('resource_policy'), observation)
            staged.update(state='staff_preparing', staff_actor=True, owner=owner, provider=provider,
                          account='personal', model=executor['model'], effort=executor['effort'],
                          attempt_count=1, runtime=executor['runtime'])
            store._save(staged, 'staff_launch_intent', checks)
            _write(store.db, record)
        intent = True

        def created(detail):
            with store.transaction():
                current = _get(store.db, seat)
                if current['actor']['owner'] != owner or current['phase'] != 'launching':
                    raise ValueError('staff launch identity changed')
                actor = current['actor']
                actor['runtime'].update(detail)
                actor['terminal'] = detail.get('terminal')
                actor['pid'] = detail.get('native_pid')
                if actor['pid']:
                    ResourceLedger(store.db).bind(attempt, dict(terminal=actor['terminal'], pid=actor['pid'], owner=owner))
                task = store.get(ident)
                task['runtime'] = actor['runtime']
                store._save(task, 'staff_native_created', dict(terminal=actor['terminal'], pid=actor['pid']))
                _write(store.db, current)

        spec = launch_spec(provider, pr, resume)
        if provider == 'claude':
            spec = claude_gui_settings(spec, str(config_path), str(state), 'personal',
                                      config.get('claude_display_commands', {}).get('personal', ''))
        spec['args'] += ['--add-dir', str(Path(state).resolve().parent)]
        spec['env'].update(TEAMKIT_TASK=ident, TEAMKIT_OWNER=owner,
                          TEAMKIT_CONFIG=str(Path(config_path).resolve()), TEAMKIT_STATE=str(Path(state).resolve()))
        if Path(config_path).read_bytes() != config_bytes:
            raise ValueError('staff configuration changed before native launch')
        _resume(store.db, seat, resume, pr, provider, root)
        CoreRuntime().launch(staged, spec, name, created)
        record = _get(store.db, seat)
        actor = record['actor']
        native = CoreRuntime(dict(id=ident, owner=owner, provider=provider, runtime=actor['runtime']))._native(name, ready=True)
        actor['empty_prompt'] = _hint(actor, native)
        record['actor'] = actor
        _native(record)
        with store.transaction():
            current = _get(store.db, seat)
            if current['phase'] != 'launching' or current['actor']['owner'] != owner:
                raise ValueError('staff changed before preparation message')
            current.update(actor=actor, phase='awaiting_receipt', message_intent_at=time.time())
            _write(store.db, current)
        command = [sys.executable, str(Path(__file__).resolve().parent.parent / 'teamkit_cli.py'),
            '--config', str(Path(config_path).resolve()), '--state', str(Path(state).resolve()),
            'staff', 'receipt', '--department', department, '--provider', provider,
            '--owner', owner, '--nonce', record['receipt_nonce']]
        message = ('Staff preparation only. Do not perform product work, edit files, create agents, claim a queue, '
            'operate a UI, post externally or change accounts. Preserve the resumed conversation and prior input. '
            'Run this exact receipt command from your actual managed terminal, then wait without further work:\n'
            + shlex.join(command))

        def guard():
            if Path(config_path).read_bytes() != config_bytes:
                raise ValueError('staff config changed; preparation input not sent')
            current = _get(store.db, seat)
            if current['phase'] != 'awaiting_receipt' or current['actor'] != actor:
                raise ValueError('staff identity changed before preparation input')
            _, _, _, _, fresh_pr, _ = _preflight(config, state, department, provider)
            if binding(fresh_pr) != actor['profile_binding']:
                raise ValueError('staff profile changed before preparation input')
            _native(current)
            if Path(config_path).read_bytes() != config_bytes:
                raise ValueError('staff configuration changed during final identity check')
        runtime = CoreRuntime(dict(id=ident, owner=owner, provider=provider, runtime=actor['runtime']))
        accepted = runtime.accept(name, message, before_send=guard)
        with store.transaction():
            current = _get(store.db, seat)
            current['preparation_transport'] = accepted
            _write(store.db, current)
        return _get(store.db, seat)
    except BaseException as error:
        if intent:
            with store.transaction():
                current = _get(store.db, seat)
                current.update(phase='uncertain', error=type(error).__name__)
                _write(store.db, current)
        raise
    finally:
        queue.close()
        store.close()


def receipt(config_path, state, department, provider, owner, nonce):
    config = load(config_path)
    _contract(config, department, provider)
    store = Store(state)
    try:
        record = _get(store.db, department + '/' + provider)
        if (not record or record['phase'] not in ('awaiting_receipt', 'received')
                or record['actor']['owner'] != owner or record['receipt_nonce'] != nonce
                or os.environ.get('HC_TERMINAL_ID') != record['actor']['terminal']
                or os.environ.get('TEAMKIT_OWNER') != owner or os.environ.get('TEAMKIT_TASK') != record['actor']['task']
                or not profile_home_matches(provider, profile(config, provider, 'personal')['home'])):
            raise ValueError('staff receipt must come from the exact native owner and personal profile')
        native = _native(record, idle=False)
        with store.transaction():
            if _get(store.db, record['seat']) != record:
                raise ValueError('staff receipt changed')
            record.update(phase='received', receipt=dict(terminal=native['address'], pid=native['foreground']['pid'],
                owner=owner, nonce=nonce, at=time.time()))
            _write(store.db, record)
        return record
    finally:
        store.close()


def finalize(config_path, state, department, provider):
    require_managed()
    config_bytes = Path(config_path).read_bytes()
    config = load(config_path)
    _, _, _, selected, pr, checks = _preflight(config, state, department, provider)
    store = Store(state)
    try:
        record = _get(store.db, department + '/' + provider)
        if not record or record['phase'] not in ('received', 'ready', 'uncertain') or not record.get('receipt'):
            raise ValueError('actual native staff receipt is required before registration')
        actor = record['actor']
        if (actor['profile_binding'] != binding(pr) or
                (actor['model'], actor['effort']) != (selected['model'], selected.get('effort', 'medium'))):
            raise ValueError('staff profile/model policy changed before registration')
        _native(record)
        with store.transaction():
            if _get(store.db, record['seat']) != record or Path(config_path).read_bytes() != config_bytes:
                raise ValueError('staff configuration or receipt changed before registration')
            actor['runtime'].update(identity_checked=True, identity_source='host-fresh-provider')
            record.update(phase='ready', checks=checks, ready_at=time.time())
            task = store.get(actor['task'])
            task.update(state='staff_ready', runtime=actor['runtime'])
            store._save(task, 'staff_registered', checks)
            ResourceLedger(store.db).release(record['resource_attempt'], execution_stopped=True)
            _write(store.db, record)
        return record
    finally:
        store.close()


def executors(state):
    queue = TicketStore(state, read_only=True)
    try:
        return [r['actor'] for r in records(queue.db) if r['phase'] == 'ready']
    finally:
        queue.close()


def parser(commands):
    item = commands.add_parser('staff')
    item.add_argument('action', choices=('prepare', 'checkpoint', 'receipt', 'finalize', 'status'))
    item.add_argument('--department', required=True)
    item.add_argument('--provider', choices=('codex', 'claude'), required=True)
    item.add_argument('--resume')
    item.add_argument('--owner')
    item.add_argument('--nonce')
    item.add_argument('--terminal')
    item.add_argument('--pid', type=int)
    item.add_argument('--session')
    item.add_argument('--journal')


def handle(args):
    if args.action == 'prepare':
        return prepare(args.config, args.state, args.department, args.provider, args.resume)
    if args.action == 'checkpoint':
        return checkpoint(args.config, args.state, args.department, args.provider,
                          args.terminal, args.pid, args.session, args.journal)
    if args.action == 'receipt':
        return receipt(args.config, args.state, args.department, args.provider, args.owner, args.nonce)
    if args.action == 'finalize':
        return finalize(args.config, args.state, args.department, args.provider)
    queue = TicketStore(args.state, read_only=True)
    try:
        return _get(queue.db, args.department + '/' + args.provider) or {'phase': 'not_prepared'}
    finally:
        queue.close()
