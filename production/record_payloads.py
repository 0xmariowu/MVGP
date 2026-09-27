"""Lossless physical storage for heavy service fields, not a new artifact format.

Logical revision hashes remain over expanded JSON. Small SQL-readable state and
reference fields stay inline. Only explicit large service payload fields use the
existing private blob store. An inline legacy/literal value wins when its hash
already matches, so authored marker-shaped JSON is never treated as authority.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from production.contracts import DomainError, canonical_json, content_hash
from production.review_payloads import DirectoryBlobs, ReviewPayloads

MARKER = '$mvgp_record_payload_v1'
MIN_FIELD_BYTES = 16 * 1024
# Storage layout only; these names impose no filmmaking or reviewer policy.
FIELDS = {
    'release': ('files', 'method_evidence'),
    'candidate': ('context', 'compilation', 'request'),
    'review-context': ('governing', 'evidence'),
    'review-run': ('messages', 'consumption', 'definitions'),
    'review-turn': ('request', 'result', 'consumption'),
    'idempotency-result': ('result',),
}


class RecordPayloads:
    def __init__(self, payloads: ReviewPayloads) -> None:
        self.payloads = payloads

    def encode(self, kind: str, body: Any) -> Any:
        if not isinstance(body, dict):
            return body
        result = dict(body)
        external = []
        for key in FIELDS.get(kind, ()):
            if key in body and len(canonical_json(body[key]).encode('utf-8')) >= MIN_FIELD_BYTES:
                result[key] = self.payloads.put(body[key])
                external.append(key)
        if external:
            marker: dict[str, Any] = {'fields': external, 'literal_present': MARKER in body}
            if MARKER in body:
                marker['literal'] = body[MARKER]
            result[MARKER] = marker
        return result

    def references(self, physical: Any, digest: str) -> list[dict[str, Any]]:
        """Verified blob roots for backup; no hidden detached object discovery."""
        self.decode(physical, digest)
        if content_hash(physical) == digest:
            return []
        return [physical[key] for key in physical[MARKER]['fields']]

    def decode(self, physical: Any, digest: str) -> Any:
        if content_hash(physical) == digest:
            return physical
        try:
            marker = physical[MARKER]
            if (not isinstance(physical, dict) or not isinstance(marker, dict)
                    or type(marker.get('literal_present')) is not bool
                    or set(marker) != ({'fields', 'literal_present', 'literal'}
                                       if marker['literal_present'] else {'fields', 'literal_present'})
                    or not isinstance(marker.get('fields'), list) or not marker['fields']
                    or any(not isinstance(key, str) or key == MARKER for key in marker['fields'])
                    or len(set(marker['fields'])) != len(marker['fields'])):
                raise ValueError
            result = {key: value for key, value in physical.items() if key != MARKER}
            for key in marker['fields']:
                result[key] = self.payloads.get(physical[key])
            if marker['literal_present']:
                result[MARKER] = marker['literal']
            if content_hash(result) != digest:
                raise ValueError
            return result
        except (KeyError, TypeError, ValueError):
            raise DomainError('provider_failure', 'Stored record differs from its logical revision hash') from None


class LocalRecordPayloads(RecordPayloads):
    """Read restored payloads while keeping all new local records inline."""

    def encode(self, kind: str, body: Any) -> Any:
        return body


def local_record_payloads(database: Path) -> RecordPayloads:
    return LocalRecordPayloads(ReviewPayloads(DirectoryBlobs(database.parent/'review-payloads')))
