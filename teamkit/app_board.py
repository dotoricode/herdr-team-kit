"""Card presentation and reversible, private board organization.

Board organization never writes work, approval, task or delivery records.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

from .app_feedback import _key, _open


def _text(value):
    return re.sub(r'\s+', ' ', value).strip() if isinstance(value, str) else ''


def _digest(value):
    body=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
    return hashlib.sha256(body.encode()).hexdigest()


def presentation(card, detail=None, profile=None):
    """Project available evidence; a recorded task is not a live execution."""
    detail=detail or {};profile=profile or {}
    interaction=card.get('interaction') or {}
    phase=card.get('status','');state=interaction.get('state','')
    complete=phase=='complete' or card.get('status_label')=='완료' or (card.get('kind')=='inventory' and phase.startswith('완료'))
    question=card.get('question') or {};decision=card.get('decision') or {}
    suppressed=state in ('pending','sending','sent','unknown','responded','deferred','recorded','complete')
    actionable=bool(question.get('options') or decision.get('options') or (phase=='awaiting_approval' and card.get('document_state')=='available'))
    required=actionable and not complete and not suppressed and card.get('can_answer',True) is True
    summary=_text(profile.get('summary')) or _text(card.get('prompt'))
    if summary=='무엇을 확인했고 어떤 조치가 필요한가요?':summary=''
    summary=summary or _text(card.get('title')) or '작업 요약이 준비되지 않았습니다.'
    owner=_text((card.get('leader') or {}).get('label')) or _text(card.get('team_label')) or '담당자 미확인'
    sessions=[]
    roles={'leader':'설계 담당','implementer':'구현 담당','reviewer':'검토 담당'}
    for task in detail.get('tasks',[]):
        if task.get('current_design') is not True:continue
        verified=task.get('identity_verified') is True
        sessions.append({'task':task.get('id'),'role':task.get('role'),'role_label':roles.get(task.get('role'),'담당자'),
            'terminal':task.get('terminal'),'native_state':task.get('native_state','unknown') if verified else 'unknown',
            'identity_verified':verified,'can_focus':verified and task.get('can_focus') is True,
            'location':task.get('session_location'),'goal':_text(task.get('goal')),'current_design':True})
    running=[item for item in sessions if item['identity_verified'] and item['native_state']=='working']
    authored=_text(profile.get('current_activity'))
    if running:
        activity='실행 중 · '+(authored or running[0]['goal'] or running[0]['role_label'])
    else:
        uncertain=bool(detail.get('native_error')) or any(not item['identity_verified'] or item['native_state']=='unknown' for item in sessions)
        activity='실행 상태 미확인' if uncertain else '확인된 실행 세션 없음'
        if authored:activity+=' · 작업 내용: '+authored
    attention=[]
    def issue(label,reason):
        item={'label':label,'reason':_text(reason) or label}
        if item not in attention:attention.append(item)
    if state in ('unknown','execution_unknown'):
        issue(interaction.get('label') or '운영 상태 확인 필요',interaction.get('reason'))
    elif phase in ('inspect','blocked','quota','environment'):
        issue(card.get('status_label') or '운영 상태 확인 필요',card.get('reason'))
    if card.get('delivery_error'):issue('전달 기록 확인 필요',card['delivery_error'])
    if detail.get('native_error'):issue('실행 관찰 미확인',detail['native_error'])
    if card.get('kind')=='work' and card.get('document_state') in ('missing','unreadable','invalid'):
        issue('현재 설계 문서 확인 필요','현재 설계 문서를 읽을 수 없어 승인 대상과 실행 연결을 확인해야 합니다.')
    if card.get('question_state',{}).get('state') in ('invalid','stale','document_unavailable'):
        issue('질문 기록 확인 필요',card['question_state'].get('reason'))
    if actionable and card.get('can_answer') is False and not complete:
        issue('사용자 행동 비활성화','현재 설정에서는 선택을 전송하거나 승인할 수 없습니다.')
    if complete:waiting=''
    elif required:waiting='사용자 결정 대기'
    elif state in ('pending','sending'):waiting='전달 결과 대기'
    elif state=='sent':waiting='담당자 처리 결과 대기'
    elif state=='deferred':waiting='보류됨 · 다음 결정 대기'
    elif state in ('responded','recorded'):waiting='응답 기록 확인 대기'
    elif state=='review_wait' or phase=='awaiting_review':waiting='검토 결과 대기'
    elif state=='assignment_wait' or phase=='approved':waiting='담당자 배정 대기'
    elif attention:waiting='담당자 상태 확인 대기'
    elif running:waiting=''
    else:waiting=_text(interaction.get('reason')) or '담당자 진행 기록 대기'
    column='complete' if complete else 'review' if required else 'progress' if running else 'waiting'
    return {'summary':summary,'current_activity':activity,'owner_label':owner,'sessions':sessions,
        'waiting_on':waiting,'attention':attention,'decision_required':required,
        'decision_label':'설계 검토·승인' if required and phase=='awaiting_approval' else '결정하기' if required else '',
        'column':column}


def semantic_token(card):
    """Content identity excludes routing, task execution and observation changes."""
    kind,ident=card.get('kind'),card.get('id');_key(kind,ident)
    question=card.get('question') or card.get('previous_question')
    document={'revision':card.get('document_revision'),'digest':card.get('document_digest')} if kind=='work' else None
    if question and kind=='work' and (question.get('document_revision'),question.get('document_digest'))!=(card.get('document_revision'),card.get('document_digest')):
        question=None
    q={k:question.get(k) for k in ('question_id','question_revision','prompt','options','other_allowed')} if question else None
    decision=card.get('decision')
    choices={k:decision.get(k) for k in ('prompt','options')} if decision else None
    return _digest({'kind':kind,'id':ident,'title':card.get('title'),'prompt':card.get('prompt'),
        'document':document,'question':q,'decision':choices,'condition':card.get('condition') if kind=='inventory' else None})


def _base(state,create=False):
    path=Path(state).expanduser().resolve()
    base=path.parent/'app-board'
    if base.is_symlink():raise ValueError('보드 기록 폴더 경로를 확인하세요.')
    if not base.exists():
        if not create:return base
        if not path.is_file():raise ValueError('기존 GUI 작업 기록이 필요합니다.')
        base.mkdir(mode=0o700,exist_ok=True)
        _sync(base.parent)
    if base.is_symlink():raise ValueError('보드 기록 폴더 경로를 확인하세요.')
    info=base.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:
        raise ValueError('보드 기록 폴더의 소유권과 비공개 권한을 확인하세요.')
    return base


def _path(base,card):
    return base/(_digest(_key(card.get('kind'),card.get('id')))+'.json')


def _read(path,card):
    if not path.exists() and not path.is_symlink():return []
    try:
        with os.fdopen(_open(path,os.O_RDONLY),'r') as source:journal=json.load(source)
        if journal.get('schema_version')!=1 or journal.get('card')!=_key(card['kind'],card['id']) or not isinstance(journal.get('events'),list):
            raise ValueError('invalid journal')
        events=journal['events'];seen=set()
        for seq,event in enumerate(events,1):
            if (not isinstance(event,dict) or type(event.get('seq')) is not int or event['seq']!=seq or event.get('action') not in ('done','archive','restore')
                    or not isinstance(event.get('token'),str) or not re.fullmatch(r'[0-9a-f]{64}',event['token'])
                    or not isinstance(event.get('client_request_id'),str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{7,127}',event['client_request_id'])
                    or event['client_request_id'] in seen or type(event.get('at')) not in (int,float) or not math.isfinite(event['at'])):
                raise ValueError('invalid event')
            seen.add(event['client_request_id'])
        return events
    except (ValueError,TypeError,KeyError,AttributeError) as error:
        raise ValueError('보드 정리 기록을 확인해야 합니다. 기존 기록을 보존합니다.') from error


def _state(card,events):
    token=semantic_token(card);last=events[-1] if events else None
    stale=bool(last and last['token']!=token)
    disposition={'done':'done','archive':'archived','restore':'visible'}[last['action']] if last and not stale else 'visible'
    return {'disposition':disposition,'revision':_digest({'token':token,'events':events}),
            'stale':stale,'can_restore':disposition!='visible'}


def board_state(state,card):
    base=_base(state)
    return _state(card,_read(_path(base,card),card) if base.exists() else [])


@contextmanager
def _locked(path):
    fd=_open(path,os.O_RDWR|os.O_CREAT)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        yield
    finally:os.close(fd)


def _write(path,value):
    temporary=path.with_name('.'+path.name+'.'+uuid.uuid4().hex)
    try:
        fd=_open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL)
        with os.fdopen(fd,'w') as target:
            json.dump(value,target,ensure_ascii=False,sort_keys=True,allow_nan=False)
            target.flush();os.fsync(target.fileno())
        os.replace(temporary,path)
        _sync(path.parent)
    finally:
        if temporary.exists():temporary.unlink()


def _sync(path):
    directory=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(directory)
    finally:os.close(directory)


def board_action(state,card,action,client_request_id,expected_revision):
    """CAS-protected organization only. Repeating a request never replays an action."""
    if action not in ('done','archive','restore'):raise ValueError('보드 정리 동작을 확인하세요.')
    if not isinstance(client_request_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{7,127}',client_request_id):
        raise ValueError('보드 정리 요청 ID를 확인하세요.')
    token=semantic_token(card);base=_base(state,True);path=_path(base,card)
    with _locked(path.with_suffix('.lock')):
        events=_read(path,card)
        for event in events:
            if event['client_request_id']==client_request_id:
                if (event['action'],event['token'])!=(action,token):raise ValueError('같은 요청 ID의 내용이 달라졌습니다.')
                return _state(card,events)
        current=_state(card,events)
        if expected_revision!=current['revision']:raise ValueError('보드 정리 상태가 바뀌었습니다. 최신 카드를 확인하세요.')
        events.append({'seq':len(events)+1,'client_request_id':client_request_id,'action':action,'token':token,'at':time.time()})
        _write(path,{'schema_version':1,'card':_key(card['kind'],card['id']),'events':events})
        return _state(card,events)
