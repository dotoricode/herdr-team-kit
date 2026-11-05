"""Read-only product-work and resource policy for durable queue consumers."""
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import subprocess

from . import workflow
from .inspection import record
from .store import ACTIVE, sha


BUSY_PHASES = {'active', 'claimed', 'working', 'running', 'sending', 'dispatching',
               'waiting_handoff', 'received', 'done', 'uncertain'}


def _snapshot(state, ident):
    if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', ident):
        raise ValueError('invalid product work identity')
    path = Path(state).expanduser().resolve()
    if not path.is_file():
        raise ValueError('task ledger does not exist')
    with path.open('rb') as source:
        header = source.read(20)
    if (header[:16] == b'SQLite format 3\x00'
            and (2 in header[18:20] or Path(str(path)+'-wal').exists() or Path(str(path)+'-shm').exists())):
        raise ValueError('WAL ledger inspection is unsupported without side-file writes')
    try:
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=1)) as db:
            db.execute('BEGIN')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_items'").fetchone():
                raise ValueError('unknown product work')
            row = db.execute('SELECT body FROM work_items WHERE id=?', (ident,)).fetchone()
            if row is None:
                raise ValueError('unknown product work')
            work = record(row[0], 'product work')
            tasks = [record(row[0], 'task') for row in db.execute('SELECT body FROM tasks')]
            events = {task: seq for task, seq in db.execute(
                "SELECT task,MAX(seq) FROM events WHERE event='awaiting_review' GROUP BY task")}
            return work, [t for t in tasks if t.get('work_id') == ident], events
    except sqlite3.Error as error:
        raise ValueError('cannot read task ledger') from error


def _absolute(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError('resource root must be an absolute path')
    return Path(value).resolve()


def _allowed(config, team):
    from .departments import organization
    definition = organization(config, team)
    return [_absolute(definition['root']),
            *(_absolute(p) for p in config.get('shared_roots', []))]


def _within(root, allowed):
    return any(root == p or root.is_relative_to(p) for p in allowed)


def _work(config, state, team, ident):
    work, tasks, events = _snapshot(state, ident)
    allowed = _allowed(config, team)
    root = _absolute(work.get('root'))
    if work.get('id') != ident or work.get('team') != team or not _within(root, allowed):
        raise ValueError('product work differs from the queue team or authorized roots')
    if type(work.get('revision')) is not int or work['revision'] < 1:
        raise ValueError('invalid product work revision')
    try:
        _, contract, digest = workflow.document(config, work)
    except (OSError, UnicodeError) as error:
        raise ValueError('cannot read current product work documents') from error
    reference = {'work_id': ident, 'revision': work['revision'], 'digest': digest, 'root': str(root)}
    return work, contract, reference, tasks, events


def capture_work(config, state, team, workflow_id):
    if workflow_id is None:
        return None
    return _work(config, state, team, workflow_id)[2]


def resources(config, team, workflow_ref=None, extra=()):
    allowed = _allowed(config, team)
    if not isinstance(extra, (list, tuple)):
        raise ValueError('extra resource roots must be a list')
    from .departments import organization
    default = workflow_ref.get('root') if workflow_ref else organization(config, team)['root']
    result = []
    for value in (default, *extra):
        path = _absolute(value)
        if not _within(path, allowed):
            raise ValueError('resource root is outside configured team and shared roots')
        if str(path) not in result:
            result.append(str(path))
    return result


def _linked(config, state, job):
    reference = job.get('workflow_ref')
    if reference is None:
        return None
    if (not isinstance(reference, dict) or set(reference) != {'work_id', 'revision', 'digest', 'root'}
            or type(reference['revision']) is not int or reference['revision'] < 1
            or not isinstance(reference['digest'], str) or not re.fullmatch(r'[a-f0-9]{64}', reference['digest'])):
        raise ValueError('invalid pinned workflow reference')
    result = _work(config, state, job['team'], reference['work_id'])
    if result[2] != reference:
        raise ValueError('pinned product work revision, digest or root changed; reconcile before execution')
    return result


def _approval(work, reference):
    approval = work.get('approval') or {}
    return (isinstance(approval, dict) and work.get('phase') in ('approved', 'in_progress', 'awaiting_review', 'complete')
            and approval.get('digest') == reference['digest']
            and approval.get('revision') == reference['revision'])


def _job_roots(config, job):
    values = job.get('resources')
    if values is None:
        return resources(config, job['team'], job.get('workflow_ref'))
    if not isinstance(values, list) or not values:
        raise ValueError('queue resources must be a nonempty list')
    # Recheck persisted roots rather than trusting an earlier enqueue.
    return resources(config, job['team'], job.get('workflow_ref'), values)


def _overlap(left, right):
    # Queue roots carry no read mode: each one is a claimed work area, so this is a write-write overlap.
    a, b = Path(left), Path(right)
    if a == b or a.is_relative_to(b) or b.is_relative_to(a):
        return True
    try:
        return a.samefile(b)
    except (FileNotFoundError, NotADirectoryError):
        return False


def admission(config, state, job, all_jobs):
    linked = None
    try:
        linked = _linked(config, state, job)
    except ValueError as error:
        return str(error)
    if linked and not _approval(linked[0], linked[2]):
        return '현재 고정 설계 버전의 사용자 승인을 기다립니다.'
    if linked:
        try:
            workflow.scope_check(linked[0], linked[1])
        except (ValueError, OSError, subprocess.SubprocessError):
            return '현재 소스의 변경 범위가 고정 승인 계약과 일치하는지 확인해야 합니다.'
    dependencies = job.get('dependencies', [])
    if (not isinstance(dependencies, list)
            or any(not isinstance(x, str) or not re.fullmatch(r'[a-f0-9]{32}', x) for x in dependencies)
            or len(set(dependencies)) != len(dependencies)):
        raise ValueError('dependencies require distinct queue request identities')
    indexed = {}
    for item in all_jobs:
        if item['id'] in indexed:
            raise ValueError('duplicate queue request identity')
        indexed[item['id']] = item
    for ident in dependencies:
        if ident not in indexed:
            return '선행 요청을 확인할 수 없습니다.'
        if indexed[ident].get('phase') != 'completed':
            return '선행 요청의 검증 완료를 기다립니다.'
    own = _job_roots(config, job)
    for other in all_jobs:
        if other['id'] == job['id'] or other['team'] == job['team'] or other.get('phase') not in BUSY_PHASES:
            continue
        if any(_overlap(a, b) for a in own for b in _job_roots(config, other)):
            return '다른 팀의 진행 중 요청과 공유 작업 경로가 겹칩니다.'
    return None


def completion(config, state, job):
    linked = _linked(config, state, job)
    if linked is None:
        return None
    work, contract, reference, tasks, events = linked
    if work.get('phase') != 'complete':
        return None
    if not _approval(work, reference):
        raise ValueError('completed work has no matching approved revision')
    head = work.get('completed_head')
    review_id = work.get('review_task')
    if not sha(head) or not isinstance(review_id, str) or not review_id:
        raise ValueError('completed work lacks its formal head or reviewer')
    milestones = work.get('milestones', {})
    if not isinstance(milestones, dict) or set(milestones) != {s['id'] for s in contract['steps']}:
        raise ValueError('completed work lacks all required milestones')
    fingerprints = set()
    for point in milestones.values():
        if (not isinstance(point, dict) or point.get('head') != head
                or not isinstance(point.get('fingerprint'), str) or not re.fullmatch(r'[a-f0-9]{64}', point['fingerprint'])
                or not workflow.strings(point.get('verification'))):
            raise ValueError('completed work milestone evidence is missing or stale')
        fingerprints.add(point['fingerprint'])
    if len(fingerprints) != 1 or any(t.get('state') in ACTIVE for t in tasks):
        raise ValueError('completed work has inconsistent evidence or an active writer')
    reviewer = next((t for t in tasks if t.get('id') == review_id), None)
    if (not reviewer or reviewer.get('role') != 'reviewer' or reviewer.get('state') != 'awaiting_review'
            or reviewer.get('design_digest') != reference['digest']
            or reviewer.get('review_verdict') != 'pass' or reviewer.get('runtime', {}).get('identity_checked') is not True
            or reviewer.get('head') != head or reviewer.get('base') != head):
        raise ValueError('formal completion reviewer is missing or stale')
    for task in tasks:
        if task.get('role') == 'implementer' and (
                task.get('owner') == reviewer.get('owner')
                or task.get('runtime', {}).get('name') == reviewer.get('runtime', {}).get('name')):
            raise ValueError('formal completion reviewer is not independent')
    reviews = [t for t in tasks if t.get('role') == 'reviewer'
               and t.get('design_digest') == reference['digest'] and t.get('base') == head]
    if any(t.get('state') != 'awaiting_review' or t['id'] not in events for t in reviews):
        raise ValueError('an applicable review is unresolved')
    if not reviews or max((events[t['id']], t['id']) for t in reviews)[1] != review_id:
        raise ValueError('formal completion is not linked to the latest applicable review')
    return {**reference, 'completed_head': head, 'review_task': review_id,
            'verification': ['공식 업무 장부 complete와 고정 승인 버전·단계 근거·독립 리뷰를 대조했습니다.']}
