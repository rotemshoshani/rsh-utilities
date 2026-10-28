from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from argparse import Namespace
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from agent_time.cli import date_range, main
from agent_time.core import connect, ingest, initialize, project_for, report_rows, totals, tracking_start


BASE = datetime(2026, 10, 22, tzinfo=timezone.utc).timestamp()


def record(kind, seconds=0, **payload):
    return {'type': 'event_msg',
            'timestamp': datetime.fromtimestamp(BASE + seconds, timezone.utc).isoformat(),
            'payload': {'type': kind, **payload}}


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / 'codex'
        (self.home / 'sessions').mkdir(parents=True)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.db = connect(self.root / 'data/timing.sqlite3')
        initialize(self.db, BASE)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def session(self, sid='s1', **meta):
        path = self.home / 'sessions' / (sid + '.jsonl')
        self.write(path, {'type': 'session_meta', 'payload': {
            'id': sid, 'cwd': str(self.project), 'source': 'cli', **meta}})
        return path

    def write(self, path, *records):
        with path.open('a') as f:
            for item in records:
                f.write(json.dumps(item) + '\n')

    def turn(self, path, tid, start, end=None, kind='task_complete', **extra):
        self.write(path, record('task_started', start, turn_id=tid))
        if end is not None:
            self.write(path, record(kind, end, turn_id=tid, **extra))

    def rows(self, start=0, end=86400, project=None):
        ingest(self.db, self.home)
        return report_rows(self.db, BASE + start, BASE + end, project)

    def test_parallel_sequential_and_idle_gaps(self):
        p = self.session()
        q = self.session('s2')
        self.turn(p, 'a', 0, 600)
        self.turn(q, 'b', 0, 600)
        self.assertEqual(totals(self.rows()), (1200, 600))
        self.turn(p, 'c', 900, 960)
        self.assertEqual(totals(self.rows()), (1260, 660))

    def test_statuses_and_unfinished(self):
        p = self.session()
        for i, kind in enumerate(('task_complete', 'turn_aborted', 'task_failed', 'turn_failed')):
            self.turn(p, str(i), i * 10, i * 10 + 5, kind)
        self.turn(p, 'unfinished', 40)
        rows = self.rows()
        self.assertEqual([r['status'] for r in rows],
                         ['completed', 'interrupted', 'failed', 'failed', 'incomplete'])
        self.assertEqual(totals(rows), (20, 20))

    def test_transient_error_does_not_end_turn(self):
        p = self.session()
        self.turn(p, 'a', 0)
        self.write(p, record('error', 5, message='private content'))
        self.assertEqual(totals(self.rows()), (0, 0))
        self.write(p, record('task_complete', 10, turn_id='a'))
        self.assertEqual(totals(self.rows()), (10, 10))

    def test_init_is_idempotent_and_excludes_in_flight(self):
        self.assertEqual(initialize(self.db, BASE + 1000), BASE)
        p = self.session()
        self.turn(p, 'old', -20, -10)
        self.turn(p, 'already-running', -5, 5, started_at=BASE - 5)
        self.turn(p, 'new', 10, 20)
        self.assertEqual([r['turn'] for r in self.rows()], ['new'])

    def test_start_before_init_without_terminal_start_field(self):
        p = self.session()
        self.turn(p, 'old', -10, 10)
        self.assertEqual(self.rows(), [])

    def test_repeat_import_and_client_duplicates(self):
        p = self.session(originator='codex-tui')
        self.turn(p, 'a', 0, 60)
        self.rows()
        q = self.home / 'sessions/copy.jsonl'
        shutil.copyfile(p, q)
        self.assertEqual(totals(self.rows()), (60, 60))
        self.assertEqual(totals(self.rows()), (60, 60))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM turns').fetchone()[0], 1)

    def test_desktop_uses_same_boundaries(self):
        p = self.session(originator='chatgpt', source='vscode')
        self.turn(p, 'a', 0, 42)
        self.assertEqual(totals(self.rows()), (42, 42))

    def test_partial_record_is_retried(self):
        p = self.session()
        self.turn(p, 'a', 0)
        end = json.dumps(record('task_complete', 60, turn_id='a'))
        with p.open('a') as f:
            f.write(end[:20])
        self.assertEqual(ingest(self.db, self.home)['partial'], 1)
        self.assertEqual(totals(self.rows()), (0, 0))
        with p.open('a') as f:
            f.write(end[20:] + '\n')
        self.assertEqual(totals(self.rows()), (60, 60))

    def test_archive_move_and_append(self):
        p = self.session()
        self.turn(p, 'a', 0, 60)
        self.rows()
        archive = self.home / 'archived_sessions'
        archive.mkdir()
        q = archive / p.name
        p.rename(q)
        self.turn(q, 'b', 120, 150)
        self.assertEqual(totals(self.rows()), (90, 90))

    def test_truncate_and_reimport(self):
        p = self.session()
        self.turn(p, 'a', 0, 60)
        self.rows()
        p.write_text('')
        self.rows()
        self.session()
        self.turn(p, 'a', 0, 60)
        self.turn(p, 'b', 120, 180)
        self.assertEqual(totals(self.rows()), (120, 120))

    def test_file_replacement(self):
        p = self.session()
        self.turn(p, 'a', 0, 60)
        self.rows()
        replacement = p.with_suffix('.tmp')
        replacement.write_bytes(p.read_bytes())
        replacement.replace(p)
        self.turn(p, 'b', 120, 180)
        self.assertEqual(totals(self.rows()), (120, 120))

    def test_subagents_excluded(self):
        p = self.session('child', source={'subagent': {'spawn': {}}})
        self.turn(p, 'a', 0, 600)
        q = self.session('child2', thread_source='subagent')
        self.turn(q, 'b', 0, 600)
        self.assertEqual(self.rows(), [])
        self.assertEqual(ingest(self.db, self.home)['excluded'], 2)

    def test_midnight_clips_and_overall_union(self):
        p = self.session()
        self.turn(p, 'a', 86390, 86410)
        q = self.session('s2', cwd=str(self.root / 'another-project'))
        self.turn(q, 'b', 86390, 86410)
        self.assertEqual(totals(self.rows(0, 86400)), (20, 10))
        self.assertEqual(totals(self.rows(86400, 172800)), (20, 10))
        self.assertEqual(totals(self.rows(0, 86400, str(self.project))), (10, 10))

    def test_bad_records_and_negative_duration(self):
        p = self.session()
        with p.open('a') as f:
            f.write('broken\n[]\n')
        self.turn(p, 'a', 20, 10)
        self.write(p, record('task_started', 30))
        coverage = ingest(self.db, self.home)
        self.assertEqual(coverage['invalid'], 2)
        self.assertEqual(coverage['unsupported'], 2)
        self.assertEqual(totals(self.rows()), (0, 0))

    def test_terminal_with_explicit_start(self):
        p = self.session()
        self.write(p, record('task_complete', 42, turn_id='a', started_at=BASE))
        self.assertEqual(totals(self.rows()), (42, 42))

    def test_old_unsupported_boundaries_do_not_warn(self):
        p = self.session()
        self.write(p, record('task_started', -20), record('task_complete', -10))
        self.assertEqual(ingest(self.db, self.home)['unsupported'], 0)

    def test_activity_without_boundaries_warns(self):
        p = self.session()
        self.write(p, record('user_message', 20, message='fixture'))
        self.assertEqual(ingest(self.db, self.home)['untimed'], 1)
        self.assertEqual(ingest(self.db, self.home)['untimed'], 1)

    def test_background_activity_after_init_is_not_new_turn(self):
        p = self.session()
        self.write(p, record('task_started', -20, turn_id='old'), record('token_count', 20))
        self.assertEqual(ingest(self.db, self.home)['untimed'], 0)

    def test_user_message_item_without_boundaries_warns(self):
        p = self.session()
        self.write(p, {'type': 'response_item', 'timestamp': '2026-10-22T00:00:20Z',
                       'payload': {'type': 'message', 'role': 'user', 'content': 'fixture'}})
        self.assertEqual(ingest(self.db, self.home)['untimed'], 1)

    def test_inconsistent_duplicate_cannot_create_negative_time(self):
        p = self.session()
        self.turn(p, 'a', 0, 20)
        self.write(p, record('task_started', 30, turn_id='a'))
        self.assertEqual(ingest(self.db, self.home)['unsupported'], 1)
        self.assertEqual(totals(self.rows()), (20, 20))

    def test_unreadable_file_warns_without_error_details(self):
        self.session()
        original = Path.open
        def failing_open(path, *args, **kwargs):
            if path.suffix == '.jsonl':
                raise PermissionError('untrusted details')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', failing_open):
            self.assertEqual(ingest(self.db, self.home)['unreadable'], 1)

    def test_failed_completion_status(self):
        p = self.session()
        self.turn(p, 'a', 0, 20, status='failed')
        self.assertEqual(self.rows()[0]['status'], 'failed')

    def test_invalid_field_types_are_skipped(self):
        p = self.session()
        self.write(p, record(['bad'], 10), record('task_started', 20, turn_id={}))
        self.assertEqual(ingest(self.db, self.home)['unsupported'], 2)
        self.assertEqual(self.rows(), [])

    def test_invalid_metadata_cannot_inherit_previous_identity(self):
        p = self.session()
        self.write(p, {'type': 'session_meta', 'payload': {'id': 'bad', 'cwd': '/bad\0path'}})
        self.turn(p, 'a', 0, 60)
        self.assertEqual(self.rows(), [])

    def test_archive_retains_existing_project_identity(self):
        p = self.session()
        self.turn(p, 'a', 0, 60)
        self.rows()
        archive = self.home / 'archived_sessions'
        archive.mkdir()
        p.rename(archive / p.name)
        with patch('agent_time.core.project_for', return_value='/changed-resolution'):
            self.assertEqual(self.rows()[0]['project'], str(self.project))

    def test_cli_requires_explicit_initialization(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--data-dir', str(self.root / 'new'), 'report']), 1)

    def test_no_conversation_content_in_database(self):
        p = self.session(base_instructions='sensitive-fixture')
        self.turn(p, 'a', 0, 60, last_agent_message='sensitive-fixture')
        self.rows()
        self.assertNotIn('sensitive-fixture', '\n'.join(self.db.iterdump()))

    def test_private_database(self):
        self.assertEqual((self.root / 'data/timing.sqlite3').stat().st_mode & 0o777, 0o600)

    def test_git_worktree_grouping(self):
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        subprocess.run(['git', '-C', str(self.project), '-c', 'user.name=Test',
                        '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false',
                        'commit', '-q', '--allow-empty', '-m', 'fixture'], check=True)
        worktree = self.root / 'worktree'
        subprocess.run(['git', '-C', str(self.project), 'worktree', 'add', '-q',
                        '--detach', str(worktree)], check=True)
        nested = worktree / 'nested'
        nested.mkdir()
        self.assertEqual(project_for(str(nested)), str(self.project))
        p = self.session(cwd=str(nested))
        self.turn(p, 'a', 0, 60)
        self.assertEqual(self.rows()[0]['project'], str(self.project))

    def test_cli_init_report_turns_and_invalid_dates(self):
        args = ['--codex-home', str(self.home), '--claude-home', str(self.root / 'claude'), '--data-dir', str(self.root / 'cli')]
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch('agent_time.cli.time.time', return_value=BASE):
            self.assertEqual(main(args + ['init']), 0)
        p = self.session()
        self.turn(p, 'a', 0, 60)
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(args + ['report', '--from', '2026-10-21', '--to', '2026-10-23']), 0)
            self.assertEqual(main(args + ['turns', '--from', '2026-10-21', '--to', '2026-10-23']), 0)
        self.assertIn('0h 01m 00s', output.getvalue())
        self.assertIn('completed', output.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(args + ['report', '--from', 'bad']), 1)


class DateTests(unittest.TestCase):
    def test_week_begins_monday(self):
        args = Namespace(period='week', date_from=None, date_to=None)
        start, end = date_range(args, date(2026, 10, 22))
        self.assertEqual(datetime.fromtimestamp(start).date(), date(2026, 10, 19))
        self.assertEqual(datetime.fromtimestamp(end).date(), date(2026, 10, 26))

    @unittest.skipUnless(hasattr(time, 'tzset'), 'requires POSIX timezone support')
    def test_dst_midnights(self):
        old = os.environ.get('TZ')
        try:
            os.environ['TZ'] = 'America/New_York'
            time.tzset()
            args = Namespace(period=None, date_from='2026-03-08', date_to='2026-03-08')
            start, end = date_range(args)
            self.assertEqual(end - start, 23 * 3600)
        finally:
            if old is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = old
            time.tzset()


if __name__ == '__main__':
    unittest.main()
