"""Large storage fields must round-trip without changing logical evidence."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from production.contracts import DomainError, canonical_json, content_hash
from production.record_payloads import MARKER, RecordPayloads, local_record_payloads
from production.review_payloads import DirectoryBlobs, ReviewPayloads
from production.store import Store
from production.tests.fixtures import Authority, Transport


class RecordPayloadTests(unittest.TestCase):
    def setUp(self):
        self.transport = Transport(Authority())
        self.codec = RecordPayloads(ReviewPayloads(self.transport))

    def test_heavy_history_is_external_and_state_remains_small_and_readable(self):
        body = {'state': 'responded', 'task_id': 'task_1', 'lease_until': 0,
                'messages': [{'role': 'user', 'content': 'retained evidence ' * 20000}],
                'dependencies': [{'object_id': 'manifest', 'revision': 1, 'digest': 'a' * 64}]}
        original = copy.deepcopy(body)
        physical = self.codec.encode('review-run', body)
        self.assertEqual(body, original)
        self.assertLess(len(canonical_json(physical)), 2000)
        self.assertEqual(physical['state'], body['state'])
        self.assertEqual(physical['dependencies'], body['dependencies'])
        self.assertEqual(self.codec.decode(physical, content_hash(body)), body)
        for state in ('ready', 'responded', 'completed'):
            another = self.codec.encode('review-run', {**body, 'state': state})
            self.assertEqual(another['messages'], physical['messages'])
        self.assertEqual(RecordPayloads(ReviewPayloads(Transport(self.transport.authority))).decode(
            physical, content_hash(body)), body)

    def test_release_candidate_context_and_replay_keep_exact_logical_hashes(self):
        for kind, field in [('release', 'files'), ('candidate', 'context'),
                            ('review-context', 'evidence'), ('review-turn', 'result'),
                            ('idempotency-result', 'result')]:
            with self.subTest(kind=kind):
                body = {field: {'text': 'different evidence ' * 2000}, 'state': 'retained'}
                physical = self.codec.encode(kind, body)
                self.assertIn(MARKER, physical)
                self.assertEqual(content_hash(self.codec.decode(physical, content_hash(body))), content_hash(body))

    def test_inline_records_and_literal_marker_are_not_misinterpreted(self):
        for body in ({'content': 'small'}, {MARKER: {'fields': ['content']}, 'content': 'literal'}):
            self.assertEqual(self.codec.decode(body, content_hash(body)), body)
        body = {MARKER: {'authored': 'literal'}, 'messages': ['large ' * 20000], 'state': 'ready'}
        packed = self.codec.encode('review-run', body)
        self.assertEqual(self.codec.decode(packed, content_hash(body)), body)
        self.assertNotEqual(packed[MARKER], body[MARKER])

    def test_corruption_wrong_reference_and_changed_inline_state_are_rejected(self):
        body = {'messages': ['original ' * 20000], 'state': 'ready'}
        physical = self.codec.encode('review-run', body)
        for altered in ({**physical, 'state': 'completed'},
                        {**physical, MARKER: {'fields': ['state'], 'literal_present': False}},
                        {**physical, 'messages': {'invalid': 'reference'}}):
            with self.subTest(altered=list(altered)), self.assertRaises(DomainError):
                self.codec.decode(altered, content_hash(body))
        pointer = physical['messages']
        self.transport.authority.blobs[pointer['manifest_sha256']] = b'corrupt'
        with self.assertRaises(DomainError):
            self.codec.decode(physical, content_hash(body))

    def test_unknown_kinds_and_small_fields_are_inline(self):
        for kind in ('asset', 'credential', 'release-activation', 'unknown-future-kind'):
            body = {'messages': ['large ' * 20000]}
            self.assertEqual(self.codec.encode(kind, body), body)
        body = {'messages': ['small'], 'state': 'ready'}
        self.assertEqual(self.codec.encode('review-run', body), body)


class LocalRecordPayloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name).resolve()/'local.sqlite'
        self.root = self.database.parent/'review-payloads'
        self.root.mkdir(mode=0o700)
        self.transport = Transport(Authority())
        self.cloud = RecordPayloads(ReviewPayloads(self.transport))
        self.codec = local_record_payloads(self.database)

    def externalize(self, kind, body):
        physical = self.cloud.encode(kind, body)
        self.assertIn(MARKER, physical)
        for sha, data in self.transport.authority.blobs.items():
            path = self.root/sha
            path.write_bytes(data)
            path.chmod(0o600)
        return physical

    def test_restored_record_decodes_exactly_and_retains_backup_references(self):
        body = {'messages': ['retained evidence ' * 20000], 'state': 'ready',
                MARKER: {'authored': 'literal'}}
        physical = self.externalize('review-run', body)
        digest = content_hash(body)
        self.assertEqual(self.codec.decode(physical, digest), body)
        self.assertEqual(self.codec.references(physical, digest), [physical['messages']])
        self.assertEqual(self.codec.references(physical, digest), self.cloud.references(physical, digest))
        with self.assertRaises(DomainError):
            self.codec.decode({**physical, 'state': 'completed'}, digest)

    def test_local_encode_and_legacy_decode_stay_inline_without_blob_writes(self):
        bodies = ({'messages': ['large evidence ' * 20000]},
                  {MARKER: {'fields': ['messages']}, 'messages': ['literal ' * 20000]},
                  {'content': 'small'}, ['literal'], None)
        with patch.object(DirectoryBlobs, 'put_blob', side_effect=AssertionError('Unexpected blob write')) as put:
            for body in bodies:
                with self.subTest(body_type=type(body).__name__):
                    physical = self.codec.encode('review-run', body)
                    self.assertIs(physical, body)
                    self.assertEqual(self.codec.decode(physical, content_hash(body)), body)
                    self.assertEqual(self.codec.references(physical, content_hash(body)), [])
            put.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_store_replay_round_trips_large_result_without_blob_writes(self):
        store = Store(self.database)
        store.record_payloads = self.codec
        result = {'evidence': 'complete result ' * 20000, MARKER: {'authored': 'literal'}}
        with patch.object(DirectoryBlobs, 'put_blob', side_effect=AssertionError('Unexpected blob write')) as put:
            encoded = store.encode_replay(result)
            self.assertEqual(json.loads(encoded)['$mvgp_replay_v1']['body'], {'result': result})
            self.assertEqual(store.decode_replay(encoded), result)
            put.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_tampered_or_missing_restored_blobs_are_rejected(self):
        body = {'messages': ['original evidence ' * 20000], 'state': 'ready'}
        physical = self.externalize('review-run', body)
        for sha, data in self.transport.authority.blobs.items():
            path = self.root/sha
            for damage in ('tampered', 'missing'):
                with self.subTest(sha=sha, damage=damage):
                    if damage == 'tampered':
                        path.write_bytes(b'corrupt')
                    else:
                        path.unlink()
                    with self.assertRaises(DomainError):
                        self.codec.decode(physical, content_hash(body))
                    path.write_bytes(data)
                    path.chmod(0o600)


if __name__ == '__main__':
    unittest.main()
