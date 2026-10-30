"""Read provider identity and quota without starting a model turn."""
import json
import os
import selectors
import subprocess
import time

from .runtime import AUTH_ENV, RuntimeError, home_key, home_value


def profile_env(provider, profile):
    env = os.environ.copy()
    for key in AUTH_ENV:
        env.pop(key, None)
    env.pop('CODEX_HOME', None)
    env.pop('CLAUDE_CONFIG_DIR', None)
    value = home_value(provider, profile['home'])
    if value:
        env[home_key(provider)] = value
    return env


class CodexRPC:
    def __init__(self, profile):
        self.profile = profile
        self.proc = None
        self.buffer = b''
        self.serial = 0

    def __enter__(self):
        self.proc = subprocess.Popen(['codex','-c','cli_auth_credentials_store="file"','app-server'],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     env=profile_env('codex', self.profile))
        try:
            self.request('initialize', {'clientInfo':{'name':'herdr_team_kit','version':'0.1.0'}})
            self._write({'method':'initialized'})
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _write(self, message):
        self.proc.stdin.write((json.dumps(message)+'\n').encode())
        self.proc.stdin.flush()

    def request(self, method, params=None):
        self.serial += 1
        target = self.serial
        self._write({'id':target, 'method':method, 'params':params})
        deadline = time.monotonic()+15
        with selectors.DefaultSelector() as selector:
            selector.register(self.proc.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if b'\n' not in self.buffer:
                    if not selector.select(max(0, deadline-time.monotonic())):
                        break
                    chunk = os.read(self.proc.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    self.buffer += chunk
                    if len(self.buffer) > 2**20:
                        raise RuntimeError('provider response too large')
                    continue
                line, self.buffer = self.buffer.split(b'\n', 1)
                response = json.loads(line)
                if response.get('id') != target:
                    continue
                if response.get('error'):
                    raise RuntimeError('provider rejected read-only account request')
                return response['result']
        raise RuntimeError('provider account request timed out')

    def __exit__(self, *args):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            self.proc.stdin.close()
            self.proc.stdout.close()


def match_identity(profile, identity):
    expected = profile['identity']
    email = identity.get('email')
    if not email or email.casefold() != expected['email'].casefold():
        raise RuntimeError('actual account does not match configured profile')
    for key in ('planType', 'organizationId'):
        if key in expected and expected[key] != identity.get(key):
            raise RuntimeError('actual organization/plan does not match configured profile')
    return {'email':email, **{k:identity[k] for k in ('planType','organizationId') if k in identity}}


def identity(provider, profile):
    if provider == 'codex':
        with CodexRPC(profile) as rpc:
            account = rpc.request('account/read', {'refreshToken':False}).get('account') or {}
            if account.get('type') != 'chatgpt':
                raise RuntimeError('subscription account required; API fallback disabled')
            return match_identity(profile, account)
    result = subprocess.run(['claude','auth','status','--json'], capture_output=True, text=True,
                            env=profile_env('claude', profile), timeout=15)
    if result.returncode:
        raise RuntimeError('Claude subscription identity unavailable')
    value = json.loads(result.stdout)
    if not value.get('loggedIn') or value.get('authMethod') != 'claude.ai':
        raise RuntimeError('Claude subscription authentication required')
    return match_identity(profile, {'email':value.get('email'), 'organizationId':value.get('orgId'),
                                    'planType':value.get('subscriptionType')})


def codex_quota(profile):
    with CodexRPC(profile) as rpc:
        account = rpc.request('account/read', {'refreshToken':False}).get('account') or {}
        if account.get('type') != 'chatgpt':
            raise RuntimeError('subscription account required')
        match_identity(profile, account)
        return rpc.request('account/rateLimits/read')
