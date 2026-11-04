"""Read the local knowledge hub's Markdown documents."""
import argparse
import json
from pathlib import Path


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
        entries.append({'team':path.stem, 'title':lines[0][2:].strip(), 'body':text,
                        'source':str(path), 'updated_at':path.stat().st_mtime})
    return sorted(entries, key=lambda item:(-item['updated_at'],item['title']))


def main():
    parser=argparse.ArgumentParser(description='로컬 지식 문서 조회')
    parser.add_argument('--wiki',default=str(Path.home()/'00_work/05_docs/wiki'))
    parser.add_argument('query',nargs='?',default='')
    args=parser.parse_args()
    print(json.dumps(search(args.wiki,args.query),ensure_ascii=False))


if __name__ == '__main__':
    main()
