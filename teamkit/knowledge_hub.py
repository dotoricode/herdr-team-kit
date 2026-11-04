"""Read knowledge and refresh a local, observational staff roster."""
import json
import argparse
import os
import subprocess
from pathlib import Path
import tempfile
import time

from .app_bridge import bindings, core_read, readonly_snapshot, verified_session
from .config import load
from .knowledge import search
from .staff import display_name


def snapshot(config_path, state, binding_path):
    config = load(config_path)
    linked = bindings(binding_path, config)
    observed = core_read()['terminals']
    terminals = {item['address']:item for item in observed}
    tasks = readonly_snapshot(state)['tasks']
    aliases = config.get('operations', {}).get('staff_aliases', {})
    teams = []
    for team, policy in config['teams'].items():
        entry = linked['teams'][team]
        terminal = entry.get('terminal')
        native = terminals.get(terminal, {})
        employees=[]
        for task in tasks:
            runtime=task.get('runtime') or {}; address=runtime.get('terminal')
            if task.get('team')!=team or not address or not task.get('owner') or address not in terminals:continue
            try:verified,status=verified_session(task,address)
            except (OSError,ValueError,subprocess.SubprocessError):continue
            if not verified:continue
            provider,account=task.get('provider'),task.get('account')
            if account not in config['profiles'].get(provider,{}):continue
            employees.append({'role':task.get('role'),'provider':provider,'account':account,'terminal':address,
                'label':display_name(provider,account,product=team,role=task.get('role'),aliases=aliases),
                'state':status.get('state','unknown')})
        teams.append({'id':team, 'label':entry.get('label',team), 'space':entry.get('space'),
                      'terminal':terminal, 'state':native.get('status','미확인'),
                      'live':native.get('state') == 'live',
                      'identity':'세션 신원 확인됨' if any(x['terminal']==terminal for x in employees) else '계정·직무 신원 미확인',
                      'employees':employees,
                      'roles':[{'role':role,'provider':value.get('provider')} for role,value in policy['roles'].items()]})
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
