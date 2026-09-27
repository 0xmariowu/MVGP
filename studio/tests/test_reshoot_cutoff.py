"""The deploy records the reshoot cutoff with both services stopped."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from studio.reshoot_cutoff import record

HERE = Path(__file__).resolve().parents[1]


class ReshootCutoffTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / 'metadata.sqlite'
        conn = sqlite3.connect(self.db)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, kind TEXT, body TEXT)')
        conn.executemany('INSERT INTO events(project_id,kind,body) VALUES (?,?,?)', [('p', 'object.created', '{}')] * 7)
        conn.commit()
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        conn.close()
        self.config = self.root / 'worker.json'
        self.config.write_text(json.dumps({'public_origin': 'http://localhost:8811', 'project_ids': []}))

    def test_the_highest_event_goes_into_the_worker_config_and_nothing_else_changes(self):
        self.assertEqual(record(self.db, self.config), 7)
        # With both services stopped the last writer has closed and SQLite has removed -wal and -shm; still read.
        for suffix in ('-wal', '-shm'):
            Path(f'{self.db}{suffix}').unlink(missing_ok=True)
        self.assertEqual(record(self.db, self.config), 7)
        self.assertEqual(json.loads(self.config.read_text()),
                         {'public_origin': 'http://localhost:8811', 'project_ids': [], 'reshoot_after_event': 7})
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_the_deploy_runs_it_as_a_script(self):
        out = subprocess.run([sys.executable, str(HERE / 'reshoot_cutoff.py'), '--db', str(self.db), '--worker-config',
                              str(self.config)], capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), '7')


if __name__ == '__main__':
    unittest.main()
