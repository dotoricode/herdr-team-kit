"""Inspect existing ledgers without write initialization or migrations."""
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3


def record(body, label):
    try:
        value = json.loads(body)
    except (ValueError, TypeError) as error:
        raise ValueError('invalid '+label+' record in task ledger') from error
    if not isinstance(value, dict):
        raise ValueError('invalid '+label+' record in task ledger')
    return value


def read(args):
    path = Path(args.state).expanduser().resolve()
    if args.command == 'work' and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', args.work):
        raise ValueError('invalid product work ID')
    if not path.is_file():
        raise ValueError('task ledger does not exist')
    try:
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=1)) as db:
            db.execute('BEGIN')
            if args.command in ('show', 'work'):
                work = args.command == 'work'
                table, label, ident = ('work_items', 'product work', args.work) if work else ('tasks', 'task', args.task)
                if work and not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_items'").fetchone():
                    raise ValueError('unknown product work')
                row = db.execute('SELECT body FROM '+table+' WHERE id=?', (ident,)).fetchone()
                if not row:
                    raise ValueError('unknown '+label)
                return record(row[0], label)
            if args.command == 'list':
                return [record(row[0], 'task') for row in db.execute('SELECT body FROM tasks ORDER BY id')]
            return [dict(seq=row[0], event=row[1], at=row[2], detail=record(row[3], 'event')) for row in
                    db.execute('SELECT seq,event,at,body FROM events WHERE task=? ORDER BY seq', (args.task,))]
    except sqlite3.Error as error:
        raise ValueError('cannot read task ledger: '+str(error)) from error
