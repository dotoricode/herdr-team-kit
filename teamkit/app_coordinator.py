"""Explicit recovery of the GUI coordinator from durable GUI records."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

from .app_bridge import core_read
from .config import load, profile, binding, cached_quota
from .core_runtime import _call, require_managed, CoreRuntime, startup_native
from .policy import choose_account
from .runtime import launch_spec, RuntimeError as KitError
from .store import Store
from . import providers


def record_path(state):
    return Path(state).resolve().with_name('coordinator.json')


def scope(state):
    return hashlib.sha256(str(Path(state).resolve()).encode()).hexdigest()[:12]


def save(path, value):
    fd, name=tempfile.mkstemp(prefix='.coordinator-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as out:
            json.dump(value,out,ensure_ascii=False,indent=2);out.flush();os.fsync(out.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def read_record(state):
    path=record_path(state)
    if not path.exists():return {}
    if path.stat().st_size>1024*1024:raise ValueError('총괄 복구 기록이 너무 큽니다.')
    value=json.loads(path.read_text())
    if not isinstance(value,dict):raise ValueError('총괄 복구 기록을 확인하세요.')
    return value


def status(state, linked, terminals=None, unavailable=False):
    try:
        if unavailable:raise ValueError('core unavailable')
        record=read_record(state)
        entries=core_read()['terminals'] if terminals is None else terminals
        live=[t for t in entries if t.get('state')=='live']
        name='gui-coordinator-'+scope(state)
        owned=[t for t in live if t.get('name')==name]
        if len(owned)>1:raise ValueError('총괄 후보가 중복돼 있습니다.')
        if owned:
            target=_call('get',owned[0]['address'])
            if target.get('labels',{}).get('teamkit.app-scope')!=scope(state):
                raise ValueError('총괄 이름과 소유권이 일치하지 않습니다.')
            if (target.get('foreground') or {}).get('name')!='codex':
                return {'state':'unknown','terminal':target['address'],'label':'총괄','can_recover':False,
                        'reason':'총괄 자리에 다른 프로그램이나 복구된 쉘이 있습니다. 먼저 확인하고 보존하세요.'}
            return {'state':'live','terminal':target['address'],'label':'총괄','can_recover':False,
                    'agent_state':(target.get('status') or {}).get('state','unknown'),
                    'reason':record.get('reason','GUI 체크포인트를 사용하는 총괄입니다.')}
        terminal=record.get('terminal') or linked.get('coordinator',{}).get('terminal')
        target=next((t for t in live if t['address']==terminal),None)
        if target:
            return {'state':'live','terminal':terminal,'label':'총괄','can_recover':False,
                    'agent_state':target.get('status','unknown'),'reason':record.get('reason','')}
        if record.get('phase') in ('recovering','launching','uncertain'):
            return {'state':'recovering' if record['phase']!='uncertain' else 'unknown',
                    'terminal':None,'label':'총괄','can_recover':False,
                    'reason':record.get('reason','이전 복구 결과를 확인 중입니다. 중복 실행하지 않습니다.')}
        return {'state':'missing','terminal':None,'label':'총괄','can_recover':True,
                'reason':record.get('reason','총괄이 닫혔습니다. GUI 작업 기록에서 새 총괄 대화를 열 수 있습니다.')}
    except (OSError,ValueError,subprocess.SubprocessError,KitError):
        return {'state':'unknown','terminal':None,'label':'총괄','can_recover':False,
                'reason':'core 연결 또는 총괄 소유권을 확인하지 못했습니다. 종료로 추정하지 않습니다.'}


def start(config_path, state, binding_path, confirmed):
    if not confirmed:raise ValueError('GUI 기록에서 총괄을 다시 여는 확인이 필요합니다.')
    state=Path(state).resolve()
    lock=os.open(state.with_name('coordinator.lock'),os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        linked=json.loads(Path(binding_path).read_text())
        current=status(state,linked)
        if current['state']=='live':
            return {'message':'이미 실행 중인 GUI 총괄을 사용합니다.','terminal':current['terminal']}
        if not current['can_recover']:raise ValueError(current.get('reason','이전 총괄 복구를 확인하세요.'))
        nonce=uuid.uuid4().hex
        jobs=state.parent/'coordinator-jobs';jobs.mkdir(mode=0o700,exist_ok=True)
        folder=jobs/nonce;folder.mkdir(mode=0o700)
        packet={'config':str(Path(config_path).resolve()),'state':str(state),
                'bindings':str(Path(binding_path).resolve()),'nonce':nonce,
                'config_digest':hashlib.sha256(Path(config_path).read_bytes()).hexdigest()}
        request=folder/'request.json';save(request,packet)
        previous=read_record(state)
        if previous:save(folder/'previous.json',previous)
        record={'phase':'recovering','nonce':nonce,'at':time.time(),
                'reason':'GUI 체크포인트에서 새 총괄을 준비하고 있습니다. 제품 업무는 자동 재개하지 않습니다.'}
        save(record_path(state),record)
        try:
            child=_call('run','--json','--bg','--no-parent','--no-notify','--name','gui-recover-'+nonce[:12],
                        '--',sys.executable,str(Path(__file__).resolve().parent.parent/'teamkit_app.py'),
                        'coordinator-job','--request',str(request))
            if not isinstance(child.get('address'),str):raise ValueError('복구 controller를 확인하지 못했습니다.')
            record['controller']=child['address'];save(record_path(state),record)
        except BaseException:
            record.update(phase='uncertain',reason='복구 시작 결과가 불명입니다. 새 총괄을 다시 만들기 전에 확인하세요.')
            save(record_path(state),record);raise
        return {'message':'총괄 복구를 시작했습니다. 준비 상태는 운영 바에서 확인하세요.'}
    except BlockingIOError as error:
        raise ValueError('총괄 복구를 이미 처리 중입니다.') from error
    finally:os.close(lock)


def run_job(request):
    packet=json.loads(Path(request).read_text());state=Path(packet['state'])
    require_managed()
    lock=os.open(state.with_name('coordinator.lock'),os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    record=None;launched=False
    try:
        fcntl.flock(lock,fcntl.LOCK_EX)
        record=read_record(state)
        if record.get('nonce')!=packet['nonce'] or record.get('phase')!='recovering':
            raise ValueError('총괄 복구 요청이 바뀌었습니다.')
        if hashlib.sha256(Path(packet['config']).read_bytes()).hexdigest()!=packet['config_digest']:
            raise ValueError('복구할 계정·실행 설정이 바뀌었습니다.')
        config=load(packet['config'])
        policy=config.get('coordinator')
        if config.get('runtime_backend')!='core' or not isinstance(policy,dict) or policy.get('provider')!='codex':
            raise ValueError('GUI 총괄의 core·Codex 정책을 먼저 설정하세요.')
        from .workflow import private_root
        root=Path(policy['root']).resolve()
        if not root.is_dir() or not root.is_relative_to(private_root(config)):
            raise ValueError('GUI 총괄 작업 폴더는 GUI 비공개 기록 안에 있어야 합니다.')
        store=Store(state)
        try:
            account=choose_account(cached_quota(config,store,'codex','team'),cached_quota(config,store,'codex','personal'),time.time(),config['quota_ttl_seconds'])
        finally:store.close()
        if not account:raise ValueError('최신 구독 사용량을 확인한 뒤 총괄을 열 수 있습니다. 사용량 갱신을 실행하세요.')
        pr=profile(config,'codex',account,policy);providers.identity('codex',pr)
        spec=launch_spec('codex',pr,None)
        spec['env'].update(TEAMKIT_CONFIG=packet['config'],TEAMKIT_STATE=str(state),TEAMKIT_GUI_COORDINATOR=packet['nonce'])
        spec['args']+=['--add-dir',str(state.parent)]
        name='gui-coordinator-'+scope(state)
        sp='sp'+uuid.uuid4().hex;tab='tb'+uuid.uuid4().hex
        checkpoints=root/'coordinator-checkpoints';checkpoints.mkdir(mode=0o700,exist_ok=True)
        checkpoint=checkpoints/(packet['nonce']+'.md')
        linked=json.loads(Path(packet['bindings']).read_text())
        inventory=Path(linked['inventory'])
        content=('# GUI 총괄 복구 체크포인트\n\nGUI 장부: '+str(state)+
            '\n고정 작업 정본: '+str(inventory)+'\n실행 코드: '+str(Path(__file__).resolve().parent.parent)+
            '\n\nTUI의 작업·승인·소유권을 가져오지 않습니다. 기존 GUI 기록·현재 소스·검증 근거가 재개의 정본입니다. '
            '승인 범위를 새로 추정하거나 제품 업무를 자동 재개하지 마세요. 종료된 대화 전문을 복원했다는 뜻은 아닙니다.\n')
        fd=os.open(checkpoint,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w') as out:out.write(content)
        record.update(phase='launching',account=account,profile_binding=binding(pr),identity_source='host-preflight',
                      model=pr['model'],effort=pr.get('effort','low'),checkpoint=str(checkpoint),space=sp,tab=tab)
        save(record_path(state),record)
        args=['run','--json','--bg','--keep','--no-parent','--no-notify','--name',name,'--cwd',str(root)]
        labels={'herdr.space':sp,'herdr.tab':tab,'teamkit.app-scope':scope(state),'teamkit.kind':'coordinator',
                'teamkit.task':'GUI-COORDINATOR','teamkit.owner':packet['nonce'],'teamkit.name':name}
        for key,value in labels.items():args+=['--label',key+'='+value]
        if hashlib.sha256(Path(packet['config']).read_bytes()).hexdigest()!=packet['config_digest']:
            raise ValueError('총괄 실행 설정이 신원 확인 중 바뀌었습니다.')
        store=Store(state)
        try:
            current=choose_account(cached_quota(config,store,'codex','team'),cached_quota(config,store,'codex','personal'),time.time(),config['quota_ttl_seconds'])
        finally:store.close()
        if current!=account:
            raise ValueError('총괄 구독 한도가 신원 확인 중 바뀌었습니다. 갱신 후 다시 확인하세요.')
        # Creation may succeed even when its response is lost. Persist uncertainty
        # before crossing the native boundary so a retry cannot duplicate it.
        launched=True
        record['phase']='uncertain';save(record_path(state),record)
        created=_call(*args,'--','env',*(k+'='+v for k,v in spec['env'].items()),'codex',*spec['args'])
        record['phase']='launching';record['terminal']=created['address'];save(record_path(state),record)
        waited=_call('wait','--json','--timeout','30',record['terminal'])
        if waited.get('ended') is not True or waited.get('status') not in ('idle','done'):
            raise ValueError('총괄 터미널은 생성됐지만 준비를 확인하지 못했습니다. 다시 만들지 말고 확인하세요.')
        native=startup_native(record['terminal']);pid=(native.get('foreground') or {}).get('pid')
        runtime={'backend':'core','name':name,'terminal':record['terminal'],'native_pid':pid}
        actor=CoreRuntime({'id':'GUI-COORDINATOR','owner':packet['nonce'],'provider':'codex','runtime':runtime})
        actor._native(name,ready=True)
        for command,values in [('space.rename',{'id':sp,'name':'GUI 총괄'}),('tab.rename',{'id':tab,'name':'총괄'})]:
            subprocess.run(['herdr-app','do',command,json.dumps(values)],capture_output=True,text=True,timeout=5)
        # Read the checkpoint; this is initialization, never a product resume.
        prompt=('GUI 독립 운영 총괄의 복구 확인입니다. '+str(checkpoint)+'와 '+str(inventory)+
                '의 현재 상태, '+str(policy.get('instructions',''))+'의 GUI 지침만 확인하세요. '+
                '제품 코드 조사/수정·작업 재개·다른 agent 생성·기억 전체 조회·외부 게시를 하지 마세요. '+
                'GUI 기록에서 이어갈 준비가 됐는지와 미완료 항목을 짧게 보고하고 사용자 요청을 기다리세요. '+
                '이것은 새 총괄 대화이며 과거 대화 전문 복원이나 제품 구현 승인이 아닙니다.')
        actor.prompt(name,prompt)
        record.update(phase='ready',native_pid=pid,reason='GUI 체크포인트에서 새 총괄 대화를 열었습니다. 제품 업무는 자동 재개하지 않았습니다.')
        save(record_path(state),record);save(Path(request).with_name('result.json'),{'terminal':record['terminal'],'phase':'ready'})
    except BaseException as error:
        if record and record.get('nonce')==packet['nonce']:
            record.update(phase='uncertain' if launched else 'failed',reason=str(error))
            save(record_path(state),record)
        save(Path(request).with_name('error.json'),{'message':str(error),'terminal':record.get('terminal') if record else None})
        raise
    finally:os.close(lock)
