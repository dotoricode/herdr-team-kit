"""Two persistent personal document actors, fenced at the delivery boundary.

Historical attempts retain their identities even after completion or uncertainty.
This module never adopts a terminal or changes the user's ticket approval.
"""
import json


PROFILES = {'codex/personal', 'claude/personal'}


def policy(config, ticket):
    value = config.get('operations', {}).get('document_staff')
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'department', 'profiles'}
            or value.get('department') not in config.get('operations', {}).get('departments', {})
            or not isinstance(value.get('profiles'), list) or len(value['profiles']) != 2
            or set(value['profiles']) != PROFILES
            or any('personal' not in config.get('profiles', {}).get(p, {}) for p in ('codex', 'claude'))):
        raise ValueError('document_staff requires one configured personal Codex and personal Claude department')
    return value if ticket['product'] == value['department'] and ticket['role'] == 'documenter' else None


def actors(config, db, ticket, provider):
    """Include launch intents and uncertain outcomes; never infer a stopped actor."""
    value = policy(config, ticket)
    if value is None:
        return []
    result = list(config.get('operations', {}).get('executors', []))
    result.extend(json.loads(row[0])['executor'] for row in db.execute('SELECT body FROM operation_attempts'))
    unique = {}
    for actor in result:
        if (actor.get('product') == value['department'] and 'documenter' in actor.get('roles', [])
                and actor.get('provider') == provider and actor.get('account') == 'personal'):
            owner = actor.get('owner')
            if not owner:
                raise ValueError('personal document staff has an unverified owner')
            unique[owner] = actor
    return list(unique.values())


def check(config, db, ticket, executor):
    if policy(config, ticket) is None:
        return
    if (executor.get('provider', '') + '/' + executor.get('account', '') not in PROFILES
            or executor.get('product') != ticket['product'] or 'documenter' not in executor.get('roles', [])):
        raise ValueError('executor is outside personal document staff')
    previous = actors(config, db, ticket, executor['provider'])
    if any(actor['owner'] != executor.get('owner') for actor in previous):
        raise ValueError('personal document staff slot already exists; wait for original actor')


def available(config, db, ticket, provider, reserved_owner=None):
    previous = actors(config, db, ticket, provider)
    return not previous or (reserved_owner is not None and
                            all(actor['owner'] == reserved_owner for actor in previous))


def routing_config(config, role):
    """Keep configured provider priority while making both staff profiles reachable."""
    preference = config.get('execution_preference', {})
    order = preference.get('provider_order', [role['provider']])
    providers = list(dict.fromkeys([*order, 'claude', 'codex']))
    return {**config, 'execution_preference': {
        'provider_order': providers,
        'weekend_account_first': preference.get('weekend_account_first', True)}}
