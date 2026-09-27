"""The migrated configs keep the owner's view and validate against the code's own config models."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from studio.migrate_config import main, migrate, validate

ISSUER = 'https://owner.cloudflareaccess.com'
M1, M2 = 'member_' + 'a' * 64, 'member_' + 'b' * 64


def store():
    conn = sqlite3.connect(':memory:')
    conn.executescript("""
        CREATE TABLE projects (project_id TEXT);
        CREATE TABLE objects (project_id TEXT, object_id TEXT, kind TEXT, current_revision INTEGER);
        CREATE TABLE revisions (project_id TEXT, object_id TEXT, revision INTEGER, body TEXT);
    """)
    for p in ('platform_system', 'project_seen', 'project_hidden', 'project_never_shared'):
        conn.execute('INSERT INTO projects VALUES (?)', (p,))
    def obj(oid, kind, body, rev=1):
        conn.execute("INSERT INTO objects VALUES ('platform_system', ?, ?, ?)", (oid, kind, rev))
        conn.execute("INSERT INTO revisions VALUES ('platform_system', ?, ?, ?)", (oid, rev, json.dumps(body)))
    obj(M1, 'member', {'issuer': ISSUER, 'subject': 'subject-one', 'email': 'o@x', 'enabled': True})
    obj(M2, 'member', {'issuer': ISSUER, 'subject': 'subject-two', 'email': 'p@x', 'enabled': True})
    obj('mem_seen', 'project-membership', {'member_id': M1, 'project_id': 'project_seen', 'enabled': True})
    obj('mem_hidden', 'project-membership', {'member_id': M2, 'project_id': 'project_hidden', 'enabled': False})
    return conn


def live_shaped():
    """The live key set of release 97 (values synthetic)."""
    api = {'storage': {'mode': 'local', 'database': '/x/metadata.sqlite', 'media_root': '/x/media', 'activation_revision': 72},
           'deployment_root': '/x/runtime/release-97', 'runtime_files': ['production/api.py'],
           'release_id': 'release_' + 'c' * 64, 'public_origin': 'https://mvgp.example', 'human_confirmation_enabled': True,
           'live_enabled': True, 'personal': {'issuer': ISSUER, 'audience': 'e' * 64, 'account_id': 'f' * 32,
                                              'group_id': '11111111-1111-1111-1111-111111111111'},
           'owner_member_ids': [M1, M2],
           'default_envelopes': [{'budget_key': 'fal_owner', 'unit': 'usd_micro', 'ceiling': 270000000}]}
    worker = {'storage': api['storage'], 'deployment_root': api['deployment_root'], 'runtime_files': api['runtime_files'],
              'release_id': api['release_id'], 'public_origin': api['public_origin'], 'project_ids': [],
              'worker_token_env': 'MVGP_WORKER_TOKEN', 'download_hosts': ['v3.fal.media'], 'reader_enabled': True,
              'cuts_enabled': True, 'review_enabled': False, 'live_enabled': True, 'composition_enabled': True,
              'ffmpeg_path': '/opt/homebrew/bin/ffmpeg', 'ffprobe_path': '/opt/homebrew/bin/ffprobe', 'concurrency': 4,
              'create_concurrency': 2, 'lease_seconds': 600, 'poll_interval': 2.0, 'film_token_env': 'MVGP_FILM_TOKEN',
              'listen_command': ['/usr/bin/python3', 'listen.py']}
    return api, worker


class MigrateConfigTests(unittest.TestCase):
    def test_owner_view_is_kept_and_the_result_validates(self):
        api, worker = live_shaped()
        new_api, new_worker, report = migrate(api, worker, store())
        self.assertEqual(new_api['owner'], {'issuer': ISSUER, 'audience': 'e' * 64, 'subjects': ['subject-one', 'subject-two']})
        # Only the enabled membership was visible; everything else, including never-shared projects, is hidden.
        self.assertEqual(new_api['hidden_projects'], ['project_hidden', 'project_never_shared'])
        self.assertEqual(report['visible_before'], ['project_seen'])
        self.assertNotIn('personal', new_api)
        self.assertNotIn('owner_member_ids', new_api)
        for key in ('review_enabled', 'composition_enabled', 'listen_command'):
            self.assertNotIn(key, new_worker)
        # the release keys go from both; runtime.json in the code dir replaces the release.
        for config in (new_api, new_worker):
            for key in ('release_id', 'deployment_root', 'runtime_files'):
                self.assertNotIn(key, config)
            self.assertNotIn('activation_revision', config['storage'])
        self.assertEqual(report['release_keys_dropped'],
                         ['deployment_root', 'release_id', 'runtime_files', 'storage.activation_revision'])
        self.assertEqual(api['storage']['activation_revision'], 72)  # the inputs are never changed
        validate(new_api, new_worker)

    def test_a_missing_or_foreign_owner_record_stops_the_migration(self):
        api, worker = live_shaped()
        api['owner_member_ids'] = [M1, 'member_' + '9' * 64]
        with self.assertRaises(SystemExit):
            migrate(api, worker, store())

    def test_never_writes_over_the_live_config(self):
        with tempfile.TemporaryDirectory() as d:
            live = Path(d) / 'live'
            live.mkdir()
            api, worker = live_shaped()
            (live / 'api.json').write_text(json.dumps(api))
            (live / 'worker.json').write_text(json.dumps(worker))
            with self.assertRaises(SystemExit):
                main(['--api', str(live / 'api.json'), '--worker', str(live / 'worker.json'),
                      '--db', str(Path(d) / 'none.sqlite'), '--out-dir', str(live)])
            self.assertEqual(json.loads((live / 'api.json').read_text()), api)


if __name__ == '__main__':
    unittest.main()
