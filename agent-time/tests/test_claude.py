import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_time.core import connect, initialize, report_rows, totals
from agent_time.claude import ingest_claude, ingest_all

class ClaudeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / 'claude'
        self.path = self.home / 'projects' / 'repo' / 'session.jsonl'
        self.path.parent.mkdir(parents=True)
        self.db = connect(self.root / 'data.sqlite3')
        initialize(self.db, 100)
    def tearDown(self):
        self.db.close(); self.tmp.cleanup()
    def event(self, **changes):
        return {**dict(type='system', subtype='turn_duration', timestamp=200, durationMs=60000,
                       sessionId='session', uuid='turn', cwd=str(self.root), isSidechain=False), **changes}
    def write(self, *rows, path=None):
        path = path or self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as f:
            for row in rows: f.write(json.dumps(row)+'\n')
    def rows(self): return self.db.execute('SELECT * FROM turns ORDER BY start').fetchall()
    def test_explicit_duration_and_idempotence(self):
        self.write(self.event())
        ingest_claude(self.db,self.home); ingest_claude(self.db,self.home)
        self.assertEqual(len(self.rows()),1)
        self.assertEqual(self.rows()[0]['session'],'claude:session')
        self.assertEqual(totals(report_rows(self.db,100,300)),(60,60))
    def test_partial_append_and_duplicate_copy(self):
        data=json.dumps(self.event())
        self.path.write_text(data[:20])
        self.assertEqual(ingest_claude(self.db,self.home)['partial'],1)
        with self.path.open('a') as f:f.write(data[20:]+'\n')
        self.write(self.event(),path=self.path.parent/'copy.jsonl')
        ingest_claude(self.db,self.home)
        self.assertEqual(len(self.rows()),1)
    def test_subagents_and_sidechains(self):
        self.write(self.event(isSidechain=True))
        self.write(self.event(),path=self.path.parent/'session/subagents/agent.jsonl')
        result=ingest_claude(self.db,self.home)
        self.assertEqual(result['excluded'],1); self.assertEqual(len(self.rows()),0)
    def test_cutoff_and_invalid_durations(self):
        self.write(self.event(timestamp=120),*[self.event(uuid=str(i),durationMs=v) for i,v in enumerate([-1,True,'300',float('nan'),float('inf'),10**1000])])
        result=ingest_claude(self.db,self.home)
        self.assertEqual(len(self.rows()),0);self.assertEqual(result['unsupported'],6)
    def test_truncation_replacement_and_preserved_project(self):
        self.write(self.event()); ingest_claude(self.db,self.home)
        self.path.write_text('')
        self.write(self.event(uuid='new',timestamp=300))
        with patch('agent_time.claude.project_for',return_value='/changed'):
            ingest_claude(self.db,self.home)
        self.assertEqual(len(self.rows()),2)
        self.assertEqual({r['project'] for r in self.rows()},{str(self.root)})
    def test_missing_duration_never_counts_idle_or_content(self):
        self.write({'type':'user','timestamp':120,'message':{'content':'private-test-text'}},
                   {'type':'assistant','timestamp':200,'message':{'content':'private-test-text'}})
        result=ingest_claude(self.db,self.home)
        self.assertEqual(result['untimed'],1);self.assertEqual(len(self.rows()),0)
        self.assertNotIn('private-test-text','\n'.join(self.db.iterdump()))
    def test_codex_and_claude_same_ids_do_not_collide(self):
        self.db.execute('INSERT INTO turns VALUES (?,?,?,?,?,?)',('session','turn',str(self.root),140,200,'completed'))
        self.db.commit();self.write(self.event())
        result=ingest_all(self.db,self.root/'codex',self.home)
        self.assertEqual(result['claude_durations'],1)
        self.assertEqual(totals(report_rows(self.db,100,300)),(120,60))
    def test_two_projects_and_no_text_in_duration_record(self):
        self.write(self.event(message='private-test-text'),self.event(uuid='second',sessionId='second-session',cwd='/other',timestamp=300))
        ingest_claude(self.db,self.home)
        self.assertEqual(len({r['project'] for r in self.rows()}),2)
        self.assertNotIn('private-test-text','\n'.join(self.db.iterdump()))
    def test_invalid_json_and_tool_results(self):
        self.path.write_text('not json\n')
        self.write({'type':'user','timestamp':120,'message':{'content':[{'type':'tool_result'}]}})
        result=ingest_claude(self.db,self.home)
        self.assertEqual(result['invalid'],1); self.assertEqual(result['untimed'],0)

if __name__=='__main__':unittest.main()
