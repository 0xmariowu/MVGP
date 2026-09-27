"""Only a configured owner identity, proven by a signed Access JWT at the configured origin, gets a desk session."""
import json
import tempfile
import time
import unittest
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from production.auth import AuthService
from production.contracts import DomainError
from production.owner_login import LocalOwnerLogin, OwnerLogin
from production.store import Store

ISSUER = 'https://owner.cloudflareaccess.com'
AUD = 'b' * 64
ORIGIN = 'https://studio.example'


class OwnerLoginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key())),
                   'kid': 'k1', 'alg': 'RS256', 'use': 'sig'}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'owner.sqlite')
        self.auth = AuthService(self.store, ORIGIN)
        self.login = OwnerLogin(self.auth, issuer=ISSUER, audience=AUD, subjects=['owner-subject'],
                                jwks=lambda: {'keys': [self.jwk]})

    def token(self, key=None, **changes):
        now = int(time.time())
        claims = {'iss': ISSUER, 'aud': [AUD], 'sub': 'owner-subject', 'email': 'owner@example.test',
                  'type': 'app', 'iat': now - 5, 'exp': now + 300, **changes}
        return jwt.encode(claims, key or self.key, algorithm='RS256', headers={'kid': 'k1'})

    def denied(self, token, origin=ORIGIN):
        with self.assertRaises(DomainError) as caught:
            self.login.login(token, origin)
        self.assertIn(caught.exception.code, ('unauthorized', 'forbidden'))
        with self.store.transaction(write=False) as db:
            sessions = db.execute("SELECT count(*) FROM objects WHERE kind='human-session'").fetchone()[0]
        self.assertEqual(sessions, 0)

    def test_the_owner_gets_a_cookie_session_with_a_hashed_csrf_token(self):
        result = self.login.login(self.token(), ORIGIN)
        self.assertIn('HttpOnly', result['cookie'])
        self.assertIn('SameSite=Strict', result['cookie'])
        self.assertIn('Secure', result['cookie'])
        session_token = result['cookie'].split(';')[0].split('=', 1)[1]
        principal = self.auth.authenticate(session_token, channel='cookie')
        self.assertEqual((principal.actor_id, principal.role), ('owner', 'human'))
        with self.store.transaction(write=False) as db:
            body = json.loads(db.execute("SELECT r.body FROM objects o JOIN revisions r USING(project_id, object_id)"
                                         " WHERE o.kind='human-session'").fetchone()[0])
        self.assertNotIn(result['csrf_token'], json.dumps(body))
        self.assertNotIn('member_id', body)
        self.assertEqual(body['access_subject'], 'owner-subject')  # which of the owner's identities signed in

    def test_a_session_is_not_a_bearer_credential(self):
        session_token = self.login.login(self.token(), ORIGIN)['cookie'].split(';')[0].split('=', 1)[1]
        with self.assertRaises(DomainError):
            self.auth.authenticate(session_token, channel='bearer')

    def test_another_person_is_refused(self):
        self.denied(self.token(sub='someone-else'))

    def test_wrong_audience_issuer_or_signature_is_refused(self):
        self.denied(self.token(aud=['c' * 64]))
        self.denied(self.token(iss='https://other.cloudflareaccess.com'))
        self.denied(self.token(key=self.other))

    def test_expired_token_is_refused(self):
        now = int(time.time())
        self.denied(self.token(iat=now - 600, exp=now - 1))

    def test_wrong_origin_is_refused(self):
        self.denied(self.token(), origin='https://evil.example')

    def test_missing_or_garbage_token_is_refused(self):
        self.denied('')
        self.denied('not-a-jwt')

    def test_a_rotated_key_is_fetched_once(self):
        calls = []
        login = OwnerLogin(self.auth, issuer=ISSUER, audience=AUD, subjects=['owner-subject'],
                           jwks=lambda: {'keys': [{**self.jwk, 'kid': 'old'}]},
                           jwks_fresh=lambda: calls.append(1) or {'keys': [self.jwk]})
        self.assertEqual(login.login(self.token(), ORIGIN)['actor'], 'owner')
        self.assertEqual(calls, [1])

    def test_no_owner_configured_is_a_startup_error(self):
        with self.assertRaises(ValueError):
            OwnerLogin(self.auth, issuer=ISSUER, audience=AUD, subjects=[], jwks=lambda: {'keys': []})



class LocalOwnerLoginTests(unittest.TestCase):
    """the desk on this Mac opens without a login, only from its own page."""
    LOCAL = 'http://localhost:8811'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'local.sqlite')
        self.auth = AuthService(self.store, self.LOCAL)
        self.login = LocalOwnerLogin(self.auth)

    def test_the_desk_page_gets_the_owner_session(self):
        result = self.login.login('', self.LOCAL, fetch_site='same-origin')
        token = result['cookie'].split(';')[0].split('=', 1)[1]
        principal = self.auth.authenticate(token, channel='cookie')
        self.assertEqual((principal.actor_id, principal.role), ('owner', 'human'))
        self.auth.authorize(principal, None, 'projects')

    def test_anything_but_the_desk_page_on_this_origin_is_refused(self):
        for origin, site in ((self.LOCAL, ''), (self.LOCAL, 'cross-site'), (self.LOCAL, 'none'),
                             ('http://evil.example', 'same-origin'), ('', 'same-origin')):
            with self.subTest(origin=origin, site=site), self.assertRaises(DomainError):
                self.login.login('', origin, fetch_site=site)
        with self.store.transaction(write=False) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM objects WHERE kind='human-session'").fetchone()[0], 0)

    def test_it_serves_only_a_localhost_origin(self):
        for origin in ('https://mvgp.example', 'http://mvgp.example'):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                LocalOwnerLogin(AuthService(self.store, origin))


if __name__ == '__main__':
    unittest.main()
