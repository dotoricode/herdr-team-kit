import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from teamkit.operations import prompt
from teamkit.resources import ResourceLedger
from teamkit.tickets import TicketStore, digest


class TicketResourceContractTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.work=self.root/'work';self.work.mkdir()
        self.other=self.root/'other-worktree';self.other.mkdir()
        self.state=self.root/'state.sqlite';self.queue=TicketStore(self.state);self.addCleanup(self.queue.close)
        self.approval=self.root/'approval';self.approval.write_text('Approved isolated fixture worktree paths.')
        def forbidden(*args,**kwargs):raise AssertionError('Real HC, git, model and auth calls are forbidden')
        for target in ('subprocess.run','subprocess.Popen','subprocess.check_output',
                       'teamkit.core_runtime._call','teamkit.providers.identity',
                       'teamkit.admission.native_get','teamkit.admission.observe_native'):
            guard=patch(target,side_effect=forbidden);guard.start();self.addCleanup(guard.stop)

    def spec(self,ident='ONE',root=None,mode='write',**changes):
        root=root or self.work
        value=dict(id=ident,product='example',role='implementer',kind='code',goal='Inspect only the fixture path',
            completion=['bounded result'],checks=['report'],verification_commands={'report':['fixture-verifier']},
            profiles=['codex/personal'],dependencies=[],resources=[dict(kind='path',name=str(root/(ident+'.txt')),mode=mode)],
            scope=dict(paths=[str(root)],actions=['read','write'] if mode=='write' else ['read'],data=['fixture']),
            approval=dict(state='approved',reference=str(self.approval)),
            workflow_ref={'root':str(root),'id':'WORK-fixture','revision':1},
            budget=dict(tickets=8,attempts=12,tokens=10000),estimated_tokens=10)
        value.update(changes);return value

    def actor(self,ident):
        return dict(provider='codex',account='personal',owner='owner-'+ident,terminal='t'+ident.lower(),pid=100+len(ident))

    def reserve(self,ident):
        return self.queue.reserve(ident,self.actor(ident),{'global_limit':4},dict(observed_at=time.time(),complete=True,terminals=[]))

    def test_unlimited_policy_admits_four_profiles_with_each_designated_account(self):
        from teamkit.admission import configure
        from teamkit.resources import PROFILES
        configure(self.queue.db, {'global_limit': None})
        for number, profile in enumerate(PROFILES):
            ident = 'UNLIMITED' + str(number)
            root = self.root / ident
            root.mkdir()
            self.queue.enqueue(self.spec(ident, root=root, profiles=[profile]), ident, user_approved=True)
            provider, account = profile.split('/')
            actor = dict(provider=provider, account=account, owner='owner-' + ident,
                         terminal='t' + ident, pid=200 + number)
            if number == 3:
                with self.assertRaisesRegex(ValueError, 'identity/profile mismatch'):
                    self.queue.reserve(ident, {**actor, 'owner': None}, None,
                                       dict(observed_at=time.time(), complete=True, terminals=[]))
            self.queue.reserve(ident, actor, None,
                               dict(observed_at=time.time(), complete=True, terminals=[]))
        self.assertEqual(4, sum(row['execution_reserved'] for row in ResourceLedger._rows(self.queue.db)))

    def test_same_worktree_different_file_writers_run_in_parallel(self):
        for ident in ('ONE','TWO'):self.queue.enqueue(self.spec(ident),ident,user_approved=True)
        self.reserve('ONE');self.reserve('TWO')
        self.assertEqual(2,sum(r['execution_reserved'] for r in ResourceLedger._rows(self.queue.db)))

    def test_different_worktrees_can_reserve_parallel_writers(self):
        self.queue.enqueue(self.spec(),'one',user_approved=True)
        self.queue.enqueue(self.spec('TWO',root=self.other),'two',user_approved=True)
        self.reserve('ONE');self.reserve('TWO')
        self.assertEqual(2,sum(x['execution_reserved'] for x in ResourceLedger._rows(self.queue.db)))

    def test_worktree_symlink_alias_does_not_bypass_the_exclusion_lock(self):
        alias=self.root/'work-alias';alias.symlink_to(self.work,target_is_directory=True)
        self.queue.enqueue(self.spec(),'one',user_approved=True)
        self.queue.enqueue(self.spec('TWO',root=alias,resources=[dict(kind='path',name=str(alias/'ONE.txt'),mode='write')]),'two',user_approved=True)
        self.reserve('ONE')
        with self.assertRaisesRegex(ValueError,'conflicting resource'):self.reserve('TWO')

    def test_review_child_inherits_same_worktree_read_constraint(self):
        self.queue.enqueue(self.spec(),'one',user_approved=True);attempt=self.reserve('ONE');actor=self.actor('ONE')
        self.queue.receipt('ONE',attempt['id'],actor['owner'],actor['terminal'],dict(actor,status='idle'))
        artifact=self.work/'report';artifact.write_text('Fixture report')
        self.queue.submit('ONE',attempt['id'],actor['owner'],dict(artifact=str(artifact),writer_stopped=True,
            source=dict(root=str(self.work),head='a'*40,fingerprint='b'*64)))
        self.queue.record_stop('ONE',attempt['id'],dict(actor,status='idle'))
        child=self.spec('REVIEW',mode='read',parent_id='ONE',relation='required',role='reviewer',kind='review')
        saved=self.queue.enqueue(child,'review')
        self.assertEqual('pending',saved['state'])
        review=self.reserve('REVIEW')
        self.assertEqual([],review['resource_constraints'])
        self.assertEqual(self.queue.get('ONE')['workflow_ref'],saved['workflow_ref'])
        self.queue.enqueue(self.spec('TWO'),'two',user_approved=True)
        # The review's read constraint does not block a writer of the same worktree.
        self.assertEqual('TWO',self.reserve('TWO')['ticket'])

    def test_required_child_wait_checks_overlapping_declared_write(self):
        self.queue.enqueue(self.spec(),'one',user_approved=True);attempt=self.reserve('ONE');actor=self.actor('ONE')
        self.queue.receipt('ONE',attempt['id'],actor['owner'],actor['terminal'],dict(actor,status='idle'))
        self.queue.enqueue(self.spec('CHILD',mode='write',parent_id='ONE',relation='required',
            resources=[dict(kind='path',name=str(self.work/'ONE.txt'),mode='write')]),'child')
        with self.assertRaisesRegex(ValueError,'CHILD.*--release-writes'):
            self.queue.request_children_wait('ONE',attempt['id'],actor['owner'])
        self.assertEqual('running',self.queue.get('ONE')['state'])

    def test_required_read_child_wait_is_not_blocked_by_the_parent_worktree_write(self):
        self.queue.enqueue(self.spec(),'one',user_approved=True);attempt=self.reserve('ONE');actor=self.actor('ONE')
        self.queue.receipt('ONE',attempt['id'],actor['owner'],actor['terminal'],dict(actor,status='idle'))
        self.queue.enqueue(self.spec('CHILD',mode='read',parent_id='ONE',relation='required'),'child')
        self.queue.request_children_wait('ONE',attempt['id'],actor['owner'])

    def test_prompt_exposes_original_contract_and_separate_host_constraints(self):
        original=self.spec();self.queue.enqueue(original,'one',user_approved=True);attempt=self.reserve('ONE')
        text=prompt(self.root/'config.json',self.state,self.queue.get('ONE'),attempt)
        contract=next(json.loads(line) for line in text.splitlines() if line.startswith('{'))
        for name in ('resources','profiles','dependencies','workflow_ref','scope'):
            self.assertEqual(original[name],contract[name])
        self.assertEqual(attempt['resource_constraints'],contract['resource_constraints'])
        self.assertIn('named leases do not grant file access',text)
        self.assertIn('declared path resources',text)
        self.assertIn('linked child',text)

    def test_prompt_keeps_missing_legacy_dependencies_as_empty(self):
        original=self.spec();original.pop('dependencies')
        self.queue.enqueue(original,'one',user_approved=True);attempt=self.reserve('ONE')
        text=prompt(self.root/'config.json',self.state,self.queue.get('ONE'),attempt)
        contract=next(json.loads(line) for line in text.splitlines() if line.startswith('{'))
        self.assertEqual([],contract['dependencies'])

    def test_worker_fence_requires_the_exclusion_lease_to_remain_held(self):
        from teamkit.workers import _fence, _ticket_signature
        from teamkit.store import Store
        self.queue.enqueue(self.spec(),'one',user_approved=True);attempt=self.reserve('ONE')
        attempt['executor']['ticket_signature']=_ticket_signature(self.queue.get('ONE'))
        self.queue._save_attempt(attempt)
        lease=ResourceLedger._rows(self.queue.db)[0];lease['writes_held']=False
        ResourceLedger._write(self.queue.db,lease)
        store=Store(self.state)
        try:
            with self.assertRaisesRegex(ValueError,'resource reservation'):
                _fence(store,self.queue.get('ONE'),attempt)
        finally:store.close()

    def test_named_constraint_does_not_expand_path_permissions_or_change_signature(self):
        original=self.spec();saved=self.queue.enqueue(original,'one',user_approved=True)
        attempt=self.reserve('ONE')
        self.assertEqual(original['resources'],saved['resources'])
        self.assertEqual(original['resources'],self.queue.get('ONE')['resources'])
        self.assertEqual(digest(original),self.queue.db.execute('SELECT digest FROM operation_tickets WHERE id=?',('ONE',)).fetchone()[0])
        self.assertEqual(self.queue.get('ONE'),self.queue.enqueue(copy.deepcopy(original),'one',user_approved=True))
        self.assertEqual([],attempt['resource_constraints'])
        outside=self.spec('OUTSIDE',resources=[dict(kind='path',name=str(self.other/'bad.txt'),mode='write'),
                                            dict(kind='named',name='worktree:'+str(self.work),mode='write')])
        with self.assertRaisesRegex(ValueError,'outside approved paths'):
            self.queue.enqueue(outside,'outside',user_approved=True)

    def test_narrow_child_cannot_upgrade_an_explicit_parent_named_read_lease(self):
        parent=self.spec(mode='read');parent['resources'].append(dict(kind='named',name='branch:fixture',mode='read'))
        self.queue.enqueue(parent,'one',user_approved=True)
        child=self.spec('CHILD',parent_id='ONE',relation='required')
        child['resources'].append(dict(kind='named',name='branch:fixture',mode='write'))
        self.assertEqual('waiting_approval',self.queue.enqueue(child,'child')['state'])

    def test_outer_write_approval_does_not_create_a_write_lease_for_declared_read_access(self):
        original=self.spec(mode='read',scope=dict(paths=[str(self.work)],actions=['read','write'],data=['fixture']))
        original['resources'].append(dict(kind='named',name='worktree:'+str(self.work),mode='read'))
        self.queue.enqueue(original,'one',user_approved=True);attempt=self.reserve('ONE')
        self.assertEqual(original['resources'],self.queue.get('ONE')['resources'])
        self.assertEqual([],attempt['resource_constraints'])
        self.assertEqual('read',self.queue.get('ONE')['resources'][0]['mode'])

    def test_approval_template_is_read_only_and_matches_the_exact_immutable_contract(self):
        original=self.spec();self.queue.enqueue(original,'one')
        changes=self.queue.db.total_changes
        readonly=TicketStore(self.state,read_only=True)
        try:template=readonly.approval_template('ONE')
        finally:readonly.close()
        self.assertEqual({'requires_user_approval':True,'packet':dict(ticket='ONE',scope_digest=digest(original['scope']),
            contract_digest=digest(original),approved_by='사용자')},template)
        self.assertEqual(changes,self.queue.db.total_changes)
        self.assertEqual('waiting_approval',self.queue.get('ONE')['state'])
        approval=self.root/'explicit-approval.json';approval.write_text(json.dumps(template['packet']))
        self.assertEqual('pending',self.queue.approve('ONE',approval)['state'])
