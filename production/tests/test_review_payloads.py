"""Private immutable bytes, not reduced model evidence or a review-pass shortcut."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from production.contracts import DomainError
from production.review_payloads import (
    BLOCK_BYTES,
    MARKER,
    MAX_BYTES,
    DirectoryBlobs,
    ReviewPayloads,
    canonical_control,
)
from production.tests.fixtures import Authority, Transport


class ReviewPayloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.transport = Transport(Authority())

    def test_corrupt_block_or_manifest_and_wrong_whole_hash_fail_closed(self):
        payloads = ReviewPayloads(self.transport)
        ref = payloads.put({'messages': ['a'* (BLOCK_BYTES+100)]})
        manifest = json.loads(self.transport.authority.blobs[ref['manifest_sha256']])
        for change in [{'size': True}, {'size': 0}, {'size': MAX_BYTES + 1}, {'value_sha256': '0'*64},
                       {MARKER: True}, {'extra': 'untrusted'}]:
            with self.subTest(change=change), self.assertRaises(DomainError):
                payloads.get({**ref, **change})
        sha = manifest['chunks'][0]['sha256']
        original = self.transport.authority.blobs[sha]
        self.transport.authority.blobs[sha] = b'changed'
        with self.assertRaises(DomainError):
            payloads.get(ref)
        self.transport.authority.blobs[sha] = original
        manifest['chunks'][0]['size'] = BLOCK_BYTES-1
        raw = canonical_control(manifest); bad_sha = hashlib.sha256(raw).hexdigest()
        self.transport.put_blob(bad_sha, raw)
        with self.assertRaises(DomainError):
            payloads.get({**ref, 'manifest_sha256': bad_sha})

    def test_backup_blob_reader_rejects_permissions_symlinks_and_mutation(self):
        root = self.root/'review-payloads'
        root.mkdir(mode=0o700)
        data = b'private evidence'
        sha = hashlib.sha256(data).hexdigest()
        path = root/sha
        path.write_bytes(data); path.chmod(0o600)
        reader = DirectoryBlobs(root)
        self.assertEqual(reader.get_blob(sha), data)
        path.chmod(0o644)
        with self.assertRaises(DomainError):
            reader.get_blob(sha)
        path.chmod(0o600); path.write_bytes(b'changed')
        with self.assertRaises(DomainError):
            reader.get_blob(sha)
        path.unlink(); path.symlink_to(self.root/'outside')
        with self.assertRaises(DomainError):
            reader.get_blob(sha)
        alias = self.root/'alias'; alias.symlink_to(root, target_is_directory=True)
        with self.assertRaises(DomainError):
            DirectoryBlobs(alias).get_blob(sha)
        with self.assertRaises(DomainError):
            reader.get_blob('../outside')
        with self.assertRaises(DomainError):
            reader.put_blob(sha, data)
