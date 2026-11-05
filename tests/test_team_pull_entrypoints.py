import time
import unittest
from unittest.mock import patch

import test_queue_override as fixtures
from teamkit import operations, queue_actions as actions
from teamkit.tickets import TicketStore, digest


class TeamPullEntryTests(unittest.TestCase):
    setUp = fixtures.RealAdmitOverrideTest.setUp
    enqueue = fixtures.RealAdmitOverrideTest.enqueue
    runtime_for = fixtures.RealAdmitOverrideTest.runtime_for
    native_call = fixtures.RealAdmitOverrideTest.native_call

    def test_manual_personal_selection_cannot_bypass_company_first(self):
        self.enqueue('ONE')
        runtime = self.runtime_for(operations={'allocation_policy':'usage'})
        from teamkit.store import Store
        from teamkit.config import binding, profile, load
        loaded=load(self.config)
        personal=runtime.executors()[0]
        company=dict(personal,account='team',terminal='t2',pid=43,owner='company-owner',
                     profile_binding=binding(profile(loaded,'codex','team')))
        runtime.executors=lambda ticket=None:[personal,company]
        runtime.native=lambda e,require_idle=True:dict(terminal=e['terminal'],pid=e['pid'],owner=e['owner'],status='idle',turn=0)
        store=Store(self.state)
        try:
            for account in ('team','personal'):
                store.quota('codex/'+account,dict(source='codex-native-quota',binding=binding(profile(loaded,'codex',account)),
                    observed_at=time.time(),five_hour={'used':10,'resets_at':time.time()+300},weekly={'used':10,'resets_at':time.time()+300}))
        finally:
            store.close()
        with patch('teamkit.core_runtime._call', side_effect=self.native_call), patch('teamkit.providers.identity'):
            choices = actions.candidates(self.config, self.state, 'ONE', runtime=runtime)
            self.assertTrue(next(c for c in choices['candidates'] if c['account']=='team')['eligible'])
            candidate = next(c for c in choices['candidates'] if c['account']=='personal')
            action = actions.enqueue(self.config,self.state,'send','plain',ticket='ONE',candidate_id=candidate['id'],expected_ticket=choices['expected_ticket'],runtime=runtime)
            actions.consume(self.config,self.state,runtime=runtime)
        self.assertEqual('blocked', actions.status(self.state,'plain')['status'])
        self.assertEqual([],runtime.sent)
        self.assertIn('queue pull', actions.status(self.state,'plain')['reason'])
        queue=TicketStore(self.state)
        try:
            self.assertIsNone(actions.selection_hold(queue.db,'ONE'))
            self.assertEqual('pending',queue.get('ONE')['state'])
        finally:
            queue.close()

    def test_real_quota_limit_cannot_be_force_sent(self):
        self.enqueue('ONE')
        runtime = self.runtime_for(operations={'allocation_policy':'usage'})
        quota={'observed_at':time.time(),'five_hour':{'used':90,'resets_at':time.time()+300},'weekly':{'used':10,'resets_at':time.time()+300}}
        from teamkit.store import Store
        from teamkit.config import binding, profile, load
        quota.update(source='codex-native-quota',binding=binding(profile(load(self.config),'codex','personal')))
        store=Store(self.state)
        try:
            store.quota('codex/personal',quota)
        finally:
            store.close()
        with patch('teamkit.core_runtime._call', side_effect=self.native_call), patch('teamkit.providers.identity'):
            choices=actions.candidates(self.config,self.state,'ONE',runtime=runtime)
            candidate=choices['candidates'][0]
            actions.enqueue(self.config,self.state,'send','forced',ticket='ONE',candidate_id=candidate['id'],expected_ticket=choices['expected_ticket'],override=True,override_reason='fixture',runtime=runtime)
            actions.consume(self.config,self.state,runtime=runtime)
        self.assertEqual([],runtime.sent)
        self.assertFalse(candidate['override_allowed'])
        self.assertFalse(candidate['reasons'][0]['bypassable'])
        self.assertEqual('blocked',actions.status(self.state,'forced')['status'])

    def test_direct_tick_manual_selection_rejected_in_usage_mode(self):
        self.enqueue('ONE')
        runtime=self.runtime_for(operations={'allocation_policy':'usage'})
        with patch('teamkit.allocation.refresh_usage'), self.assertRaisesRegex(ValueError,'team pull'):
            operations.tick(self.config,self.state,runtime=runtime,selection={'ticket':'ONE','candidate_id':'unused'})
        self.assertEqual([],runtime.sent)

    def test_preexisting_queued_manual_action_is_rejected_after_switch(self):
        self.enqueue('ONE')
        runtime=self.runtime_for()
        queue=TicketStore(self.state)
        try:
            expected=digest(queue.get('ONE'))
        finally:
            queue.close()
        actions.enqueue(self.config,self.state,'send','legacy',ticket='ONE',
                        candidate_id=actions.executor_identity(runtime.executors()[0]),expected_ticket=expected,runtime=runtime)
        runtime=self.runtime_for(operations={'allocation_policy':'usage'})
        actions.consume(self.config,self.state,runtime=runtime)
        self.assertEqual('blocked',actions.status(self.state,'legacy')['status'])
        self.assertEqual([],runtime.sent)
        queue=TicketStore(self.state)
        try:
            self.assertIsNone(actions.selection_hold(queue.db,'ONE'))
        finally:
            queue.close()

    def test_old_unstarted_blocked_selection_does_not_stall_team_pull(self):
        self.enqueue('ONE')
        runtime=self.runtime_for()
        actions.enqueue(self.config,self.state,'send','stale',ticket='ONE',candidate_id=actions.executor_identity(runtime.executors()[0]),expected_ticket='0'*64,runtime=runtime)
        self.assertEqual('blocked',actions.status(self.state,'stale')['status'])
        runtime=self.runtime_for(operations={'allocation_policy':'usage'})
        from teamkit.config import binding, profile, load
        from teamkit.store import Store
        store=Store(self.state)
        try:
            store.quota('codex/personal',dict(source='codex-native-quota',binding=binding(profile(load(self.config),'codex','personal')),
                observed_at=time.time(),five_hour={'used':10,'resets_at':time.time()+300},weekly={'used':10,'resets_at':time.time()+300}))
        finally:
            store.close()
        with patch('teamkit.providers.identity'), patch('teamkit.allocation.refresh_usage'):
            actions.consume(self.config,self.state,runtime=runtime)
            result=operations.pull(self.config,self.state,'example',ticket='ONE',runtime=runtime)
        self.assertEqual(['ONE'],[x['ticket'] for x in result['started']])
        self.assertEqual(1,len(runtime.sent))
        action=actions.status(self.state,'stale')
        self.assertTrue(action['selection_rejected'])
        self.assertEqual('ticket changed before manual selection; refresh candidates',action['selection_rejection']['original_reason'])

    def test_legacy_hold_recovery_preserves_any_execution_uncertainty(self):
        import copy
        import json
        from teamkit.config import load
        for scenario in ('consumer_started','historical_attempt','unresolved_action','current_attempt'):
            with self.subTest(scenario=scenario):
                ident='ONE-'+scenario
                self.enqueue(ident)
                runtime=self.runtime_for(operations={'allocation_policy':'role'})
                action=actions.enqueue(self.config,self.state,'send','blocked-'+scenario,ticket=ident,
                    candidate_id=actions.executor_identity(runtime.executors()[0]),expected_ticket='0'*64,runtime=runtime)
                queue=TicketStore(self.state)
                try:
                    if scenario=='consumer_started':
                        action['started_at']=time.time();actions._save(queue.db,action)
                    elif scenario=='historical_attempt':
                        queue.db.execute('INSERT INTO operation_attempts VALUES (?,?,?)',('past-'+scenario,ident,json.dumps({'id':'past-'+scenario,'ticket':ident})))
                    elif scenario=='unresolved_action':
                        other=copy.deepcopy(action);other.update(id='unknown-'+scenario,status='uncertain');actions._save(queue.db,other)
                    else:
                        item=queue.get(ident);item['attempt']='past-current';queue._save(item,'fixture_existing_attempt')
                    self.runtime_for(operations={'allocation_policy':'usage'})
                    actions.reject_unstarted_selections(queue,load(self.config))
                    self.assertFalse(actions._read(queue.db,action['id']).get('selection_rejected',False))
                    self.assertIsNotNone(actions.selection_hold(queue.db,ident))
                finally:queue.close()
