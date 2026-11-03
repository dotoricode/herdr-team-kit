"""Durable, read-only user-message capture; capture never grants execution."""
import json
from pathlib import Path
import sqlite3
import time

from .tickets import digest


def schema(connection):
    connection.execute('CREATE TABLE IF NOT EXISTS operation_instructions '
                       '(id TEXT PRIMARY KEY, body TEXT NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS operation_intake_cursors '
                       '(session TEXT PRIMARY KEY, body TEXT NOT NULL)')


def register(connection, text, source, *, clock=time.time):
    if not isinstance(text, str) or not text.strip() or len(text.encode()) > 1024 * 1024:
        raise ValueError('bounded nonempty original instruction required')
    if not isinstance(source, dict) or not source.get('key'):
        raise ValueError('stable original message source key required')
    ident = 'instruction-' + digest(source)[:32]
    schema(connection)
    old = connection.execute('SELECT body FROM operation_instructions WHERE id=?', (ident,)).fetchone()
    if old:
        result = json.loads(old[0])
        if result['text'] != text:
            raise ValueError('original message identity conflicts')
        return result
    value = dict(id=ident, text=text, source=source, created_at=clock(), state='awaiting_classification',
                 reason='사용자 지시의 목표·승인·담당 제품과 완료 조건 분류 필요',
                 next_action='운영본부에서 원 지시를 대조해 실행 티켓 등록')
    connection.execute('INSERT INTO operation_instructions VALUES (?,?)', (ident,json.dumps(value)))
    return value


def pending(connection):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operation_instructions'").fetchone():
        return []
    return [json.loads(row[0]) for row in connection.execute('SELECT body FROM operation_instructions')
            if json.loads(row[0])['state'] not in ('ticketed','dismissed')]


def capture(config, state):
    """Tail the bound coordinator's Codex event journal, retaining partial lines.

    The first observation starts at EOF. Existing historical conversations are
    never replayed as new instructions. A reboot resumes the durable byte cursor.
    """
    if config.get('operations',{}).get('intake',{}).get('enabled') is not True:
        return {'enabled':False}
    journal_format = config['operations']['intake'].get('journal_format','response_item')
    if journal_format not in ('response_item','event_msg'):
        raise ValueError('unsupported coordinator journal user-message contract')
    from .app_coordinator import read_record
    from .context_observation import _rollout_path
    from .config import profile
    record = read_record(state)
    session = record.get('context_session_id')
    if not session or config.get('coordinator',{}).get('provider')!='codex':
        return {'enabled':True,'reason':'bound Codex coordinator journal unavailable'}
    home = Path(profile(config,'codex',record['account'])['home'])
    path = _rollout_path(home,session)
    if path is None:
        return {'enabled':True,'reason':'bound coordinator journal not found; no inference of success'}
    connection = sqlite3.connect(state, timeout=10,isolation_level=None)
    try:
        schema(connection)
        connection.execute('BEGIN IMMEDIATE')
        old = connection.execute('SELECT body FROM operation_intake_cursors WHERE session=?',(session,)).fetchone()
        identity = dict(path=str(path),device=path.stat().st_dev,inode=path.stat().st_ino)
        cursor = json.loads(old[0]) if old else dict(**identity,offset=path.stat().st_size)
        if any(cursor.get(k)!=v for k,v in identity.items()) or path.stat().st_size < cursor['offset']:
            raise ValueError('coordinator journal changed or truncated; preserve original intake cursor')
        captured=[]
        with path.open('rb') as stream:
            stream.seek(cursor['offset'])
            total=0
            while total < 4*1024*1024:
                start=stream.tell()
                line=stream.readline(1024*1024+1)
                if not line or not line.endswith(b'\n'):
                    break
                total+=len(line)
                if len(line)>1024*1024:
                    raise ValueError('oversized coordinator event requires inspection')
                event=json.loads(line)
                payload=event.get('payload',{})
                text = None
                if journal_format=='response_item' and event.get('type')=='response_item' and payload.get('role')=='user':
                    text='\n'.join(x.get('text','') for x in payload.get('content',[])
                                   if isinstance(x,dict) and isinstance(x.get('text'),str))
                elif journal_format=='event_msg' and event.get('type')=='event_msg' and payload.get('type')=='user_message':
                    text=payload.get('message','')
                if isinstance(text,str):
                    # Agent notifications and environment envelopes are not user instructions.
                    if text.strip() and not text.lstrip().startswith(('<hc ', '<herdr-intake-classify ', '<environment_context>', '# AGENTS.md instructions')):
                        captured.append(register(connection,text,dict(key=session+':'+str(start),
                            session=session,terminal=record.get('terminal'),offset=start)))
                cursor['offset']=stream.tell()
        connection.execute('INSERT OR REPLACE INTO operation_intake_cursors VALUES (?,?)',(session,json.dumps(cursor)))
        connection.commit()
        return {'enabled':True,'captured':[x['id'] for x in captured], 'cursor':cursor['offset']}
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def dismiss(connection, ident, reason, expected_digest, *, clock=time.time):
    """Exclude confirmed non-work without erasing text, receipt or attempts.

    Caller holds the same immediate transaction used by ticket creation.
    Active/uncertain classification attempts are never released or cancelled.
    """
    if not connection.in_transaction:
        raise ValueError('non-work disposition requires the queue transaction')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
        raise ValueError('업무가 아닌 것으로 정리하는 구체 이유가 필요합니다.')
    row = connection.execute('SELECT body FROM operation_instructions WHERE id=?', (ident,)).fetchone()
    if row is None:
        raise ValueError('unknown original instruction')
    item = json.loads(row[0])
    signature = digest({'text': item['text'], 'source': item['source']})
    if signature != expected_digest:
        raise ValueError('원 지시가 바뀌었습니다. 원문을 다시 확인하세요.')
    if item.get('state') == 'dismissed':
        return item
    if item.get('tickets') or item.get('ticket_ids') or any(
            json.loads(row[0]).get('instruction_id') == ident
            for row in connection.execute('SELECT body FROM operation_tickets')):
        raise ValueError('이미 업무 티켓이 연결되어 있습니다. 업무 상태는 별도로 처리하세요.')
    item.setdefault('dispositions', []).append(dict(action='non_task', reason=reason.strip(),
        previous_state=item['state'], previous_reason=item.get('reason'),
        previous_next_action=item.get('next_action'), classification_attempt=item.get('classification_attempt'),
        source_digest=signature, at=clock()))
    item.update(state='dismissed', reason=reason.strip(), next_action='일상 대화로 보존 · 업무 배정 제외')
    connection.execute('UPDATE operation_instructions SET body=? WHERE id=?', (json.dumps(item), ident))
    return item
