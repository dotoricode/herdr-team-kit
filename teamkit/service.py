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
    from .context_handoff import directory, read, source_fingerprint
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
    return {'schema_version': 1, 'label': label(state), 'alive': alive,
            'heartbeat_age_seconds': age, 'heartbeat_fresh': age <= 15,
            'source_matches': heartbeat.get('source_fingerprint') == source_fingerprint(),
            'source_fingerprint': source_fingerprint(), 'heartbeat': heartbeat,
            'terminal': heartbeat.get('terminal'),
            'reason': '' if alive and age <= 15 else 'supervisor missing or heartbeat stale'}


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
    if args.action == 'status':
        return status(args.config, args.state)
    if args.action == 'install':
        return install(args.config, args.state)
    if args.action == 'guard':
        return guard(args.config, args.state, args.once)
    raise ValueError('unknown service action')
