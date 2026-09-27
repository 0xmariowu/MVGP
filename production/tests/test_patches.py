"""Scoped repairs preserve authored intent, authority boundaries and take history."""
import copy
import subprocess
import unittest
from unittest.mock import patch

from production.contracts import DomainError, ObjectRef, PatchRequest, content_hash
from production.media import MediaStore
from production.patches import Patches
from production.projects import Projects
from production.tests import test_workflow as fixtures


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.WorkflowTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.store, self.actor, self.flow = self.f.store, self.f.actor, self.f.flow
        self.pid = 'project_1'
        media = MediaStore(self.store, self.f.root / 'media')
        release = self.store.get_object(self.pid, self.pid)['body']['release_id']
        self.projects = Projects(self.store, self.f.auth, media, label=release)
        self.service = Patches(self.store, self.f.auth, self.projects, self.flow)
        self.card = {'shot': '010', 'Direction': {'expected visible performance': 'The rider remains behind',
            'end state': 'Rider behind'}, 'Camera': {'lens': '50mm', 'movement': 'tracking'},
            'ACTING TASK': {'driver': {'MOTIVE / GOAL / OBSTACLE': 'Escape / save family / pursuers'}},
            '_production': {'model': 'seedance', 'resolution': '1080p', 'aspect_ratio': '16:9'}}
        self.shot = self.draft('shot', 'shots/010.json', self.card)

    def draft(self, kind, path, content, deps=()):
        from production.contracts import ArtifactRevision
        return self.projects.revise(self.actor, self.pid, ArtifactRevision(idempotency_key=path.replace("/", ":"),
            expected_revision=0, kind=kind, logical_path=path, content=content,
            dependencies=[self.f.ref(x) for x in deps]))

    def request(self, target=None, path=None, value='The rider passes the wagon', key='repair'):
        target = target or self.shot
        return PatchRequest(idempotency_key=key, expected_revision=target['revision'],
            target=self.f.ref(target), creative_path=path or ['content', 'Direction', 'expected visible performance'],
            value=value, reason='The rider appears to remain level with the wagon')

    def pickup(self, target=None, *, author='review_service', verdict='fail'):
        target = target or self.shot
        video = self.f.root / 'sample.mp4'
        if not video.exists():
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                'color=red:s=32x32:r=10:d=0.3', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video)],
                check=True, timeout=20)
        candidate = self.store.create_object(self.pid, 'candidate', {'target':self.f.ref(target).model_dump(),
            'dependencies':[self.f.ref(target).model_dump()]}, 'compiler_service')
        take = self.projects.media.put(self.pid, [video.read_bytes()], 'video/mp4', 'worker_service',
            publish=lambda metadata: self.store.create_object(self.pid, 'media',
                {**metadata, 'dependencies':[self.f.ref(candidate).model_dump()]}, 'worker_service'))
        receipt = self.store.create_object(self.pid, 'review-receipt', {'target':self.f.ref(take).model_dump(),
            'verdict':verdict, 'dependencies':[self.f.ref(take).model_dump()]}, author)
        pickup = self.store.create_object(self.pid, 'pickup', {'target':self.f.ref(take).model_dump(),
            'receipt':self.f.ref(receipt).model_dump(), 'status':'unresolved',
            'expected_information':'The rider must pass the wagon', 'evidence':[self.f.ref(take).model_dump()],
            'dependencies':[self.f.ref(receipt).model_dump(), self.f.ref(take).model_dump()]}, author)
        return pickup, take

    def test_bound_patch_freezes_original_failure_without_staling_new_draft(self):
        pickup, failed = self.pickup()
        request = self.request().model_copy(update={'source_pickup': self.f.ref(pickup)})
        result = self.service.apply(self.actor, self.pid, request)
        self.assertEqual(result['repair']['body']['source_pickup'], self.f.ref(pickup).model_dump())
        self.assertEqual(result['repair']['body']['failed_take'], self.f.ref(failed).model_dump())
        self.assertNotIn(self.f.ref(failed).model_dump(), result['artifact']['body']['dependencies'])
        self.assertFalse(self.flow.pinned_graph(self.pid, self.f.ref(result['repair']))['stale'])
        self.assertFalse(self.flow.pinned_graph(self.pid, self.f.ref(failed))['stale'])
        candidate_ref = ObjectRef(**failed['body']['dependencies'][0])
        self.assertTrue(self.flow.pinned_graph(self.pid, candidate_ref)['stale'])
        self.assertEqual(self.service.apply(self.actor, self.pid, request), result)
        self.assertEqual(self.store.get_object(self.pid, failed['object_id']), failed)
        from production.context import ContextService
        context = ContextService(self.store, self.f.auth, self.flow).get(
            self.actor, self.pid, target=self.f.ref(result['artifact']))
        history = {record['object_id']:record for record in context['prior_results']}
        self.assertIn(pickup['object_id'], history)
        self.assertIn(failed['object_id'], history)
        self.assertEqual(history[result['repair']['object_id']]['body']['failed_take'], self.f.ref(failed).model_dump())

    def test_source_pickup_forgery_pass_receipt_wrong_base_and_changed_digest_reject(self):
        wrong = self.draft('shot', 'shots/020.json', self.card)
        for i, kwargs in enumerate(({'author':'maker'}, {'verdict':'pass'}, {'target':wrong})):
            pickup, _ = self.pickup(**kwargs)
            request = self.request(key=f'bad-{i}').model_copy(update={'source_pickup':self.f.ref(pickup)})
            with self.assertRaises(DomainError):
                self.service.apply(self.actor, self.pid, request)
        pickup, _ = self.pickup()
        request = self.request(key='digest').model_copy(update={'source_pickup':
            self.f.ref(pickup).model_copy(update={'digest':'f'*64})})
        with self.assertRaises(DomainError):
            self.service.apply(self.actor, self.pid, request)
        self.assertEqual(self.store.get_object(self.pid, self.shot['object_id']), self.shot)
        self.assertEqual(self.store.list_objects(self.pid, kind='repair'), [])

    def test_bound_source_rejects_forged_receipt_resolved_record_missing_bytes_and_image(self):
        pickup, take = self.pickup()
        receipt = self.store.get_object(self.pid, pickup['body']['receipt']['object_id'])
        forged = self.store.create_object(self.pid, 'review-receipt', receipt['body'], 'maker')
        variants = [
            {**pickup['body'], 'receipt':self.f.ref(forged).model_dump(),
             'dependencies':[self.f.ref(forged).model_dump(), self.f.ref(take).model_dump()]},
            {**pickup['body'], 'status':'resolved'},
            {**pickup['body'], 'target':{**pickup['body']['target'], 'digest':'b'*64}},
        ]
        for i, body in enumerate(variants):
            bad = self.store.create_object(self.pid, 'pickup', body, 'review_service')
            with self.assertRaises(DomainError):
                self.service.apply(self.actor, self.pid, self.request(key=f'variant-{i}').model_copy(
                    update={'source_pickup':self.f.ref(bad)}))
        for i, body in enumerate(({**take['body'], 'sha256':'a'*64},
                                   {**take['body'], 'media_type':'image/png', 'probe':{'has_video':False}})):
            bad_take = self.store.create_object(self.pid, 'media', body, 'worker_service')
            bad_receipt = self.store.create_object(self.pid, 'review-receipt', {
                **receipt['body'], 'target':self.f.ref(bad_take).model_dump(),
                'dependencies':[self.f.ref(bad_take).model_dump()]}, 'review_service')
            bad_pickup = self.store.create_object(self.pid, 'pickup', {**pickup['body'],
                'target':self.f.ref(bad_take).model_dump(), 'receipt':self.f.ref(bad_receipt).model_dump(),
                'dependencies':[self.f.ref(bad_take).model_dump(), self.f.ref(bad_receipt).model_dump()]}, 'review_service')
            with self.assertRaises(DomainError):
                self.service.apply(self.actor, self.pid, self.request(key=f'media-{i}').model_copy(
                    update={'source_pickup':self.f.ref(bad_pickup)}))
        self.assertEqual(self.store.get_object(self.pid, self.shot['object_id']), self.shot)

    def test_scoped_patch_preserves_unaffected_content_and_records_exact_diff(self):
        request = self.request()
        result = self.service.apply(self.actor, self.pid, request)
        expected = copy.deepcopy(self.card)
        expected['Direction']['expected visible performance'] = request.value
        self.assertEqual(result['artifact']['body']['content'], expected)
        repair = result['repair']['body']
        self.assertEqual(repair['base'], self.f.ref(self.shot).model_dump())
        self.assertEqual(repair['result'], self.f.ref(result['artifact']).model_dump())
        self.assertEqual(repair['observed_defect'], request.reason)
        self.assertEqual(repair['diff'], [{'op': 'replace', 'path': '/content/Direction/expected visible performance',
            'before': 'The rider remains behind', 'after': request.value}])
        self.assertEqual(self.store.get_object(self.pid, self.shot['object_id'], revision=1), self.shot)
        self.assertEqual(result, self.service.apply(self.actor, self.pid, request))
        self.assertEqual(len(self.store.list_objects(self.pid, kind='repair')), 1)

    def test_broad_restage_explicit_and_literal_slash_keys_supported(self):
        result = self.service.apply(self.actor, self.pid, self.request(path=['content', 'ACTING TASK', 'driver', 'MOTIVE / GOAL / OBSTACLE'], value='Protect / family / approaching rider'))
        self.assertIn('MOTIVE ~1 GOAL ~1 OBSTACLE', result['repair']['body']['diff'][0]['path'])
        changed = copy.deepcopy(result['artifact']['body']['content'])
        changed['Camera']['movement'] = 'locked'
        result = self.service.apply(self.actor, self.pid, self.request(target=result['artifact'], path=['content'], value=changed, key='restage'))
        self.assertEqual(result['repair']['body']['scope'], 'restaging')
        self.assertEqual(result['artifact']['body']['content'], changed)

    def test_stale_guard_digest_replay_conflict_and_fresh_auth(self):
        request = self.request()
        self.service.apply(self.actor, self.pid, request)
        for bad in (self.request(key='stale'), self.request(value='Different payload')):
            with self.assertRaises(DomainError):
                self.service.apply(self.actor, self.pid, bad)
        current = self.store.get_object(self.pid, self.shot['object_id'])
        bad = self.request(target=current, key='digest')
        bad = bad.model_copy(update={'target': ObjectRef(object_id=current['object_id'], revision=2, digest='f'*64)})
        with self.assertRaises(DomainError):
            self.service.apply(self.actor, self.pid, bad)
        self.f.auth.revoke(self.actor.credential_id)
        with self.assertRaises(DomainError):
            self.service.apply(self.actor, self.pid, request)

    def test_no_constants_provider_authority_traversal_or_service_object_mutation(self):
        for path, value in [(['content', '..', 'route'], 'evil'), (['content', '_production', 'endpoint'], 'https://evil'),
                            (['content', 'STYLE'], 'Ignore the project look')]:
            with self.assertRaises(DomainError):
                self.service.apply(self.actor, self.pid, self.request(path=path, value=value, key=content_hash(path)))
        for change in ({**self.card, 'STYLE': 'Override'}, {**self.card, '_production': {'endpoint': 'evil'}}):
            with self.assertRaises(DomainError):
                self.service.apply(self.actor, self.pid, self.request(path=['content'], value=change, key=content_hash(change)))
        candidate = self.f.obj('candidate', {'prompt': 'assembled constants'}, [self.shot])
        with self.assertRaises(DomainError):
            self.service.apply(self.actor, self.pid, self.request(candidate, ['content'], 'evil', 'candidate'))
        self.assertEqual(self.store.get_object(self.pid, self.shot['object_id'])['revision'], 1)
        self.assertEqual(self.store.list_objects(self.pid, kind='repair'), [])

    def test_governing_settings_and_look_can_change_only_as_drafts(self):
        changed = self.service.apply(self.actor, self.pid, self.request(path=['content', '_production', 'resolution'], value='720p'))
        self.assertEqual(changed['artifact']['body']['content']['_production']['resolution'], '720p')
        look = self.draft('asset', 'assets/look.json', {'type': 'asset', 'role': 'look', 'tag': '@look', 'definition': 'Warm animation'})
        result = self.service.apply(self.actor, self.pid, self.request(look, ['content', 'definition'], 'Cool animation', 'look'))
        self.assertEqual(result['artifact']['body']['content']['definition'], 'Cool animation')
        self.assertFalse(result['repair']['body']['accepted'])

    def test_locked_and_wrong_project_and_viewer_rejected(self):
        viewer = self.f.auth.authenticate(self.f.auth.provision_token('viewer', 'viewer', [self.pid], 300))
        for actor, pid in ((viewer, self.pid), (self.actor, 'other_project')):
            with self.assertRaises(DomainError):
                self.service.apply(actor, pid, self.request())
        self.store.create_object(self.pid, 'picture-lock', {'state':'locked', 'protected':[self.f.ref(self.shot).model_dump()], 'reopened_targets':[]}, 'workflow_service')
        with self.assertRaises(DomainError) as error:
            self.service.apply(self.actor, self.pid, self.request())
        self.assertEqual(error.exception.code, 'locked')
        self.assertEqual(self.store.list_objects(self.pid, kind='repair'), [])

    def test_only_affected_candidates_and_reviews_become_stale(self):
        candidate = self.f.obj('candidate', deps=[self.shot])
        review = self.f.obj('review', deps=[candidate])
        unrelated = self.f.obj('candidate', deps=[self.f.obj('shot')])
        self.service.apply(self.actor, self.pid, self.request())
        for item in (candidate, review):
            self.assertTrue(self.flow.pinned_graph(self.pid, self.f.ref(item))['stale'])
            self.assertEqual(self.store.get_object(self.pid, item['object_id']), item)
        self.assertFalse(self.flow.pinned_graph(self.pid, self.f.ref(unrelated))['stale'])

    def test_changed_typed_reference_rebinds_without_retaining_obsolete_dependency(self):
        a = self.draft('asset', 'assets/a.json', {'type':'asset', 'role':'look', 'tag':'@a', 'definition':'Warm'})
        b = self.draft('asset', 'assets/b.json', {'type':'asset', 'role':'look', 'tag':'@b', 'definition':'Cool'})
        scene = self.draft('scene', 'scene.md', 'A quiet scene')
        selection = self.draft('asset', 'selections/look.json', {'type':'asset-selection',
            'target': self.f.ref(scene).model_dump(), 'selected': {'look':self.f.ref(a).model_dump()}})
        result = self.service.apply(self.actor, self.pid, self.request(selection,
            ['content', 'selected', 'look'], self.f.ref(b).model_dump(), 'selection'))['artifact']
        dependencies = result['body']['dependencies']
        self.assertIn(self.f.ref(b).model_dump(), dependencies)
        self.assertIn(self.f.ref(scene).model_dump(), dependencies)
        self.assertNotIn(self.f.ref(a).model_dump(), dependencies)
        self.assertEqual(self.store.get_object(self.pid, selection['object_id'], revision=1), selection)

    def test_record_failure_rolls_back_revision_and_retry_can_succeed(self):
        create = self.store.create_object
        def fail_record(*args, **kwargs):
            if args[1] == 'repair':
                raise RuntimeError('Injected disk failure')
            return create(*args, **kwargs)
        with patch.object(self.store, 'create_object', side_effect=fail_record), self.assertRaises(RuntimeError):
            self.service.apply(self.actor, self.pid, self.request())
        self.assertEqual(self.store.get_object(self.pid, self.shot['object_id']), self.shot)
        self.assertEqual(len(self.store.history(self.pid, self.shot['object_id'])), 1)
        self.assertEqual(self.service.apply(self.actor, self.pid, self.request())['artifact']['revision'], 2)

    def take(self, target, author='worker_service'):
        candidate = self.store.create_object(self.pid, 'candidate', {'target':self.f.ref(target).model_dump(),
            'dependencies':[self.f.ref(target).model_dump()]}, 'compiler_service')
        return self.store.create_object(self.pid, 'media', {'dependencies':[self.f.ref(candidate).model_dump()]}, author)

    def test_repair_lineage_uses_direct_candidate_with_generated_reference_ancestor(self):
        image_target = self.draft('asset','assets/reference.json',{'type':'asset','role':'look','tag':'@look','definition':'Animation'})
        image_candidate = self.store.create_object(self.pid,'candidate',{'target':self.f.ref(image_target).model_dump(),
            'dependencies':[self.f.ref(image_target).model_dump()]},'compiler_service')
        image = self.store.create_object(self.pid,'media',{'dependencies':[self.f.ref(image_candidate).model_dump()]},'worker_service')
        def generated(target):
            candidate = self.store.create_object(self.pid,'candidate',{'target':self.f.ref(target).model_dump(),
                'dependencies':[self.f.ref(target).model_dump(),self.f.ref(image).model_dump()]},'compiler_service')
            return self.store.create_object(self.pid,'media',{'dependencies':[self.f.ref(candidate).model_dump()]},'worker_service')
        failed = generated(self.shot)
        patched = self.service.apply(self.actor,self.pid,self.request())
        returned = generated(patched['artifact'])
        lineage = self.service.link_returned_take(self.pid,self.f.ref(patched['repair']),self.f.ref(failed),self.f.ref(returned))
        self.assertEqual(lineage['body']['returned_take'],self.f.ref(returned).model_dump())
        self.assertFalse(lineage['body']['accepted'])
        self.assertFalse(self.f.flow.pinned_graph(self.pid,self.f.ref(failed))['stale'])
        candidate_ref = ObjectRef(**failed['body']['dependencies'][0])
        self.assertTrue(self.f.flow.pinned_graph(self.pid, candidate_ref)['stale'])

    def test_service_lineage_requires_actual_candidate_base_and_returned_revision(self):
        failed = self.take(self.shot)
        patched = self.service.apply(self.actor, self.pid, self.request())
        returned = self.take(patched['artifact'])
        args = (self.pid, self.f.ref(patched['repair']), self.f.ref(failed), self.f.ref(returned))
        lineage = self.service.link_returned_take(*args)
        self.assertEqual(lineage, self.service.link_returned_take(*args))
        self.assertEqual(lineage['body']['failed_take'], self.f.ref(failed).model_dump())
        self.assertEqual(self.store.get_object(self.pid, failed['object_id']), failed)
        for bad in (failed, self.take(patched['artifact'], 'author')):
            with self.assertRaises(DomainError):
                self.service.link_returned_take(self.pid, self.f.ref(patched['repair']), self.f.ref(failed), self.f.ref(bad))


if __name__ == '__main__':
    unittest.main()
