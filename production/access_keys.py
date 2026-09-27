"""Bounded public Access signing-key retrieval from the configured issuer only.

Keys are reused for ten minutes (every login fetched them, 0.2-1.2 s each).
Past that age a failed fetch fails closed: stale keys are never served.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from production.access_identity import ISSUER, _unique
from production.contracts import DomainError

TTL_SECONDS = 600


class AccessKeys:
    def __init__(self, issuer: str, *, transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not isinstance(issuer, str) or not ISSUER.fullmatch(issuer):
            raise ValueError('A fixed Cloudflare Access issuer is required')
        self._url = issuer + '/cdn-cgi/access/certs'
        self._clock = clock
        self._client = httpx.Client(timeout=10, follow_redirects=False, trust_env=False,
                                    headers={'Accept-Encoding':'identity'}, transport=transport)
        self._cached: tuple[float, dict[str, Any]] | None = None

    def close(self) -> None:
        self._client.close()

    def get(self, *, fresh: bool = False) -> dict[str, Any]:
        now = self._clock()
        if not fresh and self._cached is not None and 0 <= now - self._cached[0] < TTL_SECONDS:
            return self._cached[1]
        value = self._fetch()
        self._cached = (now, value)
        return value

    def _fetch(self) -> dict[str, Any]:
        try:
            deadline = self._clock() + 10
            data = bytearray()
            with self._client.stream('GET', self._url) as response:
                if response.status_code != 200 or response.headers.get('content-encoding', 'identity') != 'identity':
                    raise ValueError
                for chunk in response.iter_bytes(chunk_size=8192):
                    if len(data) + len(chunk) > 65536 or self._clock() >= deadline:
                        raise ValueError
                    data.extend(chunk)
            value = json.loads(data, object_pairs_hook=_unique)
            if (not isinstance(value, dict) or not isinstance(value.get('keys'), list)
                    or not 1 <= len(value['keys']) <= 8
                    or any(not isinstance(key, dict) for key in value['keys'])):
                raise ValueError
            return value
        except (httpx.HTTPError, ValueError, TypeError, KeyError, RecursionError):
            raise DomainError('unauthorized', 'Employee signing keys are unavailable') from None
