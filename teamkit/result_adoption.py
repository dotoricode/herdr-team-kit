"""Explicit operator adoption of historical results; never delivers actor input."""
import hashlib
import json
import os
from pathlib import Path
import stat
import time
from datetime import datetime


def _author(packet, task, job, config):
    if task.get('provider') != 'claude':
        raise ValueError('this adoption path requires a verifiable Claude assistant transcript')
    claim = packet.get('authorship') or {}
    session = claim.get('session_id')
    runtime = task['runtime']
    sessions = [x for x in (runtime.get('resumed_session'), runtime.get('context_session_id'),
                           (runtime.get('reboot_restore') or {}).get('session_id')) if x]
    if not sessions or not isinstance(session, str) or any(x != session for x in sessions):
        raise ValueError('authorship session does not match current recorded conversation')
    path = Path(claim.get('transcript', ''))
    if not path.is_absolute():
        raise ValueError('absolute authorship transcript required')
    from .config import profile
    expected_root = Path(profile(config,task['provider'],task['account'])['home']).resolve()/'projects'
    if path.name != session+'.jsonl' or not path.resolve().is_relative_to(expected_root):
        raise ValueError('authorship must come from the bound profile conversation transcript')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    found = None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 512*1024*1024:
            raise ValueError('bounded regular authorship transcript required')
        while True:
            line = stream.readline(2*1024*1024+1)
            if not line:
                break
            if len(line)>2*1024*1024:
                raise ValueError('authorship transcript record exceeds bound')
            raw = line.rstrip(b'\r\n')
            if hashlib.sha256(raw).hexdigest() == claim.get('record_sha256'):
                found = json.loads(raw)
                break
    if not isinstance(found, dict):
        raise ValueError('pinned authorship record missing')
    message = found.get('message') or {}
    content = message.get('content')
    text = content if isinstance(content, str) else '\n'.join(x.get('text', '') for x in content or [] if isinstance(x,dict) and x.get('type') == 'text')
    try:
        at = datetime.fromisoformat(found['timestamp'].replace('Z','+00:00')).timestamp()
    except (KeyError, TypeError, ValueError):
        raise ValueError('authorship timestamp missing or invalid')
    if (found.get('timestamp') != claim.get('timestamp') or at > time.time()+5 or at < job['at']
            or found.get('type') != 'assistant' or message.get('role') != 'assistant'
            or message.get('stop_reason') != 'end_turn' or message.get('model') != task.get('model')
            or (found.get('sessionId') or found.get('session_id')) != session
            or Path(packet['result_artifact']).name not in text):
        raise ValueError('authorship, result reference or original request chronology differs')
    return dict(**claim, authored_at=at, record=found, provider=task['provider'], model=task['model'], owner=task['owner'], task=task['id'])


def _no_leases(db, task):
    from .resources import ResourceLedger, _conflict
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='resource_reservations'").fetchone():
        return
    root = dict(kind='path', name=task['root'], mode='write')
    for row in ResourceLedger._rows(db):
        session = row.get('session') or {}
        actor = session.get('owner') == task['owner'] or session.get('terminal') == task['runtime']['terminal']
        overlaps = row['writes_held'] and any(_conflict(root, x) for x in row['resources'])
        if (actor and (row['execution_reserved'] or row['writes_held'])) or overlaps:
            raise ValueError('active execution or write lease prevents historical result adoption')


def adopt(config_path, state, ident, evidence_file, owner, attempt, operator_approved=False):
    from . import context_handoff as context, pool_control as control
    from .core_runtime import CoreRuntime, require_managed, _values, _call
    from .config import load
    from .store import Store
    require_managed()
    if operator_approved is not True or not isinstance(ident,str) or not context.JOB.fullmatch(ident):
        raise ValueError('explicit operator approval and request identity required')
    packet, evidence_digest = control._result_file(Path(evidence_file).absolute())
    if (not isinstance(owner,str) or not owner or type(attempt) is not int or attempt < 1
            or packet.get('request_id') != ident or packet.get('owner') != owner or packet.get('attempt') != attempt
            or packet.get('writer_stopped') is not True
            or not isinstance(packet.get('operator_reference'),str) or not packet['operator_reference'].strip()
            or not isinstance(packet.get('verification'),list) or not packet['verification']
            or any(not isinstance(v,str) or not v.strip() for v in packet['verification'])):
        raise ValueError('operator result identity, verification and stopped-writer evidence required')
    control._validate_artifacts(packet)
    if packet.get('result_artifact') not in [x['path'] for x in packet['artifacts']]:
        raise ValueError('authored result must be a verified artifact')
    path = control.folder(state)/(ident+'.json')
    initial = context.read(path)
    if not initial:
        raise ValueError('original request missing')
    lock = hashlib.sha256(initial['team'].encode()).hexdigest()
    with context.locked(control.folder(state)/('team-'+lock)), context.locked(path):
        job = context.read(path)
        prior = job.get('operator_result_adoption')
        if prior and prior.get('evidence_sha256') == evidence_digest:
            if (job['phase'] != 'completed' or control._file_proof(prior['original_request_file']) != prior['original_request_sha256']
                    or control._file_proof(prior['evidence_snapshot']) != prior['evidence_snapshot_sha256']):
                raise ValueError('adopted original request evidence changed')
            return {k:v for k,v in job.items() if k != 'message'}
        original = path.read_bytes()
        if (job.get('phase') not in ('pending','received','done') or job.get('submission') or job.get('delivery_id')
                or prior or hashlib.sha256(original).hexdigest() != packet.get('request_record_sha256')
                or hashlib.sha256(job['message'].encode()).hexdigest() != packet.get('request_digest')
                or job.get('digest') != packet.get('request_digest')
                or packet.get('work_id') != job.get('work_id',ident)
                or packet.get('completion_requirements') != job.get('completion_requirements')):
            raise ValueError('original request state, digest, work scope or completion requirements changed')
        receiver = job.get('receiver')
        if receiver and (receiver.get('owner') != owner or receiver.get('attempt') != attempt):
            raise ValueError('original receiver owner attempt differs')
        config = load(config_path)
        store = Store(state,read_only=True)
        try:
            task = store.get(job['task'])
            events = store.events(job['task'])
            if (config.get('runtime_backend') != 'core' or task['state'] != 'ready'
                    or task['team'] != job['team'] or task['owner'] != owner or task['attempt_count'] != attempt):
                raise ValueError('current ready task differs from the historical performer')
            _no_leases(store.db, task)
        finally:
            store.close()
        if job.get('work_id'):
            metadata_path = packet.get('result_metadata')
            if metadata_path not in [x['path'] for x in packet['artifacts']]:
                raise ValueError('linked-work result metadata must be a verified artifact')
            metadata, _ = control._result_file(Path(metadata_path))
            performer = metadata.get('executor') or {}
            if (metadata.get('work_id') != job['work_id'] or metadata.get('status') != 'completed'
                    or metadata.get('result_path') != packet['result_artifact']
                    or any(performer.get(k) != task.get(k) for k in ('provider','account','model'))
                    or performer.get('task') != task['id']):
                raise ValueError('linked result scope or declared performer differs')
        author = _author(packet, task, job, config)
        claims = [e for e in events if e['event']=='claimed']
        if (len(claims) != attempt or claims[-1].get('detail',{}).get('owner') != owner
                or claims[-1]['at'] > author['authored_at']):
            raise ValueError('result authorship is outside the pinned execution attempt')
        if receiver and receiver.get('terminal') != task['runtime']['terminal']:
            event = next((e for e in events if e['seq'] == packet.get('lineage_event_seq')),None)
            detail = (event or {}).get('detail') or {}
            if (not event or event['event'] != 'native-session-resumed' or event['at'] < job['at']
                    or detail.get('from_terminal') != receiver['terminal'] or detail.get('to_terminal') != task['runtime']['terminal']
                    or detail.get('session') != author['session_id'] or detail.get('owner_preserved') is not True
                    or detail.get('state_preserved') is not True):
                raise ValueError('owner-preserving native restart lineage required')
        listing = _call('ls','--all','--json')
        rows = listing.get('terminals')
        if not isinstance(rows,list) or any(not isinstance(x,dict) or not isinstance(x.get('address'),str) for x in rows):
            raise ValueError('complete actor-terminal inventory unavailable')
        if receiver and receiver.get('terminal') != task['runtime']['terminal']:
            previous = next((x for x in rows if x['address']==receiver['terminal']),None)
            if previous is not None and previous.get('state') != 'finished':
                raise ValueError('previous actor terminal still exists and is not finished')
        descendants = {task['runtime']['terminal']}
        while True:
            expanded = descendants | {x['address'] for x in rows if x.get('parent') in descendants}
            if expanded == descendants:
                break
            descendants = expanded
        for child in rows:
            if child['address'] not in descendants or child['address']==task['runtime']['terminal'] or child.get('state')=='finished':
                continue
            status = child.get('status')
            status = status.get('state') if isinstance(status,dict) else status
            if status not in ('idle','done'):
                raise ValueError('active or unverified child terminal prevents result adoption')
        native = CoreRuntime(task)._native(task['runtime']['name'],ready=True)
        argv = (native.get('foreground') or {}).get('argv') or []
        sessions = _values(argv,'--resume') if task['provider']=='claude' else [argv[i+1] for i,v in enumerate(argv[:-1]) if v=='resume']
        if sessions != [author['session_id']]:
            raise ValueError('actual native conversation differs from result author')
        # No input or draft read is performed: this operation only acknowledges an already authored result.
        control._validate_artifacts(packet)
        original_path = path.with_name(ident+'.before-operator-adoption-'+evidence_digest+'.json')
        def preserve(destination, raw):
            try:
                fd = os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            except FileExistsError:
                if control._file_proof(str(destination)) != hashlib.sha256(raw).hexdigest():
                    raise ValueError('preserved operator evidence differs')
                return
            with os.fdopen(fd,'wb') as output:
                output.write(raw);output.flush();os.fsync(output.fileno())
        preserve(original_path,original)
        evidence_snapshot = path.with_name(ident+'.operator-adoption-evidence-'+evidence_digest+'.json')
        evidence_bytes = json.dumps(packet,ensure_ascii=False,sort_keys=True).encode()
        preserve(evidence_snapshot,evidence_bytes)
        audit = dict(authority='operator', evidence_file=str(Path(evidence_file).absolute()), evidence_sha256=evidence_digest,
            original_request_file=str(original_path),original_request_sha256=hashlib.sha256(original).hexdigest(),
            evidence_snapshot=str(evidence_snapshot),evidence_snapshot_sha256=hashlib.sha256(evidence_bytes).hexdigest(),
            original_phase=job['phase'], original_receiver=receiver, performer=author,
            native=dict(terminal=task['runtime']['terminal'],pid=task['runtime']['native_pid'],turn=native.get('turn')),
            verification=packet['verification'],artifacts=packet['artifacts'],writer_stopped=True,at=time.time())
        job.update(phase='completed',operator_result_adoption=audit,work_resolution=audit,
                   reason='운영본부가 기존 결과·작성자·범위를 대조해 수령했습니다. 새 입력 전송 없음.',updated_at=time.time())
        control.write_request(path,job)
        control.notify(state)
        return {k:v for k,v in job.items() if k != 'message'}
