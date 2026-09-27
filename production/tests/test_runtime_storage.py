"""Real local/remote snapshot services from explicit operator configuration."""
import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from production.contracts import DomainError
from production.record_payloads import MARKER, RecordPayloads
from production.review_payloads import ReviewPayloads
from production.runtime_storage import (
    INTERNAL_MEDIA_BYTES,
    PUBLIC_UPLOAD_BYTES,
    StorageConfiguration,
    open_storage,
)
from production.store import Store
from production.tests.fixtures import Authority, Transport


class RuntimeStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.database = self.root/'state.sqlite'
        self.store = Store(self.database)
        self.store.create_project('platform_system', {}, 'operator')
        self.store.create_project('p', {}, 'operator')

    def local(self, **changes):
        return StorageConfiguration.model_validate({'mode': 'local', 'database': str(self.database),
            'media_root': str(self.root/'media'), **changes})

    def test_local_services_open_the_existing_store_with_their_upload_bound(self):
        # No release activation binding; the deploy stops and restarts the services.
        runtime = open_storage(self.local(), role='api')
        self.assertEqual(runtime.media.max_bytes, PUBLIC_UPLOAD_BYTES)
        self.assertEqual(runtime.readiness(), {'mode': 'local', 'schema_version': 2})
        runtime.store.create_object('p', 'draft', {}, 'agent')
        self.assertEqual(len(runtime.store.list_objects('p', kind='draft')), 1)

    def test_local_roles_read_restored_record_payloads_and_write_inline(self):
        transport = Transport(Authority())
        self.store.record_payloads = RecordPayloads(ReviewPayloads(transport))
        body = {'messages': ['restored evidence ' * 20000], 'state': 'ready'}
        restored = self.store.create_object('p', 'review-run', body, 'operator')
        with self.store.transaction(write=False) as db:
            physical = json.loads(db.execute('SELECT body FROM revisions WHERE object_id=?',
                                            (restored['object_id'],)).fetchone()['body'])
        self.assertIn(MARKER, physical)
        payload_root = self.root/'review-payloads'
        payload_root.mkdir(mode=0o700)
        for sha, data in transport.authority.blobs.items():
            path = payload_root/sha
            path.write_bytes(data)
            path.chmod(0o600)
        restored_files = set(payload_root.iterdir())
        self.assertTrue(restored_files)
        for role in ('api', 'worker', 'operator'):
            with self.subTest(role=role):
                runtime = open_storage(self.local(), role=role)
                self.addCleanup(runtime.close)
                self.assertEqual(runtime.store.get_object('p', restored['object_id'])['body'], body)
                new_body = {'messages': [(role+' new evidence ') * 20000], 'state': 'ready'}
                created = runtime.store.create_object('p', 'review-run', new_body, 'agent')
                self.assertEqual(runtime.store.get_object('p', created['object_id'])['body'], new_body)
                with runtime.store.transaction(write=False) as db:
                    physical = json.loads(db.execute('SELECT body FROM revisions WHERE object_id=?',
                                                    (created['object_id'],)).fetchone()['body'])
                self.assertEqual(physical, new_body)
                self.assertEqual(set(payload_root.iterdir()), restored_files)

    def test_a_missing_database_never_silently_bootstraps_production(self):
        with self.assertRaises(DomainError):
            open_storage(self.local(database=str(self.root/'missing.sqlite')), role='worker')
        self.assertFalse((self.root/'missing.sqlite').exists())

    def test_development_and_operator_are_explicit_unbound_modes(self):
        for mode, role in (('development', 'worker'), ('local', 'operator')):
            config = self.local(mode=mode, database=str(self.root/(mode+'.sqlite')))
            runtime = open_storage(config, role=role)
            self.assertEqual(runtime.media.max_bytes, INTERNAL_MEDIA_BYTES)
            self.assertIsNone(runtime.journal)
            if mode == 'development':
                self.assertIsNone(runtime.store.record_payloads)
            runtime.close()

    def test_existing_empty_file_is_never_initialized_by_production_bootstrap(self):
        empty = self.root/'empty.sqlite'
        empty.touch(mode=0o600)
        with self.assertRaises(DomainError):
            open_storage(self.local(database=str(empty)), role='api')
        self.assertEqual(empty.read_bytes(), b'')

    def test_configuration_forbids_mixed_modes_unknown_fields_and_unsafe_paths(self):
        for changes in ({'mode': 'cloudflare'}, {'origin': 'https://elsewhere.example'},
                        {'provider_key': 'SECRET_SENTINEL'}, {'activation_revision': 1},
                        {'warning_bytes': 1048576, 'max_database_bytes': 65536}):
            with self.assertRaises(ValidationError):
                self.local(**changes)
        self.database.chmod(0o644)
        with self.assertRaises(DomainError):
            open_storage(self.local(), role='api')
        self.database.chmod(0o600)
        link = self.root/'linked.sqlite'
        link.symlink_to(self.database)
        with self.assertRaises(DomainError):
            open_storage(self.local(database=str(link)), role='api')
