"""GUI account coverage. Native /usage observations never authorize admission."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

from . import providers
from .config import binding
from .policy import CLAUDE_QUOTA_SOURCE, normalize_quota, number
from .statusline import peek_quota
from .runtime import AUTH_ENV, home_value
from .store import Store

REASONS={'provider_windows_read':'구독 조회가 반환한 사용량입니다.',
         'native_usage_needed':'로그인은 확인했지만 GUI에 연결된 최신 사용량 자료가 없습니다.',
         'native_display_observation':'공식 /usage에서 확인한 표시 전용 값입니다.',
         'native_response_observation':'실제 응답에 연결된 statusline 사용량입니다.',
         'observation_expired':'마지막 사용량 자료의 유효기간이 지났습니다.',
         'observation_unreadable':'이전 표시용 기록을 읽지 못했습니다. 새 /usage 관측이 필요합니다.',
         'query_failed':'구독 CLI에서 사용량을 읽지 못했습니다. 로그인 문제로 단정하지 않습니다.',
         'login_required':'이 프로필의 구독 로그인을 확인해야 합니다.',
         'profile_mismatch':'실제 로그인 계정이 설정한 프로필과 일치하지 않습니다.'}
WINDOW_LABELS={'provided':'조회 성공','not_provided':'제공 안 함','unavailable':'확인 필요','stale':'지난 기록'}

LABELS={'query_succeeded':'조회 성공','query_failed':'조회 실패','login_required':'로그인 필요',
        'profile_mismatch':'계정 확인 필요','not_collected':'확인 필요','stale':'지난 기록'}


class UsageUnavailable(ValueError):
    pass


def _path(state, suffix):
    return Path(state).expanduser().resolve().with_name(Path(state).name+suffix)


def _read(path):
    if not path.exists():return {}
    value=json.loads(path.read_text())
    if not isinstance(value,dict):raise ValueError('usage metadata must be an object')
    return value


def _write(path,value):
    fd,name=tempfile.mkstemp(prefix='.usage-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as out:
            json.dump(value,out,ensure_ascii=False);out.flush();os.fsync(out.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


@contextmanager
def _lock(path):
    fd=os.open(str(path)+'.lock',os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as error:raise UsageUnavailable('busy') from error
        yield
    finally:os.close(fd)


def _gui(config,state):
    if config.get('runtime_backend')!='core' or not Path(state).is_file():
        raise ValueError('usage collection requires an existing explicit GUI core ledger')


def claude_identity(pr):
    try:
        result=subprocess.run(['claude','auth','status','--json'],capture_output=True,text=True,
                              env=providers.profile_env('claude',pr),timeout=15)
        value=json.loads(result.stdout)
    except (OSError,ValueError,subprocess.SubprocessError) as error:
        raise UsageUnavailable('query_failed') from error
    if not isinstance(value,dict):raise UsageUnavailable('query_failed')
    if value.get('loggedIn') is False or value.get('authMethod') not in (None,'claude.ai'):
        raise UsageUnavailable('login_required')
    if result.returncode or value.get('loggedIn') is not True:
        raise UsageUnavailable('query_failed')
    try:
        providers.match_identity(pr,{'email':value.get('email'),'organizationId':value.get('orgId'),
                                     'planType':value.get('subscriptionType')})
    except Exception as error:raise UsageUnavailable('profile_mismatch') from error


def _failure(error):
    if isinstance(error,UsageUnavailable) and str(error) in LABELS:return str(error)
    if str(error) in ('subscription account required','subscription account required; API fallback disabled'):
        return 'login_required'
    if str(error) in ('actual account does not match configured profile','actual organization/plan does not match configured profile'):
        return 'profile_mismatch'
    return 'query_failed'


def _entry(provider,account,pr,status,attempt,reason=None):
    actions={'login_required':('login','이 계정 로그인 확인'),'profile_mismatch':('verify_profile','설정과 로그인 계정 확인'),
             'query_failed':('refresh','사용량 다시 조회'),'not_collected':('read_native_usage','이 계정 /usage 확인'),
             'stale':('refresh' if provider=='codex' else 'read_native_usage','사용량 다시 확인'),
             'query_succeeded':('refresh' if provider=='codex' else 'read_native_usage','사용량 다시 확인')}
    kind,label=actions[status]
    return {'provider':provider,'account':account,'binding':binding(pr),'attempt_at':attempt,
            'observed_at':None,'source':None,'status':status,'status_label':LABELS[status],
            'reason':reason or status,'reason_label':REASONS.get(reason or status,LABELS[status]),'action':{'kind':kind,'label':label},'display_only':False,
            'windows':{key:{'status':'unavailable'} for key in ('five_hour','weekly')}}


def _measured(row,value,now,ttl,display_only=False,missing='not_provided'):
    at=value.get('observed_at');age=now-at if number(at) and 0<=now-at else None
    current=age is not None and age<=ttl and not value.get('unavailable')
    row.update(observed_at=at if age is not None else None,source=value.get('source') or 'codex-account-rate-limits',display_only=display_only)
    for key in row['windows']:
        window=value.get(key)
        used=window if display_only else window.get('used') if isinstance(window,dict) else None
        valid=number(used) and 0<=used<=100
        reset=None if display_only else window.get('resets_at') if isinstance(window,dict) else None
        valid=valid and (display_only or number(reset) and reset>now)
        state='provided' if valid and current else 'stale' if valid or age is not None and not current else missing if window is None else 'unavailable'
        out={'status':state,'status_label':WINDOW_LABELS[state],'observed_at':row['observed_at'],
             'reason':'provider_window_not_returned' if state=='not_provided' else state,
             'reason_label':'이번 공식 조회 응답에 이 시간 구간이 없습니다.' if state=='not_provided' else WINDOW_LABELS[state]}
        if valid:
            out['used']=used
            if reset is not None:out['resets_at']=reset
        row['windows'][key]=out
    return row


def read_coverage(config,state,now=None):
    now=time.time() if now is None else now
    try:data=_read(_path(state,'.usage-coverage.json'))
    except (OSError,ValueError):return {'version':1,'accounts':{},'model_requests':0,'error':'coverage_unreadable'}
    result={'version':1,'accounts':{},'model_requests':0}
    for provider,accounts in config['profiles'].items():
        for account,pr in accounts.items():
            key=provider+'/'+account;row=data.get('accounts',{}).get(key)
            if not isinstance(row,dict) or row.get('binding')!=binding(pr):continue
            at=row.get('observed_at')
            if row.get('status')=='query_succeeded' and (not number(at) or not 0<=now-at<=config.get('quota_ttl_seconds',120)):
                row={**row,'status':'stale','status_label':LABELS['stale'],'reason':'observation_expired','reason_label':REASONS['observation_expired'],
                     'windows':{k:{**v,'status':'stale','status_label':WINDOW_LABELS['stale']} for k,v in row.get('windows',{}).items()}}
            windows=row.get('windows',{})
            expired=[k for k,w in windows.items() if w.get('status')=='provided' and number(w.get('resets_at')) and w['resets_at']<=now]
            if expired:
                row={**row,'status':'stale','status_label':LABELS['stale'],'reason':'window_reset_passed',
                     'reason_label':'기존 사용량 구간의 초기화 시각이 지났습니다. 다시 조회해야 합니다.',
                     'windows':{k:{**w,'status':'stale','status_label':WINDOW_LABELS['stale']} if k in expired else w for k,w in windows.items()}}
            result['accounts'][key]=row
    return result


def collect(config,state):
    """Read official subscription interfaces, saving only numeric quotas/coverage."""
    _gui(config,state);path=_path(state,'.usage-coverage.json');accounts={}
    with _lock(path):
        observation_error=False
        try:observations=_read(_path(state,'.usage-observations.json'))
        except (OSError,ValueError):observations={};observation_error=True
        for provider,profiles in config['profiles'].items():
            for account,pr in profiles.items():
                key=provider+'/'+account;attempt=time.time();old=peek_quota(state,key)
                old=old if isinstance(old,dict) and old.get('binding')==binding(pr) else None
                if provider=='claude' and old and old.get('source')!=CLAUDE_QUOTA_SOURCE:old=None
                try:
                    if provider=='codex':
                        raw=providers.codex_quota(pr)
                        buckets=raw.get('rateLimitsByLimitId') or {}
                        bucket=buckets.get('codex') if buckets else raw.get('rateLimits')
                        if not isinstance(bucket,dict):raise UsageUnavailable('query_failed')
                        now=time.time();quota=normalize_quota('codex',raw,now);quota['binding']=binding(pr)
                        store=Store(state)
                        try:store.quota(key,quota)
                        finally:store.close()
                        row=_measured(_entry(provider,account,pr,'query_succeeded',attempt),quota,now,config.get('quota_ttl_seconds',120))
                        row['reason']='provider_windows_read'
                    else:
                        claude_identity(pr);now=time.time()
                        value=old if old and old.get('source')==CLAUDE_QUOTA_SOURCE else None
                        display=False;observation=observations.get(key)
                        if (isinstance(observation,dict) and observation.get('binding')==binding(pr)
                                and observation.get('source')=='native-cli-manual-observation'
                                and number(observation.get('observed_at')) and observation['observed_at']<=now
                                and all(number(observation.get(k)) and 0<=observation[k]<=100 for k in ('five_hour','weekly'))
                                and (not value or observation['observed_at']>value.get('observed_at',0))):
                            value=observation;display=True
                        if value and number(value.get('observed_at')) and value['observed_at']<=now:
                            fresh=now-value['observed_at']<=config.get('quota_ttl_seconds',120) and not value.get('unavailable')
                            row=_measured(_entry(provider,account,pr,'query_succeeded' if fresh else 'stale',attempt),value,now,config.get('quota_ttl_seconds',120),display)
                            row['reason']='native_display_observation' if display else 'native_response_observation'
                        else:
                            row=_entry(provider,account,pr,'not_collected',attempt,'observation_unreadable' if observation_error else 'native_usage_needed')
                            row['source']='claude-auth-status';row['auth_checked_at']=now
                        previous=_read(_native_attempt_path(state,account))
                        failed_at=previous.get('attempt_at')
                        if previous.get('error') and number(failed_at) and (not previous.get('cleaned') or not number(row.get('observed_at')) or failed_at>row['observed_at']):
                            row.update(status='query_failed',status_label=LABELS['query_failed'],
                                       reason=previous['error'],native_attempt_at=failed_at)
                            row['action']={'kind':'read_native_usage' if previous.get('cleaned') else 'inspect_native_usage',
                                           'label':'내장 /usage 다시 확인' if previous.get('cleaned') else '결과 불명 측정 세션 확인'}
                            row['windows']={k:{**w,'status':'stale','status_label':WINDOW_LABELS['stale']} for k,w in row['windows'].items()}
                except Exception as error:
                    status=_failure(error);row=_entry(provider,account,pr,status,attempt)
                    if old:
                        row=_measured(row,{**old,'unavailable':True},time.time(),config.get('quota_ttl_seconds',120))
                    if provider=='codex':
                        failed={**(old or {'binding':binding(pr)}),'unavailable':True,'refresh_attempt_at':attempt}
                        store=Store(state)
                        try:store.quota(key,failed)
                        finally:store.close()
                row['reason_label']=REASONS.get(row['reason'],row['status_label'])
                accounts[key]=row
        result={'version':1,'accounts':accounts,'model_requests':0}
        _write(path,result)
    return result


def parse_native_usage(text):
    """Read two builtin /usage bars only; cached/error screens are not fresh data."""
    if not isinstance(text,str) or re.search(r'last[- ]known|rate.limit|failed to|could not|unable to|loading',text,re.I):
        raise ValueError('native usage is cached, pending or failed')
    lines=text.splitlines();result={}
    for index,line in enumerate(lines):
        key='five_hour' if re.fullmatch(r'\s*Current session\s*',line) else 'weekly' if re.fullmatch(r'\s*Current week \(all models\)\s*',line) else None
        if not key:continue
        for following in lines[index+1:index+5]:
            match=re.search(r'(?<![\d.+-])(\d+(?:\.\d+)?)%\s+used\b',following)
            if match:
                value=float(match.group(1))
                if key in result or not 0<=value<=100:raise ValueError('native usage bars are ambiguous or invalid')
                result[key]=value;break
            if re.search(r'Current (session|week)',following):break
    if set(result)!={'five_hour','weekly'}:raise ValueError('both native usage windows are required')
    return result


def record_native_observation(config,state,account,text,observed_at):
    _gui(config,state);now=time.time()
    if not number(observed_at) or not 0<=now-observed_at<=config.get('quota_ttl_seconds',120):
        raise ValueError('native observation timestamp must be fresh and not in the future')
    values=parse_native_usage(text);pr=config['profiles']['claude'][account];claude_identity(pr)
    if time.time()-observed_at>config.get('quota_ttl_seconds',120):raise ValueError('native observation expired during identity check')
    incoming={**values,'binding':binding(pr),'observed_at':observed_at,'source':'native-cli-manual-observation'}
    path=_path(state,'.usage-observations.json')
    with _lock(path):
        observations=_read(path);key='claude/'+account;old=observations.get(key)
        if isinstance(old,dict) and old.get('binding')==binding(pr) and number(old.get('observed_at')) and old['observed_at']>=observed_at:
            raise ValueError('native observation is not newer than the saved measurement')
        observations[key]=incoming;_write(path,observations)
    return incoming


NATIVE_REASONS={'native_trust_prompt':'측정용 CLI가 신뢰 확인을 기다려 사용량을 읽지 못했습니다.',
                'native_login_required':'측정용 CLI에서 구독 로그인이 필요합니다.',
                'native_usage_cached':'CLI가 지난 사용량을 보여 새 관측으로 저장하지 않았습니다.',
                'native_usage_timeout':'내장 /usage의 두 사용량 구간을 기한 안에 읽지 못했습니다.',
                'native_usage_rate_limited':'사용량 조회 요청이 일시 제한됐습니다. 작업 잔량 부족으로 단정하지 않습니다.',
                'native_usage_failed':'내장 /usage 화면에서 사용량 조회 오류를 보고했습니다.',
                'native_outcome_unknown':'터미널 명령 결과가 불명확합니다. 재시작 전에 실행을 확인해야 합니다.',
                'native_not_ready':'측정용 CLI가 입력 가능한 대기 상태가 아닙니다.',
                'native_unexpected_turn':'내장 /usage 조회 이외의 실행 상태가 관측돼 중단했습니다.',
                'native_cli_failed':'측정용 CLI 명령이 실패했습니다.',
                'native_ownership_changed':'측정용 터미널의 소유 표지가 바뀌어 조작하지 않았습니다.'}
REASONS.update(NATIVE_REASONS)


def _native_attempt_path(state,account):
    return _path(state,'.native-usage-'+account+'.json')


def _hc(args,timeout=15,plain=False):
    try:result=subprocess.run(['hc',*args],capture_output=True,text=True,timeout=timeout)
    except (OSError,subprocess.SubprocessError) as error:
        raise UsageUnavailable('native_outcome_unknown') from error
    if args[0]=='get' and result.returncode==3:return {'address':args[1],'state':'missing'}
    if result.returncode:raise UsageUnavailable('native_cli_failed')
    if plain:return result.stdout
    try:value=json.loads(result.stdout)
    except ValueError as error:raise UsageUnavailable('native_outcome_unknown') from error
    if not isinstance(value,dict) or value.get('error'):raise UsageUnavailable('native_cli_failed')
    return value


def _dialog(text):
    if re.search(r'Allow external CLAUDE.md|trust (this|the) (folder|directory)|Yes, I trust|workspace trust',text,re.I):
        return 'native_trust_prompt'
    if re.search(r'not logged in|please log ?in|sign in to|select login method|login required',text,re.I):
        return 'native_login_required'
    return None


def _owned_usage(address,labels):
    native=_hc(['get',address])
    if native.get('state')=='missing':return native
    if native.get('address')!=address or any(native.get('labels',{}).get(k)!=v for k,v in labels.items()):
        raise UsageUnavailable('native_ownership_changed')
    return native


def _cleanup_usage(address,labels):
    native=_owned_usage(address,labels)
    if native.get('state')=='missing':return True
    if native.get('state')=='live':
        try:_hc(['kill',address])
        except UsageUnavailable:
            if _owned_usage(address,labels).get('state')!='finished':raise
    if _owned_usage(address,labels).get('state')!='finished':return False
    try:_hc(['rm',address])
    except UsageUnavailable:
        if _hc(['get',address]).get('state')!='missing':raise
    return _hc(['get',address]).get('state')=='missing'


def refresh_native_usage(config,state,account):
    """Explicit user refresh only: one owned temporary CLI, one builtin /usage."""
    _gui(config,state)
    pr=config['profiles']['claude'][account];path=_native_attempt_path(state,account)
    with _lock(path):
        previous=_read(path)
        if previous and not previous.get('cleaned'):
            raise UsageUnavailable('native_outcome_unknown')
        claude_identity(pr)
        nonce=uuid.uuid4().hex;labels={'teamkit.usage':nonce,'teamkit.usage-account':'claude/'+account,
                                      'teamkit.usage-profile':binding(pr)}
        record={'attempt_at':time.time(),'binding':binding(pr),'nonce':nonce,'cleaned':False,'terminal':None}
        _write(path,record);address=None;value=None;error=None
        try:
            root=Path(config.get('usage_probe_root',Path(state).expanduser().resolve().parent))
            if not root.is_dir():raise UsageUnavailable('native_cli_failed')
            args=['run','--json','--bg','--no-parent','--no-notify','--name','usage-'+account+'-'+nonce[:12],'--cwd',str(root)]
            for key,val in labels.items():args+=['--label',key+'='+val]
            args+=['--','env']
            for key in (*AUTH_ENV,'CODEX_HOME','CLAUDE_CONFIG_DIR','HERDR_ENV','HERDR_SESSION','HERDR_PANE_ID'):
                args+=['-u',key]
            home=home_value('claude',pr['home'])
            if home:args+=['CLAUDE_CONFIG_DIR='+home]
            args+=['claude','--safe-mode','--strict-mcp-config','--mcp-config','{"mcpServers":{}}']
            started=_hc(args,timeout=20);address=started.get('address')
            if not isinstance(address,str) or not re.fullmatch(r't[0-9a-z]+',address):
                address=None;raise UsageUnavailable('native_outcome_unknown')
            record['terminal']=address;_write(path,record)
            native=_owned_usage(address,labels)
            text=_hc(['read','--lines','100',address],plain=True)
            dialog=_dialog(text)
            if dialog:raise UsageUnavailable(dialog)
            if (native.get('status') or {}).get('state')!='idle':
                _hc(['wait','--json','--timeout','15',address],timeout=20)
                native=_owned_usage(address,labels)
            # The program's OSC idle report can arrive before the OS foreground
            # snapshot. Keep the same newly owned terminal; never relaunch it.
            foreground_deadline=time.monotonic()+3
            while (native.get('state')=='live' and (native.get('status') or {}).get('state')=='idle'
                   and native.get('foreground') is None and time.monotonic()<foreground_deadline):
                time.sleep(0.1)
                native=_owned_usage(address,labels)
            foreground=native.get('foreground') or {};status=native.get('status') or {}
            record['native']={'foreground_name':foreground.get('name'),'foreground_pid':foreground.get('pid'),
                              'status_app':status.get('app'),'status_state':status.get('state')}
            if (native.get('state')!='live' or status.get('state')!='idle'
                    or foreground.get('name') not in ('claude','claude.exe')
                    or type(foreground.get('pid')) is not int or foreground['pid']<=0
                    or status.get('app') not in ('claude','claude-code')):
                text=_hc(['read','--lines','100',address],plain=True)
                raise UsageUnavailable(_dialog(text) or 'native_not_ready')
            baseline=_hc(['read','--lines','100',address],plain=True)
            if 'Current week (all models)' in baseline:raise UsageUnavailable('native_outcome_unknown')
            sent=_hc(['send','--json','--if','idle',address,'/usage'])
            if sent.get('started') is not False or sent.get('status')!='idle':
                raise UsageUnavailable('native_unexpected_turn')
            deadline=time.monotonic()+10
            while True:
                _owned_usage(address,labels)
                text=_hc(['read','--lines','120',address],plain=True);observed=time.time()
                dialog=_dialog(text)
                if dialog:raise UsageUnavailable(dialog)
                if re.search(r'last[- ]known',text,re.I):raise UsageUnavailable('native_usage_cached')
                if re.search(r'rate.limit|too many requests|\b429\b',text,re.I):raise UsageUnavailable('native_usage_rate_limited')
                if re.search(r'failed to (load|fetch)|error (loading|fetching)|unable to (load|fetch)',text,re.I):raise UsageUnavailable('native_usage_failed')
                try:parse_native_usage(text);break
                except ValueError:
                    if time.monotonic()>=deadline:raise UsageUnavailable('native_usage_timeout')
                    # Builtin dialogs report no OSC working/turn transition. Read
                    # this new terminal's bounded UI until its query completes.
                    time.sleep(0.2)
            if not re.search(r'Usage:\s*0 input, 0 output, 0 cache read, 0 cache write',text):
                raise UsageUnavailable('native_unexpected_turn')
            value=record_native_observation(config,state,account,text,observed)
            record['observed_at']=observed;record['values']={k:value[k] for k in ('five_hour','weekly')}
        except UsageUnavailable as caught:error=caught
        except Exception as caught:error=UsageUnavailable('native_cli_failed')
        finally:
            if address is None:
                try:
                    found=_hc(['ls','--json','--label','teamkit.usage='+nonce]).get('terminals',[])
                    candidates=[r.get('address') for r in found if isinstance(r,dict) and re.fullmatch(r't[0-9a-z]+',r.get('address',''))]
                    if len(candidates)==1:address=candidates[0];record['terminal']=address
                except Exception:pass
            if address:
                try:record['cleaned']=_cleanup_usage(address,labels)
                except Exception:record['cleaned']=False
            if not record['cleaned'] and error is None:error=UsageUnavailable('native_outcome_unknown')
            if error:record['error']=str(error)
            _write(path,record)
        if error:raise error
        return value
