"""What each staff session is doing now, read from its own transcript.

Claude's desktop app shows progress by replaying tool calls from the conversation:
a todo list when the agent keeps one, otherwise the current tool ("Running a command").
Staff here rarely keep todo lists, so this reports the latest tool call and its age for
every linked session, plus explicit steps when the agent records them with the
`agent-progress` CLI or a TodoWrite/TaskCreate/TaskUpdate list.

A session is linked only when its transcript is proven: Claude through
`<config>/sessions/<pid>.json`, Codex through the rollout file its process holds open.
Unlinked sessions are reported as such, never guessed.
"""
import datetime
import json
import os
from pathlib import Path
import re
import subprocess

TAIL_BYTES = 262144
STALL_S = 600
TODO_TOOLS = ('TodoWrite', 'TaskCreate', 'TaskUpdate')


def descendants(pid, depth=4):
    found = [pid]
    if depth:
        try:
            out = subprocess.run(['pgrep', '-a', '-P', str(pid)], capture_output=True, text=True, timeout=2).stdout
        except (OSError, subprocess.SubprocessError):
            return found
        for child in out.split():
            if child.isdigit():
                found += descendants(int(child), depth - 1)
    return found


def open_files(pid):
    try:
        out = subprocess.run(['lsof', '-p', str(pid), '-Fn'], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [line[1:] for line in out.splitlines() if line.startswith('n')]


def claude_transcript(pids, config_dirs):
    for pid in pids:
        for base in config_dirs:
            meta = Path(base).expanduser() / 'sessions' / f'{pid}.json'
            try:
                info = json.loads(meta.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            sid, cwd = info.get('sessionId'), info.get('cwd')
            if not isinstance(sid, str) or not re.fullmatch(r'[0-9a-f-]{8,64}', sid):
                continue
            projects = Path(base).expanduser() / 'projects'
            if isinstance(cwd, str):
                path = projects / re.sub(r'[^A-Za-z0-9]', '-', cwd) / f'{sid}.jsonl'
                if path.is_file():
                    return path
            matches = list(projects.glob(f'*/{sid}.jsonl'))
            if len(matches) == 1:
                return matches[0]
    return None


def codex_transcript(pids, files=open_files):
    for pid in pids:
        for name in files(pid):
            if re.search(r'/rollout-[^/]*\.jsonl$', name):
                return Path(name)
    return None


def _tail_records(path, limit=None):
    limit = limit or TAIL_BYTES
    with open(path, 'rb') as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - limit))
        data = handle.read().decode('utf-8', errors='replace')
    lines = data.splitlines()
    if size > limit:
        lines = lines[1:]
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def _seconds(stamp):
    try:
        return datetime.datetime.fromisoformat(str(stamp).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def _short(text, limit=90):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


CLAUDE_LABELS = {'Bash': '명령 실행', 'Edit': '파일 수정', 'Write': '파일 작성', 'Read': '파일 읽기',
                 'Grep': '검색', 'Glob': '파일 찾기', 'Agent': '하위 에이전트', 'Task': '하위 에이전트',
                 'WebFetch': '웹 조회', 'WebSearch': '웹 검색', 'Skill': '스킬 실행', 'AskUserQuestion': '사용자 질문 대기'}


def _tool_label(name):
    name = str(name)
    if name.startswith('mcp__'):
        parts = name.split('__')
        return f"{parts[-1]} ({parts[1]})" if len(parts) >= 3 else name
    return CLAUDE_LABELS.get(name, name)


def _claude_detail(name, given):
    given = given if isinstance(given, dict) else {}
    for key in ('description', 'file_path', 'pattern', 'command', 'prompt', 'query', 'skill', 'url'):
        if given.get(key):
            value = str(given[key])
            return Path(value).name if key == 'file_path' else _short(value)
    return ''


def claude_activity(records):
    """Latest top-level tool call, and the todo list if the session keeps one."""
    todos, failed, activity, created = {}, set(), None, {}
    for rec in records:
        content = (rec.get('message') or {}).get('content')
        if rec.get('isSidechain') or not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get('type') == 'tool_result' and part.get('is_error'):
                failed.add(part.get('tool_use_id'))
    for rec in records:
        content = (rec.get('message') or {}).get('content')
        if rec.get('isSidechain') or not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get('type') == 'tool_result' and part.get('tool_use_id') in created:
                # TaskCreate's result names the task id that later TaskUpdate calls use.
                use_id = part['tool_use_id']
                real = _created_task_id(rec, part)
                if real and use_id in todos and real not in todos:
                    todos = {(real if k == use_id else k): v for k, v in todos.items()}
                created.pop(use_id, None)
            if rec.get('type') != 'assistant' or part.get('type') != 'tool_use':
                continue
            name, given = part.get('name'), part.get('input') or {}
            if name in TODO_TOOLS and part.get('id') not in failed:
                _apply_todo(todos, name, given, part.get('id'))
                if name == 'TaskCreate' and part.get('id'):
                    created[part['id']] = True
            activity = {'label': _tool_label(name), 'detail': _claude_detail(name, given),
                        'at': _seconds(rec.get('timestamp'))}
    return activity, list(todos.values())


def _resets_todos(records):
    """Whether the records hold a TodoWrite that claude_activity applies (parent session, not failed)."""
    failed = {part.get('tool_use_id') for rec in records if not rec.get('isSidechain')
              for part in ((rec.get('message') or {}).get('content') or [])
              if isinstance(part, dict) and part.get('type') == 'tool_result' and part.get('is_error')}
    return any(part.get('type') == 'tool_use' and part.get('name') == 'TodoWrite' and part.get('id') not in failed
               for rec in records if rec.get('type') == 'assistant' and not rec.get('isSidechain')
               for part in ((rec.get('message') or {}).get('content') or []) if isinstance(part, dict))


def _created_task_id(rec, part):
    result = rec.get('toolUseResult') or rec.get('tool_use_result') or {}
    task = result.get('task') if isinstance(result, dict) else None
    if isinstance(task, dict) and task.get('id') is not None:
        return str(task['id'])
    content = part.get('content')
    text = content if isinstance(content, str) else ' '.join(
        str(x.get('text', '')) for x in content if isinstance(x, dict)) if isinstance(content, list) else ''
    found = re.search(r'Task #([0-9A-Za-z-]+)', text)
    return found.group(1) if found else None


def _status(value):
    return value if value in ('pending', 'in_progress', 'completed') else 'pending'


def _apply_todo(todos, name, given, use_id):
    if name == 'TodoWrite':
        todos.clear()
        for index, item in enumerate(given.get('todos') or []):
            if isinstance(item, dict) and item.get('content'):
                key = str(item.get('id') or f'todo-{index}')
                todos[key] = {'content': _short(item['content']), 'status': _status(item.get('status')),
                              'active': _short(item.get('activeForm') or '')}
    elif name == 'TaskCreate' and given.get('subject'):
        key = str(use_id or f'task-{len(todos)}')
        todos[key] = {'content': _short(given['subject']), 'status': 'pending', 'active': _short(given.get('activeForm') or '')}
    elif name == 'TaskUpdate' and given.get('taskId'):
        key = str(given['taskId'])
        if given.get('status') == 'deleted':
            todos.pop(key, None)
            return
        old = todos.get(key, {'content': _short(given.get('subject') or f'작업 {key[:8]}'), 'status': 'pending', 'active': ''})
        todos[key] = {'content': _short(given.get('subject') or old['content']),
                      'status': _status(given.get('status')) if given.get('status') else old['status'],
                      'active': _short(given.get('activeForm') or old['active'])}


def codex_activity(records):
    activity = None
    for rec in records:
        payload = rec.get('payload') or {}
        item = payload.get('item') or {}
        at = _seconds(rec.get('timestamp'))
        if payload.get('type') in ('item_started', 'item_completed') and item.get('type') == 'CommandExecution':
            command = item.get('command') or []
            text = command[-1] if isinstance(command, list) and command else command
            activity = {'label': '명령 실행', 'detail': _short(text), 'at': at}
        elif payload.get('type') in ('function_call', 'custom_tool_call') and payload.get('name') != 'exec':
            activity = {'label': str(payload.get('name')), 'detail': '', 'at': at}
        elif payload.get('type') == 'item_completed' and item.get('type') == 'AgentMessage':
            activity = {'label': '답변 작성', 'detail': '', 'at': at}
    return activity, []


def recorded_steps(directory, terminal):
    """Steps an agent recorded itself with the agent-progress CLI."""
    if not directory or not re.fullmatch(r't[0-9a-z]+', str(terminal or '')):
        return None
    try:
        data = json.loads((Path(directory).expanduser() / f'{terminal}.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    steps = [s for s in data.get('steps') or [] if isinstance(s, dict) and s.get('name')]
    if not steps:
        return None
    return {'title': _short(data.get('title') or ''),
            'items': [{'content': _short(s['name']), 'status': _status(s.get('status')), 'active': ''} for s in steps]}


def _summary(items, title=''):
    if not items:
        return None
    done = sum(1 for x in items if x['status'] == 'completed')
    current = next((x for x in items if x['status'] == 'in_progress'), None)
    return {'title': title, 'total': len(items), 'done': done, 'items': items,
            'current': (current['active'] or current['content']) if current else ''}


FULL_BYTES = 32 * 1024 * 1024
TODO_MARKS = tuple(f'"name":"{name}"'.encode() for name in TODO_TOOLS) + tuple(f'"name": "{name}"'.encode() for name in TODO_TOOLS)


def full_todos(path, cache=None):
    """Replay the newest FULL_BYTES of the transcript for its todo list: lists can be older
    than the tail. Cached by path, size and mtime so an unchanged file is not read again."""
    stat = path.stat()
    key = f'{path}|{stat.st_size}|{stat.st_mtime_ns}'
    if cache is not None and key in cache:
        return cache[key]
    with open(path, 'rb') as handle:
        handle.seek(max(0, stat.st_size - FULL_BYTES))
        data = handle.read()
    todos = []
    if any(mark in data for mark in TODO_MARKS):
        records = []
        lines = data.decode('utf-8', errors='replace').splitlines()
        for line in (lines[1:] if stat.st_size > FULL_BYTES else lines):
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        todos = claude_activity(records)[1]
    if cache is not None:
        for old in [k for k in cache if k.startswith(f'{path}|')]:
            cache.pop(old)
        cache[key] = todos
    return todos


def observe(terminal, pid, app, state, *, claude_dirs=(), steps_dir=None, now, cache=None, tree=descendants, files=open_files):
    """Progress for one staff terminal. `state` is the app's working/idle/... status."""
    pids = tree(pid) if isinstance(pid, int) else []
    path = claude_transcript(pids, claude_dirs) if app == 'claude-code' else codex_transcript(pids, files)
    activity, todos, changed = None, [], None
    if path:
        try:
            read = claude_activity if app == 'claude-code' else codex_activity
            tail = _tail_records(path)
            activity, todos = read(tail)
            # A TodoWrite in the tail replaces the whole list, so the tail alone is current.
            tail_resets = app == 'claude-code' and _resets_todos(tail)
            if activity is None and path.stat().st_size > TAIL_BYTES:
                activity, todos = read(_tail_records(path, TAIL_BYTES * 8))
            if app == 'claude-code' and not tail_resets and path.stat().st_size > TAIL_BYTES:
                todos = full_todos(path, cache)
            changed = path.stat().st_mtime
        except OSError:
            path, changed = None, None
    recorded = recorded_steps(steps_dir, terminal)
    steps = _summary(recorded['items'], recorded['title']) if recorded else _summary(todos)
    last = max([x for x in ((activity or {}).get('at'), changed if path else None) if x] or [0]) or None
    return {'linked': bool(path), 'source': 'claude' if app == 'claude-code' else 'codex',
            'activity': activity, 'steps': steps, 'last_change': last,
            'stalled': bool(path and state == 'working' and last and now - last > STALL_S)}
