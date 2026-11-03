"""Team-leader pool commands and durable work requests using the official CLI."""
import hashlib
import json
import os
import re
import stat
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .config import load, cached_quota
from .store import Store, ACTIVE
from . import session_pool as pool
from .wake import notify


def folder(state):
    path=Path(state).resolve().parent/'pool-requests'
    path.mkdir(mode=0o700,exist_ok=True)
    if path.is_symlink():raise ValueError('invalid pool request directory')
    return path


def write_request(path,value):
    from .context_handoff import write
    write(path,value)
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


def command(config_path,state,*args):
    return [sys.executable,str(Path(__file__).resolve().parent.parent/'teamkit_cli.py'),
            '--config',str(config_path),'--state',str(state),*args]


def official(config_path,state,*args):
    result=subprocess.run(command(config_path,state,*args),capture_output=True,text=True,timeout=150)
    if result.returncode:raise ValueError('official execution did not confirm; inspect task and native before retry')
    return json.loads(result.stdout)


def prepare(config_path,state):
    config=load(config_path)
    if not pool.enabled(config):raise ValueError('team leader pool disabled')
    from .__main__ import git, instructions_basis
    from .routing import classification
    store=Store(state)
    try:
        for team,setting in config['teams'].items():
            ident=pool.task_id(config,team)
            found=next((t for t in store.list() if t['id']==ident),None)
            if found:
                if not pool.managed(config,found) or Path(found['root']).resolve()!=Path(setting['root']).resolve():
                    raise ValueError('canonical pool task has a different scope')
                continue
            if any(t['team']==team and t['state'] in ACTIVE for t in store.list()):
                raise ValueError('existing team work is active; preserve it before creating a replacement leader')
            try:base,kind=git(setting['root'],'rev-parse','HEAD'),'git'
            except subprocess.CalledProcessError:base,kind=instructions_basis(setting),'instructions'
            goal=('Prepare this App team leader only. Complete the supplied receipt, read the relevant team instructions '
                  'and current durable work records, report actual provider/account and pending approvals, then park. '
                  'Do not start or resume product work, modify product files, approve work, start workers or import TUI work. '
                  'Historical conversations remain preserved. Future assignments use the supplied task and owner.')
            store.create(ident,team,'leader',setting['root'],base,goal,kind,classification('leader','general',[]))
    finally:store.close()
    return pool.prepare(config_path,state)


def refresh_choice(config_path,state,task):
    """Refresh earlier unknown candidates before selecting a lower-priority account."""
    config=load(config_path)
    from .routing import execution_policy,execution_candidates
    from .policy import account_order
    from .context_handoff import refresh_candidates
    role=execution_policy(config,task)
    if role['action']!='dispatch':return
    candidates=execution_candidates(config,task,role,time.time())
    pairs=[(c['provider'],c['account']) for c in candidates] if candidates is not None else [(role['provider'],a) for a in account_order(config,time.time())]
    refresh_candidates(config_path,state,pairs)


def constraint(config, provider=None, account=None):
    if provider is None and account is None:
        return None
    if provider not in config['profiles'] or account not in ('team', 'personal'):
        raise ValueError('both configured provider and account are required for an execution constraint')
    return {'provider': provider, 'account': account}


def plan(config_path,state,team,provider=None,account=None):
    if not Path(state).is_file():raise ValueError('existing pool task store required')
    config=load(config_path)
    from .__main__ import select_execution
    store=Store(state)
    try:
        task=store.get(pool.task_id(config,team))
        requested=constraint(config,provider,account)
        if requested is not None:task={**task,'execution_constraint':requested}
        selection=select_execution(config,store,task,time.time())
    finally:store.close()
    result={'team':team,'task':task['id'],'selection':selection,'state':task['state'],
            'owner':task.get('owner'),'attempt':task.get('attempt_count',0),'terminal':task.get('runtime',{}).get('terminal')}
    if selection['action']!='dispatch':return {**result,'action':'wait','reason':selection.get('reason','usage unavailable')}
    same=all(task.get(k)==selection.get(k) for k in ('provider','account','model','effort'))
    if task.get('owner'):
        action='tell' if same and task['state']=='ready' else 'handoff' if task['state']=='ready' else 'wait'
        return {**result,'action':action,'reason':'current owner remains authoritative until a verified handoff'}
    try:
        slot=pool.preview(config,state,task,selection)
        return {**result,'action':'dispatch','next_session':{k:slot.get(k) for k in ('terminal','generation','provider','account','model','effort')}}
    except ValueError:
        return {**result,'action':'wait','reason':'preferred session is not prepared; account order was not changed'}


def enqueue(config_path,state,team,message_file,provider=None,account=None,work_id=None,completion_file=None,workflow_id=None,priority=0,dependencies=(),resource_paths=()):
    config=load(config_path)
    if not pool.enabled(config):raise ValueError('team leader pool disabled')
    requested=constraint(config,provider,account)
    # A work request is an explicit caller message, not an approval generated by the pool.
    source=Path(message_file)
    if source.is_symlink() or not source.is_file() or source.stat().st_size>65536:
        raise ValueError('bounded local message file required')
    text=source.read_text()
    if not text.strip():raise ValueError('empty assignment')
    task=pool.task_id(config,team)
    from . import queue_policy
    workflow_ref=queue_policy.capture_work(config,state,team,workflow_id)
    claimed_resources=queue_policy.resources(config,team,workflow_ref,resource_paths)
    if type(priority) is not int or not 0<=priority<=9:
        raise ValueError('priority must be an integer in 0..9')
    from .context_handoff import JOB
    if (not isinstance(dependencies,(tuple,list)) or len(dependencies)>64
            or any(not isinstance(d,str) or not JOB.fullmatch(d) for d in dependencies)
            or len(set(dependencies))!=len(dependencies)):
        raise ValueError('dependencies require distinct queue request identities')
    dependencies=sorted(dependencies)
    from .context_handoff import locked
    with locked(folder(state)/'requests'):
        digest=hashlib.sha256(text.encode()).hexdigest()
        work_id = work_id or workflow_id or ('message-' + digest)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}',work_id):
            raise ValueError('bounded stable work identity required')
        completion = None
        if completion_file:
            evidence = Path(completion_file)
            if evidence.is_symlink() or not evidence.is_file() or evidence.stat().st_size>16384:
                raise ValueError('bounded completion requirements file required')
            completion = evidence.read_text()
            if not completion.strip():raise ValueError('empty completion requirements')
        previous=requests(state)
        for old in previous:
            old_key=old.get('work_id') or ('message-'+old['digest'])
            if old.get('task')==task and old_key==work_id:
                old_resources=old.get('resources',queue_policy.resources(config,team,old.get('workflow_ref')))
                if (old.get('digest')==digest and old.get('execution_constraint')==requested
                        and old.get('completion_requirements')==completion
                        and old.get('workflow_ref')==workflow_ref and old.get('priority',0)==priority
                        and old.get('dependencies',[])==dependencies and old_resources==claimed_resources):
                    notify(state)
                    return {k:v for k,v in old.items() if k!='message'}
                raise ValueError('work identity already has different immutable request content')
        if any(d not in {j['id'] for j in previous} for d in dependencies):
            raise ValueError('dependency must name an existing immutable queue request')
        sequence=max((j.get('sequence',0) for j in previous),default=0)+1
        value={'id':uuid.uuid4().hex,'team':team,'task':task,'message':text,'digest':digest,
               'work_id':work_id,'sequence':sequence,'completion_requirements':completion,
               'workflow_ref':workflow_ref,'priority':priority,'dependencies':dependencies,'resources':claimed_resources,
               'execution_constraint':requested,
               'phase':'pending','at':time.time(),'updated_at':time.time()}
        write_request(folder(state)/(value['id']+'.json'),value)
    notify(state)
    return {k:v for k,v in value.items() if k!='message'}


def requests(state):
    from .context_handoff import read, JOB
    root=Path(state).resolve().parent/'pool-requests'
    if not root.exists():return []
    if root.is_symlink():raise ValueError('invalid pool request directory')
    return sorted((read(p) for p in root.glob('*.json') if JOB.fullmatch(p.stem)),
                  key=lambda j:(j.get('sequence',0),j.get('at',0),j['id']))


def idle_native(task,config,state):
    """Identity failures propagate; a verified busy/dirty terminal only waits."""
    from .core_runtime import CoreRuntime
    from . import context_handoff as context
    runtime=CoreRuntime(task)
    value=runtime._native(task['runtime']['name'])
    if (value.get('status') or {}).get('state') not in ('idle','done'):return None
    value=runtime._native(task['runtime']['name'],ready=True)
    target=next(t for t in context.targets(config,state) if t['id']==task['id'])
    return value if context.clean_prompt(value,target) else None


def advance(config_path,state,ident):
    from . import context_handoff as context
    if not context.JOB.fullmatch(ident):raise ValueError('invalid assignment identity')
    job=context.read(folder(state)/(ident+'.json'))
    if not job:return
    # Serialize each team's consumer, independently of the short enqueue lock.
    key=hashlib.sha256(job['team'].encode()).hexdigest()
    with context.locked(folder(state)/('team-'+key)):
        return _advance(config_path,state,ident)


def _advance(config_path,state,ident):
    from . import context_handoff as context
    path=folder(state)/(ident+'.json')
    if not context.JOB.fullmatch(ident):raise ValueError('invalid assignment identity')
    with context.locked(path):
        job=context.read(path)
        if not job or job['phase'] in ('done','received','completed','deferred','uncertain','cancelled'):return
        config=load(config_path)
        if not pool.enabled(config):return
        from . import queue_policy
        all_jobs=requests(state)
        team_jobs=[j for j in all_jobs if j['team']==job['team']]
        active=[j for j in team_jobs if j['id']!=ident and j['phase'] not in ('pending','completed','deferred','cancelled')]
        waiting=sorted((j for j in team_jobs if j['phase']=='pending'),
                       key=lambda j:(-j.get('priority',0),j.get('sequence',0),j.get('at',0),j['id']))
        if job['phase']=='pending':
            reason='앞선 요청의 업무 완료·보류 인계를 기다립니다.' if active else queue_policy.admission(config,state,job,all_jobs)
            if not reason:
                eligible=next((j for j in waiting if queue_policy.admission(config,state,j,all_jobs) is None),None)
                if eligible and eligible['id']!=ident:reason='먼저 실행 가능한 우선 요청을 기다립니다.'
            if reason:
                job.update(reason=reason,updated_at=time.time())
                write_request(path,job);return
        def save(phase,reason):
            job.update(phase=phase,reason=reason,updated_at=time.time());write_request(path,job)
        try:
            if job['task']!=pool.task_id(config,job['team']):return save('uncertain','담당 설정이 변경됐습니다. 요청을 보존했습니다.')
            if job['phase'] in ('sending','dispatching'):
                return save('uncertain','전송·배정 결과를 확인해야 합니다. 반복 실행하지 않습니다.')
            from . import queue_policy
            reason=queue_policy.admission(config,state,job,requests(state))
            if reason:return save('pending',reason)
            if 'execution_constraint' in job:
                store=Store(state)
                try:
                    with store.transaction():
                        task=store.get(job['task'])
                        requested=job['execution_constraint']
                        if requested is not None:
                            requested=constraint(config,**requested)
                        if task.get('execution_constraint')!=requested:
                            if task['state'] not in ('ready','queued','handed_off'):
                                return save('pending','기존 담당 실행이 안전한 경계에 도달하기를 기다립니다.')
                            if task.get('owner') and idle_native(task,config,state) is None:
                                return save('pending','기존 담당 작업·입력을 보존하고 안전한 경계를 기다립니다.')
                            if requested is None:task.pop('execution_constraint',None)
                            else:task['execution_constraint']=requested
                            store._save(task,'execution_constraint',{'request':job['id'],'selection':requested})
                finally:store.close()
            if job['phase']=='waiting_handoff':
                handoff=context.read(context.job_path(state,job['handoff_job']))
                if not handoff or handoff['phase'] in ('uncertain','cancelled'):
                    return save('uncertain','기존 담당 인계 확인이 필요합니다. 요청은 보존했습니다.')
                if handoff['phase']!='done':return
                save('pending','새 담당 수신을 확인했습니다.')
            if time.time()-job.get('last_refresh',0)>=300:
                store=Store(state)
                try:task=store.get(job['task'])
                finally:store.close()
                refresh_choice(config_path,state,task)
                job['last_refresh']=time.time();write_request(path,job)
            route=plan(config_path,state,job['team'])
            if route['action']=='wait':return save('pending',route['reason'])
            if route['action']=='handoff':
                target=next(t for t in context.targets(config,state) if t['id']==job['task'])
                handoff=context.request(config,state,target,None,policy_rotation=True,pool_request=job['id'])
                if handoff is None:
                    handoff=next(j for j in context.jobs(state) if j['target']['id']==job['task'] and j['target']['owner']==route['owner'] and j['phase'] not in context.FINISHED)
                job['handoff_job']=handoff['id'];return save('waiting_handoff','사용량·요일 순서에 맞는 다음 팀장으로 인계 중입니다.')
            with context.locked(context.directory(state)/'model-start'):
                target=next((t for t in context.targets(config,state) if t['id']==job['task']),None)
                if not context.capacity_available(state,target):return save('pending','동시 실행 여유를 기다립니다.')
                reason=queue_policy.admission(config,state,job,requests(state))
                if reason:return save('pending',reason)
                if route['action']=='dispatch':
                    save('dispatching','준비된 세션에 공식 배정합니다.')
                else:
                    store=Store(state)
                    try:task=store.get(job['task'])
                    finally:store.close()
                    if idle_native(task,config,state) is None:
                        return save('pending','현재 작업·입력 초안을 보존하고 기다립니다.')
                    latest=plan(config_path,state,job['team'])
                    keys=('action','owner','attempt','selection')
                    if any(latest.get(k)!=route.get(k) for k in keys):
                        return save('pending','사용량·선택·소유권이 바뀌어 전달 전에 다시 확인합니다.')
                    message=path.with_suffix('.txt')
                    job['delivery_id']=uuid.uuid4().hex
                    text=('QUEUE REQUEST ID: '+job['id']+'\nWORK ID: '+job.get('work_id',job['id'])+'\n'
                          'DELIVERY ID: '+job['delivery_id']+'\n'
                          'Receipt is not work completion. Save result evidence, submit it, then park. Only verified reconciliation releases the next work.\n')
                    if job.get('completion_requirements'):
                        text+='COMPLETION REQUIREMENTS:\n'+job['completion_requirements']+'\n'
                    import shlex
                    submission=command(config_path,state,'pool','submit','--request',job['id'],
                        '--expected-owner',route['owner'],'--expected-attempt',str(route['attempt']),
                        '--delivery-id',job['delivery_id'],'--evidence-file','/absolute/path/to/result.json')
                    text+=('Result JSON requires request_id, work_id, owner, attempt, delivery_id, writer_stopped=true, '
                           'a nonempty verification string array and artifacts [{"path":"absolute result path","sha256":"file SHA-256"}]. '
                           'Use the exact identities above. Optional outcome is completed or deferred; deferred requires next. '
                           'Never claim independent review from your own flag. Additional plain-text completion conditions remain waiting for an applicable verifier.\n'
                           'Submit before the supplied park command, replacing only the example evidence path:\n'+shlex.join(submission)+'\n')
                    from .protocol import leader_guidance
                    message.write_text(text+leader_guidance('leader')+'\n'+job['message'])
                    message.chmod(0o600)
                    job['delivery_message_sha256']=hashlib.sha256(message.read_bytes()).hexdigest()
                    job['receiver']={'terminal':task['runtime']['terminal'],'owner':route['owner'],
                                     'attempt':route['attempt'],'native_pid':task['runtime']['native_pid']}
                    save('sending','선택된 팀장에게 요청을 전달합니다.')
            if route['action']=='dispatch':
                result=official(config_path,state,'dispatch',job['task'],'--expected-attempt',str(route['attempt']))
                if result.get('state')!='ready':return save('uncertain','새 담당의 수신·대기를 확인해야 합니다.')
                return save('pending','새 담당 준비가 끝났습니다. 다음 경계에서 요청을 전달합니다.')
            result=official(config_path,state,'tell',job['task'],'--message-file',str(message),
                '--expected-owner',route['owner'],'--expected-attempt',str(route['attempt']),
                '--accept-only','--delivery-id',job['delivery_id'])
            receiver=job['receiver']
            if (result.get('owner')!=receiver['owner'] or result.get('attempt_count')!=receiver['attempt']
                    or result.get('runtime',{}).get('terminal')!=receiver['terminal']
                    or result.get('runtime',{}).get('native_pid')!=receiver['native_pid']):
                raise ValueError('returned delivery owner or native identity differs from the recorded receiver')
            delivery=result.get('delivery')
            if delivery is not None and (delivery.get('accepted') is not True
                    or delivery.get('delivery_id')!=job['delivery_id']
                    or delivery.get('terminal')!=receiver['terminal'] or delivery.get('native_pid')!=receiver['native_pid']):
                raise ValueError('accepted input receipt differs from the recorded delivery')
            if delivery is None and result.get('state')!='ready':
                raise ValueError('input acceptance has no receipt')
            if delivery is not None:job['delivery_receipt']=delivery
            save('received','요청 수신을 확인했습니다. 업무 완료·보류 인계 증거를 기다립니다.')
        except Exception as error:
            job['error_type']=type(error).__name__
            job['error_stage']=job['phase']
            job['error_code']='official_execution_unconfirmed' if job['phase'] in ('sending','dispatching') else 'preparation_failed'
            save('uncertain','실행 결과를 확인해 주세요. 기존 기록과 요청을 보존했습니다.')


def _validate_resolution(job,evidence,owner,attempt,outcome):
    if not evidence or evidence.get('writer_stopped') is not True:
        raise ValueError('explicit writer-stopped evidence required')
    verification=evidence.get('verification')
    if not isinstance(verification,list) or not verification or any(not isinstance(v,str) or not v.strip() for v in verification):
        raise ValueError('nonempty verification evidence required')
    receiver=job.get('receiver') or {}
    if (not owner or type(attempt) is not int or receiver.get('owner')!=owner or receiver.get('attempt')!=attempt
            or evidence.get('owner')!=owner or evidence.get('attempt')!=attempt
            or evidence.get('work_id')!=job.get('work_id',job['id'])):
        raise ValueError('completion evidence must match the received work and owner attempt')
    if job.get('completion_requirements') and evidence.get('completion_requirements')!=job['completion_requirements']:
        raise ValueError('verification must explicitly cover the fixed completion requirements')
    if outcome=='deferred' and (not isinstance(evidence.get('next'),str) or not evidence['next'].strip()):
        raise ValueError('deferred work requires an explicit handoff next step')


def _file_proof(path):
    path=Path(path)
    if not path.is_absolute():raise ValueError('absolute result artifact path required')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as source:
        info=os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0<info.st_size<=64*1024*1024:
            raise ValueError('nonempty bounded regular result artifact required')
        return hashlib.file_digest(source,'sha256').hexdigest()


def _validate_artifacts(evidence):
    artifacts=evidence.get('artifacts')
    if not isinstance(artifacts,list) or not 0<len(artifacts)<=32:
        raise ValueError('nonempty result artifacts with SHA-256 are required')
    for item in artifacts:
        if (not isinstance(item,dict) or set(item)!={'path','sha256'}
                or not isinstance(item.get('path'),str)
                or not isinstance(item.get('sha256'),str) or not re.fullmatch(r'[a-f0-9]{64}',item['sha256'])
                or _file_proof(item.get('path'))!=item['sha256']):
            raise ValueError('result artifact is missing or changed after verification')


def _result_file(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as source:
        info=os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0<info.st_size<=256*1024:
            raise ValueError('bounded nonempty JSON result evidence required')
        raw=source.read(256*1024+1)
    if len(raw)>256*1024:raise ValueError('result evidence grew during read')
    value=json.loads(raw)
    if not isinstance(value,dict):raise ValueError('result evidence must be a JSON object')
    return value,hashlib.sha256(raw).hexdigest()


def submit(config_path,state,ident,evidence_file,owner,attempt,delivery_id):
    """Persist a result candidate; submission never declares the work complete."""
    from . import context_handoff as context
    from .core_runtime import CoreRuntime,require_managed
    if (not isinstance(ident,str) or not context.JOB.fullmatch(ident)
            or not isinstance(delivery_id,str) or not context.JOB.fullmatch(delivery_id)):
        raise ValueError('request and execution delivery identities required')
    source=Path(evidence_file).absolute()
    evidence,digest=_result_file(source)
    outcome=(evidence or {}).get('outcome','completed')
    if outcome not in ('completed','deferred'):raise ValueError('completed or deferred result required')
    path=folder(state)/(ident+'.json');initial=context.read(path)
    if not initial:raise ValueError('existing work request required')
    key=hashlib.sha256(initial['team'].encode()).hexdigest()
    with context.locked(folder(state)/('team-'+key)),context.locked(path):
        job=context.read(path)
        if job.get('phase') not in ('received','done'):raise ValueError('confirmed input acceptance required before result submission')
        if (job.get('delivery_id')!=delivery_id or evidence.get('delivery_id')!=delivery_id
                or evidence.get('request_id')!=ident):
            raise ValueError('result submission execution identity differs from the current request')
        _validate_resolution(job,evidence,owner,attempt,outcome)
        _validate_artifacts(evidence)
        receiver=job.get('receiver') or {}
        if require_managed()!=receiver.get('terminal'):raise ValueError('only the received execution terminal may submit its result')
        store=Store(state)
        try:task=store.get(job['task'])
        finally:store.close()
        receipt=task.get('runtime',{}).get('last_delivery') or {}
        if (task.get('owner')!=owner or task.get('attempt_count')!=attempt
                or task.get('state') not in ('running','ready') or receipt.get('delivery_id')!=delivery_id
                or receipt.get('phase')!='accepted' or receipt.get('accepted') is not True
                or receipt.get('message_sha256')!=job.get('delivery_message_sha256')
                or any(receipt.get(k)!=receiver.get(k) for k in ('terminal','native_pid','owner','attempt'))
                or task.get('runtime',{}).get('native_pid')!=receiver.get('native_pid')):
            raise ValueError('current receipted execution differs from result submission')
        CoreRuntime(task)._native(task['runtime']['name'])
        candidate={'source':str(source),'sha256':digest,'owner':owner,'attempt':attempt,
                   'delivery_id':delivery_id,'outcome':outcome}
        previous=job.get('submission')
        if previous:
            if any(previous.get(k)!=v for k,v in candidate.items()):raise ValueError('result candidate already exists with different immutable evidence')
            notify(state)
            return {k:v for k,v in job.items() if k!='message'}
        snapshot=folder(state)/(ident+'.result-'+digest+'.json')
        context.write(snapshot,evidence)
        candidate.update(snapshot=str(snapshot),snapshot_sha256=_file_proof(snapshot),at=time.time())
        job.update(submission=candidate,updated_at=time.time(),reason='결과 제출을 저장했습니다. 실제 대기·쓰기 중단과 검증을 대조합니다.')
        write_request(path,job)
    notify(state)
    return {k:v for k,v in job.items() if k!='message'}


def resolve(config_path,state,ident,outcome,evidence_file,owner,attempt):
    from . import context_handoff as context
    if not isinstance(ident,str) or not context.JOB.fullmatch(ident) or outcome not in ('completed','deferred'):
        raise ValueError('request identity and completed/deferred outcome required')
    if not evidence_file or not owner or type(attempt) is not int:
        raise ValueError('evidence file and expected owner attempt required')
    source=Path(evidence_file)
    evidence=context.read(source)
    path=folder(state)/(ident+'.json')
    initial=context.read(path)
    if not initial:raise ValueError('existing work request required')
    key=hashlib.sha256(initial['team'].encode()).hexdigest()
    with context.locked(folder(state)/('team-'+key)),context.locked(path):
        job=context.read(path)
        if job['phase'] not in ('received','done'):
            raise ValueError('only a confirmed received request may be resolved')
        receiver=job.get('receiver') or {}
        _validate_resolution(job,evidence,owner,attempt,outcome)
        config=load(config_path)
        if outcome=='completed' and job.get('workflow_ref'):
            from .queue_policy import completion
            proof=completion(config,state,job)
            if proof is None:raise ValueError('linked workflow has not passed its official completion gate')
            evidence={**evidence,'workflow_completion':proof}
        store=Store(state)
        try:task=store.get(job['task'])
        finally:store.close()
        if (task['state']!='ready' or task.get('owner')!=owner or task.get('attempt_count')!=attempt
                or task.get('runtime',{}).get('terminal')!=receiver.get('terminal')
                or (job.get('delivery_id') or job.get('receiver_lineage'))
                   and task.get('runtime',{}).get('native_pid')!=receiver.get('native_pid')):
            raise ValueError('current ready owner must match the received work')
        if idle_native(task,config,state) is None:raise ValueError('current work is still active or has unsubmitted input')
        job.update(phase=outcome,work_resolution=evidence,updated_at=time.time(),
                   reason='업무 완료 증거를 확인했습니다.' if outcome=='completed' else '보류 인계 증거를 확인했습니다.')
        write_request(path,job)
        notify(state)
        return {k:v for k,v in job.items() if k!='message'}


def rebind_receiver(config_path,state,ident,evidence_file,owner,attempt,operator_approved=False):
    """Repair only a proven legacy session resume; never change work resolution."""
    from . import context_handoff as context
    from .core_runtime import CoreRuntime,require_managed,_call,_values
    require_managed()
    if (operator_approved is not True or not isinstance(ident,str) or not context.JOB.fullmatch(ident)
            or not isinstance(owner,str) or not owner or type(attempt) is not int or attempt<1):
        raise ValueError('approved operator reconciliation and exact request owner/attempt required')
    evidence_path=Path(evidence_file).absolute()
    evidence,evidence_digest=_result_file(evidence_path)
    if (not isinstance(evidence.get('operator_reference'),str) or not evidence['operator_reference'].strip()
            or not isinstance(evidence.get('verification'),list) or not evidence['verification']
            or any(not isinstance(x,str) or not x.strip() for x in evidence['verification'])
            or type(evidence.get('lineage_event_seq')) is not int or evidence['lineage_event_seq']<1
            or not isinstance(evidence.get('request_record_sha256'),str)
            or not re.fullmatch(r'[a-f0-9]{64}',evidence['request_record_sha256'])):
        raise ValueError('operator reference, pinned request hash and ledger lineage evidence required')
    path=folder(state)/(ident+'.json');initial=context.read(path)
    if not initial:raise ValueError('existing legacy work request required')
    key=hashlib.sha256(initial['team'].encode()).hexdigest()
    with context.locked(folder(state)/('team-'+key)),context.locked(path):
        job=context.read(path)
        if (job.get('phase') not in ('received','done') or job.get('delivery_id')
                or job.get('team')!=initial.get('team')):
            raise ValueError('only legacy confirmed received/done requests may rebind their receiver')
        history=job.get('receiver_lineage',[])
        if not isinstance(history,list):raise ValueError('receiver lineage history is invalid')
        previous=history[-1] if history and history[-1].get('evidence_sha256')==evidence_digest else None
        receiver=previous['previous_receiver'] if previous else job.get('receiver') or {}
        if (receiver.get('owner')!=owner or receiver.get('attempt')!=attempt
                or evidence.get('request_id')!=ident or evidence.get('task')!=job.get('task')
                or evidence.get('owner')!=owner or type(evidence.get('attempt')) is not int or evidence['attempt']!=attempt
                or evidence.get('from_terminal')!=receiver.get('terminal')):
            raise ValueError('legacy receiver or evidence owner/attempt differs')
        expected_hash=previous['original_request_sha256'] if previous else hashlib.sha256(path.read_bytes()).hexdigest()
        if evidence['request_record_sha256']!=expected_hash:
            raise ValueError('legacy request changed after operator inspection')
        config=load(config_path)
        store=Store(state)
        try:
            task=store.get(job['task'])
            events=store.events(job['task'])
        finally:store.close()
        runtime=task.get('runtime') or {}
        if (config.get('runtime_backend')!='core' or task.get('state')!='ready'
                or task.get('role')!='leader' or task.get('team')!=job['team']
                or task.get('owner')!=owner or task.get('attempt_count')!=attempt
                or evidence.get('to_terminal')!=runtime.get('terminal')
                or evidence.get('from_terminal')==runtime.get('terminal')):
            raise ValueError('current ready execution is not the same resumed owner/attempt')
        session=evidence.get('session_id')
        recorded=[value for value in (runtime.get('resumed_session'),runtime.get('context_session_id'),
                   (runtime.get('reboot_restore')or{}).get('session_id')) if value]
        if not isinstance(session,str) or not recorded or any(value!=session for value in recorded):
            raise ValueError('current recorded native session differs from the operator lineage')
        event=next((e for e in events if e['seq']==evidence['lineage_event_seq']),None)
        detail=(event or {}).get('detail') or {}
        if (not event or event['event']!='native-session-resumed' or event['at']<job['at']
                or detail.get('from_terminal')!=receiver['terminal']
                or detail.get('to_terminal')!=runtime['terminal'] or detail.get('session')!=session
                or detail.get('owner_preserved') is not True or detail.get('state_preserved') is not True):
            raise ValueError('matching owner-preserving native session resume event is required')
        claims=[e for e in events if e['event']=='claimed' and e['seq']<event['seq']]
        if (len(claims)!=attempt or not claims or claims[-1]['detail'].get('owner')!=owner
                or claims[-1]['at']>job['at']):
            raise ValueError('lineage event is outside the original execution attempt')
        old=_call('get',receiver['terminal'])
        old_labels=old.get('labels') or {}
        if (old.get('state')!='finished' or old.get('foreground') is not None
                or old_labels.get('teamkit.owner')!=owner or old_labels.get('teamkit.task')!=job['task']):
            raise ValueError('the original owned terminal must be proven finished before receiver rebind')
        native=idle_native(task,config,state)
        if native is None:raise ValueError('resumed native is working or has an unknown input draft')
        argv=(native.get('foreground')or{}).get('argv') or []
        if task['provider']=='claude':sessions=_values(argv,'--resume')
        else:
            positions=[i for i,value in enumerate(argv[:-1]) if value=='resume']
            sessions=[argv[i+1] for i in positions]
        if sessions!=[session]:raise ValueError('actual native argv does not resume the pinned conversation')
        CoreRuntime(task)._native(runtime['name'],ready=True)
        updated_receiver={**receiver,'terminal':runtime['terminal'],'native_pid':runtime['native_pid']}
        if previous:
            if job.get('receiver')!=updated_receiver:raise ValueError('recorded receiver changed after the earlier reconciliation')
            if _file_proof(previous['original_request_file'])!=previous['original_request_sha256']:
                raise ValueError('preserved original request bytes changed')
            return {k:v for k,v in job.items() if k!='message'}
        original=path.read_bytes()
        if hashlib.sha256(original).hexdigest()!=expected_hash:raise ValueError('legacy request changed during lineage verification')
        backup=folder(state)/(ident+'.receiver-before-'+expected_hash+'.json')
        try:fd=os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        except FileExistsError:
            if _file_proof(backup)!=expected_hash:raise ValueError('existing original-request backup differs')
        else:
            with os.fdopen(fd,'wb') as output:output.write(original);output.flush();os.fsync(output.fileno())
        audit={'at':time.time(),'operator_reference':evidence['operator_reference'],
               'evidence_file':str(evidence_path),'evidence_sha256':evidence_digest,
               'original_request_file':str(backup),'original_request_sha256':expected_hash,
               'previous_receiver':receiver,'receiver':updated_receiver,'session_id':session,
               'lineage_event_seq':event['seq'],'lineage_event':event,
               'previous_phase':job['phase'],'previous_reason':job.get('reason'),
               'verification':evidence['verification']}
        job.update(receiver=updated_receiver,receiver_lineage=[*history,audit],updated_at=time.time(),
                   reason='동일 담당·실행 시도의 대화 재개 계보를 확인했습니다. 업무 완료와 검증은 별도 확인합니다.')
        write_request(path,job)
    notify(state)
    return {k:v for k,v in job.items() if k!='message'}


def _recover_delivery(config_path,state,ident):
    """Adopt only this execution's durable accepted receipt; never resend input."""
    from . import context_handoff as context
    from .core_runtime import CoreRuntime
    path=folder(state)/(ident+'.json');initial=context.read(path)
    if not initial or not isinstance(initial.get('delivery_id'),str) or not context.JOB.fullmatch(initial['delivery_id']):return
    key=hashlib.sha256(initial['team'].encode()).hexdigest()
    with context.locked(folder(state)/('team-'+key)),context.locked(path):
        job=context.read(path)
        if job.get('phase') not in ('sending','uncertain'):return
        receiver=job.get('receiver') or {}
        store=Store(state)
        try:task=store.get(job['task'])
        finally:store.close()
        runtime=task.get('runtime') or {};receipt=runtime.get('last_delivery') or {}
        if (receipt.get('phase')!='accepted' or receipt.get('accepted') is not True
                or receipt.get('delivery_id')!=job['delivery_id']
                or not job.get('delivery_message_sha256')
                or receipt.get('message_sha256')!=job['delivery_message_sha256']
                or any(receipt.get(k)!=receiver.get(k) for k in ('terminal','native_pid','owner','attempt'))
                or task.get('owner')!=receiver.get('owner') or task.get('attempt_count')!=receiver.get('attempt')
                or runtime.get('terminal')!=receiver.get('terminal') or runtime.get('native_pid')!=receiver.get('native_pid')
                or task.get('state') not in ('running','ready')):
            return
        CoreRuntime(task)._native(runtime['name'])
        job.update(phase='received',delivery_receipt=receipt,
                   transport_recovery={'previous_phase':job['phase'],'at':time.time()},updated_at=time.time(),
                   reason='같은 실행의 저장된 입력 수락과 실제 세션 신원을 대조했습니다. 업무 완료는 별도 확인합니다.')
        write_request(path,job)


def reconcile(config_path,state,ident):
    """Reconcile accepted input and submitted proof without inventing completion."""
    from . import context_handoff as context
    from .queue_policy import completion
    from .runtime import RuntimeError as RuntimeUnavailable
    if not isinstance(ident,str) or not context.JOB.fullmatch(ident):raise ValueError('valid queue request identity required')
    job=context.read(folder(state)/(ident+'.json'))
    if not job:return
    try:
        if job['phase'] in ('sending','uncertain'):
            _recover_delivery(config_path,state,ident)
            job=context.read(folder(state)/(ident+'.json'))
        if job['phase'] not in ('received','done'):return
        candidate=job.get('submission')
        if candidate:
            if (candidate.get('delivery_id')!=job.get('delivery_id')
                    or _file_proof(candidate['source'])!=candidate['sha256']
                    or _file_proof(candidate['snapshot'])!=candidate['snapshot_sha256']):
                raise ValueError('submitted result source or snapshot changed')
            evidence=context.read(Path(candidate['snapshot']))
            if evidence.get('request_id')!=ident or evidence.get('delivery_id')!=job.get('delivery_id'):
                raise ValueError('submitted result identity changed')
            _validate_artifacts(evidence)
            if job.get('completion_requirements'):
                path=folder(state)/(ident+'.json')
                with context.locked(path):
                    current=context.read(path)
                    if current.get('phase') in ('received','done'):
                        current.update(reason='추가 완료 조건은 일반 문장만으로 자동 검증할 수 없습니다. 적용 가능한 검증 근거 확인을 기다립니다.',updated_at=time.time())
                        write_request(path,current)
                return
            return resolve(config_path,state,ident,candidate['outcome'],candidate['snapshot'],candidate['owner'],candidate['attempt'])
        if not job.get('workflow_ref'):return
        proof=completion(load(config_path),state,job)
        if proof is None:return
        if job.get('completion_requirements'):
            path=folder(state)/(ident+'.json')
            with context.locked(path):
                current=context.read(path)
                if current and current['phase'] in ('received','done') and current.get('workflow_ref')==job['workflow_ref']:
                    reason='공식 업무 완료는 확인됐지만 추가 완료 조건의 별도 검증 근거가 필요합니다.'
                    if current.get('reason')!=reason:
                        current.update(reason=reason,updated_at=time.time())
                        write_request(path,current)
            return
        receiver=job.get('receiver') or {}
        evidence={'work_id':job['work_id'],'owner':receiver.get('owner'),'attempt':receiver.get('attempt'),
                  'writer_stopped':True,'verification':proof['verification']}
        path=folder(state)/(ident+'.workflow-proof.json')
        context.write(path,evidence)
        return resolve(config_path,state,ident,'completed',path,receiver.get('owner'),receiver.get('attempt'))
    except (ValueError,KeyError,OSError,RuntimeUnavailable) as error:
        # Keep received ownership and its evidence intact for a manager to inspect.
        path=folder(state)/(ident+'.json')
        with context.locked(path):
            current=context.read(path)
            if current and current.get('submission') and current.get('phase') in ('received','done'):
                detail={'type':type(error).__name__,'reason':str(error)}
                if current.get('verification_error')!=detail:
                    current.update(verification_error=detail,reason='제출 결과의 실행 신원·파일·검증을 확인해야 합니다.',updated_at=time.time())
                    write_request(path,current)
        return


def handle(args):
    config=load(args.config)
    if args.action=='workspace-status':
        from .workspace_sessions import discover
        return {'team':args.team,'sessions':discover(config,args.state,args.team)}
    if args.action=='status':
        from .policy import eligible
        from .workflow import snapshot
        from types import SimpleNamespace
        data=snapshot(args.state)
        store=SimpleNamespace(path=Path(args.state),quota=data['quotas'].get)
        accounts=[]
        for provider in config['profiles']:
            for account in ('team','personal'):
                quota=cached_quota(config,store,provider,account) or {}
                row={k:quota.get(k) for k in ('source','observed_at','five_hour','weekly','unavailable','ordinary_usage_allowed') if k in quota}
                row.update(provider=provider,account=account,eligible=eligible(quota,time.time(),config['quota_ttl_seconds']))
                accounts.append(row)
        return {**pool.status(config,args.state),'accounts':accounts,'requests':[{k:j.get(k) for k in ('id','team','task','work_id','phase','reason','receiver','execution_constraint','completion_requirements','error_type','error_stage','error_code','workflow_ref','priority','dependencies','resources')} for j in requests(args.state)]}
    if args.action=='plan':return plan(args.config,args.state,args.team,getattr(args,'provider',None),getattr(args,'account',None))
    from .core_runtime import require_managed
    require_managed()
    if args.action=='workspace-register':
        from .workspace_sessions import register
        return register(args.config,args.state,args.team,args.terminal,args.operator_approved)
    if args.action=='workspace-receipt':
        from .workspace_sessions import receipt
        return receipt(args.config,args.state,args.terminal,args.expected_owner,args.delivery_id)
    if args.action=='resume-preparation':
        return resume_preparation(args.config,args.state,args.request,args.operator_approved)
    if args.action=='prepare':return prepare(args.config,args.state)
    if args.action=='recheck':return pool.recheck(args.config,args.state)
    if args.action=='present':return pool.present(config,args.state)
    if args.action=='refresh':
        from .context_handoff import refresh_candidates
        for provider in config['profiles']:
            for account in ('team','personal'):refresh_candidates(args.config,args.state,[(provider,account)])
        return {'refreshed':'configured account observations; unknown or failed readings remain ineligible','model_requests':0}
    if args.action=='resolve':
        return resolve(args.config,args.state,args.request,args.outcome,args.evidence_file,args.expected_owner,args.expected_attempt)
    if args.action=='submit':
        return submit(args.config,args.state,args.request,args.evidence_file,args.expected_owner,args.expected_attempt,args.delivery_id)
    if args.action=='rebind-receiver':
        return rebind_receiver(args.config,args.state,args.request,args.evidence_file,args.expected_owner,args.expected_attempt,args.operator_approved)
    if args.action=='assign':
        if not args.team or not args.message_file:raise ValueError('team and approved message file required')
        from .context_handoff import ensure
        ensure(args.config,args.state)
        return enqueue(args.config,args.state,args.team,args.message_file,getattr(args,'provider',None),getattr(args,'account',None),getattr(args,'work',None),getattr(args,'completion_file',None),
                       getattr(args,'workflow',None),getattr(args,'priority',0),getattr(args,'depends_on',[]),getattr(args,'resource',[]))
    if not args.team:raise ValueError('team required')
    store=Store(args.state)
    try:task=store.get(pool.task_id(config,args.team))
    finally:store.close()
    refresh_choice(args.config,args.state,task)
    route=plan(args.config,args.state,args.team)
    store=Store(args.state)
    try:task=store.get(route['task'])
    finally:store.close()
    if task['state']=='ready':return {'state':'ready','terminal':task['runtime']['terminal'],'message':'already received; no bootstrap repeated'}
    if task['state']!='queued' or task.get('attempt_count',0)!=0:raise ValueError('bootstrap is only for an unused pool leader')
    if route['action']!='dispatch':return route
    from .context_handoff import locked,directory,capacity_available,attach
    from .context_handoff import read
    previous=read(Path(config['session_pool']['bindings']))
    previous_terminal=(previous or {}).get('teams',{}).get(args.team,{}).get('terminal')
    with locked(directory(args.state)/'model-start'):
        if not capacity_available(args.state):return {'action':'wait','reason':'global model capacity'}
        result=official(args.config,args.state,'dispatch',route['task'],'--expected-attempt','0')
    if result.get('state')=='ready' and result.get('runtime',{}).get('identity_checked') is True:
        attach({**config,'context_bindings':config['session_pool']['bindings']},args.state,
               {'kind':'task','id':result['id'],'terminal':previous_terminal},result['runtime']['terminal'])
    return result


def resume_preparation(config_path,state,ident,approved=False):
    """Resume only a proven pre-delivery preparation failure, preserving request identity."""
    from .core_runtime import require_managed
    from .context_handoff import locked, read
    require_managed()
    if approved is not True:raise ValueError('operator approval required')
    from .context_handoff import JOB
    if not isinstance(ident,str) or not JOB.fullmatch(ident):raise ValueError('invalid request identity')
    path=folder(state)/(ident+'.json')
    with locked(path):
        job=read(path)
        if (not job or job.get('phase')!='uncertain' or job.get('error_code')!='preparation_failed'
                or job.get('error_stage') not in ('pending','waiting_handoff')
                or any(job.get(k) for k in ('receiver','delivery_id','delivery_receipt','delivery_message_sha256','submission'))):
            raise ValueError('request is not a proven pre-delivery failure; preserve original outcome')
        job.setdefault('recovery_history',[]).append(dict(at=time.time(),phase=job['phase'],
            error_type=job.get('error_type'),error_code=job.get('error_code'),error_stage=job.get('error_stage')))
        job.update(phase='pending',reason='전달 전 준비 실패를 확인했습니다. 현재 세션과 사용량을 다시 확인해 배정합니다.',updated_at=time.time())
        write_request(path,job)
    notify(state)
    return {k:v for k,v in job.items() if k!='message'}
