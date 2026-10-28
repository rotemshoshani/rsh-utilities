import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from agent_time.core import connect, initialize, report_rows, totals, _event
from agent_time.claude_hooks import setup, source_for
from agent_time.t3 import ingest_t3

class T3Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.db=connect(self.root/'timing.sqlite3');initialize(self.db,100);setup(self.db)
        self.path=self.root/'statev2.sqlite';self.source=sqlite3.connect(self.path)
        self.source.executescript('''
          CREATE TABLE orchestration_v2_projection_provider_turns(provider_turn_id,provider_thread_id,thread_id,node_id,status,started_at,completed_at,payload_json);
          CREATE TABLE orchestration_v2_projection_provider_threads(provider_thread_id,provider_session_id,driver,owner_node_id,payload_json);
          CREATE TABLE orchestration_v2_projection_nodes(node_id,parent_node_id,kind);
          CREATE TABLE orchestration_v2_projection_threads(thread_id,project_id,payload_json);
          CREATE TABLE projection_projects(project_id,workspace_root);
          CREATE TABLE orchestration_v2_projection_provider_sessions(provider_session_id,payload_json);
        ''')
    def tearDown(self):
        self.db.close();self.source.close();self.tmp.cleanup()
    def t3(self, id='t3-turn', provider='claudeAgent', native='native-turn', session='session', start=120,end=140,owner=None,status='completed'):
        self.source.execute('INSERT INTO orchestration_v2_projection_provider_turns VALUES (?,?,?,?,?,?,?,?)',(id,id,id,id,status,start,end,json.dumps({'nativeTurnRef':{'nativeId':native,'strength':'strong' if provider=='codex' else 'weak'},'message':'PRIVATE'})))
        self.source.execute('INSERT INTO orchestration_v2_projection_provider_threads VALUES (?,?,?,?,?)',(id,id,provider,owner,json.dumps({'nativeThreadRef':{'nativeId':session}})))
        self.source.execute('INSERT INTO orchestration_v2_projection_nodes VALUES (?,?,?)',(id,None,'root_turn'))
        self.source.execute('INSERT INTO orchestration_v2_projection_threads VALUES (?,?,?)',(id,id,'{}'))
        self.source.execute('INSERT INTO projection_projects VALUES (?,?)',(id,str(self.root)))
        self.source.execute('INSERT INTO orchestration_v2_projection_provider_sessions VALUES (?,?)',(id,json.dumps({'cwd':str(self.root)})))
        self.source.commit()
    def native(self, session='claude:session',turn='hook:one',start=121,end=139,status='completed'):
        self.db.execute('INSERT INTO turns VALUES (?,?,?,?,?,?)',(session,turn,str(self.root),start,end,status));self.db.commit()
    def rows(self):return report_rows(self.db,100,500)
    def test_codex_exact_native_id_keeps_key_and_total(self):
        self.t3(provider='codex');self.native(session='session',turn='native-turn')
        old=totals(self.rows());ingest_t3(self.db,self.path)
        self.assertEqual(totals(self.rows()),old);self.assertEqual(len(self.rows()),1)
        self.assertEqual(source_for(self.db,self.rows()[0])['launcher'],'t3')
    def test_claude_single_native_interval_relabels_without_duplication(self):
        self.t3();self.native();ingest_t3(self.db,self.path);ingest_t3(self.db,self.path)
        self.assertEqual(totals(self.rows()),(18,18));self.assertEqual(self.rows()[0]['turn'],'hook:one')
        self.assertEqual(source_for(self.db,self.rows()[0])['provider'],'claude')
        self.assertNotIn('PRIVATE','\n'.join(self.db.iterdump()))
    def test_missing_native_uses_t3_then_late_native_is_alias(self):
        self.t3();ingest_t3(self.db,self.path);key=self.rows()[0]['turn']
        self.native();ingest_t3(self.db,self.path);ingest_t3(self.db,self.path)
        self.assertEqual(len(self.rows()),1);self.assertEqual(self.rows()[0]['turn'],key)
        self.assertEqual(totals(self.rows()),(20,20))
        self.assertEqual(self.db.execute('SELECT count(*) FROM turn_aliases').fetchone()[0],1)
    def test_ambiguous_native_matches_never_add_another_interval(self):
        self.t3();self.native();self.native(turn='hook:two',start=125,end=138)
        r=ingest_t3(self.db,self.path);self.assertEqual(r['ambiguous'],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM turns').fetchone()[0],2)
    def test_late_ambiguity_quarantines_t3_fallback(self):
        self.t3();ingest_t3(self.db,self.path)
        self.native();self.native(turn='hook:two',start=125,end=138)
        self.assertEqual(ingest_t3(self.db,self.path)['ambiguous'],1)
        self.assertEqual(len(self.rows()),2)
        self.assertTrue(all(not r['turn'].startswith('t3:') for r in self.rows()))
    def test_subagents_and_cutoff(self):
        self.t3(owner='parent');self.t3(id='old',start=90,end=110)
        r=ingest_t3(self.db,self.path);self.assertEqual(r['excluded'],1);self.assertEqual(self.rows(),[])
    def test_unsupported_schema_is_read_only_and_safe(self):
        bad=self.root/'bad.sqlite';s=sqlite3.connect(bad);s.execute('CREATE TABLE secrets(value)');s.commit();s.close()
        self.assertEqual(ingest_t3(self.db,bad)['unsupported'],1)
        self.assertEqual(self.rows(),[])
    def test_missing_database_is_optional(self):
        self.assertEqual(ingest_t3(self.db,self.root/'absent')['available'],0)
    def test_running_then_completed_uses_same_id(self):
        self.t3(end=None,status='running');ingest_t3(self.db,self.path)
        key=self.rows()[0]['turn'];self.assertEqual(totals(self.rows()),(0,0))
        self.source.execute("UPDATE orchestration_v2_projection_provider_turns SET status='completed',completed_at=140");self.source.commit()
        ingest_t3(self.db,self.path);self.assertEqual(self.rows()[0]['turn'],key);self.assertEqual(totals(self.rows()),(20,20))
    def test_same_project_other_native_session_is_not_deduplicated(self):
        self.t3();self.native(session='claude:another')
        ingest_t3(self.db,self.path);self.assertEqual(len(self.rows()),2)
    def test_imported_history_without_t3_turn_is_not_relabelled(self):
        self.native();ingest_t3(self.db,self.path)
        self.assertEqual(source_for(self.db,self.rows()[0])['launcher'],'claude')
    def test_two_t3_turns_cannot_claim_one_native_turn(self):
        self.t3();self.t3(id='second');self.native()
        r=ingest_t3(self.db,self.path);self.assertEqual(r['ambiguous'],1);self.assertEqual(len(self.rows()),1)
    def test_invalid_time_or_unknown_provider_is_not_counted(self):
        self.t3(end=119);self.t3(id='other',provider='unknown')
        self.assertEqual(ingest_t3(self.db,self.path)['unsupported'],2);self.assertEqual(self.rows(),[])

    def test_codex_fallback_late_native_refines_same_record(self):
        self.t3(provider='codex');ingest_t3(self.db,self.path)
        with self.db:
            _event(self.db,dict(type='event_msg',timestamp=121,payload=dict(type='task_started',turn_id='native-turn')),'session',str(self.root),100)
            _event(self.db,dict(type='event_msg',timestamp=139,payload=dict(type='task_complete',turn_id='native-turn')),'session',str(self.root),100)
        ingest_t3(self.db,self.path)
        self.assertEqual(len(self.rows()),1);self.assertEqual(totals(self.rows()),(18,18))
        self.assertEqual(source_for(self.db,self.rows()[0]),dict(provider='codex',launcher='t3',timing='transcript'))

    def test_old_unfinished_native_turn_does_not_capture_later_t3_work(self):
        self.native(start=105,end=None,status='incomplete');self.t3()
        ingest_t3(self.db,self.path)
        self.assertEqual(totals(self.rows()),(20,20))
        old=self.db.execute("SELECT end FROM turns WHERE turn='hook:one'").fetchone()
        self.assertIsNone(old[0])
