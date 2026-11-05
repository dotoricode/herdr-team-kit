"""Read and append the local knowledge hub's Markdown documents."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time


def search(root, query=''):
    root = Path(root).expanduser()
    if not root.is_dir():
        raise ValueError('지식 문서 폴더를 찾을 수 없습니다.')
    terms = query.casefold().split()
    entries = []
    for path in root.glob('*.md'):
        # Dot files are add_entry's in-flight temporaries; a document replaced mid-scan is read next time.
        if path.name == 'README.md' or path.name.startswith('.'):
            continue
        try:
            text = REQUEST_MARK.sub('', path.read_text(encoding='utf-8'))
            updated_at = path.stat().st_mtime
        except FileNotFoundError:
            continue
        lines = text.splitlines()
        if not lines or not lines[0].startswith('# '):
            continue
        if not all(term in (path.stem + ' ' + text).casefold() for term in terms):
            continue
        entries.append({'team': path.stem, 'title': lines[0][2:].strip(), 'body': text,
                        'source': str(path), 'updated_at': updated_at})
    return sorted(entries, key=lambda item: (-item['updated_at'], item['title']))


REQUEST_MARK = re.compile(r'^<!-- request:[0-9A-Za-z-]{1,64}(?::[0-9a-f]{12})? -->\n?', re.M)


def add_entry(root, team, title, content, request_id=None):
    """Append one note under a lock; a repeated request_id returns without writing again."""
    from .hq import team_id, _clean
    team = team_id(team, required=True)
    title = _clean(title, '제목', 120)
    content = _clean(content, '내용', 2000, required=False, one_line=False)
    if request_id is not None and not re.fullmatch(r'[0-9A-Za-z-]{1,64}', str(request_id)):
        raise ValueError('요청 ID 형식이 올바르지 않습니다.')
    root = Path(root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    target = root / f'{team}.md'
    with open(root / '.knowledge.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.is_symlink() or (target.exists() and target.resolve().parent != root.resolve()):
            raise ValueError('지식 문서가 링크이거나 지식 폴더 밖에 있어 쓰지 않습니다.')
        existing = target.read_text(encoding='utf-8').rstrip() if target.exists() else ''
        digest = hashlib.sha256(f'{team}\0{title}\0{content}'.encode()).hexdigest()[:12]
        mark = f'<!-- request:{request_id}:{digest} -->' if request_id else ''
        legacy = f'<!-- request:{request_id} -->' if request_id else ''
        if mark and (mark in existing or legacy in existing):
            return {'team': team, 'title': title, 'source': str(target), 'updated_at': target.stat().st_mtime, 'repeated': True}
        if mark and f'<!-- request:{request_id}:' in existing:
            raise ValueError('같은 요청이 다른 내용으로 이미 저장됐습니다. 새로고침 후 확인하세요.')
        today = time.strftime('%Y-%m-%d')
        tail = f'\n{mark}' if mark else ''
        if existing:
            text = f"{existing}\n\n### {title} ({today})\n\n{content}{tail}\n"
        else:
            text = f"# {team.upper()} · {title}\n\n{content}\n\n기록일자: {today}{tail}\n"
        fd, temp = tempfile.mkstemp(prefix=f'.{team}-', suffix='.tmp', dir=root)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                out.write(text)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, target)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    return {'team': team, 'title': title, 'source': str(target), 'updated_at': target.stat().st_mtime}


def main():
    parser = argparse.ArgumentParser(description='로컬 지식 문서 조회 및 추가')
    parser.add_argument('--wiki', required=True, help='지식 문서 폴더')
    parser.add_argument('--add', action='store_true', help='새 주의사항 추가')
    parser.add_argument('--team', default='', help='대상 부서/팀 식별자')
    parser.add_argument('--title', default='', help='주의사항 제목')
    parser.add_argument('--content', default='', help='주의사항 내용')
    parser.add_argument('query', nargs='?', default='')
    args = parser.parse_args()
    if args.add:
        if not args.team or not args.title:
            parser.error('--add 시에는 --team과 --title이 필수입니다.')
        result = add_entry(args.wiki, args.team, args.title, args.content or args.query)
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(json.dumps(search(args.wiki, args.query), ensure_ascii=False))


if __name__ == '__main__':
    main()
