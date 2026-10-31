"""Claude statusline quota anchored to an actual native response, never a redraw."""
from datetime import datetime
import hashlib
import json
import os
import stat
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

from . import providers
from .config import binding, load, profile
from .policy import CLAUDE_QUOTA_SOURCE, eligible, number, normalize_quota
from .runtime import profile_home_matches
from .store import Store

TAIL_BYTES = 1024 * 1024
USAGE_KEYS = ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')
SOURCE = CLAUDE_QUOTA_SOURCE


def usage_key(value):
    if not isinstance(value, dict):
        return None
    counts = tuple(value.get(key) for key in USAGE_KEYS)
    return counts if all(number(n) and n >= 0 for n in counts) else None


def observation(raw, pr, now, ttl):
    """Read only the exact session tail; message contents never leave this function."""
    if not isinstance(raw, dict):
        return None
    session = raw.get('session_id')
    filename = raw.get('transcript_path')
    context = raw.get('context_window')
    current = usage_key(context.get('current_usage')) if isinstance(context, dict) else None
    if not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', session) or not isinstance(filename, str) or current is None:
        return None
    path = Path(filename)
    if not path.is_absolute() or path.name != session + '.jsonl':
        return None
    path = path.resolve()
    if not path.is_relative_to((Path(pr['home'])/'projects').resolve()) or not path.is_file():
        return None
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or path.resolve() != path:
            return None
        size = stream.seek(0, 2)
        offset = max(0, size - TAIL_BYTES)
        stream.seek(offset)
        data = stream.read(TAIL_BYTES)
        after = os.fstat(stream.fileno())
        live = path.stat()
        signature = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        if signature(before) != signature(after) or signature(after) != signature(live):
            return None
    if not data.endswith(b'\n'):
        return None
    lines = data.splitlines(keepends=True)
    if offset and lines:
        lines = lines[1:]
    record = None
    for line in reversed(lines):
        if not line.endswith(b'\n'):
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            return None
        if not isinstance(row, dict):
            return None
        if row.get('type') == 'assistant':
            record = row
            break
    if not record or record.get('sessionId') != session or record.get('isSidechain') is True:
        return None
    message = record.get('message')
    if not isinstance(message, dict) or usage_key(message.get('usage')) != current:
        return None
    stamp = record.get('timestamp')
    if not isinstance(stamp, str):
        return None
    try:
        dt = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        at = dt.timestamp() if dt.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None
    if not number(at) or not 0 <= now - at <= ttl:
        return None
    rates = raw.get('rate_limits')
    result = normalize_quota('claude', {'rate_limits':rates if isinstance(rates, dict) else {}}, at)
    # Store only numeric quota fields, never arbitrary nested payload values.
    for key, span in (('five_hour', 5*3600), ('weekly', 7*24*3600)):
        window = result.get(key)
        if window:
            used, reset = window['used'], window['resets_at']
            if not number(used) or not 0 <= used <= 100 or not number(reset) or not at < reset <= at + span:
                result.pop(key)
    result.update(binding=binding(pr), source=SOURCE,
                  response_id=hashlib.sha256(json.dumps([session, record.get('uuid'), stamp, current]).encode()).hexdigest())
    return result


def merge_observation(previous, incoming):
    """A delayed sample cannot roll back time; ties can only reduce availability."""
    if not previous or previous.get('binding') != incoming['binding'] or previous.get('source') != SOURCE:
        return incoming
    old_at = previous.get('observed_at')
    if not number(old_at) or old_at < incoming['observed_at']:
        return incoming
    if old_at > incoming['observed_at']:
        return None
    merged = dict(previous)
    for key in ('five_hour', 'weekly'):
        old, new = previous.get(key), incoming.get(key)
        if not isinstance(old, dict) or not isinstance(new, dict) or old.get('resets_at') != new.get('resets_at') or not number(old.get('used')) or not number(new.get('used')):
            merged.pop(key, None)
        else:
            merged[key] = {**old, 'used':max(old['used'], new['used'])}
    return merged if merged != previous else None


def peek_quota(state, key):
    path = Path(state).expanduser().resolve()
    if not path.is_file():
        return None
    db = None
    try:
        db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=0.1)
        row = db.execute('SELECT body FROM quotas WHERE profile=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None
    except (sqlite3.Error, ValueError, OSError):
        return None
    finally:
        if db is not None:
            db.close()


def collect(config, state, account, raw):
    pr = profile(config, 'claude', account)
    if not profile_home_matches('claude', pr['home']):
        raise ValueError('statusline profile namespace mismatch')
    now = time.time()
    incoming = observation(raw, pr, now, config['quota_ttl_seconds'])
    if incoming is None:
        return {'status':'unavailable'}
    key = 'claude/'+account
    old = peek_quota(state, key)
    if merge_observation(old, incoming) is None:
        return {'status':'unchanged', 'quota':old}
    providers.identity('claude', pr, claude_timeout=2)
    # Identity latency never becomes a new observation timestamp.
    if not 0 <= time.time() - incoming['observed_at'] <= config['quota_ttl_seconds']:
        return {'status':'unavailable'}
    store = Store(state, timeout=1)
    try:
        with store.transaction():
            old = store.quota(key)
            merged = merge_observation(old, incoming)
            if merged is None:
                return {'status':'unchanged', 'quota':old}
            store.quota(key, merged)
            return {'status':'observed', 'quota':merged}
    finally:
        store.close()


def render(args):
    """The original renderer's stdin/stdout/exit status survive collector errors."""
    payload = sys.stdin.buffer.read()
    code = 0
    if args.display_command is not None:
        code = subprocess.run(args.display_command, shell=True, input=payload).returncode
    status = 'pending'
    try:
        config = load(args.config)
        result = collect(config, args.state, args.account, json.loads(payload))
        if eligible(result.get('quota'), time.time(), config['quota_ttl_seconds']):
            status = 'ready'
    except Exception:
        # Do not print exceptions: paths, identity or payload may be confidential.
        print('Team Kit quota unavailable', file=sys.stderr)
    if args.display_command is None:
        print('Team Kit quota: '+status)
    return code
