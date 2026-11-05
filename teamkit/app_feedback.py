"""Version-bound card questions and replies; recording never approves work.

Typed questions are appended to questions.jsonl by the current leader only.
Direct delivery lives in app_delivery; this module validates what may be sent.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import uuid

from .config import load
from .dashboard import LABELS, model
from .policy import number
from .workflow import document

CHOICES=({'id':'confirmed','label':'확인했습니다'}, {'id':'changes','label':'수정이 필요합니다'},
         {'id':'defer','label':'나중에 확인합니다'})
MESSAGE='답변을 저장했습니다. 총괄 검토 대기입니다.'
PROMPT='무엇을 확인했고 어떤 조치가 필요한가요?'


def _decision(raw, can_approve, document=None):
    if raw is None:return None
    if not isinstance(raw,dict) or not {'prompt','options'}.issubset(raw) or not set(raw)<={'prompt','options','document'}:
        raise ValueError('선택 질문에는 prompt와 options가 필요합니다.')
    bound=raw.get('document')
    if document is not None:
        if (not isinstance(bound,dict) or set(bound)!={'revision','digest','phase'}
                or not isinstance(bound['phase'],str) or not bound['phase'].strip()
                or type(bound['revision']) is not int or bound['revision']<1
                or not isinstance(bound['digest'],str) or not re.fullmatch(r'[0-9a-f]{64}',bound['digest'])):
            raise ValueError('작업별 선택지는 설계 버전과 내용에 연결해야 합니다.')
        if bound!=document:return None
    elif bound is not None:raise ValueError('운영 확인 카드에는 설계 연결을 지정할 수 없습니다.')
    prompt=_text(raw['prompt'],'선택 질문',1000)
    options=raw['options']
    if not isinstance(options,list) or not 2 <= len(options) <= 6:
        raise ValueError('작업별 선택지는 2~6개여야 합니다.')
    parsed=[];seen=set()
    for option in options:
        if not isinstance(option,dict) or set(option)!={'id','label','description','intent'}:
            raise ValueError('선택지의 이름·설명·처리 유형을 확인하세요.')
        ident=option['id'];intent=option['intent']
        if (not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',ident)
                or ident in seen or intent not in ('confirmed','changes','defer','approve')
                or intent=='approve' and not can_approve):
            raise ValueError('이 카드에서 사용할 수 없는 선택지입니다.')
        seen.add(ident)
        parsed.append({'id':ident,'label':_text(option['label'],'선택지',100),
                       'description':_text(option['description'],'선택 설명',1000),'intent':intent})
    return dict({'prompt':prompt,'options':parsed},**({'document':dict(bound)} if bound is not None else {}))


def _key(kind,ident):
    if kind not in ('inventory','work') or not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',ident):
        raise ValueError('확인할 카드 종류와 작업 ID를 다시 선택하세요.')
    return kind+':'+ident


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()).hexdigest()


def _work_view_signature(row):
    return _digest({key:value for key,value in row.items()
                    if key not in ('native_state','feedback','previous_feedback','interaction')})


def _current(config_path,state,binding_path,kind,ident):
    _key(kind,ident)
    from .app_bridge import bindings,inventory,readonly_snapshot
    config=load(config_path);linked=bindings(binding_path,config)
    if kind=='inventory':
        rows,_=inventory(linked.get('inventory'));matches=[r for r in rows if r['id']==ident]
        if len(matches)!=1:raise ValueError('정본에서 하나의 작업 카드를 확인하지 못했습니다.')
        row=matches[0]
        if not all(isinstance(v,str) and v for v in row.values()):raise ValueError('정본의 작업 정보가 비어 있습니다.')
        raw=row['status'];complete=raw.startswith('완료')
        label='완료' if complete else '확인 필요' if re.search(r'확인|검토|승인|선택|실패|불명|오류',raw) else '진행 중' if '진행' in raw else '대기'
        leader={'label':'팀 총괄','terminal':None,'space':None,
                'reason':'운영 도구의 관리 항목입니다. 제품 팀장 세션에 배정된 제품 업무가 아닙니다.'}
        reason=raw+' · '+row['condition']
        value={'id':ident,'kind':kind,'title':row['name'],'status':raw,'status_label':label,
               'team_label':'팀 총괄','leader':leader,'reason':reason,'condition':row['condition'],
               'prompt':PROMPT}
        basis={'source':str(Path(linked['inventory']).resolve()),'row':row,'leader':leader}
    else:
        data=readonly_snapshot(state);matches=[w for w in data['works'] if w['id']==ident]
        if len(matches)!=1:raise ValueError('GUI 작업 기록에 없는 작업입니다.')
        work=matches[0];row=next(w for w in model(data,config)['rows'] if w['id']==ident)
        setting=linked['teams'][row['team']]
        leader={k:setting.get(k) for k in ('label','terminal','space')}
        condition=' / '.join(row.get('remaining_steps',[])) or row.get('next') or LABELS.get(row['phase'],row['phase'])
        complete=work['phase']=='complete';reason=row.get('reason') or row.get('next') or condition
        question=work.get('question')
        value={'id':ident,'kind':kind,'title':row['title'],'status':row['phase'],
               'status_label':LABELS.get(row['phase'],row['phase']),'team_label':setting.get('label',row['team']),
               'team':row['team'],'work_revision':work['revision'],'_view_signature':_work_view_signature(row),
               'leader':leader,'reason':reason,'condition':condition,
               'prompt':question if isinstance(question,str) and question.strip() else PROMPT}
        try:
            digest=document(config,work)[2]
            changed=(work['phase']=='awaiting_approval' and digest!=work.get('submitted_digest')) or (work.get('approval') and digest!=work['approval']['digest'])
            doc={'state':'invalid' if changed else 'available','digest':digest}
            if changed:doc['reason']='현재 설계가 확인 요청·승인 기록과 다릅니다. 담당자가 설계 버전을 확인해야 합니다.'
        except FileNotFoundError:doc={'state':'missing'}
        except OSError:doc={'state':'unreadable'}
        except ValueError:doc={'state':'invalid'}
        value['document_state']=doc['state']
        if doc.get('reason'):value['document_reason']=doc['reason']
        value['document_revision']=work['revision'];value['document_digest']=doc.get('digest')
        if doc['state']!='available':
            value['reason']+=' · '+doc.get('reason','설계 문서를 읽지 못했습니다. 일반 답변만 기록하며 설계 승인으로 처리하지 않습니다.')
        basis={'source':str(Path(state).resolve()),'work':work,'document':doc,
               'tasks':[t for t in data['tasks'] if t.get('work_id')==ident],'leader':leader}
        value['execution_states']=[t['state'] for t in basis['tasks'] if t.get('role')!='leader'
            and doc.get('digest') is not None and t.get('design_digest')==doc['digest'] and t.get('design_revision')==work['revision']]
    prompts=linked.get('card_prompts',{})
    override=prompts.get(_key(kind,ident)) if isinstance(prompts,dict) else None
    if isinstance(override,str) and override.strip():value['prompt']=override.strip()
    value['needs_decision']=not complete and (label=='확인 필요' if kind=='inventory' else
        value['status'] in ('awaiting_approval','awaiting_review','blocked','inspect','quota','environment'))
    decisions=linked.get('card_decisions',{})
    if not isinstance(decisions,dict):raise ValueError('작업별 선택지 설정을 확인하세요.')
    value['decision']=_decision(decisions.get(_key(kind,ident)),
        kind=='work' and value['status']=='awaiting_approval',
        {'revision':value['document_revision'],'digest':value.get('document_digest'),'phase':value['status']} if kind=='work' else None) if value['needs_decision'] else None
    if value['needs_decision'] and decisions.get(_key(kind,ident)) is not None and value['decision'] is None:
        value['decision_reason']='카드 상태나 설계가 바뀌었거나 문서를 읽을 수 없습니다. 현재 설계에 맞는 선택지를 다시 준비해야 합니다.'
    revision_basis={'kind':kind,'id':ident,'content':basis,'prompt':value['prompt']}
    if value['decision'] is not None:revision_basis['decision']=value['decision']
    value['revision']=_digest(revision_basis)
    value['_revision_basis']=revision_basis
    if kind=='inventory':value['document_revision']=0;value['document_digest']=value['revision']
    value['can_answer']=not complete and linked.get('actions_enabled') is True
    value['choices']=[dict(choice) for choice in CHOICES]
    return value


def _root(state,create=False):
    root=Path(state).expanduser().resolve().parent/'app-feedback'
    if root.is_symlink():raise ValueError('답변 저장 폴더가 바뀌었습니다. 경로를 확인하세요.')
    if not root.exists():
        if not create:return root
        if not Path(state).is_file():raise ValueError('기존 GUI 작업 기록이 필요합니다.')
        root.mkdir(mode=0o700,exist_ok=True)
    if root.is_symlink():raise ValueError('답변 저장 폴더가 바뀌었습니다. 경로를 확인하세요.')
    info=root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:
        raise ValueError('답변 저장 폴더의 소유권과 비공개 권한을 확인하세요.')
    return root


def _open(path,flags):
    fd=os.open(path,flags|os.O_NOFOLLOW,0o600)
    info=os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:
        os.close(fd);raise ValueError('답변 기록 파일의 소유권과 비공개 권한을 확인하세요.')
    return fd


@contextmanager
def _locked(root,write=False):
    path=root/'.lock'
    if not write and not path.exists():
        yield;return
    fd=_open(path,os.O_RDWR|os.O_CREAT if write else os.O_RDONLY)
    try:
        try:fcntl.flock(fd,(fcntl.LOCK_EX if write else fcntl.LOCK_SH)|fcntl.LOCK_NB)
        except BlockingIOError as error:raise ValueError('답변을 저장 중입니다. 잠시 뒤 다시 확인하세요.') from error
        yield
    finally:os.close(fd)


def _records(root):
    path=root/'answers.jsonl';latest={}
    if path.is_symlink():raise ValueError('답변 이력 경로가 바뀌었습니다.')
    if not path.exists():return latest
    with os.fdopen(_open(path,os.O_RDONLY),'r') as source:
        for line in source:
            if not line.endswith('\n'):raise ValueError('답변 이력의 마지막 기록을 확인해야 합니다.')
            try:
                record=json.loads(line);key=_key(record['kind'],record['id']);answer=record['answer']
                if (not isinstance(answer,dict) or answer.get('choice') not in {c['id'] for c in CHOICES}
                        or answer.get('delivery')!='recorded' or not isinstance(answer.get('text'),str)
                        or len(answer['text'])>4000 or not number(answer.get('saved_at'))
                        or not re.fullmatch(r'[0-9a-f]{64}',record.get('revision',''))
                        or answer.get('revision')!=record['revision']):
                    raise ValueError('invalid answer')
            except (ValueError,TypeError,KeyError) as error:
                raise ValueError('답변 이력 형식을 확인해야 합니다.') from error
            latest[key]=record
    return latest


def feedback_summaries(state):
    root=_root(state)
    if not root.exists():return {}
    with _locked(root):return _records(root)


def card_detail(config_path,state,binding_path,kind,ident,*,_include_signature=False):
    value=_current(config_path,state,binding_path,kind,ident)
    saved=feedback_summaries(state).get(_key(kind,ident))
    current=saved is not None and saved['revision']==value['revision']
    value['answer']=saved['answer'] if current else None
    value['previous_answer']=saved['answer'] if saved and not current else None
    if not _include_signature:value.pop('_view_signature',None)
    root=_root(state)
    value['question'],value['previous_question'],value['question_state']=None,None,{'state':'missing','reason':'담당자가 결정 질문을 준비하지 않았습니다.'}
    if root.exists():
        try:
            with _locked(root):record=_questions(root).get(_key(kind,ident))
            value.update(_question_view(value,record))
        except (OSError,ValueError):
            value['question_state']={'state':'invalid','reason':'질문 기록을 읽지 못했습니다. 담당자가 질문 기록을 확인해야 합니다.'}
    try:
        from .app_delivery import card_requests
        value['deliveries']=card_requests(state,_key(kind,ident))
    except (OSError,ValueError):
        value['deliveries']=[];value['delivery_error']='전달 기록을 읽지 못했습니다. 저장된 요청을 확인해야 합니다.'
    apply_interaction(value)
    value.pop('_revision_basis',None)
    return value


def apply_interaction(value):
    """One display decision shared by board and detail; never changes work lifecycle."""
    complete=value['status']=='complete' or value['status_label']=='완료'
    question=value.get('question')
    needs=bool(value['needs_decision'] or question) and not complete
    state='complete' if complete else 'decision' if needs else 'progress' if value['status'] in ('planning','in_progress') or value['status_label']=='진행 중' else 'waiting'
    column='complete' if complete else 'review' if needs else 'progress' if state=='progress' else 'waiting'
    result={'state':state,'column':column,'label':'완료로 기록됨' if complete and value['kind']=='inventory' else value['status_label'],'reason':'','action':'선택하기' if needs else '상세 보기'}
    matches=[]
    for item in value.get('deliveries',[]):
        bound=item.get('binding',{})
        if (bound.get('document_revision'),bound.get('document_digest')) != (value['document_revision'],value.get('document_digest')):continue
        exact_card=bound.get('card_revision')==value['revision']
        same_card=exact_card
        if not same_card and value['kind']=='work':
            # Prove the old full card hash by substituting only its recorded route.
            # A changed prompt, design, task or work state still fails this comparison.
            source=value['_revision_basis'];content=source['content'];destination=item.get('destination') or {}
            leader={**content['leader'],'terminal':destination.get('terminal'),
                    'label':destination.get('label') or content['leader'].get('label'),
                    'space':destination.get('space') or content['leader'].get('space')}
            same_card=bool(destination.get('terminal')) and bound.get('card_revision')==_digest({**source,'content':{**content,'leader':leader}})
        if not same_card:continue
        expected_question=question
        previous=value.get('previous_question')
        if (expected_question is None and previous and (not exact_card or bound.get('question_id') is not None)
                and (previous['document_revision'],previous['document_digest'])==(value['document_revision'],value.get('document_digest'))):
            expected_question=previous
        if expected_question and (bound.get('question_id'),bound.get('question_revision')) != (expected_question['question_id'],expected_question['question_revision']):continue
        matches.append(item)
    if not complete and (value.get('delivery_error') or any('binding' not in d for d in value.get('deliveries',[]))):
        needs=False;result.update(state='unknown',column='review',label='전달 기록 확인 필요',reason=value.get('delivery_error') or '읽지 못한 전달 기록이 있습니다. 재전송 전에 확인해야 합니다.',action='전달 기록 확인')
    elif not complete and matches:
        latest=matches[0];status=latest.get('effective_state',latest['state'])
        if status!='not_sent':
            needs=False
            label={'pending':'전달 준비 중','sending':'전송 확인 중','sent':'담당자 처리 대기','unknown':'전달 결과 확인 필요'}[status]
            reason='전달 확인 · 처리 결과 미기록. 담당자가 요청을 확인하고 작업 기록을 갱신해야 합니다.' if status=='sent' else latest.get('reason') or '같은 요청을 다시 보내지 않고 전달 결과를 확인합니다.'
            result.update(state=status,column='review' if status=='unknown' else 'waiting',label=label,reason=reason,action='전달 기록 확인',request_id=latest['request_id'])
            if latest.get('receipt'):
                response=latest['receipt']
                result.update(state=response['outcome'],column='waiting',label='보류 접수됨' if response['outcome']=='deferred' else '담당자 응답 확인됨',reason=response['summary'],action='응답 기록 보기')
    elif not complete and not question and value.get('answer'):
        needs=False;result.update(state='recorded',column='waiting',label='확인 기록됨 · 총괄 확인 대기',reason='선택은 기록됐습니다. 작업 완료나 설계 승인으로 처리하지 않습니다.',action='기록 보기')
    if result['state']=='progress' and value['status']=='in_progress':
        tasks=value.get('execution_states',[])
        if not any(state in ('claimed','launching','submitted','running') for state in tasks):
            if 'awaiting_review' in tasks:
                result.update(state='review_wait',column='waiting',label='검토 결과 대기',reason='구현 담당 작업은 검토 대기로 기록돼 있습니다. 검토 결과와 남은 검증을 기록해야 합니다.')
            elif 'queued' in tasks:
                result.update(state='assignment_wait',column='waiting',label='담당자 배정 대기',reason='구현 작업은 배정 대기로 기록돼 있습니다. 실행 중인 구현 요청 기록이 없습니다.')
            else:
                result.update(state='execution_unknown',column='review',label='진행 상태 확인 필요',reason='현재 설계에 연결된 실행 중인 구현 요청 기록이 없습니다. 담당자가 배정과 진행 상태를 확인해야 합니다.',action='상태 기록 확인')
    value['needs_decision']=needs
    value['interaction']=result


def _append_record(root,record):
    body=(json.dumps(record,ensure_ascii=False,allow_nan=False)+'\n').encode()
    fd=_open(root/'answers.jsonl',os.O_WRONLY|os.O_CREAT|os.O_APPEND)
    try:
        offset=0
        while offset<len(body):
            count=os.write(fd,body[offset:])
            if not count:raise OSError('answer append stopped')
            offset+=count
        os.fsync(fd)
    finally:os.close(fd)


def _atomic_latest(root,latest):
    if (root/'latest.json').is_symlink():raise ValueError('답변 최신 기록 경로가 바뀌었습니다.')
    fd,name=tempfile.mkstemp(prefix='.latest-',dir=root)
    try:
        with os.fdopen(fd,'w') as out:
            json.dump(latest,out,ensure_ascii=False,allow_nan=False);out.flush();os.fsync(out.fileno())
        os.replace(name,root/'latest.json')
    finally:
        if os.path.exists(name):os.unlink(name)


def card_answer(config_path,state,binding_path,kind,ident,revision,choice,text):
    key=_key(kind,ident)
    if choice not in {c['id'] for c in CHOICES} or not isinstance(text,str) or len(text)>4000:
        raise ValueError('선택 항목과 4000자 이내의 답변을 확인하세요.')
    text=text.strip()
    if any(ord(ch)<32 and ch not in '\n\t' for ch in text) or choice in ('changes','defer') and not text:
        raise ValueError('수정 요청과 나중 확인에는 내용을 적어 주세요.')
    if not isinstance(revision,str) or not re.fullmatch(r'[0-9a-f]{64}',revision):
        raise ValueError('열어 본 카드 버전을 확인하세요.')
    shown=_current(config_path,state,binding_path,kind,ident)
    if shown['revision']!=revision:raise ValueError('카드 내용이 바뀌었습니다. 다시 열고 답변해 주세요.')
    if not shown['can_answer']:raise ValueError('완료된 카드이거나 답변 저장이 비활성화되어 있습니다.')
    root=_root(state,create=True)
    with _locked(root,write=True):
        latest=_records(root);old=latest.get(key)
        cache=root/'latest.json'
        if cache.is_symlink():raise ValueError('답변 최신 기록 경로가 바뀌었습니다.')
        if cache.exists():
            fd=_open(cache,os.O_RDONLY);os.close(fd)
        at=time.time()
        # The source and action setting can change while acquiring the lock.
        current=_current(config_path,state,binding_path,kind,ident)
        if current['revision']!=revision or not current['can_answer']:
            raise ValueError('저장하는 동안 카드나 답변 권한이 바뀌었습니다. 다시 확인하세요.')
        if old and old['revision']==revision and old['answer']['choice']==choice and old['answer']['text']==text:
            try:_atomic_latest(root,latest)
            except (OSError,ValueError) as error:raise ValueError('답변 이력은 저장됐지만 최신 표시를 확인해야 합니다.') from error
            return {'message':MESSAGE,'answer':old['answer']}
        answer={'choice':choice,'choice_label':next(c['label'] for c in CHOICES if c['id']==choice),
                'text':text,'saved_at':at,'delivery':'recorded','revision':revision}
        record={'kind':kind,'id':ident,'revision':revision,'answer':answer}
        try:_append_record(root,record)
        except OSError as error:raise ValueError('답변 저장을 마치지 못했습니다. 이력을 다시 확인하세요.') from error
        latest[key]=record
        try:_atomic_latest(root,latest)
        except (OSError,ValueError) as error:raise ValueError('답변 이력은 저장됐지만 최신 표시를 갱신하지 못했습니다. 카드를 다시 확인하세요.') from error
        return {'message':MESSAGE,'answer':answer}


def decorate_feedback(config_path,state,binding_path,inventory_rows,work_rows):
    """Attach a reply only when it matches the card currently being displayed."""
    coherent=True
    for kind,rows in (('inventory',inventory_rows),('work',work_rows)):
        for row in rows:
            current=card_detail(config_path,state,binding_path,kind,row['id'],_include_signature=True)
            # Concurrent writers may advance state between the snapshot and this read.
            # Do not combine facts from different versions as a successful refresh.
            displayed=(row.get('name')==current['title'] and row.get('status')==current['status'] and row.get('condition')==current['condition']) if kind=='inventory' else (_work_view_signature(row)==current['_view_signature'])
            if not displayed:
                coherent=False
                if current.get('answer') or current.get('previous_answer'):row['previous_feedback']=current.get('answer') or current['previous_answer']
                row['interaction']={'state':'unknown','column':'review','label':'카드 동기화 확인 필요','reason':'조회 중 카드가 바뀌었습니다. 다음 갱신에서 다시 확인합니다.','action':'기록 확인'}
                continue
            if current.get('answer'):row['feedback']=current['answer']
            if current.get('previous_answer'):row['previous_feedback']=current['previous_answer']
            row['interaction']=current['interaction']
            if current.get('delivery_error') or any('binding' not in d for d in current.get('deliveries',[])):coherent=False
    return coherent


# ---- typed decision questions -------------------------------------------------

QUESTION_FILE='questions.jsonl'
OPTION_ID=re.compile(r'[a-z0-9][a-z0-9_-]{0,31}\Z')
QUESTION_ID=re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{5,63}\Z')
REQUEST_ID=re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{7,63}\Z')
SHA=re.compile(r'[0-9a-f]{64}\Z')
QUESTION_KEYS={'schema_version','question_id','question_revision','kind','work_id','card_revision','document_revision',
               'document_digest','prompt','options','other_allowed','author_task_id'}


def _text(value,name,limit,required=True):
    if not isinstance(value,str) or len(value)>limit:raise ValueError(name+'을(를) '+str(limit)+'자 이내 문자열로 입력하세요.')
    value=value.strip()
    if required and not value or any(ord(ch)<32 and ch not in '\n\t' for ch in value):
        raise ValueError(name+'에는 내용을 적어 주세요. 제어 문자는 사용할 수 없습니다.')
    return value


def _options(raw):
    if not isinstance(raw,list) or not 1<=len(raw)<=4:raise ValueError('선택지는 1~4개여야 합니다.')
    seen=set();options=[]
    for item in raw:
        if not isinstance(item,dict) or not set(item)<={'option_id','label','description'}:raise ValueError('선택지 형식을 확인하세요.')
        ident=item.get('option_id')
        if not isinstance(ident,str) or not OPTION_ID.match(ident) or ident=='other' or ident in seen:
            raise ValueError('선택지 ID는 중복 없는 영문 소문자·숫자·-·_ 값이어야 하며 other는 예약값입니다.')
        seen.add(ident);option={'option_id':ident,'label':_text(item.get('label'),'선택지 이름',80)}
        if item.get('description') not in (None,''):option['description']=_text(item['description'],'선택지 설명',300)
        options.append(option)
    return options


def _check_question(q):
    if not isinstance(q,dict) or set(q)!=QUESTION_KEYS or q['schema_version']!=1:raise ValueError('질문 형식을 확인해야 합니다.')
    _key(q['kind'],q['work_id'])
    if (not isinstance(q['question_id'],str) or not QUESTION_ID.match(q['question_id']) or type(q['question_revision']) is not int
            or q['question_revision']<1 or type(q['document_revision']) is not int or q['document_revision']<0
            or not isinstance(q['card_revision'],str) or not SHA.match(q['card_revision'])
            or not isinstance(q['document_digest'],str) or not SHA.match(q['document_digest'])
            or q['other_allowed'] is not True or not isinstance(q['author_task_id'],str) or not q['author_task_id']):
        raise ValueError('질문 형식을 확인해야 합니다.')
    q['prompt']=_text(q['prompt'],'질문',1000);q['options']=_options(q['options'])
    return q


def _questions(root):
    """Latest typed question per card; any malformed or partial line is an inspect error."""
    path=root/QUESTION_FILE;latest={};ids={}
    if path.is_symlink():raise ValueError('질문 기록 경로가 바뀌었습니다.')
    if not path.exists():return latest
    with os.fdopen(_open(path,os.O_RDONLY),'r') as source:
        for line in source:
            if not line.endswith('\n'):raise ValueError('질문 기록의 마지막 줄을 확인해야 합니다.')
            try:
                q=_check_question(json.loads(line));key=_key(q['kind'],q['work_id'])
            except (ValueError,TypeError,KeyError) as error:raise ValueError('질문 기록 형식을 확인해야 합니다.') from error
            old=latest.get(key)
            if ids.setdefault(q['question_id'],key)!=key or q['question_revision']!=(old['question_revision']+1 if old else 1) \
                    or old and old['question_id']!=q['question_id']:
                raise ValueError('질문 기록의 버전 순서를 확인해야 합니다.')
            latest[key]=q
    return latest


def _question_view(card,q):
    """Return the question only while it matches the displayed card and document."""
    if q is None:return {'question':None,'previous_question':None,'question_state':{'state':'missing','reason':'담당자가 결정 질문을 준비하지 않았습니다.'}}
    if card['kind']=='work' and card.get('document_state')!='available':
        return {'question':None,'previous_question':q,'question_state':{'state':'document_unavailable','reason':'설계 문서를 읽지 못해 결정 질문을 사용할 수 없습니다.'}}
    if (q['card_revision'],q['document_revision'],q['document_digest'])!=(card['revision'],card['document_revision'],card['document_digest']):
        return {'question':None,'previous_question':q,'question_state':{'state':'stale','reason':'카드나 문서가 질문 작성 뒤 바뀌었습니다. 담당자가 질문을 새로 준비해야 합니다.'}}
    return {'question':q,'previous_question':None,'question_state':{'state':'ready','reason':''}}


def question_set(config_path,state,binding_path,kind,ident,author,question,expected_revision):
    """Append one typed question version. `author` is a leader identity already verified by the bridge."""
    key=_key(kind,ident)
    if not isinstance(question,dict) or not set(question)<={'question_id','prompt','options'}:
        raise ValueError('질문 파일에는 prompt와 options(선택적으로 question_id)만 넣을 수 있습니다.')
    if type(expected_revision) is not int or expected_revision<0:raise ValueError('기대하는 질문 버전을 확인하세요.')
    prompt=_text(question.get('prompt'),'질문',1000);options=_options(question.get('options'))
    shown=_current(config_path,state,binding_path,kind,ident)
    if kind=='work' and shown.get('document_state')!='available':raise ValueError('설계 문서를 읽을 수 없어 결정 질문을 만들 수 없습니다.')
    if not shown['can_answer']:raise ValueError('완료된 카드이거나 답변 기능이 꺼져 있어 질문을 바꿀 수 없습니다.')
    root=_root(state,create=True)
    with _locked(root,write=True):
        old=_questions(root).get(key);have=old['question_revision'] if old else 0
        if have!=expected_revision:raise ValueError('질문 버전이 바뀌었습니다. 현재 질문을 다시 읽고 요청하세요.')
        qid=question.get('question_id')
        if qid is not None and (not isinstance(qid,str) or not QUESTION_ID.match(qid)):raise ValueError('질문 ID 형식을 확인하세요.')
        if old and qid not in (None,old['question_id']):raise ValueError('이 카드의 질문 ID와 다릅니다.')
        if qid and not old and any(q['question_id']==qid for q in _questions(root).values()):raise ValueError('다른 카드가 이미 쓰는 질문 ID입니다.')
        current=_current(config_path,state,binding_path,kind,ident)
        if (current['revision'],current.get('document_digest'))!=(shown['revision'],shown.get('document_digest')):
            raise ValueError('질문을 쓰는 동안 카드나 문서가 바뀌었습니다. 다시 확인하세요.')
        record={'schema_version':1,'question_id':old['question_id'] if old else qid or 'q-'+uuid.uuid4().hex[:16],
                'question_revision':have+1,'kind':kind,'work_id':ident,'card_revision':shown['revision'],
                'document_revision':shown['document_revision'],'document_digest':shown['document_digest'],
                'prompt':prompt,'options':options,'other_allowed':True,'author_task_id':author['task']}
        if old and all(old[k]==record[k] for k in record if k!='question_revision'):return {'message':'같은 질문이 이미 준비되어 있습니다.','question':old}
        try:
            body=(json.dumps(record,ensure_ascii=False,allow_nan=False)+'\n').encode()
            fd=_open(root/QUESTION_FILE,os.O_WRONLY|os.O_CREAT|os.O_APPEND)
            try:
                done=0
                while done<len(body):
                    count=os.write(fd,body[done:])
                    if not count:raise OSError('question append stopped')
                    done+=count
                os.fsync(fd)
            finally:os.close(fd)
        except OSError as error:raise ValueError('질문을 저장하지 못했습니다. 기록을 확인한 뒤 다시 요청하세요.') from error
        return {'message':'결정 질문 v'+str(record['question_revision'])+'을 준비했습니다. 승인이나 전달은 하지 않았습니다.','question':record}


def _card_send_text(value,name):
    return _text(value,name,4000,required=False) if value not in (None,'') else ''


def validate_send(config_path,state,binding_path,request):
    """Re-check a direct-send request against the card, document, permission and question currently shown."""
    keys={'client_request_id','kind','work_id','card_revision','document_revision','document_digest','question_id',
          'question_revision','option_id','other_text','choice','comment'}
    if not isinstance(request,dict) or not set(request)<=keys:raise ValueError('전송 요청 형식을 확인하세요.')
    rid=request.get('client_request_id')
    if not isinstance(rid,str) or not REQUEST_ID.match(rid):raise ValueError('전송 요청 ID를 확인하세요.')
    kind,ident=request.get('kind'),request.get('work_id');_key(kind,ident)
    shown=_current(config_path,state,binding_path,kind,ident)
    if not shown['can_answer']:raise ValueError('완료된 카드이거나 답변 권한이 꺼져 있어 전송하지 않았습니다.')
    if (request.get('card_revision'),request.get('document_revision'),request.get('document_digest'))!=(shown['revision'],shown['document_revision'],shown['document_digest']):
        raise ValueError('카드나 문서가 바뀌었습니다. 최신 내용을 확인하고 다시 작성하세요. 보내지 않았습니다.')
    other=_card_send_text(request.get('other_text'),'직접 입력');comment=_card_send_text(request.get('comment'),'의견')
    q=None
    payload={'client_request_id':rid,'kind':kind,'work_id':ident,'card_revision':shown['revision'],
             'document_revision':shown['document_revision'],'document_digest':shown['document_digest'],
             'question_id':None,'question_revision':None,'option_id':None,'other_text':other,'choice':None,'comment':comment}
    if request.get('question_id') is not None:
        root=_root(state)
        if not root.exists():raise ValueError('담당자가 결정 질문을 준비하지 않았습니다.')
        with _locked(root):q=_questions(root).get(_key(kind,ident))
        view=_question_view(shown,q)
        if view['question'] is None:raise ValueError(view['question_state']['reason'])
        q=view['question']
        if (request.get('question_id'),request.get('question_revision'))!=(q['question_id'],q['question_revision']):
            raise ValueError('질문이 바뀌었습니다. 새 질문을 확인하고 다시 선택하세요. 번호를 자동으로 옮기지 않았습니다.')
        option=request.get('option_id')
        if option=='other':
            if not other:raise ValueError('직접 입력을 선택하면 내용을 적어야 합니다.')
        elif option in {o['option_id'] for o in q['options']}:
            if other:raise ValueError('선택지와 직접 입력은 하나만 보낼 수 있습니다.')
        else:raise ValueError('현재 질문에 없는 선택지입니다. 최신 질문을 확인하세요.')
        if request.get('choice') is not None:raise ValueError('질문 답에는 일반 의견 유형을 함께 보낼 수 없습니다.')
        payload.update(question_id=q['question_id'],question_revision=q['question_revision'],option_id=option)
    else:
        if request.get('question_revision') is not None or request.get('option_id') is not None or other:
            raise ValueError('질문 없이 선택지나 직접 입력을 보낼 수 없습니다.')
        choice=request.get('choice')
        if choice not in ('changes','defer'):raise ValueError('일반 의견은 수정 필요 또는 나중 확인만 보낼 수 있습니다.')
        if not comment:raise ValueError('수정 필요와 나중 확인에는 내용을 적어 주세요.')
        payload['choice']=choice
    return payload,shown,q
