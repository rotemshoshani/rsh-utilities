import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from agent_time.core import connect
from agent_time import uat

TAB = '00000000-0000-4000-8000-000000000001'


class UatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'timing.sqlite3'
        self.db = connect(self.path)
        uat.setup(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def event(self, now, seq, kind='activity', tab=TAB, project='/project'):
        uat.record_event(self.db, project, {'tab': tab, 'seq': seq, 'kind': kind}, now)

    def test_tail_and_no_future_time(self):
        self.event(100, 1)
        self.event(160, 2)
        self.event(250, 3)
        self.assertEqual(uat.report(self.db, 0, 1000, now=300), ({'/project': 200}, 200))
        self.assertEqual(uat.report(self.db, 0, 1000, now=1000), ({'/project': 270}, 270))

    def test_idle_gaps_and_hidden(self):
        self.event(100, 1)
        self.event(300, 2)
        self.event(320, 3, 'hidden')
        self.event(330, 4, 'hidden')
        self.assertEqual(uat.report(self.db, 0, 1000, now=1000)[1], 140)

    def test_duplicates_and_out_of_order(self):
        self.event(100, 1)
        self.event(110, 3, 'hidden')
        self.event(120, 2)
        self.event(130, 3)
        self.assertEqual(uat.report(self.db, 0, 1000, now=1000)[1], 10)

    def test_overlapping_tabs_and_projects(self):
        self.event(100, 1)
        self.event(150, 1, tab='00000000-0000-4000-8000-000000000002')
        self.event(170, 1, project='/other')
        values, total = uat.report(self.db, 0, 1000, now=1000)
        self.assertEqual(values, {'/project': 170, '/other': 120})
        self.assertEqual(total, 190)
        self.assertEqual(uat.report(self.db, 200, 250, '/project', now=1000)[1], 50)

    def test_unknown_hidden_creates_no_activity(self):
        self.event(100, 1, 'hidden')
        self.assertEqual(uat.report(self.db, 0, 1000, now=1000), ({}, 0))

    def test_validation(self):
        valid = {'tab': TAB, 'seq': 1, 'kind': 'activity'}
        for event in ({**valid, 'content': 'not allowed'}, {**valid, 'seq': True},
                      {**valid, 'kind': 'keypress'}, {**valid, 'tab': 'arbitrary'}, None):
            self.assertFalse(uat.valid_event(event))

    def test_real_http_collector_and_restart_persistence(self):
        server = uat.make_server(self.path, '/project', 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with urllib.request.urlopen(base + '/health', timeout=2) as r:
                self.assertEqual(json.load(r)['service'], 'agent-time-uat')
            body = json.dumps({'tab': TAB, 'seq': 1, 'kind': 'activity'}).encode()
            with urllib.request.urlopen(urllib.request.Request(base + '/activity', data=body), timeout=2) as r:
                self.assertEqual(r.status, 204)
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(urllib.request.Request(base + '/activity', data=body,
                                       headers={'Origin': 'http://untrusted.invalid'}), timeout=2)
            self.assertEqual(cm.exception.code, 403)
            cm.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM uat_intervals').fetchone()[0], 1)
        self.assertEqual(self.db.execute('SELECT end-start FROM uat_intervals').fetchone()[0], 120)
