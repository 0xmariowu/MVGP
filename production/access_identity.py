"""Verify employee identity evidence, without granting any platform authority.

Trust configuration and JWKS come from the service, never from request claims.
The caller still has to enforce current Access admission, member status and
project permissions. This verifier performs no network calls or account writes.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from production.contracts import DomainError

ISSUER = re.compile(r'https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com')
IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,256}')


@dataclass(frozen=True)
class AccessIdentity:
    issuer: str
    subject: str
    email: str
    issued_at: int
    expires_at: int


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JWT field')
        result[key] = value
    return result


def verify_access_identity(token: str, jwks: dict[str, Any], *, issuer: str, audience: str) -> AccessIdentity:
    """RS256 app tokens only. Errors deliberately omit tokens, email and key data."""
    try:
        if (not isinstance(issuer, str) or not ISSUER.fullmatch(issuer)
                or not isinstance(audience, str) or not re.fullmatch(r'[a-f0-9]{64}', audience)
                or not isinstance(token, str) or not 1 <= len(token) <= 16384
                or not token.isascii() or not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', token)):
            raise ValueError
        # Reject ambiguous JSON even when its signature would otherwise verify.
        header, payload, _signature = token.split('.')
        decoded = []
        for part in (header, payload):
            decoded.append(json.loads(jwt.utils.base64url_decode(part).decode('utf-8'), object_pairs_hook=_unique))
        head, claims = decoded
        if (not isinstance(head, dict) or not isinstance(claims, dict)
                or set(head) - {'alg', 'typ', 'kid'} or head.get('alg') != 'RS256'
                or head.get('typ', 'JWT') != 'JWT' or not isinstance(head.get('kid'), str)
                or not IDENTIFIER.fullmatch(head['kid'])):
            raise ValueError
        if (not isinstance(jwks, dict) or len(json.dumps(jwks)) > 65536
                or not isinstance(jwks.get('keys'), list) or not 1 <= len(jwks['keys']) <= 8):
            raise ValueError
        keys = jwks['keys']
        if any(not isinstance(key, dict) for key in keys):
            raise ValueError
        matching = [key for key in keys if key.get('kid') == head['kid']]
        if len(matching) != 1:
            raise ValueError
        selected = matching[0]
        if (set(selected) - {'kid', 'kty', 'alg', 'use', 'n', 'e', 'key_ops'}
                or selected.get('kty') != 'RSA' or selected.get('alg') != 'RS256'
                or selected.get('use') != 'sig' or selected.get('key_ops', ['verify']) != ['verify']):
            raise ValueError
        public_key = jwt.PyJWK.from_dict(selected, algorithm='RS256').key
        if not isinstance(public_key, RSAPublicKey) or not 2048 <= public_key.key_size <= 8192:
            raise ValueError
        verified = jwt.decode(token, public_key, algorithms=['RS256'], issuer=issuer, audience=audience,
                              options={'require': ['iss', 'aud', 'sub', 'email', 'type', 'iat', 'exp']})
        if (verified.get('type') != 'app' or any(name in verified for name in
                ('common_name', 'service_token_id', 'service_token_status'))
                or not isinstance(verified['sub'], str) or not IDENTIFIER.fullmatch(verified['sub'])
                or not isinstance(verified['email'], str) or len(verified['email']) > 254
                or not re.fullmatch(r'[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+', verified['email'])
                or any(type(verified[name]) is not int for name in ('iat', 'exp'))
                or not 0 <= verified['iat'] < verified['exp']
                or ('nbf' in verified and type(verified['nbf']) is not int)):
            raise ValueError
        return AccessIdentity(issuer, verified['sub'], verified['email'], verified['iat'], verified['exp'])
    except (jwt.PyJWTError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise DomainError('unauthorized', 'Employee identity is invalid or expired') from None
