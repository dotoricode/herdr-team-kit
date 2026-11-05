"""Headquarters boards: bounties, proposals, coffee notes and the shared goal brief.

Records live as JSON files next to the app state, so agents and the app share them.
Each write takes an exclusive lock and replaces the file atomically.
"""
import argparse
from contextlib import contextmanager
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

BOARDS = ('bounties', 'proposals', 'coffee')
PROPOSAL_STATES = ('대기', '채택', '보류')
COFFEE_KEEP = 50
COFFEE_TTL = 24 * 3600
TEAM_ID = re.compile(r'^[a-z0-9][a-z0-9-]{0,31}$')
LIMITS = {'title': 120, 'text': 2000, 'name': 60}


def _clean(value, field, limit, required=True, one_line=True):
    text = str(value or '').strip()
    if one_line:
        text = ' '.join(text.split())
    if required and not text:
        raise ValueError(f'{field}을(를) 입력하세요.')
    if len(text) > limit:
        raise ValueError(f'{field}은(는) {limit}자 이하여야 합니다.')
    return text


def team_id(value, required=False):
    text = str(value or '').strip()
    if not text and not required:
        return ''
    if not TEAM_ID.match(text):
        raise ValueError('부서 식별자는 영문 소문자·숫자·하이픈만 쓸 수 있습니다.')
    return text


def board_root(state):
    return Path(state).expanduser().resolve().parent / 'hq'


def _path(root, board):
    if board not in BOARDS:
        raise ValueError(f'알 수 없는 보드: {board}')
    return Path(root) / f'{board}.json'


def _read(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return []
    if not isinstance(value, list):
        raise ValueError(f'{path.name} 형식이 목록이 아닙니다.')
    return value


@contextmanager
def _locked(root, board):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = _path(root, board)
    with open(root / f'.{board}.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        items = _read(path)
        box = {'items': items}
        yield box
        fd, temp = tempfile.mkstemp(prefix=f'.{board}-', dir=root)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump(box['items'], out, ensure_ascii=False, indent=1)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)


def _find(items, item_id):
    item = next((x for x in items if x.get('id') == item_id), None)
    if item is None:
        raise ValueError(f'항목을 찾을 수 없습니다: {item_id}')
    return item


def _append_once(root, board, item, request_id, keep=None):
    """Add item unless this request_id was already stored: a retry after an unknown outcome never adds twice."""
    if request_id is not None:
        if not re.fullmatch(r'[0-9A-Za-z-]{1,64}', str(request_id)):
            raise ValueError('요청 ID 형식이 올바르지 않습니다.')
        item['request_id'] = str(request_id)
    with _locked(root, board) as box:
        if request_id is not None:
            done = next((x for x in box['items'] if x.get('request_id') == item['request_id']), None)
            if done is not None:
                # Compare only what the request supplied; status, claims and decisions change later.
                asked = [k for k in item if k not in ('id', 'created_at', 'at', 'request_id', 'status')]
                if any(done.get(k) != item[k] for k in asked):
                    raise ValueError('같은 요청이 다른 내용으로 이미 저장됐습니다. 새로고침 후 확인하세요.')
                return done
        box['items'] = box['items'] + [item]
        if keep:
            box['items'] = box['items'][-keep:]
    return item


def _new_id():
    return uuid.uuid4().hex[:8]


def add_bounty(root, title, detail='', team='', points=0, by='사용자', now=None, request_id=None):
    points = int(points or 0)
    if not 0 <= points <= 100000:
        raise ValueError('현상금은 0~100000 사이여야 합니다.')
    item = {'id': _new_id(), 'title': _clean(title, '제목', LIMITS['title']),
            'detail': _clean(detail, '내용', LIMITS['text'], required=False, one_line=False),
            'team': team_id(team), 'points': points, 'status': 'open',
            'created_by': _clean(by, '등록자', LIMITS['name']), 'created_at': now or time.time()}
    return _append_once(root, 'bounties', item, request_id)


def claim_bounty(root, item_id, by, now=None):
    with _locked(root, 'bounties') as box:
        item = _find(box['items'], item_id)
        if item['status'] != 'open':
            raise ValueError('열려 있는 현상금만 맡을 수 있습니다.')
        item.update(status='claimed', claimed_by=_clean(by, '담당자', LIMITS['name']), claimed_at=now or time.time())
    return item


def resolve_bounty(root, item_id, by, note, now=None):
    with _locked(root, 'bounties') as box:
        item = _find(box['items'], item_id)
        if item['status'] == 'resolved':
            raise ValueError('이미 해결된 현상금입니다.')
        item.update(status='resolved', resolved_by=_clean(by, '해결자', LIMITS['name']),
                    resolution=_clean(note, '해결 기록', LIMITS['text'], one_line=False), resolved_at=now or time.time())
    return item


def release_bounty(root, item_id):
    with _locked(root, 'bounties') as box:
        item = _find(box['items'], item_id)
        if item['status'] != 'claimed':
            raise ValueError('맡은 현상금만 내려놓을 수 있습니다.')
        item['status'] = 'open'
        item.pop('claimed_by', None)
        item.pop('claimed_at', None)
    return item


def add_proposal(root, title, content, metric='', hypothesis='', team='', by='사용자', now=None, request_id=None):
    item = {'id': _new_id(), 'title': _clean(title, '제목', LIMITS['title']),
            'content': _clean(content, '내용', LIMITS['text'], one_line=False),
            'metric': _clean(metric, '기여 지표', LIMITS['title'], required=False),
            'hypothesis': _clean(hypothesis, '가설', LIMITS['text'], required=False, one_line=False),
            'team': team_id(team), 'status': '대기',
            'proposed_by': _clean(by, '제안자', LIMITS['name']), 'created_at': now or time.time()}
    return _append_once(root, 'proposals', item, request_id)


def decide_proposal(root, item_id, status, note='', now=None):
    if status not in PROPOSAL_STATES:
        raise ValueError(f'상태는 {", ".join(PROPOSAL_STATES)} 중 하나여야 합니다.')
    with _locked(root, 'proposals') as box:
        item = _find(box['items'], item_id)
        item.update(status=status, decision_note=_clean(note, '결정 메모', LIMITS['text'], required=False, one_line=False),
                    decided_at=now or time.time())
    return item


def post_coffee(root, text, team='', by='사용자', now=None, request_id=None):
    item = {'id': _new_id(), 'text': _clean(text, '근황', 200), 'team': team_id(team),
            'by': _clean(by, '작성자', LIMITS['name']), 'at': now or time.time()}
    return _append_once(root, 'coffee', item, request_id, keep=COFFEE_KEEP)


def _cells(line):
    return [cell.strip() for cell in line.strip().strip('|').split('|')]


def _table(lines):
    rows = [_cells(line) for line in lines if line.strip().startswith('|')]
    rows = [row for row in rows if not all(set(cell) <= set('-: ') for cell in row)]
    if len(rows) < 2:
        return []
    head = rows[0]
    return [dict(zip(head, row)) for row in rows[1:] if any(row)]


def read_goal(knowledge_root):
    """Parse GOAL-BRIEF.md (the goal-setting skill's template). None when absent."""
    path = Path(knowledge_root).expanduser() / 'GOAL-BRIEF.md'
    if not path.is_file():
        return None
    text = path.read_text(encoding='utf-8')
    name = re.search(r'^##\s*목표 브리프:\s*(.+)$', text, re.M)
    sentence = re.search(r'\*\*목표 문장\*\*\s*:\s*(.+)$', text, re.M)
    sections = {}
    current = None
    for line in text.splitlines():
        heading = re.match(r'^###\s+(.+?)\s*(\(.*\))?\s*$', line)
        if heading:
            current = heading.group(1).strip()
            sections[current] = []
        elif current:
            sections[current].append(line)
    bullets = lambda key: [line.strip()[2:].strip() for line in sections.get(key, []) if line.strip().startswith('- ')]
    return {'name': name.group(1).strip() if name else '이름 없음',
            'sentence': sentence.group(1).strip() if sentence else '',
            'metrics': _table(sections.get('성공 지표', [])),
            'levers': _table(sections.get('레버', [])),
            'guardrails': _table(sections.get('가드레일', [])),
            'donts': bullets('하지 말 것'),
            'source': str(path), 'updated_at': path.stat().st_mtime}


def daily_tip(entries, today=None):
    """One knowledge section per day, the same all day."""
    tips = []
    for entry in entries:
        parts = re.split(r'^###\s+', entry.get('body', ''), flags=re.M)
        head = parts[0].split('\n', 1)
        tips.append({'team': entry['team'], 'title': entry['title'], 'body': head[1].strip() if len(head) > 1 else ''})
        for part in parts[1:]:
            title, _, body = part.partition('\n')
            tips.append({'team': entry['team'], 'title': title.strip(), 'body': body.strip()})
    tips = [tip for tip in tips if tip['body']]
    if not tips:
        return None
    tips.sort(key=lambda tip: (tip['team'], tip['title']))
    day = (today or datetime.date.today()).toordinal()
    return tips[day % len(tips)]


def view(state, knowledge_root=None, entries=(), now=None):
    root = board_root(state)
    now = now or time.time()
    coffee = [item for item in _read(_path(root, 'coffee')) if now - item.get('at', 0) <= COFFEE_TTL]
    return {'bounties': sorted(_read(_path(root, 'bounties')), key=lambda x: -x.get('created_at', 0)),
            'proposals': sorted(_read(_path(root, 'proposals')), key=lambda x: -x.get('created_at', 0)),
            'coffee': sorted(coffee, key=lambda x: -x.get('at', 0)),
            'goal': read_goal(knowledge_root) if knowledge_root else None,
            'tip': daily_tip(entries)}


def apply(state, action):
    """One mutation from the app: {"op": ..., ...fields}."""
    root = board_root(state)
    op = action.get('op')
    fields = {key: value for key, value in action.items() if key != 'op'}
    handlers = {'bounty-add': add_bounty, 'bounty-claim': claim_bounty, 'bounty-resolve': resolve_bounty,
                'bounty-release': release_bounty, 'propose': add_proposal, 'proposal-decide': decide_proposal,
                'coffee': post_coffee}
    if op not in handlers:
        raise ValueError(f'알 수 없는 작업: {op}')
    return handlers[op](root, **fields)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python3 -m teamkit.hq', description='종합상황실 현상금·제안함·커피타임')
    parser.add_argument('--state', default=str(Path.home() / '.local/state/herdr-team-kit-app/state.sqlite'))
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('list', help='모든 보드 출력')
    p = sub.add_parser('bounty-add'); p.add_argument('--title', required=True); p.add_argument('--detail', default='')
    p.add_argument('--team', default=''); p.add_argument('--points', type=int, default=0); p.add_argument('--by', default='사용자')
    p = sub.add_parser('bounty-claim'); p.add_argument('id'); p.add_argument('--by', required=True)
    p = sub.add_parser('bounty-release'); p.add_argument('id')
    p = sub.add_parser('bounty-resolve'); p.add_argument('id'); p.add_argument('--by', required=True); p.add_argument('--note', required=True)
    p = sub.add_parser('propose'); p.add_argument('--title', required=True); p.add_argument('--content', required=True)
    p.add_argument('--metric', default=''); p.add_argument('--hypothesis', default=''); p.add_argument('--team', default=''); p.add_argument('--by', required=True)
    p = sub.add_parser('coffee'); p.add_argument('text'); p.add_argument('--team', default=''); p.add_argument('--by', required=True)
    args = parser.parse_args(argv)
    root = board_root(args.state)
    try:
        if args.cmd == 'list':
            result = view(args.state)
        elif args.cmd == 'bounty-add':
            result = add_bounty(root, args.title, args.detail, args.team, args.points, args.by)
        elif args.cmd == 'bounty-claim':
            result = claim_bounty(root, args.id, args.by)
        elif args.cmd == 'bounty-release':
            result = release_bounty(root, args.id)
        elif args.cmd == 'bounty-resolve':
            result = resolve_bounty(root, args.id, args.by, args.note)
        elif args.cmd == 'propose':
            result = add_proposal(root, args.title, args.content, args.metric, args.hypothesis, args.team, args.by)
        else:
            result = post_coffee(root, args.text, args.team, args.by)
    except ValueError as error:
        parser.exit(2, f'{error}\n')
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
