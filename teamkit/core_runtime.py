"""Explicit herdr-core execution boundary; task ownership stays in the ledger."""
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import shlex
import tomllib
import time

from .runtime import RuntimeError


ADDRESS = re.compile(r't[0-9a-z]+\Z')


def claude_gui_settings(spec,config_path,state,account,renderer=''):
    command=[sys.executable,str(Path(__file__).resolve().parent.parent/'teamkit_cli.py'),
             '--config',str(config_path),'--state',str(state),'statusline','--account',account]
    if renderer:command+=['--display-command',renderer]
    settings={'statusLine':{'type':'command','command':shlex.join(command)}}
    return {**spec,'args':[*spec['args'],'--settings',json.dumps(settings)]}


def _call(*args, timeout=45):
    try:
        result = subprocess.run(['hc', *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError('Core outcome unknown; inspect before retry') from error
    if result.returncode:
        raise RuntimeError('Core command failed; inspect local runtime before retry')
    try:
        body = json.loads(result.stdout)
    except ValueError as error:
        raise RuntimeError('Core returned invalid JSON; inspect before retry') from error
    if not isinstance(body, dict) or body.get('error') or body.get('ok') is False:
        raise RuntimeError('Core rejected command')
    return body


def _budget(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError('Core wait deadline expired; inspect before further input')
    return remaining


def require_managed():
    address = os.environ.get('HC_TERMINAL_ID', '')
    if not ADDRESS.fullmatch(address):
        raise RuntimeError('Core control requires a genuine HC_TERMINAL_ID managed terminal')
    current = _call('get', address)
    if current.get('address') != address or current.get('state') != 'live':
        raise RuntimeError('Core controller terminal is unavailable; inspect')
    return address


def _values(argv, option):
    values = []
    for index, arg in enumerate(argv):
        if arg == option:
            if index+1 >= len(argv):
                raise RuntimeError('Native agent option has no value')
            values.append(argv[index+1])
        elif arg.startswith(option+'='):
            values.append(arg[len(option)+1:])
    return values


def _model_effort(provider, argv):
    if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
        raise RuntimeError('Native agent argv unavailable; inspect')
    model = _values(argv, '--model') + _values(argv, '-m')
    if provider == 'codex':
        effort = []
        for item in _values(argv, '-c') + _values(argv, '--config'):
            key = item.partition('=')[0].strip()
            if key in ('model', 'model_reasoning_effort'):
                try:
                    value = tomllib.loads(item)[key]
                except (ValueError, KeyError) as error:
                    raise RuntimeError('Native model configuration is ambiguous') from error
                if key == 'model':
                    raise RuntimeError('Native model configuration conflicts with explicit model')
                effort.append(value)
        if '--no-daemon' not in argv:
            raise RuntimeError('Core Codex adoption requires native --no-daemon')
    else:
        effort = _values(argv, '--effort')
    if len(model) != 1 or len(effort) != 1 or not all(isinstance(value, str) and value for value in (model[0], effort[0])):
        raise RuntimeError('Native model/effort metadata must be explicit and unique')
    return model[0], effort[0]


def startup_native(address, outer_deadline=None):
    """Wait only for absent OS metadata on the same settled terminal."""
    deadline = time.monotonic() + 3
    if outer_deadline is not None:
        deadline = min(deadline, outer_deadline)
    while True:
        native = _call('get', address, timeout=_budget(deadline))
        if (native.get('state') != 'live' or native.get('foreground') is not None
                or (native.get('status') or {}).get('state') not in ('idle', 'done')
                or time.monotonic() >= deadline):
            return native
        time.sleep(.1)


def _process_matches(provider, name):
    return name in ({'claude', 'claude.exe'} if provider == 'claude' else {provider})


def _detector_matches(provider, name):
    return name in ({'claude', 'claude-code'} if provider == 'claude' else {provider})


class CoreRuntime:
    def __init__(self, task=None):
        self.task = task
        self.target = dict(task.get('runtime', {})) if task else None

    def _native(self, name, ready=False, timeout=45):
        target = self.target or {}
        address = target.get('terminal', '')
        if target.get('backend') != 'core' or target.get('name') != name or not ADDRESS.fullmatch(address):
            raise RuntimeError('Core task has no matching terminal identity; inspect')
        native = _call('get', address, timeout=timeout)
        foreground = native.get('foreground') or {}
        labels = native.get('labels') or {}
        if (native.get('address') != address or native.get('state') != 'live' or native.get('name') != target.get('native_name', name)
                or labels.get('teamkit.owner') != self.task.get('owner')
                or labels.get('teamkit.task') != self.task.get('id')
                or labels.get('teamkit.name') != name
                or not _process_matches(self.task.get('provider'), foreground.get('name'))
                or type(foreground.get('pid')) is not int or foreground['pid'] <= 0
                or type(target.get('native_pid')) is not int or target['native_pid'] <= 0
                or target['native_pid'] != foreground['pid']):
            raise RuntimeError('Core terminal owner/process changed; preserve it and inspect')
        status = native.get('status') or {}
        word = status.get('state', 'unknown')
        if ready and (word not in ('idle','done') or not _detector_matches(self.task.get('provider'), status.get('app'))):
            raise RuntimeError('Core agent is not idle and ready; inspect before sending')
        return native

    def launch(self, task, spec, name, on_created=None):
        require_managed()
        owner = spec['env'].get('TEAMKIT_OWNER')
        if not owner or spec['kind'] not in ('codex', 'claude'):
            raise RuntimeError('Core launch requires an explicit task owner and provider')
        args = ['run', '--json', '--bg', '--keep', '--outlive-parent', '--no-notify',
                '--name', name, '--cwd', task['root'],
                '--label', 'teamkit.owner='+owner, '--label', 'teamkit.task='+task['id'],
                '--label', 'teamkit.name='+name]
        # An empty CLAUDE_CONFIG_DIR makes Claude store its home in the working directory.
        env = dict(spec['env'])
        unset = ['-u', 'CLAUDE_CONFIG_DIR'] if env.get('CLAUDE_CONFIG_DIR') == '' else []
        if unset:
            del env['CLAUDE_CONFIG_DIR']
        args += ['--', 'env', *unset, *(key+'='+value for key, value in env.items()), spec['kind'], *spec['args']]
        created = _call(*args)
        address = created.get('address', '')
        if not isinstance(address, str) or not ADDRESS.fullmatch(address):
            raise RuntimeError('Core terminal address unavailable; inspect before retry')
        detail = {'backend':'core', 'name':name, 'terminal':address, 'pane':address,
                  'session_cwd':task['root'],
                  'managed_workspace':False, 'ephemeral':task['role'] != 'leader'
                  or task.get('classification', {}).get('kind') == 'design'}
        if on_created:
            on_created(dict(detail))
        # hc owns readiness waiting. A timeout is a known terminal with an
        # uncertain startup, not permission to launch a replacement.
        deadline = time.monotonic() + 30
        waited = _call('wait', '--json', '--timeout', '30', address, timeout=_budget(deadline))
        while (waited.get('terminal') == address and waited.get('ended') is False
               and waited.get('reason') == 'message'):
            remaining = _budget(deadline)
            waited = _call('wait', '--json', '--timeout', str(max(1, int(remaining))), address, timeout=remaining)
            time.sleep(0.01)
        if waited.get('terminal') != address or waited.get('ended') is not True or waited.get('reason') != 'status' or waited.get('status') != 'idle':
            raise RuntimeError('Core startup not ready; inspect the recorded terminal')
        _budget(deadline)
        native = startup_native(address, deadline)
        _budget(deadline)
        detail['native_pid'] = (native.get('foreground') or {}).get('pid')
        self.task = {**task, 'owner':owner, 'provider':spec['kind']}
        self.target = detail
        self._native(name, ready=True, timeout=_budget(deadline))
        _budget(deadline)
        if on_created:
            on_created(dict(detail))
        return detail

    @staticmethod
    def validate_adoption(task, spec, address):
        controller = require_managed()
        if (not isinstance(address, str) or not ADDRESS.fullmatch(address) or address == controller
                or task['role'] != 'leader' or task.get('work_id')
                or task.get('classification', {}).get('kind') == 'design'):
            raise RuntimeError('Only an unlinked plain core leader can adopt another terminal')
        native = _call('get', address)
        foreground = native.get('foreground') or {}
        labels = native.get('labels') or {}
        status = native.get('status') or {}
        cwd = foreground.get('cwd') or native.get('cwd')
        if (native.get('address') != address or native.get('state') != 'live'
                or status.get('state') not in ('idle','done') or not _detector_matches(spec['kind'], status.get('app'))
                or not _process_matches(spec['kind'], foreground.get('name'))
                or type(foreground.get('pid')) is not int or foreground['pid'] <= 0
                or any(key in labels for key in ('teamkit.owner','teamkit.task','teamkit.name'))
                or not isinstance(cwd, str) or Path(cwd).resolve() != Path(task['root']).resolve()
                or not (native.get('name') is None or isinstance(native.get('name'), str))):
            raise RuntimeError('Core adoption terminal is owned, changed or not idle; preserve it and inspect')
        if spec['kind'] not in ('codex','claude') or _model_effort(spec['kind'], foreground.get('argv')) != _model_effort(spec['kind'], spec['args']):
            raise RuntimeError('Core adoption model/effort metadata differs from admission')
        required = [Path(value).resolve() for value in _values(spec['args'], '--add-dir')]
        writable = [Path(cwd).resolve(), *[(Path(cwd)/value).resolve() for value in _values(foreground['argv'], '--add-dir')]]
        if any(not any(path.is_relative_to(root) for root in writable) for path in required):
            raise RuntimeError('Core adoption lacks the requested GUI writable directory; use a fresh execution')
        return native

    def adopt(self, task, spec, name, address, on_created=None):
        # The local ledger lock serializes cooperating adopters. hc label has no
        # compare-and-set against direct external label edits; verify afterward.
        require_managed()
        if not isinstance(address, str) or not ADDRESS.fullmatch(address):
            raise RuntimeError('Core adoption requires a local terminal address')
        state = Path(spec['env'].get('TEAMKIT_STATE', ''))
        owner = spec['env'].get('TEAMKIT_OWNER')
        if not owner or not state.is_absolute() or not state.parent.is_dir():
            raise RuntimeError('Core adoption requires an owner and an existing ledger directory')
        lock = os.open(str(state.with_name(state.name+'.core-'+address+'.lock')),
                       os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError('Core terminal adoption is already in progress; inspect') from error
            native = self.validate_adoption(task, spec, address)
            detail = {'backend':'core', 'name':name, 'native_name':native.get('name'),
                      'terminal':address, 'pane':address, 'native_pid':native['foreground']['pid'],
                      'managed_workspace':False, 'ephemeral':False, 'adopted':True, 'bind_intent':True}
            if on_created:
                on_created(dict(detail))
            _call('label', '--json', address, 'teamkit.owner='+owner,
                  'teamkit.task='+task['id'], 'teamkit.name='+name)
            self.task = {**task, 'owner':owner, 'provider':spec['kind']}
            self.target = detail
            self._native(name, ready=True)
            detail['binding_confirmed'] = True
            if on_created:
                on_created(dict(detail))
            return detail
        finally:
            os.close(lock)

    def call(self, *args):
        if len(args) != 3 or args[:2] != ('agent', 'get'):
            raise RuntimeError('Unsupported core operation; never route it through TUI')
        require_managed()
        native = self._native(args[2])
        reported = native.get('status') or {}
        status = reported.get('state', 'unknown') if _detector_matches(self.task.get('provider'), reported.get('app')) else 'unknown'
        return {'agent':{'name':args[2], 'agent_status':status, 'pane_id':native['address'],
                         'interactive_ready':status in ('idle','done'), 'completion_seq':native.get('turn')}}

    def accept(self, name, text, before_send=None):
        """Confirm accepted input, independently of the model turn's completion."""
        require_managed()
        native = self._native(name, ready=True)
        if before_send is not None:
            before_send()
        condition = ['--if', 'idle'] if native['status']['state'] == 'idle' else []
        sent = _call('send', '--json', *condition, native['address'], text, timeout=30)
        if (sent.get('started') is not True or type(sent.get('turn')) is not int
                or sent['turn'] < native.get('turn', 0)):
            raise RuntimeError('Core input acceptance unconfirmed; inspect before further input')
        # An input can already be working here. Recheck identity, not readiness.
        self._native(name)
        return {'accepted': True, 'terminal': native['address'],
                'native_pid': native['foreground']['pid'],
                'turn_baseline': native.get('turn', 0), 'turn': sent['turn']}

    def prompt(self, name, text):
        require_managed()
        native = self._native(name, ready=True)
        address = native['address']
        # hc has an atomic --if idle guard, but no --if done. For a checked,
        # settled done agent use ordinary send; never force a blocked program.
        condition = ['--if','idle'] if native['status']['state']=='idle' else []
        deadline = time.monotonic() + 30
        sent = _call('send', '--json', *condition, '--wait', '--timeout', '30', address, text, timeout=_budget(deadline))
        waited = sent.get('wait') or {}
        resumed = False
        # A caller message or delayed status report ends hc's wait, not the
        # accepted input. Follow that same turn; never submit the text again.
        while (waited.get('terminal') == address and waited.get('ended') is False
               and waited.get('reason') in ('message', 'not_started')
               and type(sent.get('started')) is bool):
            remaining = _budget(deadline)
            try:
                self._native(name, timeout=remaining)
            except RuntimeError as error:
                raise RuntimeError('Core post-send identity unconfirmed; inspect before further input') from error
            remaining = _budget(deadline)
            waited = _call('wait', '--json', '--timeout', str(max(1, int(remaining))), address, timeout=remaining)
            resumed = True
            time.sleep(0.01)
        _budget(deadline)
        if ((sent.get('started') is not True and not resumed) or waited.get('terminal') != address
                or waited.get('ended') is not True or waited.get('reason') != 'status'
                or waited.get('status') not in ('idle','done')
                or type(waited.get('turn')) is not int or waited['turn'] <= native.get('turn', 0)):
            reason = waited.get('reason')
            reason = reason if reason in ('status','exit','output','timeout','not_started','message') else 'unknown'
            raise RuntimeError(f'Core delivery/wait not confirmed ({reason}); inspect before further input')
        if resumed:
            remaining = _budget(deadline)
            try:
                self._native(name, timeout=remaining)
            except RuntimeError as error:
                raise RuntimeError('Core post-send identity unconfirmed; inspect before further input') from error
            _budget(deadline)
            sent = {**sent, 'wait':waited}
        # This confirms the transport turn only. Receipt and verification remain
        # separate ledger events on the host/worker paths.
        return sent
