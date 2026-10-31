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
