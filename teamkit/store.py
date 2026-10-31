"""Private local task ledger. Transactions serialize ownership, not model work."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import uuid

ACTIVE = {'claimed', 'launching', 'submitted', 'running', 'uncertain', 'stopping'}


def sha(value):
    return isinstance(value, str) and re.fullmatch(r'[a-fA-F0-9]{40}|[a-fA-F0-9]{64}', value)


def write_root(root):
    result = subprocess.run(['git','-C',str(root),'rev-parse','--show-toplevel'],
                            text=True, capture_output=True, timeout=5)
    if result.returncode:
        if not Path(root).exists():
            return Path(root).resolve()
        raise ValueError('cannot establish implementation worktree identity')
    top = result.stdout.removesuffix('\n')
    if not top or not Path(top).is_absolute():
        raise ValueError('invalid implementation worktree identity')
    return Path(top).resolve()


def writes_overlap(first, second):
    left, right = write_root(first), write_root(second)
    if left == right or left.is_relative_to(right) or right.is_relative_to(left):
        return True
    try:
        return left.samefile(right)
    except FileNotFoundError:
        return False


def validate_failure(failure, verification):
    if failure is None:
        return
    from .routing import FAILURES
    if not isinstance(failure, dict) or set(failure) != {'cause','kind'} or failure['kind'] not in FAILURES or not isinstance(failure['cause'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', failure['cause']) or not isinstance(verification, list) or not verification or not all(isinstance(v,str) and v.strip() for v in verification):
        raise ValueError('failure requires cause, kind and verification evidence')


class Store:
    def __init__(self, path, timeout=10):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Create privately before SQLite opens the file; never chmod existing parent dirs.
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        os.chmod(self.path, 0o600)
        self.db = sqlite3.connect(self.path, timeout=timeout, isolation_level=None)
        self.db.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, task TEXT, event TEXT, at REAL, body TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS quotas (profile TEXT PRIMARY KEY, body TEXT NOT NULL)')

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

    def get(self, task):
        row = self.db.execute('SELECT body FROM tasks WHERE id=?', (task,)).fetchone()
        if not row:
            raise ValueError('unknown task')
        return json.loads(row[0])

    def list(self):
        return [json.loads(row[0]) for row in self.db.execute('SELECT body FROM tasks ORDER BY id')]

    def _save(self, value, event, detail=None):
        self.db.execute('INSERT OR REPLACE INTO tasks VALUES (?,?)', (value['id'], json.dumps(value)))
        self.db.execute('INSERT INTO events(task,event,at,body) VALUES (?,?,?,?)',
                        (value['id'], event, time.time(), json.dumps(detail or {})))
        return value

    def create(self, task, team, role, root, base, goal, basis_kind="git", classification=None):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', task):
            raise ValueError('invalid task ID')
        if role not in ('leader', 'implementer', 'reviewer') or not sha(base) or not Path(root).is_absolute() or not goal.strip():
            raise ValueError('invalid task contract')
        value = dict(id=task, team=team, role=role, root=root, base=base, goal=goal, basis_kind=basis_kind, state='queued', owner=None)
        if classification is not None:
            value['classification'] = classification
        with self.transaction():
            old = self.db.execute('SELECT body FROM tasks WHERE id=?', (task,)).fetchone()
            if old:
                existing = json.loads(old[0])
                if any(existing[k] != value[k] for k in ('team','role','root','base','goal','basis_kind')):
                    raise ValueError('task ID already has another contract')
                if existing.get('classification', {'kind':'general','risks':[]}) != value.get('classification', {'kind':'general','risks':[]}):
                    raise ValueError('task ID already has another classification')
                return existing
            return self._save(value, 'created')

    def claim(self, task, owner, provider, account, model, effort, expected_task=None):
        if not owner or provider not in ('codex','claude') or account not in ('team','personal') or not model:
            raise ValueError('invalid execution identity')
        with self.transaction():
            value = self.get(task)
            if expected_task is not None and value != expected_task:
                raise ValueError('task changed during preflight; plan again')
            if value['state'] not in ('queued', 'handed_off'):
                raise ValueError('task already claimed or terminal; inspect before retry')
            active = [t for t in self.list() if t['state'] in ACTIVE]
            if len(active) >= 3:
                raise ValueError('global model limit reached')
            if value['role'] == 'implementer' and any(t['role'] == 'implementer' and (t['team'] == value['team'] or writes_overlap(t['root'], value['root'])) for t in active):
                raise ValueError('team already has an implementation owner')
            value.pop('boundary', None)
            value.update(state='claimed', owner=owner, provider=provider, account=account, model=model, effort=effort, runtime={})
            return self._save(value, 'claimed')

    def update(self, task, owner, state, detail):
        transitions = {'claimed': {'launching','uncertain'}, 'launching': {'submitted','uncertain'},
                       'submitted': {'running','uncertain'}, 'running': {'uncertain'}, 'uncertain': set()}
        with self.transaction():
            value = self.get(task)
            if value['owner'] != owner or state not in transitions.get(value['state'], set()):
                raise ValueError('owner or state transition mismatch')
            value['state'] = state
            value.setdefault('runtime', {}).update(detail)
            return self._save(value, state, detail)

    def delivery_failed(self, task, owner):
        with self.transaction():
            value = self.get(task)
            if value.get('owner') != owner:
                raise ValueError('delivery owner mismatch')
            if value['state'] in ('launching','submitted'):
                value['state'] = 'uncertain'
            return self._save(value, 'delivery_failed', {'reason':'inspect before further input'})

    def receipt(self, task, owner):
        with self.transaction():
            value = self.get(task)
            if value.get('owner') != owner or value['state'] not in ('submitted','uncertain'):
                raise ValueError('receipt owner/state mismatch')
            value['state'] = 'running'
            value.setdefault('runtime', {})['identity_checked'] = True
            return self._save(value, 'receipted')

    def note(self, task, owner, detail):
        with self.transaction():
            value = self.get(task)
            if value.get('owner') != owner or value['state'] not in ACTIVE:
                raise ValueError('runtime note owner/state mismatch')
            value.setdefault('runtime', {}).update(detail)
            return self._save(value, 'runtime', detail)

    def park(self, task, owner):
        with self.transaction():
            value = self.get(task)
            if value.get('owner') != owner or value['role'] != 'leader' or value['state'] != 'running':
                raise ValueError('only a running leader can park')
            value['state'] = 'ready'
            return self._save(value, 'ready')

    def wake(self, task, owner):
        with self.transaction():
            value = self.get(task)
            if value.get('owner') != owner or value['role'] != 'leader' or value['state'] != 'ready':
                raise ValueError('leader is not ready')
            if sum(t['state'] in ACTIVE for t in self.list()) >= 3:
                raise ValueError('global model limit reached')
            value['state'] = 'running'
            return self._save(value, 'woken')

    def handoff(self, task, owner, checkpoint, stopped, expected_task=None):
        required = {'head','dirty','verification','next'}
        if not stopped or not required.issubset(checkpoint) or not sha(checkpoint.get('head')) or not checkpoint['next']:
            raise ValueError('handoff requires stopped writer and complete checkpoint')
        with self.transaction():
            value = self.get(task)
            if expected_task is not None and value != expected_task:
                raise ValueError('task changed before handoff; inspect')
            if value.get('owner') != owner or value['state'] not in ACTIVE | {'ready'}:
                raise ValueError('handoff owner/state mismatch')
            failure = checkpoint.get('failure')
            if failure is not None:
                validate_failure(failure, checkpoint['verification'])
                value.setdefault('failures', []).append({**failure, 'effort':value['effort']})
            value.update(state='handed_off', owner=None, checkpoint=checkpoint)
            return self._save(value, 'handed_off', checkpoint)

    def boundary(self, task, owner, evidence):
        required = {'head','dirty','worktree_fingerprint','verification','next','owner','rotate_if_unknown'}
        if not isinstance(evidence, dict) or not required.issubset(evidence) or set(evidence)-required-{'failure'} or evidence['owner'] != owner or not sha(evidence['head']) or not sha(evidence['worktree_fingerprint']) or type(evidence['dirty']) is not bool or type(evidence['rotate_if_unknown']) is not bool or not isinstance(evidence['verification'], list) or not evidence['verification'] or not all(isinstance(v,str) and v.strip() for v in evidence['verification']) or not isinstance(evidence['next'], str) or not evidence['next'].strip():
            raise ValueError('boundary requires actual Git state, verification and next action')
        validate_failure(evidence.get('failure'),evidence['verification'])
        with self.transaction():
            value = self.get(task)
            if value.get('owner') != owner or value['state'] != 'running' or value['role'] == 'leader' or not value.get('runtime', {}).get('identity_checked') or value.get('runtime', {}).get('supervision_enrolled') is not True:
                raise ValueError('boundary requires a running receipted worker owner')
            value['boundary'] = evidence
            return self._save(value, 'boundary', evidence)

    def supervise_transition(self, task, owner, expected, stopping=False, detail=None):
        with self.transaction():
            value = self.get(task)
            if value != expected or value.get('owner') != owner or value['state'] != 'running' or value['role'] == 'leader' or 'boundary' not in value:
                raise ValueError('supervision task changed; inspect')
            if stopping:
                value['state'] = 'stopping'
                value['runtime']['supervision_pending'] = detail
            else:
                value['runtime']['supervision_delivery'] = {'token':uuid.uuid4().hex,'status':'pending','next':value['boundary']['next']}
                value.pop('boundary')
            return self._save(value, 'stopping' if stopping else 'continuation_pending')

    def supervision_accepted(self, task, owner, token):
        with self.transaction():
            value = self.get(task)
            pending = value.get('runtime', {}).get('supervision_delivery', {})
            if value.get('owner') != owner or value['state'] not in ('running','awaiting_review') or pending.get('token') != token or pending.get('status') != 'pending':
                raise ValueError('continuation intent changed; inspect')
            pending['status'] = 'submitted'
            return self._save(value,'continuation_submitted',{'token':token})

    def authorize(self, task, decision, approved):
        fields = {'failure_count','approval_reference','provider','model','effort'}
        if not approved or not isinstance(decision, dict) or set(decision) != fields or type(decision['failure_count']) is not int or not isinstance(decision['approval_reference'], str) or not decision['approval_reference'].strip() or decision['provider'] not in ('codex','claude') or not isinstance(decision['model'], str) or not decision['model'].strip() or decision['effort'] not in ('low','medium','high','xhigh','max'):
            raise ValueError('explicit user approval and complete model decision required')
        with self.transaction():
            value = self.get(task)
            failures = value.get('failures', [])
            meaningful = [f for f in failures if f['kind'] in ('reasoning','insufficient_evidence')]
            if value['state'] != 'handed_off' or value.get('owner') is not None or not any(f['effort'] in ('high','xhigh','max') for f in meaningful) or decision['failure_count'] != len(failures):
                raise ValueError('decision does not match the stopped high-effort failure')
            value['user_decision'] = dict(decision)
            return self._save(value, 'user_authorized', decision)

    def complete(self, task, owner, head, evidence, reviewed_head=None):
        if not sha(head) or not isinstance(evidence, list) or not evidence or not all(isinstance(x,str) and x.strip() for x in evidence):
            raise ValueError('completion requires full SHA and verification evidence')
        with self.transaction():
            value = self.get(task)
            if value.get('owner') != owner or value['state'] != 'running' or not value.get('runtime', {}).get('identity_checked'):
                raise ValueError('completion requires a running receipted owner')
            if value['role'] == 'reviewer' and (reviewed_head != value['base'] or head != reviewed_head):
                raise ValueError('reviewed SHA must match pinned base and current HEAD')
            value.update(state='awaiting_review', head=head, verification=evidence)
            return self._save(value, 'awaiting_review')

    def events(self, task):
        return [dict(seq=r[0], event=r[1], at=r[2], detail=json.loads(r[3])) for r in
                self.db.execute('SELECT seq,event,at,body FROM events WHERE task=? ORDER BY seq', (task,))]

    def quota(self, profile, value=None):
        if value is not None:
            self.db.execute('INSERT OR REPLACE INTO quotas VALUES (?,?)', (profile, json.dumps(value)))
        row = self.db.execute('SELECT body FROM quotas WHERE profile=?', (profile,)).fetchone()
        return json.loads(row[0]) if row else None
