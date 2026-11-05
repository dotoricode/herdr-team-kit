"""Bounded, read-only context observations for an exact native session."""
from contextlib import closing
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat

TAIL_BYTES = 1024 * 1024
META_BYTES = 64 * 1024
SEARCH_ENTRIES = 10000
SEARCH_DEPTH = 6
CODEX_BASELINE_TOKENS = 12000


def _number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def context_percent(raw):
    """Normalize Claude's context percentage without consulting account usage."""
    window = raw.get('context_window') if isinstance(raw, dict) else None
    if not isinstance(window, dict):
        return None
    if 'used_percentage' in window:
        used = window['used_percentage']
    else:
        remaining = window.get('remaining_percentage')
        used = 100 - remaining if _number(remaining) else None
    return float(used) if _number(used) and 0 <= used <= 100 else None


def _safe_path(home, filename):
    path = Path(filename)
    if not path.is_absolute():
        path = home / path
    if '..' in path.parts:
        return None
    try:
        relative = path.relative_to(home)
        if not relative.parts or relative.parts[0] not in ('sessions', 'archived_sessions'):
            return None
        current = home
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return None
        if not stat.S_ISREG(path.stat().st_mode):
            return None
    except (OSError, ValueError):
        return None
    return path


def _rollout_path(home, session_id):
    database = home / 'state_5.sqlite'
    if database.is_symlink():
        return None
    if database.exists():
        # Immutable access never creates journal/SHM files. A recent WAL-only row
        # may be absent; the bounded filename lookup below remains authoritative.
        try:
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=0)) as db:
                row = db.execute('SELECT rollout_path FROM threads WHERE id = ?', (session_id,)).fetchone()
            if row:
                return _safe_path(home, row[0]) if isinstance(row[0], str) else None
        except sqlite3.Error:
            pass
    matches = []
    remaining = SEARCH_ENTRIES
    pending = [(home / name, 0) for name in ('sessions', 'archived_sessions')]
    while pending:
        directory, depth = pending.pop()
        if directory.is_symlink():
            return None
        if not directory.exists():
            continue
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    remaining -= 1
                    if remaining < 0:
                        return None
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if depth >= SEARCH_DEPTH:
                            return None
                        pending.append((Path(entry.path), depth + 1))
                    elif entry.name == session_id + '.jsonl' or entry.name.endswith('-' + session_id + '.jsonl'):
                        candidate = _safe_path(home, entry.path)
                        if candidate is not None:
                            matches.append(candidate)
                            if len(matches) > 1:
                                return None
        except OSError:
            return None
    return matches[0] if matches else None


def _signature(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        stamp = dt.timestamp() if dt.tzinfo is not None else None
        return stamp if _number(stamp) else None
    except (ValueError, OverflowError, OSError):
        return None


def _token_observation(record, session_id):
    info = record['payload'].get('info')
    if not isinstance(info, dict):
        return None
    usage = info.get('last_token_usage')
    used = usage.get('total_tokens') if isinstance(usage, dict) else None
    window = info.get('model_context_window')
    observed_at = _timestamp(record.get('timestamp'))
    if not _number(used) or not _number(window) or window <= CODEX_BASELINE_TOKENS or not 0 <= used <= window or observed_at is None:
        return None
    # Match TokenUsage::percent_of_context_window_remaining, including
    # Rust's positive half-away rounding (Python round uses ties-to-even):
    # https://github.com/openai/codex/blob/main/codex-rs/protocol/src/protocol.rs
    effective_window = window - CODEX_BASELINE_TOKENS
    effective_used = max(0, used - CODEX_BASELINE_TOKENS)
    remaining = max(0, effective_window - effective_used)
    remaining_percent = min(100.0, max(0.0, remaining / effective_window * 100.0))
    displayed_used = 100 - math.floor(remaining_percent + 0.5)
    return {'session_id': session_id, 'used': displayed_used,
            'observed_at': observed_at, 'source': 'codex-rollout'}


def codex_observation(home, session_id, expected_cwd, since=None):
    """Return the Codex display's used percentage and original event time.

    Windows at or below the fixed display baseline are unsupported and unknown.
    With since, preserve the latest valid 90% crossing after that event time.
    Original event timestamps are preserved; callers enforce freshness/future limits.
    Transcript text never leaves this function.
    """
    if since is not None and not _number(since):
        return None
    if not isinstance(session_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', session_id):
        return None
    if not isinstance(expected_cwd, (str, Path)) or not Path(expected_cwd).is_absolute():
        return None
    try:
        home = Path(home).absolute()
        if home.is_symlink():
            return None
        path = _rollout_path(home, session_id)
        if path is None:
            return None
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                return None
            first = stream.readline(META_BYTES + 1)
            if len(first) > META_BYTES or not first.endswith(b'\n'):
                return None
            meta = json.loads(first)
            payload = meta.get('payload') if isinstance(meta, dict) else None
            if (not isinstance(meta, dict) or meta.get('type') != 'session_meta' or not isinstance(payload, dict)
                    or payload.get('id') != session_id or payload.get('cwd') != str(expected_cwd)):
                return None
            offset = max(0, before.st_size - TAIL_BYTES)
            stream.seek(offset)
            data = stream.read(TAIL_BYTES)
            after = os.fstat(stream.fileno())
            if (_signature(before) != _signature(after)
                    or _safe_path(home, path) != path
                    or _signature(after) != _signature(path.stat())):
                return None
        if not data.endswith(b'\n'):
            return None
        lines = data.splitlines()
        if offset:
            lines = lines[1:]
        latest = None
        crossing = None
        for line in lines:
            record = json.loads(line)
            if not isinstance(record, dict):
                return None
            payload = record.get('payload')
            if record.get('type') == 'event_msg' and isinstance(payload, dict) and payload.get('type') == 'token_count':
                latest = _token_observation(record, session_id)
                if since is not None and latest is not None and latest['observed_at'] >= since and latest['used'] >= 90:
                    crossing = latest
        return (crossing or latest) if latest is not None else None

    except (OSError, ValueError, TypeError, OverflowError):
        return None
