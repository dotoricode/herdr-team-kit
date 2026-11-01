"""Version-bound card replies; recorded locally, never delivered or approved."""
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

from .config import load
from .dashboard import LABELS, model
from .policy import number
from .workflow import document

CHOICES=({'id':'confirmed','label':'확인했습니다'}, {'id':'changes','label':'수정이 필요합니다'},
         {'id':'defer','label':'나중에 확인합니다'})
MESSAGE='답변을 저장했습니다. 총괄 검토 대기입니다.'
PROMPT='무엇을 확인했고 어떤 조치가 필요한가요?'


def _key(kind,ident):
    if kind not in ('inventory','work') or not isinstance(ident,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',ident):
        raise ValueError('확인할 카드 종류와 작업 ID를 다시 선택하세요.')
    return kind+':'+ident


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()).hexdigest()


def _work_view_signature(row):
    return _digest({key:value for key,value in row.items()
                    if key not in ('native_state','feedback','previous_feedback')})


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
        try:doc={'state':'available','digest':document(config,work)[2]}
        except FileNotFoundError:doc={'state':'missing'}
        except OSError:doc={'state':'unreadable'}
        except ValueError:doc={'state':'invalid'}
        value['document_state']=doc['state']
        if doc['state']!='available':
            value['reason']+=' · 설계 문서를 읽지 못했습니다. 일반 답변만 기록하며 설계 승인으로 처리하지 않습니다.'
        basis={'source':str(Path(state).resolve()),'work':work,'document':doc,
               'tasks':[t for t in data['tasks'] if t.get('work_id')==ident],'leader':leader}
    prompts=linked.get('card_prompts',{})
    override=prompts.get(_key(kind,ident)) if isinstance(prompts,dict) else None
    if isinstance(override,str) and override.strip():value['prompt']=override.strip()
    value['revision']=_digest({'kind':kind,'id':ident,'content':basis,'prompt':value['prompt']})
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


def card_detail(config_path,state,binding_path,kind,ident):
    value=_current(config_path,state,binding_path,kind,ident)
    saved=feedback_summaries(state).get(_key(kind,ident))
    current=saved is not None and saved['revision']==value['revision']
    value['answer']=saved['answer'] if current else None
    value['previous_answer']=saved['answer'] if saved and not current else None
    value.pop('_view_signature',None)
    return value


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
    summaries=feedback_summaries(state)
    for kind,rows in (('inventory',inventory_rows),('work',work_rows)):
        for row in rows:
            saved=summaries.get(_key(kind,row['id']))
            if not saved:continue
            try:current=_current(config_path,state,binding_path,kind,row['id'])
            except (OSError,ValueError):
                row['previous_feedback']=saved['answer'];continue
            displayed=(row.get('name')==current['title'] and row.get('status')==current['status'] and row.get('condition')==current['condition']) if kind=='inventory' else (_work_view_signature(row)==current['_view_signature'])
            field='feedback' if displayed and saved['revision']==current['revision'] else 'previous_feedback'
            row[field]=saved['answer']
