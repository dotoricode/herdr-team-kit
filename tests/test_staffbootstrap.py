"""Real startup contracts exercised through fake native/provider boundaries."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_workers
from teamkit import docsstaff, staffbootstrap as staff, operations, workers
from teamkit.config import binding


class StaffBootstrapTests(unittest.TestCase):
    setUp = test_workers.WorkerTests.setUp
    ticket = test_workers.WorkerTests.ticket

    def configure(self):
        self.config['operations'].update(worker_roles={'documenter': 'implementer'},
            document_staff={'department': 'docs', 'profiles': ['codex/personal', 'claude/personal']},
            qa_staff={'department': 'qa', 'roles': ['investigator']},
            departments={name: {'root': str(self.root), 'roles': {
                'implementer': {'provider': 'claude', 'model': 'writing-model', 'effort': 'medium'},
                'leader': {'provider': 'claude', 'model': 'general-qa-model', 'effort': 'medium'},
                'reviewer': {'provider': 'codex', 'model': 'specialist-model', 'effort': 'high'}}}
                for name in ('docs', 'qa')})
        self.config['execution_preference'] = {'provider_order': ['claude', 'codex'], 'weekend_account_first': True}
        self.config_path.write_text(json.dumps(self.config))

    def native(self, task):
        task = {**self.store.get(task['id']), **task}
        return dict(address='t100', state='live', name=task['runtime']['name'], turn=0,
            labels={'teamkit.owner': task['owner'], 'teamkit.task': task['id'], 'teamkit.name': task['runtime']['name']},
            foreground=dict(pid=123, name=task['provider'], cwd=str(self.root),
                argv=['--model', task['model'], '--effort', task['effort']] if task['provider'] == 'claude' else
                ['--model', task['model'], '-c', 'model_reasoning_effort="medium"', '--no-daemon']),
            status={'app': 'claude-code' if task['provider'] == 'claude' else 'codex', 'state': 'idle'})

    def launch(self, task, spec, name, on_created):
        self.launched.append((task, spec, name))
        on_created(dict(backend='core', name=name, terminal='t100', pane='t100', session_cwd=task['root']))
        on_created(dict(backend='core', name=name, terminal='t100', pane='t100', session_cwd=task['root'], native_pid=123))

    def accept(self, runtime, name, text, before_send=None):
        if before_send:
            before_send()
        self.messages.append(text)
        return dict(accepted=True, terminal='t100', native_pid=123, turn=1)

    def runtime_call(self, *args, **kwargs):
        if args[0] == 'read':
            return {'complete': True, 'text': '❯ '}
        raise AssertionError('unexpected native action: ' + str(args))

    def fake_boundaries(self):
        self.launched, self.messages = [], []
        for target, kwargs in [
            ('teamkit.staffbootstrap.require_managed', {'return_value': 't1'}),
            ('teamkit.staffbootstrap.providers.identity', {'return_value': {'email': 'person@example.invalid'}}),
            ('teamkit.staffbootstrap.observe_native', {'side_effect': lambda: {'complete': True, 'observed_at': time.time(), 'terminals': []}}),
            ('teamkit.staffbootstrap._call', {'side_effect': self.runtime_call}),
            ('teamkit.staffbootstrap._hint', {'return_value': None}),
            ('teamkit.staffbootstrap.CoreRuntime.launch', {'side_effect': self.launch}),
            ('teamkit.staffbootstrap.CoreRuntime._native', {'new': lambda runtime, *args, **kwargs: self.native(runtime.task)}),
            ('teamkit.staffbootstrap.CoreRuntime.accept', {'new': lambda runtime, name, text, before_send=None: self.accept(runtime, name, text, before_send)}),
        ]:
            mocked = patch(target, **kwargs)
            mocked.start()
            self.addCleanup(mocked.stop)

    def prepare(self, department='docs', provider='claude', resume=None):
        return staff.prepare(self.config_path, self.state, department, provider, resume)

    def receive(self, record):
        actor = record['actor']
        key = 'CODEX_HOME' if actor['provider'] == 'codex' else 'CLAUDE_CONFIG_DIR'
        with patch.dict(os.environ, {'HC_TERMINAL_ID': actor['terminal'], 'TEAMKIT_OWNER': actor['owner'],
                'TEAMKIT_TASK': actor['task'], key: self.config['profiles'][actor['provider']]['personal']['home']}):
            return staff.receipt(self.config_path, self.state, actor['product'], actor['provider'],
                                 actor['owner'], record['receipt_nonce'])

    def test_prepare_receipt_finalize_register_real_actor_without_product_ticket(self):
        self.configure(); self.fake_boundaries()
        before = self.queue.list()
        record = self.prepare()
        self.assertEqual(record['phase'], 'awaiting_receipt')
        self.assertEqual(len(self.launched), 1)
        self.assertIn('Do not perform product work', self.messages[0])
        self.assertEqual(staff.executors(self.state), [])
        self.receive(record)
        ready = staff.finalize(self.config_path, self.state, 'docs', 'claude')
        self.assertEqual(ready['phase'], 'ready')
        self.assertTrue(ready['actor']['runtime']['identity_checked'])
        self.assertEqual(staff.executors(self.state), [ready['actor']])
        self.assertEqual(self.queue.list(), before)
        self.assertEqual(self.prepare()['actor']['owner'], ready['actor']['owner'])
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(len(self.messages), 1)

    def test_duplicate_prepare_and_uncertain_launch_do_not_create_second_dialog_or_resend(self):
        self.configure(); self.fake_boundaries()
        with patch.object(staff.CoreRuntime, 'launch', side_effect=ValueError('native timeout')) as launch:
            with self.assertRaisesRegex(ValueError, 'native timeout'):
                self.prepare()
            self.assertEqual(self.prepare()['phase'], 'uncertain')
            self.assertEqual(launch.call_count, 1)
        self.assertEqual(self.messages, [])

    def test_ready_registration_requires_actual_receipt(self):
        self.configure(); self.fake_boundaries()
        self.prepare()
        with self.assertRaisesRegex(ValueError, 'actual native staff receipt'):
            staff.finalize(self.config_path, self.state, 'docs', 'claude')
        self.assertEqual(staff.executors(self.state), [])

    def test_wrong_profile_owner_and_terminal_cannot_receipt(self):
        self.configure(); self.fake_boundaries()
        record = self.prepare()
        with self.assertRaisesRegex(ValueError, 'exact native owner'):
            staff.receipt(self.config_path, self.state, 'docs', 'claude', record['actor']['owner'], record['receipt_nonce'])
        with self.assertRaisesRegex(ValueError, 'exact native owner'):
            staff.receipt(self.config_path, self.state, 'docs', 'claude', 'other-owner', record['receipt_nonce'])

    def test_busy_or_input_draft_keeps_staff_unregistered(self):
        self.configure(); self.fake_boundaries()
        record = self.prepare(); self.receive(record)
        with patch.object(staff, '_call', return_value={'complete': True, 'text': '❯ preserve my draft'}):
            with self.assertRaisesRegex(ValueError, 'not empty'):
                staff.finalize(self.config_path, self.state, 'docs', 'claude')
        self.assertEqual(staff.executors(self.state), [])

    def test_qa_only_has_personal_claude_investigator_and_cannot_expand_ui_or_review(self):
        self.configure(); self.fake_boundaries()
        record = self.prepare('qa')
        self.assertEqual(record['actor']['roles'], ['investigator'])
        self.assertEqual(record['actor']['model'], 'general-qa-model')
        self.assertEqual(record['actor']['account'], 'personal')
        with self.assertRaisesRegex(ValueError, 'not explicitly configured'):
            self.prepare('qa', 'codex')
        # Existing UI-lab owns UI admission and receipt; staffing must not intercept it.
        self.assertIsNone(docsstaff.check(self.config, self.store.db, dict(product='qa', role='ui'),
                                         {'provider': 'codex', 'account': 'personal', 'roles': ['ui']}))
        with self.assertRaisesRegex(ValueError, 'does not support'):
            docsstaff.check(self.config, self.store.db, dict(product='qa', role='reviewer'), record['actor'])
        ticket = self.ticket(role='reviewer', kind='review', product='qa')
        self.assertEqual(workers.plans(self.config, self.state, ticket), [])
        self.assertEqual(self.config['operations']['departments']['qa']['roles']['reviewer']['model'], 'specialist-model')

    def test_identity_quota_and_mcp_fail_before_any_model_launch(self):
        self.configure(); self.fake_boundaries()
        with patch.object(staff.providers, 'identity', side_effect=ValueError('wrong identity')):
            with self.assertRaisesRegex(ValueError, 'wrong identity'):
                self.prepare()
        with patch.object(staff, 'cached_quota', return_value=None):
            with self.assertRaisesRegex(ValueError, 'quota'):
                self.prepare()
        with patch.object(staff, '_atlassian_registered', return_value=False):
            with self.assertRaisesRegex(ValueError, 'Atlassian'):
                self.prepare()
        self.assertEqual(self.launched, [])
        self.assertEqual(self.messages, [])

    def test_staff_intent_blocks_ticket_worker_creation_before_receipt(self):
        self.configure(); self.fake_boundaries()
        self.prepare()
        ticket = self.ticket(role='documenter', kind='document', product='docs')
        plans = workers.plans(self.config, self.state, ticket)
        self.assertNotIn('claude', {p['provider'] for p in plans})
        self.assertEqual({p['account'] for p in plans}, {'personal'})

    def test_codex_staff_uses_official_launch_permissions_and_personal_profile(self):
        self.configure(); self.fake_boundaries()
        record = self.prepare(provider='codex')
        spec = self.launched[0][1]
        self.assertEqual(spec['env']['CODEX_HOME'], self.config['profiles']['codex']['personal']['home'])
        self.assertIn('--no-daemon', spec['args'])
        self.assertIn('--approve-for-me', spec['args'])
        self.receive(record)
        self.assertEqual(staff.finalize(self.config_path, self.state, 'docs', 'codex')['phase'], 'ready')

    def test_configuration_change_during_final_identity_check_prevents_input(self):
        self.configure(); self.fake_boundaries()
        calls = []
        def identity(*args):
            calls.append(True)
            if len(calls) == 2:
                self.config_path.write_text(self.config_path.read_text() + ' ')
        with patch.object(staff.providers, 'identity', side_effect=identity):
            with self.assertRaisesRegex(ValueError, 'configuration changed'):
                self.prepare()
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.messages, [])
        self.assertEqual(staff.executors(self.state), [])

    def test_checkpoint_and_stopped_exact_session_resume_preserve_the_original_journal(self):
        self.configure(); self.fake_boundaries()
        pr = self.config['profiles']['claude']['personal']
        journal = Path(pr['home']) / 'projects' / 'fixture-session.jsonl'
        journal.parent.mkdir()
        journal.write_text(json.dumps({'sessionId': 'fixture-session', 'cwd': str(self.root)}) + '\n')
        sessions = Path(pr['home']) / 'sessions'
        sessions.mkdir()
        (sessions / '999.json').write_text(json.dumps({'sessionId': 'fixture-session', 'cwd': str(self.root)}))
        original = dict(address='t99', state='live', turn=0, foreground=dict(pid=999, name='claude', cwd=str(self.root)),
                        status={'state': 'idle', 'app': 'claude-code'})
        def original_call(*args):
            return {'complete': True, 'text': '❯ '} if args[0] == 'read' else original
        with patch.object(staff, '_call', side_effect=original_call), \
                patch.object(staff.subprocess, 'run', return_value=SimpleNamespace(stdout='CLAUDE_CONFIG_DIR=' + pr['home'])):
            proof = staff.checkpoint(self.config_path, self.state, 'docs', 'claude', 't99', 999,
                                     'fixture-session', str(journal))
        self.assertEqual(proof['native_session_record']['sessionId'], 'fixture-session')
        with self.assertRaisesRegex(ValueError, 'exact --resume'):
            self.prepare()
        def stopped_call(*args):
            if args[0] == 'get':
                return {'address': 't99', 'state': 'exited', 'foreground': None}
            if args[0] == 'ls':
                return {'terminals': []}
            return {'complete': True, 'text': '❯ '}
        before = journal.read_bytes()
        with patch.object(staff.os, 'kill', side_effect=ProcessLookupError), \
                patch.object(staff, '_call', side_effect=stopped_call):
            record = self.prepare(resume='fixture-session')
        self.assertEqual(record['session'], 'fixture-session')
        self.assertEqual(self.launched[0][1]['args'][:2], ['--resume', 'fixture-session'])
        self.assertEqual(journal.read_bytes(), before)

    def test_resume_without_official_proof_never_launches(self):
        self.configure(); self.fake_boundaries()
        with self.assertRaisesRegex(ValueError, 'official original-native checkpoint'):
            self.prepare(resume='fixture-session')
        self.assertEqual(self.launched, [])

    def test_resume_requires_stopped_pid_and_unchanged_profile_journal(self):
        self.configure(); self.fake_boundaries()
        pr = self.config['profiles']['claude']['personal']
        journal = Path(pr['home']) / 'projects' / 'fixture-session.jsonl'
        journal.parent.mkdir()
        journal.write_text(json.dumps({'sessionId': 'fixture-session', 'cwd': str(self.root)}) + '\n')
        proof = dict(seat='docs/claude', terminal='t99', pid=999, session='fixture-session',
            profile_binding=binding(pr), journal=staff._journal(pr, 'claude', 'fixture-session', str(self.root), journal))
        staff._schema(self.store.db)
        self.store.db.execute('INSERT INTO staff_resume_proofs VALUES (?,?)', ('docs/claude', json.dumps(proof)))
        with patch.object(staff.os, 'kill', return_value=None):
            with self.assertRaisesRegex(ValueError, 'PID still exists'):
                self.prepare(resume='fixture-session')
        self.assertEqual(self.launched, [])
        journal.write_text(journal.read_text() + json.dumps({'type': 'user', 'message': 'new input'}) + '\n')
        with patch.object(staff.os, 'kill', side_effect=ProcessLookupError), \
                patch.object(staff, '_call', return_value={'foreground': None}):
            with self.assertRaisesRegex(ValueError, 'journal changed'):
                self.prepare(resume='fixture-session')
        self.assertEqual(self.launched, [])


if __name__ == '__main__':
    unittest.main()
