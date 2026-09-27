"""Immutable dependency/lock behavior and the runtime config's methods."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from production.auth import AuthService
from production.contracts import DomainError, ObjectRef
from production.store import Store
from production.tests.fixtures import runtime_config
from production.workflow import Workflow


class WorkflowTests(unittest.TestCase):
    def test_completed_media_keeps_provenance_when_creative_inputs_change(self):
        shot = self.obj('shot', {'action': 'A waits.'})
        candidate = self.store.create_object('project_1', 'candidate',
            {'dependencies': [self.ref(shot).model_dump()]}, 'compiler_service')
        media = self.store.create_object('project_1', 'media',
            {'dependencies': [self.ref(candidate).model_dump()]}, 'worker_service')
        authored = self.obj('media', deps=[candidate])
        self.store.append_revision('project_1', shot['object_id'], 1,
            {'content': {'action': 'A leaves.'}, 'dependencies': []}, 'author')
        self.assertTrue(self.flow.pinned_graph('project_1', self.ref(candidate))['stale'])
        self.assertTrue(self.flow.pinned_graph('project_1', self.ref(authored))['stale'])
        graph = self.flow.pinned_graph('project_1', self.ref(media))
        self.assertFalse(graph['stale'])
        self.assertIn(self.ref(shot).model_dump(), [node['ref'] for node in graph['nodes']])
        self.assertFalse(self.flow.pinned_states('project_1', [self.ref(media)])[0]['stale'])
        self.store.create_object('project_1', 'invalidation',
            {'target': self.ref(shot).model_dump()}, 'workflow_service')
        self.assertTrue(self.flow.pinned_graph('project_1', self.ref(media))['stale'])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "deploy"
        self.root.mkdir()
        self.store = Store(Path(self.tmp.name) / "state.sqlite")
        self.auth = AuthService(self.store, "https://studio.example")
        methods = {mid: {"status": {"documented": True, "adopted": True, "enabled": False, "implemented": False}, "task": task, "task_aliases": aliases, "required_capabilities": ["compile"]}
                   for mid, task, aliases in (("video", "shot", ["stress"]), ("image", "image", []))}
        self.config = runtime_config(methods=methods)
        self.store.create_project("project_1", {"branch": "original", "release_id": self.config.label}, "author")
        self.actor = self.auth.authenticate(self.auth.provision_token("author", "agent", ["project_1"], 300))
        self.flow = Workflow(self.store, self.auth, self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def ref(self, obj):
        return ObjectRef(object_id=obj["object_id"], revision=obj["revision"], digest=obj["digest"])

    def obj(self, kind, content=None, deps=()):
        return self.store.create_object("project_1", kind, {"content": content or {}, "dependencies": [self.ref(o).model_dump() for o in deps]}, "author")

    def method(self, target, method="video"):
        return self.obj("method", {"method_id": method, "rationale": "Fixture scope", "target": self.ref(target).model_dump()}, [target])

    def producer(self, *, task='stress', dependencies=(), candidate_changes=None, intent_changes=None):
        target = self.obj('asset' if task == 'image' else 'shot', deps=dependencies)
        body = {'target':self.ref(target).model_dump(), 'release_id':self.config.label,
                'task':task, 'request':{'job_type':'image_model' if task == 'image' else 'video_model',
                                      'params':{'prompt':'A visible action'}, 'references':[]},
                'dependencies':[self.ref(target).model_dump()], **(candidate_changes or {})}
        candidate = self.store.create_object('project_1', 'candidate', body, 'compiler_service')
        intent = self.store.create_object('project_1', 'dispatch-intent', {**body, 'operation':'submit',
            'candidate':self.ref(candidate).model_dump(), 'dependencies':[self.ref(candidate).model_dump()],
            **(intent_changes or {})}, 'submission_service')
        return target, candidate, intent

    def produced_media(self, candidate, intent=None, *, author='worker_service', **extra):
        deps = [self.ref(intent or candidate).model_dump()]
        body = {'media_type':'video/mp4', 'dependencies':deps, 'derivative_of':None}
        if intent:
            body['provenance'] = {'intent':self.ref(intent).model_dump(),
                                  'requested_parameters':intent['body']['request']['params']}
        return self.store.create_object('project_1', 'media', {**body, **extra}, author)

    def test_batched_states_share_only_dependency_metadata(self):
        # The upstream is a script: a shot's scene is frozen context (tested below).
        source = self.obj('script', {'large_content': 'x' * 100000})
        targets = [self.obj('shot', deps=[source]) for _ in range(5)]
        refs = [self.ref(obj) for obj in targets]
        expected = [{k: self.flow.pinned_graph('project_1', ref)[k] for k in ('stale', 'reasons')} for ref in refs]
        with self.store.transaction(write=False) as db, patch.object(self.store, 'get_object', wraps=self.store.get_object) as read:
            actual = self.flow.pinned_states('project_1', refs, conn=db)
        self.assertEqual(actual, expected)
        self.assertEqual(read.call_count, 6)
        self.assertFalse(any('nodes' in state or 'object' in state for state in actual))
        self.assertIn('large_content', self.flow.pinned_graph('project_1', refs[0])['nodes'][0]['object']['body']['content'])
        self.store.append_revision('project_1', source['object_id'], 1, {'content': {}, 'dependencies': []}, 'author')
        newer = self.flow.pinned_states('project_1', refs)
        self.assertTrue(all(state['stale'] for state in newer))
        self.assertTrue(all(state['reasons'][0]['current_revision'] == 2 for state in newer))

    def test_batched_states_preserve_digest_scope_and_frozen_paths(self):
        asset = self.obj('asset', {'type': 'asset', 'role': 'visual'})
        selection = self.obj('asset', {'type': 'asset-selection', 'selected': {'visual': self.ref(asset).model_dump()}}, [asset])
        frozen = self.obj('shot', deps=[selection])
        direct = self.obj('shot', deps=[asset])
        self.store.append_revision('project_1', asset['object_id'], 1, {'content': {}, 'dependencies': []}, 'author')
        bad = self.ref(frozen).model_copy(update={'digest': 'f' * 64})
        refs = [self.ref(frozen), bad, self.ref(direct), self.ref(frozen)]
        states = self.flow.pinned_states('project_1', refs)
        self.assertFalse(states[0]['stale'])
        self.assertEqual(states[1], {'error': 'stale_input'})
        self.assertTrue(states[2]['stale'])
        self.assertEqual(states[0], states[3])
        self.assertEqual(self.flow.pinned_states('foreign_project', [refs[0]]), [{'error': 'not_found'}])
        self.store.create_object('project_1', 'invalidation', {'target': self.ref(asset).model_dump()}, 'workflow_service')
        self.assertTrue(self.flow.pinned_states('project_1', [refs[0]])[0]['stale'])

    def test_batched_states_keep_per_root_bounds_and_cache_eviction(self):
        source = self.obj('scene')
        targets = [self.obj('shot', deps=[source]) for _ in range(3)]
        refs = [self.ref(obj) for obj in targets]
        expected = self.flow.pinned_states('project_1', refs)
        with patch('production.workflow.STATE_CACHE_ENTRIES', 1):
            self.assertEqual(self.flow.pinned_states('project_1', refs), expected)
        with patch('production.workflow.STATE_CACHE_BYTES', 1):
            self.assertEqual(self.flow.pinned_states('project_1', refs), expected)
        with patch('production.workflow.MAX_GRAPH_DEPTH', 1):
            self.assertEqual(self.flow.pinned_states('project_1', refs), [{'error': 'insufficient_context'}] * 3)
        with patch('production.workflow.MAX_GRAPH_NODES', 1):
            self.assertEqual(self.flow.pinned_states('project_1', refs), [{'error': 'insufficient_context'}] * 3)
        # A malformed dependency cycle is rejected independently on each root.
        cycle = self.store.create_object('project_1', 'shot', {
            'dependencies': [ObjectRef(object_id='cycle_fixture', revision=1).model_dump()]},
            'author', object_id='cycle_fixture')
        self.assertEqual(self.flow.pinned_states('project_1', [self.ref(cycle)] * 3),
                         [{'error': 'invalid_input'}] * 3)

    def test_batched_states_keep_qualification_freezing_and_validation(self):
        asset = self.obj('asset', {'type': 'asset', 'role': 'visual'})
        ref = self.ref(asset).model_dump()
        qualification = self.store.create_object('project_1', 'qualification-set', {
            'asset': ref, 'method_id': 'mvgp-video-v1', 'dependencies': [ref]}, 'qualification_service')
        self.store.append_revision('project_1', asset['object_id'], 1, {'content': {}, 'dependencies': []}, 'author')
        expected = self.flow.pinned_graph('project_1', self.ref(qualification))
        self.assertEqual(self.flow.pinned_states('project_1', [self.ref(qualification)]),
                         [{k: expected[k] for k in ('stale', 'reasons')}])
        malformed = self.store.create_object('project_1', 'qualification-set', {
            'asset': ref, 'method_id': 'other-method', 'dependencies': [ref]}, 'qualification_service')
        self.assertEqual(self.flow.pinned_states('project_1', [self.ref(malformed)]), [{'error': 'invalid_input'}])

    def test_graph_compares_current_revision_without_decoding_current_body(self):
        source = self.obj('script', {'note': 'old'})
        target = self.obj('shot', deps=[source])
        self.store.append_revision('project_1', source['object_id'], 1,
                                   {'content': {'note': 'new'}, 'dependencies': []}, 'author')
        with self.store.transaction(write=False) as conn,\
                patch.object(self.store, 'get_object', wraps=self.store.get_object) as read:
            graph = self.flow.pinned_graph('project_1', self.ref(target), conn=conn)
        self.assertEqual(read.call_count, 2)
        self.assertTrue(all(call.kwargs.get('revision') == 1 for call in read.call_args_list))
        self.assertTrue(graph['stale'])
        self.assertEqual(graph['reasons'], [{'code': 'revision_changed',
                         'ref': self.ref(source).model_dump(), 'current_revision': 2}])
        self.assertEqual([n['object']['body'] for n in graph['nodes']], [source['body'], target['body']])

    def test_media_origin_selects_actual_video_producer_not_generated_image_ancestor(self):
        _, image_candidate, image_intent = self.producer(task='image')
        image = self.produced_media(image_candidate, image_intent, media_type='image/png')
        asset = self.obj('asset', deps=[image])
        _, candidate, intent = self.producer(dependencies=[asset])
        media = self.produced_media(candidate, intent)
        graph = self.flow.pinned_graph('project_1', self.ref(media))
        self.assertEqual(len([n for n in graph['nodes'] if n['object']['kind'] == 'candidate']), 2)
        origin = self.flow.media_origin('project_1', self.ref(media))
        self.assertEqual(origin['kind'], 'candidate')
        self.assertEqual(origin['authority'], candidate)
        self.assertEqual(origin['media'], media)
        self.assertEqual(origin['chain'], [self.ref(x).model_dump() for x in (media, intent, candidate)])
        self.assertEqual(origin['provenance']['binding'], 'dispatch-intent')
        self.assertEqual(self.flow.pinned_graph('project_1', self.ref(media)), graph)

    def test_media_origin_direct_candidate_compatibility_never_scans_ancestors(self):
        _, candidate, _ = self.producer()
        direct = self.produced_media(candidate)
        self.assertEqual(self.flow.media_origin('project_1', self.ref(direct))['authority'], candidate)
        wrapper = self.obj('asset', deps=[candidate])
        concealed = self.produced_media(candidate, dependencies=[self.ref(wrapper).model_dump()])
        with self.assertRaises(DomainError):
            self.flow.media_origin('project_1', self.ref(concealed))
        for author in ('author', 'compiler_service', 'composition_service', 'importer_service'):
            foreign = self.produced_media(candidate, author=author)
            with self.subTest(author=author), self.assertRaises(DomainError):
                self.flow.media_origin('project_1', self.ref(foreign))

    def test_media_origin_explicit_bad_provenance_never_falls_back(self):
        _, candidate, intent = self.producer()
        _, other, other_intent = self.producer()
        for extra in (
            {'provenance':None}, {'provenance':{}},
            {'provenance':{'intent':self.ref(other_intent).model_dump()}},
            {'provenance':{'intent':self.ref(intent).model_dump()}, 'dependencies':[self.ref(candidate).model_dump()]},
            {'dependencies':[self.ref(intent).model_dump(), self.ref(other).model_dump()]},
            {'dependencies':[self.ref(intent).model_dump(), self.ref(other_intent).model_dump()]},
            {'provenance':{'intent':self.ref(intent).model_dump(), 'requested_parameters':{'prompt':'different'}}},
        ):
            media = self.produced_media(candidate, intent, **extra)
            with self.subTest(extra=extra), self.assertRaises(DomainError):
                self.flow.media_origin('project_1', self.ref(media))
        multiple = self.produced_media(candidate, dependencies=[self.ref(candidate).model_dump(), self.ref(other).model_dump()])
        with self.assertRaises(DomainError):
            self.flow.media_origin('project_1', self.ref(multiple))

    def test_media_origin_intent_candidate_binding_is_exact(self):
        _, other, _ = self.producer()
        for change in ({'operation':'observe'}, {'task':'image'}, {'release_id':'release_'+'f'*64},
                       {'target':other['body']['target']}, {'request':{'params':{'prompt':'other'}}},
                       {'dependencies':[self.ref(other).model_dump()]}):
            _, candidate, intent = self.producer(intent_changes=change)
            media = self.produced_media(candidate, intent)
            with self.subTest(change=change), self.assertRaises(DomainError):
                self.flow.media_origin('project_1', self.ref(media))
        _, candidate, intent = self.producer()
        altered = self.store.append_revision('project_1', candidate['object_id'], 1, candidate['body'], 'compiler_service')
        media = self.produced_media(altered)
        with self.assertRaises(DomainError):
            self.flow.media_origin('project_1', self.ref(media))
        bad_author = self.store.create_object('project_1', 'dispatch-intent', intent['body'], 'author')
        with self.assertRaises(DomainError):
            self.flow.media_origin('project_1', self.ref(self.produced_media(candidate, bad_author)))

    def test_media_origin_cut_binding_and_declared_finishing_upload(self):
        cut = self.store.create_object('project_1', 'cut', {'dependencies':[]}, 'cut_service')
        cut_ref = self.ref(cut).model_dump()
        for author in ('cut_service', self.actor.actor_id):
            media = self.store.create_object('project_1', 'media', {'source_cut':cut_ref,
                'assembly_manifest':cut_ref, 'dependencies':[cut_ref]}, author)
            origin = self.flow.media_origin('project_1', self.ref(media))
            self.assertEqual(origin['kind'], 'cut')
            self.assertEqual(origin['authority'], cut)
            self.assertEqual(origin['provenance']['declared_upload'], author != 'cut_service')
        finishing = self.store.create_object('project_1', 'media', {
            'derivative_of':self.ref(media).model_dump(), 'dependencies':[self.ref(media).model_dump()]}, 'worker_service')
        self.assertEqual(self.flow.media_origin('project_1', self.ref(finishing), allow_derivatives=True)['authority'], cut)
        with self.assertRaises(DomainError):
            self.flow.media_origin('project_1', self.ref(finishing))
        _, candidate, intent = self.producer(dependencies=[cut])
        media = self.produced_media(candidate, intent)
        self.assertEqual(self.flow.media_origin('project_1', self.ref(media))['authority'], candidate)
        wrapper = self.obj('asset', deps=[cut])
        for body in ({'dependencies':[self.ref(wrapper).model_dump()]},
                     {'source_cut':cut_ref, 'dependencies':[self.ref(wrapper).model_dump()]},
                     {'assembly_manifest':cut_ref, 'dependencies':[cut_ref]},
                     {'source_cut':cut_ref, 'dependencies':[cut_ref], 'assembly_manifest':self.ref(wrapper).model_dump()},
                     {'source_cut':cut_ref, 'dependencies':[cut_ref,self.ref(intent).model_dump()]}):
            media = self.store.create_object('project_1', 'media', body, 'cut_service')
            with self.subTest(body=body), self.assertRaises(DomainError):
                self.flow.media_origin('project_1', self.ref(media))

    def test_media_origin_derivatives_are_explicit_service_edges_and_bounded(self):
        _, candidate, intent = self.producer()
        base = self.produced_media(candidate, intent)
        derived = self.store.create_object('project_1', 'media', {'derivative_of':self.ref(base).model_dump(),
            'dependencies':[self.ref(base).model_dump()]}, 'reader_service')
        with self.assertRaises(DomainError):
            self.flow.media_origin('project_1', self.ref(derived))
        origin = self.flow.media_origin('project_1', self.ref(derived), allow_derivatives=True)
        self.assertEqual(origin['authority'], candidate)
        self.assertEqual(origin['media'], derived)
        self.assertEqual(origin['chain'], [self.ref(x).model_dump() for x in (derived,base,intent,candidate)])
        for author, extra in (('composition_service', {}), ('author', {}), ('worker_service', {'dependencies':[]}),
                              ('worker_service', {'dependencies':[self.ref(base).model_dump(),self.ref(candidate).model_dump()]})):
            bad = self.store.create_object('project_1', 'media', {**derived['body'], **extra}, author)
            with self.subTest(author=author,extra=extra), self.assertRaises(DomainError):
                self.flow.media_origin('project_1', self.ref(bad), allow_derivatives=True)
        end = base
        for _ in range(16):
            end = self.store.create_object('project_1', 'media', {'derivative_of':self.ref(end).model_dump(),
                'dependencies':[self.ref(end).model_dump()]}, 'worker_service')
        with self.assertRaises(DomainError) as error:
            self.flow.media_origin('project_1', self.ref(end), allow_derivatives=True)
        self.assertEqual(error.exception.code, 'insufficient_context')

    def test_media_origin_missing_foreign_digest_and_cycle_refs_fail(self):
        _, candidate, intent = self.producer()
        media = self.produced_media(candidate, intent)
        for reference in (ObjectRef(object_id=media['object_id'], revision=1),
                          self.ref(media).model_copy(update={'digest':'0'*64}),
                          ObjectRef(object_id='absent',revision=1,digest='0'*64)):
            with self.subTest(reference=reference), self.assertRaises(DomainError):
                self.flow.media_origin('project_1', reference)
        self.store.create_project('other_project', {}, 'author')
        other = self.store.create_object('other_project', 'candidate', candidate['body'], 'compiler_service')
        with self.assertRaises(DomainError):
            self.flow.media_origin('project_1', self.ref(self.produced_media(other)))
        # A self-referencing immutable record cannot honestly supply its own hash;
        # the corrupt reference must fail rather than being traversed indefinitely.
        self_ref = {'object_id':'cycle_media','revision':1,'digest':'0'*64}
        cyclic = self.store.create_object('project_1', 'media', {'derivative_of':self_ref,
            'dependencies':[self_ref]}, 'worker_service', object_id='cycle_media')
        with self.assertRaises(DomainError):
            self.flow.media_origin('project_1', self.ref(cyclic), allow_derivatives=True)

    def test_media_origin_historical_resolution_is_not_freshness_or_acceptance(self):
        target, candidate, intent = self.producer()
        media = self.produced_media(candidate, intent)
        self.store.append_revision('project_1', target['object_id'], 1, {'content':'changed'}, 'author')
        self.assertFalse(self.flow.pinned_graph('project_1', self.ref(media))['stale'])
        self.assertTrue(self.flow.pinned_graph('project_1', self.ref(candidate))['stale'])
        origin = self.flow.media_origin('project_1', self.ref(media))
        self.assertEqual(origin['authority'], candidate)
        self.assertNotIn('accepted', origin)
        self.assertNotIn('fresh', origin)

    def test_a_scene_edit_never_stales_a_written_shot_card(self):
        # the card carries its whole prompt; the scene is the writer's context.
        scene = self.obj("scene")
        shot = self.obj("shot", deps=[scene])
        candidate = self.obj("candidate", deps=[shot])
        self.store.append_revision("project_1", scene["object_id"], 1, {"content": "changed", "dependencies": []}, "author")
        self.assertFalse(self.flow.pinned_graph("project_1", self.ref(shot))["stale"])
        self.assertFalse(self.flow.pinned_graph("project_1", self.ref(candidate))["stale"])
        self.store.append_revision("project_1", shot["object_id"], 1, {"content": "card changed", "dependencies": [self.ref(scene).model_dump()]}, "author")
        self.assertTrue(self.flow.pinned_graph("project_1", self.ref(candidate))["stale"])  # the card itself still counts

    def test_transitive_staleness_and_unrelated_parallel_work(self):
        scene = self.obj("script")  # any upstream but a shot's scene (frozen for the card)
        shot = self.obj("shot", deps=[scene])
        candidate = self.obj("candidate", deps=[shot])
        self.obj("asset", {"role": "voice"})
        self.assertFalse(self.flow.pinned_graph("project_1", self.ref(candidate))["stale"])
        self.store.append_revision("project_1", scene["object_id"], 1, {"content": "changed", "dependencies": []}, "author")
        graph = self.flow.pinned_graph("project_1", self.ref(candidate))
        self.assertTrue(graph["stale"])
        self.assertIn(scene["object_id"], str(graph["reasons"]))
        self.assertEqual(self.store.get_object("project_1", candidate["object_id"])["revision"], 1)

    def test_selected_old_asset_stays_selected_when_draft_changes(self):
        visual = self.obj("asset", {"type": "asset", "role": "visual"})
        selection = self.obj("asset", {"type": "asset-selection", "selected": {"visual": self.ref(visual).model_dump()}}, [visual])
        shot = self.obj("shot", deps=[selection])
        self.store.append_revision("project_1", visual["object_id"], 1, {"content": {"type":"asset", "role":"visual", "new_draft":True}, "dependencies":[]}, "author")
        self.assertFalse(self.flow.pinned_graph("project_1", self.ref(shot))["stale"])
        self.store.append_revision("project_1", selection["object_id"], 1, {"content": selection["body"]["content"], "dependencies":[]}, "author")
        self.assertTrue(self.flow.pinned_graph("project_1", self.ref(shot))["stale"])

    def test_selection_does_not_freeze_its_target_scene(self):
        scene = self.obj('scene', 'Original scene')
        look = self.obj('asset', {'type':'asset','role':'look'})
        selection = self.obj('asset', {'type':'asset-selection', 'target':self.ref(scene).model_dump(),
            'selected':{'look':self.ref(look).model_dump()}}, [scene,look])
        shot = self.obj('shot', deps=[selection])
        self.store.append_revision('project_1', scene['object_id'], 1, {'content':'Changed scene','dependencies':[]}, 'author')
        graph = self.flow.pinned_graph('project_1', self.ref(shot))
        self.assertTrue(graph['stale'])
        target = next(n for n in graph['nodes'] if n['ref']['object_id']==scene['object_id'])
        self.assertFalse(target['frozen_selection'])

    def qualification_graph(self, *, author='qualification_service', kind='qualification-sample', mutate=None):
        image = self.obj('media', {'reference': 'original'})
        asset = self.obj('asset', {'type': 'asset', 'role': 'visual', 'media_refs': [self.ref(image).model_dump()]}, [image])
        scene = self.obj('scene', 'Noon: the actor turns.')
        video = self.obj('media', {'probe': 'video'})
        ref = self.ref(asset).model_dump()
        body = {'method_id': 'mvgp-video-v1', 'asset': ref,
                'asset_references': [{'asset': ref, 'media': self.ref(image).model_dump(), 'sha256': 'a'*64, 'role': 'subject'}],
                'dependencies': [ref, self.ref(scene).model_dump(), self.ref(video).model_dump()]}
        if mutate:
            mutate(body, scene)
        sample = self.store.create_object('project_1', kind, body, author)
        collection = self.store.create_object('project_1', 'qualification-set', {'method_id': 'mvgp-video-v1',
            'asset': ref, 'dependencies': [ref, self.ref(sample).model_dump()]}, author)
        candidate = self.obj('candidate', deps=[collection])
        return asset, image, scene, video, sample, collection, candidate

    def test_qualification_freezes_only_exact_service_asset_and_descendants(self):
        asset, image, _, _, _, _, candidate = self.qualification_graph()
        for obj in (asset, image):
            self.store.append_revision('project_1', obj['object_id'], 1, {**obj['body'], 'draft_changed': True}, 'author')
        graph = self.flow.pinned_graph('project_1', self.ref(candidate))
        self.assertFalse(graph['stale'])
        self.assertTrue(all(n['frozen_selection'] for n in graph['nodes'] if n['ref']['object_id'] in (asset['object_id'], image['object_id'])))

    def test_qualification_does_not_freeze_scene_sample_or_collection_changes(self):
        for position in (2, 3, 4, 5):
            with self.subTest(position=position):
                objects = self.qualification_graph()
                changed = objects[position]
                self.store.append_revision('project_1', changed['object_id'], 1, {**changed['body'], 'changed': True}, changed['author'])
                graph = self.flow.pinned_graph('project_1', self.ref(objects[-1]))
                self.assertTrue(graph['stale'])
                node = next(n for n in graph['nodes'] if n['ref']['object_id'] == changed['object_id'])
                self.assertFalse(node['frozen_selection'])

    def test_author_or_unknown_kind_cannot_create_qualification_freeze(self):
        for author, kind in [('author', 'qualification-sample'), ('qualification_service', 'author-proposal')]:
            with self.subTest(author=author, kind=kind):
                asset, _, _, _, sample, _, _ = self.qualification_graph(author=author, kind=kind)
                self.store.append_revision('project_1', asset['object_id'], 1, {**asset['body'], 'changed': True}, 'author')
                self.assertTrue(self.flow.pinned_graph('project_1', self.ref(sample))['stale'])

    def test_qualification_asset_declaration_cannot_freeze_scene_or_wrong_hash(self):
        def wrong_hash(body, scene):
            body['asset_references'][0]['asset'] = {**body['asset'], 'digest': 'f'*64}
        def scene_as_asset(body, scene):
            body['asset_references'][0]['asset'] = self.ref(scene).model_dump()
        for mutate in (wrong_hash, scene_as_asset):
            with self.subTest(mutate=mutate.__name__):
                *_, sample, _, _ = self.qualification_graph(mutate=mutate)
                with self.assertRaises(DomainError):
                    self.flow.pinned_graph('project_1', self.ref(sample))

    def test_service_invalidation_still_blocks_a_frozen_qualification_asset(self):
        asset, _, _, _, _, _, candidate = self.qualification_graph()
        self.store.create_object('project_1', 'invalidation', {'target': self.ref(asset).model_dump()}, 'workflow_service')
        graph = self.flow.pinned_graph('project_1', self.ref(candidate))
        self.assertTrue(graph['stale'])
        self.assertTrue(any(reason['code'] == 'service_invalidated' for reason in graph['reasons']))

    def test_image_probe_has_no_script_or_qualification_cycle(self):
        asset = self.obj("asset", {"type":"asset", "role":"visual", "definition":"A cook"})
        method = self.method(asset, "image")
        result = self.flow.inspect(self.actor, "project_1", target=self.ref(asset), task="image", method=self.ref(method))
        self.assertIn("prepare", result["allowed"])
        self.assertFalse(result["accepted"])
        self.assertEqual(result["review_required"], [])  # no platform reviewer

    def test_missing_expected_performance_is_advice_and_prepare_stays_allowed(self):

        scene = self.obj("scene", "Expected whole scene")
        shot = self.obj("shot", {"Direction": {}}, [scene])
        method = self.method(shot)
        result = self.flow.inspect(self.actor, "project_1", target=self.ref(shot), task="shot", method=self.ref(method))
        self.assertIn("prepare", result["allowed"])
        self.assertNotIn("segment-expectation", result["missing"])
        self.assertIn("segment-expectation", result["advice"])

    def test_reading_the_source_first_is_required_while_its_switch_is_on(self):

        project = self.store.get_object("project_1", "project_1")
        self.store.append_revision("project_1", "project_1", project["revision"],
                                   {**project["body"], "branch": "recreation", "reader_switch": True}, "author")
        media = self.obj("media")
        blind = self.obj("source-understanding", {"source": self.ref(media).model_dump()}, [media])
        seen = self.store.create_object("project_1", "observation", {"source": self.ref(media).model_dump(), "status": "succeeded",
                                        "dependencies": [self.ref(media).model_dump()]}, "reader_service")
        cited = self.obj("source-understanding", {"source": self.ref(media).model_dump(),
                                                  "observation": self.ref(seen).model_dump()}, [media, seen])
        def missing(understanding):
            scene = self.obj("scene", "Scene", [understanding])
            shot = self.obj("shot", {"Direction": {"expected visible performance": "x"}}, [scene])
            return self.flow.inspect(self.actor, "project_1", target=self.ref(shot), task="shot", method=self.ref(self.method(shot)))["missing"]
        self.assertIn("source-observation", missing(blind))
        self.assertNotIn("source-observation", missing(cited))
        from production import switches
        self.store.create_object("project_1", switches.KIND, {"switches": {"source_reading": False}}, switches.AUTHOR,
                                 object_id=switches.object_id("project_1"))
        self.assertNotIn("source-observation", missing(blind))

    def test_recreation_requires_source_in_relevant_closure_only_with_the_reader_switch_on(self):
        # the owner's switch asks for the source reading; with it off the gap is advice.
        scene = self.obj("scene", "Expected whole scene")
        shot = self.obj("shot", {"Direction":{"expected visible performance":"Cook reaches out and recipient takes the bowl"}}, [scene])
        method = self.method(shot)
        self.assertIn("prepare", self.flow.inspect(self.actor, "project_1", target=self.ref(shot), task="shot", method=self.ref(method))["allowed"])
        project = self.store.get_object("project_1", "project_1")
        self.store.append_revision("project_1", "project_1", 1, {**project["body"], "branch":"recreation"}, "author")
        self.obj("source-understanding", {"unrelated":True})
        off = self.flow.inspect(self.actor, "project_1", target=self.ref(shot), task="shot", method=self.ref(method))
        self.assertIn("prepare", off["allowed"])
        self.assertIn("source-understanding", off["advice"])
        project = self.store.get_object("project_1", "project_1")
        self.store.append_revision("project_1", "project_1", project["revision"], {**project["body"], "reader_switch": True}, "author")
        result = self.flow.inspect(self.actor, "project_1", target=self.ref(shot), task="shot", method=self.ref(method))
        self.assertNotIn("prepare", result["allowed"])
        self.assertIn("source-understanding", result["missing"])

    def test_a_shot_without_a_scene_is_advice_not_a_refusal(self):
        # the whole-scene intent is craft; HF blocks nothing on it.
        shot = self.obj("shot", {"Direction": {"expected visible performance": "x"}})
        result = self.flow.inspect(self.actor, "project_1", target=self.ref(shot), task="shot", method=self.ref(self.method(shot)))
        self.assertIn("prepare", result["allowed"])
        self.assertIn("whole-scene-intent", result["advice"])

    def test_methods_come_from_the_runtime_config_and_their_task(self):
        asset = self.obj("asset")
        method = self.method(asset, "image")
        applicable = self.flow.applicable_method("project_1", self.ref(method), "image")
        self.assertEqual(applicable["method_id"], "image")
        self.assertEqual((applicable["rules"], applicable["release_id"]), ([], self.config.label))
        with self.assertRaises(DomainError):
            self.flow.applicable_method("project_1", self.ref(method), "shot")
        self.config.set("methods", {"methods": {"video": self.config.require_method("video")}})
        with self.assertRaises(DomainError):
            self.flow.applicable_method("project_1", self.ref(method), "image")

    def test_picture_lock_reopen_invalidates_only_dependents(self):
        shot = self.obj("shot")
        other = self.obj("shot")
        protected_other = self.obj("shot")
        cut = self.obj("cut", deps=[shot, protected_other])
        unrelated_cut = self.obj("cut", deps=[other])
        finish = self.obj("finishing", deps=[cut])
        other_finish = self.obj("finishing", deps=[unrelated_cut])
        lock = self.flow.record_picture_lock("project_1", self.ref(cut))
        with self.store.transaction() as conn, self.assertRaises(DomainError):
            Workflow.guard_mutation(self.store, "project_1", shot["object_id"], conn)
        with self.store.transaction() as conn:
            Workflow.guard_mutation(self.store, "project_1", other["object_id"], conn)
        reopened = self.flow.reopen(self.actor, "project_1", self.ref(lock), [self.ref(shot)], "Correct the missed action")
        self.assertFalse(reopened["accepted"])
        with self.store.transaction() as conn, self.assertRaises(DomainError):
            Workflow.guard_mutation(self.store, "project_1", protected_other["object_id"], conn)
        with self.store.transaction() as conn:
            Workflow.guard_mutation(self.store, "project_1", shot["object_id"], conn)
        self.assertTrue(self.flow.pinned_graph("project_1", self.ref(finish))["stale"])
        self.assertFalse(self.flow.pinned_graph("project_1", self.ref(other_finish))["stale"])
        with self.assertRaises(DomainError):
            self.flow.reopen(self.actor, "project_1", self.ref(lock), [self.ref(other)], "Unrelated")

    def test_reopening_a_card_also_reopens_its_method(self):
        # simulated run: after 这版可以, reshooting a reopened card stopped on its locked method.
        shot = self.obj("shot")
        method = self.method(shot)
        cut = self.obj("cut", deps=[shot, method])
        lock = self.flow.record_picture_lock("project_1", self.ref(cut))
        with self.store.transaction() as conn, self.assertRaises(DomainError):
            Workflow.guard_mutation(self.store, "project_1", method["object_id"], conn)
        self.flow.reopen(self.actor, "project_1", self.ref(lock), [self.ref(shot)], "Reshoot the missed action")
        with self.store.transaction() as conn:
            Workflow.guard_mutation(self.store, "project_1", method["object_id"], conn)

    def test_owner_repick_reopens_the_shot_and_its_cut(self):
        # the lock binds the agent, not the owner's pick.
        shot = self.obj("shot")
        other = self.obj("shot")
        cut = self.obj("cut", deps=[shot, other])
        self.flow.record_picture_lock("project_1", self.ref(cut))
        with self.store.transaction() as conn:
            reopened = self.flow.reopen_for_owner("project_1", shot["object_id"], "Owner picked another take", conn=conn)
            self.assertEqual(len(reopened), 1)
            Workflow.guard_mutation(self.store, "project_1", shot["object_id"], conn)
            Workflow.guard_mutation(self.store, "project_1", cut["object_id"], conn)
            with self.assertRaises(DomainError):
                Workflow.guard_mutation(self.store, "project_1", other["object_id"], conn)
            self.assertEqual(self.flow.reopen_for_owner("project_1", shot["object_id"], "again", conn=conn), [])

    def test_recreation_with_source_closure_may_prepare_but_not_submit(self):
        source_media = self.obj("media", {"verified": "fixture"})
        understanding = self.obj("source-understanding", {"facts": ["Cook offers bowl"]}, [source_media])
        scene = self.obj("scene", "Expected entire scene", [understanding])
        shot = self.obj("shot", {"Direction": {"expected visible performance": "A clear offer followed by acceptance"}}, [scene])
        method = self.method(shot)
        project = self.store.get_object("project_1", "project_1")
        self.store.append_revision("project_1", "project_1", 1, {**project["body"], "branch": "recreation"}, "author")
        result = self.flow.inspect(self.actor, "project_1", target=self.ref(shot), task="shot", method=self.ref(method))
        self.assertIn("prepare", result["allowed"])
        self.assertNotIn("submit", result["allowed"])
        self.assertFalse(result["accepted"])
        self.assertEqual(result["review_required"], [])  # no platform reviewer

    def test_dependency_cycle_and_reopen_scope_fail_closed(self):
        cyclic = self.store.create_object("project_1", "shot", {"dependencies": [{"object_id":"cyclic", "revision":1}]}, "author", object_id="cyclic")
        with self.assertRaises(DomainError):
            self.flow.pinned_graph("project_1", self.ref(cyclic))
        shot = self.obj("shot")
        cut = self.obj("cut", deps=[shot])
        lock = self.flow.record_picture_lock("project_1", self.ref(cut))
        viewer = self.auth.authenticate(self.auth.provision_token("viewer", "viewer", ["project_1"], 300))
        for actor, reason in ((self.actor, ""), (viewer, "Fix")):
            with self.assertRaises(DomainError):
                self.flow.reopen(actor, "project_1", self.ref(lock), [self.ref(shot)], reason)
        self.assertEqual(self.store.list_objects("project_1", kind="reopen"), [])
        self.assertEqual(self.store.list_objects("project_1", kind="invalidation"), [])

    def test_no_platform_reviewer_is_ever_required(self):
        # the writer's reviewer is advice on the desk; the platform requires no role.
        policy = self.flow.review_policy("project_1", "prepare", "image", "image")
        self.assertEqual((policy["required_roles"], policy["routes"]), ([], {}))
        asset = self.obj("asset")
        method = self.method(asset, "image")
        result = self.flow.inspect(self.actor, "project_1", target=self.ref(asset), task="image", method=self.ref(method))
        self.assertNotIn("review-policy", result["missing"])
        self.assertIn("prepare", result["allowed"])

    def test_recreation_image_probe_does_not_wait_for_source_scene(self):
        project = self.store.get_object("project_1", "project_1")
        self.store.append_revision("project_1", "project_1", 1, {**project["body"], "branch": "recreation"}, "fixture_operator")
        asset = self.obj("asset", {"type":"asset", "role":"visual"})
        method = self.method(asset, "image")
        result = self.flow.inspect(self.actor, "project_1", target=self.ref(asset), task="image", method=self.ref(method))
        self.assertIn("prepare", result["allowed"])
        self.assertNotIn("source-understanding", result["missing"])

    def test_deep_author_chain_fails_as_domain_error(self):
        with self.store.transaction() as conn:
            previous = None
            for _ in range(132):
                deps = [self.ref(previous).model_dump()] if previous else []
                previous = self.store.create_object("project_1", "shot", {"dependencies": deps}, "author", conn=conn)
        with self.assertRaises(DomainError) as caught:
            self.flow.pinned_graph("project_1", self.ref(previous))
        self.assertEqual(caught.exception.code, "insufficient_context")

    def test_inspection_can_share_callers_uncommitted_snapshot(self):
        with self.store.transaction() as conn:
            asset = self.store.create_object("project_1", "asset", {"content": {}, "dependencies": []}, "author", conn=conn)
            result = self.flow.inspect(self.actor, "project_1", target=self.ref(asset), conn=conn)
            self.assertFalse(result["stale"])
            self.assertIn("revise-artifact", result["allowed"])

    def test_dependency_digest_and_scope_fail_closed(self):
        item = self.obj("scene")
        with self.assertRaises(DomainError):
            self.flow.pinned_graph("project_1", ObjectRef(object_id=item["object_id"], revision=1, digest="f"*64))
        stranger = self.auth.authenticate(self.auth.provision_token("other", "agent", [], 300))
        with self.assertRaises(DomainError):
            self.flow.inspect(stranger, "project_1", target=self.ref(item), task="shot")


if __name__ == "__main__":
    unittest.main()
