"""Real HTTP identity, immutable reads and native media-range boundaries."""
import copy
import hashlib
import io
import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from production.api import MutationServices, Services, create_app
from production.context import ContextService
from production.contracts import ObjectRef
from production.decisions import Decisions
from production.media import MediaStore
from production.queries import Queries
from production.shoot import Shoot
from production.tests.fixtures import owner_jwt, owner_login
from production.tests import test_context, test_craft_journey


class ReadAPITests(unittest.TestCase):
    def setUp(self):
        self.c = test_context.ContextTests()
        self.c.setUp()
        self.addCleanup(self.c.doCleanups)
        self.f = self.c.fixture
        self.store, self.auth = self.f.store, self.f.auth
        self.media = MediaStore(self.store, Path(self.f.tmp.name)/'media')
        self.q = Queries(self.store, self.auth, self.f.flow)
        self.app = create_app(Services(self.store, self.auth, self.q, self.c.context, self.media), owner_login=owner_login(self.auth))
        self.client = TestClient(self.app, base_url='https://studio.example')
        self.addCleanup(self.client.close)
        self.viewer = self.auth.provision_token('viewer', 'viewer', ['project_1'], 300)
        self.headers = {'Authorization': 'Bearer '+self.viewer}
        image = io.BytesIO()
        Image.new('RGB', (12, 8), 'green').save(image, format='PNG')
        self.raw = image.getvalue()
        self.image = self.media.put('project_1', [self.raw], 'image/png', 'uploader')
        self.url = f'/v1/projects/project_1/media/{self.image["object_id"]}?revision=1'

    def get(self, path, **kwargs):
        return self.client.get(path, headers=self.headers, **kwargs)

    def test_read_routes_share_one_fresh_snapshot_with_authentication(self):
        paths = ['/v1/session', '/v1/projects', '/v1/projects/project_1',
                 '/v1/projects/project_1/tree', '/v1/projects/project_1/review-feed',
                 '/v1/projects/project_1/project-tree', '/v1/projects/project_1/generation-record',
                 '/v1/projects/project_1/artifacts/' + self.image['object_id']]
        for path in paths:
            with self.subTest(path=path), patch.object(
                    self.store, 'transaction', wraps=self.store.transaction) as transactions:
                response = self.get(path)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(transactions.call_count, 1)
                self.assertEqual(transactions.call_args.kwargs, {'write': False})
        self.auth.revoke(self.auth.authenticate(self.viewer).credential_id)
        for path in paths:
            self.assertEqual(self.get(path).status_code, 401)

    def test_the_manuals_are_handed_over_with_a_version_and_the_fetch_is_recorded(self):
        # (owner: 别嘴上说交了，结果没交出去).
        from production import playbook
        body = self.get('/v1/projects/project_1/playbook').json()
        self.assertEqual(body['version'], playbook.version())
        self.assertEqual([f['name'] for f in body['files']], [r['name'] for r in playbook.manifest()['files']])
        self.assertTrue(all(f['text'] and f['sha256'] for f in body['files']))
        viewer = self.auth.authenticate(self.viewer).credential_id
        agent_token = self.auth.provision_token('writer', 'agent', ['project_1'], 300)
        agent = self.auth.authenticate(agent_token).credential_id
        self.assertEqual(self.client.get('/v1/projects/project_1/playbook', headers={'Authorization': 'Bearer ' + agent_token}).status_code, 200)
        with self.store.transaction(write=False) as db:
            self.assertFalse(playbook.fetched(self.store, 'project_1', viewer, playbook.version(), conn=db))  # only a writer's fetch counts
            self.assertTrue(playbook.fetched(self.store, 'project_1', agent, playbook.version(), conn=db))
            self.assertFalse(playbook.fetched(self.store, 'project_1', 'credential_other', playbook.version(), conn=db))
            self.assertFalse(playbook.fetched(self.store, 'project_1', agent, 'pb-000000000000', conn=db))
        self.assertEqual(self.client.get('/v1/projects/project_1/playbook').status_code, 401)
        self.assertEqual(self.get('/v1/projects/project_other/playbook').status_code, 403)

    def test_slow_authentication_does_not_block_unrelated_http_requests(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        entered, release = threading.Event(), threading.Event()
        authenticate = self.auth.authenticate
        def slow_authenticate(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('Test did not release the blocked authentication')
            return authenticate(*args, **kwargs)
        # A shared TestClient context uses one app event loop for both requests.
        with self.client, ThreadPoolExecutor(max_workers=2) as pool, patch.object(
                self.auth, 'authenticate', side_effect=slow_authenticate):
            blocked = pool.submit(self.client.post, '/v1/projects/project_1/reference',
                headers=self.headers, json={'target': self.f.ref(self.image).model_dump()})
            try:
                self.assertTrue(entered.wait(2))
                health = pool.submit(self.client.get, '/health')
                self.assertEqual(health.result(timeout=2).status_code, 200)
                self.assertFalse(blocked.done())
            finally:
                release.set()
            self.assertEqual(blocked.result(timeout=3).status_code, 200)

    def test_read_discovery_project_history_and_context(self):
        self.assertEqual(self.client.get('/v1/projects').status_code, 401)
        projects = self.get('/v1/projects').json()
        self.assertEqual([p['project_id'] for p in projects], ['project_1'])
        discovery = self.get('/v1/discovery').json()
        self.assertFalse(discovery['production_mutations_available'])
        self.assertNotIn('force', discovery['contract']['operations'])
        oid = self.c.shot['object_id']
        path = f'/v1/projects/project_1/artifacts/{oid}'
        first = self.get(path).json()
        self.store.append_revision('project_1', oid, 1, {**self.c.shot['body'], 'note': 'new draft'}, 'author')
        self.assertEqual(self.get(path+'?revision=1').json()['object_ref'], first['object_ref'])
        self.assertEqual(len(self.get(path+'/history').json()), 2)
        context = self.client.post('/v1/projects/project_1/context', headers=self.headers,
            json={'target': self.f.ref(self.c.shot).model_dump(), 'method': self.f.ref(self.c.method).model_dump(), 'task': 'shot'})
        self.assertEqual(context.status_code, 200, context.text)
        self.assertIn('ENDING:', context.text)
        self.assertEqual(self.get('/v1/projects/project_1/methods').status_code, 200)
        self.assertEqual(self.get('/v1/projects/project_1/references/execution_policy').status_code, 200)
        self.assertEqual(self.get('/v1/projects/project_1/references/rules').status_code, 404)  # no rule catalog

    def test_private_kinds_cross_project_and_host_rejected(self):
        private = self.store.create_object('project_1', 'review-turn', {'messages':['PRIVATE_TRANSCRIPT']}, 'review_runner_service')
        response = self.get('/v1/projects/project_1/artifacts/'+private['object_id'])
        # Allowlisted viewer projection exposes turn status only, never transcript.
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('PRIVATE_TRANSCRIPT', response.text)
        secret = self.store.create_object('project_1', 'review-context', {'secret':'PRIVATE_CONTEXT'}, 'review_context_service')
        self.assertEqual(self.get('/v1/projects/project_1/artifacts/'+secret['object_id']).status_code, 403)
        self.store.create_project('other', {}, 'operator')
        for path in ('/v1/projects/other', '/v1/projects/other/tree', '/v1/projects/platform_system'):
            self.assertEqual(self.get(path).status_code, 403)
        self.assertEqual(self.client.get('/health', headers={'Host':'evil.example'}).status_code, 403)
        for path in ('/v1/raw-submit','/v1/projects/project_1/submit','/v1/rules','/v1/force'):
            self.assertEqual(self.client.post(path, headers=self.headers, json={}).status_code, 404)

    def test_media_requests_use_two_read_transactions(self):
        cases = [('GET', {}, 200, self.raw),
                 ('GET', {'Range': 'bytes=1-7'}, 206, self.raw[1:8]),
                 ('HEAD', {}, 200, b'')]
        for method, headers, status, content in cases:
            with self.subTest(method=method, headers=headers), patch.object(
                    self.store, 'transaction', wraps=self.store.transaction) as transactions:
                response = self.client.request(method, self.url, headers={**self.headers, **headers})
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.content, content)
                self.assertEqual(int(response.headers['content-length']),
                                 len(self.raw) if method == 'HEAD' else len(content))
                # The second transaction is path_for; sharing it is deferred.
                self.assertEqual(transactions.call_count, 2)
                for transaction in transactions.call_args_list:
                    self.assertEqual(transaction.kwargs, {'write': False})

    def test_media_ranges_head_and_revocation_on_every_request(self):
        full = self.get(self.url)
        self.assertEqual(full.content, self.raw)
        self.assertEqual(full.headers['content-type'], 'image/png')
        # Media is addressed by object revision, so its bytes never change under one URL:
        # the browser keeps it; every other response, including media errors, stays no-store.
        self.assertEqual(full.headers['cache-control'], 'private, max-age=31536000, immutable')
        self.assertEqual(full.headers['x-content-type-options'], 'nosniff')
        self.assertIn("frame-ancestors 'none'", full.headers['content-security-policy'])
        for raw, expected in [('bytes=1-7',self.raw[1:8]), ('bytes=-8',self.raw[-8:]), ('bytes=8-',self.raw[8:])]:
            response = self.client.get(self.url, headers={**self.headers,'Range':raw})
            self.assertEqual(response.status_code,206)
            self.assertEqual(response.content,expected)
        head = self.client.head(self.url, headers=self.headers)
        self.assertEqual(head.content,b'')
        self.assertEqual(int(head.headers['content-length']),len(self.raw))
        self.assertEqual(head.headers['cache-control'], 'private, max-age=31536000, immutable')
        self.assertEqual(self.client.get(self.url, headers={**self.headers,'Range':'bytes=1-7'}).headers['cache-control'],
                         'private, max-age=31536000, immutable')
        for value in ('bytes=999999-', 'bytes=10-1', 'bytes=-0', 'bytes=0-1,5-7', 'garbage'):
            response = self.client.get(self.url, headers={**self.headers,'Range':value})
            self.assertEqual(response.status_code,416)
            self.assertEqual(response.headers['cache-control'], 'private, no-store')
        self.assertEqual(self.client.get('/health', headers=self.headers).headers['cache-control'], 'private, no-store')
        self.auth.revoke(self.auth.authenticate(self.viewer).credential_id)
        denied = self.get(self.url)
        self.assertEqual(denied.status_code,401)
        self.assertEqual(denied.headers['cache-control'], 'private, no-store')

    def test_viewer_cookie_exchange_and_native_playback_never_returns_token(self):
        payload = {'kind':'viewer','secret':self.viewer}
        response = self.client.post('/v1/session/exchange', json=payload, headers={'Origin':'https://elsewhere.example'})
        self.assertEqual(response.status_code,403)
        response = self.client.post('/v1/session/exchange', json=payload, headers={'Origin':'https://studio.example'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json(),{'role':'viewer'})
        self.assertNotIn(self.viewer,response.text)
        self.assertIn('HttpOnly',response.headers['set-cookie'])
        self.assertEqual(self.client.get(self.url).content,self.raw)
        self.assertEqual(self.client.get('/v1/session').json()['role'],'viewer')
        self.assertEqual(self.client.get(self.url,headers=self.headers).status_code,401)
        self.assertEqual(self.client.post('/v1/session/logout').status_code,403)
        response = self.client.post('/v1/session/logout',headers={'Origin':'https://studio.example'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.client.get(self.url).status_code,401)

    def test_only_the_owner_access_login_makes_a_human_session(self):
        # the operator human-exchange backdoor is gone; the owner signs in through Access.
        token = self.auth.provision_token('maker','agent',['project_1'],300)
        response = self.client.post('/v1/session/exchange',json={'kind':'viewer','secret':token},
                                    headers={'Origin':'https://studio.example'})
        self.assertEqual(response.status_code,403)
        response = self.client.post('/v1/session/exchange',json={'kind':'human','secret':token},
                                    headers={'Origin':'https://studio.example'})
        self.assertEqual(response.status_code,422)  # no human exchange at all
        for headers in ({'cf-access-jwt-assertion': owner_jwt(sub='someone-else')},
                        {'cf-access-jwt-assertion': owner_jwt(), 'Authorization': 'Bearer '+token},
                        {}):
            with self.subTest(headers=sorted(headers)):
                refused = self.client.post('/v1/session/access',json={},headers={'Origin':'https://studio.example',**headers})
                self.assertIn(refused.status_code,(401,403))
        self.assertEqual(self.client.get('/v1/session').status_code,401)
        response = self.client.post('/v1/session/access',json={},
                                    headers={'Origin':'https://studio.example','cf-access-jwt-assertion':owner_jwt()})
        self.assertEqual(response.status_code,200)
        self.assertIn('csrf_token',response.json())
        self.assertEqual(self.client.get('/v1/session').json()['role'],'human')
        # the desk reads when the session lapses to renew it in time.
        self.assertGreater(self.client.get('/v1/session').json()['expires_at'], 0)

    def test_exact_reference_and_json_body_rejection_without_secret_echo(self):
        ref = self.f.ref(self.image).model_dump()
        response = self.client.post('/v1/projects/project_1/reference',json={'target':ref},headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['object_ref'],ref)
        for data in ('{"secret":"SECRET_SENTINEL","kind":"wrong"}',
                     '{"kind":"viewer","kind":"human","secret":"SECRET_SENTINEL"}',
                     '{"kind":"viewer","secret":"'+('x'*5000)+'SECRET_SENTINEL"}'):
            response = self.client.post('/v1/session/exchange',content=data,headers={'Content-Type':'application/json'})
            self.assertEqual(response.status_code,422)
            self.assertNotIn('SECRET_SENTINEL',response.text)
        response = self.client.post('/v1/projects/project_1/reference',content=json.dumps({'target':ref,'seconds':float('nan')}),
                                    headers={**self.headers,'Content-Type':'application/json'})
        self.assertEqual(response.status_code,422)
        response = self.get('/v1/projects/project_1/tree?parent=../private')
        self.assertEqual(response.status_code,422)

    def test_no_paid_read_side_effects(self):
        before = self.store.list_objects('project_1')
        for path in ('/v1/projects','/v1/projects/project_1','/v1/projects/project_1/tree',
                     '/v1/projects/project_1/review-feed','/v1/projects/project_1/project-tree',self.url):
            self.assertEqual(self.get(path).status_code,200)
        feed = self.get('/v1/projects/project_1/review-feed').json()
        self.assertEqual(set(feed), {'project_id','requests','selections','picks','shots','making','shooting','notes','sources','completions'})
        tree = self.get('/v1/projects/project_1/project-tree').json()
        self.assertEqual(set(tree), {'project_id','title','folders','items','notes','stage','brief'})
        record = self.get('/v1/projects/project_1/generation-record').json()
        self.assertEqual(set(record), {'project_id','rows','totals','by_model'})
        self.assertEqual(before,self.store.list_objects('project_1'))

    def test_services_cannot_mix_identity_instances(self):
        from production.auth import AuthService
        other = AuthService(self.store,'https://studio.example')
        context = ContextService(self.store,other,self.f.flow)
        with self.assertRaises(ValueError):
            Services(self.store,self.auth,self.q,context,self.media)


class MutationAPITests(unittest.TestCase):
    def setUp(self):
        self.c = test_craft_journey.CraftJourneyTests()
        self.c.setUp()
        self.addCleanup(self.c.doCleanups)
        self.pid, self.store, self.auth = self.c.pid, self.c.store, self.c.auth
        self.decisions = Decisions(self.store, self.auth, self.c.flow, self.c.cuts,
                                  human_confirmation_enabled=True)
        self.m = MutationServices(self.c.projects, self.c.compiler, self.c.submissions, self.c.reviews,
            self.c.batches, self.c.cuts, self.c.patches, self.decisions,
            Shoot(self.store, self.auth, self.c.projects, self.c.compiler, self.c.batches))
        self.services = Services(self.store, self.auth, Queries(self.store, self.auth, self.c.flow),
                                 self.c.context, self.c.media, self.m)
        self.app = create_app(self.services, owner_login=owner_login(self.auth))
        self.client = TestClient(self.app, base_url='https://craft.example')
        self.addCleanup(self.client.close)
        self.token = self.auth.provision_token('api_author', 'agent', [self.pid], 3600, allow_create_project=True)
        self.headers = {'Authorization': 'Bearer ' + self.token}
        self.viewer = {'Authorization': 'Bearer ' + self.auth.provision_token('viewer', 'viewer', [self.pid], 300)}

    def post(self, path, body, expected=200, headers=None):
        response = self.client.post('/v1/projects/' + self.pid + path, headers=headers or self.headers, json=body)
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    def draft(self, kind='scene', path='scene.md', content='A cart passes a marker.', deps=()):
        return self.post('/artifacts', {'idempotency_key': path.replace('/', ':'), 'expected_revision': 0,
            'kind': kind, 'logical_path': path, 'content': content, 'dependencies': list(deps)})

    def upload(self, raw=None, key='image', **updates):
        if raw is None:
            buf = io.BytesIO()
            Image.new('RGB', (160, 96), 'blue').save(buf, format='PNG')
            raw = buf.getvalue()
        meta = {'idempotency_key': key, 'logical_path': f'uploads/{key}.png', 'media_type': 'image/png',
                'byte_length': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), **updates}
        response = self.client.post(f'/v1/projects/{self.pid}/uploads', content=raw, headers={**self.headers,
            'Content-Type': 'application/octet-stream', 'X-MVGP-Upload': json.dumps(meta)})
        return response, meta, raw

    def candidate(self, n=1):
        script = self.draft('script', f'story-{n}.fountain', test_craft_journey.SOURCE)
        expected = self.draft('expectation', f'expected-{n}.md', '\n'.join(test_craft_journey.EXPECTED))
        scene = self.draft('scene', f'scene-{n}.md', 'S02 · EXT · Straight road · DAY\n' + test_craft_journey.SOURCE +
            '\n## GEO SPATIAL LAYOUT\nCamera ALWAYS stays south, NEVER crosses. East is frame-right.\n'
            '## ACTIVE REFERENCES\n@loc_lane @bluecart\n', [script['object_ref'], expected['object_ref']])
        shot = self.draft('shot', f'shots/{n}.json', self.c.card(n), [scene['object_ref']])
        selected = {}
        for role, tag in (('world', 'loc_lane'), ('visual', 'bluecart')):
            buf = io.BytesIO()
            Image.new('RGB', (160, 96), 'green' if role == 'world' else 'blue').save(buf, format='PNG')
            image, _, _ = self.upload(raw=buf.getvalue(), key=f'image{n}-{role}')
            self.assertEqual(image.status_code, 201, image.text)
            asset = self.draft('asset', f'assets/{n}-{tag}.json', {'type': 'asset', 'role': role, 'tag': '@' + tag,
                'definition': 'Synthetic ' + tag, 'media_refs': [image.json()['object_ref']]})
            selected[role] = asset['object_ref']
        selected['look'] = self.draft('asset', f'assets/{n}-look.json', {'type': 'asset', 'role': 'look', 'tag': '@look',
            'definition': {'visual_treatment': 'project-animation', 'description': 'Clear colored shapes.'}})['object_ref']
        selection = self.draft('asset', f'assets/{n}-selection.json', {'type': 'asset-selection',
            'target': scene['object_ref'], 'selected': selected})
        method = self.post('/method-selections', {'idempotency_key': f'method{n}', 'expected_revision': 1,
            'target': shot['object_ref'], 'method_id': 'mvgp-video-v1', 'rationale': 'Synthetic prop-relative-motion stress test.'})
        # The agent takes the manuals before it prepares.
        taken = self.client.get(f'/v1/projects/{self.pid}/playbook', headers=self.headers)
        self.assertEqual(taken.status_code, 200, taken.text)
        candidate = self.post('/candidates', {'idempotency_key': f'prepare{n}', 'expected_revision': 1,
            'target': shot['object_ref'], 'task': 'stress', 'method_selection': method['object_ref'], 'inputs': [selection['object_ref']]})
        return candidate, shot

    def test_project_draft_patch_feedback_lesson_and_safe_replay(self):
        body = {'idempotency_key': 'create', 'title': 'New film', 'branch': 'original'}
        created = self.client.post('/v1/projects', headers=self.headers, json=body)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()['object_ref']['revision'], 1)
        self.assertEqual(self.client.post('/v1/projects', headers=self.headers, json=body).json(), created.json())
        self.assertEqual(self.client.get('/v1/projects/' + created.json()['project_id'], headers=self.headers).status_code, 200)
        scene = self.draft()
        patch_body = {'idempotency_key': 'patch', 'expected_revision': 1, 'target': scene['object_ref'],
                      'creative_path': ['content'], 'value': 'The cart passes, then stays ahead.', 'reason': 'Clarify the ending.'}
        changed = self.post('/patches', patch_body)
        self.assertEqual(changed, self.post('/patches', patch_body))
        target = changed['artifact']['object_ref']
        self.post('/feedback', {'idempotency_key': 'note', 'expected_revision': 2, 'target': target, 'text': 'The ending is now clear.'})
        self.post('/reports', {'idempotency_key': 'report', 'expected_revision': 2, 'target': target,
                              'observation': 'This is my report, not independent approval.', 'evidence': [target]})
        self.assertEqual(self.store.list_objects(self.pid, kind='review-receipt'), [])
        stale = {**patch_body, 'idempotency_key': 'stale'}
        self.post('/patches', stale, expected=409)
        with self.subTest('CAS revision update'):
            response = self.client.put(f'/v1/projects/{self.pid}/artifacts/{target["object_id"]}', headers=self.headers,
                json={'idempotency_key': 'revision', 'expected_revision': 2, 'kind': 'scene',
                      'logical_path': 'scene.md', 'content': 'A third draft.', 'dependencies': []})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['object_ref']['revision'], 3)

    def test_mutation_and_multi_object_response_use_distinct_bounded_snapshots(self):
        scene = self.draft()
        body = {'idempotency_key': 'bounded-patch', 'expected_revision': 1, 'target': scene['object_ref'],
                'creative_path': ['content'], 'value': 'The cart is ahead.', 'reason': 'Clear ending.'}
        with patch.object(self.store, 'transaction', wraps=self.store.transaction) as transactions:
            changed = self.post('/patches', body)
        self.assertEqual(changed['artifact']['object_ref']['revision'], 2)
        # Admission, protected mutation, then one response snapshot for both
        # the changed artifact and its repair record. Never share across writes.
        self.assertEqual([c.kwargs.get('write', True) for c in transactions.call_args_list],
                         [False, True, False])

    def test_revocation_after_admission_still_prevents_mutation(self):
        from production.api import parse_body

        credential = self.auth.authenticate(self.token).credential_id

        async def revoke_after_parsing(*args, **kwargs):
            body = await parse_body(*args, **kwargs)
            self.auth.revoke(credential)
            return body

        before = len(self.store.list_objects(self.pid, kind='scene'))
        with patch('production.api.parse_body', side_effect=revoke_after_parsing):
            response = self.client.post(f'/v1/projects/{self.pid}/artifacts', headers=self.headers,
                json={'idempotency_key': 'revoked-draft', 'expected_revision': 0,
                      'kind': 'scene', 'logical_path': 'revoked.md', 'content': 'Must not be saved.'})
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='scene')), before)

    def test_closed_discovery_and_role_scope_schema_boundaries(self):
        discovery = self.client.get('/v1/discovery', headers=self.headers).json()
        self.assertTrue(discovery['production_mutations_available'])
        routes = discovery['mutation_routes']
        self.assertTrue(all(route['schema']['additionalProperties'] is False for route in routes))
        self.assertNotIn('/v1/projects/{pid}/assets/qualifications', [r['path'] for r in routes])
        self.assertNotIn('worker', repr(self.services))
        for route in routes:
            if route['operation'] in ('create-project', 'human-decision', 'upload'):
                continue
            path = route['path'].replace('{pid}', self.pid).replace('{oid}', 'missing').replace('{bid}', 'missing')
            response = self.client.request(route['method'], path, headers=self.viewer, json={})
            self.assertEqual(response.status_code, 403, (path, response.text))
        for path in ('/invoke', '/approve', '/record-review', '/release', '/force', '/cancel'):
            self.assertEqual(self.client.post('/v1/projects/' + self.pid + path, headers=self.headers, json={}).status_code, 404)
        for body in ({}, {'idempotency_key': 'draft', 'expected_revision': 0, 'kind': 'scene', 'logical_path': 's.md',
                         'content': 'text', 'actor': 'operator'}, {'idempotency_key': 'x', 'candidate_id': 'x'}):
            self.post('/artifacts', body, expected=422)
        foreign = self.client.post('/v1/projects/foreign/artifacts', headers=self.headers, json={})
        self.assertEqual(foreign.status_code, 403)
        duplicate = self.client.post(f'/v1/projects/{self.pid}/artifacts', headers={**self.headers, 'Content-Type': 'application/json'},
                                     content=b'{"idempotency_key":"x","idempotency_key":"y"}')
        self.assertEqual(duplicate.status_code, 422)
        self.assertNotIn('idempotency_key', duplicate.text)

    def test_binary_upload_stream_integrity_bounds_and_no_trusted_origin(self):
        response, meta, raw = self.upload()
        self.assertEqual(response.status_code, 201, response.text)
        self.assertFalse(response.json()['confirmed_final'])
        self.assertFalse(response.json()['imported_unverified'])  # Ordinary upload is not a trusted legacy import.
        replay = self.client.post(f'/v1/projects/{self.pid}/uploads', headers={**self.headers,
            'Content-Type': 'application/octet-stream', 'X-MVGP-Upload': json.dumps(meta)}, content=iter([raw[:10], raw[10:]]))
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.json()['object_ref'], response.json()['object_ref'])
        before = len(self.store.list_objects(self.pid, kind='media'))
        for updates in ({'sha256': '0'*64}, {'byte_length': len(raw)-1}, {'byte_length': len(raw)+1},
                        {'logical_path': '../escape'}, {'source_cut': response.json()['object_ref']}, {'trusted': True}):
            bad, _, _ = self.upload(key='bad', **updates)
            self.assertEqual(bad.status_code, 422, bad.text)
        with patch.object(self.c.media, 'max_bytes', len(raw)-1):
            bad, _, _ = self.upload(key='limit')
            self.assertEqual(bad.status_code, 422)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='media')), before)
        self.assertEqual(self.c.provider_calls, [])
        self.assertEqual(self.c.review_calls, [])

    def test_preflight_review_is_advice_and_submission_only_queues(self):
        candidate, _shot = self.candidate()
        ref = candidate['object_ref']
        prepared = self.post('/candidates/inspect', {'candidate': ref})
        self.assertTrue(prepared['mechanical_pass'], prepared)
        self.assertFalse(prepared['generation_authorized'])
        submit = {'idempotency_key': 'submit', 'expected_revision': ref['revision'], 'candidate_id': ref['object_id']}
        # Owner decision 2026-09-23: the review is advice; submission does not wait for it.
        first = self.post('/submissions', submit, expected=202)
        # there is no AI review route any more.
        self.post('/reviews', {'idempotency_key': 'review', 'expected_revision': ref['revision'],
            'target': ref, 'purpose': 'preflight'}, expected=404)
        before = len(self.c.review_calls)
        submitted = self.post('/submissions', submit, expected=202)
        self.assertEqual(submitted, first)
        self.assertEqual(submitted['status'], 'queued')
        self.assertEqual(self.post('/submissions', submit, expected=202), submitted)
        self.assertEqual(len(self.c.review_calls), before)
        self.assertEqual(self.c.provider_calls, [])
        self.assertEqual(len(self.store.list_objects(self.pid, kind='job')), 1)
        self.post('/review-appeals', {'idempotency_key': 'appeal', 'expected_revision': 1,
                  'task': ref, 'reason': 'There is no review to appeal.'}, expected=404)
        batch_body = {'idempotency_key': 'batch', 'expected_revision': 1, 'candidate_ids': [ref['object_id']]}
        batch = self.post('/batches', batch_body, expected=202)
        self.assertEqual(batch, self.post('/batches', batch_body, expected=202))
        read = self.client.get(f'/v1/projects/{self.pid}/batches/' + batch['object_ref']['object_id'], headers=self.headers)
        self.assertEqual(read.status_code, 200, read.text)
        self.assertEqual(read.json()['counts'], {'queued': 1})
        self.assertEqual(self.c.provider_calls, [])
        self.assertEqual(len(self.store.list_objects(self.pid, kind='job')), 2)

    def test_cut_and_observation_queue_without_worker_authority(self):
        video = self.c.synthetic_movie('source', 'pass')
        uploaded, _, _ = self.upload(raw=video, key='video', media_type='video/mp4', logical_path='imports/source.mp4')
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        upload_ref = uploaded.json()['object_ref']
        # The separately privileged importer owns this provenance; HTTP cannot forge it.
        upload_obj = self.store.get_object(self.pid, upload_ref['object_id'])
        imported = self.store.create_object(self.pid, 'media', {**upload_obj['body'],
            'import_status': 'imported-unverified'}, 'importer_service')
        media_ref = {k: imported[k] for k in ('object_id', 'revision', 'digest')}
        cut = self.post('/cuts', {'idempotency_key': 'cut', 'expected_revision': 1,
            'segments': [{'take': media_ref, 'start_seconds': 0.0, 'end_seconds': 1.0}], 'intent': 'Rough review only.'})
        self.assertTrue(cut['imported_unverified'])
        render = self.post('/cuts/render', {'idempotency_key': 'render', 'expected_revision': 1, 'cut': cut['object_ref']}, expected=202)
        self.assertEqual(render['status'], 'queued')
        observe = self.post('/observations', {'idempotency_key': 'observe', 'expected_revision': 1,
            'media_id': media_ref['object_id'], 'reader': 'video', 'questions': ['Does the cart pass the marker?']}, expected=202)
        self.assertEqual(observe['status'], 'queued')
        self.assertEqual(self.c.provider_calls, [])
        self.assertEqual(self.c.review_calls, [])

    def test_human_cookie_origin_csrf_and_exact_one_use_decision(self):
        project = self.store.get_object(self.pid, self.pid)
        target = {key: project[key] for key in ('object_id', 'revision', 'digest')}
        decision = self.post('/decision-requests', {'idempotency_key': 'decision', 'expected_revision': 1,
            'target': target, 'purpose': 'envelope', 'rationale': 'Increase test envelope.', 'proposed_limit': 50, 'budget_unit': 'synthetic_unit'})
        request_id = decision['object_ref']['object_id']
        body = {'idempotency_key': 'confirm', 'request_id': request_id, 'target_hash': target['digest'],
                'choice': 'confirm', 'csrf_token': 'x'*32}
        self.post('/human-decisions', body, expected=403)
        response = self.client.post('/v1/session/access', json={},
                                    headers={'Origin': 'https://craft.example', 'cf-access-jwt-assertion': owner_jwt()})
        self.assertEqual(response.status_code, 200, response.text)
        body['csrf_token'] = response.json()['csrf_token']
        path = f'/v1/projects/{self.pid}/human-decisions'
        self.assertEqual(self.client.post(path, json=body, headers={'Origin': 'https://wrong.example'}).status_code, 403)
        self.assertEqual(self.client.post(path, json={**body, 'csrf_token': 'bad'*12}, headers={'Origin': 'https://craft.example'}).status_code, 403)
        confirmed = self.client.post(path, json=body, headers={'Origin': 'https://craft.example'})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertNotIn('human_credential', confirmed.text)
        self.assertEqual(self.store.budget(self.pid)['ceiling'], 50)
        self.assertEqual(self.client.post(path, json=body, headers={'Origin': 'https://craft.example'}).json(), confirmed.json())
        self.assertEqual(self.client.post(path, json={**body, 'idempotency_key': 'second'}, headers={'Origin': 'https://craft.example'}).status_code, 409)
        self.assertEqual(self.client.post(f'/v1/projects/{self.pid}/feedback', json={}, headers={'Origin': 'https://craft.example'}).status_code, 403)

    def test_owner_switches_read_by_anyone_changed_only_by_the_owners_session(self):
        # the stress-test switch lives here.
        path = f'/v1/projects/{self.pid}/switches'
        seen = self.client.get(path, headers=self.headers)
        self.assertEqual(seen.status_code, 200, seen.text)
        self.assertFalse(seen.json()['switches']['asset_stress_test'])
        body = {'idempotency_key': 'switch', 'switches': {'asset_stress_test': True}, 'csrf_token': 'x'*32}
        self.post('/switches', body, expected=403)  # an agent is not the owner
        response = self.client.post('/v1/session/access', json={},
                                    headers={'Origin': 'https://craft.example', 'cf-access-jwt-assertion': owner_jwt()})
        body['csrf_token'] = response.json()['csrf_token']
        self.assertEqual(self.client.post(path, json={**body, 'csrf_token': 'bad'*12}, headers={'Origin': 'https://craft.example'}).status_code, 403)
        changed = self.client.post(path, json=body, headers={'Origin': 'https://craft.example'})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertTrue(changed.json()['switches']['asset_stress_test'])
        self.assertEqual(self.client.post(path, json={**body, 'switches': {'nonsense': True}},
                                          headers={'Origin': 'https://craft.example'}).status_code, 422)

    def test_owner_notes_are_written_only_by_the_owners_session(self):

        shot = self.store.create_object(self.pid, 'shot', {'content': {'shot': 'S01-010A'}}, 'author')
        shot_ref = {k: shot[k] for k in ('object_id', 'revision', 'digest')}
        body = {'idempotency_key': 'note', 'target': shot_ref, 'text': '这一段节奏慢了', 'csrf_token': 'x'*32}
        self.post('/owner-notes', body, expected=403)  # an agent is not the owner
        self.post('/owner-notes', body, expected=403, headers=self.viewer)
        response = self.client.post('/v1/session/access', json={},
                                    headers={'Origin': 'https://craft.example', 'cf-access-jwt-assertion': owner_jwt()})
        body['csrf_token'] = response.json()['csrf_token']
        path = f'/v1/projects/{self.pid}/owner-notes'
        written = self.client.post(path, json=body, headers={'Origin': 'https://craft.example'})
        self.assertEqual(written.status_code, 200, written.text)
        note = written.json()['object_ref']
        withdrawn = self.client.post(path + '/withdraw', json={'idempotency_key': 'w', 'note_id': note['object_id'],
            'expected_revision': 1, 'csrf_token': body['csrf_token']}, headers={'Origin': 'https://craft.example'})
        self.assertEqual(withdrawn.status_code, 200, withdrawn.text)

    def test_shoot_order_is_an_agent_call_that_reports_each_card(self):
        # the agent's one call; a card that is not a shot card is reported, not fired.
        scene = self.draft()
        body = {'idempotency_key': 'shoot', 'cards': [scene['object_ref']]}
        self.post('/shoot-orders', body, expected=403, headers=self.viewer)
        placed = self.post('/shoot-orders', body)
        self.assertEqual([c['stage'] for c in placed['cards']], ['stopped'])
        self.assertIn('shot card', placed['cards'][0]['reason'])
        self.assertEqual(self.post('/shoot-orders', body), placed)
        self.assertEqual(self.store.list_objects(self.pid, kind='job'), [])

    def test_the_quote_is_a_read_for_the_agent_and_the_owner(self):
        # GET /v1/projects/{pid}/quote, read-only; default every shot card.
        path = f'/v1/projects/{self.pid}/quote'
        shots = self.store.list_objects(self.pid, kind='shot')
        for headers in (self.headers, self.viewer):
            response = self.client.get(path, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(response.json()['cards']), len(shots))
        scene = self.draft()
        one = self.client.get(path, params={'card': scene['object_ref']['object_id'], 'takes': 2}, headers=self.headers).json()
        self.assertEqual([c.get('advice') for c in one['cards']], ['这张卡的路线或秒数平台算不出价'])
        self.assertEqual(self.client.get(path, params={'takes': 5}, headers=self.headers).status_code, 422)
        self.assertEqual(self.store.list_objects(self.pid, kind='job'), [])

    def test_the_ledger_is_read_in_the_owners_session_only(self):
        # GET /v1/ledger and /v1/projects/{pid}/ledger, read-only, the owner's desk session.
        for path in ('/v1/ledger', f'/v1/projects/{self.pid}/ledger'):
            for headers in (self.headers, self.viewer):
                self.assertEqual(self.client.get(path, headers=headers).status_code, 403, path)
        self.client.post('/v1/session/access', json={}, headers={'Origin': 'https://craft.example', 'cf-access-jwt-assertion': owner_jwt()})
        for path in ('/v1/ledger', f'/v1/projects/{self.pid}/ledger'):
            response = self.client.get(path, headers={'Origin': 'https://craft.example'})
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertIn('rows', body)
            self.assertEqual(len(body['days']), 7)
            self.assertEqual(set(body['days'][0]['providers']), {'fal', 'apilio', 'higgsfield'})

    def test_scoped_reopen_is_exact_idempotent_and_does_not_approve(self):
        scene = self.draft()
        # A trusted fixture lock exercises API semantics; it is not human acceptance.
        cut = self.store.create_object(self.pid, 'cut', {'dependencies': [scene['object_ref']]}, 'cut_service')
        lock = self.c.flow.record_picture_lock(self.pid, ObjectRef(**{k: cut[k] for k in ('object_id', 'revision', 'digest')}))
        ref = {k: lock[k] for k in ('object_id', 'revision', 'digest')}
        body = {'idempotency_key': 'reopen', 'expected_revision': ref['revision'], 'lock': ref,
                'targets': [scene['object_ref']], 'reason': 'Repair this specific scene.'}
        self.post('/patches', {'idempotency_key': 'locked', 'expected_revision': 1, 'target': scene['object_ref'],
                              'creative_path': ['content'], 'value': 'New ending.', 'reason': 'Revise.'}, expected=422)
        result = self.post('/reopens', body)
        self.assertEqual(result, self.post('/reopens', body))
        self.assertFalse(result['confirmed_final'])
        self.post('/reopens', {**body, 'idempotency_key': 'wrong', 'expected_revision': 99}, expected=422)
        self.post('/patches', {'idempotency_key': 'unlocked', 'expected_revision': 1, 'target': scene['object_ref'],
                              'creative_path': ['content'], 'value': 'New ending.', 'reason': 'Revise.'})

    def test_mismatched_or_fake_authority_assembly_is_rejected(self):
        other_cuts = copy.copy(self.c.cuts)
        with self.assertRaises(ValueError):
            replace(self.services, mutations=replace(self.m, decisions=Decisions(self.store, self.auth, self.c.flow, other_cuts)))
