"""First install gives the real services private, usable configs without changing an existing studio."""
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from production.auth import AuthService
from production.store import Store
from studio.check_config import problems
from studio.init import initialize, sample_configs
from tools.rehearsal import rehearse

REPO = Path(__file__).resolve().parents[2]


class InitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.studio = Path(self.tmp.name).resolve()

    def init(self):
        with contextlib.redirect_stdout(io.StringIO()), patch.dict(os.environ, {'MVGP_HF_BIN': ''}):
            return initialize(self.studio, 8821)

    def test_services_and_owner_credential_work_without_provider_keys(self):
        self.init()
        configs = {n: json.loads((self.studio / f'config/live/{n}.json').read_text()) for n in ('api', 'worker')}
        self.assertEqual(problems(configs['api'], configs['worker']), [])
        self.assertEqual(configs['api']['public_origin'], 'http://localhost:8821')
        self.assertTrue(configs['api']['local_owner'])
        store = Store(self.studio / 'state/live/metadata.sqlite')
        auth = AuthService(store, configs['api']['public_origin'])
        principal = auth.authenticate((self.studio / 'config/live/credentials/owner-agent.token').read_text().strip())
        self.assertEqual(principal.role, 'agent')
        auth.authorize(principal, None, 'create-project')
        env = json.loads((self.studio / 'env/live-worker.json').read_text())
        result = subprocess.run([sys.executable, '-m', 'production.worker', '--config',
                                 str(self.studio / 'config/live/worker.json'), '--once'],
                                cwd=REPO, env={**os.environ, **env}, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(env['FAL_AI_TOKEN'], '')
        self.assertEqual(env['APILIO_AI_KEY'], '')
        for path in self.studio.rglob('*'):
            if path.is_file() and path.suffix in ('.json', '.token', '.sqlite'):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600, path)
        self.assertTrue((self.studio / 'run-service.sh').stat().st_mode & 0o100)

    def test_repeat_preserves_all_files_including_user_edits_and_credentials(self):
        self.init()
        path = self.studio / 'env/live-ledger.json'
        path.write_text('{"user_setting": "preserve"}')
        before = {p: p.read_bytes() for p in self.studio.rglob('*') if p.is_file()}
        self.assertFalse(self.init())
        self.assertEqual(before, {p: p.read_bytes() for p in self.studio.rglob('*') if p.is_file()})

    def test_foreign_or_partial_install_is_refused_without_overwriting(self):
        path = self.studio / 'launch.py'
        path.write_text('existing launcher')
        with self.assertRaisesRegex(SystemExit, 'refusing to overwrite'):
            self.init()
        self.assertEqual(path.read_text(), 'existing launcher')
        self.assertFalse((self.studio / 'config').exists())

    def test_invalid_port_creates_no_config(self):
        with self.assertRaises(SystemExit):
            initialize(self.studio, 0)
        self.assertEqual(list(self.studio.iterdir()), [])

    def test_missing_ffmpeg_is_actionable(self):
        with patch('shutil.which', return_value=None), self.assertRaisesRegex(SystemExit, 'ffmpeg'):
            sample_configs(self.studio, 8811)

    def test_rehearsal_without_backup_uses_an_empty_separate_store(self):
        self.init()
        live = self.studio / 'state/live/metadata.sqlite'
        # A live-only record must not leak into the no-backup rehearsal.
        store = Store(live)
        store.create_project('project_live_only', {}, 'test')
        before = live.read_bytes()
        result = rehearse.prepare(self.studio, REPO, None)
        self.assertIsNone(result['backup'])
        self.assertEqual(live.read_bytes(), before)
        with sqlite3.connect(self.studio / 'rehearsal/state/metadata.sqlite') as db:
            self.assertEqual(db.execute("SELECT count(*) FROM projects WHERE project_id='project_live_only'").fetchone()[0], 0)
        config = json.loads((self.studio / 'rehearsal/config/worker.json').read_text())
        self.assertIn('/rehearsal/fake-hf/', config['hf']['native_path'])
        self.assertEqual(config['storage']['database'], str(self.studio / 'rehearsal/state/metadata.sqlite'))
        rehearse.prepare(self.studio, REPO, None)
        self.assertTrue(list((self.studio / 'rehearsal/old').iterdir()))

    def test_latest_backup_remains_the_source_when_present(self):
        self.init()
        backup = self.studio / 'backup/fixture'
        backup.mkdir()
        import shutil
        shutil.copyfile(self.studio / 'state/live/metadata.sqlite', backup / 'metadata.sqlite')
        shutil.copytree(self.studio / 'config/live', backup / 'config-live')
        Store(backup / 'metadata.sqlite').create_project('project_backed_up', {}, 'test')
        result = rehearse.prepare(self.studio, REPO, None)
        self.assertEqual(result['backup'], str(backup))
        with sqlite3.connect(self.studio / 'rehearsal/state/metadata.sqlite') as db:
            self.assertEqual(db.execute("SELECT count(*) FROM projects WHERE project_id='project_backed_up'").fetchone()[0], 1)

    def test_native_higgsfield_is_copied_pinned_and_accepted_by_worker(self):
        fake_root = self.studio / 'fake-source'
        fake_root.mkdir()
        native = rehearse.install_fake_hf(rehearse.Paths(fake_root), REPO)['native_path']
        with contextlib.redirect_stdout(io.StringIO()), patch.dict(os.environ, {'MVGP_HF_BIN': native}):
            initialize(self.studio, 8821)
        config = json.loads((self.studio / 'config/live/worker.json').read_text())
        self.assertEqual(config['hf']['native_path'], str(self.studio / 'bin/hf'))
        self.assertEqual(config['hf']['credential_home'], str(self.studio / 'hf-home'))
        from production.worker import build_worker
        with patch.dict(os.environ, json.loads((self.studio / 'env/live-worker.json').read_text())):
            worker = build_worker(self.studio / 'config/live/worker.json')
            worker.close()

    def test_shell_entry_reuses_existing_python_environment_without_installing(self):
        result = subprocess.run([str(REPO / 'studio/init.sh')], cwd=REPO,
                                env={**os.environ, 'MVGP_STUDIO': str(self.studio), 'MVGP_PORT': '8821',
                                     'MVGP_PYTHON': sys.executable, 'MVGP_HF_BIN': ''},
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.studio / 'venv').resolve(), Path(sys.prefix).resolve())
        self.assertTrue((self.studio / 'init.json').exists())

    def test_missing_backup_on_uninitialized_studio_is_actionable(self):
        with self.assertRaisesRegex(SystemExit, 'studio/init.sh'):
            rehearse.prepare(self.studio, REPO, None)

    def test_explicit_missing_backup_is_not_silently_replaced(self):
        self.init()
        with self.assertRaises(sqlite3.OperationalError):
            rehearse.prepare(self.studio, REPO, self.studio / 'missing-backup')


class FirstDeployTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.studio = Path(tmp.name).resolve()
        (self.studio / 'venv/bin').mkdir(parents=True)
        (self.studio / 'venv/bin/python').symlink_to(sys.executable)
        self.prefix = (REPO / 'studio/deploy.sh').read_text().split('export MVGP_CURRENT=$CURRENT')[0]

    def run_ensure(self):
        return subprocess.run(['bash', '-c', self.prefix + '\nensure_current\necho first=$FIRST_INSTALL'],
                              env={**os.environ, 'MVGP_STUDIO': str(self.studio)}, capture_output=True, text=True, check=False)

    def test_initialized_studio_selects_first_install_without_a_historical_launcher(self):
        for name in ('init.json', 'config/live/api.json', 'config/live/worker.json', 'state/live/metadata.sqlite'):
            path = self.studio / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}')
        result = self.run_ensure()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('first=1', result.stdout)
        self.assertFalse((self.studio / 'current.json').exists())

    def test_historical_launcher_still_records_the_old_release(self):
        (self.studio / 'launch.py').write_text("RUNTIME = 'runtime/release-97'")
        result = self.run_ensure()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('first=0', result.stdout)
        self.assertEqual(json.loads((self.studio / 'current.json').read_text())['code_dir'],
                         str(self.studio / 'runtime/release-97'))

    def test_current_is_preserved_exactly(self):
        path = self.studio / 'current.json'
        path.write_text('{"existing":"preserve"}')
        self.assertEqual(self.run_ensure().returncode, 0)
        self.assertEqual(path.read_text(), '{"existing":"preserve"}')

    def test_empty_uninitialized_studio_is_refused(self):
        result = self.run_ensure()
        self.assertEqual(result.returncode, 2)
        self.assertIn('no current.json', result.stderr)
