"""Readonly knowledge hub data and employee roster snapshot."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from .app_bridge import bindings, readonly_snapshot, verified_session, session_read
from .config import load
from .knowledge import search
from .staff import display_name


def app_sessions():
    result = subprocess.run(['herdr-app', 'ls'], capture_output=True, text=True, timeout=5, check=True)
    value = json.loads(result.stdout)
    if not isinstance(value.get('spaces'), list):
        raise ValueError('App 세션 목록을 확인하지 못했습니다.')
    return value


def _all_units(config):
    ops = config.get('operations', {}) if isinstance(config.get('operations'), dict) else {}
    deps = ops.get('departments', {}) if isinstance(ops.get('departments'), dict) else {}
    teams = config.get('teams', {}) if isinstance(config.get('teams'), dict) else {}
    seen = set()
    units = []
    for uid in ('operations', 'eversafe', 'fakefinder', 'server', 'codemix', 'qa', 'docs', 'infra'):
        if uid in teams:
            units.append((uid, teams[uid]))
            seen.add(uid)
        elif uid in deps:
            units.append((uid, deps[uid]))
            seen.add(uid)
    for uid, policy in teams.items():
        if uid not in seen:
            units.append((uid, policy))
    return units


def _save_json(target, value):
    fd, temp = tempfile.mkstemp(prefix=f'.{target.name}-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(value, out, ensure_ascii=False)
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def snapshot(config_path, state, binding_path):
    config = load(config_path)
    linked = bindings(binding_path, config)
    spaces = app_sessions()['spaces']
    try:
        tasks = readonly_snapshot(state)['tasks']
    except (OSError, ValueError):
        tasks = []
    ops = config.get('operations', {}) if isinstance(config.get('operations'), dict) else {}
    aliases = ops.get('staff_aliases', {})
    spaces_cfg = ops.get('spaces', {})
    labels_cfg = ops.get('product_labels', {})
    units = _all_units(config)
    from . import progress
    homes = [Path(v['home']) for v in (config.get('profiles', {}).get('claude', {}) or {}).values()
             if isinstance(v, dict) and v.get('home')]
    claude_dirs = homes + [Path.home() / '.claude']
    steps_dir = ops.get('agent_progress_dir')
    now = time.time()
    cache_path = Path(state).resolve().parent / 'progress-cache.json'
    try:
        progress_cache = json.loads(cache_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        progress_cache = {}
    if not isinstance(progress_cache, dict):
        progress_cache = {}
    teams = []
    for team, policy in units:
        entry = (linked.get('teams', {}) or {}).get(team, {})
        target_space_id = entry.get('space') or spaces_cfg.get(team)
        space = next((item for item in spaces if item['id'] == target_space_id), None)
        if space is None:
            matches = [item for item in spaces if item.get('folder') == policy.get('root')]
            if len(matches) == 1:
                space = matches[0]
        employees = []
        seen = set()
        for tab in (space or {}).get('tabs', []):
            for item in tab.get('terminals', []):
                address = item.get('terminal')
                if not address or address in seen or item.get('app') not in ('codex', 'claude-code'):
                    continue
                seen.add(address)
                label = None
                verified_task = None
                s_info = {}
                try:
                    s_info = session_read(address)
                    lbls = s_info.get('labels') or {}
                    label = lbls.get('ui.display-name') or lbls.get('operations.display-name')
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
                for task in tasks:
                    if task.get('team') != team or (task.get('runtime') or {}).get('terminal') != address or not task.get('owner'):
                        continue
                    try:
                        verified, _status = verified_session(task, address)
                    except (OSError, ValueError, subprocess.SubprocessError):
                        continue
                    if verified:
                        verified_task = task
                        break
                provider = 'codex' if item['app'] == 'codex' else 'claude'
                account = (verified_task or {}).get('account')
                if (verified_task or {}).get('provider') != provider or account not in config.get('profiles', {}).get(provider, {}):
                    account = None
                    verified_task = None
                employees.append({
                    'terminal': address,
                    'provider': provider,
                    'account': account,
                    'role': (verified_task or {}).get('role'),
                    'label': label or f"{tab.get('name', '직원')} · {provider}",
                    'state': item.get('status', 'unknown'),
                    'identity_verified': verified_task is not None,
                    'progress': progress.observe(address, s_info.get('pid'), item['app'], item.get('status'),
                                                 claude_dirs=claude_dirs, steps_dir=steps_dir, now=now, cache=progress_cache)
                })
        terminal = entry.get('terminal')
        if terminal not in seen:
            terminal = employees[0]['terminal'] if employees else None
        selected = next((item for item in employees if item['terminal'] == terminal), {})
        team_label = (space or {}).get('name') or labels_cfg.get(team) or entry.get('label', team)
        teams.append({
            'id': team,
            'label': team_label,
            'space': (space or {}).get('id'),
            'terminal': terminal,
            'state': selected.get('state', '미확인'),
            'live': bool(employees),
            'identity': 'App 화면 기준 · 계정 신원은 별도 확인',
            'employees': employees,
            'roles': [{'role': role, 'provider': value.get('provider')} for role, value in policy.get('roles', {}).items()]
        })
    profiles = [{'provider': provider, 'account': account, 'label': display_name(provider, account, aliases=aliases)}
                for provider, accounts in config.get('profiles', {}).items() for account in accounts]
    progress_cache = {k: v for k, v in progress_cache.items() if os.path.exists(k.split('|', 1)[0])}
    _save_json(cache_path, progress_cache)
    result = {'observed_at': time.time(), 'teams': teams, 'profiles': profiles}
    target = Path(state).resolve().parent / 'knowledge-hub-roster.json'
    fd, temp = tempfile.mkstemp(prefix='.knowledge-hub-roster-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(result, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return result


def knowledge_root(config):
    ops = config.get('operations', {}) if isinstance(config.get('operations'), dict) else {}
    root = ops.get('knowledge_root')
    if not root:
        raise ValueError('config의 operations.knowledge_root(지식 문서 폴더)를 설정하세요.')
    return Path(root).expanduser()


def view(config_path, state, binding_path):
    from . import hq
    root = knowledge_root(load(config_path))
    knowledge = search(root)
    return {
        'roster': snapshot(config_path, state, binding_path),
        'knowledge': knowledge,
        'hq': hq.view(state, root, knowledge)
    }
