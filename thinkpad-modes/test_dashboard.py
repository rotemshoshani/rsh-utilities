import importlib.util
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


d = load('dashboard')
m = load('modes')


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_worker_output_keeps_only_counts(self):
        output = json.dumps({'command': 'run', 'queueEmpty': False,
                             'results': ['pr-open', 'failed-retryable', 'unknown-value'],
                             'failures': ['PRIVATE-SENTINEL'], 'reconciliation': {'private': 'PRIVATE-SENTINEL'}})
        counts, empty = m.summarize_output(output)
        self.assertEqual(counts['pr-open'], 1)
        self.assertEqual(counts['failed-retryable'], 1)
        self.assertEqual(counts['other'], 1)
        self.assertFalse(empty)
        self.assertNotIn('PRIVATE', json.dumps(counts))
        self.assertNotIn('unknown-value', json.dumps(counts))

    def test_invalid_worker_output_is_not_success(self):
        with self.assertRaises(RuntimeError):
            m.summarize_output('not a structured worker result')

    def test_history_bounded_and_private(self):
        with patch.object(m, 'STATE', self.root):
            for i in range(25):
                m.save_record('worker', {'status': 'success', 'started': i}, finished=True)
        history = self.root / 'worker-history.json'
        self.assertEqual(len(json.loads(history.read_text())), 20)
        self.assertEqual(history.stat().st_mode & 0o777, 0o600)

    def test_failure_writes_end_status(self):
        for name in ('server-enabled', 'schedules-armed'):
            (self.root / name).touch()
        with patch.object(m, 'STATE', self.root), patch.object(m, 'in_window', return_value=True), patch.object(m, 'execute_job', side_effect=RuntimeError('PRIVATE-SENTINEL')):
            with self.assertRaises(RuntimeError):
                m.job('worker')
        text = (self.root / 'worker-latest.json').read_text()
        self.assertNotIn('PRIVATE', text)
        data = json.loads(text)
        self.assertEqual(data['status'], 'failed')
        self.assertIn('finished', data)

    def test_archive_reads_health_only(self):
        path = self.root / '.local/state/meniv-bug-worker/error-archive/errors.sqlite'
        path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE health (deployment TEXT,lastPageAt REAL,running INTEGER,reconnects INTEGER)')
            db.execute('INSERT INTO health VALUES (?,?,?,?)', ('tremendous-avocet-15', time.time() * 1000, 1, 2))
            db.commit()
        with patch.object(d, 'HOME', self.root):
            health = d.archive_health()
        self.assertTrue(health['running'])
        self.assertLess(health['age'], 5)
        self.assertEqual(health['reconnects'], 2)

    def test_systemd_parser_excludes_other_properties(self):
        text = 'Id=thinkpad-bug-worker.service\nActiveState=failed\nEnvironment=PRIVATE-SENTINEL\n'
        parsed = d.properties(text)
        self.assertEqual(parsed['thinkpad-bug-worker.service']['ActiveState'], 'failed')
        self.assertNotIn('PRIVATE', json.dumps(parsed))

    def test_stale_collector_not_healthy(self):
        data = {'server': True, 'armed': True, 'units': {'thinkpad-error-collector.service': {'ActiveState': 'active'}},
                'archive': {'running': True, 'age': 120, 'reconnects': 1}, 'worker': None, 'report': None,
                'laptop': {'load': 0, 'cores': 4, 'temperature': None, 'ac': True, 'batteries': []}}
        text = '\n'.join(t for t, _ in d.render(data))
        self.assertIn('CHECK', text)
        self.assertNotIn('HEALTHY', text)
        self.assertIn('no recorded run yet', text)


if __name__ == '__main__':
    unittest.main()
