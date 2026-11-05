#!/usr/bin/env python3
"""Render team-operations with sample data through `herdr-app check`.

`herdr-app check` draws a mod once, before its async data arrives, so code that
only runs with data (cards, tabs, dialogs) is never drawn. This writes variants
of the mod with a sample payload or dialog props already in place and checks each.
It also flags element-chain methods that gpui-kit does not declare.

Usage: check_team_operations.py <fixture.json> [--types ~/.config/herdr-app]
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MOD = ROOT / 'app-mods/team-operations'
INIT_MARK = '    this.seenRev = shared(REV);\n'
PROPS_MARK = 'const p = props() ?? {};'


def variants(data):
    teams = [{'id': t['id'], 'label': t['label']} for t in data['roster']['teams']]
    employee = dict(next((e for t in data['roster']['teams'] for e in t['employees']), {'label': '직원'}))
    steps = [{'content': '앱 실행', 'status': 'completed', 'active': ''}, {'content': '화면 비교', 'status': 'in_progress', 'active': '화면 비교 중'},
             {'content': '보고', 'status': 'pending', 'active': ''}]
    employee['progress'] = {'linked': True, 'source': 'claude', 'stalled': True, 'last_change': 1.0,
                            'activity': {'label': '명령 실행', 'detail': 'Run unit tests', 'at': 1.0},
                            'steps': {'title': 'UI 검증', 'total': 3, 'done': 1, 'current': '화면 비교 중', 'items': steps}}
    first = data['roster']['teams'][0]['employees'] if data['roster']['teams'] and data['roster']['teams'][0]['employees'] else None
    if first:
        first[0] = employee
    bounty = next((b for b in data['hq']['bounties'] if b['status'] == 'claimed'), {'id': 'x', 'title': '난제'})
    tabs = ('progress', 'floor', 'goal', 'bounty')
    pages = {f'page-{tab}': {'tab': tab, 'data': data} for tab in tabs}
    empty = json.loads(json.dumps(data))
    empty['hq'] = {'bounties': [], 'proposals': [], 'coffee': [], 'goal': None, 'tip': None}
    empty['knowledge'] = []
    pages.update({f'empty-{tab}': {'tab': tab, 'data': empty} for tab in tabs})
    pages['loading'] = {'tab': 'floor', 'data': None}
    dialogs = {f'dialog-{role}': {'props': {'role': role, 'teams': teams, **extra}} for role, extra in (
        ('badge', {'employee': {**employee, 'teamLabel': teams[0]['label'] if teams else ''}}),
        ('gotcha', {}), ('bounty', {}), ('coffee', {}), ('resolve', {'item': bounty}),
        ('doc', {'doc': {'title': '문서', 'body': '# 제목\n\n본문\n\n### 최근 항목 (2026-11-05)\n\n내용'}}), ('unknown', {}))}
    return {**pages, **dialogs}


def build(source, spec):
    if 'props' in spec:
        assert PROPS_MARK in source
        return source.replace(PROPS_MARK, f'const p = {json.dumps(spec["props"], ensure_ascii=False)};')
    assert INIT_MARK in source
    seed = (f'    this.data = {json.dumps(spec["data"], ensure_ascii=False)};\n'
            f'    this.loading = {"true" if spec["data"] is None else "false"};\n'
            f'    this.tab = {json.dumps(spec["tab"])};\n')
    return source.replace(INIT_MARK, INIT_MARK + seed).replace('await this.refresh(cx);\n    });', '});')


def unknown_chain_methods(source, types):
    text = (types / 'gpui-kit.d.ts').read_text(encoding='utf-8')
    element = set(re.findall(r'^\s+([A-Za-z_]\w*)<Self extends \w+>\(this: Self', text, re.M))
    code = re.sub(r'//[^\n]*', '', source)
    code = re.sub(r'"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`', '""', code)
    builtin = {'map', 'filter', 'slice', 'join', 'split', 'trim', 'toLowerCase', 'flatMap', 'sort', 'then', 'catch',
               'match', 'pop', 'padStart', 'includes', 'find', 'some', 'every', 'reduce', 'concat', 'toString',
               'indexOf', 'lastIndexOf', 'replace', 'toUpperCase', 'startsWith', 'endsWith'}
    found = {}
    for match in re.finditer(r'\)\s*\.([A-Za-z_]\w*)\s*\(', code):
        name = match.group(1)
        if name not in element and name not in builtin:
            found.setdefault(name, code.count('\n', 0, match.start()) + 1)
    return found


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('fixture')
    parser.add_argument('--types', default=str(Path.home() / '.config/herdr-app'))
    parser.add_argument('--main', default=str(MOD / 'main.js'), help='script to check (default: the repository copy)')
    args = parser.parse_args()
    source = Path(args.main).read_text(encoding='utf-8')
    failed = 0
    for name, line in unknown_chain_methods(source, Path(args.types).expanduser()).items():
        print(f'FAIL methods: .{name}( at line {line} is not a gpui-kit element method')
        failed += 1
    data = json.loads(Path(args.fixture).read_text(encoding='utf-8'))
    with tempfile.TemporaryDirectory() as tmp:
        for name, spec in variants(data).items():
            folder = Path(tmp) / name
            folder.mkdir()
            (folder / 'mod.json').write_text((MOD / 'mod.json').read_text(encoding='utf-8'), encoding='utf-8')
            (folder / 'main.js').write_text(build(source, spec), encoding='utf-8')
            result = subprocess.run(['herdr-app', 'check', str(folder)], capture_output=True, text=True)
            ok = result.returncode == 0
            failed += not ok
            detail = [line.strip() for line in (result.stderr + result.stdout).splitlines() if line.strip()]
            print(f'{"PASS" if ok else "FAIL"} {name}' + ('' if ok else f': {" | ".join(detail[:2])}'))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
