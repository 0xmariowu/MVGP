"""Immutable private record JSON kept outside the database; pointers are evidence, never execution authority.

Some stored records (live: candidates and releases written in the cloud period) keep a small content reference
to blobs under `<db dir>/review-payloads`. They are read back here and verified by hash. New records are stored
inline (local storage only).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections import deque
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from production.contracts import DomainError, canonical_json
from production.review_http import MAX_REQUEST_BYTES
from production.store import Store

BLOCK_BYTES = 1_048_576  # the blob size the stored manifests were written with (was cloud_transport.BLOCK_BYTES)
SAFE_INTEGER = 2**53 - 1
MAX_BYTES = MAX_REQUEST_BYTES
MAX_CHUNKS = (MAX_BYTES + BLOCK_BYTES - 1) // BLOCK_BYTES
MARKER = '$mvgp_review_payload'


def canonical_control(value: Any) -> bytes:
    """The manifest encoding the stored payloads were written with (moved from cloud_transport, unchanged)."""
    def check(item: Any, depth: int = 0) -> None:
        if depth > 12:
            raise ValueError('Control nesting exceeds bounds')
        if isinstance(item, dict):
            for key, child in item.items():
                if type(key) is not str or not key.isascii():
                    raise ValueError('Control keys must be ASCII')
                check(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                check(child, depth + 1)
        elif type(item) is str:
            if not item.isascii():
                raise ValueError('Control strings must be ASCII')
        elif type(item) is int:
            if not 0 <= item <= SAFE_INTEGER:
                raise ValueError('Control integer out of bounds')
        elif item is not None and type(item) is not bool:
            raise ValueError('Invalid control value')
    check(value)
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def _hash(data: bytes | bytearray) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(value: Any) -> bool:
    return type(value) is str and re.fullmatch(r'[a-f0-9]{64}', value) is not None


class BlobTransport(Protocol):
    def get_blob(self, sha256: str) -> bytes: ...
    def put_blob(self, sha256: str, data: bytes) -> dict[str, Any]: ...


class DirectoryBlobs:
    """Read-only exact private backup blobs; never a fallback cloud authority."""
    def __init__(self, root: Path) -> None:
        self.root = root

    def get_blob(self, sha256: str) -> bytes:
        try:
            if (not _digest(sha256) or not self.root.is_absolute() or '..' in self.root.parts
                    or any(p.is_symlink() for p in (self.root, *self.root.parents))):
                raise ValueError
            info = self.root.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise ValueError
            fd = os.open(self.root/sha256, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_mode & 0o077 or not 1 <= info.st_size <= BLOCK_BYTES):
                    raise ValueError
                data = stream.read(BLOCK_BYTES+1)
            if _hash(data) != sha256:
                raise ValueError
            return data
        except (OSError, ValueError):
            raise DomainError('provider_failure', 'Private backup review payload is missing or invalid') from None

    def put_blob(self, sha256: str, data: bytes) -> dict[str, Any]:
        raise DomainError('forbidden', 'Backup review payload storage is read-only')


class ReviewPayloads:
    def __init__(self, transport: BlobTransport) -> None:
        self.transport = transport

    def blobs(self, reference: Any) -> Iterator[tuple[str, bytes]]:
        """Verified dependency closure for a private backup, never public media."""
        self.get(reference)
        sha = reference['manifest_sha256']
        raw = self.transport.get_blob(sha)
        yield sha, raw
        for chunk in json.loads(raw)['chunks']:
            data = self.transport.get_blob(chunk['sha256'])
            if len(data) != chunk['size'] or _hash(data) != chunk['sha256']:
                raise DomainError('provider_failure', 'Private backup payload changed')
            yield chunk['sha256'], data

    def put(self, value: Any) -> dict[str, Any]:
        data = canonical_json(value).encode('utf-8')
        if not 1 <= len(data) <= MAX_BYTES:
            raise DomainError('insufficient_context', 'Complete review payload exceeds private storage bound')
        chunks = []
        pending: deque[Future[Any]] = deque()
        with ThreadPoolExecutor(max_workers=4) as pool:
            for offset in range(0, len(data), BLOCK_BYTES):
                block = data[offset:offset + BLOCK_BYTES]
                digest = _hash(block)
                pending.append(pool.submit(self.transport.put_blob, digest, block))
                chunks.append({'sha256': digest, 'size': len(block)})
                if len(pending) == 4:
                    pending.popleft().result()
            while pending:
                pending.popleft().result()
        manifest = {'format': 1, 'kind': 'review-payload', 'size': len(data),
                    'sha256': _hash(data), 'chunks': chunks}
        raw = canonical_control(manifest)
        digest = _hash(raw)
        self.transport.put_blob(digest, raw)
        return {MARKER: 1, 'manifest_sha256': digest, 'value_sha256': manifest['sha256'], 'size': len(data)}

    def get(self, reference: Any) -> Any:
        try:
            if (type(reference) is not dict or set(reference) != {MARKER, 'manifest_sha256', 'value_sha256', 'size'}
                    or type(reference[MARKER]) is not int or reference[MARKER] != 1
                    or not _digest(reference['manifest_sha256']) or not _digest(reference['value_sha256'])
                    or type(reference['size']) is not int or not 1 <= reference['size'] <= MAX_BYTES):
                raise ValueError
            raw = self.transport.get_blob(reference['manifest_sha256'])
            manifest = json.loads(raw)
            if (len(raw) > 131072 or _hash(raw) != reference['manifest_sha256']
                    or canonical_control(manifest) != raw
                    or set(manifest) != {'format', 'kind', 'size', 'sha256', 'chunks'}
                    or type(manifest['format']) is not int or manifest['format'] != 1
                    or manifest['kind'] != 'review-payload' or type(manifest['size']) is not int
                    or manifest['size'] != reference['size'] or manifest['sha256'] != reference['value_sha256']
                    or type(manifest['chunks']) is not list or not 1 <= len(manifest['chunks']) <= MAX_CHUNKS):
                raise ValueError
            chunks = manifest['chunks']
            for index, chunk in enumerate(chunks):
                if (type(chunk) is not dict or set(chunk) != {'sha256', 'size'} or not _digest(chunk['sha256'])
                        or type(chunk['size']) is not int or not 1 <= chunk['size'] <= BLOCK_BYTES
                        or index < len(chunks)-1 and chunk['size'] != BLOCK_BYTES):
                    raise ValueError
            if sum(chunk['size'] for chunk in chunks) != manifest['size']:
                raise ValueError
            data = bytearray()
            with ThreadPoolExecutor(max_workers=4) as pool:
                pending: deque[Future[bytes]] = deque()
                next_index = 0
                for chunk in chunks:
                    while len(pending) < 4 and next_index < len(chunks):
                        pending.append(pool.submit(self.transport.get_blob, chunks[next_index]['sha256']))
                        next_index += 1
                    block = pending.popleft().result()
                    if len(block) != chunk['size'] or _hash(block) != chunk['sha256']:
                        raise ValueError
                    data.extend(block)
            if _hash(data) != manifest['sha256']:
                raise ValueError
            value = json.loads(data)
            if canonical_json(value).encode('utf-8') != data:
                raise ValueError
            return value
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError('provider_failure', 'Private review payload failed integrity or shape validation') from None


def pack(store: Store, value: Any) -> Any:
    return value  # local storage keeps new records inline


def unpack(store: Store, value: Any) -> Any:
    if isinstance(value, dict) and MARKER in value:
        return for_store(store).get(value)
    return value


def for_store(store: Store) -> ReviewPayloads:
    return ReviewPayloads(DirectoryBlobs(store.path.parent/'review-payloads'))
