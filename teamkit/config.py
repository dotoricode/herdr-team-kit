import json
from pathlib import Path

from .runtime import launch_spec


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
    return value if value and value.get('binding') == binding(profile(config, provider, account)) else None
