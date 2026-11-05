"""Read and append the local knowledge hub's Markdown documents."""
import argparse
import json
from pathlib import Path
import time


def search(root, query=''):
    root = Path(root).expanduser()
    if not root.is_dir():
        raise ValueError('지식 문서 폴더를 찾을 수 없습니다.')
    terms = query.casefold().split()
    entries = []
    for path in root.glob('*.md'):
        if path.name == 'README.md':
            continue
        text = path.read_text(encoding='utf-8')
        lines = text.splitlines()
        if not lines or not lines[0].startswith('# '):
            continue
        if not all(term in (path.stem + ' ' + text).casefold() for term in terms):
            continue
        entries.append({'team': path.stem, 'title': lines[0][2:].strip(), 'body': text,
                        'source': str(path), 'updated_at': path.stat().st_mtime})
    return sorted(entries, key=lambda item: (-item['updated_at'], item['title']))


def add_entry(root, team, title, content):
    root = Path(root).expanduser()
    if not root.is_dir():
        root.mkdir(parents=True, exist_ok=True)
    target = root / f'{team}.md'
    today = time.strftime('%Y-%m-%d')
    content = content.strip()
    if target.exists():
        existing = target.read_text(encoding='utf-8').rstrip()
        text = f"{existing}\n\n### {title} ({today})\n\n{content}\n"
    else:
        text = f"# {team.upper()} · {title}\n\n{content}\n\n기록일자: {today}\n"
    target.write_text(text, encoding='utf-8')
    return {'team': team, 'title': title, 'source': str(target), 'updated_at': target.stat().st_mtime}


def main():
    parser = argparse.ArgumentParser(description='로컬 지식 문서 조회 및 추가')
    parser.add_argument('--wiki', default=str(Path.home() / '00_work/05_docs/wiki'))
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
