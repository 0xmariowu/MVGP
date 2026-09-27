"""Preparation oracle: released asset methods, legacy entry and atomic freshness."""
import copy
import subprocess
import tempfile
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from production.asset_methods import AssetMethods
from production.compiler import Compiler
from production.context import ContextService
from production.contracts import DomainError, PrepareRequest
from production.runtime_config import RuntimeConfig
from production.tests import test_asset_methods, writer_fixture
from production.tests.fixtures import hf_era_document

REPO = Path(__file__).resolve().parents[2]
SCENE = ('## 第一段\nS02 · INT · Test room · DAY\n\n## GEO SPATIAL LAYOUT\n'
         'Door on west wall. Camera stays south of the west-east axis.\n\n## ACTIVE REFERENCES\n@loc_demo\n')


def test_card():
    """A four-second silent shot of a box crossing a test room, on the writer's card template."""
    card = json.loads((REPO / 'production/templates/card.json').read_text())
    card['shot'] = 'S02-020A'
    card['The material'].update({
        'the location and INT/EXT with the asset that covers it': 'INT @loc_demo',
        'the time of day': 'day', 'the running time in seconds': 4,
        'the complexity — simple, medium or complex': 'simple',
        'the action in one to three sentences': 'A white test box moves from frame-left to frame-right.'})
    card['Direction'].update({
        'the goal of the shot in one line': 'Show the box crossing the empty room.',
        'The dramaturgy — what changed between the start and the end': 'The box reaches the right wall.',
        'The blocking relative to the camera': 'The box is at frame-left, two meters from the camera.',
        'end state': 'The box rests at the right wall.'})
    card['Camera'] = {'shot size': 'Wide', 'movement': 'Locked off', 'lens': '60 degree field of view', 'angle': 'Eye level'}
    card['ACTING TASK'] = {}
    return card


class CompilerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_asset_methods.AssetMethodTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.f = self.fixture
        self.actor = self.f.auth.authenticate(self.f.auth.provision_token('author', 'agent', ['project_1'], 300))
        from production.tests import writer_fixture
        writer_fixture.took_manuals(self.f.store, 'project_1', self.actor)  # images need the manuals too
        self.profiles = {}
        self.activate()

    def activate(self):
        # The services read the fixture's runtime config; documents are set on it before this.
        self.assets = AssetMethods(self.f.store, self.f.media, self.f.flow, route_profiles={'nbp': self.f.profile})
        self.context = ContextService(self.f.store, self.f.auth, self.f.flow)
        self.compiler = Compiler(self.f.store, self.f.auth, self.f.flow, self.context, self.assets,
                                 route_profiles=self.profiles)

    def request(self, target, *, task='image', method=None, inputs=(), key='prepare'):
        method = method or self.f.choice(target, task)
        return PrepareRequest(idempotency_key=key, expected_revision=target['revision'], task=task,
                              target=self.f.ref(target), method_selection=self.f.ref(method),
                              inputs=[self.f.ref(x) for x in inputs])

    def video(self, branch='original', *, controls=None, look=True, card_edit=None, fal=False, world_definition='a white test room.',
              world_video=False):
        card = test_card()
        card['Direction']['expected visible performance'] = 'The box crosses the empty room and stops against the right wall.'
        card['_production'] = {'model': 'seedance_2_5', 'aspect_ratio': '16:9', 'resolution': '1080p',
                               **writer_fixture.authored(card, store=self.f.store, pid='project_1', actor=self.actor)}
        card['_production'].update(controls or {})
        if card_edit:
            card_edit(card)
        methods = self.f.config.section('methods')
        methods['methods']['mvgp-video-v1'] = RuntimeConfig.load().require_method('mvgp-video-v1')
        self.f.config.set('methods', methods)
        self.profiles = {'sd25': {'job_type': 'seedance_2_5', 'tasks': ['shot', 'stress'],
            'aspect_ratios': ['16:9'], 'resolutions': ['1080p'], 'max_references': 9,
            'capability_role': 'video_capability', 'timing_capability_role': 'video_timing',
            'timing_modes': ['timed', 'stages', 'ordinal'], 'mode': 'omni_reference',
            'legacy_duration_route': {'backend': 'higgsfield', 'model': 'seedance_2_5', 'endpoint': 'seedance_2_5'}}}
        self.profiles['sd25']['output_contract'] = hf_era_document('video_routes')['profiles']['hf-seedance25-video-v1']['output_contract']
        documents = {
            'video_routes': {'method_routes': {'mvgp-video-v1': 'sd25'}, 'profiles': self.profiles},
            'video_capability': {'job_type': 'seedance_2_5', 'type': 'video', 'params': [
                {'name': 'prompt'}, {'name': 'duration', 'type': 'integer'}, {'name': 'image_references', 'type': 'array'},
                {'name': 'resolution', 'enum': ['1080p']}, {'name': 'aspect_ratio', 'enum': ['16:9']},
                {'name': 'mode', 'enum': ['omni_reference']},
                {'name': 'generate_audio', 'type': 'boolean', 'default': True}]},
            'video_timing': {'job_type': 'seedance_2_5', 'tasks': ['shot', 'stress'], 'timing_modes': ['timed', 'stages', 'ordinal']},
        }
        if fal:
            # the fal draft route (480p takes; the pick is completed to 1080p).
            self.profiles['sd25'].update(job_type='fal_seedance_2_5', resolutions=['480p'], mode='reference', draft=True,
                legacy_duration_route={'backend': 'fal', 'model': 'fal_seedance_2_5',
                                       'endpoint': 'bytedance/seedance-2.5/reference-to-video'})
            self.profiles['sd25']['output_contract']['minimum_pixels_by_resolution']['480p'] = 480
            documents['video_capability'] = {'job_type': 'fal_seedance_2_5', 'type': 'video',
                'endpoint': 'bytedance/seedance-2.5/reference-to-video', 'max_references': 30, 'params': [
                {'name': 'prompt', 'type': 'string'}, {'name': 'duration', 'type': 'integer'},
                {'name': 'resolution', 'type': 'string', 'enum': ['480p', '720p', '1080p']},
                {'name': 'aspect_ratio', 'type': 'string', 'enum': ['16:9']}, {'name': 'draft', 'type': 'boolean'},
                {'name': 'generate_audio', 'type': 'boolean', 'default': True}]}
            documents['video_timing'] = {**documents['video_timing'], 'job_type': 'fal_seedance_2_5'}
            card['_production'].update(model='fal_seedance_2_5', resolution='480p')
            if card_edit:
                card_edit(card)
        for role, value in documents.items():
            self.f.config.set(role, value)
        self.activate()
        project = self.f.store.get_object('project_1', 'project_1')
        self.f.store.append_revision('project_1', 'project_1', project['revision'], {**project['body'], 'branch': branch}, 'operator')
        source_deps = []
        if branch == 'recreation':
            source = self.f.store.create_object('project_1', 'media', {'media_type': 'video/mp4', 'sha256': '0' * 64}, 'source-fixture')
            understanding = self.f.store.create_object('project_1', 'source-understanding',
                {'content': 'Original shows the box moving left to right.', 'dependencies': [self.f.ref(source).model_dump()]}, 'author')
            source_deps = [self.f.ref(understanding).model_dump()]
        scene_content = SCENE + f'\n## Coverage draft\n{card["shot"].split("-", 1)[-1]},4s: The box crosses the room.\n'
        scene = self.f.store.create_object('project_1', 'scene', {'content': scene_content, 'dependencies': source_deps}, 'author')
        target = self.f.store.create_object('project_1', 'shot', {'content': card, 'dependencies': [self.f.ref(scene).model_dump()]}, 'author')
        image = self.f.image()
        if world_video:
            # a hand-placed previs clip as the location's reference (a real, probed MP4).
            folder = tempfile.TemporaryDirectory()
            self.addCleanup(folder.cleanup)
            clip = Path(folder.name) / 'previs.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=gray:s=320x180:r=24:d=2', '-c:v', 'libx264',
                            '-pix_fmt', 'yuv420p', str(clip)], check=True)
            image = self.f.media.put('project_1', [clip.read_bytes()], 'video/mp4', 'author')
        world = self.f.draft(world_definition, role='world', media_refs=[image])
        self.f.store.append_revision('project_1', world['object_id'], 1,
            {**world['body'], 'content': {**world['body']['content'], 'tag': '@loc_demo'}}, 'author')
        world = self.f.store.get_object('project_1', world['object_id'])
        chosen = {'world': self.f.ref(world).model_dump()}
        if look:
            look = self.f.draft({'visual_treatment': 'project-animation', 'description': 'Clean animation contours and restrained cel shading.'}, role='look')
            look = self.f.store.append_revision('project_1', look['object_id'], 1,
                {**look['body'], 'content': {**look['body']['content'], 'tag': '@look_animation'}}, 'author')
            chosen['look'] = self.f.ref(look).model_dump()
        selection = self.f.store.create_object('project_1', 'asset', {'content': {'type': 'asset-selection',
            'target': self.f.ref(target).model_dump(), 'selected': chosen},
            'dependencies': list(chosen.values())}, 'author')
        method = self.f.store.create_object('project_1', 'method', {'content': {'method_id': 'mvgp-video-v1',
            'target': self.f.ref(target).model_dump(), 'rationale': 'Reference-only animated continuous shot'},
            'dependencies': [self.f.ref(target).model_dump()]}, 'author')
        return target, selection, method, image

    def test_a_generated_element_puts_its_descriptor_in_the_shot_prompt_not_its_image_prompt(self):
        # an element made through apilio carries its image prompt in `description`; the shot takes `descriptor`.
        from production import playbook
        world = {'recipe': 'location-angle', 'visual_treatment': 'project-animation', 'playbook_version': playbook.version(),
                 'description': 'IMAGE PROMPT: a wide empty plate, soft studio light, no people.',
                 'descriptor': 'A white test room with a grey floor.'}
        target, selection, method, _ = self.video(look=False, fal=True, world_definition=world)
        candidate = self.compiler.prepare(self.actor, 'project_1', self.request(target, task='shot', method=method,
                                                                                inputs=[selection], key='descriptor'))
        prompt = candidate['body']['request']['params']['prompt']
        self.assertIn('A white test room with a grey floor.', prompt)
        self.assertNotIn('IMAGE PROMPT', prompt)

    def test_a_video_reference_goes_to_higgsfield_as_video_1_and_the_fal_route_refuses_it(self):
        """(owner 2026-09-27 "得接啊"): an asset carrying a video (a previs, a turnaround) is bound
        @Video1 and frozen with its video media type, so the Higgsfield adapter sends it as --video-references."""
        target, selection, method, clip = self.video(look=False, world_video=True)
        candidate = self.compiler.prepare(self.actor, 'project_1', self.request(target, task='shot', method=method,
                                                                                inputs=[selection], key='video-ref'))
        request = candidate['body']['request']
        self.assertEqual([(r['tag'], r['n'], r['media_type']) for r in request['references']], [('loc_demo', 1, 'video/mp4')])
        # The Higgsfield route numbers references HF's way (passport-rush writes <<<video_1>>> in 6,452 of 6,750).
        self.assertIn('<<<video_1>>> @loc_demo', request['params']['prompt'])
        self.assertNotIn('<<<image_1>>>', request['params']['prompt'])
        self.assertNotIn('@Video1', request['params']['prompt'])
        self.assertEqual(request['params']['mode'], 'omni_reference')
        target, selection, method, _ = self.video(look=False, fal=True, world_video=True)
        with self.assertRaises(DomainError) as refused:
            self.compiler.prepare(self.actor, 'project_1', self.request(target, task='shot', method=method, inputs=[selection],
                                                                         key='video-ref-fal'))
        self.assertEqual(refused.exception.code, 'unsupported_route')
        self.assertIn('样片模式', refused.exception.message)

    def test_a_fal_card_prepares_a_480p_draft_request_without_mode(self):
        target, selection, method, _ = self.video(look=False, fal=True)
        candidate = self.compiler.prepare(self.actor, 'project_1', self.request(target, task='shot', method=method,
                                                                                inputs=[selection], key='fal-draft'))
        request = candidate['body']['request']
        self.assertEqual(request['job_type'], 'fal_seedance_2_5')
        self.assertEqual({k: v for k, v in request['params'].items() if k != 'prompt'},
                         {'resolution': '480p', 'aspect_ratio': '16:9', 'duration': 4, 'generate_audio': True, 'draft': True})
        self.assertIn('@Image1', request['params']['prompt'])

    def test_a_card_asking_for_another_model_is_told_what_the_fal_route_takes(self):
        def hf(card):
            card['_production'].update(model='seedance_2_5', resolution='720p')
        target, selection, method, _ = self.video(look=False, fal=True, card_edit=hf)
        with self.assertRaises(DomainError) as refused:
            self.compiler.prepare(self.actor, 'project_1', self.request(target, task='shot', method=method,
                                                                        inputs=[selection], key='fal-wrong'))
        self.assertIn('fal_seedance_2_5', refused.exception.message)
        self.assertIn('480p', refused.exception.message)

    def test_craft_gaps_are_advice_and_the_shot_still_prepares(self):
        # what HF's majority writes and this prompt does not (lens, timed beats) is advice.
        def bare(card):
            card['_production']['prompt'] = 'The box slides across the empty room @loc_demo and stops at the wall.'
        target, selection, method, _ = self.video(look=False, card_edit=bare)
        candidate = self.compiler.prepare(self.actor, 'project_1', self.request(target, task='shot', method=method,
                                                                                inputs=[selection], key='craft-gaps'))
        advice = candidate['body']['compilation']['authorship']['advice']
        self.assertTrue(any('No lens stated' in a for a in advice), advice)
        self.assertTrue(any('No timed beats' in a for a in advice), advice)
        self.assertNotIn('STYLE', candidate['body']['compilation']['prompt'])

    def test_preparation_media_context_omits_private_machine_metadata(self):
        source = self.f.image()
        source = self.f.store.append_revision('project_1', source['object_id'], source['revision'],
            {**source['body'], 'raw_response': 'PRIVATE_PROVIDER_RESPONSE',
             'storage_path': '/private/provider/storage'}, 'media_service')
        target = self.f.draft(self.f.definition(), media_refs=[source])
        candidate = self.compiler.prepare(self.actor, 'project_1', self.request(target))
        context = candidate['body']['context']
        self.assertNotIn('PRIVATE_PROVIDER_RESPONSE', json.dumps(context))
        self.assertNotIn('/private/provider/storage', json.dumps(context))
        media = next(x for x in context['preparation_inputs'] if x['object_ref']['object_id'] == source['object_id'])
        self.assertEqual(media['body']['sha256'], source['body']['sha256'])
        self.assertEqual(media['object_ref'], self.f.ref(source).model_dump())

    def test_real_image_compiler_freezes_pending_candidate_and_reuses_idempotency(self):
        target = self.f.draft(self.f.definition())
        request = self.request(target)
        result = self.compiler.prepare(self.actor, 'project_1', request)
        body = result['body']
        self.assertEqual((result['kind'], result['revision'], result['author']), ('candidate', 1, 'compiler_service'))
        self.assertEqual(body['gate_status'], 'pending')
        self.assertFalse(body['accepted'])
        self.assertEqual(body['request']['job_type'], 'nano_banana_pro')
        self.assertNotIn('Asset task:', body['request']['params']['prompt'])  # LIRA prose as written
        self.assertNotIn('ACTION TIMING', body['request']['params']['prompt'])
        self.assertEqual(self.compiler.prepare(self.actor, 'project_1', request), result)
        self.assertEqual(self.f.store.list_objects('project_1', kind='job'), [])

    def test_external_preparation_replay_returns_the_same_candidate_without_compilation(self):
        from production.record_payloads import RecordPayloads
        from production.review_payloads import ReviewPayloads
        from production.tests.fixtures import Authority, Transport
        self.f.store.record_payloads = RecordPayloads(ReviewPayloads(Transport(Authority())))
        target = self.f.draft(self.f.definition())
        request = self.request(target)
        first = self.compiler.prepare(self.actor, 'project_1', request)
        with patch.object(self.assets, 'compile', side_effect=AssertionError('must not compile twice')):
            self.assertEqual(self.compiler.prepare(self.actor, 'project_1', request), first)
        self.assertEqual(len(self.f.store.list_objects('project_1', kind='candidate')), 1)

    def test_edit_uses_scoped_change_preserve_and_exact_base(self):
        image = self.f.image()
        target = self.f.draft(self.f.definition(base=self.f.ref(image).model_dump(), change='Change only the coat.',
                                               preserve='Keep the same face and pose.'), media_refs=[image])
        result = self.compiler.prepare(self.actor, 'project_1', self.request(target, task='image-edit'))
        wire = result['body']['request']
        self.assertEqual(wire['params']['prompt'], self.f.definition()['description'])  # LIRA text only
        self.assertTrue(any('not written in the prompt' in a for a in result['body']['compilation']['authorship']['advice']))
        self.assertEqual(wire['references'][0]['sha256'], image['body']['sha256'])
        self.assertFalse(result['body']['compilation']['lineage']['pixel_preservation_verified'])

    def test_permission_and_input_race_block_publication(self):
        target = self.f.draft(self.f.definition())
        request = self.request(target)
        actual = self.assets.compile
        def change(*args, **kwargs):
            result = actual(*args, **kwargs)
            self.f.store.append_revision('project_1', target['object_id'], 1, target['body'], 'author')
            return result
        with patch.object(self.assets, 'compile', side_effect=change), self.assertRaises(DomainError) as error:
            self.compiler.prepare(self.actor, 'project_1', request)
        self.assertEqual(error.exception.code, 'stale_input')
        self.assertEqual(self.f.store.list_objects('project_1', kind='candidate'), [])
        current = self.f.store.get_object('project_1', target['object_id'])
        request = self.request(current, key='revoked')
        def revoke(*args, **kwargs):
            result = actual(*args, **kwargs)
            self.f.auth.revoke(self.actor.credential_id)
            return result
        with patch.object(self.assets, 'compile', side_effect=revoke), self.assertRaises(DomainError) as error:
            self.compiler.prepare(self.actor, 'project_1', request)
        self.assertEqual(error.exception.code, 'unauthorized')
        self.assertEqual(self.f.store.list_objects('project_1', kind='candidate'), [])

    def test_selected_old_look_is_frozen_without_flattening_roots(self):
        target = self.f.draft(self.f.definition())
        image = self.f.image()
        look = self.f.draft({'visual_treatment': 'project-animation', 'description': 'Soft cel shading.'}, role='look', media_refs=[image])
        selection = self.f.store.create_object('project_1', 'asset', {'content': {
            'type': 'asset-selection', 'target': self.f.ref(target).model_dump(),
            'selected': {'look': self.f.ref(look).model_dump()}}, 'dependencies': [self.f.ref(look).model_dump()]}, 'author')
        result = self.compiler.prepare(self.actor, 'project_1', self.request(target, inputs=[selection]))
        self.f.store.append_revision('project_1', look['object_id'], 1, {**look['body'], 'alternative': True}, 'author')
        self.assertFalse(self.f.flow.pinned_graph('project_1', self.f.ref(result))['stale'])
        self.assertNotIn(look['object_id'], [x['object_id'] for x in result['body']['dependencies']])
        self.f.store.append_revision('project_1', selection['object_id'], 1, selection['body'], 'author')
        self.assertTrue(self.f.flow.pinned_graph('project_1', self.f.ref(result))['stale'])

    def test_selected_input_feedback_is_frozen_and_midcompile_feedback_invalidates(self):
        target = self.f.draft(self.f.definition())
        look = self.f.draft({'visual_treatment': 'project-animation', 'description': 'Soft cel shading.'}, role='look')
        selection = self.f.store.create_object('project_1', 'asset', {'content': {
            'type': 'asset-selection', 'target': self.f.ref(target).model_dump(),
            'selected': {'look': self.f.ref(look).model_dump()}}, 'dependencies': [self.f.ref(look).model_dump()]}, 'author')
        prior = self.f.store.create_object('project_1', 'feedback', {'content': 'Keep this palette in the references.',
            'dependencies': [self.f.ref(look).model_dump()]}, 'author')
        media = self.f.store.create_object('project_1', 'media', {'dependencies': [self.f.ref(look).model_dump()]}, 'worker_service')
        performance_note = self.f.store.create_object('project_1', 'feedback', {'content': 'This actual face is still stiff.',
            'dependencies': [self.f.ref(media).model_dump()]}, 'author')
        result = self.compiler.prepare(self.actor, 'project_1', self.request(target, inputs=[selection]))
        history = result['body']['context']['prior_results']
        self.assertIn(prior['object_id'], [item['object_id'] for item in history])
        self.assertIn(performance_note['object_id'], [item['object_id'] for item in history])
        self.assertEqual(next(item for item in history if item['object_id'] == prior['object_id'])['body'], prior['body'])
        actual = self.assets.compile
        def new_feedback(*args, **kwargs):
            compiled = actual(*args, **kwargs)
            # A reply to already relevant feedback exercises the transitive walk.
            self.f.store.create_object('project_1', 'feedback', {'content': 'Actually use softer shadows.',
                'dependencies': [self.f.ref(prior).model_dump()]}, 'author')
            return compiled
        with patch.object(self.assets, 'compile', side_effect=new_feedback), self.assertRaises(DomainError) as error:
            self.compiler.prepare(self.actor, 'project_1', self.request(target, inputs=[selection], key='changed-feedback'))
        self.assertEqual(error.exception.code, 'stale_input')
        self.assertEqual(len(self.f.store.list_objects('project_1', kind='candidate')), 1)

    def test_selected_input_production_dispatch_lineage_and_racing_feedback(self):
        target = self.f.draft(self.f.definition())
        look = self.f.draft({'visual_treatment': 'project-animation', 'description': 'Soft cel shading.'}, role='look')
        selection = self.f.store.create_object('project_1', 'asset', {'content': {
            'type': 'asset-selection', 'target': self.f.ref(target).model_dump(),
            'selected': {'look': self.f.ref(look).model_dump()}}, 'dependencies': [self.f.ref(look).model_dump()]}, 'author')
        def node(kind, parent, author, **body):
            return self.f.store.create_object('project_1', kind, {
                **body, 'dependencies': [self.f.ref(parent).model_dump()]}, author)
        previous = node('candidate', look, 'compiler_service')
        intent = node('dispatch-intent', previous, 'submission_service', cost='PRIVATE_COST', origin='PRIVATE_CREDENTIAL')
        job = node('job', intent, 'worker_service', state='succeeded')
        media = node('media', intent, 'worker_service', sha256='a' * 64, media_type='video/mp4')
        feedback = node('feedback', media, 'author', content='The actual generated face is too stiff.')
        forged = node('dispatch-intent', previous, 'author')
        rejected_note = node('feedback', forged, 'author', content='A forged branch.')
        candidate = self.compiler.prepare(self.actor, 'project_1', self.request(target, inputs=[selection]))
        context = candidate['body']['context']
        history = {item['object_id']: item for item in context['prior_results']}
        for obj in (previous, job, media, feedback):
            self.assertEqual({k: history[obj['object_id']][k] for k in ('object_id', 'revision', 'digest')},
                             self.f.ref(obj).model_dump())
        for obj in (intent, forged, rejected_note):
            self.assertNotIn(obj['object_id'], history)
        self.assertNotIn('PRIVATE_', json.dumps(context))
        actual = self.assets.compile
        def new_feedback(*args, **kwargs):
            result = actual(*args, **kwargs)
            node('feedback', media, 'author', content='The mouth also stops moving.')
            return result
        with patch.object(self.assets, 'compile', side_effect=new_feedback), self.assertRaises(DomainError) as error:
            self.compiler.prepare(self.actor, 'project_1', self.request(target, inputs=[selection], key='raced-production'))
        self.assertEqual(error.exception.code, 'stale_input')
        self.assertEqual(len(self.f.store.list_objects('project_1', kind='candidate')), 2)

    def test_historical_receipt_proof_does_not_expand_frozen_creative_context(self):
        target = self.f.draft(self.f.definition())
        verdict = {'verdict': 'fail', 'summary': 'Keep the face readable.',
                   'evidence': [{'resource_id': 'request', 'path': '/params/prompt'}]}
        receipt = self.f.store.create_object('project_1', 'review-receipt', {
            'structured_verdict': verdict, 'verdict': 'fail', 'evidence': verdict['evidence'],
            'consumption': {'context_hash': 'a' * 64, 'read_by_model': True,
                'delivered_text_coverage': [{'path': '/creative/' + str(i), 'excluded': ['/audit/' + str(i)]} for i in range(2000)]},
            'dependencies': [self.f.ref(target).model_dump()]}, 'review_service')
        candidate = self.compiler.prepare(self.actor, 'project_1', self.request(target))
        record = next(o for o in candidate['body']['context']['prior_results'] if o['object_id'] == receipt['object_id'])
        self.assertEqual(record['body']['structured_verdict'], verdict)
        self.assertEqual(record['body']['consumption'], {'context_hash': 'a' * 64, 'read_by_model': True})
        self.assertNotIn('delivered_text_coverage', json.dumps(candidate['body']['context']))
        self.assertEqual(self.f.store.get_object('project_1', receipt['object_id']), receipt)

    def test_the_writer_text_goes_out_with_only_the_allowed_additions(self):
        # (owner 2026-09-26 "最关键的一步，必须100%做到").
        from production import prompt as prompts
        target, selection, method, image = self.video()
        result = self.compiler.prepare(self.actor, 'project_1', self.request(target, task='shot', method=method, inputs=[selection]))
        body, compiled = result['body'], result['body']['compilation']
        sent, written = body['request']['params']['prompt'], compiled['writer_text']
        self.assertEqual(written, target['body']['content']['_production']['prompt'])
        self.assertEqual(prompts.apply(written, compiled['additions']), sent)
        self.assertEqual(prompts.check(written, compiled['additions'], sent,
                                       [{'n': r['n'], 'tag': r['tag']} for r in body['request']['references']],
                                       compiled['prompt_constants']), [])
        self.assertEqual([a['kind'] for a in compiled['additions']], ['bind', 'descriptor', 'look', 'no_music'])
        self.assertIn('<<<image_1>>> @loc_demo — a white test room.', sent)  # the Higgsfield route: HF's own token
        self.assertTrue(sent.endswith('STYLE: Clean animation contours and restrained cel shading.\n\nNo music.'))
        self.assertEqual(body['request']['references'][0]['object_ref']['object_id'], image['object_id'])
        self.assertEqual(body['request']['references'][0]['sha256'], image['body']['sha256'])
        self.assertEqual((body['request']['references'][0]['n'], body['request']['references'][0]['tag']), (1, 'loc_demo'))
        self.assertEqual(compiled['wire_provenance']['wire_sha256'], hashlib.sha256(sent.encode()).hexdigest())
        self.assertEqual(compiled['writer_sha256'], hashlib.sha256(written.encode()).hexdigest())
        self.assertEqual(compiled['additions_sha256'], hashlib.sha256(
            json.dumps(compiled['additions'], sort_keys=True, ensure_ascii=False).encode()).hexdigest())
        self.assertNotIn('assembly', compiled)
        self.assertFalse(body['accepted'])

    def test_the_card_names_its_world_look_and_an_old_card_is_told_to_write_the_whole_prompt(self):
        target, selection, method, _ = self.video(controls={'look': '@noir'})
        unknown = self.compiler.prepare(self.actor, 'project_1', self.request(target, task='shot', method=method, inputs=[selection], key='noir'))
        self.assertIn('@noir', ' '.join(unknown['body']['compilation']['authorship']['advice']))
        self.assertNotIn('STYLE', unknown['body']['request']['params']['prompt'])  # the card asked for a look that is not selected
        target, selection, method, _ = self.video(controls={'look': '@look_animation'})
        named = self.compiler.prepare(self.actor, 'project_1', self.request(target, task='shot', method=method, inputs=[selection], key='named'))
        self.assertIn('STYLE: Clean animation contours', named['body']['request']['params']['prompt'])
        def old(card):
            del card['_production']['prompt']
            card['_production']['writer_sections'] = [{'section': 'CAMERA', 'text': 'Locked off.'}]
        target, selection, method, _ = self.video(card_edit=old)
        with self.assertRaises(DomainError) as refused:
            self.compiler.prepare(self.actor, 'project_1', self.request(target, task='shot', method=method, inputs=[selection], key='old'))
        self.assertEqual(refused.exception.code, 'missing_prerequisite')
        self.assertIn('_production.prompt', refused.exception.message)
        self.assertIn('writer_sections', refused.exception.message)

    def test_audio_default_is_frozen_from_the_pinned_capability(self):
        for default in (True, False):
            target, selection, method, _ = self.video()
            capability = self.f.config.section('video_capability')
            audio = next((v for v in capability['params'] if v['name'] == 'generate_audio'), None)
            if audio is None:
                capability['params'].append({'name':'generate_audio', 'type':'boolean', 'default':default})
            else:
                audio['default'] = default
            self.f.config.set('video_capability', capability)
            self.activate()
            result = self.compiler.prepare(self.actor, 'project_1', self.request(target, task='shot', method=method,
                inputs=[selection], key='audio-'+str(default)))
            self.assertIs(result['body']['request']['params']['generate_audio'], default)
            self.assertIs(result['body']['compilation']['parameters']['generate_audio'], default)

    def test_a_card_outside_the_released_route_is_told_what_is_allowed(self):
        # simulated run: a 1080p card on a 720p-only route got only 'absent or incompatible'.
        target, selection, method, _ = self.video(controls={'resolution': '4k'})
        with self.assertRaises(DomainError) as refused:
            self.compiler.prepare(self.actor, 'project_1', self.request(target, task='shot', method=method, inputs=[selection], key='4k'))
        self.assertEqual(refused.exception.code, 'unsupported_route')
        self.assertIn('resolution 1080p', refused.exception.message)
        self.assertIn('asks for seedance_2_5 shot 16:9 4k', refused.exception.message)

    def test_author_cannot_override_output_policy_or_effective_audio(self):
        for override in ({'generate_audio': False}, {'output_contract': {'authority':'maker'}}):
            target, selection, method, _ = self.video(controls=override)
            with self.subTest(override=override), self.assertRaises(DomainError):
                self.compiler.prepare(self.actor, 'project_1', self.request(target, task='shot', method=method,
                    inputs=[selection], key='override-'+str(len(override))))

    def revised(self, target, selection, production):
        """A new version of the shot card, with its own selection and method, as an agent revises one."""
        body = copy.deepcopy(target['body'])
        body['content']['_production'].update(production)
        body['content']['Direction']['end state'] = 'The box rests against the right wall, turned a quarter.'
        target = self.f.store.append_revision('project_1', target['object_id'], target['revision'], body, 'author')
        chosen = copy.deepcopy(selection['body'])
        chosen['content']['target'] = self.f.ref(target).model_dump()
        selection = self.f.store.append_revision('project_1', selection['object_id'], selection['revision'], chosen, 'author')
        method = self.f.store.create_object('project_1', 'method', {'content': {'method_id': 'mvgp-video-v1',
            'target': self.f.ref(target).model_dump(), 'rationale': 'Reference-only animated continuous shot'},
            'dependencies': [self.f.ref(target).model_dump()]}, 'author')
        return target, selection, method

    def test_the_manuals_check_is_advice_for_the_image_writer_too(self):
        # (Q6: HF neither blocks nor reminds; the desk shows 手册不是最新).
        from production import playbook
        other = self.f.auth.authenticate(self.f.auth.provision_token('author', 'agent', ['project_1'], 300))
        target = self.f.draft(self.f.definition())
        unfetched = self.compiler.prepare(other, 'project_1', self.request(target, key='image-no-manuals'))
        self.assertIn('fetch the manuals first', ' '.join(unfetched['body']['compilation']['authorship']['advice']))
        stale = self.f.draft(self.f.definition(playbook_version='pb-000000000000'))
        old = self.compiler.prepare(self.actor, 'project_1', self.request(stale, key='image-old-manuals'))
        self.assertIn(playbook.version(), ' '.join(old['body']['compilation']['authorship']['advice']))
        made = self.compiler.prepare(self.actor, 'project_1', self.request(target, key='image-ok'))
        self.assertEqual(made['body']['compilation']['authorship']['playbook_version'], playbook.version())
        self.assertEqual(made['body']['compilation']['authorship']['advice'], [])

    def test_the_manuals_check_is_advice_and_only_an_empty_prompt_is_refused(self):
        # the manuals check and the version note are recorded and shown, never a refusal.
        from production import playbook
        other = self.f.auth.authenticate(self.f.auth.provision_token('author', 'agent', ['project_1'], 300))
        def advised(controls, key, *, fetch=True):
            target, selection, method, _ = self.video(controls=controls)
            actor = self.actor if fetch else other  # another credential of the same agent never took the manuals
            made = self.compiler.prepare(actor, 'project_1', self.request(target, task='shot', method=method, inputs=[selection], key=key))
            return ' '.join(made['body']['compilation']['authorship']['advice'])
        self.assertIn('fetch the manuals first', advised({}, 'no-fetch', fetch=False))
        self.assertIn(playbook.version(), advised({'playbook_version': 'pb-000000000000'}, 'old-manuals'))
        self.assertIn('change_note', advised({'change_note': ' '}, 'no-note'))
        # nothing written at all is refused (there is nothing to send).
        target, selection, method, _ = self.video(controls={'prompt': '  '})
        with self.assertRaises(DomainError) as empty:
            self.compiler.prepare(self.actor, 'project_1', self.request(target, task='shot', method=method, inputs=[selection], key='none'))
        self.assertIn('Write the whole prompt', empty.exception.message)
        # Authored: the compilation records the manuals version and the note.
        target, selection, method, _ = self.video()
        first = self.compiler.prepare(self.actor, 'project_1', self.request(target, task='shot', method=method, inputs=[selection], key='ok'))
        authorship = first['body']['compilation']['authorship']
        self.assertEqual(authorship, {'advice': [], 'playbook_version': playbook.version(), 'change_note': 'First version of this card.'})
        # a note may repeat; one is asked for only when the card text changed.
        target2, selection2, method2 = self.revised(target, selection, {})
        same = self.compiler.prepare(self.actor, 'project_1', self.request(target2, task='shot', method=method2, inputs=[selection2], key='same-note'))
        self.assertEqual(same['body']['compilation']['authorship']['advice'], [])
        target3, selection3, method3 = self.revised(target2, selection2, {'change_note': 'The box ends turned a quarter.'})
        again = self.compiler.prepare(self.actor, 'project_1', self.request(target3, task='shot', method=method3, inputs=[selection3], key='new-note'))
        self.assertEqual(again['body']['compilation']['authorship']['change_note'], 'The box ends turned a quarter.')
        # The prompt changed and no note says why: advice (never a refusal).
        prompt = target3['body']['content']['_production']['prompt'] + '\nThe box stops with a small bounce.'
        target4, selection4, method4 = self.revised(target3, selection3, {'change_note': '', 'prompt': prompt})
        advice = self.compiler.prepare(self.actor, 'project_1', self.request(target4, task='shot', method=method4, inputs=[selection4],
                                       key='changed-no-note'))['body']['compilation']['authorship']['advice']
        self.assertTrue(any('change_note' in a for a in advice), advice)
        # Only the manuals version changed (no card text): no note is asked for.
        target5, selection5, method5 = self.revised(target4, selection4, {'playbook_version': 'pb-000000000000'})
        advice = self.compiler.prepare(self.actor, 'project_1', self.request(target5, task='shot', method=method5, inputs=[selection5],
                                       key='version-only'))['body']['compilation']['authorship']['advice']
        self.assertFalse([a for a in advice if 'change_note' in a], advice)

    def test_a_card_may_leave_the_route_ratio_and_manuals_version_to_the_platform(self):
        # the method's route model and its only resolution, 16:9, the manuals this writer took.
        from production import playbook
        def bare(card):
            card['_production'] = {'prompt': card['_production']['prompt'], 'change_note': 'First.'}
        target, selection, method, _ = self.video(card_edit=bare)
        made = self.compiler.prepare(self.actor, 'project_1', self.request(target, task='shot', method=method, inputs=[selection], key='bare'))
        params = made['body']['request']['params']
        self.assertEqual((made['body']['request']['job_type'], params['resolution'], params['aspect_ratio']), ('seedance_2_5', '1080p', '16:9'))
        self.assertEqual(made['body']['compilation']['authorship']['playbook_version'], playbook.version())
        self.assertEqual(made['body']['compilation']['authorship']['advice'], [])
        other = self.f.auth.authenticate(self.f.auth.provision_token('author', 'agent', ['project_1'], 300))
        unfetched = self.compiler.prepare(other, 'project_1', self.request(target, task='shot', method=method, inputs=[selection], key='bare-2'))
        self.assertIsNone(unfetched['body']['compilation']['authorship']['playbook_version'])  # never claims manuals not taken

    def test_other_work_moving_on_during_compilation_does_not_refuse_the_card(self):
        # dry run: in a two-card shoot order the first card's method, candidate and jobs changed the
        # project while the second compiled, and the second was refused although its own inputs were unchanged.
        from production import prompt as prompts
        target, selection, method, _ = self.video()
        compile_ = prompts.build
        def busy(*args, **kwargs):
            result = compile_(*args, **kwargs)
            # Another card's shoot order picks its method mid-compile, as the first card of an order does.
            other, _, _, _ = self.video()
            self.f.store.create_object('project_1', 'method', {'content': {'method_id': 'mvgp-video-v1',
                'target': self.f.ref(other).model_dump(), 'rationale': 'Shoot order: the released video method for this card.'},
                'dependencies': [self.f.ref(other).model_dump()]}, 'author')
            return result
        with patch.object(prompts, 'build', side_effect=busy):
            candidate = self.compiler.prepare(self.actor, 'project_1', self.request(target, task='shot', method=method,
                                              inputs=[selection], key='busy-project'))
        self.assertEqual(candidate['body']['context_hash'], candidate['body']['context']['context_hash'])
        # A change to the card itself during compilation is still refused.
        target2, selection2, method2, _ = self.video()
        def edited(*args, **kwargs):
            result = compile_(*args, **kwargs)
            body = copy.deepcopy(target2['body']); body['content']['Direction']['end state'] = 'Changed mid-compile.'
            self.f.store.append_revision('project_1', target2['object_id'], target2['revision'], body, 'author')
            return result
        with patch.object(prompts, 'build', side_effect=edited), self.assertRaises(DomainError):
            self.compiler.prepare(self.actor, 'project_1', self.request(target2, task='shot', method=method2,
                                  inputs=[selection2], key='edited-card'))

    def test_recreation_source_is_context_only_not_a_generation_reference(self):
        target, selection, method, image = self.video('recreation')
        result = self.compiler.prepare(self.actor, 'project_1', self.request(target, task='shot', method=method, inputs=[selection]))
        self.assertEqual([x['object_ref']['object_id'] for x in result['body']['request']['references']], [image['object_id']])
        self.assertTrue(result['body']['context']['creative']['source_understanding'])
        self.assertNotIn('video_references', result['body']['request']['params'])

    def state_video(self, *, person=True, tags=('@cook_wet',), state_tag='@cook_wet'):
        target, selection, method, _ = self.video()
        card = copy.deepcopy(target['body']['content'])
        field = 'everyone in frame with their tags and state variants' if person else 'props and vehicles with tags'
        card['The material'][field] = list(tags)
        card['_production']['prompt'] = writer_fixture.prompt(card)
        card['Direction']['The task — what the character does to get what he wants, as a verb'] = 'Push the box to the wall.'
        target = self.f.store.append_revision('project_1', target['object_id'], target['revision'],
            {**target['body'], 'content': card}, 'author')
        selection = self.f.store.append_revision('project_1', selection['object_id'], selection['revision'],
            {**selection['body'], 'content': {**selection['body']['content'], 'target': self.f.ref(target).model_dump()}}, 'author')
        method = self.f.store.append_revision('project_1', method['object_id'], method['revision'],
            {**method['body'], 'content': {**method['body']['content'], 'target': self.f.ref(target).model_dump()},
             'dependencies': [self.f.ref(target).model_dump()]}, 'author')
        base = self.f.draft('A cook in a blue coat.', media_refs=[self.f.image('blue')])
        base = self.f.store.append_revision('project_1', base['object_id'], base['revision'],
            {**base['body'], 'content': {**base['body']['content'], 'tag': '@cook'}}, 'author')
        state = self.f.draft('The same cook soaked in rain.', role='state', media_refs=[self.f.image('green')], base_identity=base)
        state = self.f.store.append_revision('project_1', state['object_id'], state['revision'],
            {**state['body'], 'content': {**state['body']['content'], 'tag': state_tag}}, 'author')
        props = self.f.store.create_object('project_1', 'asset', {'content': {'type': 'asset-selection',
            'target': self.f.ref(target).model_dump(), 'selected': {'visual': self.f.ref(base).model_dump(), 'state': self.f.ref(state).model_dump()}},
            'dependencies': [self.f.ref(base).model_dump(), self.f.ref(state).model_dump()]}, 'author')
        return target, selection, method, props, base, state

    def test_a_state_is_its_own_element_and_the_writers_tags_pick_the_images(self):
        # HF: every element has one image and a state is its own named element (HF_CANONICAL.md §2).
        target, selection, method, props, base, state = self.state_video()
        candidate = self.compiler.prepare(self.actor, 'project_1', self.request(target, task='shot', method=method, inputs=[selection, props]))
        references = candidate['body']['request']['references']
        self.assertEqual([(r['tag'], r['role'], r['asset']) for r in references],
                         [('cook_wet', 'state', self.f.ref(state).model_dump()), ('loc_demo', 'place', references[1]['asset'])])
        target, selection, method, props, base, state = self.state_video(tags=('@cook', '@cook_wet'))
        candidate = self.compiler.prepare(self.actor, 'project_1', self.request(target, task='shot', method=method,
                                                                                inputs=[selection, props], key='both'))
        references = candidate['body']['request']['references']
        self.assertEqual([(r['n'], r['tag'], r['role']) for r in references], [(1, 'cook', 'person'), (2, 'cook_wet', 'state'), (3, 'loc_demo', 'place')])
        self.assertEqual(references[0]['asset'], self.f.ref(base).model_dump())

    def test_a_state_sharing_its_base_tag_sends_the_base_and_says_so(self):
        # Journeys before 2026-09-27 gave a state its base's tag; the shot still prepares (only integrity blocks).
        target, selection, method, props, base, _ = self.state_video(tags=('@cook',), state_tag='@cook')
        candidate = self.compiler.prepare(self.actor, 'project_1', self.request(target, task='shot', method=method, inputs=[selection, props]))
        self.assertEqual(candidate['body']['request']['references'][0]['asset'], self.f.ref(base).model_dump())
        self.assertTrue(any('@cook names selected visual, state assets' in a for a in candidate['body']['compilation']['authorship']['advice']))

    def test_a_prop_state_prepares_like_any_element(self):
        target, selection, method, props, _, state = self.state_video(person=False)
        candidate = self.compiler.prepare(self.actor, 'project_1', self.request(target, task='shot', method=method, inputs=[selection, props]))
        self.assertEqual(candidate['body']['request']['references'][0]['asset'], self.f.ref(state).model_dump())

    def test_missing_or_unversioned_video_controls_do_not_compile(self):
        target, selection, method, _ = self.video(controls={'endpoint': 'https://untrusted.example'})
        with self.assertRaises(DomainError) as error:
            self.compiler.prepare(self.actor, 'project_1', self.request(target, task='shot', method=method, inputs=[selection]))
        self.assertEqual(error.exception.code, 'missing_prerequisite')
        with self.assertRaises(DomainError):
            self.compiler.prepare(self.actor, 'project_1', self.request(target, task='shot', method=method, inputs=[]))


if __name__ == '__main__':
    unittest.main()

