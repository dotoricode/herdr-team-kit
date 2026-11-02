"""Team-leader pool commands and durable work requests using the official CLI."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .config import load, cached_quota
from .store import Store, ACTIVE
from . import session_pool as pool


def folder(state):
    path=Path(state).resolve().parent/'pool-requests'
    path.mkdir(mode=0o700,exist_ok=True)
    if path.is_symlink():raise ValueError('invalid pool request directory')
    return path


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


def enqueue(config_path,state,team,message_file,provider=None,account=None):
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
    from .context_handoff import locked, write, read
    with locked(folder(state)/'requests'):
        digest=hashlib.sha256(text.encode()).hexdigest()
        for path in folder(state).glob('*.json'):
            old=read(path)
            if old and old.get('task')==task and old.get('phase') not in ('done','cancelled'):
                if old.get('digest')==digest and old.get('execution_constraint')==requested:return {k:v for k,v in old.items() if k!='message'}
                raise ValueError('this team already has a pending request; preserve it before another assignment')
        value={'id':uuid.uuid4().hex,'team':team,'task':task,'message':text,'digest':digest,
               'execution_constraint':requested,
               'phase':'pending','at':time.time(),'updated_at':time.time()}
        write(folder(state)/(value['id']+'.json'),value)
    return {k:v for k,v in value.items() if k!='message'}


def requests(state):
    from .context_handoff import read, JOB
    root=Path(state).resolve().parent/'pool-requests'
    if not root.exists():return []
    if root.is_symlink():raise ValueError('invalid pool request directory')
    return [read(p) for p in sorted(root.glob('*.json')) if JOB.fullmatch(p.stem)]


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
    path=folder(state)/(ident+'.json')
    if not context.JOB.fullmatch(ident):raise ValueError('invalid assignment identity')
    with context.locked(path):
        job=context.read(path)
        if not job or job['phase'] in ('done','uncertain','cancelled'):return
        config=load(config_path)
        if not pool.enabled(config):return
        def save(phase,reason):
            job.update(phase=phase,reason=reason,updated_at=time.time());context.write(path,job)
        try:
            if job['task']!=pool.task_id(config,job['team']):return save('uncertain','담당 설정이 변경됐습니다. 요청을 보존했습니다.')
            if job['phase'] in ('sending','dispatching'):
                return save('uncertain','전송·배정 결과를 확인해야 합니다. 반복 실행하지 않습니다.')
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
                job['last_refresh']=time.time();context.write(path,job)
            route=plan(config_path,state,job['team'])
            if route['action']=='wait':return save('pending',route['reason'])
            if route['action']=='handoff':
                target=next(t for t in context.targets(config,state) if t['id']==job['task'])
                handoff=context.request(config,state,target,None,policy_rotation=True)
                if handoff is None:
                    handoff=next(j for j in context.jobs(state) if j['target']['id']==job['task'] and j['target']['owner']==route['owner'] and j['phase'] not in context.FINISHED)
                job['handoff_job']=handoff['id'];return save('waiting_handoff','사용량·요일 순서에 맞는 다음 팀장으로 인계 중입니다.')
            with context.locked(context.directory(state)/'model-start'):
                target=next((t for t in context.targets(config,state) if t['id']==job['task']),None)
                if not context.capacity_available(state,target):return save('pending','동시 실행 여유를 기다립니다.')
                if route['action']=='dispatch':
                    save('dispatching','준비된 세션에 공식 배정합니다.')
                    result=official(config_path,state,'dispatch',job['task'],'--expected-attempt',str(route['attempt']))
                    if result.get('state')!='ready':return save('uncertain','새 담당의 수신·대기를 확인해야 합니다.')
                    return save('pending','새 담당 준비가 끝났습니다. 다음 경계에서 요청을 전달합니다.')
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
                message.write_text(job['message'])
                message.chmod(0o600)
                save('sending','선택된 팀장에게 요청을 전달합니다.')
                result=official(config_path,state,'tell',job['task'],'--message-file',str(message),
                    '--expected-owner',route['owner'],'--expected-attempt',str(route['attempt']))
                job['receiver']={'terminal':result['runtime']['terminal'],'owner':result['owner'],'attempt':result['attempt_count']}
                save('done','요청 전달과 담당 응답을 확인했습니다.')
        except Exception as error:
            job['error_type']=type(error).__name__
            save('uncertain','실행 결과를 확인해 주세요. 기존 기록과 요청을 보존했습니다.')


def handle(args):
    config=load(args.config)
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
        return {**pool.status(config,args.state),'accounts':accounts,'requests':[{k:j.get(k) for k in ('id','team','task','phase','reason','receiver')} for j in requests(args.state)]}
    if args.action=='plan':return plan(args.config,args.state,args.team,getattr(args,'provider',None),getattr(args,'account',None))
    from .core_runtime import require_managed
    require_managed()
    if args.action=='prepare':return prepare(args.config,args.state)
    if args.action=='recheck':return pool.recheck(args.config,args.state)
    if args.action=='present':return pool.present(config,args.state)
    if args.action=='refresh':
        from .context_handoff import refresh_candidates
        for provider in config['profiles']:
            for account in ('team','personal'):refresh_candidates(args.config,args.state,[(provider,account)])
        return {'refreshed':'configured account observations; unknown or failed readings remain ineligible','model_requests':0}
    if args.action=='assign':
        if not args.team or not args.message_file:raise ValueError('team and approved message file required')
        from .context_handoff import ensure
        ensure(args.config,args.state)
        return enqueue(args.config,args.state,args.team,args.message_file,getattr(args,'provider',None),getattr(args,'account',None))
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
