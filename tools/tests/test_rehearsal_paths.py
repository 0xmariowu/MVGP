"""The rehearsal never touches live: every path stays under
<studio>/rehearsal, and a config naming another database is refused before anything starts."""
import json
import tempfile
import unittest
from pathlib import Path

from tools.rehearsal import rehearse


class RehearsalPathTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.studio = Path(tmp.name).resolve()
        self.paths = rehearse.Paths(self.studio)
        self.paths.config.mkdir(parents=True)
        self.paths.state.mkdir(parents=True)

    def current(self, database: str) -> None:
        for name in ('api', 'worker'):
            (self.paths.config / f'{name}.json').write_text(json.dumps(
                {'storage': {'mode': 'local', 'database': database.format(root=self.studio)}, 'ffmpeg_path': '/usr/bin/ffmpeg'}))
        self.paths.current.write_text(json.dumps({'code_dir': str(self.studio), 'api_config': str(self.paths.config / 'api.json'),
                                                  'worker_config': str(self.paths.config / 'worker.json')}))

    def test_only_paths_under_the_rehearsal_folder_are_accepted(self):
        self.assertEqual(self.paths.inside(self.paths.state / 'media'), (self.paths.state / 'media').resolve())
        for path in (self.studio / 'state/live/metadata.sqlite', self.paths.root / '../state/live', Path('/tmp/other')):
            with self.subTest(path=path), self.assertRaises(SystemExit):
                self.paths.inside(path)

    def test_a_config_naming_another_database_is_refused_before_anything_starts(self):
        for database in ('{root}/state/live/metadata.sqlite', '{root}/rehearsal/../state/live/metadata.sqlite', '/tmp/other.sqlite'):
            self.current(database)
            with self.subTest(database=database), self.assertRaises(SystemExit):
                rehearse.up(self.studio)
            self.assertFalse(self.paths.run.exists())

    def test_prepare_refuses_a_code_dir_without_the_runtime_config(self):
        with self.assertRaises(SystemExit):
            rehearse.prepare(self.studio, self.studio, None)
        self.assertEqual(sorted(p.name for p in self.paths.state.iterdir()), [])  # nothing restored

    def test_the_rehearsal_worker_refuses_a_live_config(self):
        source = (Path(__file__).resolve().parents[1] / 'rehearsal/worker.py').read_text()
        self.assertIn("'/rehearsal/' not in config['storage']['database']", source)


    def test_the_rehearsal_records_the_reshoot_cutoff_only_for_a_code_dir_that_reads_it(self):
        # as the deploy does, before the rehearsal worker starts.
        import sqlite3
        database = self.paths.state / 'metadata.sqlite'
        conn = sqlite3.connect(database)
        conn.execute('CREATE TABLE events(sequence INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, kind TEXT, body TEXT)')
        conn.executemany('INSERT INTO events(project_id,kind,body) VALUES (?,?,?)', [('p', 'k', '{}')] * 3)
        conn.commit()
        conn.close()
        config = self.paths.config / 'worker.json'
        config.write_text(json.dumps({'project_ids': []}))
        old, new = self.studio / 'old-code', self.studio / 'new-code'
        for code, text in ((old, 'class WorkerConfiguration: ...'), (new, 'reshoot_after_event: int | None = None')):
            (code / 'production').mkdir(parents=True)
            (code / 'production/worker.py').write_text(text)
        self.assertIsNone(rehearse.record_reshoot_cutoff(database, config, old))
        self.assertEqual(json.loads(config.read_text()), {'project_ids': []})
        self.assertEqual(rehearse.record_reshoot_cutoff(database, config, new), 3)
        self.assertEqual(json.loads(config.read_text())['reshoot_after_event'], 3)

if __name__ == '__main__':
    unittest.main()
