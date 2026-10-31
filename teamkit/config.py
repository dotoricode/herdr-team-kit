import json
from pathlib import Path

from .runtime import launch_spec
from .policy import CLAUDE_QUOTA_SOURCE


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
    routes = value.get('routing', {})
    from .routing import KINDS
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
    if provider == 'claude' and value and value.get('source') != CLAUDE_QUOTA_SOURCE:
        return None
    return value if value and value.get('binding') == binding(profile(config, provider, account)) else None
