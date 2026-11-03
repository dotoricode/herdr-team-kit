"""Transactional execution reservations and independent resource leases.

The observation boundary is herdr-core, not every model process on the host.
Reservations never expire automatically: elapsed time is not stopped execution.
"""
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time


PROFILES = tuple(p + '/' + a for p in ('codex', 'claude') for a in ('team', 'personal'))
IDENTITY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z')
COVERAGE = ('HC foreground model processes/status; nested agents and API model calls are not separately counted; '
            'direct starts and host-wide activity are not controlled')


class ResourceUnavailable(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_policy(policy=None):
    policy = {} if policy is None else policy
    if not isinstance(policy, dict) or set(policy) - {'global_limit', 'profile_limits', 'observation_ttl_seconds'}:
        raise ValueError('unknown resource policy field')
    limit = policy.get('global_limit', 3)
    ttl = policy.get('observation_ttl_seconds', 15)
    profiles = policy.get('profile_limits', {})
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 1024):
        raise ValueError('global_limit must be null (unlimited) or an integer in 1..1024')
    if not _number(ttl) or not 0 < ttl <= 300:
        raise ValueError('observation_ttl_seconds must be in 0..300')
    if (not isinstance(profiles, dict) or set(profiles) - set(PROFILES)
            or any(type(value) is not int or not 0 <= value <= 1024 for value in profiles.values())):
        raise ValueError('profile_limits requires known profiles and integer limits in 0..1024')
    return dict(global_limit=limit, profile_limits={p: profiles.get(p, 3 if limit is None else limit) for p in PROFILES},
                observation_ttl_seconds=ttl)


def normalize_resources(resources):
    if not isinstance(resources, (list, tuple)):
        raise ValueError('resources must be a list of explicit typed resources')
    result = []
    for resource in resources:
        if (not isinstance(resource, dict) or set(resource) != {'kind', 'name', 'mode'}
                or resource['kind'] not in ('path', 'named') or resource['mode'] not in ('read', 'write')
                or not isinstance(resource['name'], str) or not resource['name']
                or not resource['name'].isprintable()):
            raise ValueError('resource requires kind=path/named, name and mode=read/write')
        name = resource['name']
        if resource['kind'] == 'path':
            if not Path(name).is_absolute():
                raise ValueError('path resource must be absolute')
            name = str(Path(name).resolve())
        elif len(name) > 256 or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*:.+', name):
            raise ValueError('named resource requires a bounded namespace:name')
        value = dict(kind=resource['kind'], name=name, mode=resource['mode'])
        if value not in result:
            result.append(value)
    return sorted(result, key=lambda r: (r['kind'], r['name'], r['mode']))


def _profile(value):
    if isinstance(value, dict) and set(value) == {'provider', 'account'}:
        value = value['provider'] + '/' + value['account']
    if value not in PROFILES:
        raise ValueError('unknown execution profile')
    return value


def _session(session):
    if session is None:
        return None
    if (not isinstance(session, dict) or set(session) != {'terminal', 'pid', 'owner'}
            or not isinstance(session['terminal'], str) or not session['terminal']
            or type(session['pid']) is not int or session['pid'] <= 0
            or not isinstance(session['owner'], str) or not session['owner']):
        raise ValueError('session requires terminal, positive native pid and owner')
    return dict(session)


def _conflict(left, right):
    if left['kind'] != right['kind'] or 'write' not in (left['mode'], right['mode']):
        return False
    if left['kind'] == 'named':
        return left['name'] == right['name']
    a, b = Path(left['name']), Path(right['name'])
    if a == b or a.is_relative_to(b) or b.is_relative_to(a):
        return True
    try:
        return a.samefile(b)
    except (FileNotFoundError, NotADirectoryError):
        return False


def observe_native(call=None, clock=time.time):
    """Read HC once; individual model identities include exact OS process IDs.

    Unknown accounts remain unknown. Native account identity must come from the
    configured profile binding, not a display label inferred by this adapter.
    """
    if call is None:
        from .core_runtime import _call
        call = _call
    started = clock()
    rows = []
    listing = call('ls', '--json')
    for item in listing['terminals']:
        if item.get('state') != 'live':
            continue
        native = call('get', item['address'])
        if native.get('state') != 'live':
            continue
        foreground = native.get('foreground') or {}
        app = (native.get('status') or {}).get('app') or item.get('agent')
        name = foreground.get('name')
        if isinstance(name, str) and Path(name).name in ('zsh', 'bash', 'sh', 'fish', 'nu', 'pwsh', 'csh', 'tcsh'):
            continue
        provider = ('codex' if name == 'codex' or app == 'codex' else
                    'claude' if name in ('claude', 'claude.exe') or app in ('claude', 'claude-code') else None)
        if provider is None:
            continue
        labels = native.get('labels') or {}
        status = (native.get('status') or {}).get('state', 'unknown') if name else 'unknown'
        rows.append(dict(terminal=native['address'], pid=foreground.get('pid'), provider=provider,
                         account=None, status=status, owner=labels.get('teamkit.owner'),
                         attempt=labels.get('teamkit.attempt')))
    return dict(observed_at=started, finished_at=clock(), complete=True, terminals=rows,
                outside_controlled=False, coverage=COVERAGE)


def _native_matches(reservation, native):
    session = reservation.get('session')
    if (not session or session['terminal'] != native['terminal'] or session['pid'] != native.get('pid')
            or reservation['profile'].split('/')[0] != native['provider']):
        return False
    if native.get('owner') is not None and native['owner'] != session['owner']:
        return False
    if native.get('attempt') is not None and native['attempt'] != reservation['attempt']:
        return False
    account = native.get('account')
    return account is None or reservation['profile'] == native['provider'] + '/' + account


class ResourceLedger:
    def __init__(self, state_or_connection, clock=time.time, *, initialize=True):
        self.clock = clock
        self._owned = not isinstance(state_or_connection, sqlite3.Connection)
        self._closed = False
        if self._owned:
            path = Path(state_or_connection).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
            self.connection = sqlite3.connect(path, isolation_level=None, timeout=10)
        else:
            self.connection = state_or_connection
        if initialize:
            self.connection.execute('CREATE TABLE IF NOT EXISTS resource_reservations '
                                    '(attempt TEXT PRIMARY KEY, ticket TEXT NOT NULL, body TEXT NOT NULL)')
        elif not self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='resource_reservations'").fetchone():
            raise ValueError('resource schema migration required')

    def close(self):
        if self._owned and not self._closed:
            self.connection.close()
            self._closed = True

    @contextmanager
    def _transaction(self):
        if self.connection.in_transaction:
            yield self.connection
            return
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            yield self.connection
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    @staticmethod
    def _rows(connection):
        return [json.loads(row[0]) for row in connection.execute(
            'SELECT body FROM resource_reservations ORDER BY attempt')]

    @staticmethod
    def _write(connection, record):
        connection.execute('INSERT OR REPLACE INTO resource_reservations VALUES (?,?,?)',
                           (record['attempt'], record['ticket'], json.dumps(record, sort_keys=True)))

    def _session_available(self, connection, attempt, session):
        if session is None:
            return
        if any(row['attempt'] != attempt and row['execution_reserved'] and row.get('session')
               and row['session']['terminal'] == session['terminal'] for row in self._rows(connection)):
            raise ResourceUnavailable('session_conflict', 'native terminal already belongs to another live attempt')

    def _snapshot(self, connection, policy, native_observation):
        policy = validate_policy(policy)
        now = self.clock()
        obs = native_observation
        if (not isinstance(obs, dict) or obs.get('complete') is not True
                or not _number(obs.get('observed_at'))
                or not 0 <= now - obs['observed_at'] <= policy['observation_ttl_seconds']
                or not isinstance(obs.get('terminals'), list)):
            raise ResourceUnavailable('observation_unavailable', 'fresh complete HC observation is required')
        reservations = [r for r in self._rows(connection) if r['execution_reserved']]
        entries = {r['attempt']: dict(profile=r['profile'], unknown=False) for r in reservations}
        seen, outside = set(), 0
        for row in obs['terminals']:
            if not isinstance(row, dict) or not isinstance(row.get('terminal'), str) or not row['terminal']:
                raise ResourceUnavailable('observation_invalid', 'native terminal identity is missing')
            terminal = row['terminal']
            if terminal in seen:
                raise ResourceUnavailable('observation_invalid', 'duplicate native terminal observation')
            seen.add(terminal)
            if row.get('model') is False:
                continue
            provider = row.get('provider')
            if provider not in ('codex', 'claude'):
                raise ResourceUnavailable('observation_invalid', 'model provider is unknown')
            status = row.get('status', 'unknown')
            if status in ('idle', 'done', 'blocked', 'finished'):
                continue
            matched = next((r for r in reservations if _native_matches(r, row)), None)
            if matched:
                entries[matched['attempt']]['unknown'] |= status != 'working'
                continue
            account = row.get('account')
            profile = provider + '/' + account if account in ('team', 'personal') else None
            entries['native:' + terminal] = dict(profile=profile, provider=provider, unknown=status != 'working')
            outside += 1
        counts = {p: sum(e['profile'] == p for e in entries.values()) for p in PROFILES}
        unknown_profiles = {provider: sum(e.get('profile') is None and e.get('provider') == provider
                                         for e in entries.values()) for provider in ('codex', 'claude')}
        used = len(entries)
        return dict(global_limit=policy['global_limit'], used=used,
                    available=None if policy['global_limit'] is None else max(0, policy['global_limit'] - used), reserved=len(reservations),
                    profile_used=counts, profile_limits=policy['profile_limits'],
                    profile_unknown=unknown_profiles, outside_ledger=outside,
                    unknown_slots=sum(e['unknown'] for e in entries.values()),
                    observed_at=obs['observed_at'], observation_age=now - obs['observed_at'],
                    outside_controlled=False, coverage=COVERAGE)

    def snapshot(self, policy=None, native_observation=None):
        return self._snapshot(self.connection, policy, native_observation)

    def reserve(self, attempt, ticket, profile, resources, policy, native_observation, session=None):
        with self._transaction() as connection:
            return self.reserve_in_transaction(connection, attempt, ticket, profile, resources,
                                               policy, native_observation, session=session)

    def reserve_in_transaction(self, connection, attempt, ticket, profile, resources,
                               policy, native_observation, session=None, override_capacity=False):
        if not connection.in_transaction:
            raise ValueError('reservation requires caller-owned active transaction')
        if not isinstance(attempt, str) or not IDENTITY.fullmatch(attempt) or not isinstance(ticket, str) or not IDENTITY.fullmatch(ticket):
            raise ValueError('bounded attempt and ticket identities required')
        profile, resources, session = _profile(profile), normalize_resources(resources), _session(session)
        old = next((r for r in self._rows(connection) if r['attempt'] == attempt), None)
        if old:
            if any(old[k] != v for k, v in [('ticket', ticket), ('profile', profile), ('resources', resources)]):
                raise ValueError('attempt identity already has another immutable request')
            if session is not None and old.get('session') != session:
                raise ValueError('attempt native binding changed')
            if not old['execution_reserved']:
                raise ResourceUnavailable('attempt_released', 'released attempts cannot be resurrected')
            return old
        snap = self._snapshot(connection, policy, native_observation)
        # Only an explicit manual priority send skips the two capacity counters.
        # Session and write-lease conflicts below always remain enforced.
        if override_capacity is not True and snap['available'] is not None and snap['available'] <= 0:
            raise ResourceUnavailable('global_capacity', 'global execution capacity is exhausted')
        if (override_capacity is not True
                and snap['profile_used'][profile] + snap['profile_unknown'][profile.split('/')[0]] >= snap['profile_limits'][profile]):
            raise ResourceUnavailable('profile_capacity', 'profile execution capacity is exhausted or unknown')
        self._session_available(connection, attempt, session)
        for existing in self._rows(connection):
            if existing['writes_held'] and any(_conflict(a, b) for a in resources for b in existing['resources']):
                raise ResourceUnavailable('resource_conflict', 'another attempt holds a conflicting resource lease')
        record = dict(attempt=attempt, ticket=ticket, profile=profile, resources=resources,
                      session=session, execution_reserved=True, writes_held=bool(resources),
                      reserved_at=self.clock(), released_at=None)
        self._write(connection, record)
        return record

    def bind(self, attempt, session):
        session = _session(session)
        if session is None:
            raise ValueError('native session binding is required')
        with self._transaction() as connection:
            row = next((r for r in self._rows(connection) if r['attempt'] == attempt), None)
            if row is None or not row['execution_reserved']:
                raise ValueError('only a reserved attempt can bind a native session')
            if row.get('session') is not None and row['session'] != session:
                raise ValueError('native binding is immutable for this attempt')
            self._session_available(connection, attempt, session)
            row['session'] = session
            self._write(connection, row)
            return row

    def release(self, attempt, execution_stopped, release_writes=False, writer_stopped=False):
        with self._transaction() as connection:
            return self.release_in_transaction(connection, attempt, execution_stopped,
                                               release_writes, writer_stopped)

    def release_in_transaction(self, connection, attempt, execution_stopped,
                               release_writes=False, writer_stopped=False):
        if not connection.in_transaction:
            raise ValueError('release requires caller-owned active transaction')
        if execution_stopped is not True:
            raise ValueError('explicit stopped-execution evidence is required')
        if type(release_writes) is not bool or release_writes and writer_stopped is not True:
            raise ValueError('write lease release requires separate stopped-writer evidence')
        row = next((r for r in self._rows(connection) if r['attempt'] == attempt), None)
        if row is None:
            raise ValueError('unknown resource reservation attempt')
        row['execution_reserved'] = False
        if release_writes:
            row['writes_held'] = False
        if row['released_at'] is None:
            row['released_at'] = self.clock()
        self._write(connection, row)
        return row
