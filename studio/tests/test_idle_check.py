import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from studio.idle_check import check, connect_ro


def db(objects, reservations=()):
    conn = sqlite3.connect(':memory:')
    conn.executescript("""
        CREATE TABLE objects (project_id TEXT, object_id TEXT, kind TEXT, current_revision INTEGER);
        CREATE TABLE revisions (project_id TEXT, object_id TEXT, revision INTEGER, body TEXT);
        CREATE TABLE reservations (project_id TEXT, reservation_id TEXT, budget_key TEXT, amount INTEGER, state TEXT);
    """)
    for n, (kind, body) in enumerate(objects):
        conn.execute("INSERT INTO objects VALUES ('p', ?, ?, 2)", (f'o{n}', kind))
        conn.execute("INSERT INTO revisions VALUES ('p', ?, 1, ?)", (f'o{n}', json.dumps({'state': 'queued'})))
        conn.execute("INSERT INTO revisions VALUES ('p', ?, 2, ?)", (f'o{n}', json.dumps(body)))
    for r in reservations:
        conn.execute("INSERT INTO reservations VALUES ('p', ?, 'fal_owner', 5, ?)", r)
    return conn


class IdleCheck(unittest.TestCase):
    def test_terminal_work_is_idle(self):
        r = check(db([('job', {'state': 'succeeded'}), ('review-turn', {'status': 'received'}),
                      ('shoot-order', {'cards': [{'stage': 'offered'}]})], [('r1', 'settled'), ('r2', 'unknown')]))
        self.assertTrue(r['idle'])

    def test_current_revision_is_read(self):
        # revision 1 says queued, the current revision 2 says succeeded
        self.assertTrue(check(db([('job', {'state': 'succeeded'})]))['idle'])

    def test_running_job_is_busy(self):
        for state in ('queued', 'dispatching', 'submitted', 'running'):
            self.assertFalse(check(db([('job', {'state': state})]))['idle'], state)

    def test_unknown_job_still_polling_is_busy(self):
        self.assertFalse(check(db([('job', {'state': 'unknown', 'remote_job_id': 'x'})]))['idle'])

    def test_parked_unknown_job_is_reported_not_busy(self):
        r = check(db([('job', {'state': 'unknown'})]))
        self.assertTrue(r['idle'])
        self.assertEqual(len(r['parked']), 1)

    def test_a_job_the_worker_stopped_polling_or_downloading_is_parked(self):
        # a paid job out of polls or downloads no longer blocks every deploy.
        r = check(db([('job', {'state': 'unknown', 'remote_job_id': 'x', 'poll_count': 20}),
                      ('job', {'state': 'unknown', 'pending_result': {'object_id': 'r'}, 'download_count': 3})]))
        self.assertTrue(r['idle'])
        self.assertEqual(len(r['parked']), 2)
        self.assertFalse(check(db([('job', {'state': 'unknown', 'remote_job_id': 'x', 'poll_count': 19})]))['idle'])

    def test_held_reservation_is_busy(self):
        self.assertFalse(check(db([], [('r1', 'held')]))['idle'])

    def test_open_shoot_order_is_busy(self):
        self.assertFalse(check(db([('shoot-order', {'cards': [{'stage': 'firing'}]})]))['idle'])

    def test_unfinished_review_or_composition_is_busy(self):
        self.assertFalse(check(db([('review-task', {'state': 'running'})]))['idle'])
        self.assertFalse(check(db([('composition-job', {'state': 'queued'})]))['idle'])


class ReadOnlyConnection(unittest.TestCase):
    def test_reads_a_database_whose_wal_was_checkpointed_away(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / 'm.sqlite')
            w = sqlite3.connect(path)
            w.execute('PRAGMA journal_mode=WAL')
            w.execute('CREATE TABLE t (x)')
            w.execute('INSERT INTO t VALUES (1)')
            w.commit()
            w.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            w.close()
            # Some SQLite builds keep -wal/-shm after the last close; the live one removes them. Simulate that.
            for suffix in ('-wal', '-shm'):
                Path(path + suffix).unlink(missing_ok=True)
            self.assertEqual(connect_ro(path).execute('SELECT x FROM t').fetchone()[0], 1)

    def test_sees_uncheckpointed_rows_while_a_writer_is_open(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / 'm.sqlite')
            w = sqlite3.connect(path)
            w.execute('PRAGMA journal_mode=WAL')
            w.execute('PRAGMA wal_autocheckpoint=0')
            w.execute('CREATE TABLE t (x)')
            w.execute('INSERT INTO t VALUES (2)')
            w.commit()
            try:
                self.assertEqual(connect_ro(path).execute('SELECT x FROM t').fetchone()[0], 2)
            finally:
                w.close()


if __name__ == '__main__':
    unittest.main()
