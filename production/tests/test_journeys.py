"""Agent journeys through CLI/HTTP, with synthetic model and human counterparts.

The independently authored visual oracle is a blue cart crossing a fixed red
marker and remaining on its far side. These checks prove workflow and real bytes,
not a model's film comprehension or an independently owned human device.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from production import cli, playbook
from production.jobs import Download, SafeDownloader
from production.reader import ReaderResponse
from production.runtime_config import RuntimeConfig
from production.tests import test_api
from production.tests.fixtures import FakeProvider, owner_jwt

STORY = ('A blue delivery cart approaches the red checkpoint, crosses it to the right, '
         'then keeps moving away on that same side. The next view must not put it behind the checkpoint again.')
EXPECTED = ['The audience sees an approach become a completed crossing.',
            'The audience sees continued escape past the checkpoint, rather than an unexplained reset.']


def ref(obj):
    return {key: obj[key] for key in ('object_id', 'revision', 'digest')}


class JourneyTests(unittest.TestCase):
    def setUp(self):
        self.f = test_api.MutationAPITests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.c, self.store = self.f.c, self.f.store
        self.pid = self.f.pid
        self.headers = self.f.headers
        self.env = {'MVGP_URL': 'https://craft.example', 'MVGP_TOKEN': self.f.token}
        self.transport = httpx.MockTransport(self.forward)
        self.counter = 0
        self.responses = {}; self.native_calls = []; self.review_calls = []; self.read_calls = []
        self.manifest = None; self.next_verdict = 'pass'
        self.publish_image_fixture()
        self.c.worker.provider.transport = self.native
        self.c.worker.reader.transport = self.observe_http
        self.c.jobs.downloader = SafeDownloader({'cdn.example'}, resolver=lambda host: ['8.8.8.8'], transport=self.download)

    def publish_image_fixture(self):
        # The image-generate method is enabled beside video (TEST-only; runtime config).
        methods = self.c.config.section('methods')
        methods['methods']['mvgp-image-generate-v1'] = RuntimeConfig.load().require_method('mvgp-image-generate-v1')
        self.c.config.set('methods', methods)
        self.c.worker.provider = FakeProvider({'seedance_2_5', 'nano_banana_pro'}, self.native)

    def key(self):
        self.counter += 1
        return 'journey-'+str(self.counter)

    def forward(self, request):
        self.agent_calls = getattr(self, 'agent_calls', 0) + 1
        response = self.f.client.request(request.method, str(request.url), content=request.read(), headers=dict(request.headers))
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    def command(self, name, body=None, *, extra=(), ok=True):
        args = [name]
        if name not in ('discovery', 'projects', 'create-project', 'resolve'): args.append(self.pid)
        args += list(extra)
        if body is not None: args += ['--input', '-']
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(args, environ=self.env, stdin=io.StringIO(json.dumps(body) if body is not None else ''),
                        stdout=out, stderr=err, transport=self.transport)
        value = json.loads(out.getvalue() or err.getvalue())
        self.assertNotIn(self.f.token, out.getvalue()+err.getvalue())
        if ok: self.assertEqual(code, 0, value)
        else: self.assertNotEqual(code, 0, value)
        return value

    def object(self, value):
        return self.store.get_object(self.pid, value['object_ref']['object_id'], revision=value['object_ref']['revision'])

    def draft(self, kind, path, content, deps=()):
        return self.command('draft', {'idempotency_key': self.key(), 'expected_revision': 0, 'kind': kind,
            'logical_path': path, 'content': content, 'dependencies': [v['object_ref'] for v in deps]})

    def upload(self, raw, name, media_type, **extra):
        path = (self.c.root/name).resolve(); path.write_bytes(raw)
        return self.command('upload', {'idempotency_key': self.key(), 'logical_path': 'uploads/'+name,
            'media_type': media_type, 'sha256': hashlib.sha256(raw).hexdigest(), 'byte_length': len(raw), **extra}, extra=[str(path)])

    def native(self, action, request):
        self.native_calls.append(request)
        prompt = request['params']['prompt']
        _request, raw, _kind = self.responses[prompt]
        return {'id': 'fake-'+str(len(self.native_calls)), 'status': 'failed' if raw is None else 'completed',
                'result_url': 'https://cdn.example/'+hashlib.sha256(prompt.encode()).hexdigest()}

    @contextmanager
    def download(self, url, host, ip, timeout):
        suffix = url.rsplit('/', 1)[1]
        _, raw, kind = next(v for prompt, v in self.responses.items() if hashlib.sha256(prompt.encode()).hexdigest() == suffix)
        yield Download(kind, [raw])

    def review_http(self, request):
        payload = json.loads(request.content); self.review_calls.append(payload)
        manifest = self.manifest
        native = manifest.get('review_mode') == 'native-video-brief-v1'
        if native:
            # The watching director (video take/cut) posts one Gemini generateContent request with the MP4s.
            self.assertTrue(request.url.path.endswith('gemini-3.8-flash:generateContent'))
            self.assertEqual(sum('inlineData' in part for part in payload['contents'][0]['parts']), len(manifest['resources']))
        else:
            self.assertEqual(payload['model'], self.c.routes['profiles'][manifest['route_profile_id']]['model'])
        citations = ([{'resource_id': 'request', 'path': '/params/prompt'}] if manifest['purpose'] == 'preflight'
                     else [{'resource_id': r['resource_id'], 'start_seconds': 0.0, 'end_seconds': 0.1} if native
                           else {'resource_id': r['resource_id']} for r in manifest['resources']])
        if manifest['evidence']['context'].get('repair_comparisons'):
            citations.append({'resource_id': 'context', 'path': '/repair_comparisons/0'})
        value = {'verdict': self.next_verdict, 'summary': 'Synthetic controlled review of the actual delivered evidence.',
                 'evidence': citations, 'findings': []}
        if self.next_verdict == 'fail':
            value['findings'] = [{'kind': 'creative_discrepancy', 'message': EXPECTED[1], 'evidence': citations}]
        if manifest['purpose'] == 'asset':
            scope = manifest['evidence']['context']['asset_assessment']
            sample = next(r['resource_id'] for r in manifest['resources'] if r['object_ref'] == scope['sample'])
            value['asset_assessments'] = [{'asset': asset, 'sample_resource_id': sample, 'recognition': 'yes',
                'coframe': 'present', 'observation': 'Synthetic image comparison: cart and red marker are both visible.', 'evidence': citations}
                for asset in scope.get('assessed_assets', [scope['asset']])]
        if native:
            return httpx.Response(200, json={'responseId': 'review-'+str(len(self.review_calls)), 'modelVersion': 'gemini-3.8-flash',
                'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': json.dumps(value)}]}}],
                'usageMetadata': {'promptTokenCount': 30, 'candidatesTokenCount': 10, 'totalTokenCount': 40,
                                  'promptTokensDetails': [{'modality': 'TEXT', 'tokenCount': 20}, {'modality': 'VIDEO', 'tokenCount': 10}]}})
        return httpx.Response(200, json={'id': 'review-'+str(len(self.review_calls)), 'model': payload['model'],
            'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': json.dumps(value)}}],
            'usage': {'total_tokens': 20}})

    def observe_http(self, endpoint, payload, headers, timeout, maximum):
        self.read_calls.append(payload)
        value = {'observations': [
            {'input_id': 'original', 'timestamp_seconds': 0.1, 'modality': 'visual', 'fact': 'A blue rectangle and red marker are visible.', 'interpretation': None},
            {'input_id': 'original', 'timestamp_seconds': 0.1, 'modality': 'audible', 'fact': 'A synthetic tone is audible.', 'interpretation': None}],
            'answers': [{'question_index': 0, 'verdict': 'supported', 'evidence_indices': [0, 1], 'explanation': 'Controlled synthetic evidence.'}],
            'uncertainty': ['Fake observer; no real video understanding is claimed.']}
        return ReaderResponse(200, json.dumps({'modelVersion': 'gemini-3.8-flash', 'candidates': [{'finishReason': 'STOP',
            'content': {'parts': [{'text': json.dumps(value)}]}}], 'usageMetadata': {'promptTokensDetails': [
                {'modality': 'VIDEO', 'tokenCount': 10}, {'modality': 'AUDIO', 'tokenCount': 10}]}}).encode())

    def worker(self):
        result = self.c.worker.run_once()
        self.assertTrue(result)
        self.assertFalse(any(o.get('error_code') for o in result), result)
        return result


    def observe(self, media):
        self.command('observe', {'idempotency_key': self.key(), 'expected_revision': media['object_ref']['revision'],
            'media_id': media['object_ref']['object_id'], 'reader': 'video', 'questions': ['What is visible and audible?']})
        self.assertEqual(self.worker()[0]['state'], 'succeeded')
        seen = [o for o in self.store.list_objects(self.pid, kind='observation') if o['author'] == 'reader_service'
                and (o['body'].get('source') or {}).get('object_id') == media['object_ref']['object_id']]
        return {k: seen[-1][k] for k in ('object_id', 'revision', 'digest')}

    def prepare(self, target, task, inputs=(), *, ok=True):
        if task in ('shot', 'stress', 'image', 'image-edit'):  # the agent takes the manuals first, including for images
            folder = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, folder, True)
            self.assertTrue(self.command('playbook', extra=['--dir', folder])['files'])
        mid = 'mvgp-image-generate-v1' if task == 'image' else 'mvgp-image-edit-v1' if task == 'image-edit' else 'mvgp-video-v1'
        previous = [o for o in self.store.list_objects(self.pid, kind='method') if o['body']['content']['target']['object_id'] == target['object_ref']['object_id']]
        method = self.command('select-method', {'idempotency_key': self.key(), 'expected_revision': previous[0]['revision'] if previous else target['object_ref']['revision'],
            'target': target['object_ref'], 'method_id': mid, 'rationale': 'Use the explicitly supported route for this synthetic task.'})
        return self.command('prepare', {'idempotency_key': self.key(), 'expected_revision': target['object_ref']['revision'],
            'target': target['object_ref'], 'task': task, 'method_selection': method['object_ref'], 'inputs': [v['object_ref'] for v in inputs]}, ok=ok)

    def generate(self, candidate, raw, kind='video/mp4'):
        request = self.object(candidate)['body']['request']
        self.responses[request['params']['prompt']] = (request, raw, kind)
        job = self.command('submit', {'idempotency_key': self.key(), 'expected_revision': 1, 'candidate_id': candidate['object_ref']['object_id']})
        self.assertEqual(self.worker()[0]['state'], 'succeeded')
        result = self.store.get_object(self.pid, self.object(job)['object_id'])['body']['result']
        return {'object_ref': result}

    def png(self, color, *, generated=False):
        out = io.BytesIO(); Image.new('RGB', (2048, 1152) if generated else (160, 96), color).save(out, 'PNG'); return out.getvalue()

    def movie(self, name, index=0, mode='pass', *, generated=False):
        root = self.c.root/name; root.mkdir()
        for n in range(24 if mode == 'source' else 12):
            image = Image.new('RGB', (160, 96), (240-index, 240, 240))
            draw = ImageDraw.Draw(image); draw.rectangle((77, 10, 83, 80), fill='red')
            x = (15+10*n if n < 12 else 105+n-12) if mode == 'source' else 15+10*n if mode == 'pass' else 105+n if mode == 'continue' else 20
            draw.rectangle((x, 50+index%5, x+10, 65+index%5), fill='blue'); image.save(root/f'{n:03d}.png')
        path = root/'clip.mp4'
        # Real delivered pixels; the short action and its oracle remain unchanged.
        filters = ['-vf', 'scale=1920:1080,setsar=1,tpad=stop_mode=clone:stop_duration=3.5'] if generated else []
        duration = '4' if generated else '1' if mode == 'source' else '0.5'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-framerate', '24', '-i', str(root/'%03d.png'),
            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration='+duration, *filters, '-c:v', 'libx264',
            '-threads', '1', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(path)], check=True, capture_output=True, timeout=20)
        return path.read_bytes()

    def create_project(self, branch):
        result = self.command('create-project', {'idempotency_key': self.key(), 'title': 'Checkpoint continuity '+branch, 'branch': branch, 'brief': STORY})
        self.pid = result['project_id']; self.f.pid = self.pid; self.c.pid = self.pid
        self.c.worker.projects = [self.pid]
        self.c.worker.principal = self.c.auth.authenticate(self.c.auth.provision_token('journey_worker', 'worker', [self.pid], 3600))
        self.store.set_budget(self.pid, 200, 'synthetic_unit')
        return result

    def owner_picks_become_the_film(self, picks):
        """the agent offers takes; after the owner's picks the platform alone makes and offers the film."""
        from production.worker import AutoFilm
        worker = self.c.worker
        film = self.c.auth.authenticate(self.c.auth.provision_token('film_service', 'agent', [self.pid], 3600))
        worker.film = AutoFilm(film, worker.cuts, self.c.jobs.submissions, self.f.decisions)
        worker.film_interval = 0
        requests = [self.command('request-decision', {'idempotency_key': self.key(), 'expected_revision': shot['object_ref']['revision'],
            'target': shot['object_ref'], 'shot': shot['object_ref'], 'takes': [take['object_ref']], 'purpose': 'take',
            'rationale': 'Pick the take for this shot.'}) for shot, take in picks]
        human = TestClient(self.f.app, base_url='https://craft.example')
        csrf = human.post('/v1/session/access', json={}, headers={'Origin': 'https://craft.example', 'cf-access-jwt-assertion': owner_jwt()}).json()['csrf_token']
        def decide(request_id, target_hash, **values):
            response = human.post('/v1/projects/'+self.pid+'/human-decisions', headers={'Origin': 'https://craft.example'},
                                  json={'idempotency_key': self.key(), 'request_id': request_id, 'target_hash': target_hash,
                                        'csrf_token': csrf, **values})
            self.assertEqual(response.status_code, 200, response.text)
        def film_until(goal):
            seen = []
            for _ in range(12):
                worker.run_once()
                seen.append(worker.film_status[self.pid])
                if seen[-1] == goal:
                    return seen
            self.fail(seen)
        self.assertEqual(film_until('waiting-for-picks'), ['waiting-for-picks'])
        for request, (shot, take) in zip(requests, picks, strict=True):
            decide(request['object_ref']['object_id'], shot['object_ref']['digest'], choice='confirm', selected_take=take['object_ref'])
        calls = self.agent_calls
        seen = film_until('waiting-for-owner')
        self.assertEqual([s for s in seen if s != 'rendering'],
                         ['cut-created', 'render-requested', 'final-requested', 'waiting-for-owner'])  # no finishing step
        self.assertEqual(self.agent_calls, calls, 'No agent call between the picks and the offered film')
        cut, = [o for o in self.store.list_objects(self.pid, kind='cut') if o['author'] == 'cut_service']
        self.assertEqual([s['take'] for s in cut['body']['segments']], [take['object_ref'] for _, take in picks])
        self.assertEqual(self.store.list_objects(self.pid, kind='finishing'), [])
        final, = [o for o in self.store.list_objects(self.pid, kind='decision-request') if o['body']['purpose'] == 'final']
        rendered = self.store.get_object(self.pid, final['body']['target']['object_id'])
        self.assertEqual((rendered['author'], rendered['body']['source_cut']['object_id']), ('cut_service', cut['object_id']))
        # Withdrawing a pick supersedes the offered film; picking again offers the same film anew.
        first = requests[0]['object_ref']['object_id']
        decide(first, picks[0][0]['object_ref']['digest'], choice='decline', reason='取消')
        self.assertEqual(film_until('waiting-for-picks')[-1], 'waiting-for-picks')
        self.assertEqual(self.store.get_object(self.pid, final['object_id'])['body']['state'], 'superseded')
        decide(first, picks[0][0]['object_ref']['digest'], choice='confirm', selected_take=picks[0][1]['object_ref'])
        self.assertEqual(film_until('waiting-for-owner'), ['final-requested', 'waiting-for-owner'])
        pending, = [o for o in self.store.list_objects(self.pid, kind='decision-request')
                    if o['body']['purpose'] == 'final' and o['body']['state'] == 'pending']
        self.assertEqual(pending['body']['target'], final['body']['target'])
        # The service never confirms: only the owner's decision makes the final.
        self.assertEqual(self.store.list_objects(self.pid, kind='final'), [])
        decide(pending['object_id'], pending['body']['target_hash'], choice='confirm')
        self.assertEqual(film_until('confirmed')[-1], 'confirmed')
        self.assertEqual(len(self.store.list_objects(self.pid, kind='final')), 1)
        self.assertEqual(self.agent_calls, calls)

    def run_journey(self, branch, *, auto_film=False):
        self.create_project(branch)
        script = self.draft('script', 'story.fountain', 'EXT. CHECKPOINT - DAY\n'+STORY)
        expected = self.draft('expectation', 'expectation.md', '\n'.join(EXPECTED))
        deps = [script, expected]
        if branch == 'recreation':
            source = self.upload(self.movie('original-source', mode='source'), 'original-source.mp4', 'video/mp4')
            # the reader watches the source first and the understanding cites it.
            observation = self.observe(source)
            deps.append(self.draft('source-understanding', 'source.md', {'type': 'source-understanding',
                'source': source['object_ref'], 'start_seconds': 0, 'end_seconds': 1, 'observation': observation,
                'observed_facts': [STORY], 'uncertain_interpretations': ['No offscreen motive inferred.'],
                'adaptation_scope': 'Preserve crossing and continuation; new generated performances.'}))
        scene = self.draft('scene', 'scene.md', 'S02 · EXT · ROAD · DAY\n'+STORY+'\n## GEO SPATIAL LAYOUT\n'
            'Camera ALWAYS stays south, NEVER crosses. East is frame-right. Red marker remains fixed.\n## ACTIVE REFERENCES\n@loc_lane @bluecart\n'
            '## Coverage draft\nS02-010A,8s: the cart approaches and crosses the marker.\nS02-020A,8s: the cart continues past the marker.\n', deps)
        image_draft = self.draft('asset', 'assets/probe.json', {'type': 'asset', 'role': 'visual', 'tag': '@bluecart',
            'definition': {'recipe': 'base-portrait', 'description': 'Blue rigid delivery cart with four wheels.', 'visual_treatment': 'project-animation',
                           'playbook_version': playbook.version()}}, [scene])
        image_candidate = self.prepare(image_draft, 'image')
        image = self.generate(image_candidate, self.png('blue', generated=True), 'image/png')
        cart = self.draft('asset', 'assets/cart.json', {'type': 'asset', 'role': 'visual', 'tag': '@bluecart', 'definition': 'A rigid blue cart.', 'media_refs': [image['object_ref']]})
        marker_image = self.upload(self.png('red'), 'marker.png', 'image/png')
        world = self.draft('asset', 'assets/world.json', {'type': 'asset', 'role': 'world', 'tag': '@loc_lane', 'definition': 'Straight road, red stationary checkpoint.', 'media_refs': [marker_image['object_ref']]})
        state = self.draft('asset', 'assets/state.json', {'type': 'asset', 'role': 'state', 'tag': '@bluecart',
            'base_identity': cart['object_ref'], 'definition': {'recipe': 'state-variant', 'description': 'Same cart after crossing.',
                'visual_treatment': 'project-animation', 'base': image['object_ref'], 'change': 'Only dust on wheels.', 'preserve': 'Rigid body and blue identity.',
                'playbook_version': playbook.version()},
            'media_refs': [image['object_ref']]})
        self.prepare(state, 'image-edit', ok=False)
        look = self.draft('asset', 'assets/look.json', {'type': 'asset', 'role': 'look', 'tag': '@look',
            'definition': {'visual_treatment': 'project-animation', 'description': 'Flat colored teaching illustration with clear silhouettes.'}})
        selection = self.draft('asset', 'assets/selection.json', {'type': 'asset-selection', 'target': scene['object_ref'],
            'selected': {'world': world['object_ref'], 'visual': cart['object_ref'], 'state': state['object_ref'], 'look': look['object_ref']}})
        shots = []
        for n in (1, 2):
            card = self.c.card(n)
            card['The material']['the action in one to three sentences'] = EXPECTED[n-1]
            card['Direction']['expected visible performance'] = EXPECTED[n-1]
            shots.append(self.draft('shot', f'shots/{n}.json', card, [scene]))
        unqualified = self.prepare(shots[0], 'shot', [selection])
        provisional = self.command('candidate-inspect', {'candidate': unqualified['object_ref']})
        self.assertTrue(provisional['mechanical_pass'])
        # no AI review or asset qualification sits between preparing and shooting.
        candidates = [self.prepare(shot, 'shot', [selection]) for shot in shots]
        good, bad, fixed = self.movie('cross', generated=True), self.movie('wrong', mode='wrong', generated=True), self.movie('continue', mode='continue', generated=True)
        for candidate, raw in zip(candidates, [good, None], strict=True):
            request = self.object(candidate)['body']['request']
            self.responses[request['params']['prompt']] = (request, raw, 'video/mp4')
            for reference in request['references']:
                media = self.store.get_object(self.pid, reference['object_ref']['object_id'])
                self.assertTrue(media['body']['media_type'].startswith('image/'))
        batch = self.command('create-batch', {'idempotency_key': self.key(), 'expected_revision': 1,
            'candidate_ids': [c['object_ref']['object_id'] for c in candidates]})
        self.worker(); self.worker()
        status = self.command('batch', extra=[batch['object_ref']['object_id']])
        self.assertEqual(status['status'], 'partial')
        success = next(c for c in status['children'] if c['state'] == 'succeeded')
        self.command('batch-select', {'idempotency_key': self.key(), 'expected_revision': 1,
            'shot': shots[0]['object_ref'], 'take': success['result'], 'rationale': 'Preserve the successful batch child.'}, extra=[batch['object_ref']['object_id']])
        first_take = {'object_ref': success['result']}
        self.observe(first_take)
        takes = [first_take, self.generate(candidates[1], bad)]
        self.assertLess(self.c.x_position(self.object(takes[0]), 0), 0.5)
        self.assertGreater(self.c.x_position(self.object(takes[0]), 0.45), 0.5)
        self.assertLess(self.c.x_position(self.object(takes[1]), 0.2), 0.5)
        self.observe(takes[1])
        # The owner (not an AI reviewer) says what is wrong; the agent changes the smallest section.
        self.command('feedback', {'idempotency_key': self.key(), 'expected_revision': 1, 'target': takes[1]['object_ref'],
            'playback_seconds': 0.1, 'text': 'The cart returned behind the checkpoint; it must remain beyond it.'})
        patch = self.command('patch', {'idempotency_key': self.key(), 'expected_revision': 1, 'target': shots[1]['object_ref'],
            'creative_path': ['content', 'Direction', 'end state'], 'value': 'Cart already east of marker; continue farther east.', 'reason': EXPECTED[1]})
        revised = patch['artifact']
        repaired = self.prepare(revised, 'shot', [selection])
        returned = self.generate(repaired, fixed)
        self.assertGreater(self.c.x_position(self.object(returned), 0.1), 0.5)
        self.observe(returned)
        self.command('select-take', {'idempotency_key': self.key(), 'expected_revision': revised['object_ref']['revision'], 'shot': revised['object_ref'], 'take': returned['object_ref'], 'rationale': 'Returned take preserves the crossing.'})
        if auto_film:
            return self.owner_picks_become_the_film([(shots[0], first_take), (revised, returned)])
        cut = self.command('cut', {'idempotency_key': self.key(), 'expected_revision': 1,
            'segments': [{'take': takes[0]['object_ref'], 'start_seconds': 0, 'end_seconds': 0.5},
                         {'take': returned['object_ref'], 'start_seconds': 0, 'end_seconds': 0.5}], 'intent': STORY})
        job = self.command('render-cut', {'idempotency_key': self.key(), 'expected_revision': 1, 'cut': cut['object_ref']})
        self.assertEqual(self.worker()[0]['state'], 'succeeded')
        rendered = self.store.get_object(self.pid, self.object(job)['object_id'])['body']['result']
        raw = self.c.media.read(self.pid, rendered['object_id'])
        rendered_object = self.store.get_object(self.pid, rendered['object_id'])
        self.assertGreater(self.c.x_position(rendered_object, 0.45), 0.5)
        self.assertGreater(self.c.x_position(rendered_object, 0.65), 0.5)
        finished = self.upload(raw, 'finished.mp4', 'video/mp4', source_cut=cut['object_ref'])
        # a final needs no finishing records.
        self.observe(finished)
        pending = self.command('request-decision', {'idempotency_key': self.key(), 'expected_revision': 1, 'target': finished['object_ref'], 'purpose': 'final', 'rationale': 'Watch this exact synthetic final.'})
        with TestClient(self.f.app, base_url='https://craft.example') as human:
            session = human.post('/v1/session/access', json={}, headers={'Origin': 'https://craft.example', 'cf-access-jwt-assertion': owner_jwt()}).json()
            accepted = human.post('/v1/projects/'+self.pid+'/human-decisions', headers={'Origin': 'https://craft.example'}, json={
                'idempotency_key': self.key(), 'request_id': pending['object_ref']['object_id'], 'target_hash': finished['object_ref']['digest'],
                'choice': 'confirm', 'csrf_token': session['csrf_token']})
            self.assertEqual(accepted.status_code, 200, accepted.text)
            playback = human.get('/v1/projects/'+self.pid+'/media/'+finished['object_ref']['object_id']+'?revision=1', headers={'Range': 'bytes=0-31'})
            self.assertEqual(playback.status_code, 206); self.assertEqual(playback.content, raw[:32])
        copy = self.command('reference', {'target': finished['object_ref'], 'seconds': 0.75})
        self.fresh_process(copy)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='final')), 1)
        self.assertEqual(len(self.command('project')['confirmed_finals']), 1)

    def fresh_process(self, copied):
        fixture = self.f
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                response = fixture.client.post(self.path, content=self.rfile.read(int(self.headers['Content-Length'])),
                    headers={'Authorization': self.headers['Authorization'], 'Content-Type': 'application/json'})
                self.send_response(response.status_code); self.end_headers(); self.wfile.write(response.content)
            def do_GET(self):
                response = fixture.client.get(self.path, headers={'Authorization': self.headers['Authorization']})
                self.send_response(response.status_code); self.end_headers(); self.wfile.write(response.content)
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            result = subprocess.run([sys.executable, '-m', 'production.cli', 'resolve', '--input', '-'],
                input=json.dumps(copied), text=True, capture_output=True, timeout=20, check=False,
                env={**os.environ, 'MVGP_URL': f'http://127.0.0.1:{server.server_port}', 'MVGP_TOKEN': self.f.token})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(copied['object_ref']['object_id'], result.stdout)
        finally: server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_original_full_agent_journey(self): self.run_journey('original')
    def test_original_owner_picks_become_the_film(self): self.run_journey('original', auto_film=True)
    def test_recreation_full_agent_journey(self): self.run_journey('recreation')


if __name__ == '__main__': unittest.main()
