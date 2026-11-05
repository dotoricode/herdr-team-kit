import copy
import json
from pathlib import Path
import unittest

from test_cli import Fixture
from teamkit import departments, queue_policy


def config():
    role = {'provider': 'codex', 'model': 'fixture-model', 'effort': 'high'}
    return {'teams': {name: {'root': '/srv/' + name, 'roles': {'leader': role}}
                      for name in ('alpha', 'beta', 'gamma', 'delta')},
            'profiles': {'codex': {'team': {}, 'personal': {}}},
            'operations': {'departments': {'qa': {'root': '/srv/qa', 'roles': {'reviewer': role}},
                                          'infra': {'root': '/srv/infra', 'roles': {'implementer': role}}}}}


class DepartmentPolicyTests(unittest.TestCase):
    def test_union_is_execution_only_and_never_mutates_product_pool_teams(self):
        original = config()
        before = copy.deepcopy(original)
        effective = departments.execution_config(original)
        self.assertEqual(set(effective['teams']), {'alpha', 'beta', 'gamma', 'delta', 'qa', 'infra'})
        self.assertEqual(original, before)
        effective['teams']['infra']['roles']['implementer']['model'] = 'another'
        self.assertEqual(original, before)
        self.assertEqual(len(original['teams']) * 2 * 2, 16)

    def test_duplicate_product_identity_and_implicit_model_policy_are_rejected(self):
        cases = [dict(root='/srv/qa', roles={'reviewer': {'provider': 'codex', 'effort': 'high'}}),
                 dict(root='/srv/qa', roles={'reviewer': {'provider': 'unknown', 'model': 'm', 'effort': 'high'}}),
                 dict(root='relative', roles={'reviewer': {'provider': 'codex', 'model': 'm', 'effort': 'high'}}),
                 dict(root='/srv/qa', roles={'reviewer': {'provider': 'codex', 'model': 'm'}})]
        for bad in cases:
            value = config()
            value['operations']['departments']['qa'] = bad
            with self.subTest(definition=bad), self.assertRaises(ValueError):
                departments.execution_config(value)
        value = config()
        value['operations']['departments']['alpha'] = value['operations']['departments']['qa']
        with self.assertRaises(ValueError):
            departments.execution_config(value)

    def test_shared_resource_policy_uses_department_root_without_authorizing_other_roots(self):
        value = config()
        self.assertEqual(queue_policy.resources(value, 'infra'), ['/srv/infra'])
        self.assertEqual(queue_policy.resources(value, 'infra', extra=['/srv/infra/cache']),
                         ['/srv/infra', '/srv/infra/cache'])
        with self.assertRaises(ValueError):
            queue_policy.resources(value, 'infra', extra=['/srv/alpha'])
        with self.assertRaises(ValueError):
            departments.organization(value, 'unconfigured')


class DepartmentWorkflowTests(Fixture):
    def test_department_workflow_uses_existing_approval_revision_contract(self):
        self.value['private_plan_root'] = str(self.root / 'plans')
        self.value['operations'] = {'departments': {'infra': {
            'root': str(self.repo), 'roles': {'implementer': {
                'provider': 'codex', 'model': 'infrastructure-model', 'effort': 'medium'}}}}}
        self.config.write_text(json.dumps(self.value))
        plan = self.root / 'plan.md'
        plan.write_text('One bounded infrastructure change.')
        contract = self.root / 'contract.json'
        contract.write_text(json.dumps({'title': 'Infrastructure contract', 'requirements': ['A checked change'],
            'allowed_paths': ['feature.txt'], 'steps': [{'id': 'check', 'title': 'Check', 'weight': 100}]}))
        created = self.call('work', 'create', 'INFRA-WORK', '--team', 'infra', '--root', str(self.repo),
                            '--plan-file', str(plan), '--contract-file', str(contract))
        self.assertEqual(created['team'], 'infra')
        reference = queue_policy.capture_work(self.value, self.state, 'infra', 'INFRA-WORK')
        request = {'id': 'a' * 32, 'team': 'infra', 'workflow_ref': reference,
                   'resources': queue_policy.resources(self.value, 'infra', reference), 'phase': 'pending'}
        self.assertIsNotNone(queue_policy.admission(self.value, self.state, request, [request]))
        self.call('work', 'submit', 'INFRA-WORK')
        self.call('work', 'approve', 'INFRA-WORK', '--user-approved', '--approval-reference', 'Fixture user approval')
        self.assertIsNone(queue_policy.admission(self.value, self.state, request, [request]))
        self.assertEqual(set(self.value['teams']), {'alpha'})


if __name__ == '__main__':
    unittest.main()
