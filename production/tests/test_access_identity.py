"""Real RSA signatures; identity verification is not membership or permission."""
import copy
import json
import time
import unittest

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from production.access_identity import verify_access_identity
from production.contracts import DomainError

ISSUER = 'https://employees.cloudflareaccess.com'
AUD = 'a' * 64


class AccessIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key())),
                   'kid': 'rotation-key-1', 'alg': 'RS256', 'use': 'sig'}

    def claims(self, **changes):
        now = int(time.time())
        return {'iss': ISSUER, 'aud': [AUD], 'sub': 'employee-123',
                'email': 'employee@example.test', 'type': 'app',
                'iat': now - 5, 'nbf': now - 5, 'exp': now + 300, **changes}

    def token(self, claims=None, key=None, **headers):
        return jwt.encode(self.claims() if claims is None else claims, key or self.key,
                          algorithm='RS256', headers={'kid': 'rotation-key-1', **headers})

    def verify(self, token, jwks=None, **config):
        return verify_access_identity(token, {'keys': [self.jwk]} if jwks is None else jwks,
                                      issuer=config.get('issuer', ISSUER), audience=config.get('audience', AUD))

    def denied(self, token, **kwargs):
        with self.assertRaises(DomainError) as caught:
            self.verify(token, **kwargs)
        self.assertEqual(caught.exception.code, 'unauthorized')
        if token:
            self.assertNotIn(token, str(caught.exception))

    def test_verified_employee_has_identity_only(self):
        identity = self.verify(self.token())
        self.assertEqual(identity.subject, 'employee-123')
        self.assertEqual(identity.email, 'employee@example.test')
        self.assertEqual(identity.issuer, ISSUER)
        self.assertFalse(hasattr(identity, 'role'))
        self.assertFalse(hasattr(identity, 'project_ids'))

    def test_wrong_signature_algorithm_and_unsigned_are_denied(self):
        self.denied(self.token(key=self.other))
        self.denied(jwt.encode(self.claims(), 'synthetic-' * 5, algorithm='HS256', headers={'kid': 'rotation-key-1'}))
        self.denied(jwt.encode(self.claims(), '', algorithm='none', headers={'kid': 'rotation-key-1'}))

    def test_expired_future_missing_and_wrong_type_dates(self):
        for changes in [{'exp': int(time.time()) - 1}, {'iat': int(time.time()) + 60},
                        {'nbf': int(time.time()) + 60}, {'exp': '9999999999'}, {'iat': False}]:
            with self.subTest(changes=changes):
                self.denied(self.token(self.claims(**changes)))
        for name in ['iss', 'aud', 'sub', 'email', 'type', 'iat', 'exp']:
            claims = self.claims(); del claims[name]
            with self.subTest(missing=name):
                self.denied(self.token(claims))

    def test_wrong_app_issuer_and_global_session(self):
        for changes in [{'aud': ['b' * 64]}, {'iss': 'https://other.cloudflareaccess.com'}, {'type': 'org'}]:
            self.denied(self.token(self.claims(**changes)))

    def test_service_identity_never_becomes_employee(self):
        for changes in [{'sub': '', 'common_name': 'service.access'}, {'common_name': 'service.access'},
                        {'service_token_status': True}, {'service_token_id': 'service.access'}]:
            self.denied(self.token(self.claims(**changes)))

    def test_claim_strings_and_input_bounds(self):
        for changes in [{'email': 'bad\r\nvalue@example.test'}, {'email': 'missing-at'}, {'sub': ' '},
                        {'sub': ['employee']}, {'email': 123}]:
            self.denied(self.token(self.claims(**changes)))
        for token in ['', 'x' * 16385, 'a.b.c', 'é.x.y']:
            self.denied(token)

    def test_header_cannot_select_network_or_alternate_key(self):
        for headers in [{'jku': 'https://evil.test/keys'}, {'x5u': 'https://evil.test/cert'},
                        {'crit': ['unexpected']}, {'kid': 'unknown'}]:
            self.denied(self.token(**headers))

    def test_signed_duplicate_claims_are_rejected(self):
        payload = json.dumps(self.claims())[:-1] + ',"email":"another@example.test"}'
        token = jwt.api_jws.encode(payload.encode(), self.key, algorithm='RS256', headers={'kid': 'rotation-key-1'})
        self.denied(token)

    def test_jwks_key_rotation_and_ambiguous_or_private_keys(self):
        other = {**self.jwk, 'kid': 'previous'}
        self.assertEqual(self.verify(self.token(), {'keys': [other, self.jwk]}).subject, 'employee-123')
        for keys in [[], [self.jwk] * 2, [self.jwk] * 9, [{**self.jwk, 'd': 'private'}],
                     [{**self.jwk, 'alg': 'HS256'}], [{**self.jwk, 'use': 'enc'}]]:
            self.denied(self.token(), jwks={'keys': keys})

    def test_fixed_trust_configuration(self):
        for issuer in ['http://employees.cloudflareaccess.com', 'https://evil.test', ISSUER + '/path']:
            self.denied(self.token(), issuer=issuer)
        self.denied(self.token(), audience='')
        original = copy.deepcopy(self.jwk)
        self.verify(self.token())
        self.assertEqual(original, self.jwk)


if __name__ == '__main__':
    unittest.main()
