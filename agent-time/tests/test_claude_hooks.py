import json
import io
import contextlib
from agent_time.cli import main
from pathlib import Path
import tempfile
import unittest
from agent_time.core import connect, initialize
from agent_time.claude import ingest_claude
from agent_time.claude_hooks import record, setup, source_for

class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = connect(self.root / 'timing.sqlite3')
        initialize(self.db, 100)
        setup(self.db)
    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()
    def event(self, name, now, **extra):
        record(self.db, dict(session_id='s', cwd=str(self.root), hook_event_name=name,
                             prompt='PRIVATE', last_assistant_message='PRIVATE', **extra), now)
    def rows(self):
        return self.db.execute('SELECT * FROM turns ORDER BY start').fetchall()
    def test_complete_and_source_without_content(self):
        self.event('UserPromptSubmit', 120);self.event('Stop', 130)
        row=self.rows()[0]
        self.assertEqual(row['end']-row['start'],10)
        self.assertEqual(source_for(self.db,row),dict(provider='claude',launcher='claude',timing='hooks'))
        self.assertNotIn('PRIVATE','\n'.join(self.db.iterdump()))
    def test_duplicate_stop_is_idempotent(self):
        self.event('UserPromptSubmit',120);self.event('Stop',130);self.event('Stop',200)
        self.assertEqual(self.rows()[0]['end'],130)
    def test_continuing_stop_hook_extends_same_turn(self):
        self.event('UserPromptSubmit',120);self.event('Stop',130)
        self.event('Stop',140,stop_hook_active=True)
        self.assertEqual(len(self.rows()),1);self.assertEqual(self.rows()[0]['end'],140)
    def test_failed(self):
        self.event('UserPromptSubmit',120);self.event('StopFailure',130)
        self.assertEqual(self.rows()[0]['status'],'failed')
    def test_new_prompt_does_not_close_unknown_interruption_at_later_time(self):
        self.event('UserPromptSubmit',120);self.event('UserPromptSubmit',200);self.event('Stop',210)
        self.assertIsNone(self.rows()[0]['end'])
        self.assertEqual(self.rows()[1]['end']-self.rows()[1]['start'],10)
    def test_completed_turns_exclude_gap(self):
        self.event('UserPromptSubmit',120);self.event('Stop',130)
        self.event('UserPromptSubmit',200);self.event('Stop',210)
        self.assertEqual(sum(r['end']-r['start'] for r in self.rows()),20)
    def test_cutoff_subagent_and_orphan_stop(self):
        self.event('UserPromptSubmit',90);self.event('Stop',130)
        self.event('UserPromptSubmit',140,agent_id='child')
        self.assertEqual(self.rows(),[])
    def test_transcript_hook_ownership_keeps_history_without_double_count(self):
        home=self.root/'claude';path=home/'projects/repo/s.jsonl';path.parent.mkdir(parents=True)
        self.event('UserPromptSubmit',150);self.event('Stop',160)
        def duration(end,tid):return dict(type='system',subtype='turn_duration',timestamp=end,
            durationMs=10000,sessionId='s',uuid=tid,cwd=str(self.root),isSidechain=False)
        path.write_text('\n'.join(json.dumps(x) for x in [duration(130,'old'),duration(160,'same-turn')])+'\n')
        ingest_claude(self.db,home);ingest_claude(self.db,home)
        self.assertEqual(len(self.rows()),2)
        self.assertEqual(sum(r['end']-r['start'] for r in self.rows()),20)
    def test_legacy_sources(self):
        self.assertEqual(source_for(self.db,dict(session='codex-id',turn='t'))['provider'],'codex')
        self.assertEqual(source_for(self.db,dict(session='claude:id',turn='t'))['provider'],'claude')

    def test_cli_filters_and_labels_launcher(self):
        self.event('UserPromptSubmit',120);self.event('Stop',130)
        args=['--data-dir',str(self.root),'--t3-db', str(self.root / 'no-t3.sqlite'), '--codex-home',str(self.root/'codex'),
              '--claude-home',str(self.root/'claude'),'turns','--from','1970-01-01','--to','1970-01-02','--source']
        out=io.StringIO()
        with contextlib.redirect_stdout(out):self.assertEqual(main(args+['claude']),0)
        self.assertIn('claude / claude',out.getvalue())
        out=io.StringIO()
        with contextlib.redirect_stdout(out):self.assertEqual(main(args+['codex']),0)
        self.assertIn('No tracked agent turns',out.getvalue())
