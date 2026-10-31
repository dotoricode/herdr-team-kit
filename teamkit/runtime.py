"""Herdr control is available only inside a managed pane."""
import json
import os
from pathlib import Path
import subprocess


class RuntimeError(Exception):
    pass


AUTH_ENV = ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'CODEX_ACCESS_TOKEN', 'ANTHROPIC_API_KEY',
            'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_OAUTH_TOKEN',
            'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY')


def home_key(provider):
    return 'CODEX_HOME' if provider == 'codex' else 'CLAUDE_CONFIG_DIR'


def home_value(provider, home):
    # Claude's default keychain namespace differs from an explicit default path.
    if provider == 'claude' and home == str(Path.home()/'.claude'):
        return ''
    return home


def profile_home_matches(provider, home):
    observed = os.environ.get(home_key(provider))
    expected = home_value(provider, home)
    if provider == 'claude' and expected == '':
        return not observed
    effective = observed or str(Path.home()/('.codex' if provider == 'codex' else '.claude'))
    return effective == expected


def launch_spec(provider, profile, session):
    home = profile.get('home', '')
    if provider not in ('codex','claude') or not Path(home).is_absolute() or not profile.get('model') or not profile.get('identity', {}).get('email'):
        raise ValueError('profile requires absolute home, model, and expected identity.email')
    effort = profile.get('effort', 'medium')
    env = {key: '' for key in AUTH_ENV}
    if provider == 'codex':
        env['CODEX_HOME'] = home
        args = ['resume', session] if session else []
        args += ['--model', profile['model'], '-c', 'model_reasoning_effort='+json.dumps(effort),
                 '-c', 'cli_auth_credentials_store="file"', '--approve-for-me', '--no-alt-screen', '--no-daemon']
    else:
        env['CLAUDE_CONFIG_DIR'] = home_value(provider, home)
        args = ['--resume', session] if session else []
        args += ['--model', profile['model'], '--effort', effort, '--permission-mode', 'auto']
    return {'kind':provider, 'env':env, 'args':args}


class Herdr:
    def call(self, *args):
        if os.environ.get('HERDR_ENV') != '1':
            raise RuntimeError('Herdr control requires HERDR_ENV=1 in a managed pane')
        try:
            result = subprocess.run(['herdr', *args], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError('Herdr outcome unknown; inspect before retry') from error
        if result.returncode:
            # Do not echo terminal text or internal paths into external logs.
            raise RuntimeError('Herdr command failed; inspect local runtime before retry')
        try:
            body = json.loads(result.stdout)
        except ValueError as error:
            raise RuntimeError('Herdr returned invalid JSON; outcome may be unknown') from error
        if not isinstance(body, dict) or body.get('error') or body.get('ok') is False:
            raise RuntimeError('Herdr rejected command')
        return body.get('result', body)

    def read(self, name):
        if os.environ.get('HERDR_ENV') != '1':
            raise RuntimeError('Herdr read requires a managed pane')
        try:
            result = subprocess.run(['herdr','agent','read',name,'--source','detection','--lines','12'],
                                    capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError('context observation unavailable; inspect') from error
        if result.returncode:
            raise RuntimeError('context observation unavailable; inspect')
        return result.stdout

    def launch(self, task, spec, name, on_created=None):
        args = ['workspace','create','--cwd',task['root'],'--label',name,'--no-focus']
        for key, value in spec['env'].items():
            args += ['--env',key+'='+value]
        result = self.call(*args)
        pane = result['root_pane']['pane_id']
        if on_created:
            on_created({'name':name, 'pane':pane})
        self.call('agent','start',name,'--kind',spec['kind'],'--pane',pane,'--',*spec['args'])
        return {'name':name, 'pane':pane}

    def prompt(self, name, text):
        # A settled state is not a task receipt. Completion is a separate ledger event.
        return self.call('agent','prompt',name,text,'--wait','--timeout','30000')
