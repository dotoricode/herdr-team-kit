"""Durable tickets, immutable approval scope, fenced attempts and origin budgets.

The legacy task ledger remains the native actor identity source. Tickets express
responsibility independently of those actors; display names are never identities.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid


ROLES = {'leader', 'implementer', 'reviewer', 'investigator', 'documenter', 'ui', 'integrator'}
KINDS = {'code', 'investigation', 'document', 'ui', 'review', 'integration'}
FINAL = {'completed', 'cancelled'}
PROFILE = re.compile(r'(codex|claude)/(team|personal)\Z')
IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_digest(path):
    source = Path(path)
    if not source.is_absolute() or not source.is_file():
        raise ValueError('evidence requires an existing absolute file')
    if source.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('evidence exceeds the 64 MiB bound')
    result = hashlib.sha256()
    with source.open('rb') as stream:
        total = 0
        while block := stream.read(1024 * 1024):
            total += len(block)
            if total > 64 * 1024 * 1024:
                raise ValueError('evidence grew beyond the 64 MiB bound')
            result.update(block)
    return result.hexdigest()


def within(path, roots):
    actual = Path(path).resolve()
    return any(actual.is_relative_to(Path(root).resolve()) for root in roots)


def strings(value, name, nonempty=True):
    if (not isinstance(value, list) or nonempty and not value
            or any(not isinstance(x, str) or not x.strip() for x in value)
            or len(set(value)) != len(value)):
        raise ValueError(name + ' requires distinct nonempty strings')
    return value


class TicketStore:
    def __init__(self, state, clock=time.time, *, read_only=False):
        self.path = Path(state)
        self.clock = clock
        self.read_only = read_only
        if read_only:
            self.db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True,
                                      timeout=10, isolation_level=None)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.db.execute('PRAGMA busy_timeout=10000')
        self.clock = clock
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS operation_tickets
              (id TEXT PRIMARY KEY, registration_key TEXT UNIQUE NOT NULL,
               digest TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operation_attempts
              (id TEXT PRIMARY KEY, ticket TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operation_budgets
              (origin TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operation_events
              (seq INTEGER PRIMARY KEY, ticket TEXT, event TEXT NOT NULL,
               at REAL NOT NULL, body TEXT NOT NULL);
        ''')
        from .intake import schema
        schema(self.db)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def get(self, ident):
        row = self.db.execute('SELECT body FROM operation_tickets WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise ValueError('unknown ticket: ' + ident)
        return json.loads(row[0])

    def list(self):
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operation_tickets'").fetchone():
            return []
        return [json.loads(x[0]) for x in self.db.execute('SELECT body FROM operation_tickets ORDER BY id')]

    def _save(self, item, event, detail=None):
        item['updated_at'] = self.clock()
        self.db.execute('UPDATE operation_tickets SET body=? WHERE id=?',
                        (json.dumps(item), item['id']))
        self.db.execute('INSERT INTO operation_events(ticket,event,at,body) VALUES (?,?,?,?)',
                        (item['id'], event, self.clock(), json.dumps(detail or {})))
        return item

    def _budget(self, origin):
        row = self.db.execute('SELECT body FROM operation_budgets WHERE origin=?', (origin,)).fetchone()
        if not row:
            raise ValueError('origin budget missing')
        return json.loads(row[0])

    def _save_budget(self, origin, budget):
        self.db.execute('INSERT OR REPLACE INTO operation_budgets VALUES (?,?)',
                        (origin, json.dumps(budget)))

    def enqueue(self, spec, registration_key, *, user_approved=False):
        if type(user_approved) is not bool:
            raise ValueError('user_approved must be explicit boolean')
        spec = json.loads(json.dumps(spec))
        ident = spec.get('id', '')
        if not IDENT.fullmatch(ident) or not isinstance(registration_key, str) or not registration_key:
            raise ValueError('ticket identity and idempotency key required')
        if spec.get('role') not in ROLES or spec.get('kind') not in KINDS:
            raise ValueError('explicit ticket role and kind required')
        for field in ('product', 'goal'):
            if not isinstance(spec.get(field), str) or not spec[field].strip():
                raise ValueError(field + ' required')
        for field in ('completion', 'checks', 'profiles'):
            strings(spec.get(field), field)
        if any(not PROFILE.fullmatch(x) for x in spec['profiles']):
            raise ValueError('unknown allowed profile')
        if type(spec.get('priority', 0)) is not int:
            raise ValueError('integer priority required')
        scope = spec.get('scope')
        if not isinstance(scope, dict) or set(scope) != {'paths', 'actions', 'data'}:
            raise ValueError('explicit paths/actions/data scope required')
        for name, values in scope.items():
            strings(values, 'scope.' + name)
        if any(not Path(x).is_absolute() for x in scope['paths']):
            raise ValueError('scope paths must be absolute')
        resources = spec.get('resources')
        if not isinstance(resources, list) or not resources:
            raise ValueError('explicit resources required')
        for resource in resources:
            if (not isinstance(resource, dict) or set(resource) != {'kind', 'name', 'mode'}
                    or resource['kind'] not in ('path', 'named') or resource['mode'] not in ('read', 'write')):
                raise ValueError('invalid resource')
            if resource['kind'] == 'path' and not within(resource['name'], scope['paths']):
                raise ValueError('resource is outside approved paths')
            if resource['mode'] == 'write' and 'write' not in scope['actions']:
                raise ValueError('write resource requires write scope')
        if type(spec.get('estimated_tokens', 4096)) is not int or spec.get('estimated_tokens', 4096) <= 0:
            raise ValueError('positive estimated token reservation required')
        signature = digest(spec)
        with self.transaction():
            previous = self.db.execute('SELECT digest,body FROM operation_tickets WHERE registration_key=? OR id=?',
                                       (registration_key, ident)).fetchall()
            if previous:
                if len(previous) != 1 or previous[0][0] != signature:
                    raise ValueError('registration identity conflicts with immutable instruction')
                return json.loads(previous[0][1])
            parent = self.get(spec['parent_id']) if spec.get('parent_id') else None
            instruction_id = parent.get('instruction_id') if parent else spec.get('instruction_id')
            if instruction_id:
                instruction = self.db.execute('SELECT body FROM operation_instructions WHERE id=?', (instruction_id,)).fetchone()
                if instruction is None:
                    raise ValueError('original instruction must be registered before execution ticket')
            relation = spec.get('relation', 'required')
            if relation not in ('required', 'defect', 'improvement'):
                raise ValueError('unknown follow-up relation')
            if parent:
                if parent['state'] in FINAL:
                    raise ValueError('closed parent requires a new instruction')
                narrowed = (all(set(scope[name]).issubset(parent['scope'][name]) for name in ('actions', 'data'))
                            and all(within(x, parent['scope']['paths']) for x in scope['paths'])
                            and set(spec['profiles']).issubset(parent['profiles'])
                            and spec['product'] == parent['product'])
                same_verifiers = spec.get('verification_commands', {}) == parent.get('verification_commands', {})
                safe_review = False
                if spec.get('role') == 'reviewer' and spec.get('kind') == 'review' and parent.get('attempt'):
                    from .artifact_verifier import command
                    parent_attempt = self.attempt(parent['attempt'])
                    if parent_attempt.get('submission'):
                        expected = {'independent-review-report': command(parent_attempt['submission'])}
                        safe_review = spec.get('checks') == ['independent-review-report'] and spec.get('verification_commands') == expected
                narrowed = narrowed and (same_verifiers or safe_review)
                # These fields affect routing/approval, rather than the requested
                # subtask. Descendants cannot weaken a parent's execution policy.
                for field in ('execution_constraint', 'classification', 'user_decision', 'workflow_ref'):
                    if spec.get(field) != parent.get(field):
                        narrowed = False
                for resource in resources:
                    if resource['kind'] == 'named' and not any(
                            r['kind'] == 'named' and r['name'] == resource['name']
                            and (r['mode'] == 'write' or resource['mode'] == 'read') for r in parent['resources']):
                        narrowed = False
                approval = parent['approval'] if relation != 'improvement' and narrowed else {'state': 'pending'}
                if spec.get('approval', {}).get('reference') not in (None, parent['approval'].get('reference')):
                    raise ValueError('child approval must be explicitly granted as a new instruction')
                origin = parent['origin_id']
            else:
                approval = spec.get('approval', {'state': 'pending'})
                if not user_approved:
                    approval = {'state': 'pending'}
                origin = ident
            if approval.get('state') == 'approved':
                # Descendants retain the original pinned grant, even if its file changed.
                approval = dict(approval) if parent else {**approval, 'digest': file_digest(approval.get('reference', ''))}
            else:
                approval = {'state': 'pending'}
            dependencies = strings(spec.get('dependencies', []), 'dependencies', False)
            for dep in dependencies:
                if dep == ident:
                    raise ValueError('dependency cycle')
                self.get(dep)
            if parent:
                budget = self._budget(origin)
                if budget['created'] >= budget['limits']['tickets']:
                    raise ValueError('origin follow-up budget exhausted; escalate to operations')
            else:
                limits = spec.get('budget', {'tickets': 32, 'attempts': 64, 'tokens': 300000})
                if set(limits) != {'tickets', 'attempts', 'tokens'} or any(type(x) is not int or x <= 0 for x in limits.values()):
                    raise ValueError('positive origin ticket/attempt/token budgets required')
                budget = dict(limits=limits, created=0, attempts=0, tokens_reserved=0, tokens_spent=0)
            budget['created'] += 1
            self._save_budget(origin, budget)
            state = 'backlog' if parent and relation == 'improvement' else 'pending' if approval['state'] == 'approved' else 'waiting_approval'
            item = {**spec, 'origin_id': origin, 'parent_id': parent['id'] if parent else None,
                    'instruction_id': instruction_id,
                    'relation': relation, 'approval': approval, 'state': state,
                    'created_at': self.clock(), 'updated_at': self.clock(), 'attempt': None,
                    'responsible': spec['product'], 'executor': None, 'reviewer': None,
                    'reason': '', 'next_action': 'check admission', 'attempt_count': 0}
            self.db.execute('INSERT INTO operation_tickets VALUES (?,?,?,?)',
                            (ident, registration_key, signature, json.dumps(item)))
            self._check_graph()
            self._save(item, 'registered', {'registration_key': registration_key})
            if instruction_id and not parent:
                source = json.loads(instruction[0])
                source.update(state='ticketed', tickets=list(dict.fromkeys([*source.get('tickets',[]), ident])))
                self.db.execute('UPDATE operation_instructions SET body=? WHERE id=?', (json.dumps(source), instruction_id))
        self.notify()
        return item

    def notify(self):
        from .wake import notify
        notify(self.path)

    def set_dependencies(self, ident, dependencies):
        strings(dependencies, 'dependencies', False)
        with self.transaction():
            item = self.get(ident)
            if item['attempt'] is not None or item['state'] in FINAL:
                raise ValueError('running or closed dependency graph is immutable')
            if any(x not in {t['id'] for t in self.list()} for x in dependencies):
                raise ValueError('unknown dependency')
            item['dependencies'] = dependencies
            self._save(item, 'dependencies_changed')
            self._check_graph()
        self.notify()

    def _check_graph(self):
        index = {x['id']: set(x.get('dependencies', [])) for x in self.list()}
        for child in self.list():
            if child.get('parent_id') and child.get('relation') in ('required', 'defect'):
                index[child['parent_id']].add(child['id'])
        def visit(current, seen):
            if current in seen:
                raise ValueError('dependency cycle including parent/child wait')
            for dep in index[current]:
                visit(dep, seen | {current})
        for ident in index:
            visit(ident, set())

    def incomplete_children(self, ident):
        return [x['id'] for x in self.list() if x.get('parent_id') == ident
                and x['relation'] in ('required', 'defect') and x['state'] != 'completed']

    def gate(self, ident):
        item = self.get(ident)
        if item['state'] == 'backlog':
            return 'optional improvement requires explicit approval'
        try:
            if item['approval'].get('state') != 'approved' or file_digest(item['approval']['reference']) != item['approval']['digest']:
                return 'approval missing or changed'
        except (ValueError, KeyError, OSError):
            return 'approval evidence unavailable'
        if any(self.get(x)['state'] != 'completed' for x in item.get('dependencies', [])):
            return 'dependency completion required'
        return None

    def ready(self):
        now = self.clock()
        boost = {'review': 20, 'integration': 15}
        return sorted((x for x in self.list() if x['state'] == 'pending' and self.gate(x['id']) is None),
                      key=lambda x: (-(x.get('priority', 0) + boost.get(x['kind'], 0)
                                        + (now - x['created_at']) / 300), x['created_at'], x['id']))

    def attempt(self, ident):
        row = self.db.execute('SELECT body FROM operation_attempts WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise ValueError('unknown attempt')
        return json.loads(row[0])

    def _save_attempt(self, attempt):
        self.db.execute('INSERT OR REPLACE INTO operation_attempts VALUES (?,?,?)',
                        (attempt['id'], attempt['ticket'], json.dumps(attempt)))

    def reserve(self, ident, executor, policy, observation):
        from .resources import ResourceLedger
        ledger = ResourceLedger(self.db)
        with self.transaction():
            item = self.get(ident)
            if item['state'] != 'pending' or self.gate(ident):
                raise ValueError('ticket is not eligible')
            profile = executor['provider'] + '/' + executor['account']
            planned = (executor.get('new') is True and executor.get('pid') is None
                       and executor.get('terminal') is None)
            if (profile not in item['profiles'] or not executor.get('owner')
                    or not planned and type(executor.get('pid')) is not int):
                raise ValueError('executor identity/profile mismatch')
            budget = self._budget(item['origin_id'])
            estimate = item.get('estimated_tokens', 4096)
            if (budget['attempts'] >= budget['limits']['attempts'] or item['attempt_count'] >= 3
                    or budget['tokens_reserved'] + budget['tokens_spent'] + estimate > budget['limits']['tokens']):
                raise ValueError('origin execution budget exhausted; escalate to operations')
            attempt = dict(id=uuid.uuid4().hex, ticket=ident, ordinal=item['attempt_count'] + 1,
                           executor=executor, phase='reserved', created_at=self.clock(),
                           delivery_id=uuid.uuid4().hex, estimated_tokens=estimate)
            self._continue_own_writes(item, attempt, executor, observation)
            from .admission import reserve_in_transaction
            reserve_in_transaction(self.db, attempt['id'], ident, profile,
                                   item['resources'], policy, observation,
                                   session=None if planned else {k: executor[k] for k in ('terminal', 'pid', 'owner')})
            budget['attempts'] += 1
            budget['tokens_reserved'] += estimate
            self._save_budget(item['origin_id'], budget)
            self._save_attempt(attempt)
            item.update(state='dispatching', attempt=attempt['id'], executor=executor,
                        attempt_count=attempt['ordinal'], reason='', next_action='deliver persisted attempt')
            self._save(item, 'execution_reserved', {'attempt': attempt['id']})
        return attempt

    def _continue_own_writes(self, ticket, attempt, executor, observation):
        """Transfer a parent's lease only to the very same observed idle actor."""
        from .resources import ResourceLedger
        session = {k: executor.get(k) for k in ('terminal', 'pid', 'owner')}
        observed = next((x for x in observation.get('terminals', [])
                         if x.get('terminal') == session['terminal']), None)
        for old in ResourceLedger._rows(self.db):
            if old['ticket'] != ticket['id'] or old['execution_reserved'] or not old['writes_held']:
                continue
            if (old.get('session') != session or observed is None
                    or observed.get('pid') != session['pid'] or observed.get('owner') != session['owner']
                    or observed.get('status') not in ('idle', 'done')
                    or old['profile'] != executor['provider'] + '/' + executor['account']):
                raise ValueError('parent retained write lease; original idle actor or explicit stopped-writer release required')
            old.update(writes_held=False, write_transfer_to=attempt['id'])
            ResourceLedger._write(self.db, old)

    def delivery(self, ident, accepted, detail):
        with self.transaction():
            attempt = self.attempt(ident)
            item = self.get(attempt['ticket'])
            if item['attempt'] != ident:
                raise ValueError('stale or already delivered attempt')
            if attempt['phase'] == 'received':
                # The actor's durable receipt may beat the transport reply.
                return item
            if attempt['phase'] != 'reserved':
                raise ValueError('stale or already delivered attempt')
            attempt.update(phase='accepted' if accepted else 'uncertain', delivery=detail,
                           delivered_at=self.clock())
            self._save_attempt(attempt)
            item.update(state='received' if accepted else 'uncertain',
                        reason='' if accepted else 'delivery outcome unknown; inspect original receiver',
                        next_action='wait actor receipt' if accepted else 'reconcile original attempt; never resend')
            self._save(item, 'delivery_accepted' if accepted else 'delivery_uncertain', {'attempt': ident})
        self.notify()

    def receipt(self, ident, attempt_id, owner, terminal, native):
        with self.transaction():
            item = self.get(ident)
            attempt = self.attempt(attempt_id)
            executor = attempt['executor']
            if (item['attempt'] != attempt_id or executor['owner'] != owner
                    or executor['terminal'] != terminal or native.get('terminal') != terminal
                    or native.get('pid') != executor['pid'] or native.get('owner') != owner
                    or item['state'] not in ('dispatching', 'received', 'uncertain', 'running')):
                raise ValueError('receipt actor or attempt mismatch')
            if attempt['phase'] == 'received':
                return item
            attempt.update(phase='received', actor_receipt={**native, 'at': self.clock()})
            self._save_attempt(attempt)
            item.update(state='running', reason='', next_action='submit result')
            self._save(item, 'actor_received', {'attempt': attempt_id})
        self.notify()
        return item

    def submit(self, ident, attempt_id, owner, result):
        # A submission is evidence to validate, never a successful completion.
        with self.transaction():
            item = self.get(ident)
            attempt = self.attempt(attempt_id)
            if (item['attempt'] != attempt_id or attempt['executor']['owner'] != owner
                    or item['state'] not in ('running', 'uncertain', 'awaiting_verification')):
                raise ValueError('stale result or wrong execution owner')
            proof = dict(result)
            if not within(proof.get('artifact', ''),
                          [*item['scope']['paths'], str(self.path.resolve().parent / 'operation-results' / ident)]):
                raise ValueError('result artifact is outside approved data/output scope')
            proof['artifact_digest'] = file_digest(proof['artifact'])
            if 'submission' in attempt:
                if attempt['submission'] != proof:
                    raise ValueError('conflicting repeated submission')
                return item
            attempt['submission'] = proof
            self._save_attempt(attempt)
            item.update(state='awaiting_verification', next_action='validate task-specific evidence')
            self._save(item, 'result_submitted', {'attempt': attempt_id})
        self.notify()
        return item

    def approve(self, ident, approval_file):
        """Called by the explicit user-approved CLI path, never inherited from a child."""
        packet = json.loads(Path(approval_file).read_text())
        with self.transaction():
            item = self.get(ident)
            contract = self.db.execute('SELECT digest FROM operation_tickets WHERE id=?', (ident,)).fetchone()[0]
            if (item['attempt'] is not None or item['state'] not in ('waiting_approval', 'backlog')
                    or packet.get('ticket') != ident or packet.get('scope_digest') != digest(item['scope'])
                    or packet.get('contract_digest') != contract
                    or packet.get('approved_by') != '사용자'):
                raise ValueError('explicit approval does not match this ticket scope')
            item.update(approval={'state': 'approved', 'reference': str(Path(approval_file).resolve()),
                                   'digest': file_digest(str(Path(approval_file).resolve()))},
                        state='pending', reason='', next_action='check admission')
            self._save(item, 'user_approved', {'scope_digest': packet['scope_digest']})
        self.notify()
        return item

    def wait_children(self, ident, attempt_id, owner, stopped, release_writes=False):
        from .resources import ResourceLedger
        ledger = ResourceLedger(self.db)
        with self.transaction():
            item = self.get(ident)
            attempt = self.attempt(attempt_id)
            if (item['attempt'] != attempt_id or attempt['executor']['owner'] != owner
                    or item['state'] not in ('running', 'waiting_children_stop', 'awaiting_verification', 'awaiting_review')
                    or not self.incomplete_children(ident)):
                raise ValueError('parent attempt or required children mismatch')
            ledger.release_in_transaction(self.db, attempt_id, execution_stopped=stopped,
                                           release_writes=release_writes, writer_stopped=stopped)
            item.update(state='waiting_children', next_action='wait required children; resume as a new attempt')
            self._save(item, 'waiting_children', {'attempt': attempt_id, 'writes_released': release_writes})
        self.notify()

    def request_children_wait(self, ident, attempt_id, owner, release_writes=False):
        with self.transaction():
            item = self.get(ident)
            attempt = self.attempt(attempt_id)
            if (item['attempt'] != attempt_id or attempt['executor']['owner'] != owner
                    or item['state'] != 'running' or not self.incomplete_children(ident)):
                raise ValueError('parent attempt or required children mismatch')
            item.update(state='waiting_children_stop', release_writes_on_stop=release_writes,
                        next_action='finish native turn so host can release execution reservation')
            self._save(item, 'children_wait_requested')
        self.notify()
        return item

    def resume_parents(self):
        changed = False
        with self.transaction():
            for item in self.list():
                if item['state'] == 'waiting_children' and not self.incomplete_children(item['id']):
                    item.update(state='pending', attempt=None, next_action='resume with child results')
                    self._save(item, 'children_completed')
                    changed = True
        if changed:
            self.notify()

    def record_stop(self, ident, attempt_id, native):
        """The service calls this only after fresh, fenced native idle observation."""
        from .resources import ResourceLedger
        ledger = ResourceLedger(self.db)
        with self.transaction():
            item = self.get(ident)
            attempt = self.attempt(attempt_id)
            executor = attempt['executor']
            if (item['attempt'] != attempt_id or native.get('terminal') != executor['terminal']
                    or native.get('pid') != executor['pid'] or native.get('owner') != executor['owner']
                    or native.get('status') not in ('idle', 'done')
                    or not attempt.get('submission', {}).get('writer_stopped')):
                raise ValueError('writer stop requires fenced native idle and explicit evidence')
            attempt['stop_observation'] = {**native, 'at': self.clock()}
            self._save_attempt(attempt)
            ledger.release_in_transaction(self.db, attempt_id, execution_stopped=True,
                                           release_writes=True, writer_stopped=True)
            self._save(item, 'writer_stopped', {'attempt': attempt_id})
        self.notify()

    def finish(self, ident, attempt_id, verification):
        """Adopt a service verification of the exact submitted artifact, not a model boolean."""
        with self.transaction():
            item = self.get(ident)
            attempt = self.attempt(attempt_id)
            submission = attempt.get('submission') or {}
            if (item['attempt'] != attempt_id or item['state'] not in ('awaiting_verification', 'awaiting_review')
                    or not attempt.get('stop_observation') or self.gate(ident)
                    or self.incomplete_children(ident)):
                raise ValueError('completion gate is not satisfied')
            current = file_digest(submission['artifact'])
            if current != submission['artifact_digest'] or verification.get('artifact_digest') != current:
                raise ValueError('completion evidence changed')
            checks = verification.get('checks')
            if (not isinstance(checks, dict) or set(checks) != set(item['checks'])
                    or any(value is not True for value in checks.values())):
                raise ValueError('all approved verification checks must pass')
            review = verification.get('review')
            if item['kind'] in ('code', 'integration') or item.get('review_required'):
                if (not isinstance(review, dict) or review.get('verdict') != 'pass'
                        or review.get('artifact_digest') != current
                        or not review.get('owner') or review['owner'] == attempt['executor']['owner']
                        or review.get('terminal') == attempt['executor']['terminal']):
                    raise ValueError('independent review of current artifact is required')
                file_digest(review['evidence'])
                item['reviewer'] = review
            usage = submission.get('usage_tokens')
            budget = self._budget(item['origin_id'])
            if type(usage) is int and usage >= 0:
                budget['tokens_reserved'] -= attempt['estimated_tokens']
                budget['tokens_spent'] += usage
                self._save_budget(item['origin_id'], budget)
            # Missing usage remains reserved, not fabricated as zero cost.
            attempt.update(phase='completed', verification=verification, completed_at=self.clock())
            self._save_attempt(attempt)
            item.update(state='completed', reason='', next_action='release dependencies',
                        result={'artifact': submission['artifact'], 'digest': current})
            self._save(item, 'completed', {'attempt': attempt_id})
        self.notify()
        self.resume_parents()
        return item

    def block(self, ident, reason):
        with self.transaction():
            item = self.get(ident)
            if item['state'] not in FINAL and item.get('reason') != reason:
                item.update(reason=reason, next_action='operations reconciliation required')
                self._save(item, 'gate', {'reason': reason})
        return item
