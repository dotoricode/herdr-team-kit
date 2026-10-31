"""Explicit host retirement of an owned transient workspace, preserving files."""
import os
import time

from .runtime import Herdr


def retire(store,config,task,owner,evidence,stopped):
    value=store.get(task);runtime=value.get('runtime',{})
    if not stopped or not isinstance(evidence,dict) or not isinstance(evidence.get('verification'),list) or not evidence['verification'] or not all(isinstance(x,str) and x.strip() for x in evidence['verification']):
        raise ValueError('retirement requires stopped-writer verification evidence')
    if value.get('owner')!=owner or value['state'] not in ('ready','awaiting_review'):
        raise ValueError('only the current stopped ready/review owner can retire')
    if not runtime.get('managed_workspace') or not runtime.get('ephemeral') or runtime.get('retire_requested'):
        raise ValueError('workspace is persistent, unowned or already retiring; inspect')
    workspace=runtime.get('workspace');protected={x['id'] for x in config.get('dashboard_workspaces',{}).values()}
    if not workspace or workspace in protected or workspace==os.environ.get('HERDR_WORKSPACE_ID'):
        raise ValueError('protected or current workspace cannot retire')
    native=Herdr();prefix=['--session',config['herdr_session']] if config.get('herdr_session') else []
    current=native.call(*prefix,'agent','get',runtime['name'])['agent']
    if current.get('agent_status') not in ('idle','done') or not current.get('interactive_ready') or current.get('pane_id')!=runtime.get('pane') or current.get('workspace_id')!=workspace:
        raise ValueError('native owner is not settled in the owned workspace')
    details=native.call(*prefix,'workspace','get',workspace)['workspace']
    if details.get('label')!=runtime.get('workspace_label') or details.get('pane_count')!=1 or details.get('tab_count')!=1:
        raise ValueError('workspace identity/layout changed; inspect instead of closing')
    foreground=native.call(*prefix,'pane','process-info','--pane',runtime['pane'])['process_info']['foreground_processes']
    if len(foreground)!=1 or foreground[0].get('pid')!=runtime.get('native_pid') or foreground[0].get('name')!=value['provider']:
        raise ValueError('native foreground process changed; preserve the workspace')
    latest=native.call(*prefix,'agent','get',runtime['name'])['agent']
    if latest!=current:raise ValueError('native activity changed before retirement')
    with store.transaction():
        if store.get(task)!=value:raise ValueError('task changed before retirement')
        value['runtime']['retire_requested']=True
        store._save(value,'workspace_retire_requested',{'workspace':workspace,'verification':evidence['verification']})
    # A timeout is not retried: the persisted intent requires explicit inspection.
    native.call(*prefix,'workspace','close',workspace)
    with store.transaction():
        if store.get(task)!=value:raise ValueError('workspace closed but task changed; inspect')
        value['runtime'].update(workspace_closed=True,retired_at=time.time())
        return store._save(value,'workspace_retired',{'workspace':workspace,'files_preserved':True})
