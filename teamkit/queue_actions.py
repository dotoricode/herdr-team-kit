"""Durable UI requests consumed by the existing sequential dispatch supervisor.

A wake is a hint, never a delivery receipt. Claimed actions are never reclaimed:
inspect their original attempt after a consumer crash instead of retrying a send.
"""
import json
import re
import sqlite3
import time

from .config import load
from .operations import NativeRuntime, enabled, policy, tick
from .tickets import TicketStore, digest


def schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS queue_actions
                  (id TEXT PRIMARY KEY, body TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS queue_action_holds
                  (ticket TEXT PRIMARY KEY, action TEXT NOT NULL, candidate TEXT NOT NULL)''')


def executor_identity(executor):
    if executor.get('new') is True:
        from .workers import PLAN_FIELDS
        return digest({key: executor.get(key) for key in ('new', *PLAN_FIELDS)})
    # Display aliases never participate in identity.
    return digest(executor)


def selection_hold(db, ticket):
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='queue_action_holds'").fetchone():
        row = db.execute('SELECT action,candidate FROM queue_action_holds WHERE ticket=?', (ticket,)).fetchone()
        if row:
            return dict(action=row[0], candidate=row[1])
    # Earlier durable send records also express a manual selection. Upgrading
    # must not turn an existing blocked/queued action back into automatic work.
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='queue_actions'").fetchone():
        for row in db.execute('SELECT body FROM queue_actions ORDER BY rowid DESC'):
            action = json.loads(row[0])
            if action['kind'] == 'send' and action['request']['ticket'] == ticket:
                return dict(action=action['id'], candidate=action['request']['candidate_id'])
    return None


def reserve_selection(db, ticket, executor, action_id=None):
    """Called inside the official reserve transaction, including automatic ticks."""
    hold = selection_hold(db, ticket)
    if hold is None:
        if action_id is not None:
            raise ValueError('manual selection hold missing; no input sent')
        return
    if (hold['action'] != action_id or hold['candidate'] != executor_identity(executor)
            or _read(db, hold['action'])['status'] != 'running'):
        raise ValueError('manual executor selection held; explicit fresh selection required, no fallback')


def _previous_selection_reason(db, ticket):
    hold = selection_hold(db, ticket)
    if hold and _read(db, hold['action'])['status'] in ('queued', 'running', 'uncertain'):
        return 'previous selected action is queued, running or uncertain; inspect original outcome before reselection'
    return None


def _send_reason(queue, ticket, config):
    if not enabled(config):
        return 'new work is disabled by current operations policy'
    if ticket['state'] != 'pending':
        return 'ticket already assigned, uncertain, completed or not pending; never resend'
    if ticket['role'] == 'ui':
        return 'UI work requires the existing UI lab'
    return queue.gate(ticket['id'])


def candidates(config_path, state, ident, runtime=None):
    config = load(config_path)
    runtime = runtime or NativeRuntime(config, state)
    queue = TicketStore(state, read_only=True)
    try:
        ticket = queue.get(ident)
        reason = _send_reason(queue, ticket, config) or _previous_selection_reason(queue.db, ident)
        rows = []
        if reason:
            return dict(ticket=ident, expected_ticket=digest(ticket), candidates=[], send_allowed=False, reason=reason)
        try:
            executors = runtime.executors(ticket)
        except Exception as error:
            return dict(ticket=ident, expected_ticket=digest(ticket), candidates=[], send_allowed=False, reason=str(error))
        for executor in executors:
            problem = None
            try:
                runtime.admit(ticket, executor)
                # Run the official reservation gates against a private SQLite copy.
                # This preview cannot hold resources, spend budget or alter a ticket.
                preview = object.__new__(TicketStore)
                preview.db = sqlite3.connect(':memory:', isolation_level=None)
                preview.path, preview.clock = queue.path, time.time
                try:
                    queue.db.backup(preview.db)
                    # Candidate previews may offer a fresh explicit reselection;
                    # only the real reserve transaction enforces a current hold.
                    preview.db.execute('DROP TABLE IF EXISTS queue_action_holds')
                    preview.db.execute('DROP TABLE IF EXISTS queue_actions')
                    from .admission import configure
                    configure(preview.db, policy(config))
                    preview.reserve(ident, executor, policy(config), runtime.observation(), expected_revision=digest(ticket))
                finally:
                    preview.close()
            except Exception as error:
                problem = str(error)
            from .staff import display_name
            rows.append(dict(id=executor_identity(executor),
                alias=display_name(executor['provider'], executor['account'], product=ticket['product'],
                    role=ticket['role'], owner=None if executor.get('new') else executor.get('owner'),
                    aliases=config.get('operations', {}).get('staff_aliases')),
                account=executor['account'], provider=executor['provider'], role=ticket['role'],
                eligible=problem is None, reason=problem or '', planned=executor.get('new') is True))
        return dict(ticket=ident, expected_ticket=digest(ticket), candidates=rows,
                    send_allowed=any(row['eligible'] for row in rows),
                    reason='' if any(row['eligible'] for row in rows) else 'no eligible registered executor or approved worker plan')
    finally:
        queue.close()


def _read(db, ident):
    row = db.execute('SELECT body FROM queue_actions WHERE id=?', (ident,)).fetchone()
    if row is None:
        raise ValueError('unknown queue action')
    return json.loads(row[0])


def status(state, ident):
    queue = TicketStore(state, read_only=True)
    try:
        if not queue.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='queue_actions'").fetchone():
            raise ValueError('unknown queue action')
        return _read(queue.db, ident)
    finally:
        queue.close()


def _save(db, action):
    db.execute('INSERT OR REPLACE INTO queue_actions VALUES (?,?)', (action['id'], json.dumps(action)))


def enqueue(config_path, state, kind, client_request_id, *, ticket=None, candidate_id=None, expected_ticket=None):
    if not isinstance(client_request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', client_request_id):
        raise ValueError('bounded stable client-request-id required')
    if kind not in ('send', 'sync'):
        raise ValueError('unknown queue action')
    if kind == 'send' and (not ticket or any(not isinstance(x, str) or not re.fullmatch('[0-9a-f]{64}', x)
                                           for x in (candidate_id, expected_ticket))):
        raise ValueError('ticket, exact candidate-id and expected-ticket required')
    request = dict(kind=kind, ticket=ticket, candidate_id=candidate_id, expected_ticket=expected_ticket)
    queue = TicketStore(state)
    try:
        schema(queue.db)
        with queue.transaction():
            row = queue.db.execute('SELECT body FROM queue_actions WHERE id=?', (client_request_id,)).fetchone()
            if row:
                result = json.loads(row[0])
                if result['request'] != request:
                    raise ValueError('client-request-id already binds another immutable action')
                return result
            result = dict(id=client_request_id, kind=kind, request=request, status='queued',
                          reason='durable request recorded; waiting for dispatch consumer', created_at=time.time(), result=None)
            if kind == 'send':
                current = queue.get(ticket)
                previous = _previous_selection_reason(queue.db, ticket)
                problem = (_send_reason(queue, current, load(config_path)) or previous)
                if digest(current) != expected_ticket:
                    problem = 'ticket changed before manual selection; refresh candidates'
                if current.get('attempt'):
                    problem = 'existing execution attempt requires original outcome inspection; never resend'
                # Even a stale preview expresses a manual selection. Preserve
                # that intent on pending work, but never replace a live action.
                if current['state']=='pending' and not current.get('attempt') and not previous:
                    queue.db.execute('INSERT OR REPLACE INTO queue_action_holds VALUES (?,?,?)',
                                     (ticket, client_request_id, candidate_id))
                if problem:
                    result.update(status='blocked', reason=problem, finished_at=time.time())
            _save(queue.db, result)
        from .wake import notify
        # Do not hold SQLite while waking the existing consumer.
        result['wake_accepted'] = notify(state)
        return result
    finally:
        queue.close()


def consume(config_path, state, runtime=None):
    """CAS-claim queued requests; a second consumer never repeats a claimed action."""
    queue = TicketStore(state)
    try:
        schema(queue.db)
        pending = [row[0] for row in queue.db.execute("SELECT id FROM queue_actions ORDER BY rowid")]
        results = []
        for ident in pending:
            with queue.transaction():
                action = _read(queue.db, ident)
                if action['status'] != 'queued':
                    continue
                action.update(status='running', started_at=time.time(), reason='dispatch consumer processing original action')
                _save(queue.db, action)
            try:
                before = {x['id']: x for x in queue.list()}
                request = action['request']
                if action['kind'] == 'send':
                    ticket = queue.get(request['ticket'])
                    problem = _send_reason(queue, ticket, load(config_path))
                    if digest(ticket) != request['expected_ticket']:
                        problem = 'ticket changed since candidate preview; refresh candidates'
                    if problem:
                        raise ValueError(problem)
                    dispatch = tick(config_path, state, runtime=runtime, selection={**request, 'action_id':action['id']})
                else:
                    dispatch = tick(config_path, state, runtime=runtime)
                after = {x['id']: x for x in queue.list()}
                actual = dict(dispatch=dispatch, tickets={key: dict(before=before.get(key, {}).get('state'), after=value['state'])
                                                         for key, value in after.items()},
                              observed_count=len(after), completed=[key for key, value in after.items()
                                if value['state']=='completed' and before.get(key, {}).get('state')!='completed'])
                action.update(status='completed', reason='synchronization processed' if action['kind']=='sync' else 'selected dispatch processed', result=actual)
                if dispatch.get('enabled') is not True:
                    action.update(status='blocked', reason='operations disabled; no dispatch or reconciliation processed')
                if action['kind'] == 'send':
                    current = after[request['ticket']]
                    if current['state'] == 'uncertain':
                        action.update(status='uncertain', reason=current.get('reason') or 'delivery unknown; never resend')
                    elif not dispatch.get('started'):
                        action.update(status='blocked', reason=current.get('reason') or 'selected executor no longer available; refresh candidates')
            except Exception as error:
                action.update(status='blocked', reason=str(error))
            except BaseException:
                # A stopped consumer is not proof that native delivery failed.
                # Preserve the action and original attempt; no claim is retried.
                with queue.transaction():
                    action.update(status='uncertain', reason='consumer stopped; inspect original attempt, never resend',
                                  finished_at=time.time())
                    _save(queue.db, action)
                raise
            with queue.transaction():
                action['finished_at'] = time.time()
                _save(queue.db, action)
            results.append(action)
        return results
    finally:
        queue.close()
