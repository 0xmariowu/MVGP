"""Trusted fixed-origin key discovery does not follow JWT-controlled URLs."""
import unittest

import httpx

from production.access_keys import AccessKeys
from production.contracts import DomainError


class AccessKeysTests(unittest.TestCase):
    def test_fixed_path_no_credentials_reused_ten_minutes_then_no_stale_grant(self):
        # one fetch serves logins for ten minutes; past that a failed
        # fetch fails closed instead of serving the old keys.
        seen = []
        now = [1000.0]
        def handler(req):
            seen.append(req)
            if len(seen) > 1: return httpx.Response(503)
            return httpx.Response(200, json={'keys': [{'kid':'current'}]})
        keys = AccessKeys('https://employees.cloudflareaccess.com', transport=httpx.MockTransport(handler), clock=lambda: now[0])
        try:
            self.assertEqual(keys.get(), {'keys':[{'kid':'current'}]})
            self.assertEqual(str(seen[0].url), 'https://employees.cloudflareaccess.com/cdn-cgi/access/certs')
            self.assertNotIn('authorization', seen[0].headers)
            now[0] += 599
            self.assertEqual(keys.get(), {'keys':[{'kid':'current'}]})
            self.assertEqual(len(seen), 1)
            with self.assertRaises(DomainError): keys.get(fresh=True)  # a forced refetch skips the cache
            self.assertEqual(len(seen), 2)
            now[0] += 2
            with self.assertRaises(DomainError): keys.get()
            self.assertEqual(len(seen), 3)
        finally: keys.close()

    def test_bad_issuer(self):
        for issuer in ['http://employees.cloudflareaccess.com', 'https://evil.example',
                       'https://employees.cloudflareaccess.com/path', 'https://user@employees.cloudflareaccess.com']:
            with self.assertRaises(ValueError): AccessKeys(issuer)

    def test_redirect_oversized_duplicate_missing_keys_timeout_fail_closed(self):
        cases = [httpx.Response(302, headers={'location':'https://evil.example'}),
                 httpx.Response(200, content=b'x'*65537),
                 httpx.Response(200, content=b'{"keys":[],"keys":[{}]}'),
                 httpx.Response(200, json={'keys':[]}),
                 httpx.Response(200, json={'keys':[{}]*9}),
                 httpx.Response(200, json={'keys':[1]})]
        for response in cases:
            with self.subTest(status=response.status_code):
                keys=AccessKeys('https://employees.cloudflareaccess.com', transport=httpx.MockTransport(lambda _, response=response: response))
                try:
                    with self.assertRaises(DomainError): keys.get()
                finally: keys.close()
        def timeout(req): raise httpx.ReadTimeout('private diagnostic', request=req)
        keys=AccessKeys('https://employees.cloudflareaccess.com', transport=httpx.MockTransport(timeout))
        try:
            with self.assertRaises(DomainError) as exc: keys.get()
            self.assertNotIn('private diagnostic', str(exc.exception))
        finally: keys.close()
