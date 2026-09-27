"""Shared test fixtures.

The owner signs in the way production does: a Cloudflare Access JWT, signed here by a test key, is verified by
the real OwnerLogin. There is no backdoor session issuer for tests.
"""
from __future__ import annotations

import json
import time
from functools import lru_cache
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from production.auth import AuthService
from production.owner_login import OwnerLogin
from production.runtime_config import RuntimeConfig

OWNER_ISSUER = 'https://owner-test.cloudflareaccess.com'
OWNER_AUDIENCE = 'd' * 64
OWNER_SUBJECT = 'owner-test-subject'


@lru_cache(maxsize=1)
def _key() -> Any:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def owner_jwks() -> dict[str, Any]:
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_key().public_key()))
    return {'keys': [{**public, 'kid': 'owner-test-key', 'alg': 'RS256', 'use': 'sig'}]}


def owner_jwt(**changes: Any) -> str:
    now = int(time.time())
    claims = {'iss': OWNER_ISSUER, 'aud': [OWNER_AUDIENCE], 'sub': OWNER_SUBJECT, 'email': 'owner@example.test',
              'type': 'app', 'iat': now - 5, 'exp': now + 600, **changes}
    return jwt.encode(claims, _key(), algorithm='RS256', headers={'kid': 'owner-test-key'})


def owner_login(auth: AuthService) -> OwnerLogin:
    return OwnerLogin(auth, issuer=OWNER_ISSUER, audience=OWNER_AUDIENCE, subjects=[OWNER_SUBJECT], jwks=owner_jwks)


def owner_session(auth: AuthService) -> dict[str, str]:
    """The owner's desk session through the real login: session_token, csrf_token and cookie."""
    result = owner_login(auth).login(owner_jwt(), auth.public_origin)
    token = result['cookie'].split(';', 1)[0].split('=', 1)[1]
    return {'session_token': token, 'csrf_token': result['csrf_token'], 'cookie': result['cookie']}


def owner_config() -> dict[str, Any]:
    """The `owner` block of an API config for tests (no jwks_file: tests inject keys directly)."""
    return {'issuer': OWNER_ISSUER, 'audience': OWNER_AUDIENCE, 'subjects': [OWNER_SUBJECT]}


def hf_era_document(role: str) -> Any:
    """One document of the HF-era test world (tests/data_hf_era_documents.json), as a fresh copy."""
    from pathlib import Path
    return json.loads(Path(__file__).with_name('data_hf_era_documents.json').read_text())['documents'][role]


def runtime_config(documents: dict[str, Any] | None = None, *, methods: dict[str, Any] | None = None) -> RuntimeConfig:
    """The real runtime.json with test documents (by their old release role names) and methods swapped in."""
    config = RuntimeConfig.load()
    if methods is not None:
        config.set('methods', {'methods': methods})
    for role, value in (documents or {}).items():
        config.set(role, value)
    return config


class Authority:
    """In-memory blob authority for external-record tests (was test_cloud_store.Authority; blob part only)."""
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.puts = 0
        self.gets: list[str] = []


class Transport:
    """Blob transport over an Authority: exactly what ReviewPayloads needs (put_blob, get_blob)."""
    def __init__(self, authority: Authority, role: str = 'operator') -> None:
        self.authority, self.role = authority, role

    def put_blob(self, sha: str, data: bytes) -> dict[str, Any]:
        import hashlib
        assert hashlib.sha256(data).hexdigest() == sha
        self.authority.blobs.setdefault(sha, data)
        self.authority.puts += 1
        return {'sha256': sha, 'size': len(data)}

    def get_blob(self, sha: str) -> bytes:
        import hashlib

        from production.contracts import DomainError
        self.authority.gets.append(sha)
        data = self.authority.blobs[sha]
        if hashlib.sha256(data).hexdigest() != sha:
            raise DomainError('provider_failure', 'Corrupt blob')
        return data


class FakeProvider:
    """A generation adapter for tests with the interface the kept adapters (fal, apilio) give the worker.

    It cannot list its jobs, so a lost answer after sending stays an unknown outcome for the operator.
    `transport(action, request)` plays the provider: `action` is 'create' or 'get'; it returns the provider's
    record {'id', 'status', 'result_url'?} or raises. A raise after a create is an unknown outcome (the call may
    have run; never resent); a raise on a get is a failed read the worker retries later.
    """
    STATES = {'completed': 'succeeded', 'queued': 'submitted', 'in_progress': 'running', 'failed': 'failed'}

    def __init__(self, job_types: Any, transport: Any, *, timeout: float = 1) -> None:
        self.job_types = frozenset(job_types)
        self.transport = transport
        self.fake, self.live_enabled, self.timeout = True, False, float(timeout)

    def capabilities(self, job_type: str) -> dict[str, Any]:
        return {'job_type': job_type}

    def can_list(self, job_type: str) -> bool:
        return False

    def _isolation(self) -> dict[str, str]:
        return {}

    def _parameters(self, request: dict[str, Any], *, has_references: bool) -> list[str]:
        from production.contracts import DomainError
        if request.get('job_type') not in self.job_types:
            raise DomainError('unsupported_route', 'Model was not pinned by this service')
        return []

    def _references(self, request: dict[str, Any], resolved: list[Any]) -> list[str]:
        return [str(r.path) for r in resolved]

    def submit(self, intent: dict[str, Any], resolved: list[Any], **_: Any) -> Any:
        from production.contracts import DomainError
        if not self.fake and not self.live_enabled:
            raise DomainError('unsupported_route', 'Live generation requires explicit service enablement')
        if intent.get('cost', {}).get('mode') != ('fake' if self.fake else 'live'):
            raise DomainError('forbidden', 'Adapter mode differs from service-authorized intent')
        try:
            raw = self.transport('create', intent['request'])
        except DomainError:
            raise
        except Exception as exc:  # noqa: BLE001 -- the paid call may have run; never resend.
            raise DomainError('unknown_outcome', 'No answer after sending; no automatic retry') from exc
        return self._receipt(raw, intent['request'], submission=True)

    def get(self, job_id: str, expected_request: dict[str, Any], **_: Any) -> Any:
        from production.contracts import DomainError
        try:
            raw = self.transport('get', expected_request)
        except DomainError:
            raise
        except Exception as exc:  # noqa: BLE001 -- a read is free; the worker polls again.
            raise DomainError('provider_failure', 'Status read failed; the worker polls again') from exc
        return self._receipt(raw, expected_request)

    def list_recent(self, job_type: str) -> list[dict[str, Any]]:
        from production.contracts import DomainError
        raise DomainError('unsupported_route', 'This provider cannot list its jobs; the operator settles them')

    def _receipt(self, raw: Any, request: dict[str, Any], *, submission: bool = False) -> Any:
        from production.contracts import DomainError
        from production.provider_types import ProviderReceipt
        job_id = raw.get('id') if isinstance(raw, dict) else None
        if not isinstance(job_id, str) or not job_id or not isinstance(raw.get('status'), str):
            raise DomainError('unknown_outcome' if submission else 'provider_failure',
                              'Unverified or malformed provider receipt; no automatic retry')
        state = self.STATES.get(raw['status'], 'unknown')
        url = raw.get('result_url') if state == 'succeeded' else None
        if state == 'succeeded' and not url:
            state = 'unknown'
        return ProviderReceipt(job_id, request['job_type'], raw['status'], state, {'id': job_id, 'status': raw['status']},
                               {}, {}, result_url=url)
