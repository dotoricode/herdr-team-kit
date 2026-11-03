"""Two persistent personal document actors, fenced at the delivery boundary.

Historical attempts retain their identities even after completion or uncertainty.
This module never adopts a terminal or changes the user's ticket approval.
"""
import json


PROFILES = {'codex/personal', 'claude/personal'}


def policy(config, ticket):
    operations = config.get('operations', {})
    departments = operations.get('departments', {})
    value = operations.get('document_staff')
    if value is not None:
        if (not isinstance(value, dict) or set(value) != {'department', 'profiles'}
                or not isinstance(value.get('department'), str) or value['department'] not in departments
                or not isinstance(value.get('profiles'), list) or len(value['profiles']) != 2
                or any(not isinstance(p, str) for p in value['profiles']) or set(value['profiles']) != PROFILES
                or any('personal' not in config.get('profiles', {}).get(p, {}) for p in ('codex', 'claude'))):
            raise ValueError('document_staff requires one configured personal Codex and personal Claude department')
        if ticket['product'] == value['department'] and ticket['role'] == 'documenter':
            return value
    qa = operations.get('qa_staff')
    if qa is not None:
        if (not isinstance(qa, dict) or set(qa) != {'department', 'roles'}
                or not isinstance(qa.get('department'), str) or qa['department'] not in departments
                or qa.get('roles') != ['investigator'] or 'personal' not in config.get('profiles', {}).get('claude', {})):
            raise ValueError('qa_staff permits only a personal Claude general investigator; UI and review policies remain separate')
        if ticket['product'] == qa['department'] and ticket['role'] == 'investigator':
            return dict(department=qa['department'], profiles=['claude/personal'])
    return None


def department(config, product):
    operations = config.get('operations', {})
    return any(isinstance(operations.get(key), dict) and operations[key].get('department') == product
               for key in ('document_staff', 'qa_staff'))


def actors(config, db, ticket, provider):
    """Include launch intents and uncertain outcomes; never infer a stopped actor."""
    value = policy(config, ticket)
    if value is None:
        return []
    result = list(config.get('operations', {}).get('executors', []))
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operation_attempts'").fetchone():
        result.extend(json.loads(row[0])['executor'] for row in db.execute('SELECT body FROM operation_attempts'))
    from .staffbootstrap import records
    result.extend(record['actor'] for record in records(db))
    unique = {}
    for actor in result:
        if (actor.get('product') == value['department'] and ticket['role'] in actor.get('roles', [])
                and actor.get('provider') == provider and actor.get('account') == 'personal'):
            owner = actor.get('owner')
            if not owner:
                raise ValueError('personal document staff has an unverified owner')
            unique[owner] = actor
    return list(unique.values())


def check(config, db, ticket, executor):
    value = policy(config, ticket)
    if value is None:
        if department(config, ticket['product']):
            raise ValueError('staff department does not support this role; use the existing specialist or UI path')
        return
    if (executor.get('provider', '') + '/' + executor.get('account', '') not in value['profiles']
            or executor.get('product') != ticket['product'] or ticket['role'] not in executor.get('roles', [])):
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
