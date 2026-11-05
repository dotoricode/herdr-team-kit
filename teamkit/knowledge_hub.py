"""Read knowledge and refresh a local, observational staff roster."""
import json
import argparse
import os
import subprocess
from pathlib import Path
import tempfile
import time

from .app_bridge import bindings, readonly_snapshot, verified_session, session_read
from .config import load
from .knowledge import search
from .staff import display_name


def app_sessions():
    result=subprocess.run(['herdr-app','ls'],capture_output=True,text=True,timeout=5,check=True)
    value=json.loads(result.stdout)
    if not isinstance(value.get('spaces'),list):raise ValueError('App 세션 목록을 확인하지 못했습니다.')
    return value


def snapshot(config_path, state, binding_path):
    config = load(config_path)
    linked = bindings(binding_path, config)
    spaces = app_sessions()['spaces']
    try:tasks = readonly_snapshot(state)['tasks']
    except (OSError,ValueError):tasks=[]
    aliases = config.get('operations', {}).get('staff_aliases', {})
    teams = []
    for team, policy in config['teams'].items():
        entry = linked['teams'][team]
        space=next((item for item in spaces if item['id']==entry.get('space')),None)
        if space is None:
            matches=[item for item in spaces if item.get('folder')==policy.get('root')]
            if len(matches)==1:space=matches[0]
        employees=[];seen=set()
        for tab in (space or {}).get('tabs',[]):
            for item in tab.get('terminals',[]):
                address=item['terminal']
                if address in seen or item.get('app') not in ('codex','claude-code'):continue
                seen.add(address)
                label=None;verified_task=None
                try:label=(session_read(address).get('labels') or {}).get('ui.display-name')
                except (OSError,ValueError,subprocess.SubprocessError):pass
                for task in tasks:
                    if task.get('team')!=team or (task.get('runtime') or {}).get('terminal')!=address or not task.get('owner'):continue
                    try:verified,_status=verified_session(task,address)
                    except (OSError,ValueError,subprocess.SubprocessError):continue
                    if verified:verified_task=task;break
                provider='codex' if item['app']=='codex' else 'claude'
                account=(verified_task or {}).get('account')
                if (verified_task or {}).get('provider')!=provider or account not in config['profiles'].get(provider,{}):
                    account=None;verified_task=None
                employees.append({'terminal':address,'provider':provider,'account':account,
                    'role':(verified_task or {}).get('role'),'label':label or f"{tab.get('name','직원')} · {provider}",
                    'state':item.get('status','unknown'),'identity_verified':verified_task is not None})
        terminal=entry.get('terminal')
        if terminal not in seen:terminal=employees[0]['terminal'] if employees else None
        selected=next((item for item in employees if item['terminal']==terminal),{})
        teams.append({'id':team,'label':(space or {}).get('name',entry.get('label',team)),
            'space':(space or {}).get('id'),'terminal':terminal,'state':selected.get('state','미확인'),
            'live':bool(employees),'identity':'App 화면 기준 · 계정 신원은 별도 확인',
            'employees':employees,'roles':[{'role':role,'provider':value.get('provider')} for role,value in policy['roles'].items()]})
    profiles = [{'provider':provider,'account':account,'label':display_name(provider,account,aliases=aliases)}
                for provider,accounts in config['profiles'].items() for account in accounts]
    result = {'observed_at':time.time(),'teams':teams,'profiles':profiles}
    target = Path(state).resolve().parent/'knowledge-hub-roster.json'
    fd, temp = tempfile.mkstemp(prefix='.knowledge-hub-roster-',dir=target.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as out:
            json.dump(result,out,ensure_ascii=False);out.flush();os.fsync(out.fileno())
        os.replace(temp,target)
    finally:
        if os.path.exists(temp):os.unlink(temp)
    return result


def view(config_path, state, binding_path):
    return {'roster':snapshot(config_path,state,binding_path),
            'knowledge':search(Path.home()/'00_work/05_docs/wiki')}


def main():
    parser=argparse.ArgumentParser(description='직원명부 관찰 및 갱신')
    parser.add_argument('--config',default=str(Path.home()/'.config/herdr-team-kit-app/config.json'))
    parser.add_argument('--state',default=str(Path.home()/'.local/state/herdr-team-kit-app/state.sqlite'))
    parser.add_argument('--bindings',default=str(Path.home()/'.config/herdr-team-kit-app/bindings.json'))
    args=parser.parse_args()
    result=snapshot(args.config,args.state,args.bindings)
    print(json.dumps({'observed_at':result['observed_at'],'team_count':len(result['teams'])},ensure_ascii=False))


if __name__ == '__main__':
    main()
