import json
import time
from pathlib import Path

from .runtime import launch_spec
from .policy import CLAUDE_QUOTA_SOURCE, CLAUDE_NATIVE_QUOTA_SOURCE, number, eligible, account_order


def load(path):
    value = json.loads(Path(path).expanduser().read_text())
    if value.get('version') != 1 or not isinstance(value.get('teams'), dict) or not isinstance(value.get('profiles'), dict):
        raise ValueError('config requires version=1, teams, profiles')
    homes = set()
    for provider, accounts in value['profiles'].items():
        if provider not in ('codex','claude') or set(accounts) != {'team','personal'}:
            raise ValueError('each provider requires team and personal profiles')
        for profile in accounts.values():
            launch_spec(provider, profile, None)
            home = str(Path(profile['home']).resolve())
            if home in homes:
                raise ValueError('profile homes must be distinct')
            homes.add(home)
    for team in value['teams'].values():
        if not Path(team['root']).is_absolute() or not team.get('roles'):
            raise ValueError('team requires absolute root and explicit role policies')
        for role, policy in team['roles'].items():
            if role not in ('leader','implementer','reviewer') or policy.get('provider') not in value['profiles'] or not policy.get('model'):
                raise ValueError('role requires configured provider and explicit model')
    pool=value.get('session_pool')
    if pool is not None:
        if (not isinstance(pool,dict) or set(pool)!={'enabled','bindings','tasks'} or type(pool['enabled']) is not bool
                or not isinstance(pool['bindings'],str) or not Path(pool['bindings']).is_absolute()
                or not isinstance(pool['tasks'],dict) or set(pool['tasks'])!=set(value['teams'])
                or any(not isinstance(t,str) or not t for t in pool['tasks'].values())
                or len(set(pool['tasks'].values()))!=len(pool['tasks'])
                or pool['enabled'] and value.get('runtime_backend')!='core'):
            raise ValueError('session_pool requires core, enabled, absolute bindings and a distinct canonical task per team')
    routes = value.get('routing', {})
    from .routing import KINDS, execution_preference, leader_preference
    execution_preference(value)
    leader_preference(value)
    if not isinstance(routes, dict) or any(kind not in KINDS for kind in routes):
        raise ValueError('invalid routing kinds')
    for route in routes.values():
        if not isinstance(route, dict) or set(route) != {'provider','model','effort'} or route.get('provider') not in value['profiles'] or not isinstance(route.get('model'), str) or not route['model'] or route.get('effort') not in ('low','medium','high'):
            raise ValueError('routing requires provider, model and supported effort')
    shared = value.get('shared_roots', [])
    if not isinstance(shared, list) or any(not isinstance(root, str) or not Path(root).is_absolute() for root in shared):
        raise ValueError('shared_roots requires a list of absolute paths')
    ttl = value.get('quota_ttl_seconds', 120)
    if type(ttl) is not int or not 1 <= ttl <= 300:
        raise ValueError('quota TTL must be 1..300 seconds')
    value['quota_ttl_seconds'] = ttl
    account_order(value, time.time())
    from .context_handoff import settings as context_settings
    if context_settings(value)['enabled']:
        if value.get('runtime_backend') != 'core' or not isinstance(value.get('context_bindings'), str) or not Path(value['context_bindings']).is_absolute():
            raise ValueError('enabled context_handoff requires core and absolute context_bindings')
    if value.get('runtime_backend', 'tui') not in ('tui', 'core'):
        raise ValueError('runtime_backend must be tui or core')
    if type(value.get('require_design_approval',False)) is not bool:
        raise ValueError('require_design_approval must be boolean')
    private=value.get('private_plan_root')
    if private is not None and (not isinstance(private,str) or not Path(private).expanduser().is_absolute()):
        raise ValueError('private_plan_root must be absolute')
    if not isinstance(value.get('dashboard_workspaces',{}),dict) or any(k not in value['teams'] or not isinstance(v,dict) or set(v)!={'id','label'} or not all(isinstance(s,str) and s for s in v.values()) for k,v in value.get('dashboard_workspaces',{}).items()):
        raise ValueError('dashboard_workspaces must map configured teams to explicit workspace IDs')
    if value.get('herdr_session') is not None and (not isinstance(value['herdr_session'],str) or not value['herdr_session']):
        raise ValueError('herdr_session must be a session name')
    from .allocation import usage_only
    usage_only(value)
    from .team_dispatch import settings as dispatch_settings
    dispatch_settings(value)
    return value


def profile(config, provider, account, role=None):
    result = dict(config['profiles'][provider][account])
    if role:
        result.update({k:role[k] for k in ('model','effort') if k in role})
    return result


def binding(profile):
    import hashlib
    raw = json.dumps({'home':profile['home'], 'identity':profile['identity']}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def cached_quota(config, store, provider, account):
    value = store.quota(provider+'/'+account)
    if value and value.get('binding') != binding(profile(config, provider, account)):
        value = None
    if provider != 'claude':
        return None if value and value.get('source') == CLAUDE_NATIVE_QUOTA_SOURCE else value
    if value and value.get('source') != CLAUDE_QUOTA_SOURCE:
        value = None
    from .app_usage import native_admission_quota
    native = native_admission_quota(config, store.path, account)
    if native is None:
        return value
    if value is None or not number(value.get('observed_at')) or native['observed_at'] > value['observed_at']:
        return native
    if native['observed_at'] == value['observed_at']:
        if not eligible(value, time.time(), config.get('quota_ttl_seconds', 120)):
            return value
        for key in ('five_hour', 'weekly'):
            native[key]['used'] = max(native[key]['used'], value[key]['used'])
        return native
    return value
