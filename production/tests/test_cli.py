"""CLI talks only to scoped HTTP projections, including exact historical references."""
import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import httpx

from production import cli, folder
from production.tests import test_api


class CLIBase(unittest.TestCase):
    def setUp(self):
        self.fixture = test_api.MutationAPITests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.pid = self.fixture.pid
        self.env = {'MVGP_URL': 'https://craft.example', 'MVGP_TOKEN': self.fixture.token,
                    'OPENAI_API_KEY': 'NEVER_USE_PROVIDER', 'HF_API_KEY': 'NEVER_USE_PROVIDER'}
        self.calls = []
        self.transport = httpx.MockTransport(self.service)

    def service(self, request):
        self.calls.append(request)
        response = self.fixture.client.request(request.method, str(request.url), content=request.read(),
            headers=dict(request.headers))
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    def run_cli(self, args, body=None, *, env=None, transport=None):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(args, environ=self.env if env is None else env, stdin=io.StringIO(json.dumps(body) if body is not None else ''),
                        stdout=out, stderr=err, transport=transport or self.transport)
        self.assertNotIn(self.fixture.token, out.getvalue() + err.getvalue())
        return code, json.loads(out.getvalue() or err.getvalue()), out.getvalue(), err.getvalue()


class CLITests(CLIBase):
    def test_a_persons_access_login_is_refused_before_any_request(self):
        # the owner's Access login opens his desk; an agent never carries one.
        access = {'MVGP_ACCESS_ORIGIN': 'https://craft.example', 'MVGP_ACCESS_TOKEN': 'employee.jwt.signature'}
        for extra in ({}, {'MVGP_ACCESS_CLIENT_ID': 'machine', 'MVGP_ACCESS_CLIENT_SECRET': 'secret'}):
            code, value, out, err = self.run_cli(['projects'], env={**self.env, **access, **extra})
            self.assertEqual(code, 2, value)
            self.assertNotIn(access['MVGP_ACCESS_TOKEN'], out + err)
        self.assertFalse(self.calls)

    def test_playbook_writes_the_manuals_and_the_platform_records_the_fetch(self):

        from production import playbook
        with tempfile.TemporaryDirectory() as tmp:
            code, value, _, _ = self.run_cli(['playbook', self.pid, '--dir', tmp + '/manuals'])
            self.assertEqual(code, 0, value)
            self.assertEqual(value['playbook_version'], playbook.version())
            for item in value['files']:
                self.assertEqual(hashlib.sha256(Path(item['path']).read_bytes()).hexdigest(), item['sha256'])
            self.assertEqual(sorted(p.name for p in Path(tmp, 'manuals').iterdir()),
                             sorted(r['name'] for r in playbook.manifest()['files']))
        credential = self.fixture.auth.authenticate(self.fixture.token).credential_id
        with self.fixture.store.transaction(write=False) as db:
            self.assertTrue(playbook.fetched(self.fixture.store, self.pid, credential, playbook.version(), conn=db))

    def test_playbook_refuses_manuals_that_do_not_match_their_hashes(self):
        forged = {'version': 'pb-000000000000', 'files': [{'name': 'writer.md', 'text': 'x', 'sha256': '0' * 64}]}
        with tempfile.TemporaryDirectory() as tmp:
            code, value, _, _ = self.run_cli(['playbook', self.pid, '--dir', tmp],
                                             transport=httpx.MockTransport(lambda req: httpx.Response(200, json=forged)))
            self.assertNotEqual(code, 0)
            self.assertEqual(value['error']['code'], 'invalid_response')
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_client_refuses_external_paths_before_sending_any_credentials(self):
        with cli.Client('https://craft.example', self.fixture.token, transport=self.transport,
                        access=('test-client.access', 'test-secret')) as client:
            for path in ('https://other.example/x', '//other.example/x', '/\\other.example/x', '/x\nheader'):
                with self.assertRaises(cli.CLIError):
                    client.request('GET', path)
        self.assertFalse(self.calls)

    def test_access_ingress_does_not_follow_redirects_or_echo_errors(self):
        access = {'MVGP_ACCESS_ORIGIN': 'https://craft.example', 'MVGP_ACCESS_CLIENT_ID': 'test-client.access',
                  'MVGP_ACCESS_CLIENT_SECRET': 'test-secret-value'}
        for response in (httpx.Response(302, headers={'Location': 'https://other.example'}),
                         httpx.Response(403, json={'error': {'code': 'forbidden', 'message': access['MVGP_ACCESS_CLIENT_SECRET']}}),
                         httpx.Response(200, text=access['MVGP_ACCESS_CLIENT_SECRET'])):
            seen = []
            def service(request, seen=seen, response=response):
                seen.append(request)
                return response
            code, _, out, err = self.run_cli(['projects'], env={**self.env, **access},
                                           transport=httpx.MockTransport(service))
            self.assertNotEqual(code, 0)
            self.assertEqual(len(seen), 1)
            self.assertNotIn(access['MVGP_ACCESS_CLIENT_SECRET'], out + err)

    def test_access_ingress_keeps_platform_auth_and_redacts_both_credentials(self):
        access = {'MVGP_ACCESS_ORIGIN': 'https://craft.example',
                  'MVGP_ACCESS_CLIENT_ID': 'test-client.access',
                  'MVGP_ACCESS_CLIENT_SECRET': 'cfast_test-private-secret'}
        def gate(request):
            self.assertEqual(request.headers.get('CF-Access-Client-Id'), access['MVGP_ACCESS_CLIENT_ID'])
            self.assertEqual(request.headers.get('CF-Access-Client-Secret'), access['MVGP_ACCESS_CLIENT_SECRET'])
            self.assertEqual(request.headers['Authorization'], 'Bearer ' + self.fixture.token)
            return httpx.Response(200, json={'id': access['MVGP_ACCESS_CLIENT_ID'],
                                           'secret': access['MVGP_ACCESS_CLIENT_SECRET']})
        code, value, out, err = self.run_cli(['projects'], env={**self.env, **access},
                                            transport=httpx.MockTransport(gate))
        self.assertEqual(code, 0, value)
        for key in ('MVGP_ACCESS_CLIENT_ID', 'MVGP_ACCESS_CLIENT_SECRET'):
            self.assertNotIn(access[key], out + err)
        self.assertEqual(value, {'id': '[credential omitted]', 'secret': '[credential omitted]'})

    def test_access_credentials_require_complete_exact_https_origin(self):
        access = {'MVGP_ACCESS_ORIGIN': 'https://craft.example',
                  'MVGP_ACCESS_CLIENT_ID': 'test-client.access',
                  'MVGP_ACCESS_CLIENT_SECRET': 'cfast_private'}
        invalid = [{k: v for k, v in access.items() if k != omitted} for omitted in access]
        invalid.extend([{**access, 'MVGP_ACCESS_ORIGIN': 'https://other.example'},
                        {**access, 'MVGP_ACCESS_ORIGIN': 'https://craft.example/path'},
                        {**access, 'MVGP_ACCESS_CLIENT_SECRET': 'bad\r\nheader'},
                        {**access, 'MVGP_ACCESS_CLIENT_ID': 'x' * 8193},
                        {**access, 'MVGP_URL': 'http://127.0.0.1:8892',
                         'MVGP_ACCESS_ORIGIN': 'http://127.0.0.1:8892'}])
        for values in invalid:
            code, result, _, _ = self.run_cli(['projects'], env={**self.env, **values})
            self.assertEqual(code, 2, result)
        self.assertFalse(self.calls)

    def test_access_is_optional_and_never_follows_redirects(self):
        self.assertEqual(self.run_cli(['projects'])[0], 0)
        self.assertNotIn('CF-Access-Client-Id', self.calls[-1].headers)
        seen = []
        def redirect(request):
            seen.append(request)
            return httpx.Response(302, headers={'Location': 'https://other.example/login'})
        env = {**self.env, 'MVGP_ACCESS_ORIGIN': 'https://craft.example',
               'MVGP_ACCESS_CLIENT_ID': 'test-client.access', 'MVGP_ACCESS_CLIENT_SECRET': 'cfast_private'}
        code, result, _, _ = self.run_cli(['projects'], env=env, transport=httpx.MockTransport(redirect))
        self.assertEqual(code, 7, result)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].url.host, 'craft.example')

    def test_discovery_and_actual_read_routes_do_not_mutate_or_call_providers(self):
        scene = self.fixture.draft()
        before = self.fixture.store.list_objects(self.pid)
        commands = [('discovery',), ('projects',), ('project', self.pid), ('methods', self.pid),
                    ('tree', self.pid), ('document', self.pid, 'execution_policy'), ('history', self.pid, scene['object_ref']['object_id']),
                    ('read', self.pid, scene['object_ref']['object_id'], '--revision', '1'), ('jobs', self.pid)]
        for args in commands:
            code, result, _out, err = self.run_cli(args)
            self.assertEqual(code, 0, result)
            self.assertEqual(err, '')
        code, discovery, _, _ = self.run_cli(['discovery'])
        self.assertTrue(discovery['production_mutations_available'])
        self.assertTrue(discovery['mutation_routes'])
        code, context, _, _ = self.run_cli(['context', self.pid, '--input', '-'], {'target': scene['object_ref']})
        self.assertEqual(code, 0, context)
        code, inspection, _, _ = self.run_cli(['inspect', self.pid, '--input', '-'], {'target': scene['object_ref']})
        self.assertEqual(code, 0, inspection)
        self.assertEqual(before, self.fixture.store.list_objects(self.pid))
        self.assertEqual(self.fixture.c.provider_calls, [])
        self.assertEqual(self.fixture.c.review_calls, [])
        for request in self.calls:
            self.assertEqual(request.url.host, 'craft.example')
            self.assertEqual(request.headers['authorization'], 'Bearer ' + self.fixture.token)
            self.assertNotIn('NEVER_USE_PROVIDER', str(request.headers))

    def test_project_page_read_route(self):
        # the agent reads the same project page the owner sees.
        code, page, _, err = self.run_cli(['project-page', self.pid])
        self.assertEqual((code, err), (0, ''))
        self.assertIn(page['stage'], ('开发', '前期', '拍摄', '后期'))
        self.assertIn('brief', page)
        self.assertTrue(any(r.url.path.endswith('/project-tree') for r in self.calls))

    def test_candidate_inspection_stays_advisory_and_batch_reads_use_exact_route(self):
        candidate, _shot = self.fixture.candidate()
        code, result, _, _ = self.run_cli(['candidate-inspect', self.pid, '--input', '-'],
                                        {'candidate': candidate['object_ref']})
        self.assertEqual(code, 0, result)
        self.assertTrue(result['mechanical_pass'])
        self.assertFalse(result['generation_authorized'])
        code, result, _, _ = self.run_cli(['batch', self.pid, 'missing_batch'])
        self.assertEqual(code, 4, result)
        self.assertTrue(str(self.calls[-1].url).endswith('/batches/missing_batch'))
        self.assertEqual(self.fixture.c.provider_calls, [])
        self.assertEqual(self.fixture.c.review_calls, [])

    def test_copy_reference_resolves_exact_old_version_and_ignores_external_link(self):
        scene = self.fixture.draft()
        ref = scene['object_ref']
        self.fixture.store.append_revision(self.pid, ref['object_id'], 1, {'content': 'NEW VERSION'}, 'api_author')
        copied = {'project_id': self.pid, 'object_ref': ref, 'playback_seconds': None,
                  'link': 'https://attacker.example/steal', 'source_mapping': None}
        code, result, _, _ = self.run_cli(['resolve', '--input', '-'], copied)
        self.assertEqual(code, 0, result)
        self.assertEqual(result['artifact']['object_ref'], ref)
        self.assertFalse(result['artifact']['current'])
        self.assertNotIn('NEW VERSION', json.dumps(result))
        self.assertEqual(self.calls[-1].url.params['revision'], '1')
        code, result, _, _ = self.run_cli(['resolve', '--input', '-'], {**copied, 'object_ref': {**ref, 'digest': '0'*64}})
        self.assertEqual(code, 5, result)
        self.assertEqual(result['error']['code'], 'stale_input')
        code, result, _, _ = self.run_cli(['reference', self.pid, '--input', '-'], {'target': ref})
        self.assertEqual(code, 0, result)
        self.assertEqual(result['object_ref'], ref)
        self.assertTrue(all(req.url.host == 'craft.example' for req in self.calls))

    def test_polling_is_read_only_terminal_or_bounded_and_resumable(self):
        job = self.fixture.store.create_object(self.pid, 'job', {'state': 'queued'}, 'submission_service')
        oid = job['object_id']
        states = iter(['queued', 'running', 'succeeded'])
        def advance(request):
            current = self.fixture.store.get_object(self.pid, oid)
            self.fixture.store.append_revision(self.pid, oid, current['revision'], {'state': next(states)}, 'worker_service')
            return self.service(request)
        code, result, _, _ = self.run_cli(['status', self.pid, oid, '--wait-seconds', '1', '--interval', '0.05'],
                                        transport=httpx.MockTransport(advance))
        self.assertEqual(code, 0, result)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(all(req.method == 'GET' for req in self.calls))
        current = self.fixture.store.get_object(self.pid, oid)
        self.fixture.store.append_revision(self.pid, oid, current['revision'], {'state': 'unknown'}, 'worker_service')
        self.assertEqual(self.run_cli(['status', self.pid, oid, '--wait-seconds', '1'])[1]['status'], 'unknown')
        current = self.fixture.store.get_object(self.pid, oid)
        self.fixture.store.append_revision(self.pid, oid, current['revision'], {'state': 'queued'}, 'worker_service')
        code, result, out, _ = self.run_cli(['status', self.pid, oid, '--wait-seconds', '0.05', '--interval', '0.05'])
        self.assertEqual(code, 8)
        self.assertEqual(out, '')
        self.assertEqual(result['error']['object_ref']['object_id'], oid)
        self.assertEqual(self.run_cli(['status', self.pid, oid])[0], 0)
        jobs = self.run_cli(['jobs', self.pid])[1]['jobs']
        self.assertEqual([row['object_ref']['object_id'] for row in jobs], [oid])
        self.assertEqual(self.fixture.c.provider_calls, [])

    def test_schema_scope_revocation_and_exit_codes_are_platform_decisions(self):
        code, result, _, _ = self.run_cli(['context', self.pid, '--input', '-'], {'force': True})
        self.assertEqual(code, 2)
        self.assertEqual(result['error']['code'], 'invalid_input')  # server schema rejects unknown fields
        self.assertEqual(self.run_cli(['project', 'foreign'])[0], 3)
        self.assertEqual(self.run_cli(['read', self.pid, 'missing'])[0], 4)
        self.fixture.auth.revoke(self.fixture.auth.authenticate(self.fixture.token).credential_id)
        self.assertEqual(self.run_cli(['projects'])[0], 3)

    def test_input_file_stdin_duplicates_limits_and_path_arguments(self):
        scene = self.fixture.draft()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'request.json'
            path.write_text(json.dumps({'target': scene['object_ref']}))
            self.assertEqual(self.run_cli(['context', self.pid, '--input', str(path)])[0], 0)
            path.write_text('{"target":null,"target":null}')
            before = len(self.calls)
            self.assertEqual(self.run_cli(['context', self.pid, '--input', str(path)])[0], 2)
            path.write_bytes(b'x' * (cli.INPUT_LIMIT + 1))
            self.assertEqual(self.run_cli(['context', self.pid, '--input', str(path)])[0], 2)
            for content in ('{"seconds":NaN}', '{"seconds":1e9999}', '[]'):
                path.write_text(content)
                self.assertEqual(self.run_cli(['context', self.pid, '--input', str(path)])[0], 2)
            self.assertEqual(len(self.calls), before)
        for args in (['read', self.pid, '../secret'], ['read', self.pid, '%2fprivate'],
                     ['read', self.pid, 'obj', '--revision', '0'], ['force'], ['projects', '--token', 'DO_NOT_ECHO'],
                     ['status', self.pid, 'obj', '--wait-seconds', 'nan'],
                     ['status', self.pid, 'obj', '--wait-seconds', '1', '--interval', '0']):
            code, result, _, err = self.run_cli(args)
            self.assertEqual(code, 2, result)
            self.assertNotIn('DO_NOT_ECHO', err)

    def test_url_and_redirect_boundaries_never_forward_credentials(self):
        for url in ('http://remote.example', 'https://user:password@studio.example', 'https://studio.example?x=secret',
                    'https://studio.example/#bad', 'file:///tmp/mvgp', 'https://studio.example/base', 'https://studio.example:bad'):
            code, result, _, err = self.run_cli(['projects'], env={**self.env, 'MVGP_URL': url})
            self.assertEqual(code, 2, result)
            self.assertNotIn('password', err)
        self.assertEqual(self.calls, [])
        for url in ('http://127.0.0.1:8810', 'http://[::1]:8810', 'http://localhost:8810', 'https://studio.example'):
            self.assertEqual(cli.configuration({**self.env, 'MVGP_URL': url})[0], url)
        seen = []
        def redirect(request):
            seen.append(request)
            return httpx.Response(307, headers={'Location': 'https://attacker.example'})
        code, result, _, _ = self.run_cli(['projects'], transport=httpx.MockTransport(redirect))
        self.assertEqual(code, 7, result)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].url.host, 'craft.example')
        self.assertEqual(self.calls, [])

    def test_cloud_request_waits_for_cold_start_without_retrying_mutations(self):
        seen = []
        def service(request):
            seen.append(request)
            self.assertEqual(request.extensions['timeout']['read'], 180)
            self.assertEqual(request.extensions['timeout']['connect'], 180)
            return httpx.Response(200, json={'recorded': True})
        with cli.Client('https://craft.example', self.fixture.token, transport=httpx.MockTransport(service)) as client:
            self.assertTrue(client.request('POST', '/v1/projects', body={'idempotency_key': 'fixed'})['recorded'])
        self.assertEqual(len(seen), 1)
        # A caller's shorter polling deadline must still override this default.
        with cli.Client('https://craft.example', self.fixture.token,
                        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={'timeout': req.extensions['timeout']['read']}))) as client:
            self.assertEqual(client.request('GET', '/v1/projects', timeout=3)['timeout'], 3)

    def test_transport_response_bounds_and_errors_never_echo_secrets(self):
        seen = []
        def failure(request):
            seen.append(request)
            raise httpx.ConnectError('SECRET_PATH_TOKEN ' + self.fixture.token, request=request)
        code, result, _, err = self.run_cli(['projects'], transport=httpx.MockTransport(failure))
        self.assertEqual(code, 7, result)
        self.assertNotIn('SECRET_PATH_TOKEN', err)
        self.assertEqual(len(seen), 1)
        for response in (httpx.Response(200, text='RAW_SECRET'), httpx.Response(503, json={'wrong': 'RAW_SECRET'}),
                         httpx.Response(200, content=b'{}' * 20), httpx.Response(200, json={'echo': self.fixture.token})):
            with patch.object(cli, 'RESPONSE_LIMIT', 30):
                code, result, _, err = self.run_cli(['projects'], transport=httpx.MockTransport(lambda req, response=response: response))
            self.assertEqual(code, 9, result)
            self.assertNotIn('RAW_SECRET', err)
        code, result, out, _ = self.run_cli(['projects'], transport=httpx.MockTransport(lambda req: httpx.Response(200, json={'echo': self.fixture.token})))
        self.assertEqual(code, 0)
        self.assertIn('[credential omitted]', out)
        self.assertEqual(result['echo'], '[credential omitted]')

    def test_help_and_module_do_not_need_credentials_or_read_local_truth(self):
        with redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as raised:
            cli.main(['--help'], environ={})
        self.assertEqual(raised.exception.code, 0)
        self.assertIn('MVGP_URL', out.getvalue())
        self.assertNotIn('--token', out.getvalue())
        self.assertEqual(self.calls, [])
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'PRIVATE'}, clear=True):
            code, result, _, _ = self.run_cli(['projects'], env={})
        self.assertEqual(code, 2, result)


class MutationCLITests(CLIBase):
    # Exercise the same real API fixture through production mutation commands.
    def test_create_revise_feedback_and_proposal_preserve_request_identity(self):
        create = {'idempotency_key': 'cli-create', 'title': 'CLI Film', 'branch': 'original'}
        code, project, _, _ = self.run_cli(['create-project', '--input', '-'], create)
        self.assertEqual(code, 0, project)
        self.assertEqual(self.run_cli(['create-project', '--input', '-'], create)[1], project)
        draft = {'idempotency_key': 'cli-draft', 'expected_revision': 0, 'kind': 'scene',
                 'logical_path': 'cli/scene.md', 'content': 'The cart passes. 眼神跟随。', 'dependencies': []}
        code, scene, _, _ = self.run_cli(['draft', self.pid, '--input', '-'], draft)
        self.assertEqual(code, 0, scene)
        self.assertEqual(json.loads(self.calls[-1].content), draft)
        oid = scene['object_ref']['object_id']
        revised = {**draft, 'idempotency_key': 'cli-revise', 'expected_revision': 1, 'content': 'The cart stays ahead.'}
        code, second, _, _ = self.run_cli(['revise', self.pid, oid, '--input', '-'], revised)
        self.assertEqual(code, 0, second)
        self.assertEqual(second['object_ref']['revision'], 2)
        self.assertEqual(self.calls[-1].method, 'PUT')
        self.assertEqual(json.loads(self.calls[-1].content), revised)
        self.assertEqual(self.run_cli(['revise', self.pid, oid, '--input', '-'], revised)[1], second)
        code, result, _, _ = self.run_cli(['revise', self.pid, oid, '--input', '-'], {**revised, 'idempotency_key': 'stale'})
        self.assertEqual(code, 5, result)
        ref = second['object_ref']
        feedback = {'idempotency_key': 'cli-feedback', 'expected_revision': 2, 'target': ref, 'text': 'The direction is now clear.'}
        self.assertEqual(self.run_cli(['feedback', self.pid, '--input', '-'], feedback)[0], 0)
        self.assertEqual(self.fixture.c.provider_calls, [])
        self.assertEqual(self.fixture.c.review_calls, [])

    def test_explicit_mutation_routes_match_discovery_and_forbidden_authority_is_absent(self):
        published = self.run_cli(['discovery'])[1]['mutation_routes']
        routes = {(row['method'], row['path'].replace('{pid}', '{project}').replace('{oid}', '{object}').replace('{bid}', '{object}'))
                  for row in published}
        self.assertTrue(set(cli.MUTATIONS.values()) <= routes)
        before = self.fixture.store.list_objects(self.pid)
        for name, (_method, route) in cli.MUTATIONS.items():
            args = [name]
            if '{project}' in route:
                args.append(self.pid)
            if '{object}' in route:
                args.append('missing')
            code, result, _, _ = self.run_cli([*args, '--input', '-'], {'actor': 'operator', 'force': True})
            self.assertEqual(code, 2, (name, result))
        self.assertEqual(before, self.fixture.store.list_objects(self.pid))
        for name in ('human-confirm', 'human-decision', 'approve', 'reviewer', 'raw-submit', 'operator', 'invoke', 'force'):
            count = len(self.calls)
            self.assertEqual(self.run_cli([name, '--input', '-'], {})[0], 2)
            self.assertEqual(count, len(self.calls))

    def test_prepare_and_submit_are_platform_checked_and_queue_only(self):
        original, _shot = self.fixture.candidate()
        source = self.fixture.store.get_object(self.pid, original['object_ref']['object_id'])['body']
        selected = next(obj for obj in self.fixture.store.list_objects(self.pid, kind='asset')
                        if obj['body']['content'].get('type') == 'asset-selection')
        request = {'idempotency_key': 'cli-prepare', 'expected_revision': source['target']['revision'],
                   'target': source['target'], 'task': 'stress', 'method_selection': source['method_selection'],
                   'inputs': [{key: selected[key] for key in ('object_id', 'revision', 'digest')}]}
        code, candidate, _, _ = self.run_cli(['prepare', self.pid, '--input', '-'], request)
        self.assertEqual(code, 0, candidate)
        ref = candidate['object_ref']
        submit = {'idempotency_key': 'cli-submit', 'expected_revision': ref['revision'], 'candidate_id': ref['object_id']}
        # Owner decision 2026-09-23: the review is advice, so submit only queues without waiting for it.
        code, queued, _, _ = self.run_cli(['submit', self.pid, '--input', '-'], submit)
        self.assertEqual(code, 0, queued)
        self.assertEqual(queued['status'], 'queued')
        self.assertEqual(len(self.fixture.store.list_objects(self.pid, kind='job')), 1)
        # there is no review or appeal command any more.
        review = {'idempotency_key': 'cli-review', 'expected_revision': ref['revision'], 'target': ref, 'purpose': 'preflight'}
        for command in ('review', 'appeal'):
            self.assertNotEqual(self.run_cli([command, self.pid, '--input', '-'], review)[0], 0)
        self.assertEqual(self.fixture.c.provider_calls, [])
        self.assertEqual(self.fixture.c.review_calls, [])

    def upload_fixture(self, root):
        data = io.BytesIO()
        test_api.Image.new('RGB', (80, 60), 'orange').save(data, format='PNG')
        raw = data.getvalue()
        path = root / 'image.png'
        path.write_bytes(raw)
        metadata = {'idempotency_key': 'cli-upload', 'logical_path': 'cli/image.png', 'media_type': 'image/png',
                    'byte_length': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        return path, metadata, raw

    def test_streamed_upload_exact_hash_replay_and_server_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, metadata, raw = self.upload_fixture(Path(tmp).resolve())
            args = ['upload', self.pid, str(path), '--input', '-']
            code, uploaded, _, _ = self.run_cli(args, metadata)
            self.assertEqual(code, 0, uploaded)
            self.assertEqual(self.calls[-1].content, raw)
            self.assertEqual(json.loads(self.calls[-1].headers['x-mvgp-upload']), metadata)
            self.assertEqual(self.calls[-1].headers['content-type'], 'application/octet-stream')
            self.assertEqual(self.run_cli(args, metadata)[1], uploaded)
            before = len(self.fixture.store.list_objects(self.pid, kind='media'))
            for updates in ({'sha256': '0'*64}, {'byte_length': len(raw)-1}, {'byte_length': len(raw)+1}):
                start = len(self.calls)
                code, result, _, _ = self.run_cli(args, {**metadata, **updates})
                self.assertEqual(code, 2, result)
                self.assertTrue(all(call.method == 'GET' for call in self.calls[start:]))
            code, result, _, _ = self.run_cli(args, {**metadata, 'trusted': True})
            self.assertEqual(code, 2, result)
            self.assertEqual(len(self.fixture.store.list_objects(self.pid, kind='media')), before)
            self.assertEqual(self.fixture.c.provider_calls, [])

    def test_upload_refuses_symlinks_devices_and_changes_before_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            path, metadata, raw = self.upload_fixture(root)
            (root / 'link.png').symlink_to(path)
            (root / 'directory-link').symlink_to(root, target_is_directory=True)
            for bad in (root / 'link.png', root / 'directory-link' / 'image.png', root, Path('/dev/null')):
                start = len(self.calls)
                code, result, _, _ = self.run_cli(['upload', self.pid, str(bad), '--input', '-'], metadata)
                self.assertEqual(code, 2, result)
                self.assertTrue(all(call.method == 'GET' for call in self.calls[start:]))
            service = self.service
            class ChangeBeforeReading(httpx.BaseTransport):
                def handle_request(self, request):
                    if request.method == 'POST':
                        path.write_bytes(raw + b'changed')
                    return service(request)
            code, result, _, _ = self.run_cli(['upload', self.pid, str(path), '--input', '-'], metadata,
                transport=ChangeBeforeReading())
            self.assertEqual(code, 2, result)
            self.assertEqual(self.fixture.store.list_objects(self.pid, kind='media'), [])

    def test_mutation_connection_loss_never_retries_or_changes_idempotency(self):
        seen = []
        body = {'idempotency_key': 'stable-request', 'title': 'Lost response', 'branch': 'original'}
        def lose_response(request):
            seen.append(request)
            # The server may have committed before the connection vanished.
            self.service(request)
            raise httpx.ReadTimeout('SECRET should never be echoed', request=request)
        code, result, _, err = self.run_cli(['create-project', '--input', '-'], body,
                                           transport=httpx.MockTransport(lose_response))
        self.assertEqual(code, 7, result)
        self.assertEqual(len(seen), 1)
        self.assertEqual(json.loads(seen[0].content), body)
        self.assertNotIn('SECRET', err)
        code, project, _, _ = self.run_cli(['create-project', '--input', '-'], body)
        self.assertEqual(code, 0, project)
        projects = self.run_cli(['projects'])[1]
        self.assertEqual(sum(p['title'] == 'Lost response' for p in projects), 1)


class FolderCLITests(CLIBase):
    """the agent works in an HF-shaped folder (production/FOLDER.md) with a few commands."""
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.films = Path(tmp.name)
        self.root = self.films / '送货车'
        self.root.mkdir()
        (self.root / 'brief.md').write_text('# Logline\nA blue cart keeps going.\n')

    def test_open_creates_the_project_writes_its_id_fetches_the_manuals_and_makes_the_skeleton(self):
        from production import playbook
        code, value, _, _ = self.run_cli(['open', str(self.root)])
        self.assertEqual(code, 0, value)
        pid = value['project_id']
        self.assertTrue(value['created'])
        self.assertEqual((self.root / 'brief.md').read_text(), f'project: {pid}\n# Logline\nA blue cart keeps going.\n')
        self.assertEqual(value['manuals']['version'], playbook.version())
        self.assertTrue((self.films / '.manuals' / playbook.version() / 'writer.md').exists())  # outside the film's folder
        for name in ('ASSETS/CHARACTERS', 'ASSETS/LOCATIONS', 'ASSETS/PROPS', 'script.md', 'registry.md'):
            self.assertTrue((self.root / name).exists(), name)
        self.assertEqual(self.fixture.store.get_object(pid, pid)['body']['title'], '送货车')
        again = self.run_cli(['open', str(self.root)])[1]  # resuming never makes a second project
        self.assertEqual((again['project_id'], again['created'], again['skeleton_made']), (pid, False, []))

    @staticmethod
    def _png(color):
        from PIL import Image
        out = io.BytesIO()
        Image.new('RGB', (160, 96), color).save(out, format='PNG')
        return out.getvalue()

    def _write_film(self):
        (self.root / 'script.md').write_text('INT. DEPOT - NIGHT\nKel loads the cart.\n')
        (self.root / 'registry.md').write_text('## Looks\n### city\nWet neon, anamorphic, 35mm grain.\n\n'
                                              '## @kel · character\nKel, 30s courier, yellow raincoat.\n')
        (self.root / 'ASSETS/CHARACTERS/@kel.md').write_text('Full-body turnaround of Kel in a yellow raincoat, grey backdrop.\n')
        (self.root / 'ASSETS/CHARACTERS/@kel.png').write_bytes(self._png('yellow'))
        scene = self.root / 'SCENE 01 - DEPOT'
        scene.mkdir()
        (scene / 'shotlist.md').write_text('The depot at night.\n\n'
                                           '## 010 · 6s · Kel loads the cart\nlook: city\n@kel lifts a crate into the cart. Slow push in.\n\n'
                                           '## 020 · 5s · The cart leaves\n@kel pedals away into the rain. Wide, static.\n')

    def test_a_hand_placed_video_is_the_assets_reference(self):
        """a previs or turnaround clip beside the prompt (ASSETS/<KIND>/<tag>.mp4) is uploaded as
        video/mp4 and becomes the asset's one reference; a picture and a video for one asset is refused."""
        import subprocess
        self.assertEqual(self.run_cli(['open', str(self.root)])[0], 0)
        self._write_film()
        (self.root / 'ASSETS/CHARACTERS/@kel.png').unlink()
        clip = self.root / 'ASSETS/CHARACTERS/@kel.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=yellow:s=320x180:r=24:d=2', '-c:v', 'libx264',
                        '-pix_fmt', 'yuv420p', str(clip)], check=True)
        code, value, _, _ = self.run_cli(['push', str(self.root)])
        self.assertEqual(code, 0, value)
        store = self.fixture.store
        asset = next(o for o in store.list_objects(value['project_id'], kind='asset') if o['body']['content']['tag'] == '@kel')
        media = store.get_object(value['project_id'], asset['body']['content']['media_refs'][0]['object_id'])
        self.assertEqual(media['body']['media_type'], 'video/mp4')
        (self.root / 'ASSETS/CHARACTERS/@kel.png').write_bytes(self._png('yellow'))
        code, value, _, _ = self.run_cli(['push', str(self.root)])
        self.assertNotEqual(code, 0)
        self.assertIn('one picture or one video per asset', json.dumps(value, ensure_ascii=False))

    def test_push_sends_what_changed_and_nothing_else(self):

        self.assertEqual(self.run_cli(['open', str(self.root)])[0], 0)
        self._write_film()
        code, value, _, _ = self.run_cli(['push', str(self.root)])
        self.assertEqual(code, 0, value)
        pid = value['project_id']
        self.assertEqual(value['sent'], ['brief', 'script', 'look:city', 'asset:@kel', 'scene:01', 'shot:01:010', 'shot:01:020'])
        store = self.fixture.store
        shots = {o['body']['content']['shot']: o for o in store.list_objects(pid, kind='shot')}
        self.assertEqual(shots['S01-010']['body']['content']['_production'],
                         {'prompt': '@kel lifts a crate into the cart. Slow push in.', 'look': 'look_city'})
        self.assertEqual(shots['S01-020']['body']['content']['The material']['the running time in seconds'], 5)
        asset = next(o for o in store.list_objects(pid, kind='asset') if o['body']['content']['tag'] == '@kel')
        media = asset['body']['content']['media_refs']
        self.assertEqual(len(media), 1)
        self.assertEqual(asset['body']['content']['definition']['description'],
                         'Full-body turnaround of Kel in a yellow raincoat, grey backdrop.')

        calls = len(self.calls)
        again = self.run_cli(['push', str(self.root)])[1]
        self.assertEqual((again['sent'], again['unchanged']), ([], 7))
        self.assertEqual(len(self.calls), calls)  # an unchanged folder sends no request at all

        shotlist = self.root / 'SCENE 01 - DEPOT/shotlist.md'
        shotlist.write_text(shotlist.read_text().replace('Wide, static.', 'Wide, static. Rain on the lens.'))
        registry = self.root / 'registry.md'
        registry.write_text(registry.read_text().replace('yellow raincoat.', 'yellow raincoat, scar on chin.'))
        edited = self.run_cli(['push', str(self.root)])[1]
        self.assertEqual(edited['sent'], ['asset:@kel', 'shot:01:020'])
        shot = store.get_object(pid, shots['S01-020']['object_id'])
        self.assertEqual(shot['revision'], 2)
        self.assertEqual(shot['body']['content']['_production']['prompt'],
                         '@kel pedals away into the rain. Wide, static. Rain on the lens.')
        self.assertEqual(shot['body']['content']['_production']['change_note'], 'Changed in the folder: prompt')
        asset = store.get_object(pid, asset['object_id'])
        self.assertEqual(asset['body']['content']['media_refs'], media)  # the image the platform holds is kept
        self.assertEqual(asset['body']['content']['definition']['descriptor'], 'Kel, 30s courier, yellow raincoat, scar on chin.')

    def test_push_uploads_a_new_image_and_refuses_a_folder_it_cannot_read(self):
        self.assertEqual(self.run_cli(['open', str(self.root)])[0], 0)
        self._write_film()
        pid = self.run_cli(['push', str(self.root)])[1]['project_id']
        store = self.fixture.store
        before = next(o for o in store.list_objects(pid, kind='asset') if o['body']['content']['tag'] == '@kel')
        (self.root / 'ASSETS/CHARACTERS/@kel.png').write_bytes(self._png('orange'))
        self.assertEqual(self.run_cli(['push', str(self.root)])[1]['sent'], ['asset:@kel'])
        after = store.get_object(pid, before['object_id'])
        self.assertNotEqual(after['body']['content']['media_refs'], before['body']['content']['media_refs'])
        (self.root / 'SCENE 01 - DEPOT/shotlist.md').write_text('## 030 · 45s · Too long\nA prompt.\n')
        code, value, _, _ = self.run_cli(['push', str(self.root)])
        self.assertEqual(code, 2, value)
        self.assertIn('4–30', value['error']['message'])

    def _movie(self):
        """A real 6 s 1920×1080 clip with sound (the test route's size), made once per test."""
        if getattr(self, 'movie', None) is None:
            import subprocess
            path = self.films / 'take.mp4'
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=size=1920x1080:rate=24:duration=6',
                            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=6', '-c:v', 'libx264',
                            '-threads', '1', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(path)],
                           check=True, capture_output=True, timeout=30)
            self.movie = path.read_bytes()
        return self.movie

    def _image_provider(self, color='teal'):
        """The image method on and a fake image provider (as test_journeys.publish_image_fixture); the worker runs
        whenever the agent reads a record, standing in for the deployed worker."""
        from contextlib import contextmanager

        from PIL import Image

        from production.jobs import Download, SafeDownloader
        from production.runtime_config import RuntimeConfig
        from production.tests.fixtures import FakeProvider
        c = self.fixture.c
        methods = c.config.section('methods')
        methods['methods']['mvgp-image-generate-v1'] = RuntimeConfig.load().require_method('mvgp-image-generate-v1')
        c.config.set('methods', methods)
        self.image_prompts, self.worker_paused, self.image_fails = [], False, False
        self.video_prompts = []
        def native(action, request):
            if 'image' not in str(request.get('job_type')) and 'banana' not in str(request.get('job_type')):
                self.video_prompts.append(request['params']['prompt'])
                return {'id': f'video-{len(self.video_prompts)}', 'status': 'completed', 'result_url': 'https://cdn.example/video'}
            self.image_prompts.append(request['params']['prompt'])
            return {'id': f'fake-{len(self.image_prompts)}', 'status': 'failed' if self.image_fails else 'completed',
                    'result_url': 'https://cdn.example/image'}
        out = io.BytesIO()
        Image.new('RGB', (2048, 1152), color).save(out, 'PNG')
        self.generated = out.getvalue()
        @contextmanager
        def download(url, host, ip, timeout):
            if url.endswith('/video'):
                yield Download('video/mp4', [self._movie()])
            else:
                yield Download('image/png', [self.generated])
        c.jobs.downloader = SafeDownloader({'cdn.example'}, resolver=lambda host: ['8.8.8.8'], transport=download)
        from production.worker import Worker
        pid = folder.project_id(self.root)
        c.store.set_budget(pid, 40, 'synthetic_unit')
        actor = c.auth.authenticate(c.auth.provision_token('worker', 'worker', [pid], 3600))
        worker = Worker(c.jobs, FakeProvider({'seedance_2_5', 'nano_banana_pro'}, native), actor, [pid], cuts=c.cuts)
        service = self.service
        def working(request):
            if request.method == 'GET' and '/artifacts/' in request.url.path and not self.worker_paused:
                worker.run_once()
            return service(request)
        self.transport = httpx.MockTransport(working)

    def test_image_makes_the_picture_puts_it_in_the_folder_and_never_pays_twice(self):

        self.assertEqual(self.run_cli(['open', str(self.root)])[0], 0)
        self._write_film()
        (self.root / 'ASSETS/CHARACTERS/@kel.png').unlink()
        self._image_provider()
        code, value, _, _ = self.run_cli(['image', str(self.root), '@kel', '--wait-seconds', '5'])
        self.assertEqual(code, 0, value)
        self.assertIn('asset:@kel', value['pushed'])
        self.assertEqual([(i['tag'], i['state'], i['path']) for i in value['images']],
                         [('@kel', 'succeeded', 'ASSETS/CHARACTERS/@kel.png')])
        self.assertEqual((self.root / 'ASSETS/CHARACTERS/@kel.png').read_bytes(), self.generated)
        self.assertEqual(self.image_prompts, ['Full-body turnaround of Kel in a yellow raincoat, grey backdrop.'])
        pid = value['project_id']
        asset = next(o for o in self.fixture.store.list_objects(pid, kind='asset') if o['body']['content']['tag'] == '@kel')
        self.assertEqual(len(asset['body']['content']['media_refs']), 1)  # the platform bound it to the asset
        # The picture came from the platform: the next push sends nothing back.
        self.assertEqual(self.run_cli(['push', str(self.root)])[1]['sent'], [])
        # A new image prompt makes a new image; the old picture is kept aside, not lost.
        prompt = self.root / 'ASSETS/CHARACTERS/@kel.md'
        prompt.write_text('Kel, 3/4 portrait, yellow raincoat, grey backdrop.\n')
        self._image_provider('purple')
        value = self.run_cli(['image', str(self.root), '@kel', '--wait-seconds', '5'])[1]
        self.assertIn('images', value, value)
        self.assertEqual(value['images'][0]['state'], 'succeeded', value)
        self.assertEqual(len(self.image_prompts), 1)
        self.assertEqual(len(list((self.root / '.mvgp/replaced').glob('@kel-*.png'))), 1)

    def test_image_refuses_an_asset_without_an_image_prompt_before_any_spend(self):
        self.assertEqual(self.run_cli(['open', str(self.root)])[0], 0)
        self._write_film()
        (self.root / 'ASSETS/CHARACTERS/@kel.md').unlink()
        (self.root / 'ASSETS/CHARACTERS/@kel.png').unlink()
        self._image_provider()
        code, value, _, _ = self.run_cli(['image', str(self.root), '@kel', '@nobody'])
        self.assertEqual(code, 2, value)
        self.assertIn('@nobody is not an asset', value['error']['message'])
        self.assertIn('ASSETS/<KIND>/@kel.md', value['error']['message'])
        self.assertEqual(self.image_prompts, [])

    def test_image_resumes_after_a_timeout_without_a_second_order(self):
        self.assertEqual(self.run_cli(['open', str(self.root)])[0], 0)
        self._write_film()
        (self.root / 'ASSETS/CHARACTERS/@kel.png').unlink()
        self._image_provider()
        self.worker_paused = True
        waiting = self.run_cli(['image', str(self.root), '@kel', '--wait-seconds', '0'])[1]
        self.assertEqual(waiting['images'][0]['state'], 'waiting', waiting)
        self.worker_paused = False
        done = self.run_cli(['image', str(self.root), '@kel', '--wait-seconds', '5'])[1]
        self.assertEqual(done['images'][0]['state'], 'succeeded', done)
        self.assertEqual(len(self.image_prompts), 1)
        jobs = self.fixture.store.list_objects(done['project_id'], kind='job')
        self.assertEqual(len(jobs), 1)

    def test_quote_and_shoot_from_the_folder(self):
        # (audit: AGENT_GUIDE named a shoot order the CLI could not send).
        self.assertEqual(self.run_cli(['open', str(self.root)])[0], 0)
        self._write_film()
        self._image_provider()
        code, value, _, _ = self.run_cli(['quote', str(self.root)])
        self.assertEqual(code, 0, value)
        self.assertEqual([(c['shot'], c['takes'], c['drafts']) for c in value['cards']], [('S01-010', 4, 4), ('S01-020', 4, 4)])
        self.assertEqual(value['advice'], [])  # a card that leaves the route out is priced on the route it will use
        self.assertEqual([c['shot'] for c in self.run_cli(['quote', str(self.root), '01:020'])[1]['cards']], ['S01-020'])
        pid = value['project_id']
        self.assertEqual(self.fixture.store.list_objects(pid, kind='job'), [])  # a quote spends nothing

        review = self.root / 'review.json'
        review.write_text(json.dumps({'reviewer': 'fresh reviewer', 'notes': [
            {'line': 'cinedance:336', 'note': 'No lens named.', 'answer': 'Kept: the push-in carries it.', 'shot': 'S01-010'}]}))
        code, value, _, _ = self.run_cli(['shoot', str(self.root), 'S01-010', '--review', str(review)])
        self.assertEqual(code, 0, value)
        self.assertEqual(value['shots'], ['01:010'])
        order = self.fixture.store.get_object(pid, value['order']['order']['object_id'])
        self.assertEqual(order['body']['review']['notes'][0]['line'], 'cinedance:336')
        self.assertEqual([c['takes'] for c in order['body']['cards']], [4])
        candidate = self.fixture.store.get_object(pid, value['order']['cards'][0]['candidate']['object_id'])
        sent = candidate['body']['request']['params']['prompt']
        self.assertIn('lifts a crate into the cart. Slow push in.', sent)  # the writer's text, as written
        self.assertIn('STYLE: Wet neon, anamorphic, 35mm grain.', sent)    # the shot's named look, added by the platform
        again = self.run_cli(['shoot', str(self.root), 'S01-010', '--review', str(review)])[1]
        self.assertEqual(again['order']['order']['object_id'], value['order']['order']['object_id'])
        self.assertEqual(len(self.fixture.store.list_objects(pid, kind='shoot-order')), 1)

        self.assertEqual(self.run_cli(['shoot', str(self.root)])[0], 2)  # name what to shoot
        code, value, _, _ = self.run_cli(['shoot', str(self.root), 'S09-999'])
        self.assertEqual(code, 2, value)
        self.assertIn('S09-999', value['error']['message'])

    def test_pull_writes_the_log_and_downloads_the_picks(self):
        # real takes from the worker, the owner's pick and notes through his own session.
        from fastapi.testclient import TestClient

        from production.tests.fixtures import owner_jwt
        self.assertEqual(self.run_cli(['open', str(self.root)])[0], 0)
        self._write_film()
        self._image_provider()
        review = self.root / 'review.json'
        review.write_text(json.dumps({'reviewer': 'fresh reviewer', 'notes': [
            {'line': 'cinedance:336', 'note': 'No lens named.', 'answer': 'Kept: the push-in carries it.', 'shot': 'S01-010'}]}))
        pid = self.run_cli(['shoot', str(self.root), 'S01-010', '--review', str(review)])[1]['project_id']
        for _ in range(6):
            self.run_cli(['quote', str(self.root), 'S01-010'])  # each read lets the worker run
        store = self.fixture.store
        shot = next(o for o in store.list_objects(pid, kind='shot') if o['body']['content']['shot'] == 'S01-010')
        shot_ref = {k: shot[k] for k in ('object_id', 'revision', 'digest')}
        takes = [{k: m[k] for k in ('object_id', 'revision', 'digest')} for m in store.list_objects(pid, kind='media')
                 if m['author'] == 'worker_service' and m['body'].get('media_type') == 'video/mp4']
        self.assertEqual(len(takes), 4)
        offered = self.fixture.client.post(f'/v1/projects/{pid}/decision-requests', headers=self.fixture.headers, json={
            'idempotency_key': 'offer', 'expected_revision': shot['revision'], 'target': shot_ref, 'shot': shot_ref,
            'takes': takes, 'purpose': 'take', 'rationale': 'Pick the take for this shot.'})
        self.assertEqual(offered.status_code, 200, offered.text)
        owner = TestClient(self.fixture.app, base_url='https://craft.example')
        self.addCleanup(owner.close)
        csrf = owner.post('/v1/session/access', json={}, headers={'Origin': 'https://craft.example',
                                                                   'cf-access-jwt-assertion': owner_jwt()}).json()['csrf_token']
        origin = {'Origin': 'https://craft.example'}
        for body in ({'idempotency_key': 'n1', 'target': shot_ref, 'take': takes[1], 'at_seconds': 3.2, 'text': '这里车停得太早'},
                     {'idempotency_key': 'n2', 'target': shot_ref, 'text': '整体节奏对'}):
            answer = owner.post(f'/v1/projects/{pid}/owner-notes', headers=origin, json={**body, 'csrf_token': csrf})
            self.assertEqual(answer.status_code, 200, answer.text)
        picked = owner.post(f'/v1/projects/{pid}/human-decisions', headers=origin, json={
            'idempotency_key': 'pick', 'request_id': offered.json()['object_ref']['object_id'], 'target_hash': shot['digest'],
            'choice': 'confirm', 'selected_take': takes[1], 'reason': '第二条的推进最稳', 'csrf_token': csrf})
        self.assertEqual(picked.status_code, 200, picked.text)

        code, value, _, _ = self.run_cli(['pull', str(self.root)])
        self.assertEqual(code, 0, value)
        self.assertEqual(len(value['downloaded']), 1, value)
        pick = self.root / value['downloaded'][0]
        self.assertEqual(pick.read_bytes(), self.movie)
        log = (self.root / 'log.md').read_text()
        self.assertIn('## S01-010 · Kel loads the cart · 已选', log)
        self.assertIn('| 1 | — | 4 | 选了 | 第二条的推进最稳 |', log)
        self.assertIn('Reviewer: fresh reviewer', log)
        self.assertIn('- cinedance:336: No lens named. → Kept: the push-in carries it.', log)
        self.assertIn('第 2 条 at 3.2 s: 这里车停得太早', log)
        self.assertEqual(log.count('这里车停得太早'), 1)  # a take's note is listed once, not under the shot again
        self.assertIn('整条镜头: 整体节奏对', log)
        self.assertIn('## S01-020 · The cart leaves · 未拍', log)
        self.assertIn(f"- {value['downloaded'][0]} (picked)", log)
        again = self.run_cli(['pull', str(self.root)])[1]
        self.assertEqual((again['downloaded'], again['already_here']), ([], value['downloaded']))
        every = self.run_cli(['pull', str(self.root), '--all'])[1]
        self.assertEqual(len(every['downloaded']), 3)
        self.assertEqual(len(list((self.root / 'TAKES').glob('*.mp4'))), 4)

    def test_push_lists_a_recreation_shots_source_and_keeps_it_on_every_revision(self):
        # reads do not return dependencies, so the folder names the source-understanding.
        self.assertEqual(self.run_cli(['open', str(self.root)])[0], 0)
        self._write_film()
        pid = folder.project_id(self.root)
        store = self.fixture.store
        source = store.create_object(pid, 'source-understanding', {'logical_path': 'source/s01.json', 'content': {
            'type': 'source-understanding', 'start_seconds': 0, 'end_seconds': 6, 'observed_facts': ['Kel loads the cart.']}},
            'api_author')
        shotlist = self.root / 'SCENE 01 - DEPOT/shotlist.md'
        shotlist.write_text(shotlist.read_text().replace('look: city\n', f"look: city\nsource: {source['object_id']}\n"))
        self.assertEqual(self.run_cli(['push', str(self.root)])[0], 0)
        card = next(o for o in store.list_objects(pid, kind='shot') if o['body']['content']['shot'] == 'S01-010')
        self.assertIn(source['object_id'], [d['object_id'] for d in card['body']['dependencies']])
        self.assertNotIn('source', card['body']['content']['_production'])  # the prompt and look only
        shotlist.write_text(shotlist.read_text().replace('Slow push in.', 'Slow push in, low angle.'))
        self.assertEqual(self.run_cli(['push', str(self.root)])[1]['sent'], ['shot:01:010'])
        card = store.get_object(pid, card['object_id'])
        self.assertEqual(card['revision'], 2)
        self.assertIn(source['object_id'], [d['object_id'] for d in card['body']['dependencies']])
        shotlist.write_text(shotlist.read_text().replace(f"source: {source['object_id']}", 'source: obj_missing'))
        self.assertNotEqual(self.run_cli(['push', str(self.root)])[0], 0)

    def test_image_replays_a_lost_submit_answer_instead_of_ordering_again(self):
        # Bug hunt 2026-09-27: the order was recorded only after the submit answer came back; a lost answer, the
        # image binding meanwhile (a new asset revision) and a rerun placed a second paid order.
        self.assertEqual(self.run_cli(['open', str(self.root)])[0], 0)
        self._write_film()
        (self.root / 'ASSETS/CHARACTERS/@kel.png').unlink()
        self._image_provider()
        inner, lose = self.transport, [True]
        def lossy(request):
            if request.method == 'POST' and request.url.path.endswith('/submissions') and lose[0]:
                lose[0] = False
                inner.handle_request(request)  # the platform took the order
                raise httpx.ReadTimeout('answer lost', request=request)
            return inner.handle_request(request)
        self.assertEqual(self.run_cli(['image', str(self.root), '@kel'], transport=httpx.MockTransport(lossy))[0], 7)
        pid = folder.project_id(self.root)
        for _ in range(3):
            self.run_cli(['quote', str(self.root)])  # the worker runs: the image is made and bound to the asset
        value = self.run_cli(['image', str(self.root), '@kel', '--wait-seconds', '5'])[1]
        self.assertEqual([(i['tag'], i['state']) for i in value['images']], [('@kel', 'succeeded')])
        self.assertEqual((len(self.image_prompts), len(self.fixture.store.list_objects(pid, kind='job'))), (1, 1))

    def test_a_failed_image_can_be_ordered_again_with_the_same_prompt(self):
        self.assertEqual(self.run_cli(['open', str(self.root)])[0], 0)
        self._write_film()
        (self.root / 'ASSETS/CHARACTERS/@kel.png').unlink()
        self._image_provider()
        self.image_fails = True
        failed = self.run_cli(['image', str(self.root), '@kel', '--wait-seconds', '5'])[1]
        self.assertEqual(failed['images'][0]['state'], 'failed', failed)
        self.image_fails = False
        again = self.run_cli(['image', str(self.root), '@kel', '--wait-seconds', '5'])[1]
        self.assertEqual(again['images'][0]['state'], 'succeeded', again)
        self.assertEqual(len(self.image_prompts), 2)

    # Bug hunt 2026-09-27: each of these failed before its fix.
    def test_a_shot_deleted_from_the_folder_is_never_quoted_or_shot(self):
        self.assertEqual(self.run_cli(['open', str(self.root)])[0], 0)
        self._write_film()
        self._image_provider()
        self.assertEqual(self.run_cli(['push', str(self.root)])[0], 0)
        shotlist = self.root / 'SCENE 01 - DEPOT/shotlist.md'
        shotlist.write_text(shotlist.read_text().split('## 020')[0])
        self.assertEqual([c['shot'] for c in self.run_cli(['quote', str(self.root)])[1]['cards']], ['S01-010'])
        self.assertEqual(self.run_cli(['shoot', str(self.root), '01'])[1]['shots'], ['01:010'])
        log = (self.root / 'log.md')
        self.run_cli(['pull', str(self.root)])
        self.assertNotIn('S01-020', log.read_text())

    def test_two_tags_may_share_a_picture(self):
        self.assertEqual(self.run_cli(['open', str(self.root)])[0], 0)
        self._write_film()
        registry = self.root / 'registry.md'
        registry.write_text(registry.read_text() + '\n## @jack · character\nJack, tall man, grey coat.\n')
        (self.root / 'ASSETS/CHARACTERS/@jack.md').write_text('Jack turnaround.\n')
        import shutil
        shutil.copy(self.root / 'ASSETS/CHARACTERS/@kel.png', self.root / 'ASSETS/CHARACTERS/@jack.png')
        code, value, _, _ = self.run_cli(['push', str(self.root)])
        self.assertEqual(code, 0, value)
        self.assertIn('asset:@jack', value['sent'])

    def test_an_asset_change_reaches_the_next_order_of_an_unchanged_card(self):
        self.assertEqual(self.run_cli(['open', str(self.root)])[0], 0)
        self._write_film()
        self._image_provider()
        first = self.run_cli(['shoot', str(self.root), 'S01-010', '--takes', '1'])[1]
        pid, store = first['project_id'], self.fixture.store
        (self.root / 'ASSETS/CHARACTERS/@kel.png').write_bytes(self._png('orange'))
        registry = self.root / 'registry.md'
        registry.write_text(registry.read_text().replace('yellow raincoat.', 'red raincoat.'))
        second = self.run_cli(['shoot', str(self.root), 'S01-010', '--takes', '1'])[1]
        self.assertNotEqual(second['order']['order']['object_id'], first['order']['order']['object_id'])
        self.assertIsNotNone(second['order']['cards'][0].get('candidate'), second['order'])
        candidate = store.get_object(pid, second['order']['cards'][0]['candidate']['object_id'])
        sent = {r['sha256'] for r in candidate['body']['request']['references']}
        self.assertIn(hashlib.sha256(self._png('orange')).hexdigest(), sent)
        self.assertIn('red raincoat', json.dumps(candidate['body']['compilation']['prompt_constants']))

    def test_a_copied_folder_starts_clean_on_its_new_project(self):
        import shutil
        self.assertEqual(self.run_cli(['open', str(self.root)])[0], 0)
        self._write_film()
        old = self.run_cli(['push', str(self.root)])[1]['project_id']
        copy = self.films / 'copy'
        shutil.copytree(self.root, copy)
        brief = copy / 'brief.md'
        text = brief.read_text()
        brief.write_text(text.split('\n', 1)[1])
        new = self.run_cli(['open', str(copy)])[1]['project_id']
        self.assertNotEqual(new, old)
        self.assertEqual(len(self.run_cli(['push', str(copy)])[1]['sent']), 7)
        brief.write_text(f'project: {old}\n' + brief.read_text().split('\n', 1)[1])  # state and brief now disagree
        code, value, _, _ = self.run_cli(['push', str(copy)])
        self.assertEqual(code, 2, value)
        self.assertIn('belongs to', value['error']['message'])

    def test_the_change_note_names_only_what_this_version_changed(self):
        self.assertEqual(self.run_cli(['open', str(self.root)])[0], 0)
        self._write_film()
        pid = self.run_cli(['push', str(self.root)])[1]['project_id']
        shotlist = self.root / 'SCENE 01 - DEPOT/shotlist.md'
        for extra in (' Rain.', ' Wind.'):
            shotlist.write_text(shotlist.read_text().replace('Wide, static.', 'Wide, static.' + extra))
            self.run_cli(['push', str(self.root)])
        shot = next(o for o in self.fixture.store.list_objects(pid, kind='shot') if o['body']['content']['shot'] == 'S01-020')
        self.assertEqual((shot['revision'], shot['body']['content']['_production']['change_note']), (3, 'Changed in the folder: prompt'))
