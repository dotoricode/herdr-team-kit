import copy
import unittest

from teamkit.routing import execution_policy, execution_candidates
from teamkit.workers import _policy_task


class LeaderPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.config = {'profiles': {p: {a: {'model': p+'-profile'} for a in ('team','personal')}
                                    for p in ('claude','codex')},
            'teams': {'alpha': {'roles': {'leader': {'provider':'codex','model':'old','effort':'medium'},
                                         'reviewer': {'provider':'codex','model':'review','effort':'high'}}}},
            'execution_preference': {'provider_order':['claude','codex'],'weekend_account_first':True},
            'leader_execution_preference': {'provider':'claude','account':'team','model':'claude-opus-5-5','effort':'medium'}}
        self.task = {'team':'alpha','role':'leader'}

    def test_weekend_leader_priority_overrides_general_account_schedule(self):
        role = execution_policy(self.config, self.task)
        choices = execution_candidates(self.config, self.task, role, 1793502000)
        self.assertEqual([(x['provider'],x['account']) for x in choices],
                         [('claude','team'),('claude','personal'),('codex','personal'),('codex','team')])
        self.assertEqual((choices[0]['model'],choices[0]['effort']),('claude-opus-5-5','medium'))

    def test_explicit_account_pins_and_model_decisions_keep_policy(self):
        for extra in ({'execution_constraint':{'provider':'codex','account':'personal'}},
                      {'user_decision':{'provider':'codex','model':'approved','effort':'high','failure_count':0}}):
            task = self.task | extra
            self.assertEqual(execution_policy(self.config,task)['provider'],'codex')

    def test_investigator_mapping_does_not_become_a_team_leader(self):
        task = _policy_task(self.config, {'product':'alpha','role':'investigator'})
        self.assertEqual(execution_policy(self.config,task)['model'],'old')

    def test_reviewer_and_legacy_configuration_unchanged(self):
        self.assertEqual(execution_policy(self.config,self.task | {'role':'reviewer'})['model'],'review')
        self.config.pop('leader_execution_preference')
        self.assertEqual(execution_policy(self.config,self.task)['model'],'old')

    def test_invalid_leader_preference_rejected(self):
        for change in ({'account':'invalid'}, {'provider':'missing'}, {'provider':[]}, {'effort':'invalid'}, {'model':''}):
            value=copy.deepcopy(self.config)
            value['leader_execution_preference'].update(change)
            with self.assertRaises(ValueError):
                execution_policy(value,self.task)

    def test_implicit_design_and_risk_classification_keep_leader_priority(self):
        self.config['routing'] = {'design': {'provider':'codex','model':'design','effort':'high'}}
        for classification in ({'kind':'design','risks':[]}, {'kind':'general','risks':['security']}):
            task = self.task | {'classification':classification}
            role = execution_policy(self.config, task)
            self.assertEqual(role['model'],'claude-opus-5-5')
            self.assertEqual(execution_candidates(self.config,task,role,1793502000)[0]['account'],'team')

    def test_delegation_contract_is_for_true_leader_only(self):
        from teamkit.protocol import leader_guidance
        self.assertIn('bounded subagents', leader_guidance('leader'))
        self.assertIn('available', leader_guidance('leader'))
        self.assertEqual(leader_guidance('investigator'), '')

    def test_real_operation_prompt_delegates_only_actual_leader(self):
        import tempfile
        from pathlib import Path
        from teamkit.operations import prompt
        with tempfile.TemporaryDirectory() as directory:
            ticket = {key: {} for key in ('scope','approval','responsible')}
            ticket.update(id='T1', origin_id='O1', parent_id=None, role='leader', kind='investigation',
                          goal='Prepare a plan', completion=['Plan exists'], checks=['report'], resources=[], profiles=[])
            attempt = {'id':'A1','executor':{'owner':'owner','provider':'claude'}}
            text = prompt(Path(directory)/'config.json',Path(directory)/'state.sqlite',ticket,attempt)
            self.assertIn('bounded subagents',text)
            ticket['role']='investigator'
            self.assertNotIn('TEAM LEADER:',prompt(Path(directory)/'config.json',Path(directory)/'state.sqlite',ticket,attempt))
