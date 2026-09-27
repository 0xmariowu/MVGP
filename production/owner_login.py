"""The owner's desk login: Cloudflare Access proves who is at the browser, the
configured owner subjects say whether that person is the owner. No employee directory, no group lookup.

The Access JWT is verified with its signature, issuer, audience and expiry (access_identity.py). A session is
issued only to a configured (issuer, subject) and only for the configured public origin; its CSRF token is
returned once and only its hash is stored. Picks still need that cookie + origin + CSRF (auth.py:257-261).
"""
from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from typing import Any

from production.access_identity import AccessIdentity, verify_access_identity
from production.auth import OWNER_ACTOR, SESSION_SECONDS, AuthService, session_cookie
from production.contracts import DomainError



class OwnerLogin:
    def __init__(self, auth: AuthService, *, issuer: str, audience: str, subjects: list[str],
                 jwks: Callable[[], dict[str, Any]], jwks_fresh: Callable[[], dict[str, Any]] | None = None) -> None:
        if not subjects or any(not isinstance(s, str) or not s for s in subjects):
            raise ValueError('The owner login needs at least one configured owner subject')
        self.auth, self.issuer, self.audience = auth, issuer, audience
        self.subjects = frozenset(subjects)
        self.jwks, self.jwks_fresh = jwks, jwks_fresh

    def _identity(self, access_token: str) -> AccessIdentity:
        try:
            return verify_access_identity(access_token, self.jwks(), issuer=self.issuer, audience=self.audience)
        except DomainError:
            if self.jwks_fresh is None:
                raise
            # Keys are cached; one refetch covers a key Cloudflare just rotated in.
            return verify_access_identity(access_token, self.jwks_fresh(), issuer=self.issuer, audience=self.audience)

    def login(self, access_token: str, origin: str, *, fetch_site: str = '') -> dict[str, str]:
        if origin != self.auth.public_origin:
            raise DomainError('forbidden', 'The owner login requires the configured origin')
        identity = self._identity(access_token)
        if identity.issuer != self.issuer or identity.subject not in self.subjects:
            raise DomainError('forbidden', 'Only the owner can sign in to the desk')
        ttl = min(SESSION_SECONDS, int(identity.expires_at - self.auth.clock()))
        if ttl < 1:
            raise DomainError('unauthorized', 'The owner login has expired')
        csrf = secrets.token_urlsafe(32)
        with self.auth.store.transaction() as db:
            # The Access subject is kept on the session, so a pick can be traced to the identity that made it.
            token = self.auth._issue(OWNER_ACTOR, 'human', [], ttl, 'human-session', db,
                                     csrf_hash=hashlib.sha256(csrf.encode()).hexdigest(), access_subject=identity.subject)
        return {'actor': OWNER_ACTOR, 'csrf_token': csrf,
                'cookie': session_cookie(self.auth.public_origin, token, ttl)}


class LocalOwnerLogin:
    """The desk on this Mac without a login (owner 2026-09-27: 只在这台 Mac 上看，公开地址关掉).

    The public tunnel is closed and the API listens on 127.0.0.1 with a `http://localhost:<port>` origin, so only this
    Mac reaches it. A session is issued to a browser request from the desk page itself: the exact origin and the
    browser's own `Sec-Fetch-Site: same-origin`. A bearer credential is refused before this is called (api.py), so an
    agent's token never opens the desk. Picks still need the session cookie, origin and CSRF token.
    """
    SUBJECT = 'local-mac'

    def __init__(self, auth: AuthService) -> None:
        from urllib.parse import urlsplit
        origin = urlsplit(auth.public_origin)
        if origin.scheme != 'http' or origin.hostname not in ('localhost', '127.0.0.1'):
            raise ValueError('The local owner login serves only a http://localhost origin')
        self.auth = auth

    def login(self, access_token: str, origin: str, *, fetch_site: str = '') -> dict[str, str]:
        if origin != self.auth.public_origin or fetch_site != 'same-origin':
            raise DomainError('forbidden', 'The desk opens only from its own page on this Mac')
        csrf = secrets.token_urlsafe(32)
        with self.auth.store.transaction() as db:
            token = self.auth._issue(OWNER_ACTOR, 'human', [], SESSION_SECONDS, 'human-session', db,
                                     csrf_hash=hashlib.sha256(csrf.encode()).hexdigest(), access_subject=self.SUBJECT)
        return {'actor': OWNER_ACTOR, 'csrf_token': csrf,
                'cookie': session_cookie(self.auth.public_origin, token, SESSION_SECONDS)}
