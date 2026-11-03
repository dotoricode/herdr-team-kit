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
from teamkit import admission, docsstaff, staffbootstrap as staff, workers, operations
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

    def test_explicit_prepare_uses_current_config_instead_of_stale_stored_admission_policy(self):
        self.configure(); self.fake_boundaries()
        admission.configure(self.store.db, {'global_limit': 1})
        record = self.prepare()
        self.assertEqual(record['phase'], 'awaiting_receipt')
        self.assertEqual(admission.policy_for(None, self.store.db)[1]['global_limit'], 3)

    def resume_fixture(self):
        self.configure(); self.fake_boundaries()
        pr = self.config['profiles']['claude']['personal']
        journal = Path(pr['home']) / 'projects' / 'fixture-session.jsonl'
        journal.parent.mkdir()
        journal.write_text(json.dumps({'sessionId': 'fixture-session', 'cwd': str(self.root)}) + '\n')
        proof = dict(seat='docs/claude', terminal='t99', pid=999, session='fixture-session',
            profile_binding=binding(pr), journal=staff._journal(pr, 'claude', 'fixture-session', str(self.root), journal))
        staff._schema(self.store.db)
        self.store.db.execute('INSERT INTO staff_resume_proofs VALUES (?,?)', ('docs/claude', json.dumps(proof)))
        return pr, proof

    def resume_listing(self, argv):
        native = dict(address='t200', state='live', foreground=dict(pid=555, name='claude',
            cwd=str(self.root), argv=argv), status={'app': 'claude-code', 'state': 'working'})
        def call(*args):
            if args[0] == 'ls':
                return {'terminals': [{'address': 't200', 'state': 'live'}]}
            return native if args[1] == 't200' else {'foreground': None}
        return call

    def test_existing_sid_metadata_blocks_equals_continue_short_and_bare_native(self):
        pr, proof = self.resume_fixture()
        sessions = Path(pr['home']) / 'sessions'
        sessions.mkdir()
        (sessions / '555.json').write_text(json.dumps({'sessionId': proof['session'], 'cwd': str(self.root)}))
        for argv in (['claude', '--resume=fixture-session'], ['claude', '--continue'],
                     ['claude', '-c'], ['claude', '-r', 'fixture-session'], ['claude']):
            with self.subTest(argv=argv), patch.object(staff.os, 'kill', side_effect=ProcessLookupError), \
                    patch.object(staff, '_call', side_effect=self.resume_listing(argv)), \
                    patch.object(staff.subprocess, 'run', return_value=SimpleNamespace(stdout='CLAUDE_CONFIG_DIR=' + pr['home'])):
                with self.assertRaisesRegex(ValueError, 'already resumed'):
                    staff._resume(self.store.db, proof['seat'], proof['session'], pr, 'claude', str(self.root))
        self.assertEqual(self.launched, [])

    def test_missing_or_inconsistent_live_session_metadata_fails_closed(self):
        pr, proof = self.resume_fixture()
        sessions = Path(pr['home']) / 'sessions'
        sessions.mkdir()
        for metadata in (None, {'sessionId': 'other-session'},
                         {'sessionId': 'other-session', 'cwd': '/srv/other'},
                         {'sessionId': 'other-session', 'cwd': str(self.root)}):
            path = sessions / '555.json'
            if metadata is not None:
                path.write_text(json.dumps(metadata))
            with self.subTest(metadata=metadata), patch.object(staff.os, 'kill', side_effect=ProcessLookupError), \
                    patch.object(staff, '_call', side_effect=self.resume_listing(['claude', '--resume=another-session'])), \
                    patch.object(staff.subprocess, 'run', return_value=SimpleNamespace(stdout='CLAUDE_CONFIG_DIR=' + pr['home'])):
                with self.assertRaisesRegex(ValueError, 'session|ambiguous'):
                    staff._resume(self.store.db, proof['seat'], proof['session'], pr, 'claude', str(self.root))
        self.assertEqual(self.launched, [])

    def test_confirmed_different_session_in_same_profile_does_not_block_resume(self):
        pr, proof = self.resume_fixture()
        sessions = Path(pr['home']) / 'sessions'
        sessions.mkdir()
        (sessions / '555.json').write_text(json.dumps({'sessionId': 'other-session', 'cwd': str(self.root)}))
        with patch.object(staff.os, 'kill', side_effect=ProcessLookupError), \
                patch.object(staff, '_call', side_effect=self.resume_listing(['claude', '--continue'])), \
                patch.object(staff.subprocess, 'run', return_value=SimpleNamespace(stdout='CLAUDE_CONFIG_DIR=' + pr['home'])):
            self.assertEqual(staff._resume(self.store.db, proof['seat'], proof['session'], pr, 'claude', str(self.root)), proof)

    def test_process_namespace_matches_existing_runtime_contract_without_host_env_mutation(self):
        self.configure(); self.fake_boundaries()
        default = str(self.root / '.claude')
        with patch.object(staff.Path, 'home', return_value=self.root):
            for observed in (None, '', default, default + '/', str(self.root / 'other')):
                env = {} if observed is None else {'CLAUDE_CONFIG_DIR': observed}
                with self.subTest(observed=observed), patch.dict(os.environ, env, clear=True):
                    self.assertEqual(staff._namespace_matches('claude', {'home': default}, env),
                                     staff.profile_home_matches('claude', default))
                    self.assertEqual(staff._namespace_matches('claude', {'home': str(self.root / 'other')}, env),
                                     staff.profile_home_matches('claude', str(self.root / 'other')))

    def test_default_claude_explicit_home_namespace_cannot_checkpoint(self):
        self.configure(); self.fake_boundaries()
        pr = {**self.config['profiles']['claude']['personal'], 'home': str(self.root / '.claude')}
        sessions = Path(pr['home']) / 'sessions'
        sessions.mkdir(parents=True)
        (sessions / '999.json').write_text(json.dumps({'sessionId': 'fixture-session', 'cwd': str(self.root)}))
        journal = Path(pr['home']) / 'projects' / 'fixture-session.jsonl'
        journal.parent.mkdir()
        journal.write_text(json.dumps({'sessionId': 'fixture-session', 'cwd': str(self.root)}) + '\n')
        original = dict(address='t99', state='live', turn=0, foreground=dict(pid=999, name='claude', cwd=str(self.root)),
                        status={'state': 'idle', 'app': 'claude-code'})
        def call(*args):
            return {'complete': True, 'text': '❯ '} if args[0] == 'read' else original
        preflight = (dict(product='docs', role='documenter'), 'implementer', str(self.root), {}, pr, {})
        with patch.object(staff, '_preflight', return_value=preflight), patch.object(staff.Path, 'home', return_value=self.root), \
                patch.object(staff, '_call', side_effect=call), \
                patch.object(staff.subprocess, 'run', return_value=SimpleNamespace(stdout='CLAUDE_CONFIG_DIR=' + pr['home'])):
            with self.assertRaisesRegex(ValueError, 'namespace'):
                staff.checkpoint(self.config_path, self.state, 'docs', 'claude', 't99', 999, 'fixture-session', str(journal))
        self.assertEqual(self.launched, [])

    def shutdown_fixture(self):
        pr, proof = self.resume_fixture()
        prompt = 'Keep the original approved conversation.'
        prefix = [dict(type='user', uuid='user-1', sessionId=proof['session'], cwd=str(self.root),
                       message={'content': prompt}),
                  dict(type='assistant', uuid='leaf-1', sessionId=proof['session'], cwd=str(self.root),
                       message={'content': []})]
        data = b''.join((json.dumps(row) + '\n').encode() for row in prefix)
        journal = Path(proof['journal']['path'])
        journal.write_bytes(data)
        proof['journal'] = staff._journal(pr, 'claude', proof['session'], str(self.root), journal)
        self.store.db.execute('UPDATE staff_resume_proofs SET body=? WHERE seat=?', (json.dumps(proof), proof['seat']))
        snapshot = self.root / 'original-journal.jsonl'
        snapshot.write_bytes(data)
        cost = dict(type='cost-state', sessionId=proof['session'], totalCostUSD=1.5,
            totalAPIDuration=10, totalAPIDurationWithoutRetries=10, totalToolDuration=1,
            totalLinesAdded=0, totalLinesRemoved=0, totalDuration=100, startTime=1000,
            modelUsage={'fixture-model': dict(inputTokens=1, outputTokens=1, thinkingTokens=0,
                cacheReadInputTokens=0, cacheCreationInputTokens=0, cacheCreationEphemeral5mInputTokens=0,
                cacheCreationEphemeral1hInputTokens=0, webSearchRequests=0, costUSD=1.5)}, hasUnknownModelCost=False)
        rows = [cost, dict(type='last-prompt', sessionId=proof['session'], lastPrompt=prompt, leafUuid='leaf-1'),
                {**cost, 'totalDuration': 101}]
        journal.write_bytes(data + b''.join((json.dumps(row) + '\n').encode() for row in rows))
        return pr, proof, snapshot, data, rows

    def seal_action(self, proof, snapshot):
        return staff.handle(SimpleNamespace(action='seal-checkpoint', config=self.config_path, state=self.state,
            department='docs', provider='claude', session=proof['session'], original_journal=str(snapshot),
            reason='Normal native termination appended verified shutdown metadata.'))

    def stopped_inventory(self, *args):
        return {'terminals': []} if args[0] == 'ls' else {'foreground': None}

    def test_normal_shutdown_metadata_is_sealed_with_original_proof_and_audit(self):
        pr, proof, snapshot, prefix, rows = self.shutdown_fixture()
        journal = Path(proof['journal']['path'])
        before = journal.read_bytes()
        with patch.object(staff.os, 'kill', side_effect=ProcessLookupError), \
                patch.object(staff, '_call', side_effect=self.stopped_inventory):
            with self.assertRaisesRegex(ValueError, 'journal changed'):
                staff._resume(self.store.db, proof['seat'], proof['session'], pr, 'claude', str(self.root))
            sealed = self.seal_action(proof, snapshot)
            self.assertIn('journal', sealed)
            self.assertEqual(sealed['journal']['digest'], staff.file_digest(journal))
            self.assertEqual(sealed['shutdown_seal']['original_checkpoint'], proof)
            self.assertEqual(sealed['shutdown_seal']['prefix_bytes'], len(prefix))
            self.assertEqual(sealed['shutdown_seal']['tail_bytes'], len(before) - len(prefix))
            self.assertEqual(self.seal_action(proof, snapshot), sealed)
            self.assertEqual(staff._resume(self.store.db, proof['seat'], proof['session'], pr, 'claude', str(self.root)), sealed)
        self.assertEqual(journal.read_bytes(), before)
        self.assertEqual(staff.records(self.store.db), [])
        self.assertEqual(self.launched, [])

    def test_shutdown_seal_rejects_new_messages_unknown_metadata_and_unanchored_prompt(self):
        pr, proof, snapshot, prefix, rows = self.shutdown_fixture()
        journal = Path(proof['journal']['path'])
        cases = [
            [*rows, dict(type='user', sessionId=proof['session'], message={'content': 'new work'})],
            [rows[0], {**rows[1], 'lastPrompt': 'Invented work'}, rows[2]],
            [rows[0], {**rows[1], 'leafUuid': 'unknown-leaf'}, rows[2]],
            [{**rows[0], 'message': 'unknown'}, rows[1], rows[2]],
            [{**rows[0], 'totalCostUSD': float('nan')}, rows[1], rows[2]],
            [rows[0], {**rows[1], 'sessionId': 'other-session'}, rows[2]],
            [rows[0], rows[1], {**rows[2], 'totalAPIDuration': 99}],
        ]
        with patch.object(staff.os, 'kill', side_effect=ProcessLookupError), \
                patch.object(staff, '_call', side_effect=self.stopped_inventory):
            for changed in cases:
                with self.subTest(changed_types=[r['type'] for r in changed]):
                    journal.write_bytes(prefix + b''.join((json.dumps(row) + '\n').encode() for row in changed))
                    with self.assertRaisesRegex(ValueError, 'shutdown|prompt|metadata|session'):
                        self.seal_action(proof, snapshot)
                    current = json.loads(self.store.db.execute('SELECT body FROM staff_resume_proofs WHERE seat=?', (proof['seat'],)).fetchone()[0])
                    self.assertEqual(current, proof)
        self.assertEqual(self.launched, [])

    def test_shutdown_seal_rejects_modified_prefix_or_live_original_pid(self):
        pr, proof, snapshot, prefix, rows = self.shutdown_fixture()
        journal = Path(proof['journal']['path'])
        original = journal.read_bytes()
        with patch.object(staff, '_call', side_effect=self.stopped_inventory):
            with patch.object(staff.os, 'kill', return_value=None):
                with self.assertRaisesRegex(ValueError, 'PID still exists'):
                    self.seal_action(proof, snapshot)
            journal.write_bytes(original.replace(b'Keep', b'Lose', 1))
            with patch.object(staff.os, 'kill', side_effect=ProcessLookupError):
                with self.assertRaisesRegex(ValueError, 'prefix'):
                    self.seal_action(proof, snapshot)
        current = json.loads(self.store.db.execute('SELECT body FROM staff_resume_proofs WHERE seat=?', (proof['seat'],)).fetchone()[0])
        self.assertEqual(current, proof)
        self.assertEqual(self.launched, [])

    def test_pending_resume_checkpoint_fences_only_cold_factory_and_keeps_existing_codex(self):
        self.configure(); self.fake_boundaries()
        ticket = self.ticket(role='documenter', kind='document', product='docs')
        codex = next(p for p in workers.plans(self.config, self.state, ticket) if p['provider'] == 'codex')
        codex.update(new=False, terminal='t300', pid=300)
        self.queue._save_attempt(dict(id='completed-codex', ticket=ticket['id'], executor=codex, phase='completed'))
        self.queue._save({**ticket, 'state': 'completed', 'attempt': 'completed-codex'}, 'fixture_completed')
        # A second approved docs request uses its original identity, independently of the completed one.
        spec = {k: ticket[k] for k in ('product', 'role', 'kind', 'goal', 'completion', 'checks', 'scope', 'profiles', 'resources', 'approval')}
        spec['id'] = 'T-pending-docs'
        pending = self.queue.enqueue(spec, 'pending-docs', user_approved=True)
        staff._schema(self.store.db)
        self.store.db.execute('INSERT INTO staff_resume_proofs VALUES (?,?)', ('docs/claude', json.dumps({'session': 'original-session'})))
        self.assertEqual(workers.plans(self.config, self.state, pending), [])
        with patch('teamkit.session_pool._read', return_value={'slots': {}}):
            candidates = operations.NativeRuntime(self.config, self.state).executors(pending)
        self.assertIn(codex, candidates)
        self.assertNotIn('claude', {p['provider'] for p in candidates})

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
