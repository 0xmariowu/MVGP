"""Real service bootstrap and CLI requests against the mounted API/viewer."""
import hashlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from production.cli import main as cli_main
from production.contracts import DomainError
from production.server import app_factory, build_server, load_config
from production.tests import test_craft_journey


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.c = test_craft_journey.CraftJourneyTests()
        self.c.setUp()
        self.addCleanup(self.c.doCleanups)
        self.root = self.c.root.resolve()
        self.config = {'storage': {'mode': 'local', 'database': str(self.c.store.path.resolve()),
                                  'media_root': str(self.c.media.root.resolve())},
                       'public_origin': 'https://craft.example'}
        self.path = self.root/'api.json'
        self.write_config()
        self.token = self.c.auth.provision_token('api_maker', 'agent', [self.c.pid], 300, allow_create_project=True)
        self.headers = {'Authorization': 'Bearer '+self.token}

    def write_config(self):
        self.path.write_text(json.dumps(self.config))
        self.path.chmod(0o600)

    def client(self):
        self.app = build_server(self.path)
        client = TestClient(self.app, base_url='https://craft.example')
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def owner_config(self):
        """The owner block with a private local JWKS file (the rehearsal seam), so the real login path runs."""
        from production.tests.fixtures import owner_config, owner_jwks
        jwks = Path(self.path).parent / 'owner-jwks.json'
        jwks.write_text(json.dumps(owner_jwks()))
        return {**owner_config(), 'jwks_file': str(jwks)}

    def test_the_local_desk_opens_without_a_login_only_from_its_own_page(self):
        # (owner 2026-09-27): only on this Mac, the public address closed.
        self.config.update(public_origin='http://localhost:8811', local_owner=True)
        self.write_config()
        self.app = build_server(self.path)
        client = TestClient(self.app, base_url='http://localhost:8811')
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        page = {'Origin': 'http://localhost:8811', 'Sec-Fetch-Site': 'same-origin'}
        self.assertEqual(client.post('/v1/session/access', json={}, headers={'Origin': 'http://localhost:8811'}).status_code, 403)
        self.assertEqual(client.post('/v1/session/access', json={}, headers={**page, **self.headers}).status_code, 403)
        opened = client.post('/v1/session/access', json={}, headers=page)
        self.assertEqual(opened.status_code, 200, opened.text)
        self.assertEqual(client.get('/v1/session').json()['role'], 'human')
        other = TestClient(self.app, base_url='http://127.0.0.1:8811')
        self.assertEqual(other.get('/review').status_code, 403)  # only the one local address is served
        for change in ({'public_origin': 'https://craft.example'}, {'owner': self.owner_config()}):
            bad = {**self.config, **change}
            self.path.write_text(json.dumps(bad))
            with self.subTest(change=list(change)), self.assertRaises(DomainError):
                load_config(self.path)

    def test_owner_login_is_wired_from_config_and_needs_no_employee_keys(self):
        # no vault keyring or Access read token; only the owner block.
        from production.tests.fixtures import owner_jwt
        self.config['owner'] = self.owner_config()
        self.write_config()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('MVGP_VAULT_KEYRING', None)
            os.environ.pop('MVGP_ACCESS_READ_TOKEN', None)
            client = self.client()
        refused = client.post('/v1/session/access', json={},
                              headers={'Origin': 'https://craft.example', 'Cf-Access-Jwt-Assertion': owner_jwt(sub='intruder')})
        self.assertEqual(refused.status_code, 403, refused.text)
        response = client.post('/v1/session/access', json={},
                               headers={'Origin': 'https://craft.example', 'Cf-Access-Jwt-Assertion': owner_jwt()})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(client.get('/v1/session').json()['role'], 'human')
        self.assertEqual(client.get('/v1/personal/agents').status_code, 404)

    def test_complete_shared_services_and_readonly_viewer_mounts(self):
        client = self.client()
        services = self.app.state.services
        # no qualification, composition, lessons or AI review services are built.
        for name in ('qualification', 'composition', 'lessons', 'review_tasks'):
            self.assertFalse(hasattr(services.mutations, name), name)
        self.assertFalse(hasattr(services.mutations.reviews, 'qualification'))
        self.assertFalse(hasattr(services.mutations.submissions, 'review_tasks'))
        self.assertIsNone(services.mutations.submissions.review_check)
        self.assertIsNone(services.mutations.decisions.receipt_check)
        self.assertIsNone(services.queries.resolution_check)
        self.assertEqual(services.media.max_bytes, 90*1024*1024)
        for path in ('/projects', '/projects/'+self.c.pid, '/projects/'+self.c.pid+'/artifacts/example'):
            response = client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn('拍片工作台', response.text)
            self.assertIn("frame-ancestors 'none'", response.headers['content-security-policy'])
        for path in ('/viewer/app.js', '/viewer/style.css', '/viewer/fonts/IBMPlexSans-400-latin.woff2'):
            self.assertEqual(client.get(path).status_code, 200)
        review = client.get('/review')
        self.assertEqual(review.status_code, 200)
        self.assertIn('看片', review.text)
        self.assertIn("script-src 'self'", review.headers['content-security-policy'])
        for path in ('/viewer/review.js', '/viewer/review.css'):
            self.assertEqual(client.get(path).status_code, 200)
        # Owner 2026-09-24: typing the bare address must open the review desk, not a JSON 404.
        home = client.get('/', follow_redirects=False)
        self.assertEqual((home.status_code, home.headers['location']), (307, '/review'))
        # the HF-style project page and its files.
        page = client.get('/p/' + self.c.pid)
        self.assertEqual(page.status_code, 200)
        self.assertIn('MVGP 项目', page.text)
        self.assertIn("script-src 'self'", page.headers['content-security-policy'])
        for path in ('/viewer/project.js', '/viewer/project.css'):
            self.assertEqual(client.get(path).status_code, 200)
        self.assertEqual(client.get('/v1/projects').status_code, 401)
        self.assertEqual(client.get('/ready').json()['schema_version'], 2)
        self.assertEqual(client.get('/ready', headers={'Host': 'evil.example'}).status_code, 403)
        for path in ('/v1/operator', '/internal/storage/head', '/v1/raw-submit', '/viewer/operations.py'):
            self.assertEqual(client.get(path).status_code, 404)

    def test_actual_cli_discovery_and_project_creation_use_authority_graph(self):
        client = self.client()
        def wire(request):
            response = client.request(request.method, request.url.raw_path.decode(),
                                      headers=dict(request.headers), content=request.content)
            return httpx.Response(response.status_code, content=response.content, headers=response.headers)
        env = {'MVGP_URL': 'https://craft.example', 'MVGP_TOKEN': self.token}
        out, err = io.StringIO(), io.StringIO()
        status = cli_main(['discovery'], environ=env, stdout=out, stderr=err, transport=httpx.MockTransport(wire))
        self.assertEqual(status, 0, err.getvalue())
        self.assertTrue(json.loads(out.getvalue())['production_mutations_available'])
        response = client.post('/v1/projects', headers=self.headers,
            json={'idempotency_key': 'new-film', 'title': 'A test film', 'branch': 'original'})
        self.assertEqual(response.status_code, 201, response.text)
        pid = response.json()['project_id']
        self.assertEqual(client.get('/v1/projects/'+pid, headers=self.headers).status_code, 200)
        self.assertEqual(client.post('/v1/projects/'+pid+'/submit', headers=self.headers, json={}).status_code, 404)

    def test_new_project_is_funded_and_on_the_owners_desk_from_config(self):

        # runtime.json's cost policy bills fal_owner in usd_micro.
        self.config['owner'] = self.owner_config()
        self.config['default_envelopes'] = [{'budget_key': 'fal_owner', 'unit': 'usd_micro', 'ceiling': 200_000_000}]
        self.write_config()
        client = self.client()
        response = client.post('/v1/projects', headers=self.headers,
            json={'idempotency_key': 'funded-film', 'title': 'A funded film', 'branch': 'original'})
        self.assertEqual(response.status_code, 201, response.text)
        pid = response.json()['project_id']
        self.assertEqual(self.c.store.budget(pid, budget_key='fal_owner')['ceiling'], 200_000_000)
        # The owner's desk shows the new project with no membership record.
        from production.tests.fixtures import owner_jwt
        login = client.post('/v1/session/access', json={},
                            headers={'Origin': 'https://craft.example', 'Cf-Access-Jwt-Assertion': owner_jwt()})
        self.assertEqual(login.status_code, 200, login.text)
        self.assertIn(pid, client.get('/v1/session').json()['projects'])
        self.assertEqual(self.c.store.list_objects('platform_system', kind='project-membership'), [])
        self.config['default_envelopes'] = [{'budget_key': 'nobody_bills_this', 'unit': 'usd_micro', 'ceiling': 1}]
        self.write_config()
        with self.assertRaises(DomainError):
            build_server(self.path)

    def test_live_enablement_requires_private_config_and_preserves_release_controls(self):
        self.client()
        self.assertFalse(self.app.state.services.mutations.submissions.live_enabled)
        self.config['live_enabled'] = True
        self.write_config()
        self.client()
        submissions = self.app.state.services.mutations.submissions
        self.assertTrue(submissions.live_enabled)
        self.assertFalse(hasattr(submissions, 'review_tasks'))
        policy = {'operations': {'submit': {'image': {
            'mode': 'live', 'budget_key': 'hf', 'budget_unit': 'credits',
            'estimated_cost': 1, 'reservation': 1, 'max_attempts': 1,
            'live_controls': {'pricing_verified': True, 'isolation_verified': False,
                              'account_limits_verified': True}}}}}
        with patch.object(submissions.workflow.config, 'section', return_value=policy):
            with self.assertRaises(DomainError) as failure:
                submissions._policy('submit', 'image')
            self.assertEqual(failure.exception.code, 'forbidden')

    def test_live_enablement_rejects_coerced_configuration_values(self):
        for value in ('true', 'false', 1, 0, None):
            with self.subTest(value=value):
                self.config['live_enabled'] = value
                self.write_config()
                with self.assertRaises(DomainError):
                    load_config(self.path)

    def test_native_media_range_and_viewer_session_through_real_bootstrap(self):
        client = self.client()
        image = io.BytesIO()
        Image.new('RGB', (16, 12), 'green').save(image, format='PNG')
        raw = image.getvalue()
        metadata = {'idempotency_key': 'image', 'logical_path': 'assets/reference.png', 'media_type': 'image/png',
                    'byte_length': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        response = client.post('/v1/projects/'+self.c.pid+'/uploads', content=raw,
            headers={**self.headers, 'X-MVGP-Upload': json.dumps(metadata), 'Content-Type': 'application/octet-stream'})
        self.assertEqual(response.status_code, 201, response.text)
        oid = response.json()['object_ref']['object_id']
        viewer = self.c.auth.provision_token('viewer', 'viewer', [self.c.pid], 300)
        response = client.post('/v1/session/exchange', json={'kind': 'viewer', 'secret': viewer},
                                headers={'Origin': 'https://craft.example'})
        self.assertEqual(response.status_code, 200)
        response = client.get('/v1/projects/'+self.c.pid+'/media/'+oid+'?revision=1', headers={'Range': 'bytes=2-9'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, raw[2:10])

    def test_unsafe_config_unknown_fields_duplicate_keys_and_runtime_mismatch_fail(self):
        for change in ({'provider_key': 'DO_NOT_ECHO'}, {'public_origin': 'http://remote.example'},
                       {'factory': 'untrusted.module'}, {'release_id': 'release_'+'b'*64},
                       {'deployment_root': '/tmp'}, {'runtime_files': ['x']}):
            previous = dict(self.config)
            self.config.update(change)
            self.write_config()
            with self.assertRaises((DomainError, ValueError)) as failure:
                build_server(self.path)
            self.assertNotIn('DO_NOT_ECHO', str(failure.exception))
            self.config = previous
        self.write_config()
        self.path.chmod(0o644)
        with self.assertRaises(DomainError):
            load_config(self.path)
        self.path.chmod(0o600)
        raw = self.path.read_text()
        self.path.write_text(raw[:-1]+',"public_origin":"https://craft.example"}')
        with self.assertRaises(DomainError):
            load_config(self.path)

    def test_factory_requires_explicit_private_config_and_never_provider_credentials(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(DomainError):
            app_factory()
        with patch.dict(os.environ, {'MVGP_API_CONFIG': str(self.path), 'APILIO_AI_KEY': 'DO_NOT_ECHO'}):
            app = app_factory()
            with TestClient(app, base_url='https://craft.example') as client:
                response = client.get('/v1/discovery', headers=self.headers)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('DO_NOT_ECHO', response.text)


if __name__ == '__main__':
    unittest.main()
