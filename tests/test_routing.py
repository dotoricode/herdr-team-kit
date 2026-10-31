import json
import os
import time
from unittest.mock import patch

from test_cli import Fixture, StoreContext
from teamkit.config import binding


class RoutingTests(Fixture):
    def setUp(self):
        super().setUp()
        self.value['routing'] = {
            'small': {'provider':'codex','model':'small-fixture','effort':'low'},
            'general': {'provider':'codex','model':'general-fixture','effort':'medium'},
            'design': {'provider':'codex','model':'leader-fixture','effort':'medium'},
            'wording': {'provider':'codex','model':'review-fixture','effort':'medium'}}
        self.config.write_text(json.dumps(self.value))

    def make(self, *extra):
        return self.call('create','T1','--team','alpha','--root',str(self.repo),
                         '--goal-file',str(self.root/'goal.txt'),*extra)

    def test_small_task_selects_explicit_model_without_claiming(self):
        self.make('--work-kind','small')
        plan = self.call('plan','T1')
        self.assertEqual((plan['model'],plan['effort']),('small-fixture','low'))
        self.assertEqual(self.call('show','T1')['state'],'queued')

    def test_risky_small_task_uses_general_medium(self):
        for risk in ('security','jni','authentication','concurrency'):
            with self.subTest(risk=risk):
                from teamkit.routing import execution_policy
                task={'team':'alpha','role':'implementer','classification':{'kind':'small','risks':[risk]}}
                plan=execution_policy(self.value,task)
                self.assertEqual((plan['model'],plan['effort']),('general-fixture','medium'))

    def test_same_id_different_classification_rejected(self):
        self.make('--work-kind','small')
        with self.assertRaises(ValueError): self.make()

    def test_failure_history_survives_handoffs_and_raises_effort(self):
        from teamkit.routing import execution_policy
        task=self.make()
        for n in range(2):
            with StoreContext(self.state) as db:
                db.claim('T1',str(n),'codex','team','general-fixture','medium')
                db.handoff('T1',str(n),{'head':task['base'],'dirty':False,
                    'verification':['fixture assertion failed'],'next':'retry after diagnosis',
                    'failure':{'cause':'same-root-cause','kind':'reasoning'}},True)
        task=self.call('show','T1')
        self.assertEqual(execution_policy(self.value,task)['effort'],'high')

    def test_environment_errors_do_not_escalate_and_high_failure_requires_user(self):
        from teamkit.routing import execution_policy
        task=self.make()
        task['failures']=[{'cause':'compiler-missing','kind':'environment','effort':'medium'}]*3
        self.assertEqual(execution_policy(self.value,task)['effort'],'medium')
        task['failures'].append({'cause':'unknown','kind':'reasoning','effort':'high'})
        self.assertEqual(execution_policy(self.value,task)['action'],'ask_user')

    def test_missing_evidence_escalates_and_typo_does_not(self):
        from teamkit.routing import execution_policy
        task=self.make()
        task['failures']=[{'cause':'invariant-unproven','kind':'insufficient_evidence','effort':'medium'}]
        self.assertEqual(execution_policy(self.value,task)['effort'],'high')
        task['failures']=[{'cause':'typo','kind':'typo','effort':'medium'}]*4
        self.assertEqual(execution_policy(self.value,task)['effort'],'medium')

    def test_dispatch_uses_selected_model_and_same_provider_personal_fallback(self):
        self.make('--work-kind','small')
        now=time.time()
        with StoreContext(self.state) as db:
            db.quota('codex/personal', {'binding':binding(self.value['profiles']['codex']['personal']),
                'observed_at':now,'five_hour':{'used':1,'resets_at':now+900},'weekly':{'used':1,'resets_at':now+900}})
        with patch.dict(os.environ,{'HERDR_ENV':'1'}),patch('teamkit.providers.identity'),patch('teamkit.__main__.Herdr') as h:
            h.return_value.launch.return_value={'name':'fixture','pane':'w1:p1'}
            task=self.call('dispatch','T1')
            spec=h.return_value.launch.call_args.args[1]
            self.assertEqual(task['account'],'personal')
            self.assertIn('small-fixture',spec['args'])
            self.assertIn('model_reasoning_effort="low"',spec['args'])

    def test_invalid_failure_is_rejected_without_releasing_owner(self):
        task=self.make()
        with StoreContext(self.state) as db:
            db.claim('T1','owner','codex','team','general-fixture','medium')
            with self.assertRaises(ValueError):
                db.handoff('T1','owner',{'head':task['base'],'dirty':False,'verification':['failed'],
                    'next':'retry','failure':{'cause':'x','kind':'unrecognized'}},True)
            self.assertEqual(db.get('T1')['state'],'claimed')

    def test_high_failure_blocks_dispatch_before_auth_or_launch(self):
        task=self.make()
        with StoreContext(self.state) as db:
            db.claim('T1','owner','codex','team','general-fixture','high')
            db.handoff('T1','owner',{'head':task['base'],'dirty':False,'verification':['still failing'],
                'next':'report unresolved issue','failure':{'cause':'same','kind':'reasoning'}},True)
        with patch.dict(os.environ,{'HERDR_ENV':'1'}),patch('teamkit.providers.identity') as auth,patch('teamkit.__main__.Herdr') as h:
            self.assertEqual(self.call('dispatch','T1')['action'],'ask_user')
            auth.assert_not_called();h.assert_not_called()
        self.assertEqual(self.call('show','T1')['state'],'handed_off')

    def test_other_provider_quota_cannot_change_selected_provider(self):
        self.make('--work-kind','small')
        with patch.dict(os.environ,{'HERDR_ENV':'1'}),patch('teamkit.providers.identity') as auth,patch('teamkit.__main__.Herdr') as h:
            self.assertEqual(self.call('dispatch','T1')['action'],'wait')
            auth.assert_not_called();h.assert_not_called()
        self.assertEqual(self.call('show','T1')['state'],'queued')

    def test_work_kind_must_match_role(self):
        for kind in ('design','wording'):
            with self.assertRaises(ValueError):self.make('--work-kind',kind)
