"""Opt-in admission shared by legacy tasks, coordinator turns and ticket work.

HC observations describe the managed boundary only. Old uncertain reservations
remain authoritative until actual stopped execution is independently observed.
"""
from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import sqlite3
import uuid

from .resources import ResourceLedger, ResourceUnavailable, _conflict, normalize_resources, observe_native, validate_policy

ACTIVE = {'claimed', 'launching', 'submitted', 'running', 'uncertain'}
STOPPABLE = {'ready', 'awaiting_review', 'handed_off'}


@contextmanager
def _connection(state_or_connection):
    owned = not isinstance(state_or_connection, sqlite3.Connection)
    connection = (sqlite3.connect(state_or_connection, isolation_level=None, timeout=10)
                  if owned else state_or_connection)
    try:
        yield connection
    finally:
        if owned:
            connection.close()


def _has_table(connection, name):
    return bool(connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _tasks(connection):
    if not _has_table(connection, 'tasks'):
        return []
    return [json.loads(row[0]) for row in connection.execute('SELECT body FROM tasks')]


def policy_for(config=None, connection=None):
    operations = config.get('operations') if isinstance(config, dict) else None
    if operations is not None:
        if not isinstance(operations, dict) or type(operations.get('enabled')) is not bool:
            raise ValueError('operations requires explicit boolean enabled')
        return operations['enabled'], validate_policy(operations.get('resource_policy'))
    if connection is not None and _has_table(connection, 'operations_admission_policy'):
        row = connection.execute('SELECT body FROM operations_admission_policy WHERE id=1').fetchone()
        if row:
            value = json.loads(row[0])
            if not isinstance(value, dict) or type(value.get('enabled')) is not bool:
                raise ValueError('stored operations admission policy is invalid')
            return value['enabled'], validate_policy(value.get('policy'))
    return False, validate_policy()


def configure(state_or_connection, policy=None, enabled=True):
    if type(enabled) is not bool:
        raise ValueError('enabled must be boolean')
    policy = validate_policy(policy)
    with _connection(state_or_connection) as connection:
        ResourceLedger(connection)
        connection.execute('CREATE TABLE IF NOT EXISTS operations_admission_policy (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
        value = {'enabled': enabled, 'policy': policy}
        connection.execute('INSERT OR REPLACE INTO operations_admission_policy VALUES (1,?)', (json.dumps(value),))
        return value


def _reservations(connection):
    return ResourceLedger._rows(connection) if _has_table(connection, 'resource_reservations') else []


def task_resources(task):
    if 'resources' in task:
        return normalize_resources(task['resources'])
    mode = {'implementer': 'write', 'reviewer': 'read'}.get(task.get('role'))
    return normalize_resources([{'kind': 'path', 'name': task['root'], 'mode': mode}]) if mode else []


def observe(state_or_connection, native_observation=None):
    """Include pre-migration ACTIVE tasks without double-counting their HC turn."""
    observation = copy.deepcopy(observe_native() if native_observation is None else native_observation)
    if not isinstance(observation, dict) or not isinstance(observation.get('terminals'), list):
        raise ResourceUnavailable('observation_invalid', 'HC native observation is unavailable')
    with _connection(state_or_connection) as connection:
        recorded = {r['attempt'] for r in _reservations(connection) if r['execution_reserved']}
        for task in _tasks(connection):
            runtime = task.get('runtime') or {}
            if task['state'] not in ACTIVE or runtime.get('resource_attempt') in recorded:
                continue
            terminal = runtime.get('terminal')
            pid = runtime.get('native_pid')
            if terminal and type(pid) is int:
                observation['terminals'] = [r for r in observation['terminals']
                                           if not (r.get('terminal') == terminal and r.get('pid') == pid)]
            observation['terminals'] = [r for r in observation['terminals'] if r.get('legacy_task') != task['id']]
            observation['terminals'].append(dict(terminal='legacy-task:' + task['id'],
                                                provider=task.get('provider'), account=task.get('account'),
                                                status='unknown', legacy_task=task['id']))
    return observation


def _legacy_resource_conflicts(connection, ticket, resources):
    reserved = {r['attempt'] for r in _reservations(connection) if r['writes_held']}
    for task in _tasks(connection):
        if (task['id'] == ticket or task['state'] not in ACTIVE
                or (task.get('runtime') or {}).get('resource_attempt') in reserved):
            continue
        if any(_conflict(left, right) for left in resources for right in task_resources(task)):
            raise ResourceUnavailable('resource_conflict', 'pre-migration work retains a conflicting resource')


def reserve_in_transaction(connection, attempt, ticket, profile, resources, policy,
                           native_observation=None, session=None):
    ledger = ResourceLedger(connection)
    if _has_table(connection, 'operations_admission_policy'):
        enabled, stored_policy = policy_for(None, connection)
        if not enabled:
            raise ResourceUnavailable('operations_disabled', 'operations admission is disabled')
        if policy is not None and validate_policy(policy) != stored_policy:
            raise ResourceUnavailable('policy_changed', 'execution policy differs from the current shared policy')
        policy = stored_policy
    resources = normalize_resources(resources)
    reconcile_stopped(connection)
    if any(r['ticket'] == ticket and r['attempt'] != attempt and r['execution_reserved']
           for r in _reservations(connection)):
        raise ResourceUnavailable('previous_execution_unconfirmed', 'a previous execution of this ticket has not stopped')
    _legacy_resource_conflicts(connection, ticket, resources)
    return ledger.reserve_in_transaction(connection, attempt, ticket, profile, resources,
                                          policy, observe(connection, native_observation), session=session)


def reserve_task(store, task, owner, provider, account, config=None, native_observation=None):
    enabled, policy = policy_for(config, store.db)
    if not enabled:
        return None
    # Persist the opt-in policy so later park/reconcile and ticket admissions use
    # the same contract even when their caller has no configuration argument.
    configure(store.db, policy)
    attempt = 'legacy:' + task['id'] + ':' + uuid.uuid4().hex
    session = None
    runtime = task.get('runtime') or {}
    if (task.get('state') == 'ready' and task.get('owner') == owner
            and runtime.get('terminal') and type(runtime.get('native_pid')) is int):
        session = dict(terminal=runtime['terminal'], pid=runtime['native_pid'], owner=owner)
    reserve_in_transaction(store.db, attempt, task['id'], provider + '/' + account,
                           task_resources(task), policy, native_observation, session)
    return attempt


def bind_task(store, task):
    runtime = task.get('runtime') or {}
    attempt = runtime.get('resource_attempt')
    if not attempt or not runtime.get('terminal') or type(runtime.get('native_pid')) is not int:
        return
    ResourceLedger(store.db).bind(attempt, dict(terminal=runtime['terminal'],
                                             pid=runtime['native_pid'], owner=task['owner']))


def native_get(terminal):
    from .core_runtime import _call
    return _call('get', terminal)


def _stopped(session, profile):
    if not session:
        return False
    try:
        native = native_get(session['terminal'])
        if (native.get('address') != session['terminal']
                or (native.get('labels') or {}).get('teamkit.owner') != session['owner']):
            return False
        foreground = native.get('foreground') or {}
        provider = profile.split('/')[0]
        names = ('codex',) if provider == 'codex' else ('claude', 'claude.exe')
        apps = ('codex',) if provider == 'codex' else ('claude', 'claude-code')
        if (native.get('state') == 'live' and foreground.get('pid') == session['pid']
                and foreground.get('name') in names and (native.get('status') or {}).get('app') in apps
                and (native.get('status') or {}).get('state') in ('idle', 'done')):
            return True
        # A changed foreground/expired heartbeat does not prove the old PID died.
        if native.get('state') == 'finished':
            try:
                os.kill(session['pid'], 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
    except Exception:
        return False
    return False


def reconcile_stopped(state_or_connection):
    with _connection(state_or_connection) as connection:
        if not _has_table(connection, 'resource_reservations'):
            return []
        ledger = ResourceLedger(connection)
        released = []
        with ledger._transaction():
            tasks = {(t.get('runtime') or {}).get('resource_attempt'): t for t in _tasks(connection)}
            for reservation in _reservations(connection):
                task = tasks.get(reservation['attempt'])
                if task is None or task['state'] not in STOPPABLE or not _stopped(reservation.get('session'), reservation['profile']):
                    continue
                runtime = task.get('runtime') or {}
                has_writes = any(r['mode'] == 'write' for r in reservation['resources'])
                stopped_writer = runtime.get('resource_writer_stopped') is True or not has_writes
                if reservation['execution_reserved'] or reservation['writes_held'] and stopped_writer:
                    ledger.release_in_transaction(connection, reservation['attempt'], True,
                                                   release_writes=stopped_writer, writer_stopped=stopped_writer)
                    released.append(reservation['attempt'])
        return released


def release_task(store, task, writer_stopped=False):
    if type(writer_stopped) is not bool:
        raise ValueError('writer_stopped must be an explicit boolean')
    runtime = task.get('runtime') or {}
    if not runtime.get('resource_attempt'):
        return False
    if writer_stopped:
        runtime['resource_writer_stopped'] = True
    reservation = next((r for r in _reservations(store.db) if r['attempt'] == runtime['resource_attempt']), None)
    if reservation is None or not _stopped(reservation.get('session'), reservation['profile']):
        return False
    has_writes = any(r['mode'] == 'write' for r in reservation['resources'])
    stopped_writer = runtime.get('resource_writer_stopped') is True or not has_writes
    ResourceLedger(store.db).release_in_transaction(store.db, reservation['attempt'], True,
                                                  release_writes=stopped_writer, writer_stopped=stopped_writer)
    return True


def snapshot(state_or_connection, config=None, native_observation=None):
    with _connection(state_or_connection) as connection:
        _, policy = policy_for(config, connection)
        reconcile_stopped(connection)
        return ResourceLedger(connection).snapshot(policy, observe(connection, native_observation))


def reserve_external(state_or_connection, attempt, ticket, profile, config=None, resources=(),
                     native_observation=None):
    with _connection(state_or_connection) as connection:
        enabled, policy = policy_for(config, connection)
        if not enabled:
            return None
        ledger = ResourceLedger(connection)
        with ledger._transaction():
            configure(connection, policy)
            reserve_in_transaction(connection, attempt, ticket, profile, resources, policy, native_observation)
        return attempt


def bind_external(state_or_connection, attempt, session):
    if attempt is None:
        return
    with _connection(state_or_connection) as connection:
        ResourceLedger(connection).bind(attempt, session)


def release_external(state_or_connection, attempt, writer_stopped=False):
    if attempt is None:
        return False
    with _connection(state_or_connection) as connection:
        ledger = ResourceLedger(connection)
        reservation = next((r for r in _reservations(connection) if r['attempt'] == attempt), None)
        if reservation is None or not _stopped(reservation.get('session'), reservation['profile']):
            return False
        with ledger._transaction():
            ledger.release_in_transaction(connection, attempt, True,
                                           release_writes=writer_stopped, writer_stopped=writer_stopped)
        return True
