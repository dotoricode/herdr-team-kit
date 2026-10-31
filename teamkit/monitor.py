"""Host-only quota/native observation service; never starts a model turn."""
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import tempfile
import time

from .config import binding, load, profile
from .policy import normalize_quota
from .providers import codex_quota
from .runtime import Herdr
from .store import Store


def namespace(config):
    value={'session':config.get('herdr_session'),'teams':config.get('dashboard_workspaces',{}),
           'profiles':{p+'/'+a:binding(pr) for p,accounts in config['profiles'].items() for a,pr in accounts.items()}}
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def cache_path(state):
    path=Path(state).expanduser().resolve()
    return path.with_name(path.name+'.monitor.json')


def write_cache(state, value):
    path=cache_path(state)
    fd,name=tempfile.mkstemp(prefix='.monitor-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as out:
            json.dump(value,out);out.flush();os.fsync(out.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def read_native(config):
    prefix=['--session',config['herdr_session']] if config.get('herdr_session') else []
    result=Herdr().call(*prefix,'agent','list')
    agents=result.get('agents')
    if not isinstance(agents,list):raise ValueError('invalid native observation')
    return [{k:a.get(k) for k in ('name','agent_status','pane_id','workspace_id','completion_seq')} for a in agents if isinstance(a,dict)]


def collect_codex(config):
    results=[]
    for account,pr in config['profiles'].get('codex',{}).items():
        try:
            raw=codex_quota(pr)
            quota=normalize_quota('codex',raw,time.time())
            quota['binding']=binding(pr)
        except Exception:
            quota=None
        results.append((account,binding(pr),quota))
    return results


def save_quotas(state,config,results):
    store=Store(state)
    try:
        for account,expected,quota in results:
            if binding(profile(config,'codex',account))!=expected:continue
            if quota is None:
                previous=store.quota('codex/'+account) or {}
                quota=previous if previous.get('binding')==expected else {'binding':expected}
                quota={**quota,'unavailable':True,'refresh_attempt_at':time.time()}
            store.quota('codex/'+account,quota)
    finally:store.close()


def run(args,config):
    if not Path(args.state).is_file():raise ValueError('monitor requires an existing ledger')
    if not 1<=args.interval<=60 or not 15<=args.quota_interval<=300:
        raise ValueError('monitor interval must be 1..60, quota interval 15..300')
    lock=os.open(str(cache_path(args.state))+'.lock',os.O_CREAT|os.O_WRONLY,0o600)
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock);raise ValueError('another monitor is active')
    stopping=False
    def stop(*unused):
        nonlocal stopping
        stopping=True
    old=signal.signal(signal.SIGTERM,stop)
    future=None;next_quota=0
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            while not stopping:
                config=load(args.config)
                if future is not None and future.done():
                    save_quotas(args.state,config,future.result());future=None
                if future is None and time.monotonic()>=next_quota:
                    future=pool.submit(collect_codex,config);next_quota=time.monotonic()+args.quota_interval
                try:agents=read_native(config);error=''
                except Exception:agents=[];error='Herdr 실행 상태 조회 실패'
                write_cache(args.state,{'namespace':namespace(config),'observed_at':time.time(),
                    'agents':agents,'error':error,'quota_refreshing':future is not None,'native_ttl':max(10,args.interval*3)})
                if args.once:
                    save_quotas(args.state,load(args.config),future.result())
                    break
                until=time.monotonic()+args.interval
                while not stopping and time.monotonic()<until:time.sleep(0.1)
    except KeyboardInterrupt:pass
    finally:
        signal.signal(signal.SIGTERM,old)
        # Do not leave a stopped collector looking live.
        if not args.once:
            write_cache(args.state,{'namespace':namespace(config),'observed_at':time.time(),'agents':[],
                                    'error':'monitor 중지됨','native_ttl':0})
        os.close(lock)
    return {'monitor':'stopped','model_requests':0}
