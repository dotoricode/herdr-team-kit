"""Explicit task classification and evidence-based effort escalation."""
from collections import Counter

KINDS = ('general', 'small', 'design', 'wording')
RISKS = ('security', 'jni', 'authentication', 'concurrency')
FAILURES = ('reasoning', 'insufficient_evidence', 'environment', 'typo')


def classification(role, kind='general', risks=()):
    if kind not in KINDS or not isinstance(risks, (list, tuple)) or any(r not in RISKS for r in risks):
        raise ValueError('invalid task classification')
    allowed = {'leader': ('general','design'), 'implementer': ('general','small'),
               'reviewer': ('general','wording')}
    if kind not in allowed.get(role, ()):
        raise ValueError('work kind does not match role')
    return {'kind':kind, 'risks':sorted(set(risks))}


def execution_policy(config, task):
    role = dict(config['teams'][task['team']]['roles'][task['role']])
    cls = task.get('classification', {'kind':'general', 'risks':[]})
    cls = classification(task['role'], cls['kind'], cls['risks'])
    kind = cls['kind']
    if cls['risks'] and kind in ('small', 'wording'):
        kind = 'general'
    routes = config.get('routing', {})
    if kind != 'general' or task['role'] == 'implementer':
        if kind in routes:
            role = dict(routes[kind])
        elif kind != 'general':
            raise ValueError('work kind requires an explicit routing policy')
    failures = task.get('failures', [])
    decision = task.get('user_decision')
    if decision:
        role = {k:decision[k] for k in ('provider','model','effort')}
        failures = failures[decision['failure_count']:]
    meaningful = [f for f in failures if f['kind'] in ('reasoning','insufficient_evidence')]
    if meaningful and meaningful[-1]['effort'] in ('high','xhigh','max'):
        return {'action':'ask_user', 'reason':'unresolved after high effort; report evidence before choosing another model'}
    counts = Counter(f['cause'] for f in meaningful)
    if any(n >= 2 for n in counts.values()) or any(f['kind'] == 'insufficient_evidence' for f in meaningful):
        if role.get('effort', 'medium') not in ('high','xhigh','max'):
            role['effort'] = 'high'
    return {'action':'dispatch', **role}


def execution_preference(config):
    """Validate opt-in provider fallback separately from fixed role policies."""
    if 'execution_preference' not in config:
        return None
    preference = config['execution_preference']
    if (not isinstance(preference, dict)
            or set(preference) != {'provider_order', 'weekend_account_first'}
            or type(preference['weekend_account_first']) is not bool):
        raise ValueError('execution_preference requires provider_order and weekend_account_first')
    order = preference['provider_order']
    if (not isinstance(order, list) or not order
            or any(not isinstance(provider, str) or provider not in config['profiles'] for provider in order)
            or len(set(order)) != len(order)):
        raise ValueError('provider_order must list distinct configured providers')
    return preference


def execution_candidates(config, task, role, now):
    """Return ordered flexible candidates, or None for role-fixed execution."""
    constraint = task.get('execution_constraint')
    if constraint is not None:
        if (not isinstance(constraint, dict) or set(constraint) != {'provider', 'account'}
                or constraint.get('provider') not in config['profiles']
                or constraint.get('account') not in ('team', 'personal')):
            raise ValueError('execution constraint requires a configured provider and account')
        provider, account = constraint['provider'], constraint['account']
        cls = task.get('classification', {'kind': 'general', 'risks': []})
        fixed = (task.get('user_decision') or task['role'] not in ('leader', 'implementer')
                 or cls['kind'] not in ('general', 'small') or cls['risks'])
        if fixed and provider != role['provider']:
            return []  # A request pin cannot replace an approved or specialized model policy.
        return [{**role, **constraint,
                 'model': role['model'] if provider == role['provider'] else config['profiles'][provider][account]['model']}]
    preference = execution_preference(config)
    cls = task.get('classification', {'kind': 'general', 'risks': []})
    if (preference is None or task.get('user_decision') or task['role'] not in ('leader', 'implementer')
            or cls['kind'] not in ('general', 'small') or cls['risks']):
        return None
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from .policy import account_order
    schedule = config.get('account_selection', {'timezone': 'Asia/Seoul',
        'weekdays': ['team', 'personal'], 'weekends': ['personal', 'team']})
    weekend = datetime.fromtimestamp(now, ZoneInfo(schedule['timezone'])).weekday() >= 5
    accounts = account_order({'account_selection': schedule}, now)
    providers = preference['provider_order']
    pairs = ([(provider, account) for account in accounts for provider in providers]
             if weekend and preference['weekend_account_first']
             else [(provider, account) for provider in providers for account in accounts])
    return [{**role, 'provider': provider, 'account': account,
             'model': role['model'] if provider == role['provider'] else config['profiles'][provider][account]['model'],
             'effort': role.get('effort', 'medium')}
            for provider, account in pairs]
