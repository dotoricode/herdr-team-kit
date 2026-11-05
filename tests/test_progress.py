import json
from pathlib import Path
import tempfile
import unittest

from teamkit import progress


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records), encoding='utf-8')


def claude_use(name, given, use_id='u1', at='2026-11-05T01:00:00Z', sidechain=False):
    return {'type': 'assistant', 'isSidechain': sidechain, 'timestamp': at,
            'message': {'content': [{'type': 'tool_use', 'id': use_id, 'name': name, 'input': given}]}}


def claude_result(use_id, error=False):
    return {'type': 'user', 'isSidechain': False,
            'message': {'content': [{'type': 'tool_result', 'tool_use_id': use_id, 'is_error': error}]}}


class ClaudeLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'profile'
        (self.home / 'sessions').mkdir(parents=True)
        (self.home / 'sessions' / '42.json').write_text(json.dumps(
            {'pid': 42, 'sessionId': 'abc12345-0000-0000-0000-000000000000', 'cwd': '/srv/example/my_repo'}), encoding='utf-8')
        self.transcript = self.home / 'projects' / '-srv-example-my-repo' / 'abc12345-0000-0000-0000-000000000000.jsonl'

    def observe(self, state='working', now=1793840000.0, steps_dir=None):
        return progress.observe('t9', 7, 'claude-code', state, claude_dirs=[self.home], steps_dir=steps_dir,
                                now=now, tree=lambda pid: [7, 42], files=lambda pid: [])

    def test_links_through_session_file_and_reports_latest_top_level_tool(self):
        write_jsonl(self.transcript, [
            claude_use('Read', {'file_path': '/srv/example/my_repo/a.py'}, 'u1', '2026-11-05T01:00:00Z'),
            claude_use('Bash', {'command': 'pytest', 'description': 'Run unit tests'}, 'u2', '2026-11-05T01:05:00Z'),
            claude_use('Edit', {'file_path': '/srv/x.py'}, 'u3', '2026-11-05T01:06:00Z', sidechain=True)])
        got = self.observe()
        self.assertTrue(got['linked'])
        self.assertEqual((got['activity']['label'], got['activity']['detail']), ('명령 실행', 'Run unit tests'))
        self.assertIsNone(got['steps'])

    def test_unlinked_session_is_reported_not_guessed(self):
        got = progress.observe('t9', 7, 'claude-code', 'working', claude_dirs=[self.home], now=0,
                               tree=lambda pid: [7, 99], files=lambda pid: [])
        self.assertEqual((got['linked'], got['activity'], got['stalled']), (False, None, False))

    def test_todo_list_replays_like_the_desktop_app_and_skips_failed_updates(self):
        write_jsonl(self.transcript, [
            claude_use('TodoWrite', {'todos': [
                {'content': 'Read logs', 'activeForm': 'Reading logs', 'status': 'completed'},
                {'content': 'Fix parser', 'activeForm': 'Fixing parser', 'status': 'in_progress'},
                {'content': 'Run tests', 'activeForm': 'Running tests', 'status': 'pending'}]}, 'w1'),
            claude_use('TaskUpdate', {'taskId': 'todo-2', 'status': 'completed'}, 'bad'),
            claude_result('bad', error=True)])
        steps = self.observe()['steps']
        self.assertEqual((steps['done'], steps['total'], steps['current']), (1, 3, 'Fixing parser'))

    def test_working_session_without_change_for_ten_minutes_is_stalled(self):
        write_jsonl(self.transcript, [claude_use('Bash', {'description': 'build'}, 'u1', '2026-11-05T01:00:00Z')])
        at = progress._seconds('2026-11-05T01:00:00Z')
        import os
        os.utime(self.transcript, (at, at))
        self.assertTrue(self.observe('working', now=at + progress.STALL_S + 1)['stalled'])
        self.assertFalse(self.observe('idle', now=at + progress.STALL_S + 1)['stalled'])
        self.assertFalse(self.observe('working', now=at + 60)['stalled'])

    def test_recorded_steps_win_over_transcript_todos(self):
        write_jsonl(self.transcript, [claude_use('Bash', {'description': 'x'})])
        steps = Path(self.tmp.name) / 'steps'
        steps.mkdir()
        (steps / 't9.json').write_text(json.dumps({'title': 'UI 검증', 'steps': [
            {'id': '1', 'name': '앱 실행', 'status': 'completed'}, {'id': '2', 'name': '화면 비교', 'status': 'in_progress'}]}),
            encoding='utf-8')
        got = self.observe(steps_dir=steps)['steps']
        self.assertEqual((got['title'], got['done'], got['total'], got['current']), ('UI 검증', 1, 2, '화면 비교'))

    def test_mcp_tool_names_are_readable(self):
        write_jsonl(self.transcript, [claude_use('mcp__ai-memory__memory_query', {'query': 'x'})])
        self.assertEqual(self.observe()['activity']['label'], 'memory_query (ai-memory)')


class CodexLinkTests(unittest.TestCase):
    def test_links_through_open_rollout_and_reads_last_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / 'sessions' / 'rollout-2026-11-05T01-00-00-x.jsonl'
            write_jsonl(rollout, [
                {'type': 'session_meta', 'timestamp': '2026-11-05T01:00:00Z', 'payload': {'cwd': '/srv/example'}},
                {'type': 'event_msg', 'timestamp': '2026-11-05T01:02:00Z', 'payload': {'type': 'item_completed',
                 'item': {'type': 'CommandExecution', 'command': ['/bin/zsh', '-lc', 'npm test']}}}])
            got = progress.observe('t3', 5, 'codex', 'working', now=0, tree=lambda pid: [5, 6],
                                   files=lambda pid: [str(rollout)] if pid == 6 else ['/dev/null'])
            self.assertTrue(got['linked'])
            self.assertEqual((got['activity']['label'], got['activity']['detail']), ('명령 실행', 'npm test'))

    def test_invalid_terminal_never_reads_steps_outside_the_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(progress.recorded_steps(tmp, '../t1'))


class ReviewFindingTests(unittest.TestCase):
    def test_task_update_finds_the_task_created_by_task_create(self):
        records = [claude_use('TaskCreate', {'subject': 'Build', 'activeForm': 'Building'}, 'toolu_create'),
                   {'type': 'user', 'isSidechain': False, 'toolUseResult': {'task': {'id': '1'}},
                    'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'toolu_create', 'content': 'Task #1 created'}]}},
                   claude_use('TaskUpdate', {'taskId': '1', 'status': 'completed'}, 'toolu_update')]
        _, todos = progress.claude_activity(records)
        self.assertEqual([(t['content'], t['status']) for t in todos], [('Build', 'completed')])

    def test_steps_survive_when_the_todo_list_is_older_than_the_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / 'sessions').mkdir()
            (home / 'sessions' / '42.json').write_text(json.dumps(
                {'pid': 42, 'sessionId': 'abc12345-0000-0000-0000-000000000000', 'cwd': '/srv/r'}), encoding='utf-8')
            filler = {'type': 'user', 'isSidechain': False, 'message': {'content': [{'type': 'text', 'text': 'x' * 4096}]}}
            write_jsonl(home / 'projects' / '-srv-r' / 'abc12345-0000-0000-0000-000000000000.jsonl', [
                claude_use('TodoWrite', {'todos': [{'content': 'A', 'status': 'completed'}, {'content': 'B', 'status': 'in_progress', 'activeForm': 'Doing B'}]}, 'w1')]
                + [filler] * (progress.TAIL_BYTES // 4000 + 10) + [claude_use('Read', {'file_path': '/srv/r/x'}, 'u9')])
            cache = {}
            got = progress.observe('t9', 7, 'claude-code', 'working', claude_dirs=[home], now=0, cache=cache,
                                   tree=lambda pid: [7, 42], files=lambda pid: [])
            self.assertEqual((got['activity']['label'], got['steps']['done'], got['steps']['current']), ('파일 읽기', 1, 'Doing B'))
            self.assertTrue(cache)


class ReReviewFindingTests(unittest.TestCase):
    def test_latest_todo_list_wins_in_a_transcript_longer_than_the_replay_limit(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / 'sessions').mkdir()
            (home / 'sessions' / '42.json').write_text(json.dumps(
                {'pid': 42, 'sessionId': 'abc12345-0000-0000-0000-000000000000', 'cwd': '/srv/r'}), encoding='utf-8')
            filler = {'type': 'user', 'isSidechain': False, 'message': {'content': [{'type': 'text', 'text': 'x' * 1000}]}}
            todo = lambda status, use: claude_use('TodoWrite', {'todos': [{'content': 'A', 'status': status}]}, use)
            write_jsonl(home / 'projects' / '-srv-r' / 'abc12345-0000-0000-0000-000000000000.jsonl',
                        [todo('pending', 'w1')] + [filler] * 60 + [todo('completed', 'w2')] + [filler] * 20
                        + [claude_use('Read', {'file_path': '/srv/r/x'}, 'u9')])
            with patch.object(progress, 'TAIL_BYTES', 10000), patch.object(progress, 'FULL_BYTES', 50000):
                got = progress.observe('t9', 7, 'claude-code', 'working', claude_dirs=[home], now=0, cache={},
                                       tree=lambda pid: [7, 42], files=lambda pid: [])
            self.assertEqual((got['steps']['done'], got['steps']['total']), (1, 1))

    def test_sidechain_or_failed_todo_in_the_tail_does_not_hide_the_parent_list(self):
        from unittest.mock import patch
        for late in ('sidechain', 'failed'):
            with self.subTest(late=late), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                (home / 'sessions').mkdir()
                (home / 'sessions' / '42.json').write_text(json.dumps(
                    {'pid': 42, 'sessionId': 'abc12345-0000-0000-0000-000000000000', 'cwd': '/srv/r'}), encoding='utf-8')
                filler = {'type': 'user', 'isSidechain': False, 'message': {'content': [{'type': 'text', 'text': 'x' * 1000}]}}
                parent = claude_use('TodoWrite', {'todos': [{'content': 'Parent', 'status': 'in_progress'}]}, 'p1')
                child = claude_use('TodoWrite', {'todos': [{'content': 'Child', 'status': 'pending'}]}, 'c1',
                                   sidechain=(late == 'sidechain'))
                tail = [child] + ([claude_result('c1', error=True)] if late == 'failed' else [])
                write_jsonl(home / 'projects' / '-srv-r' / 'abc12345-0000-0000-0000-000000000000.jsonl',
                            [parent] + [filler] * 20 + tail + [claude_use('Read', {'file_path': '/srv/r/x'}, 'u9')])
                with patch.object(progress, 'TAIL_BYTES', 3000), patch.object(progress, 'FULL_BYTES', 50000):
                    got = progress.observe('t9', 7, 'claude-code', 'working', claude_dirs=[home], now=0, cache={},
                                           tree=lambda pid: [7, 42], files=lambda pid: [])
                self.assertEqual(got['steps']['current'], 'Parent')


if __name__ == '__main__':
    unittest.main()
