"""launchd guards the existing HC supervisor; it does not execute model work."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import time


def label(state):
    return 'dev.teamkit.supervisor.' + hashlib.sha256(str(Path(state).resolve()).encode()).hexdigest()[:12]


def definition(config_path, state, cli=None, python=None):
    cli = Path(cli or Path(__file__).resolve().parent.parent / 'teamkit_cli.py').resolve()
    state = Path(state).resolve()
    return {'Label': label(state), 'ProgramArguments': [python or sys.executable, str(cli),
            '--config', str(Path(config_path).resolve()), '--state', str(state), 'service', 'guard'],
            'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 5,
            'ProcessType': 'Background', 'WorkingDirectory': str(cli.parent),
            'StandardOutPath': str(state.parent / 'supervisor-guard.log'),
            'StandardErrorPath': str(state.parent / 'supervisor-guard.log'),
            'EnvironmentVariables': {'PATH': os.environ.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin')}}


def status(config_path, state):
    from .context_handoff import directory, read, source_fingerprint, dispatch_status
    heartbeat = read(directory(state) / 'supervisor.json') or {}
    alive = False
    native = None
    if heartbeat.get('terminal'):
        from .core_runtime import _call
        try:
            native = _call('get', heartbeat['terminal'])
            foreground = native.get('foreground') or {}
            alive = (native.get('state') == 'live' and foreground.get('pid') == heartbeat.get('pid')
                     and 'auto-context' in foreground.get('argv', [])
                     and 'serve' in foreground.get('argv', []))
        except Exception:
            pass
    age = max(0, time.time() - heartbeat.get('observed_at', 0))
    from .config import load
    dispatch = dispatch_status(state)
    required = load(config_path).get('operations',{}).get('enabled') is True
    dispatch_healthy = (not required or dispatch.get('pid') == heartbeat.get('pid')
        and dispatch.get('phase') in ('idle', 'working')
        and dispatch.get('last_success_at') is not None
        and dispatch.get('heartbeat_age_seconds', float('inf')) <= 15)
    return {'schema_version': 1, 'label': label(state), 'alive': alive,
            'heartbeat_age_seconds': age, 'heartbeat_fresh': age <= 15,
            'dispatch': dispatch, 'dispatch_healthy': dispatch_healthy,
            'source_matches': heartbeat.get('source_fingerprint') == source_fingerprint(),
            'source_fingerprint': source_fingerprint(), 'heartbeat': heartbeat,
            'terminal': heartbeat.get('terminal'),
            'reason': '' if alive and age <= 15 and dispatch_healthy else 'supervisor/dispatcher missing, degraded or heartbeat stale'}


def install(config_path, state, directory=None):
    destination = Path(directory or Path.home() / 'Library/LaunchAgents') / (label(state) + '.plist')
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = plistlib.dumps(definition(config_path, state))
    if destination.exists():
        original = destination.read_bytes()
        if original == data:
            return {'path': str(destination), 'changed': False}
        backup = destination.with_name(destination.name + '.backup-' + str(time.time_ns()))
        backup.write_bytes(original)
        backup.chmod(0o600)
    temporary = destination.with_name(destination.name + '.tmp-' + str(os.getpid()))
    with temporary.open('xb') as output:
        os.chmod(temporary, 0o600)
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(destination)
    return {'path': str(destination), 'changed': True}


def guard(config_path, state, once=False):
    """Native context.ensure is the single existing launch/ownership authority."""
    from .context_handoff import ensure, locked
    state = Path(state).resolve()
    stopping = False
    def stop(*unused):
        nonlocal stopping
        stopping = True
    old = signal.signal(signal.SIGTERM, stop)
    try:
        with locked(state.parent / 'supervisor-guard'):
            while not stopping:
                try:
                    current = status(config_path, state)
                    if not current['alive']:
                        ensure(config_path, state)
                    # A live but stale supervisor is reported, never killed blindly.
                    message = {**status(config_path, state), 'guard_pid': os.getpid(), 'observed_at': time.time()}
                except Exception as error:
                    message = {'guard_pid': os.getpid(), 'observed_at': time.time(),
                               'alive': False, 'reason': type(error).__name__ + ': ' + str(error)}
                from .context_handoff import write
                write(state.parent / 'supervisor-guard.json', message)
                if once:
                    return message
                for _ in range(50):
                    if stopping:
                        break
                    time.sleep(.1)
    finally:
        signal.signal(signal.SIGTERM, old)


def handle(args):
    if args.action == 'rebind-prepare':
        from .app_coordinator import prepare_rebind
        if not args.checkpoint_file or not args.owner or not args.pid or not args.session:
            raise ValueError('checkpoint-file, owner, original pid and session are required')
        return prepare_rebind(args.config,args.state,args.checkpoint_file,args.owner,args.pid,args.session)
    if args.action == 'rebind-complete':
        from .app_coordinator import complete_rebind
        if not args.intent or not args.owner or not args.pid:
            raise ValueError('intent, original owner and new pid are required')
        return complete_rebind(args.config,args.state,args.intent,args.owner,args.pid)
    if args.action == 'status':
        return status(args.config, args.state)
    if args.action == 'install':
        return install(args.config, args.state)
    if args.action == 'guard':
        return guard(args.config, args.state, args.once)
    raise ValueError('unknown service action')
