"""Durable request and event journal for direct card delivery.

This module owns request durability, the single claim and the delivery
lifecycle (pending -> sending -> sent | not_sent | unknown). It never talks to
a terminal. A crash or timeout leaves the request for inspection; nothing here
re-sends, repairs or truncates a record.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import time
import uuid

from .app_feedback import REQUEST_ID, _key, _open, _root

STATES = ('pending', 'sending', 'sent', 'not_sent', 'unknown')
NEXT = {'pending': {'sending', 'not_sent', 'unknown'}, 'sending': {'sent', 'not_sent', 'unknown'}}
# A request still pending/sending after this long has no live controller we can vouch for.
PENDING_ORPHAN_AFTER = 90
SENDING_ORPHAN_AFTER = 240
LABELS = {'pending': '전달 준비 중', 'sending': '전송 확인 중', 'sent': '전송 확인됨', 'not_sent': '보내지 않음', 'unknown': '결과 불명 · 확인 필요'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _base(state, create=False):
    root = _root(state, create)
    base = root / 'delivery'
    if base.is_symlink():
        raise ValueError('전달 기록 폴더가 바뀌었습니다. 경로를 확인하세요.')
    if not base.exists():
        if not create:
            return base
        base.mkdir(mode=0o700, exist_ok=True)
        _sync(root)
    if base.is_symlink():
        raise ValueError('전달 기록 폴더가 바뀌었습니다. 경로를 확인하세요.')
    info = base.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError('전달 기록 폴더의 소유권과 비공개 권한을 확인하세요.')
    return base


def _sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _locked(base):
    # Held only for short journal reads/appends, never while waiting on a terminal.
    fd = _open(base / '.lock', os.O_RDWR | os.O_CREAT)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('전달 기록을 쓰는 중입니다. 잠시 뒤 상태를 다시 확인하세요.') from error
        yield
    finally:
        os.close(fd)


def _write_new(path, body):
    fd = _open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        done = 0
        while done < len(body):
            count = os.write(fd, body[done:])
            if not count:
                raise OSError('delivery write stopped')
            done += count
        os.fsync(fd)
    finally:
        os.close(fd)


def _folder(base, rid):
    if not isinstance(rid, str) or not REQUEST_ID.match(rid):
        raise ValueError('전송 요청 ID를 확인하세요.')
    return base / rid


def read_request(folder):
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError('전달 요청 기록을 확인해야 합니다.')
    try:
        with os.fdopen(_open(folder / 'request.json', os.O_RDONLY), 'r') as source:
            request = json.load(source)
    except (OSError, ValueError) as error:
        raise ValueError('전달 요청 기록을 읽지 못했습니다. 삭제나 복구 없이 확인해야 합니다.') from error
    if (not isinstance(request, dict) or request.get('client_request_id') != folder.name
            or not isinstance(request.get('payload'), dict) or request.get('payload_hash') != digest(request['payload'])):
        raise ValueError('전달 요청 기록이 변조되었거나 형식이 다릅니다. 확인해야 합니다.')
    return request


def read_events(folder):
    """Strict replay: a partial line, gap or illegal transition is an inspect error, never repaired."""
    path = folder / 'events.jsonl'
    if path.is_symlink() or not path.exists():
        raise ValueError('전달 이벤트 기록을 확인해야 합니다.')
    events, current = [], None
    with os.fdopen(_open(path, os.O_RDONLY), 'r') as source:
        for line in source:
            try:
                if not line.endswith('\n'):
                    raise ValueError('partial')
                event = json.loads(line)
                if (event.get('seq') != len(events) + 1 or event.get('state') not in STATES
                        or not isinstance(event.get('detail'), dict) or not isinstance(event.get('at'), (int, float))):
                    raise ValueError('shape')
                if (current is None) != (event['state'] == 'pending') or current is not None and event['state'] not in NEXT.get(current, ()):
                    raise ValueError('order')
            except (ValueError, AttributeError) as error:
                raise ValueError('전달 이벤트 기록 형식을 확인해야 합니다. 자동 복구나 재전송은 하지 않습니다.') from error
            events.append(event)
            current = event['state']
    if not events:
        raise ValueError('전달 이벤트 기록이 비어 있습니다. 확인해야 합니다.')
    return events


def _append(folder, state, detail):
    events = read_events(folder)
    if state not in NEXT.get(events[-1]['state'], ()):
        raise ValueError('이미 처리된 전달 요청입니다.')
    event = {'seq': len(events) + 1, 'state': state, 'at': time.time(), 'detail': detail}
    fd = _open(folder / 'events.jsonl', os.O_WRONLY | os.O_APPEND)
    try:
        body = (canonical(event) + '\n').encode()
        done = 0
        while done < len(body):
            count = os.write(fd, body[done:])
            if not count:
                raise OSError('event append stopped')
            done += count
        os.fsync(fd)
    finally:
        os.close(fd)
    return event


def receipt(folder, request):
    path=folder/'receipt.json'
    if not path.exists() and not path.is_symlink():return None
    with os.fdopen(_open(path,os.O_RDONLY),'r') as source:value=json.load(source)
    dest=request.get('destination') or {}
    if (value.get('request_id')!=request['client_request_id'] or value.get('payload_hash')!=request['payload_hash']
            or value.get('destination')!=dest or not isinstance(value.get('summary'),str)
            or value.get('outcome') not in ('responded','deferred') or not isinstance(value.get('recorded_at'),(int,float))):
        raise ValueError('담당자 처리 기록의 요청·내용·소유권을 확인해야 합니다.')
    body=value.get('evidence')
    if body is None:
        # Earlier host-verified recoveries kept evidence beside the receipt.
        with os.fdopen(_open(path.with_name('receipt-evidence.txt'),os.O_RDONLY),'r') as source:body=source.read()
    if not isinstance(body,str):raise ValueError('담당자 응답 근거 형식을 확인해야 합니다.')
    if hashlib.sha256(body.encode()).hexdigest()!=value.get('evidence_digest'):
        raise ValueError('담당자 응답 근거가 바뀌었습니다.')
    return value


def record_receipt(state,rid,outcome,text,evidence,verifier):
    """Attach verified response evidence; never rewrite transport history or work status."""
    if outcome not in ('responded','deferred') or not isinstance(text,str) or not text.strip() or len(text)>4000:
        raise ValueError('응답 결과와 4000자 이내의 처리 요약이 필요합니다.')
    if not isinstance(evidence,str) or rid not in evidence or not isinstance(verifier,dict) or not verifier:
        raise ValueError('요청 ID가 포함된 응답 근거와 확인 주체가 필요합니다.')
    base=_base(state)
    with _locked(base):
        folder=request_dir(state,rid);req=read_request(folder)
        old=receipt(folder,req)
        if old:
            if old['outcome']==outcome and old['summary']==text.strip():return old
            raise ValueError('이미 기록된 응답입니다. 기존 근거를 덮어쓰지 않습니다.')
        if read_events(folder)[-1]['state'] not in ('sending','sent','unknown'):
            raise ValueError('전달하지 않은 요청에는 응답을 연결할 수 없습니다.')
        value={'request_id':rid,'payload_hash':req['payload_hash'],'destination':req['destination'],
               'outcome':outcome,'summary':text.strip(),'recorded_at':time.time(),'verifier':verifier,
               'evidence':evidence,'evidence_digest':hashlib.sha256(evidence.encode()).hexdigest()}
        temporary=folder/('.receipt-'+uuid.uuid4().hex)
        try:
            _write_new(temporary,canonical(value).encode())
            os.replace(temporary,folder/'receipt.json');_sync(folder)
        finally:
            if temporary.exists():temporary.unlink()
        return value


def summary(folder, now=None):
    """Display view. Orphan detection only changes what is shown; it records nothing and resends nothing."""
    request, events = read_request(folder), read_events(folder)
    last, now = events[-1], time.time() if now is None else now
    effective, orphaned = last['state'], False
    limit = {'pending': PENDING_ORPHAN_AFTER, 'sending': SENDING_ORPHAN_AFTER}.get(last['state'])
    if limit and now - last['at'] > limit:
        effective, orphaned = 'unknown', True
    dest = request.get('destination') or {}
    response=receipt(folder,request)
    if response is not None:response={k:v for k,v in response.items() if k!='evidence'}
    return {'request_id': request['client_request_id'], 'kind': request['payload']['kind'], 'work_id': request['payload']['work_id'],
            'state': last['state'], 'effective_state': effective, 'receipt': response, 'orphaned': orphaned, 'label': LABELS[effective],
            'reason': last['detail'].get('reason', '') if not orphaned else '제어 프로세스의 마지막 기록 뒤 결과를 확인하지 못했습니다. 자동 재전송 없이 보존합니다.',
            'created_at': request['created_at'], 'updated_at': last['at'], 'payload_hash': request['payload_hash'],
            'binding': {k: request['payload'].get(k) for k in ('card_revision', 'document_revision', 'document_digest', 'question_id', 'question_revision')},
            'selection': {k: request['payload'].get(k) for k in ('choice', 'comment', 'option_id', 'other_text')},
            'destination': {k: dest.get(k) for k in ('team', 'task', 'owner', 'attempt', 'terminal', 'label', 'space')} if dest else None}


def submit(state, request):
    """Persist a new request or return the existing one. Returns (summary, created)."""
    base = _base(state, True)
    rid = request['client_request_id']
    with _locked(base):
        folder = _folder(base, rid)
        if folder.exists():
            old = read_request(folder)
            if old['payload_hash'] != request['payload_hash']:
                raise ValueError('같은 요청 ID에 다른 내용이 들어왔습니다. 새 요청으로 다시 확인하세요.')
            return summary(folder), False
        for other in sorted(base.iterdir()):
            if other.name.startswith('.') or other == folder:
                continue
            seen, old = summary(other), read_request(other)
            if (seen['effective_state'] in ('pending', 'sending', 'unknown') and old['content_hash'] == request['content_hash']
                    and old.get('destination') == request.get('destination')):
                raise ValueError('같은 내용·목적지의 이전 전달 결과가 확인되지 않았습니다(' + seen['request_id'] + '). 확인 전에는 다시 보내지 않습니다.')
        folder.mkdir(mode=0o700)
        _write_new(folder / 'request.json', canonical(request).encode())
        _write_new(folder / 'events.jsonl', (canonical({'seq': 1, 'state': 'pending', 'at': time.time(), 'detail': {}}) + '\n').encode())
        _sync(folder)
        _sync(base)
        return summary(folder), True


def request_dir(state, rid):
    base = _base(state)
    folder = _folder(base, rid)
    if not folder.exists():
        raise ValueError('전달 요청 기록이 없습니다.')
    return folder


def record(state, rid, new_state, detail):
    """Append one transition under the short lock. Claiming and finishing both pass through here."""
    base = _base(state)
    with _locked(base):
        return _append(request_dir(state, rid), new_state, detail)


def claim(state, rid, controller):
    """Only one controller may move pending -> sending; its identity is durable before any terminal call."""
    base = _base(state)
    with _locked(base):
        folder = request_dir(state, rid)
        if read_events(folder)[-1]['state'] != 'pending':
            raise ValueError('이 전달 요청은 이미 처리 중이거나 처리됐습니다. 다시 보내지 않습니다.')
        _append(folder, 'sending', {'controller': controller})
        return read_request(folder)


def card_requests(state, key, limit=None):
    base = _base(state)
    if not base.exists():
        return []
    found = []
    for folder in base.iterdir():
        if folder.name.startswith('.'):
            continue
        try:
            seen = summary(folder)
        except (OSError, ValueError):
            found.append({'request_id': folder.name, 'state': 'unknown', 'effective_state': 'unknown', 'label': '기록 확인 필요',
                          'reason': '전달 기록을 읽지 못했습니다. 삭제나 재전송 없이 확인해야 합니다.', 'created_at': 0, 'updated_at': 0})
            continue
        if _key(seen['kind'], seen['work_id']) == key:
            found.append(seen)
    return sorted(found, key=lambda item: item['created_at'], reverse=True)[:limit]
