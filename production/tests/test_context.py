"""A fresh session sees exact scene evidence, not hidden writer chat history."""
import json
import unittest

from production.context import ContextService
from production.contracts import DomainError
from production.tests import test_workflow as fixtures


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WorkflowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        f = self.fixture
        self.store, self.actor = f.store, f.actor
        self.context = ContextService(f.store, f.auth, f.flow)
        self.scene = f.obj('scene', 'Cook reaches across the table. ENDING: recipient takes the bowl. GEO: door left.')
        self.script = f.obj('script', 'The complete scene, including its final silent reaction.')
        self.shot = f.obj('shot', {'Direction': {'expected visible performance': 'The bowl changes hands.'}}, [self.scene, self.script])
        self.method = f.method(self.shot)

    def get(self, **kwargs):
        return self.context.get(self.actor, 'project_1', target=self.fixture.ref(self.shot),
                                task='shot', method=self.fixture.ref(self.method), **kwargs)

    def test_fresh_session_gets_complete_scene_and_ending(self):
        result = self.get()
        self.assertIn('ENDING: recipient', json.dumps(result['creative']['scenes']))
        self.assertIn('final silent reaction', json.dumps(result['creative']['scripts']))
        self.assertIn('bowl changes hands', json.dumps(result['creative']['expectations']))
        self.assertEqual(result['branch'], 'original')
        self.assertFalse(result['artistic_acceptance'])
        self.assertEqual(result['method']['method_id'], 'video')
        self.assertIn('prepare', result['progression']['allowed'])
        self.assertEqual(result['consumption']['paid_calls'], 0)

    def test_neighbors_and_prior_failures_are_explicit_data(self):
        neighbor = self.fixture.obj('shot', {'Direction': {'expected visible performance': 'He reacts.'}}, [self.scene])
        failure = self.fixture.obj('job', {'state': 'failed'}, [self.shot])
        result = self.get()
        self.assertIn(neighbor['object_id'], str(result['neighbors']))
        self.assertIn(failure['object_id'], str(result['prior_results']))
        self.assertFalse(result['neighbors']['cut_order_known'])

    def test_the_owners_desk_note_reaches_the_agent(self):

        shot = self.fixture.ref(self.shot).model_dump()
        note = self.store.create_object('project_1', 'owner-note', {'target': shot, 'take': None, 'text': '她应该先看门再开口',
                                        'withdrawn': False, 'verified_human_session': True, 'dependencies': [shot]},
                                        'owner_note_service')
        result = self.get()
        self.assertIn(note['object_id'], str(result['prior_results']))
        self.assertIn('她应该先看门再开口', json.dumps(result['prior_results'], ensure_ascii=False))

    def test_standalone_stress_retains_explicit_context_without_unrelated_probes(self):
        f = self.fixture
        unrelated = f.obj('shot', {'Direction': {'expected visible performance': 'Another probe.'}}, [self.scene])
        explicit = f.obj('shot', {'Direction': {'expected visible performance': 'Required partner position.'}}, [self.scene])
        target = f.obj('shot', {'Direction': {'expected visible performance': 'Turn toward partner.'}},
                       [self.scene, self.script, explicit])
        feedback = f.obj('feedback', {'text': 'Keep the partner visible.'}, [target])
        result = self.context.get(self.actor, 'project_1', target=f.ref(target), task='stress')
        self.assertEqual(result['neighbors']['shots'], [])
        self.assertEqual(result['neighbors']['scope'], 'explicit_dependencies')
        self.assertIn(explicit['object_id'], str(result['creative']['shots']))
        self.assertNotIn(unrelated['object_id'], str(result['creative']['shots']))
        self.assertIn('final silent reaction', str(result['creative']['scripts']))
        self.assertIn(feedback['object_id'], str(result['prior_results']))

    def test_failed_jobs_and_feedback_follow_transitive_candidate_lineage(self):
        candidate = self.fixture.obj('candidate', {}, [self.shot])
        job = self.fixture.obj('job', {'state':'failed'}, [candidate])
        feedback = self.fixture.obj('feedback', {'text':'The offered bowl stayed in the wrong hand.'}, [job])
        unrelated = self.fixture.obj('job', {'state':'failed'})
        result = self.get()
        ids = {item['object_id'] for item in result['prior_results']}
        self.assertTrue({candidate['object_id'],job['object_id'],feedback['object_id']} <= ids)
        self.assertNotIn(unrelated['object_id'], ids)

    def test_feedback_on_worker_media_and_repair_lineage_remain_reachable(self):
        f = self.fixture
        candidate = f.obj('candidate', {}, [self.shot])
        media = self.store.create_object('project_1', 'media', {
            'sha256': 'a' * 64, 'media_type': 'video/mp4',
            'dependencies': [f.ref(candidate).model_dump()],
        }, 'worker_service')
        feedback = f.obj('feedback', {'text': 'The rider never passes the wagon.'}, [media])
        repair = f.obj('repair-lineage', {'status': 'unresolved'}, [feedback])
        unrelated = self.store.create_object('project_1', 'media', {
            'sha256': 'b' * 64, 'media_type': 'video/mp4', 'dependencies': [],
        }, 'worker_service')
        unrelated_feedback = f.obj('feedback', {'text': 'Other upload.'}, [unrelated])
        history = {item['object_id']: item for item in self.get()['prior_results']}
        for obj in (candidate, media, feedback, repair):
            self.assertIn(obj['object_id'], history)
            self.assertEqual({key: history[obj['object_id']][key] for key in ('object_id', 'revision', 'digest')},
                             f.ref(obj).model_dump())
            if obj['kind'] not in ('candidate', 'repair-lineage'):
                self.assertEqual(history[obj['object_id']]['body'], obj['body'])
        self.assertNotIn(unrelated['object_id'], history)
        self.assertNotIn(unrelated_feedback['object_id'], history)

    def test_source_media_context_preserves_observations_without_raw_provider_response(self):
        f = self.fixture
        media = self.store.create_object('project_1', 'media', {'media_type':'video/mp4', 'sha256':'a'*64,
            'probe':{'has_video':True}, 'provenance':{'result_url':'https://cdn.example/a?signature=PRIVATE_PROVENANCE'},
            'dependencies':[]}, 'worker_service')
        observations = [{'timestamp_seconds':0.5,'source_seconds':0.5,'fact':'The rider passes the wagon.'}]
        observation = self.store.create_object('project_1', 'observation', {
            'source':f.ref(media).model_dump(), 'status':'failed', 'observations':observations,
            'answers':[{'question_index':0,'verdict':'uncertain','evidence_indices':[]}],
            'uncertainty':['Impact is occluded.'], 'consumption':{'effective_fps':None},
            'raw_response':'https://cdn.example/private.mp4?signature=PRIVATE_RESPONSE',
            'headers':{'Authorization':'PRIVATE_HEADER'},
            'dependencies':[f.ref(media).model_dump()]}, 'reader_service')
        result = self.context.get(self.actor,'project_1',target=f.ref(media))
        serialized = json.dumps(result)
        for private in ('PRIVATE_RESPONSE','PRIVATE_HEADER','PRIVATE_PROVENANCE','raw_response','provenance'):
            self.assertNotIn(private,serialized)
        item = next(x for x in result['prior_results'] if x['object_id']==observation['object_id'])
        self.assertEqual(item['body']['observations'],observations)
        self.assertEqual(item['body']['answers'][0]['verdict'],'uncertain')
        self.assertEqual(item['body']['uncertainty'],['Impact is occluded.'])
        self.assertEqual(item['digest'],observation['digest'])

    def test_shared_compiler_projection_keeps_exact_prompt_but_no_nested_context_or_lease(self):
        from production.compiler import _view as compiler_view
        prompt = 'Character reads "TOKEN=fictional" on a paper; show the exact text.'
        candidate = self.store.create_object('project_1','candidate',{
            'target':self.fixture.ref(self.shot).model_dump(), 'request':{'job_type':'seedance_2_5',
                'params':{'prompt':prompt,'resolution':'1080p','quality':'high','variant':'sunburst',
                    'provider_secret':'PRIVATE_PARAMETER'},
                'references':[{'object_ref':self.fixture.ref(self.shot).model_dump(),'sha256':'a'*64,
                    'url':'https://cdn.example/a?signature=PRIVATE_REF'}]},
            'compilation':{'assembly':{'prompt':prompt},'private':'PRIVATE_COMPILATION'},
            'context':{'prior_results':[{'raw_response':'PRIVATE_NESTED'}]},
            'dependencies':[self.fixture.ref(self.shot).model_dump()]},'compiler_service')
        job = self.store.create_object('project_1','job',{'state':'running','lease':{'token':'PRIVATE_LEASE'},
            'dependencies':[self.fixture.ref(candidate).model_dump()]},'worker_service')
        result = self.get()
        history = {item['object_id']:item for item in result['prior_results']}
        projected = history[candidate['object_id']]
        self.assertEqual(projected,compiler_view(candidate))
        self.assertEqual(projected['body']['request']['params']['prompt'],prompt)
        self.assertEqual(projected['body']['request']['params']['quality'],'high')
        self.assertEqual(projected['body']['request']['params']['variant'],'sunburst')
        self.assertEqual(projected['body']['assembled_prompt'],prompt)
        self.assertEqual(history[job['object_id']]['body']['state'],'running')
        self.assertNotIn('PRIVATE_',json.dumps(result))
        self.assertIn('ENDING: recipient',json.dumps(result['creative']))

    def test_trusted_dispatch_bridge_reaches_real_production_topology_without_private_body(self):
        f = self.fixture
        candidate = f.obj('candidate', {}, [self.shot])
        intent = self.store.create_object('project_1', 'dispatch-intent', {
            'dependencies': [f.ref(candidate).model_dump()], 'request': {'private': 'PRIVATE_WIRE'},
            'cost': {'private': 'PRIVATE_COST'}, 'origin': {'credential_id': 'PRIVATE_ORIGIN'},
        }, 'submission_service')
        job = self.store.create_object('project_1', 'job', {
            'state': 'succeeded', 'dependencies': [f.ref(intent).model_dump()],
        }, 'worker_service')
        media = self.store.create_object('project_1', 'media', {
            'sha256': 'a' * 64, 'media_type': 'video/mp4', 'dependencies': [f.ref(intent).model_dump()],
        }, 'worker_service')
        feedback = f.obj('feedback', {'text': 'Cart remained behind the marker.'}, [media])
        repair = f.obj('repair-lineage', {'reason': 'Repair the relative position.'}, [feedback])
        forged = self.store.create_object('project_1', 'dispatch-intent', {
            'dependencies': [f.ref(candidate).model_dump()],
        }, 'author')
        leaked = f.obj('feedback', {'text': 'Do not traverse this forged bridge.'}, [forged])
        result = self.get()
        history = {o['object_id']: o for o in result['prior_results']}
        for obj in (candidate, job, media, feedback, repair):
            self.assertEqual({key: history[obj['object_id']][key] for key in ('object_id', 'revision', 'digest')},
                             f.ref(obj).model_dump())
        for obj in (intent, forged, leaked):
            self.assertNotIn(obj['object_id'], history)
        self.assertNotIn('PRIVATE_', json.dumps(result))

    def test_history_bridge_is_bounded_immutable_and_not_a_result(self):
        from production.context import related_history
        f = self.fixture
        bridge = self.store.create_object('project_1', 'dispatch-intent', {
            'dependencies': [f.ref(self.shot).model_dump()],
        }, 'submission_service')
        feedback = f.obj('feedback', {'text': 'Observed discrepancy.'}, [bridge])
        objects = [feedback, bridge]
        self.assertEqual(related_history(objects, {self.shot['object_id']}, max_nodes=2), [feedback])
        with self.assertRaises(DomainError) as overflow:
            related_history(objects, {self.shot['object_id']}, max_nodes=1)
        self.assertEqual(overflow.exception.code, 'insufficient_context')
        changed = self.store.append_revision('project_1', bridge['object_id'], 1, bridge['body'], 'submission_service')
        self.assertEqual(related_history([feedback, changed], {self.shot['object_id']}), [])
        self.assertEqual(related_history(objects, {self.shot['object_id']}, kinds={'media'}), [])

    def test_historical_receipt_keeps_conclusions_without_recursive_delivery_proof(self):
        from production.context import context_body
        f = self.fixture
        evidence = [{'resource_id': 'request', 'path': '/params/prompt'}]
        verdict = {'verdict': 'fail', 'summary': 'The cart remains behind.', 'evidence': evidence}
        receipt = self.store.create_object('project_1', 'review-receipt', {
            'target': f.ref(self.shot).model_dump(), 'purpose': 'preflight', 'role': 'standards',
            'verdict': 'fail', 'structured_verdict': verdict, 'evidence': evidence,
            'issues': [{'description': 'Preserve the observed relative position.'}],
            'consumption': {'context_hash': 'a' * 64, 'read_by_model': True, 'delivery_mode': 'normalized-v1',
                'delivered_resource_ids': ['request'], 'delivered_text_coverage': [{'excluded': ['X' * 300000]}],
                'delivered_fragments': [{'private': 'PRIVATE_FRAGMENT'}], 'delivery_proof': 'PRIVATE_PROOF',
                'projection': {'manifest_hash': 'b' * 64, 'content': 'PRIVATE_PROJECTION'}},
            'dependencies': [f.ref(self.shot).model_dump()],
        }, 'review_service')
        projected = context_body(receipt)
        self.assertEqual(projected['structured_verdict'], verdict)
        self.assertEqual(projected['evidence'], evidence)
        self.assertTrue(projected['consumption']['read_by_model'])
        self.assertEqual(projected['consumption']['context_hash'], 'a' * 64)
        self.assertEqual(projected['consumption']['delivered_resource_ids'], ['request'])
        self.assertLess(len(json.dumps(projected)), 2000)
        for key in ('delivered_text_coverage', 'delivered_fragments', 'delivery_proof', 'projection'):
            self.assertNotIn(key, projected['consumption'])
        self.assertIn('delivered_text_coverage', self.store.get_object('project_1', receipt['object_id'])['body']['consumption'])
        observation = {'kind': 'observation', 'body': {'consumption': {
            'video_evidence_available': True, 'audio_evidence_available': False,
            'effective_fps': None, 'sampling_coverage': 'unknown', 'delivery_proof': 'PRIVATE_PROOF'}}}
        self.assertEqual(context_body(observation)['consumption'], {
            'video_evidence_available': True, 'audio_evidence_available': False,
            'effective_fps': None, 'sampling_coverage': 'unknown'})

    def test_machine_journals_are_opaque_but_creative_prose_is_exact(self):
        from production.context import context_body
        f = self.fixture
        dependency = f.ref(self.shot).model_dump()
        prose = 'TOKEN=fictional; https://story.example/?scene=ending; ' + 'complete ' * 26000
        for kind in ('script', 'scene', 'shot', 'asset', 'expectation', 'source-understanding', 'method', 'feedback'):
            body = {'content': prose, 'dependencies': [dependency]}
            self.assertEqual(context_body({'kind': kind, 'body': body}), body)
        for kind in ('review-context', 'review-turn', 'dispatch-intent', 'review-tool-read', 'future-service-record'):
            body = {'target': dependency, 'context_hash': 'a' * 64,
                    'request': {'messages': ['PRIVATE_WIRE' * 30000]},
                    'result': {'raw': 'PRIVATE_RESULT'}, 'authorization': 'PRIVATE_AUTH',
                    'governing': {'frozen': 'PRIVATE_NESTED'}, 'dependencies': [dependency]}
            projected = context_body({'kind': kind, 'body': body})
            self.assertFalse('PRIVATE_' in json.dumps(projected), 'Machine projection leaked private wire')
            self.assertEqual(projected['dependencies'], [dependency])
            self.assertLess(len(json.dumps(projected)), 2000)

    def test_generated_reference_ancestry_stays_complete_without_recursive_review_payloads(self):
        f = self.fixture
        def service(kind, body, author, parents):
            return self.store.create_object('project_1', kind,
                {**body, 'dependencies': [f.ref(p).model_dump() for p in parents]}, author)
        image_intent = f.obj('asset', {'role': 'visual', 'definition': 'Exact character design.'})
        image_candidate = service('candidate', {'target': f.ref(image_intent).model_dump(),
            'request': {'job_type': 'image', 'params': {'prompt': 'Preserve the chipped blue bowl.'}},
            'context': {'private': 'PRIVATE_COMPILED'}}, 'compiler_service', [image_intent])
        manifest = service('review-context', {'target': f.ref(image_candidate).model_dump(),
            'context_hash': 'a' * 64, 'evidence': {'context': {'duplicated': 'PRIVATE_MANIFEST' * 160000}},
            'governing': {'instruction': 'PRIVATE_MANIFEST_INSTRUCTION'}}, 'review_context_service', [image_candidate])
        turn = service('review-turn', {'status': 'received', 'request': {'messages': ['PRIVATE_REQUEST' * 20000]},
            'result': {'raw_response': 'PRIVATE_RESPONSE'}, 'authorization': 'PRIVATE_AUTH'},
            'review_runner_service', [manifest])
        receipt = service('review-receipt', {'target': f.ref(image_candidate).model_dump(), 'verdict': 'pass',
            'issues': [], 'structured_verdict': {'summary': 'The bowl and face remain readable.'}},
            'review_service', [image_candidate, manifest, turn])
        intent = service('dispatch-intent', {'candidate': f.ref(image_candidate).model_dump(),
            'request': {'private': 'PRIVATE_PROVIDER'}, 'origin': {'private': 'PRIVATE_CREDENTIAL'}},
            'submission_service', [image_candidate, receipt])
        image = service('media', {'sha256': 'b' * 64, 'media_type': 'image/png'}, 'worker_service', [intent])
        asset = f.obj('asset', {'role': 'visual', 'definition': 'Exact generated portrait.'}, [image])
        selection = f.obj('asset', {'type': 'asset-selection', 'selected': {'visual': f.ref(asset).model_dump()}}, [asset])
        self.shot = self.store.append_revision('project_1', self.shot['object_id'], 1,
            {**self.shot['body'], 'dependencies': [*self.shot['body']['dependencies'], f.ref(selection).model_dump()]}, 'author')
        self.method = f.method(self.shot)
        result = self.get()
        self.assertLess(len(json.dumps(result)), 2 * 1024 * 1024)
        self.assertNotIn('PRIVATE_', json.dumps(result))
        refs = result['dependencies']
        for obj in (image_intent, image_candidate, manifest, turn, receipt, intent, image, asset, selection):
            self.assertIn(f.ref(obj).model_dump(), refs)
        from production.compiler import _view as compiler_view
        graph = f.flow.pinned_graph('project_1', f.ref(self.shot))
        preparation = [compiler_view(node['object']) for node in graph['nodes']]
        self.assertLess(len(json.dumps(preparation)), 2 * 1024 * 1024)
        self.assertFalse('PRIVATE_' in json.dumps(preparation), 'Compiler ancestor projection leaked wire')
        self.assertIn('Exact generated portrait.', json.dumps(result['creative']))
        self.assertIn('The bowl and face remain readable.', json.dumps(result))
        self.assertIn('PRIVATE_MANIFEST', json.dumps(self.store.get_object('project_1', manifest['object_id'])))

    def test_actual_repair_fields_preserve_failed_take_and_bound_pickup(self):
        from production.context import context_body
        ref = self.fixture.ref(self.shot).model_dump()
        for kind, fields in (('repair', ('base', 'result', 'source_pickup', 'failed_take')),
                             ('repair-lineage', ('repair', 'source_pickup', 'failed_take', 'returned_take'))):
            body = {key: ref for key in fields}
            body.update(accepted=False, dependencies=[], raw_response='PRIVATE_WIRE')
            obj = self.store.create_object('project_1', kind, body, 'patch_service')
            projected = context_body(obj)
            for key in fields:
                self.assertEqual(projected[key], ref)
            self.assertFalse(projected['accepted'])
            self.assertNotIn('PRIVATE_WIRE', str(projected))

    def test_selected_assets_preserve_separate_voice_and_visual(self):
        visual = self.fixture.obj('asset', {'type':'asset','role':'visual','definition':'Cook in blue'})
        voice = self.fixture.obj('asset', {'type':'asset','role':'voice','definition':'Quiet warm voice'})
        selection = self.fixture.obj('asset', {'type':'asset-selection','selected': {
            'visual':self.fixture.ref(visual).model_dump(), 'voice':self.fixture.ref(voice).model_dump()}}, [visual,voice])
        self.shot = self.store.append_revision('project_1', self.shot['object_id'], 1,
            {**self.shot['body'], 'dependencies': self.shot['body']['dependencies']+[self.fixture.ref(selection).model_dump()]}, 'author')
        self.method = self.fixture.method(self.shot)
        result = self.get()
        self.assertIn('Quiet warm voice', str(result['creative']['assets']))
        self.assertIn('Cook in blue', str(result['creative']['assets']))
        self.assertEqual(len(result['creative']['selections']), 1)

    def test_missing_neighbors_do_not_get_invented(self):
        result = self.get()
        self.assertEqual(result['neighbors']['shots'], [])
        self.assertIn('neighboring_shots', result['limitations'])

    def test_a_scene_edit_neither_stales_the_card_nor_rewrites_what_it_was_written_from(self):
        # the card is not stale (its prompt is complete); the context keeps the pinned scene.
        self.store.append_revision('project_1', self.scene['object_id'], 1,
            {'content':'New ending', 'dependencies':[]}, 'author')
        result = self.get()
        self.assertFalse(result['progression']['stale'])
        self.assertIn('ENDING: recipient', str(result['creative']['scenes']))
        self.assertNotIn('New ending', str(result['creative']['scenes']))

    def test_cross_project_access_is_denied_before_context(self):
        f=self.fixture
        other=f.auth.authenticate(f.auth.provision_token('outsider','viewer',[],300))
        with self.assertRaises(DomainError):
            self.context.get(other,'project_1',target=f.ref(self.shot))
        with self.assertRaises(DomainError):
            self.context.document(other,'project_1','methods')

    def test_overflow_rejects_instead_of_truncating_ending(self):
        self.context.max_bytes=100
        with self.assertRaises(DomainError) as error:
            self.get()
        self.assertEqual(error.exception.code,'insufficient_context')

    def test_complete_history_over_two_mib_preserves_scene_and_feedback(self):
        feedback_text = 'Observed failure: ' + 'x' * (2 * 1024 * 1024) + ' END: the bowl never moved.'
        feedback = self.fixture.obj('feedback', {'text': feedback_text}, [self.shot])
        result = self.get()
        retained = next(item for item in result['prior_results'] if item['object_id'] == feedback['object_id'])
        self.assertEqual(retained['body']['content']['text'], feedback_text)
        self.assertIn('ENDING: recipient takes the bowl.', str(result['creative']['scenes']))
        self.assertGreater(len(json.dumps(result).encode()), 2 * 1024 * 1024)
        self.assertLess(len(json.dumps(result).encode()), 4 * 1024 * 1024)
        self.assertFalse(result['artistic_acceptance'])

    def test_default_context_still_rejects_history_above_four_mib(self):
        self.fixture.obj('feedback', {'text': 'x' * (4 * 1024 * 1024)}, [self.shot])
        with self.assertRaises(DomainError) as error:
            self.get()
        self.assertEqual(error.exception.code, 'insufficient_context')

    def test_documents_are_pinned_and_never_arbitrary_paths(self):
        result=self.context.document(self.actor,'project_1','methods')
        self.assertIn('video',result['text'])
        with self.assertRaises(DomainError):
            self.context.document(self.actor,'project_1','/etc/passwd')
        result=self.context.methods(self.actor,'project_1')
        self.assertIn('video',result['methods'])
        self.assertTrue(result['methods']['video']['platform_enabled'])

    def test_new_session_can_discover_existing_method_choices(self):
        result = self.context.get(self.actor, 'project_1', target=self.fixture.ref(self.shot), task='shot')
        self.assertIn(self.method['object_id'], str(result['available_method_selections']))
        self.assertEqual(result['whole_scene_completeness'], 'unverified')
        self.assertIn('method-selection', result['progression']['missing'])

    def test_image_context_does_not_invent_film_prerequisites(self):
        asset=self.fixture.obj('asset',{'type':'asset','role':'look','definition':'Warm drawn animation'})
        method=self.fixture.method(asset,'image')
        result=self.context.get(self.actor,'project_1',target=self.fixture.ref(asset),task='image',method=self.fixture.ref(method))
        self.assertEqual(result['creative']['scenes'],[])
        self.assertIn('prepare',result['progression']['allowed'])
        self.assertNotIn('whole-scene-intent',result['progression']['missing'])


if __name__=='__main__':
    unittest.main()
