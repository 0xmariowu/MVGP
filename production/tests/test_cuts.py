"""Actual local rendering preserves explicit trims, sound and exact source lineage."""
import array
import copy
import json
import subprocess
import unittest
from unittest.mock import patch

from production.contracts import CutRequest, DomainError, RenderCutRequest, content_hash
from production.cuts import Cuts
from production.gates import Gates
from production.jobs import Jobs
from production.submissions import Submissions
from production.tests import test_compiler
from production.tests.fixtures import hf_era_document


class CutTests(unittest.TestCase):
    def setUp(self):
        self.c = test_compiler.CompilerTests()
        self.c.setUp()
        self.addCleanup(self.c.doCleanups)
        self.f, self.actor, self.pid = self.c.f, self.c.actor, 'project_1'
        self.store = self.f.store
        self.policy = hf_era_document('cut_policy')
        # Tiny explicitly published output profile keeps unit renders inexpensive.
        width, height = getattr(self, 'output_size', (64, 48))
        self.policy['output'].update(width=width, height=height, fps=24)
        self.f.config.set('cut_policy', self.policy)
        self.f.config.set('execution_policy', {'operations':{'render-cut':{'mode':'local','max_attempts':4}}})
        routes = self.f.config.section('review_routes')
        routes['profiles']['director']['limits'] = {'max_input_bytes_per_turn':33554432,'max_images_per_turn':16}
        self.f.config.set('review_routes', routes)
        target, selection, method, _ = self.c.video()
        self.shot = target
        self.candidate = self.c.compiler.prepare(self.actor,self.pid,self.c.request(target,task='stress',method=method,inputs=[selection]))
        file = self.f.root / 'source.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=s=64x48:r=24:d=2',
                        '-f','lavfi','-i','sine=frequency=440:duration=2','-c:v','libx264','-pix_fmt','yuv420p',
                        '-c:a','aac','-shortest',str(file)],check=True,capture_output=True)
        raw = self.f.media.put(self.pid,[file.read_bytes()],'video/mp4','worker_service')
        self.take = self.store.create_object(self.pid,'media',{**raw['body'],'dependencies':[self.ref(self.candidate).model_dump()]},'worker_service')
        self.service = Cuts(self.store,self.f.auth,self.f.flow,self.f.media)
        self.submissions = Submissions(self.store,self.f.auth,self.f.flow,Gates(self.store,self.f.flow,self.f.media))
        self.jobs = Jobs(self.store,self.f.auth,self.submissions,self.f.media,lease_seconds=300)
        self.worker = self.f.auth.authenticate(self.f.auth.provision_token('worker','worker',[self.pid],300))

    def ref(self, obj):
        return self.f.ref(obj)

    def request(self, **values):
        project = self.store.get_object(self.pid,self.pid)
        return CutRequest(**{'idempotency_key':'cut1','expected_revision':project['revision'],
            'segments':[{'take':self.ref(self.take),'start_seconds':0.25,'end_seconds':0.75},
                        {'take':self.ref(self.take),'start_seconds':1.0,'end_seconds':1.5}],
            'sound_inputs':[],'intent':'See the same action continue across the cut.',**values})

    def dispatch(self, cut, key='render1'):
        job = self.submissions.render_cut(self.actor,self.pid,RenderCutRequest(idempotency_key=key,
            expected_revision=cut['revision'],cut=self.ref(cut)))
        claim = self.jobs.claim(self.worker,self.pid,job['object_id'])
        dispatch = self.jobs.begin_dispatch(self.worker,self.pid,self.ref(claim['job']),claim['fence'])
        return dispatch['job'],claim['fence']

    def render(self, cut):
        job,fence = self.dispatch(cut)
        return self.service.render(self.worker,self.pid,self.ref(cut),jobs=self.jobs,job_ref=self.ref(job),fence=fence)

    def test_cut_preserves_each_source_history_scope_and_selected_media(self):
        from production.contracts import content_hash
        extra = self.store.create_object(self.pid, 'expectation', {'content': 'Continue after the crossing.'}, 'author')
        body = copy.deepcopy(self.candidate['body'])
        body['dependencies'].append(self.ref(extra).model_dump())
        body['context']['history_roots'].append(self.ref(extra).model_dump())
        body['context']['context_hash'] = content_hash({k:v for k,v in body['context'].items() if k != 'context_hash'})
        body['context_hash'] = body['context']['context_hash']
        second = self.store.create_object(self.pid, 'candidate', body, 'compiler_service')
        take = self.store.create_object(self.pid, 'media', {**self.take['body'],
            'dependencies': [self.ref(second).model_dump()]}, 'worker_service')
        request = self.request(segments=[{'take': self.ref(self.take), 'start_seconds': 0, 'end_seconds': 0.5},
                                        {'take': self.ref(take), 'start_seconds': 0.5, 'end_seconds': 1}])
        cut = self.service.create(self.actor, self.pid, request)
        scope = cut['body']['context']['history_roots']
        for reference in [self.ref(extra).model_dump(), self.ref(self.take).model_dump(), self.ref(take).model_dump(),
                          *self.candidate['body']['context']['history_roots']]:
            self.assertIn(reference, scope)

    def test_generated_reference_ancestor_does_not_replace_take_producer(self):
        image_target = self.f.draft(self.f.definition())
        image_candidate = self.c.compiler.prepare(self.actor,self.pid,self.c.request(image_target,key='generated-reference'))
        image = self.f.image()
        generated = self.store.create_object(self.pid,'media',{**image['body'],
            'dependencies':[self.ref(image_candidate).model_dump()]},'worker_service')
        ancestor = self.store.create_object(self.pid,'asset',{'dependencies':[self.ref(generated).model_dump()]},'author')
        body = {**self.candidate['body'], 'dependencies':[*self.candidate['body']['dependencies'],self.ref(ancestor).model_dump()]}
        producer = self.store.create_object(self.pid,'candidate',body,'compiler_service')
        intent = self.store.create_object(self.pid,'dispatch-intent',{**{k:body[k] for k in ('target','task','release_id','request')},
            'operation':'submit','candidate':self.ref(producer).model_dump(),'dependencies':[self.ref(producer).model_dump()]},'submission_service')
        self.take = self.store.create_object(self.pid,'media',{**self.take['body'],
            'dependencies':[self.ref(intent).model_dump()], 'provenance':{'intent':self.ref(intent).model_dump()}},'worker_service')
        cut = self.service.create(self.actor,self.pid,self.request())
        self.assertEqual(cut['body']['source_methods'][0]['candidate'],self.ref(producer).model_dump())
        self.assertEqual(len([n for n in self.f.flow.pinned_graph(self.pid,self.ref(self.take))['nodes']
                              if n['object']['kind']=='candidate']),2)
        self.assertFalse(cut['body']['accepted'])
        bad = self.store.create_object(self.pid,'media',{**self.take['body'], 'provenance':{}},'worker_service')
        with self.assertRaises(DomainError):
            self.service.create(self.actor,self.pid,self.request(idempotency_key='bad-origin',expected_revision=cut['revision'],
                segments=[{'take':self.ref(bad),'start_seconds':0.0,'end_seconds':0.5}]))

    def test_exact_manifest_stable_revision_and_actual_render_with_audio(self):
        cut = self.service.create(self.actor,self.pid,self.request())
        self.assertEqual(cut,self.service.create(self.actor,self.pid,self.request()))
        self.assertEqual(cut['body']['segments'][0]['start_seconds'],0.25)
        self.assertEqual(cut['body']['duration_seconds'],1.0)
        self.assertEqual(cut['body']['sound_mode'],'source-audio')
        self.assertEqual(cut['body']['method_id'],'local-cut-assembly-v1')
        self.assertTrue(cut['body']['source_methods'])
        media = self.render(cut)
        self.assertAlmostEqual(media['body']['probe']['duration'],1.0,delta=0.084)
        self.assertTrue(media['body']['probe']['has_audio'])
        self.assertEqual(media['body']['source_cut'],self.ref(cut).model_dump())
        self.assertFalse(media['body']['accepted'])
        self.assertEqual(self.store.list_objects(self.pid,kind='job')[0]['body']['state'],'succeeded')
        revised = self.service.create(self.actor,self.pid,self.request(idempotency_key='cut2',expected_revision=1,intent='A revised order.'))
        self.assertEqual(revised['object_id'],cut['object_id'])
        self.assertEqual(revised['revision'],2)
        with self.assertRaises(DomainError):
            self.service.create(self.actor,self.pid,self.request(idempotency_key='stale',expected_revision=1))

    def test_real_1080p_lossless_intermediates_keep_video_sound_and_finite_bounds(self):
        self._assert_real_render_bounds((1920, 1080))

    def test_real_720p_lossless_intermediates_keep_video_sound_and_finite_bounds(self):
        self._assert_real_render_bounds((1280, 720))

    def _assert_real_render_bounds(self, size):
        full = CutTests()
        full.output_size = size
        full.setUp()
        self.addCleanup(full.doCleanups)
        cut = full.service.create(full.actor, full.pid, full.request())
        self.assertEqual((cut['body']['output']['width'], cut['body']['output']['height']), size)
        with patch('production.cuts.subprocess.run', wraps=subprocess.run) as execute:
            media = full.render(cut)
        commands = [call.args[0] for call in execute.call_args_list if '-max_alloc' in call.args[0]]
        self.assertTrue(commands)
        lossless = [command for command in commands if 'ffv1' in command]
        ordinary = [command for command in commands if 'ffv1' not in command]
        self.assertTrue(lossless)
        self.assertTrue(ordinary)
        self.assertTrue(all(306909184 <= int(command[command.index('-max_alloc')+1]) <= 536870912
                            for command in lossless))
        self.assertTrue(all(0 < int(command[command.index('-max_alloc')+1]) <= 134217728
                            for command in ordinary))
        self.assertTrue(all(call.kwargs.get('timeout', 0) > 0 for call in execute.call_args_list))
        path = full.f.media.path_for(full.pid, media['object_id'])
        probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams',
                           '-show_format', '-of', 'json', str(path)], timeout=15))
        video = next(stream for stream in probe['streams'] if stream['codec_type'] == 'video')
        sound = next(stream for stream in probe['streams'] if stream['codec_type'] == 'audio')
        self.assertEqual((video['width'], video['height']), size)
        self.assertEqual((video['codec_name'], video['pix_fmt'], video['r_frame_rate']), ('h264', 'yuv420p', '24/1'))
        self.assertEqual(int(video['nb_frames']), 24)
        self.assertEqual((sound['codec_name'], int(sound['sample_rate'])), ('aac', 48000))
        self.assertAlmostEqual(float(probe['format']['duration']), 1.0, delta=1/24)
        pcm = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path), '-vn', '-t', '0.1',
                                      '-ac', '1', '-f', 's16le', 'pipe:1'], timeout=15)
        self.assertGreater(max(abs(sample) for sample in array.array('h', pcm)), 100)
        self.assertFalse(media['body']['accepted'])
        self.assertEqual(media['body']['source_cut'], full.ref(cut).model_dump())
        self.assertEqual(full.store.list_objects(full.pid, kind='job')[0]['body']['state'], 'succeeded')

    def test_invalid_trim_sound_refuse_but_historical_take_can_form_rough_cut(self):
        for request in [self.request(segments=[{'take':self.ref(self.take),'start_seconds':1.0,'end_seconds':3.0}]),
                        self.request(sound_inputs=[self.ref(self.take),self.ref(self.take)])]:
            with self.assertRaises(DomainError):
                self.service.create(self.actor,self.pid,request)
        self.assertEqual(self.store.list_objects(self.pid,kind='cut'),[])
        self.store.append_revision(self.pid,self.shot['object_id'],1,self.shot['body'],'author')
        rough = self.service.create(self.actor,self.pid,self.request())
        self.assertFalse(rough['body']['accepted'])
        self.assertEqual(rough['body']['segments'][0]['take'], self.ref(self.take).model_dump())

    def test_a_new_rough_cut_preserves_exact_footage_and_source_labels(self):
        previous = self.candidate['body']['release_id']
        self.c.activate()
        cut = self.service.create(self.actor,self.pid,self.request())
        self.assertEqual(cut['body']['release_id'], self.f.flow.config.label)
        self.assertEqual(cut['body']['source_methods'][0]['source_release_id'], previous)
        self.assertEqual(cut['body']['segments'][0]['take'], self.ref(self.take).model_dump())
        self.assertFalse(cut['body']['accepted'])

    def test_imported_media_can_form_rough_cut_without_fabricated_method(self):
        body = {**self.take['body'], 'dependencies':[], 'import_status':'imported-unverified'}
        imported = self.store.create_object(self.pid,'media',body,'importer_service')
        cut = self.service.create(self.actor,self.pid,self.request(segments=[{
            'take':self.ref(imported),'start_seconds':0.0,'end_seconds':0.5}]))
        self.assertTrue(cut['body']['imported_unverified'])
        self.assertIsNone(cut['body']['source_methods'][0]['method'])
        self.render(cut)

    def test_stored_cut_keeps_its_policy_hash_so_it_still_renders(self):
        # (review M-6): finishing is no longer used, but the cut policy keeps its
        # 'finishing' field, so a cut stored earlier still matches content_hash(policy) and renders.
        cut = self.service.create(self.actor,self.pid,self.request())
        policy = self.service.policy()
        self.assertIn('finishing', policy)
        self.assertEqual(cut['body']['policy_hash'], content_hash(policy))
        self.render(cut)

    def test_explicit_replacement_sound_is_rendered(self):
        cut = self.service.create(self.actor,self.pid,self.request(sound_inputs=[self.ref(self.take)]))
        self.assertEqual(cut['body']['sound_mode'],'whole-cut-replacement')
        self.assertTrue(self.render(cut)['body']['probe']['has_audio'])

    def test_fractional_trims_keep_every_shot_and_independent_sound_timing(self):
        colors = [(255,0,0), (0,255,0), (255,255,0), (0,255,255), (255,0,255),
                  (255,255,255), (255,128,0), (128,0,255), (128,128,128), (0,0,255)]
        segments = []
        for index, rgb in enumerate(colors):
            path = self.f.root / f'color{index}.mp4'
            args = ['ffmpeg','-v','error','-f','lavfi','-i',
                    'color=c=0x'+''.join(f'{c:02x}' for c in rgb)+':s=64x48:r=24:d=0.5']
            if index % 2 == 0:
                args += ['-f','lavfi','-i','sine=frequency=440:duration=0.5','-c:a','aac']
            subprocess.run([*args,'-c:v','libx264','-pix_fmt','yuv420p',str(path)],
                           check=True,capture_output=True,timeout=15)
            raw = self.f.media.put(self.pid,[path.read_bytes()],'video/mp4','worker_service')
            take = self.store.create_object(self.pid,'media',{**raw['body'],
                'dependencies':[self.ref(self.candidate).model_dump()]},'worker_service')
            segments.append({'take':self.ref(take),'start_seconds':0.05,'end_seconds':0.16})
        cut = self.service.create(self.actor,self.pid,self.request(segments=segments))
        self.assertEqual(cut['body']['segments'][-1]['output_frame_range'],[24,26])
        self.assertEqual(cut['body']['segments'][-1]['output_sample_range'],[47520,52800])
        media = self.render(cut)
        self.assertAlmostEqual(media['body']['probe']['duration'],1.1,delta=1/24)
        path = self.f.media.path_for(self.pid,media['object_id'],revision=media['revision'])
        raw_frames = subprocess.check_output(['ffmpeg','-v','error','-i',str(path),
            '-f','rawvideo','-pix_fmt','rgb24','pipe:1'],timeout=15)
        seen: list[int] = []
        for offset in range(0,len(raw_frames),64*48*3):
            pixel = raw_frames[offset:offset+3]
            index = min(range(len(colors)),key=lambda i:sum((pixel[c]-colors[i][c])**2 for c in range(3)))
            if not seen or seen[-1] != index:
                seen.append(index)
        self.assertEqual(seen,list(range(10)), 'Every declared shot, including the final blue shot, must survive')
        samples = array.array('f',subprocess.check_output(['ffmpeg','-v','error','-i',str(path),
            '-map','0:a:0','-ac','1','-ar','48000','-f','f32le','pipe:1'],timeout=15))
        for index in range(10):
            middle = round((index*0.11+0.055)*48000)
            window = samples[middle-240:middle+240]
            energy = sum(x*x for x in window)/len(window)
            with self.subTest(segment=index):
                if index % 2 == 0:
                    self.assertGreater(energy,0.001)
                else:
                    self.assertLess(energy,0.00002)

    def test_replacement_sound_obeys_per_input_byte_bound(self):
        path = self.f.root/'large-sound.wav'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=duration=2',str(path)],
                       check=True,capture_output=True,timeout=15)
        sound = self.f.media.put(self.pid,[path.read_bytes()],'audio/wav','uploader')
        policy = copy.deepcopy(self.policy)
        policy['limits']['max_input_bytes'] = self.take['body']['size']+1
        self.assertGreater(sound['body']['size'],policy['limits']['max_input_bytes'])
        with patch.object(self.service,'policy',return_value=policy),self.assertRaises(DomainError):
            self.service.create(self.actor,self.pid,self.request(sound_inputs=[self.ref(sound)]))

    def test_zero_frame_segment_is_rejected_instead_of_dropped(self):
        with self.assertRaisesRegex(DomainError,'no frame'):
            self.service.create(self.actor,self.pid,self.request(segments=[{
                'take':self.ref(self.take),'start_seconds':0.0,'end_seconds':0.001}]))
        self.assertEqual(self.store.list_objects(self.pid,kind='cut'),[])

    def test_replacement_requires_real_audio_coverage_not_container_duration(self):
        path = self.f.root/'short-track.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=blue:s=64x48:r=24:d=2',
            '-f','lavfi','-i','sine=duration=0.1','-c:v','libx264','-c:a','aac',str(path)],
            check=True,capture_output=True,timeout=15)
        sound = self.f.media.put(self.pid,[path.read_bytes()],'video/mp4','uploader')
        self.assertGreaterEqual(sound['body']['probe']['duration'],1)
        cut = self.service.create(self.actor,self.pid,self.request(sound_inputs=[self.ref(sound)]))
        with self.assertRaisesRegex(DomainError,'source audio'):
            self.render(cut)
        self.assertFalse([o for o in self.store.list_objects(self.pid,kind='media') if o['author']=='cut_service'])

    def test_intermediate_byte_bound_never_publishes_a_truncated_result(self):
        policy = copy.deepcopy(self.policy)
        policy['limits']['max_intermediate_bytes'] = 64
        with patch.object(self.service,'policy',return_value=policy):
            cut = self.service.create(self.actor,self.pid,self.request())
            with self.assertRaises(DomainError):
                self.render(cut)
        self.assertFalse([o for o in self.store.list_objects(self.pid,kind='media') if o['author']=='cut_service'])

    def test_failure_and_expired_fence_never_publish_success(self):
        cut = self.service.create(self.actor,self.pid,self.request())
        job,fence = self.dispatch(cut)
        with patch('production.cuts.subprocess.run',side_effect=subprocess.TimeoutExpired('ffmpeg',1)),self.assertRaises(DomainError):
            self.service.render(self.worker,self.pid,self.ref(cut),jobs=self.jobs,job_ref=self.ref(job),fence=fence)
        self.assertFalse([o for o in self.store.list_objects(self.pid,kind='media') if o['author']=='cut_service'])
        self.jobs.clock=lambda:float('inf')
        with self.assertRaises(DomainError):
            self.service.render(self.worker,self.pid,self.ref(cut),jobs=self.jobs,job_ref=self.ref(job),fence=fence)

    def test_unsupported_fixed_policy_semantics_are_rejected(self):
        for section,key,value in [('assembly','trim_mode','implicit-tail-trim'),('output','pixel_format','yuv444p'),
                                  ('sound','default_mode','discard-source-audio')]:
            policy = copy.deepcopy(self.policy)
            policy[section][key] = value
            with patch.object(self.f.flow.config,'section',return_value=policy),self.assertRaises(DomainError):
                self.service.policy()

class CompletionCutTests(unittest.TestCase):
    """A 1080p completion is a take the film can cut, traced to its draft's card."""
    @classmethod
    def setUpClass(cls):
        from production.tests.test_submissions import FalCompletionTests
        FalCompletionTests.setUpClass()
        cls.videos = (FalCompletionTests.draft_video, FalCompletionTests.complete_video)

    def setUp(self):
        from production.tests.test_submissions import FalCompletionTests
        self.fal = FalCompletionTests()
        self.fal.draft_video, self.fal.complete_video = self.videos
        self.fal.setUp()
        self.addCleanup(self.fal.doCleanups)

    def test_the_cut_takes_the_1080p_completion_under_its_drafts_card(self):
        fal = self.fal
        completion = fal.completed(fal.takes[0])
        cuts = Cuts(fal.store, fal.f.auth, fal.f.flow, fal.f.media)
        project = fal.store.get_object(fal.pid, fal.pid)
        cut = cuts.create(fal.s.actor, fal.pid, CutRequest(idempotency_key='cut-completion', expected_revision=project['revision'],
            segments=[{'take': fal.f.ref(completion), 'start_seconds': 0.0, 'end_seconds': 4.0}], sound_inputs=[],
            intent='The picked take at 1080p.'))
        body = cut['body']
        self.assertEqual(body['segments'][0]['take']['object_id'], completion['object_id'])
        draft_candidate = fal.f.flow.media_origin(fal.pid, fal.f.ref(fal.takes[0]))['authority']
        self.assertEqual(body['source_methods'][0]['candidate']['object_id'], draft_candidate['object_id'])
