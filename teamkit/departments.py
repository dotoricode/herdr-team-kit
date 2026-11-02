"""Explicit common departments for ticket execution, separate from product pools.

Only caller-local routing views include departments. Never pass that view into
session_pool preparation: canonical config.teams remains the product inventory.
"""
import copy
from pathlib import Path
import re


ROLES = {'leader', 'implementer', 'reviewer'}
EFFORTS = {'low', 'medium', 'high', 'xhigh', 'max'}


def definitions(config):
    products = config.get('teams', {})
    operations = config.get('operations', {})
    if not isinstance(products, dict) or not isinstance(operations, dict):
        raise ValueError('product and operations definitions must be objects')
    departments = operations.get('departments', {})
    if not isinstance(departments, dict):
        raise ValueError('operations.departments must be an explicit identity map')
    for ident, department in departments.items():
        if (not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', ident)
                or ident in products):
            raise ValueError('department identity must be distinct from every product identity')
        if (not isinstance(department, dict) or not isinstance(department.get('root'), str)
                or not Path(department['root']).is_absolute()):
            raise ValueError('department requires an explicit absolute working root')
        roles = department.get('roles')
        if not isinstance(roles, dict) or not roles or set(roles) - ROLES:
            raise ValueError('department requires explicit supported execution roles')
        for policy in roles.values():
            if (not isinstance(policy, dict) or policy.get('provider') not in config.get('profiles', {})
                    or not isinstance(policy.get('model'), str) or not policy['model'].strip()
                    or policy.get('effort') not in EFFORTS):
                raise ValueError('department role requires configured provider, explicit model and effort')
        references = department.get('references', [])
        if (not isinstance(references, list)
                or any(not isinstance(path, str) or not Path(path).is_absolute() for path in references)):
            raise ValueError('department references must be absolute file paths')
    return {**copy.deepcopy(products), **copy.deepcopy(departments)}


def organization(config, ident):
    result = definitions(config)
    if ident not in result:
        raise ValueError('unknown configured product or department')
    return result[ident]


def execution_config(config):
    result = copy.deepcopy(config)
    result['teams'] = definitions(config)
    return result
