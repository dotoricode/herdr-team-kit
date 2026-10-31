import json
import os
import time
from unittest.mock import patch

from test_cli import Fixture, StoreContext
from teamkit.config import binding
from teamkit.receipt import PROTOCOL, requested, verify_pending
from teamkit.providers import CodexRPC
from teamkit.runtime import RuntimeError


class ReceiptTests(Fixture):
    def submitted(self):
        self.create()
        with StoreContext(self.state) as db:
            db.claim('T1','owner','codex','team','fixture','medium')
            db.update('T1','owner','launching',{'name':'native','receipt_protocol':PROTOCOL,
                       'profile_binding':binding(self.value['profiles']['codex']['team'])})
            return db.update('T1','owner','submitted',{})

    def request(self, db):
        return requested(db,'T1','owner',binding(self.value['profiles']['codex']['team']))

    def test_dispatch_host_checks_fresh_identity_after_worker_request(self):
        self.create();now=time.time()
        with StoreContext(self.state) as db:
            db.quota('codex/team',{'binding':binding(self.value['profiles']['codex']['team']),
                'observed_at':now,'five_hour':{'used':1,'resets_at':now+900},'weekly':{'used':1,'resets_at':now+900}})
        def worker(*args):
            task=self.call('show','T1')
            self.call('receipt','T1','--owner',task['owner'])
        with patch.dict(os.environ,{'HERDR_ENV':'1','CODEX_HOME':str(self.root/'team')}),patch('teamkit.providers.identity') as identity,patch('teamkit.__main__.Herdr') as runtime:
            runtime.return_value.launch.return_value={'name':'native','pane':'w1:p1'}
            runtime.return_value.prompt.side_effect=worker
            result=self.call('dispatch','T1')
            self.assertEqual(identity.call_count,2)
        self.assertEqual(result['state'],'running')
        self.assertEqual(result['runtime']['identity_source'],'host-fresh-provider')
        events=[e['event'] for e in self.call('events','T1')]
        self.assertLess(events.index('receipt_requested'),events.index('receipted'))

    def test_changed_account_fails_without_accepting_preflight(self):
        self.submitted()
        with StoreContext(self.state) as db:
            self.request(db)
            with patch('teamkit.providers.identity',side_effect=RuntimeError('account changed')):
                verify_pending(db,self.config,'T1','owner')
            self.assertEqual(db.get('T1')['state'],'uncertain')
            self.assertFalse(db.get('T1')['runtime'].get('identity_checked'))

    def test_changed_config_is_rejected_before_identity(self):
        self.submitted()
        with StoreContext(self.state) as db:
            self.request(db)
            self.value['profiles']['codex']['team']['identity']['email']='changed@example.invalid'
            self.config.write_text(json.dumps(self.value))
            with patch('teamkit.providers.identity') as identity:
                verify_pending(db,self.config,'T1','owner');identity.assert_not_called()
            self.assertEqual(db.get('T1')['state'],'uncertain')

    def test_late_identity_cannot_confirm_expired_request(self):
        self.submitted();clock=[100]
        with StoreContext(self.state) as db,patch('teamkit.receipt.time.time',side_effect=lambda:clock[0]):
            self.request(db)
            def late(*args):clock[0]=131
            with patch('teamkit.providers.identity',side_effect=late):verify_pending(db,self.config,'T1','owner')
            self.assertEqual(db.get('T1')['state'],'uncertain')
            self.assertNotIn('identity_checked',db.get('T1')['runtime'])

    def test_owner_change_during_identity_never_overwrites_new_owner(self):
        self.submitted()
        with StoreContext(self.state) as db:
            self.request(db)
            def transfer(*args):
                task=db.get('T1')
                db.handoff('T1','owner',{'head':task['base'],'dirty':False,'verification':['stopped fixture'],'next':'new attempt'},True)
                db.claim('T1','new-owner','codex','team','fixture','medium')
            with patch('teamkit.providers.identity',side_effect=transfer),self.assertRaises(ValueError):
                verify_pending(db,self.config,'T1','owner')
            self.assertEqual(db.get('T1')['owner'],'new-owner')
            self.assertEqual(db.get('T1')['state'],'claimed')

    def test_duplicate_receipt_request_is_not_replayed(self):
        self.submitted()
        with StoreContext(self.state) as db:
            first=self.request(db)
            with self.assertRaises(ValueError):self.request(db)
            self.assertEqual(db.get('T1')['runtime']['receipt_request'],first)

    def test_worker_profile_mismatch_never_requests_host_check(self):
        self.submitted()
        with patch.dict(os.environ,{'CODEX_HOME':str(self.root/'personal')}),patch('teamkit.providers.identity') as identity:
            with self.assertRaises(ValueError):self.call('receipt','T1','--owner','owner')
            identity.assert_not_called()
        self.assertNotIn('receipt_request',self.call('show','T1')['runtime'])

    def test_child_exit_is_not_reported_as_timeout_or_raw_stderr(self):
        binary=self.root/'bin';binary.mkdir();fake=binary/'codex'
        fake.write_text('#!/bin/sh\nprintf "private-diagnostic" >&2\nexit 7\n');fake.chmod(0o700)
        with patch.dict(os.environ,{'PATH':str(binary)+os.pathsep+os.environ['PATH']}):
            with self.assertRaisesRegex(RuntimeError,r'closed stdout.*7') as error:
                with CodexRPC(self.value['profiles']['codex']['team']):pass
        self.assertNotIn('private-diagnostic',str(error.exception))

    def test_config_changed_during_identity_is_rejected(self):
        self.submitted()
        with StoreContext(self.state) as db:
            self.request(db)
            def changed(*args):
                self.value['profiles']['codex']['team']['identity']['email']='new@example.invalid'
                self.config.write_text(json.dumps(self.value))
            with patch('teamkit.providers.identity',side_effect=changed):verify_pending(db,self.config,'T1','owner')
            self.assertEqual(db.get('T1')['state'],'uncertain')
            self.assertNotIn('identity_checked',db.get('T1')['runtime'])

    def test_finished_prompt_does_not_confirm_late_request(self):
        from teamkit.receipt import prompt
        from unittest.mock import MagicMock
        self.submitted()
        with StoreContext(self.state) as db:
            self.request(db)
            with patch('teamkit.receipt.ThreadPoolExecutor') as pool,patch('teamkit.providers.identity') as identity:
                future=pool.return_value.__enter__.return_value.submit.return_value
                future.done.return_value=True
                prompt(MagicMock(),db,self.config,db.get('T1'),'owner','fixture')
                identity.assert_not_called()
            self.assertEqual(db.get('T1')['state'],'submitted')
