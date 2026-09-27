"""Public read projections preserve authority, exact versions and private boundaries."""
import json
import time
import unittest
from unittest.mock import patch

from production.contracts import DomainError, ObjectRef
from production.queries import Queries
from production.tests import test_decisions, test_projects
from production.workflow import Workflow


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.p = test_projects.ProjectTests()
        self.p.setUp()
        self.addCleanup(self.p.tearDown)
        self.store, self.auth, self.actor, self.pid = self.p.store, self.p.auth, self.p.actor, self.p.pid
        self.flow = Workflow(self.store, self.auth, None)
        self.q = Queries(self.store, self.auth, self.flow)
        self.viewer = self.auth.authenticate(self.auth.provision_token('viewer', 'viewer', [self.pid], 300))

    def ref(self, obj):
        return ObjectRef(**self.p.ref(obj))

    def test_the_owners_project_list_leaves_out_hidden_projects(self):
        # his "收起" projects never show on the desk; every other one does.
        from production.tests.fixtures import owner_session
        self.store.create_project('project_hidden', {'title': 'Hidden'}, 'operator')
        self.auth.hidden_projects = frozenset({'project_hidden'})
        owner = self.auth.authenticate(owner_session(self.auth)['session_token'], channel='cookie')
        listed = [row['project_id'] for row in self.q.list_projects(owner)]
        self.assertIn(self.pid, listed)
        self.assertNotIn('project_hidden', listed)

    def add(self, kind, body, author='author_1'):
        return self.store.create_object(self.pid, kind, body, author)

    def test_project_batch_matches_individual_graph_results(self):
        scene = self.add('scene', {'content': 'Scene'})
        self.add('shot', {'content': 'Current shot', 'dependencies': [self.ref(scene).model_dump()]})
        bad = self.ref(scene).model_copy(update={'digest': 'f' * 64})
        self.add('shot', {'content': 'Bad dependency', 'dependencies': [bad.model_dump()]})
        self.add('media', {'media_type': 'video/mp4', 'current': False, 'dependencies': []})
        self.store.append_revision(self.pid, scene['object_id'], 1, {'content': 'New scene'}, 'author_1')
        def individual(pid, refs, *, conn):
            results = []
            for ref in refs:
                try:
                    graph = self.flow.pinned_graph(pid, ref, conn=conn)
                    results.append({key: graph[key] for key in ('stale', 'reasons')})
                except DomainError as exc:
                    results.append({'error': exc.code})
            return results
        with patch.object(self.flow, 'pinned_states', side_effect=individual):
            expected = self.q.project(self.viewer, self.pid)
        with patch.object(self.flow, 'pinned_graph', side_effect=AssertionError('Ordinary rows must use the batch')):
            actual = self.q.project(self.viewer, self.pid)
        self.assertEqual(actual, expected)
        self.auth.revoke(self.viewer.credential_id)
        with (patch.object(self.flow, 'pinned_states', side_effect=AssertionError('Authorization must run first')),
              self.assertRaises(DomainError)):
            self.q.project(self.viewer, self.pid)

    def test_owned_read_snapshot_projects_without_nested_transactions(self):
        item = self.add('brief', {'logical_path': 'brief.md', 'content': 'First version'})
        self.store.append_revision(self.pid, item['object_id'], 1,
            {'logical_path': 'brief.md', 'content': 'Second version'}, 'author_1')
        with self.store.transaction(write=False) as db, patch.object(
                self.store, 'transaction', side_effect=AssertionError('Unexpected snapshot restore')):
            self.assertEqual(self.q.list_projects(self.viewer, conn=db)[0]['project_id'], self.pid)
            self.assertEqual(self.q.project(self.viewer, self.pid, conn=db)['project_id'], self.pid)
            view = self.q.artifact(self.viewer, self.pid, item['object_id'], 1, conn=db)
            self.assertEqual(view['details']['content'], 'First version')
            self.assertEqual(view['object_ref']['revision'], 1)
            self.assertTrue(self.q.tree(self.viewer, self.pid, conn=db))

    def test_shared_snapshot_still_rechecks_revocation_and_project_scope(self):
        item = self.add('brief', {'content': 'Private content'})
        foreign = self.auth.authenticate(self.auth.provision_token('outsider', 'viewer', [], 300))
        self.auth.revoke(self.viewer.credential_id)
        with self.store.transaction(write=False) as db:
            for actor in (foreign, self.viewer):
                for query in (
                    lambda actor=actor: self.q.project(actor, self.pid, conn=db),
                    lambda actor=actor: self.q.artifact(actor, self.pid, item['object_id'], conn=db),
                    lambda actor=actor: self.q.tree(actor, self.pid, conn=db),
                ):
                    with self.assertRaises(DomainError):
                        query()
            with self.assertRaises(DomainError):
                self.q.list_projects(self.viewer, conn=db)
            self.assertEqual(self.q.list_projects(foreign, conn=db), [])

    def test_project_history_uses_exact_authorized_operator_lineage_not_title(self):
        source = self.store.get_object(self.pid, self.pid)
        migration = {'source_project': self.p.ref(source), 'requires_revalidation': True, 'copied_approvals': False}
        body = {**source['body'], 'migration': migration}
        for pid, author, content in [('new_run', 'operator', body), ('same_name', 'author', source['body']),
                ('forged', 'author', body), ('wrong_hash', 'operator', {**body, 'migration': {
                    **migration, 'source_project': {**migration['source_project'], 'digest': 'f'*64}}})]:
            self.store.create_project(pid, content, author)
        viewer = self.auth.authenticate(self.auth.provision_token('all_runs', 'viewer',
            [self.pid, 'new_run', 'same_name', 'forged', 'wrong_hash'], 300))
        rows = {r['project_id']: r for r in self.q.list_projects(viewer)}
        self.assertEqual(rows[self.pid]['superseded_by'], ['new_run'])
        self.assertEqual(rows['new_run']['previous_project_id'], self.pid)
        self.assertTrue(rows['new_run']['created_at'])
        for pid in ('same_name', 'forged', 'wrong_hash'):
            self.assertIsNone(rows[pid]['previous_project_id'])
            self.assertEqual(rows[pid]['superseded_by'], [])
        child_only = self.auth.authenticate(self.auth.provision_token('one_run', 'viewer', ['new_run'], 300))
        isolated = self.q.list_projects(child_only)
        self.assertIsNone(isolated[0]['previous_project_id'])
        self.assertNotIn(self.pid, json.dumps(isolated))
        self.assertEqual(self.q.list_projects(self.viewer)[0]['superseded_by'], [])

    def test_appeal_is_visible_as_argument_with_only_a_safe_prior_reference(self):
        prior = self.add('review-task', {'state': 'completed', 'verdict': 'fail'}, 'review_service')
        task = self.add('review-task', {'state': 'queued',
            'appeal_of': {**self.p.ref(prior), 'private_response': 'PRIVATE_WIRE'},
            'appeal_reason': 'This is a character asset, not a final two-shot.'}, 'review_service')
        detail = self.q.artifact(self.viewer, self.pid, task['object_id'])
        self.assertEqual(detail['details']['appeal_of'], self.p.ref(prior))
        self.assertEqual(detail['details']['appeal_reason'], task['body']['appeal_reason'])
        self.assertIn('not an approval', detail['details']['appeal_authority'])
        self.assertFalse(detail['confirmed_final'])
        self.assertNotIn('PRIVATE_WIRE', json.dumps(detail))

    def test_failed_review_explains_service_failure_without_private_provider_data(self):
        task = self.add('review-task', {'state': 'running', 'context_hash': 'a'*64}, 'review_service')
        execution = self.add('review-turn', {'task_id': task['object_id'], 'context_hash': 'a'*64,
            'status': 'failed', 'failure': 'invalid_input', 'diagnostic': 'read_timeout', 'result': {'raw_response': 'PRIVATE_WIRE'}},
            'review_runner_service')
        task = self.store.append_revision(self.pid, task['object_id'], task['revision'],
            {**task['body'], 'state': 'failed', 'execution': self.p.ref(execution)}, 'review_service')
        actual = self.q.artifact(self.viewer, self.pid, task['object_id'])
        self.assertEqual(actual['details']['failure'], 'invalid_input')
        self.assertEqual(actual['details']['diagnostic'], 'read_timeout')
        self.assertEqual(actual['details']['execution'], self.p.ref(execution))
        self.assertNotIn('verdict', actual['details'])
        self.assertNotIn('PRIVATE', json.dumps(actual))
        for kind in ('review-run', 'review-turn'):
            record = self.add(kind, {'failure': 'PRIVATE_WIRE', 'diagnostic': 'PRIVATE_REASON', 'result': 'PRIVATE_RESULT'}, 'review_runner_service')
            detail = self.q.artifact(self.viewer, self.pid, record['object_id'])
            self.assertEqual(detail['details']['failure'], 'review_execution_failed')
            self.assertIsNone(detail['details']['diagnostic'])
            self.assertNotIn('PRIVATE', json.dumps(detail))
        fake = self.add('review-task', {**task['body'], 'execution': self.p.ref(execution)}, 'review_service')
        self.assertNotIn('failure', self.q.artifact(self.viewer, self.pid, fake['object_id'])['details'])

    def test_image_candidate_shows_released_quality_and_variant_without_private_parameters(self):
        params = {'prompt': 'A brightly lit rehearsal room.', 'resolution': '2k',
                  'quality': 'high', 'variant': 'sunburst'}
        candidate = self.add('candidate', {'request': {'job_type': 'gpt_image_2_5',
            'params': {**params, 'provider_secret': 'PRIVATE_PARAMETER'}}}, 'compiler_service')
        detail = self.q.artifact(self.viewer, self.pid, candidate['object_id'])
        self.assertEqual(detail['details']['request']['params'], params)
        self.assertNotIn('PRIVATE_PARAMETER', json.dumps(detail))

    def test_source_and_reference_uploads_do_not_imply_production(self):
        source = self.add('media', {'logical_path': 'source/scene.mp4', 'media_type': 'video/mp4'})
        portraits = [self.add('media', {'logical_path': f'assets/{name}/washed.png',
                                      'media_type': 'image/png'}) for name in ('lupin', 'newcomer')]
        # Observer derivatives are viewing aids, not new generated performances.
        frame = self.add('media', {'media_type': 'image/png',
                         'derivative_of': self.p.ref(source)}, 'worker_service')
        view = self.q.project(self.viewer, self.pid)
        self.assertEqual(view['phase'], 'preparation')
        self.assertEqual(view['results'], [])
        self.assertEqual({v['object_ref']['object_id'] for v in view['plan']},
                         {o['object_id'] for o in [source, frame, *portraits]})
        names = [self.q.artifact(self.viewer, self.pid, o['object_id'])['display_name'] for o in portraits]
        self.assertEqual(names, ['lupin / washed.png', 'newcomer / washed.png'])
        for obj in [source, frame, *portraits]:
            self.assertFalse(self.q.artifact(self.viewer, self.pid, obj['object_id'])['confirmed_final'])

    def test_source_observation_failure_and_uncertainty_are_visible_in_preparation(self):
        source = self.add('media', {'media_type': 'video/mp4'})
        intent = self.add('dispatch-intent', {'operation': 'observe', 'target': self.p.ref(source)}, 'submission_service')
        job = self.add('job', {'state': 'failed', 'intent': self.p.ref(intent),
            'preparation_failure': {'code': 'invalid_media', 'provider_called': False, 'stderr': 'PRIVATE_STDERR'},
            'lease': {'credential_id': 'PRIVATE_WORKER'}}, 'worker_service')
        result = self.add('observation', {'source': self.p.ref(source), 'status': 'succeeded',
            'answers': [{'question_index': 0, 'verdict': 'uncertain', 'evidence_indices': [], 'explanation': 'Speech is unclear.'}],
            'uncertainty': ['No AUDIO consumption evidence.'],
            'consumption': {'video_evidence_available': True, 'audio_evidence_available': False},
            'raw_response': 'PRIVATE_TRANSPORT'}, 'reader_service')
        view = self.q.project(self.viewer, self.pid)
        self.assertEqual((view['phase'], view['state']), ('preparation', 'blocked'))
        self.assertEqual(view['results'], [])
        detail = self.q.artifact(self.viewer, self.pid, job['object_id'])
        self.assertEqual(detail['details']['preparation_failure'], {'code': 'invalid_media', 'provider_called': False})
        observed = self.q.artifact(self.viewer, self.pid, result['object_id'])
        self.assertEqual(observed['details']['answers'], result['body']['answers'])
        self.assertEqual(observed['details']['uncertainty'], result['body']['uncertainty'])
        self.assertFalse(observed['details']['consumption']['audio_evidence_available'])
        self.assertNotIn('PRIVATE_', json.dumps([detail, observed, view]))

    def test_generated_composed_finished_and_imported_media_remain_results(self):
        take = self.add('media', {'media_type': 'video/mp4', 'provenance': {'intent': {}}}, 'worker_service')
        composition = self.add('media', {'media_type': 'image/png', 'composition': {'manifest': {}},
            'derivative_of': self.p.ref(take)}, 'composition_service')
        imported = self.add('media', {'media_type': 'video/mp4',
            'import_status': 'imported-unverified'}, 'importer_service')
        view = self.q.project(self.viewer, self.pid)
        self.assertEqual(view['phase'], 'production')
        self.assertEqual({v['object_ref']['object_id'] for v in view['results']},
                         {o['object_id'] for o in [take, composition, imported]})
        cut = self.add('cut', {'segments': []}, 'cut_service')
        finished = self.add('media', {'source_cut': self.p.ref(cut),
            'media_type': 'video/mp4', 'dependencies': [self.p.ref(cut)]})
        forged_cut = self.add('cut', {'segments': []})
        forged = self.add('media', {'source_cut': self.p.ref(forged_cut), 'media_type': 'video/mp4'})
        view = self.q.project(self.viewer, self.pid)
        self.assertIn(self.p.ref(finished), [v['object_ref'] for v in view['results']])
        self.assertIn(self.p.ref(forged), [v['object_ref'] for v in view['plan']])
        self.assertFalse(view['confirmed_finals'])

    def test_resolution_without_live_checker_is_historical_unverified_not_accepted(self):
        pickup = self.add('pickup', {'status': 'unresolved', 'dependencies': []}, 'review_service')
        resolution = self.add('pickup-resolution', {'source_pickup': self.p.ref(pickup),
            'status': 'independent-review-pass', 'accepted': False, 'dependencies': [],
            'private_response': 'PRIVATE_WIRE'}, 'review_service')
        detail = self.q.artifact(self.viewer, self.pid, resolution['object_id'])
        self.assertFalse(detail['current'])
        self.assertTrue(detail['unverified'])
        self.assertEqual(detail['status'], 'resolution-unverified')
        self.assertFalse(detail['details']['accepted'])
        self.assertNotIn('PRIVATE_WIRE', json.dumps(detail))
        original = self.q.artifact(self.viewer, self.pid, pickup['object_id'])
        self.assertEqual(original['stored_status'], 'unresolved')
        self.assertEqual(original['resolution_status'], 'resolution-unverified')
        self.assertFalse(original['confirmed_final'])

    def test_repair_details_expose_actual_old_new_and_source_finding(self):
        base = self.add('shot', {'content': 'Before'})
        updated = self.add('shot', {'content': 'After'})
        failed = self.add('media', {'media_type': 'video/mp4'})
        pickup = self.add('pickup', {'target': self.p.ref(failed), 'status': 'unresolved'}, 'review_service')
        body = {'base': self.p.ref(base), 'result': self.p.ref(updated),
            'source_pickup': self.p.ref(pickup), 'failed_take': self.p.ref(failed),
            'observed_defect': 'Cart remained behind the marker.',
            'creative_path': ['content', 'Direction'], 'scope': 'section',
            'desired_change': 'Cart passes the marker.', 'accepted': False,
            'raw_request': 'PRIVATE_REPAIR_JOURNAL'}
        repair = self.add('repair', body, 'patch_service')
        detail = self.q.artifact(self.viewer, self.pid, repair['object_id'])['details']
        for key in ('base', 'result', 'source_pickup', 'failed_take', 'observed_defect', 'creative_path', 'desired_change'):
            self.assertEqual(detail[key], body[key])
        self.assertFalse(detail['accepted'])
        self.assertNotIn('PRIVATE_REPAIR_JOURNAL', json.dumps(detail))

    def test_local_composition_queue_is_visible_without_private_origin(self):
        job = self.add('composition-job', {'state': 'running', 'attempt': 1,
                       'lease': {'credential_id': 'PRIVATE_ORIGIN'}, 'origin': 'PRIVATE_ORIGIN'},
                       'composition_worker_service')
        project = self.q.project(self.viewer, self.pid)
        self.assertEqual((project['phase'], project['state']), ('production', 'processing'))
        self.assertIn(self.p.ref(job), [v['object_ref'] for v in project['results']])
        detail = self.q.artifact(self.viewer, self.pid, job['object_id'])
        self.assertEqual(detail['details'], {'state': 'running', 'attempt': 1})
        self.assertNotIn('PRIVATE_ORIGIN', json.dumps(detail))

    def test_minimal_project_views_and_current_plan_not_acceptance(self):
        self.assertEqual(self.q.list_projects(self.viewer), self.q.list_projects(self.actor))
        empty = self.q.project(self.viewer, self.pid)
        self.assertEqual((empty['phase'], empty['state']), ('preparation', 'empty'))
        scene = self.p.service.revise(self.actor, self.pid, self.p.request('scene', 'EP01/scene.md', 'He refuses the bowl.'))
        candidate = self.add('candidate', {'target':self.p.ref(scene), 'task':'shot', 'method_id':'mvgp-video-v1',
            'request':{'params':{'prompt':'The actual submitted prompt.', 'resolution':'1080p'},
                       'references':[{'object_ref':self.p.ref(scene),'role':'person','result_url':'https://cdn/x?token=bad'}]},
            'compilation':{'assembly':{'prompt':'The assembled source prompt.'},'files':{'large':'DO_NOT_DUMP'}},
            'context':{'private':'DO_NOT_DUMP'},'dependencies':[self.p.ref(scene)],'accepted':True}, 'compiler_service')
        view = self.q.project(self.viewer, self.pid)
        self.assertEqual(view['phase'], 'production')
        self.assertEqual(view['current_candidates'], [self.p.ref(candidate)])
        self.assertFalse(view['confirmed_finals'])
        self.assertNotIn('DO_NOT_DUMP',json.dumps(view))
        detail = self.q.artifact(self.viewer,self.pid,candidate['object_id'])
        self.assertEqual(detail['details']['request']['params']['prompt'],'The actual submitted prompt.')
        self.assertEqual(detail['details']['assembled_prompt'],'The assembled source prompt.')
        self.assertNotIn('result_url',json.dumps(detail))

    def test_private_service_records_use_strict_allowlists_even_old_revisions(self):
        for kind in ('provider-receipt','review-run','review-turn'):
            with self.subTest(kind=kind):
                record = self.add(kind, {'state':'unknown','status':'unknown','task_id':'task_1',
                    'result_url':'https://cdn.invalid/media.mp4?token=SIGNED_SENTINEL',
                    'download_reference':{'object_id':'SYSTEM_SECRET'},'messages':[{'content':'RAW_REVIEW_SECRET'}],
                    'authorization':{'key':'API_SECRET'},'request':{'image':'data:image/png;base64,BASE64_SENTINEL'},
                    'storage_path':'/home/example/private/file','response':{'secret':'RESPONSE_SECRET'}},'worker_service')
                self.store.append_revision(self.pid,record['object_id'],1,{'state':'completed'},'worker_service')
                detail = json.dumps(self.q.artifact(self.viewer,self.pid,record['object_id'],revision=1))
                for sentinel in ('SIGNED_SENTINEL','SYSTEM_SECRET','RAW_REVIEW_SECRET','API_SECRET','BASE64_SENTINEL','RESPONSE_SECRET','/Users/'):
                    self.assertNotIn(sentinel,detail)
        internal = self.add('review-context',{'governing':{'system':'PRIVATE_INSTRUCTIONS'}},'review_context_service')
        with self.assertRaises(DomainError):
            self.q.artifact(self.actor,self.pid,internal['object_id'])

    def test_independent_receipts_agent_selection_and_human_are_distinct(self):
        forged = self.add('review-receipt',{'verdict':'pass','issues':[],'approved':True})
        review = self.add('review-receipt',{'verdict':'uncertain','issues':['Cannot identify the rider.']},'review_service')
        selection = self.add('take-selection',{'take':self.p.ref(forged),'rationale':'Maker prefers this take.'},'review_selection_service')
        self.assertEqual(self.q.artifact(self.viewer,self.pid,forged['object_id'])['verdict'],'unverified')
        self.assertEqual(self.q.artifact(self.viewer,self.pid,review['object_id'])['attribution'],'independent-review')
        self.assertEqual(self.q.artifact(self.viewer,self.pid,selection['object_id'])['attribution'],'agent-selection')
        view = self.q.project(self.viewer,self.pid)
        self.assertEqual(view['state'],'blocked')
        self.assertEqual(len(view['blockers']),1)
        self.assertFalse(view['confirmed_finals'])
        fake_final = self.add('final',{'accepted':True,'media':self.p.ref(forged)})
        self.assertFalse(self.q.artifact(self.viewer,self.pid,fake_final['object_id'])['confirmed_final'])

    def test_lazy_tree_and_traversal_and_foreign_access(self):
        shot = self.p.service.revise(self.actor,self.pid,self.p.request('shot','EP01/S01/card.json',{'action':'Turn.'}))
        roots = self.q.tree(self.viewer,self.pid)
        self.assertEqual(roots,[{'name':'EP01','path':'EP01','type':'directory'}])
        self.assertEqual(self.q.tree(self.viewer,self.pid,'EP01'),[{'name':'S01','path':'EP01/S01','type':'directory'}])
        leaf = self.q.tree(self.viewer,self.pid,'EP01/S01')[0]
        self.assertEqual(leaf['object_ref'],self.p.ref(shot))
        for parent in ('../secret','/etc','EP01/../private'):
            with self.assertRaises((ValueError,DomainError)):
                self.q.tree(self.viewer,self.pid,parent)
        other = self.store.create_project('foreign',{'title':'Private'},'other')
        for action in (lambda:self.q.project(self.viewer,'foreign'),lambda:self.q.tree(self.viewer,'foreign'),
                       lambda:self.q.artifact(self.viewer,'foreign',other['object_id']),
                       lambda:self.q.copy_reference(self.viewer,'foreign',self.ref(other))):
            with self.assertRaises(DomainError):
                action()
        self.assertNotIn('foreign',json.dumps(self.q.list_projects(self.viewer)))
        self.auth.revoke(self.viewer.credential_id)
        with self.assertRaises(DomainError):
            self.q.list_projects(self.viewer)

    def test_historical_copy_reference_is_fixed_and_time_bound(self):
        source = self.add('media',{'probe':{'duration':5.0},'media_type':'video/mp4'},'worker_service')
        cut = self.add('cut',{'segments':[{'take':self.p.ref(source),'start_seconds':1.25,'duration_seconds':1.0,'cut_start_seconds':0.0}]},'cut_service')
        media = self.add('media',{'source_cut':self.p.ref(cut),'assembly_manifest':self.p.ref(cut),
            'probe':{'duration':1.0},'media_type':'video/mp4','dependencies':[self.p.ref(cut)]},'cut_service')
        payload = self.q.copy_reference(self.viewer,self.pid,self.ref(media),0.375)
        self.assertEqual(payload['source_mapping']['source_seconds'],1.625)
        self.assertEqual(payload['playback_seconds'],0.375)
        self.store.append_revision(self.pid,media['object_id'],1,media['body'],'cut_service')
        self.assertEqual(self.q.copy_reference(self.viewer,self.pid,self.ref(media),0.375),payload)
        self.assertTrue(self.q.artifact(self.viewer,self.pid,media['object_id'],1)['stale'])
        self.assertIn('revision=1',payload['link'])
        for seconds in (-1,2,float('nan'),float('inf')):
            with self.assertRaises(DomainError):
                self.q.copy_reference(self.viewer,self.pid,self.ref(media),seconds)
        external = self.add('media',media['body'],'uploader')
        self.assertIsNone(self.q.copy_reference(self.viewer,self.pid,self.ref(external),0.375)['source_mapping'])

    def test_import_and_plain_text_claims_never_become_acceptance(self):
        imported = self.add('media',{'label':'Passed all checks','import_status':'imported-unverified','accepted':True},'importer_service')
        view = self.q.artifact(self.viewer,self.pid,imported['object_id'])
        self.assertTrue(view['imported_unverified'])
        self.assertFalse(view['confirmed_final'])
        note = self.add('feedback',{'content':'See https://cdn.invalid/a.mp4?token=SECRET_QUERY and /home/example/secret/file',
            'token':'SECRET_VALUE','accepted':True})
        detail = json.dumps(self.q.artifact(self.viewer,self.pid,note['object_id']))
        for sentinel in ('SECRET_QUERY','SECRET_VALUE','/Users/'):
            self.assertNotIn(sentinel,detail)

    def test_recorded_non_current_and_job_origin_are_visible_without_private_request(self):
        target = self.add('shot',{'content':'Turn toward the rider.'})
        intent = self.add('dispatch-intent',{'operation':'submit','target':self.p.ref(target),'task':'shot',
            'method_id':'mvgp-video-v1','request':{'private':'PRIVATE_WIRE_SECRET'}},'submission_service')
        job = self.add('job',{'intent':self.p.ref(intent),'state':'succeeded','current':False,
            'last_error':'forbidden'},'worker_service')
        view = self.q.artifact(self.viewer,self.pid,job['object_id'])
        self.assertFalse(view['current'])
        self.assertEqual(view['details']['dispatch']['target'],self.p.ref(target))
        self.assertNotIn('PRIVATE_WIRE_SECRET',json.dumps(view))
        report = self.add('agent-report',{'target':self.p.ref(target),'observation':'I think it works.',
            'evidence':[],'attribution':'maker-assessment','accepted':True})
        public = self.q.artifact(self.viewer,self.pid,report['object_id'])
        self.assertEqual(public['details']['observation'],'I think it works.')
        self.assertFalse(public['confirmed_final'])

    def test_qualification_lessons_reopen_and_repair_lineage_are_visible_without_approval(self):
        asset = self.add('asset', {'content': 'Cook identity'})
        media = self.add('media', {'media_type': 'video/mp4', 'probe': {'duration': 1.0}})
        ref, video = self.p.ref(asset), self.p.ref(media)
        records = [
            self.add('qualification-set', {'asset': ref, 'samples': [video], 'context_refs': [ref], 'state': 'enrolled',
                'qualified': False, 'rationale': 'Assess this identity.', 'request': {'raw': 'PRIVATE_ENROLLMENT'},
                'dependencies': [ref]}, 'qualification_service'),
            self.add('qualification-sample', {'asset': ref, 'sample': video, 'sample_sha256': 'a'*64,
                'asset_references': [{'asset': ref, 'media': video, 'sha256': 'a'*64, 'role': 'subject', 'internal': 'PRIVATE_REFERENCE'}],
                'budget_identity': 'PRIVATE_BUDGET_IDENTITY', 'dependencies': [ref]}, 'qualification_service'),
            self.add('lesson-proposal', {'target': ref, 'evidence': [video], 'observation': 'Face changed while turning.',
                'proposed_change': 'Clarify the reference role.', 'status': 'proposed', 'operative': False,
                'private_draft': 'PRIVATE_LESSON', 'dependencies': [ref]}),
            self.add('reopen', {'lock': ref, 'targets': [ref], 'reason': 'Fix the continuity.', 'invalidated': [video],
                'requested_by': 'PRIVATE_ORIGIN'}, 'workflow_service'),
            self.add('repair-lineage', {'repair': ref, 'failed_take': video, 'returned_take': video,
                'accepted': False, 'raw_response': 'PRIVATE_REPAIR'}, 'patch_service')]
        project = self.q.project(self.viewer, self.pid)
        self.assertTrue({o['object_id'] for o in records} <= {v['object_ref']['object_id'] for v in project['results']})
        for record in records:
            detail = self.q.artifact(self.viewer, self.pid, record['object_id'])
            self.assertFalse(detail['confirmed_final'])
            self.assertNotIn('PRIVATE_', json.dumps(detail))
            self.assertEqual(self.q.copy_reference(self.viewer, self.pid, self.ref(record))['object_ref'], self.p.ref(record))
        qualification = self.q.artifact(self.viewer, self.pid, records[0]['object_id'])
        self.assertFalse(qualification['details']['qualified'])
        self.assertEqual(qualification['status'], 'enrolled')
        self.store.append_revision(self.pid, records[0]['object_id'], 1,
            {**records[0]['body'], 'state': 'enrolled', 'qualified': True, 'rationale': 'Author claims all passed.'}, 'author')
        self.assertFalse(self.q.artifact(self.viewer, self.pid, records[0]['object_id'])['details']['qualified'])
        self.assertEqual(self.q.artifact(self.viewer, self.pid, records[0]['object_id'], 1)['details']['rationale'], 'Assess this identity.')

    def test_batch_children_expose_only_exact_public_lineage_fields(self):
        target = self.add('shot', {'content': 'Turn.'})
        ref = self.p.ref(target)
        child = {'candidate': ref, 'target': ref, 'job': ref, 'resolution': '1080p', 'relation': 'rerun',
            'previous_attempts': [ref], 'cost': {'live_controls': 'PRIVATE_COST'}, 'review': {'verdict': 'PRIVATE_REVIEW'},
            'route_key': 'PRIVATE_ROUTE', 'reservation_id': 'PRIVATE_RESERVATION', 'idempotency_key': 'PRIVATE_IDEMPOTENCY'}
        batch = self.add('batch', {'state': 'queued', 'count': 1, 'children': [child],
            'budget_decision': {'unit': 'PRIVATE_POLICY'}, 'support': {'internal': 'PRIVATE_SUPPORT'}}, 'batch_service')
        detail = self.q.artifact(self.viewer, self.pid, batch['object_id'])
        expected = {key: child[key] for key in ('candidate', 'target', 'job', 'resolution', 'relation', 'previous_attempts')}
        self.assertEqual(detail['details'], {'state': 'queued', 'count': 1, 'children': [expected]})
        self.assertNotIn('PRIVATE_', json.dumps(detail))
        self.assertEqual(self.q.project(self.viewer, self.pid)['state'], 'processing')

    def test_decision_evidence_is_purpose_specific_and_keeps_exact_budget_values(self):
        target = self.add('media', {'media_type': 'video/mp4'})
        ref = self.p.ref(target)
        envelope = self.add('decision-request', {'target': ref, 'purpose': 'envelope', 'state': 'pending',
            'evidence': {'budget_before': {'ceiling': 100, 'spent': 20, 'reserved': 15, 'unit': 'synthetic_unit',
                                         'private_policy': 'PRIVATE_BEFORE'},
                'proposed_limit': 150, 'budget_unit': 'synthetic_unit', 'policy': 'PRIVATE_POLICY', 'receipts': ['PRIVATE_OTHER_PURPOSE']}}, 'decision_service')
        detail = self.q.artifact(self.viewer, self.pid, envelope['object_id'])['details']['evidence']
        self.assertEqual(detail, {'budget_before': {'ceiling': 100, 'spent': 20, 'reserved': 15, 'unit': 'synthetic_unit'},
                                  'proposed_limit': 150, 'budget_unit': 'synthetic_unit'})
        final = self.add('decision-request', {'target': ref, 'purpose': 'final', 'evidence': {
            'cut': ref, 'media': ref, 'receipts': [ref], 'finishing': [ref], 'policy_hash': 'PRIVATE_POLICY',
            'raw_response': 'PRIVATE_BODY', 'budget_before': {'ceiling': 999}}}, 'decision_service')
        self.assertEqual(self.q.artifact(self.viewer, self.pid, final['object_id'])['details']['evidence'],
                         {'cut': ref, 'media': ref, 'receipts': [ref], 'finishing': [ref]})
        self.assertNotIn('PRIVATE_', json.dumps(self.q.artifact(self.viewer, self.pid, final['object_id'])))

    def test_take_request_and_human_selection_project_only_public_evidence(self):
        shot = self.add('shot', {'content': 'Shot'})
        take = self.add('media', {'media_type': 'video/mp4'})
        shot_ref, take_ref = self.p.ref(shot), self.p.ref(take)
        status = {'take': take_ref, 'qualified': False, 'reason': 'review_required'}
        decision = self.add('decision-request', {'target': shot_ref, 'purpose': 'take', 'evidence': {
            'shot': {**shot_ref, 'raw_response': 'PRIVATE_SHOT'},
            'takes': [{**take_ref, 'raw_response': 'PRIVATE_TAKE'}],
            'qualification_status': [{**status, 'raw_response': 'PRIVATE_STATUS'}],
            'budget_before': {'ceiling': 999}, 'raw_response': 'PRIVATE_BODY'}}, 'decision_service')
        details = self.q.artifact(self.viewer, self.pid, decision['object_id'])['details']
        self.assertEqual(details['evidence'], {'shot': shot_ref, 'takes': [take_ref], 'qualification_status': [status]})
        receipt = self.add('human-receipt', {'verified_human_session': True}, 'decision_service')
        selection = self.add('human-take-selection', {'shot': shot_ref, 'take': take_ref, 'reason': 'Clearer motion.',
            'human_receipt': self.p.ref(receipt), 'verified_human_session': True,
            'human_credential': 'PRIVATE_CREDENTIAL', 'raw_response': 'PRIVATE_BODY'}, 'decision_service')
        view = self.q.artifact(self.viewer, self.pid, selection['object_id'])
        self.assertEqual(view['attribution'], 'human-selection')
        self.assertEqual(view['details']['human_receipt'], self.p.ref(receipt))
        self.assertEqual(view['details']['take'], take_ref)
        self.assertFalse(view['confirmed_final'])
        self.assertNotIn('PRIVATE_', json.dumps([details, view]))

    def test_review_feed_resolves_current_pick_by_creation_order_not_object_id(self):
        # the desk loads one feed; a re-pick must win even when its object_id sorts first.
        shot = self.add('shot', {'content': {'shot': 'S01-010A'}})
        takes = [self.add('media', {'media_type': 'video/mp4'}) for _ in range(3)]
        shot_ref = self.p.ref(shot)
        request = self.add('decision-request', {'target': shot_ref, 'purpose': 'take', 'state': 'pending', 'expires_at': 9e9,
            'evidence': {'shot': shot_ref, 'takes': [self.p.ref(t) for t in takes], 'raw_response': 'PRIVATE_BODY'}}, 'decision_service')
        receipt = self.add('human-receipt', {'verified_human_session': True}, 'decision_service')
        def select(object_id, take, author='decision_service'):
            return self.store.create_object(self.pid, 'human-take-selection', {'shot': shot_ref, 'take': self.p.ref(take),
                'human_receipt': self.p.ref(receipt), 'verified_human_session': True, 'human_credential': 'PRIVATE_CREDENTIAL'},
                author, object_id=object_id)
        first = select('obj_zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz', takes[0])
        second = select('obj_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', takes[1])
        select('obj_mmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmm', takes[2], author='author_1')  # an agent record is not a human pick
        job = self.add('job', {'state': 'running'}, 'submission_service')
        done = self.add('job', {'state': 'succeeded', 'result': self.p.ref(takes[2])}, 'worker_service')
        child = lambda j: {'candidate': shot_ref, 'target': shot_ref, 'job': self.p.ref(j)}
        live = self.add('batch', {'state': 'running', 'count': 2, 'children': [child(job), child(done)]}, 'batch_service')
        self.add('batch', {'state': 'completed', 'count': 1, 'children': [child(done)]}, 'batch_service')
        self.add('scene', {'content': 'Unrelated plan text.'})
        feed = self.q.review_feed(self.viewer, self.pid)
        self.assertEqual([r['object_ref'] for r in feed['requests']], [self.p.ref(request)])
        self.assertEqual([s['object_ref'] for s in feed['selections']], [self.p.ref(first), self.p.ref(second)])
        self.assertEqual(feed['picks'][shot['object_id']]['object_ref'], self.p.ref(second))
        self.assertEqual(feed['picks'][shot['object_id']]['details']['take'], self.p.ref(takes[1]))
        self.assertEqual(feed['shots'][f"{shot['object_id']}:1"]['details']['content'], {'shot': 'S01-010A'})
        self.assertEqual([m['batch']['object_ref'] for m in feed['making']], [self.p.ref(live)])
        self.assertEqual(feed['making'][0]['jobs'], {job['object_id']: 'running', done['object_id']: 'succeeded'})
        self.assertEqual(feed['making'][0]['ready'], {done['object_id']: self.p.ref(takes[2])})
        self.assertNotIn('PRIVATE_', json.dumps(feed))
        self.assertNotIn('Unrelated plan text.', json.dumps(feed))
        worker = self.auth.authenticate(self.auth.provision_token('worker', 'worker', [self.pid], 300))
        with self.assertRaises(DomainError):
            self.q.review_feed(worker, self.pid)

    def test_an_earlier_batch_stays_on_the_desk_after_the_card_is_patched(self):
        # the old batch and the pick stay visible; 收片中 still follows the current order.
        shot = self.add('shot', {'content': {'shot': 'S01-010A'}})
        take = self.add('media', {'media_type': 'video/mp4'})
        shot_ref = self.p.ref(shot)
        old = self.add('decision-request', {'target': shot_ref, 'purpose': 'take', 'state': 'declined', 'expires_at': 0,
            'evidence': {'shot': shot_ref, 'takes': [self.p.ref(take)]}, 'dependencies': [shot_ref]}, 'decision_service')
        self.store.append_revision(self.pid, shot['object_id'], shot['revision'], {**shot['body'], 'content': {'shot': 'S01-010A', 'v': 2}}, 'author_1')
        feed = self.q.review_feed(self.viewer, self.pid)
        views = {r['object_ref']['object_id']: r for r in feed['requests']}
        self.assertIn(old['object_id'], views)
        self.assertFalse(views[old['object_id']]['current'])

    def test_the_owners_notes_are_in_the_desk_feed_until_withdrawn(self):

        shot = self.add('shot', {'content': {'shot': 'S01-010A'}})
        shot_ref = self.p.ref(shot)
        note = self.add('owner-note', {'target': shot_ref, 'take': None, 'at_seconds': 1.5, 'text': '光再暖一点',
                                       'withdrawn': False, 'verified_human_session': True, 'human_actor': 'PRIVATE_ACTOR',
                                       'dependencies': [shot_ref]}, 'owner_note_service')
        self.add('owner-note', {'target': shot_ref, 'text': 'forged', 'withdrawn': False, 'verified_human_session': True},
                 'author_1')  # not written by the owner's session
        feed = self.q.review_feed(self.viewer, self.pid)
        self.assertEqual([n['details']['text'] for n in feed['notes']], ['光再暖一点'])
        self.assertEqual(feed['notes'][0]['attribution'], 'human-note')
        self.assertNotIn('PRIVATE_ACTOR', json.dumps(feed))
        self.store.append_revision(self.pid, note['object_id'], 1, {**note['body'], 'withdrawn': True}, 'owner_note_service')
        self.assertEqual(self.q.review_feed(self.viewer, self.pid)['notes'], [])

    def test_each_fal_draft_on_the_desk_carries_its_1080p_state(self):
        # 样片 → 正片生成中 → 正片, or 已过期 after seven days; the shapes are jobs.py's.
        import time as clock
        shot = self.add('shot', {'content': {'shot': 'S01-010A'}})
        shot_ref = self.p.ref(shot)
        later = int(clock.time()) + 86400
        def draft(expires):
            return self.add('media', {'media_type': 'video/mp4', 'provenance': {'provider_output': {
                'seed': 7, 'draft_id': 'draft_x', 'draft_expires_at': expires}}}, 'worker_service')
        waiting, making, ready, expired = draft(later), draft(later), draft(later), draft(int(clock.time()) - 1)
        plain = self.add('media', {'media_type': 'video/mp4'}, 'worker_service')  # an HF take has no 1080p step
        self.add('decision-request', {'target': shot_ref, 'purpose': 'take', 'state': 'pending', 'expires_at': 9e9,
            'evidence': {'shot': shot_ref, 'takes': [self.p.ref(t) for t in (waiting, making, ready, expired, plain)]},
            'dependencies': [shot_ref]}, 'decision_service')
        def completion(take, state, result=None):
            intent = self.add('dispatch-intent', {'operation': 'complete-draft', 'target': self.p.ref(take)}, 'submission_service')
            self.add('job', {'state': state, 'intent': self.p.ref(intent), 'result': result}, 'submission_service')
        completion(making, 'running')
        full = self.add('media', {'media_type': 'video/mp4', 'completes': self.p.ref(ready)}, 'worker_service')
        completion(ready, 'succeeded', self.p.ref(full))
        states = self.q.review_feed(self.viewer, self.pid)['completions']
        self.assertEqual({k: v['state'] for k, v in states.items()}, {waiting['object_id']: 'draft', making['object_id']: 'making',
                         ready['object_id']: 'ready', expired['object_id']: 'expired'})
        self.assertEqual(states[ready['object_id']]['take'], self.p.ref(full))
        self.assertEqual(states[waiting['object_id']]['expires_at'], later)

    def test_a_pick_that_stays_at_480p_says_why(self):
        # 预算不够，正片没做 / 正片失败，已重试 / 样片已过期.
        import time as clock
        shot = self.add('shot', {'content': {'shot': 'S01-020A'}})
        shot_ref = self.p.ref(shot)
        later = int(clock.time()) + 86400
        def draft(expires):
            return self.add('media', {'media_type': 'video/mp4', 'provenance': {'provider_output': {
                'seed': 7, 'draft_id': 'draft_y', 'draft_expires_at': expires}}}, 'worker_service')
        broke, retried, once, expired = draft(later), draft(later), draft(later), draft(int(clock.time()) - 1)
        self.add('decision-request', {'target': shot_ref, 'purpose': 'take', 'state': 'pending', 'expires_at': 9e9,
            'evidence': {'shot': shot_ref, 'takes': [self.p.ref(t) for t in (broke, retried, once, expired)]},
            'dependencies': [shot_ref]}, 'decision_service')
        def completion(take, state, attempt):
            intent = self.add('dispatch-intent', {'operation': 'complete-draft', 'target': self.p.ref(take)}, 'submission_service')
            self.add('job', {'state': state, 'intent': self.p.ref(intent), 'attempt': attempt}, 'submission_service')
        completion(retried, 'failed', 1)
        completion(retried, 'failed', 2)
        completion(once, 'failed', 1)
        self.store.append_event(self.pid, 'completion.stopped', {'take': self.p.ref(broke), 'code': 'budget_exceeded', 'reason': 'x'})
        states = self.q.review_feed(self.viewer, self.pid)['completions']
        self.assertEqual({k: (v['state'], v['note']) for k, v in states.items()}, {
            broke['object_id']: ('stopped', '预算不够，正片没做'), retried['object_id']: ('failed', '正片失败，已重试'),
            once['object_id']: ('failed', '正片没做成'), expired['object_id']: ('expired', '样片已过期')})

    def test_a_withdrawn_pick_reads_撤销_in_the_version_log(self):
        # (audit: a withdrawal was logged as 不行).
        ref = self.p.ref
        scene = self.add('scene', {'logical_path': 'PREP/scene.md', 'content': '# S01-DOOR'})
        shot = self.add('shot', {'content': {'shot': 'S01-030A'}, 'dependencies': [ref(scene)]})
        candidate = self.add('candidate', {'target': ref(shot), 'compilation': {'prompt': 'P'}}, 'compiler_service')
        intent = self.add('dispatch-intent', {'candidate': ref(candidate), 'dependencies': [ref(candidate)]}, 'submission_service')
        take = self.add('media', {'media_type': 'video/mp4', 'dependencies': [ref(intent)]}, 'worker_service')
        request = self.add('decision-request', {'target': ref(shot), 'purpose': 'take', 'state': 'confirmed', 'expires_at': 9e9,
            'evidence': {'shot': ref(shot), 'takes': [ref(take)]}, 'dependencies': [ref(shot)]}, 'decision_service')
        self.add('human-receipt', {'decision': ref(request), 'purpose': 'take', 'choice': 'confirm', 'reason': None}, 'decision_service')
        withdrawn = self.store.append_revision(self.pid, request['object_id'], request['revision'],
                                               {**request['body'], 'state': 'pending'}, 'decision_service')
        self.add('human-receipt', {'decision': ref(withdrawn), 'purpose': 'take', 'choice': 'decline', 'reason': '换一条'}, 'decision_service')
        folder = next(f for f in self.q.project_tree(self.viewer, self.pid)['folders'] if f['id'] == 'shot:' + shot['object_id'])
        self.assertEqual([(v['verdict'], v['reason']) for v in folder['log']], [('撤销', '换一条')])

    def test_a_recreation_shot_carries_its_source_segment(self):
        # the desk and the project page play the source shot next to the takes.
        clip = self.add('media', {'media_type': 'video/mp4', 'probe': {'duration': 20.0, 'has_video': True}})
        clip_ref = self.p.ref(clip)
        understanding = self.add('source-understanding', {'content': {'type': 'source-understanding', 'source': clip_ref,
            'start_seconds': 3.5, 'end_seconds': 7.0}, 'dependencies': [clip_ref]})
        scene = self.add('scene', {'content': '# S01', 'dependencies': [self.p.ref(understanding)]})
        shot = self.add('shot', {'content': {'shot': 'S01-010A'}, 'dependencies': [self.p.ref(scene)]})
        take = self.add('media', {'media_type': 'video/mp4'})
        shot_ref = self.p.ref(shot)
        self.add('decision-request', {'target': shot_ref, 'purpose': 'take', 'state': 'pending', 'expires_at': 9e9,
            'evidence': {'shot': shot_ref, 'takes': [self.p.ref(take)]}, 'dependencies': [shot_ref]}, 'decision_service')
        feed = self.q.review_feed(self.viewer, self.pid)
        source = feed['sources'][f"{shot['object_id']}:1"]
        self.assertEqual((source['media'], source['start_seconds'], source['end_seconds']), (clip_ref, 3.5, 7.0))
        tree = self.q.project_tree(self.viewer, self.pid)
        first = next(i for i in tree['items'] if i['folder'] == 'shot:' + shot['object_id'])
        self.assertEqual(first['name'], '原片 3.5–7 秒')
        self.assertEqual((first['details']['source_start'], first['details']['source_end']), (3.5, 7.0))

    def test_project_stage_follows_hf_chapters(self):
        # 开发 → 前期 → 拍摄 → 后期 (HF_CANONICAL.md §9).
        stage = lambda: self.q.project_tree(self.viewer, self.pid)['stage']
        self.assertEqual(stage(), '开发')
        self.add('asset', {'content': {'type': 'asset', 'role': 'visual', 'tag': '@ann'}})
        self.assertEqual(stage(), '前期')
        self.add('candidate', {'task': 'shot'}, 'compiler_service')
        self.assertEqual(stage(), '拍摄')
        self.add('cut', {'segments': []}, 'cut_service')
        self.assertEqual(stage(), '后期')
        self.assertEqual([p['stage'] for p in self.q.list_projects(self.viewer)], ['后期'])

    def test_shot_rows_and_version_log_with_owner_verdict(self):
        # HF brief outline, shotlist row, version / what changed / verdict, 10–15 advice.
        shot = self.add('shot', {'content': {'shot': 'S01-010A', 'Camera': {'shot size': 'MCU', 'lens': '50mm'},
            'The material': {'the running time in seconds': 6}, 'Direction': {'the goal of the shot in one line': '她停下'}}})
        shot_ref = self.p.ref(shot)
        takes = []
        for n in range(11):
            cand = self.add('candidate', {'target': shot_ref, 'task': 'shot', 'compilation': {'job_type': 'fal_seedance_2_5',
                'authorship': {'change_note': f'改了第 {n + 1} 处'}}}, 'compiler_service')
            takes.append(self.add('media', {'media_type': 'video/mp4', 'dependencies': [self.p.ref(cand)]}, 'worker_service'))
        request = self.add('decision-request', {'target': shot_ref, 'purpose': 'take', 'state': 'declined',
            'evidence': {'takes': [self.p.ref(takes[0])]}}, 'decision_service')
        self.add('human-receipt', {'decision': self.p.ref(request), 'purpose': 'take', 'choice': 'decline',
            'reason': '再拍一批：光太暗', 'verified_human_session': True}, 'decision_service')
        tree = self.q.project_tree(self.viewer, self.pid)
        folder = next(f for f in tree['folders'] if f['id'] == 'shot:' + shot['object_id'])
        self.assertEqual((folder['shot']['size'], folder['shot']['versions'], folder['shot']['status']), ('MCU', 11, '待选'))
        self.assertEqual((folder['log'][0]['change_note'], folder['log'][0]['verdict'], folder['log'][0]['reason']),
                         ('改了第 1 处', '不行', '再拍一批：光太暗'))
        self.assertIn('10–15', folder['advice'])
        self.assertEqual(tree['brief']['tools'], ['fal_seedance_2_5'])
        self.assertEqual(tree['brief']['production']['版本'], 11)

    def test_a_generated_asset_image_lands_in_its_assets_folder(self):

        asset = self.add('asset', {'content': {'type': 'asset', 'role': 'visual', 'tag': '@lin', 'category': 'character'}})
        cand = self.add('candidate', {'target': self.p.ref(asset), 'task': 'image', 'compilation': {'job_type': 'apilio_gpt_image_2_5',
            'prompt': 'A grey character sheet.', 'parameters': {'aspect_ratio': '16:9', 'resolution': '2k'}}}, 'compiler_service')
        self.add('media', {'media_type': 'image/png', 'dependencies': [self.p.ref(cand)]}, 'worker_service')
        tree = self.q.project_tree(self.viewer, self.pid)
        item = next(i for i in tree['items'] if i['folder'] == 'assets/characters' and i['kind'] == 'image')
        self.assertEqual(item['name'], '@lin · 第 1 条')
        self.assertEqual(item['details']['model'], 'apilio_gpt_image_2_5')

    def test_a_record_view_carries_when_its_revision_was_written(self):
        # the desk card shows the request's time, as in design v6.
        scene = self.add('scene', {'content': '# S01'})
        artifact = self.q.artifact(self.viewer, self.pid, scene['object_id'])
        self.assertRegex(artifact['created_at'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}')
        time.sleep(0.02)  # a later revision (a pick, a withdrawal) must not move the card's time
        second = self.store.append_revision(self.pid, scene['object_id'], 1, {'content': '# S01 v2'}, 'author')
        first = self.store.get_object(self.pid, scene['object_id'], revision=1)
        self.assertNotEqual(first['created_at'], second['created_at'])
        self.assertEqual(self.q.artifact(self.viewer, self.pid, scene['object_id'])['created_at'], first['created_at'])

    def test_project_tree_finds_takes_through_their_dispatch_intent(self):
        # live takes depend on the dispatch intent, not the candidate.
        ref = self.p.ref
        scene = self.add('scene', {'logical_path': 'PREP/scene.md', 'content': '# S01-INTERROGATION'})
        shot = self.add('shot', {'content': {'shot': 'S01-010A'}, 'dependencies': [ref(scene)]})
        candidate = self.add('candidate', {'target': ref(shot), 'compilation': {'assembly': {'prompt': 'PROMPT'}}}, 'compiler_service')
        intent = self.add('dispatch-intent', {'candidate': ref(candidate), 'dependencies': [ref(candidate)]}, 'submission_service')
        takes = [self.add('media', {'media_type': 'video/mp4', 'dependencies': [ref(intent)]}, 'worker_service') for _ in range(4)]
        tree = self.q.project_tree(self.viewer, self.pid)
        videos = [i for i in tree['items'] if i['folder'] == 'shot:' + shot['object_id'] and i['kind'] == 'video']
        self.assertEqual([i['object_ref']['object_id'] for i in videos], [t['object_id'] for t in takes])
        self.assertEqual([i['name'] for i in videos], ['第 1 条', '第 2 条', '第 3 条', '第 4 条'])

    def test_a_take_keeps_the_number_the_desk_showed_when_takes_finish_out_of_order(self):
        # live: fal finished the desk's take 1 third, and the project page called it 第 3 条.
        ref = self.p.ref
        scene = self.add('scene', {'content': '# S01'})
        shot = self.add('shot', {'content': {'shot': 'S01-010A'}, 'dependencies': [ref(scene)]})
        candidate = self.add('candidate', {'target': ref(shot), 'compilation': {'assembly': {'prompt': 'PROMPT'}}}, 'compiler_service')
        intent = self.add('dispatch-intent', {'candidate': ref(candidate), 'dependencies': [ref(candidate)]}, 'submission_service')
        takes = [self.add('media', {'media_type': 'video/mp4', 'dependencies': [ref(intent)]}, 'worker_service') for _ in range(3)]
        offered = [takes[2], takes[0], takes[1]]  # the order the desk shows
        self.add('decision-request', {'target': ref(shot), 'purpose': 'take', 'state': 'pending', 'expires_at': 9e9,
                 'evidence': {'shot': ref(shot), 'takes': [ref(t) for t in offered]}, 'dependencies': [ref(shot)]}, 'decision_service')
        names = {i['object_ref']['object_id']: i['name'] for i in self.q.project_tree(self.viewer, self.pid)['items']
                 if i['folder'] == 'shot:' + shot['object_id']}
        self.assertEqual([names[t['object_id']] for t in offered], ['第 1 条', '第 2 条', '第 3 条'])

    def test_a_take_shows_the_manuals_version_the_writer_sections_and_the_change(self):
        # (hell-grind.txt:106: every version logs what changed).
        ref = self.p.ref
        scene = self.add('scene', {'logical_path': 'PREP/scene.md', 'content': '# S01-DOOR'})
        shot = self.add('shot', {'content': {'shot': 'S01-010A'}, 'dependencies': [ref(scene)]})
        authored = self.add('candidate', {'target': ref(shot), 'compilation': {'prompt': 'P2', 'authorship': {
            'playbook_version': 'pb-0123456789ab', 'change_note': 'She stops one step earlier.', 'written': 5, 'variable': 5}}},
            'compiler_service')
        old = self.add('candidate', {'target': ref(shot), 'compilation': {'prompt': 'P1'}}, 'compiler_service')
        for candidate in (authored, old):
            intent = self.add('dispatch-intent', {'candidate': ref(candidate), 'dependencies': [ref(candidate)]}, 'submission_service')
            self.add('media', {'media_type': 'video/mp4', 'dependencies': [ref(intent)]}, 'worker_service')
        tree = self.q.project_tree(self.viewer, self.pid)
        new, before = [i['details'] for i in tree['items'] if i['folder'] == 'shot:' + shot['object_id']]
        self.assertEqual((new['playbook_version'], new['writer'], new['change_note']),
                         ('pb-0123456789ab', '5/5', 'She stops one step earlier.'))
        self.assertEqual((before.get('playbook_version'), before.get('writer'), before.get('change_note')), (None, None, None))

    def test_a_take_shows_the_writer_text_with_each_platform_addition_marked(self):
        # (owner 2026-09-26 "你要确保做到位"): the desk proves sent = writer text + additions.
        from production import prompt as prompts
        ref = self.p.ref
        scene = self.add('scene', {'logical_path': 'PREP/scene.md', 'content': '# S01-DOOR'})
        shot = self.add('shot', {'content': {'shot': 'S01-010A'}, 'dependencies': [ref(scene)]})
        written = 'ACTIVE REFERENCES\n@ann\n\n50mm. 0-4s | @ann stops at the door.'
        assets = {'ann': {'descriptor': 'a woman in a grey coat', 'image': 'img'}}
        sent, _, additions = prompts.build(written, assets, {}, None, 4, "every")
        constants = prompts.constants(assets, {}, 4)
        def candidate(prompt):
            return self.add('candidate', {'target': ref(shot), 'request': {'references': [{'n': 1, 'tag': 'ann'}]},
                'compilation': {'prompt': prompt, 'writer_text': written, 'additions': additions, 'prompt_constants': constants,
                                'authorship': {'playbook_version': 'pb-0123456789ab', 'change_note': 'First.'}}}, 'compiler_service')
        for c in (candidate(sent), candidate(sent + ' Tampered.')):
            intent = self.add('dispatch-intent', {'candidate': ref(c), 'dependencies': [ref(c)]}, 'submission_service')
            self.add('media', {'media_type': 'video/mp4', 'dependencies': [ref(intent)]}, 'worker_service')
        tree = self.q.project_tree(self.viewer, self.pid)
        good, bad = [i['details'] for i in tree['items'] if i['folder'] == 'shot:' + shot['object_id']]
        self.assertEqual(good['prompt'], sent)
        self.assertEqual(good['additions'], len(additions))
        self.assertIs(good['sent_is_writer_plus_additions'], True)
        self.assertEqual([sent[a:b] for a, b, _ in good['added']], [a['text'] for a in additions])
        self.assertEqual([k for _, _, k in good['added']], [a['kind'] for a in additions])
        self.assertIs(bad['sent_is_writer_plus_additions'], False)

    def test_project_tree_places_records_like_the_hf_project_page(self):
        # (owner 2026-09-24 "应该能在我们的平台上看到 … 展现的方式也可以是HF那种").
        ref = self.p.ref
        self.add('script', {'logical_path': 'story/script.md', 'content': 'INT. ROOM\nANN: 等等。'})
        look = self.add('asset', {'logical_path': 'assets/look/warm.json', 'content': {'type': 'asset', 'role': 'look', 'tag': '@warm'}})
        room = self.add('asset', {'logical_path': 'assets/planned/room.json', 'content': {'type': 'asset', 'role': 'world', 'tag': '@room'}})
        portrait = self.add('media', {'media_type': 'image/png', 'logical_path': 'assets/cast/ann.png'}, 'uploader')
        ann = self.add('asset', {'logical_path': 'assets/cast/ann.json', 'content': {'type': 'asset', 'role': 'visual', 'tag': '@ann',
                                 'media_refs': [ref(portrait)]}})
        voice = self.add('asset', {'logical_path': 'assets/performance/ann-voice.json', 'content': {'type': 'asset', 'role': 'voice', 'tag': '@ann'}})
        cup = self.add('asset', {'logical_path': 'assets/planned/cup.json', 'content': {'type': 'asset', 'role': 'visual', 'tag': '@cup'}})
        self.add('asset-selection', {'logical_path': 'assets/selections/x.json', 'content': {'type': 'asset-selection'}})
        scene = self.add('scene', {'logical_path': 'PREP/scene.md', 'content': '# S03-DOOR\n## 镜头清单\n'})
        shot = self.add('shot', {'content': {'shot': 'S03-010A'}, 'dependencies': [ref(scene)]})
        old_candidate = self.add('candidate', {'target': ref(shot), 'compilation': {'assembly': {'prompt': 'PROMPT ONE'}}}, 'compiler_service')
        old_take = self.add('media', {'media_type': 'video/mp4', 'dependencies': [ref(old_candidate)]}, 'worker_service')
        shot = self.store.append_revision(self.pid, shot['object_id'], 1, {'content': {'shot': 'S03-010A', 'note': 'v2'},
                                          'dependencies': [ref(scene)]}, 'author')
        candidate = self.add('candidate', {'target': ref(shot), 'compilation': {'prompt': 'WIRE TWO', 'job_type': 'seedance_2_5',
            'parameters': {'duration': 15, 'aspect_ratio': '21:9', 'resolution': '1080p'}, 'assembly': {'prompt': '# S03\nPROMPT TWO'}}},
            'compiler_service')
        takes = [self.add('media', {'media_type': 'video/mp4', 'dependencies': [ref(candidate)]}, 'worker_service') for _ in range(2)]
        receipt = self.add('human-receipt', {'verified_human_session': True}, 'decision_service')
        self.add('human-take-selection', {'shot': ref(shot), 'take': ref(takes[1]), 'human_receipt': ref(receipt),
                                          'verified_human_session': True}, 'decision_service')
        self.add('review-receipt', {'target': ref(takes[0]), 'purpose': 'take', 'role': 'director', 'verdict': 'fail',
                                    'structured_verdict': {'summary': 'She never stops at the door.'}}, 'review_service')
        cut = self.add('cut', {'segments': [], 'intent': 'Owner picks in shot order'}, 'cut_service')
        film = self.add('media', {'media_type': 'video/mp4', 'source_cut': ref(cut)}, 'cut_service')
        self.add('job', {'state': 'succeeded', 'result': ref(takes[0]), 'secret': 'PRIVATE_JOB'}, 'worker_service')
        stress_shot = self.add('shot', {'content': {'shot': 'S01-901A'}})
        stress = self.add('candidate', {'target': ref(stress_shot), 'task': 'stress',
                                        'compilation': {'assembly': {'prompt': 'STRESS'}}}, 'compiler_service')
        stress_take = self.add('media', {'media_type': 'video/mp4', 'dependencies': [ref(stress)]}, 'worker_service')
        tree = self.q.project_tree(self.viewer, self.pid)
        folders = {f['id']: f for f in tree['folders']}
        # the HF skeleton, grid items are images and videos only.
        for fixed in ('assets', 'assets/characters', 'assets/locations', 'assets/props', 'tests', 'film'):
            self.assertIn(fixed, folders)
        self.assertNotIn('retakes', folders)  # takes stay with their shot (bug hunt)
        self.assertNotIn('story', folders)
        self.assertNotIn('assets/style', folders)
        at = lambda folder: [i for i in tree['items'] if i['folder'] == folder]
        self.assertEqual({i['kind'] for i in tree['items']} - {'image', 'video', 'placeholder'}, set())
        characters = at('assets/characters')
        self.assertEqual([(i['name'], i['kind'], i['object_ref']['object_id']) for i in characters], [('@ann', 'image', portrait['object_id'])])
        self.assertEqual([(i['name'], i['kind']) for i in at('assets/locations')], [('@room', 'placeholder')])
        self.assertEqual([(i['name'], i['kind']) for i in at('assets/props')], [('@cup', 'placeholder')])
        self.assertEqual([n['name'] for n in tree['notes']], ['剧本', '风格 @warm'])
        self.assertEqual(folders['scene:' + scene['object_id']]['name'], 'S03-DOOR')
        shot_folder = 'shot:' + shot['object_id']
        self.assertEqual((folders[shot_folder]['parent'], folders[shot_folder]['name']), ('scene:' + scene['object_id'], 'S03-010A'))
        items = at(shot_folder)
        self.assertEqual([i['kind'] for i in items], ['video', 'video', 'video'])
        self.assertEqual([i['name'] for i in items], ['第 1 版 · 第 1 条', '第 2 版 · 第 1 条', '第 2 版 · 第 2 条'])
        self.assertEqual(items[0]['object_ref']['object_id'], old_take['object_id'])
        self.assertEqual(items[0]['details']['prompt'], 'PROMPT ONE')  # older records fall back to the assembly
        items = items[1:]
        self.assertEqual(items[0]['details']['prompt'], 'WIRE TWO')  # the text that was sent
        self.assertEqual((items[0]['details']['model'], items[0]['details']['duration'], items[0]['details']['aspect_ratio']),
                         ('seedance_2_5', 15, '21:9'))
        self.assertEqual([i['details']['picked'] for i in items], [False, True])
        self.assertNotIn('director', items[0]['details'])  # no AI director notes
        unshot_probe = self.add('shot', {'logical_path': 'probes/S01-P09/card.json', 'content': {'shot': 'S01-909A'}})
        tree = self.q.project_tree(self.viewer, self.pid)
        folders = {f['id']: f for f in tree['folders']}
        self.assertEqual(folders['shot:' + unshot_probe['object_id']]['parent'], 'tests')  # a probe card is a test even unshot
        self.assertEqual(folders['shot:' + stress_shot['object_id']]['parent'], 'tests')
        self.assertEqual([i['object_ref']['object_id'] for i in at('shot:' + stress_shot['object_id'])], [stress_take['object_id']])
        self.assertEqual([i['object_ref']['object_id'] for i in at('film')], [film['object_id']])
        self.assertEqual(folders[shot_folder]['count'], 3)
        self.assertNotIn('{"', json.dumps([i['name'] for i in tree['items']]))
        dumped = json.dumps(tree)
        self.assertNotIn('PRIVATE_', dumped)
        self.assertNotIn('asset-selection', dumped)
        self.assertNotIn('"job"', dumped)
        worker = self.auth.authenticate(self.auth.provision_token('worker', 'worker', [self.pid], 300))
        with self.assertRaises(DomainError):
            self.q.project_tree(worker, self.pid)

    def test_generation_record_says_what_was_made_where_when_and_what_it_cost(self):
        # (owner: 生成了啥，价格啊，这个片子在哪啊，什么时间啊).
        ref = self.p.ref
        self.store.set_budget(self.pid, 1000, 'hf_credit', budget_key='hf_owner')
        self.store.set_budget(self.pid, 10**9, 'apilio_quota', budget_key='apilio')
        scene = self.add('scene', {'content': '# S03-DOOR\n'})
        shot = self.add('shot', {'content': {'shot': 'S03-010A'}, 'dependencies': [ref(scene)]})
        candidate = self.add('candidate', {'target': ref(shot), 'request': {'job_type': 'seedance_2_5', 'params': {'duration': 8}}},
                             'compiler_service')
        takes = []
        for n, (state, actual) in enumerate((('succeeded', 56), ('failed', 0), ('unknown', None))):
            intent = self.add('dispatch-intent', {'operation': 'submit', 'candidate': ref(candidate), 'target': ref(shot),
                              'task': 'shot', 'request': {'job_type': 'seedance_2_5', 'params': {'prompt': 'PRIVATE_PROMPT'}}}, 'submission_service')
            # Same shape as a live take: it depends on its dispatch intent (production/jobs.py:368).
            take = self.add('media', {'media_type': 'video/mp4', 'dependencies': [ref(intent)]}, 'worker_service') if state == 'succeeded' else None
            if take: takes.append(take)
            self.add('job', {'intent': ref(intent), 'state': state, **({'result': ref(take)} if take else {}),
                             'remote_job_id': 'PRIVATE_REMOTE'}, 'submission_service')
            self.store.reserve(self.pid, f'r{n}', 60, 'hf_credit', object_id=intent['object_id'], budget_key='hf_owner')
            if actual is not None:
                self.store.settle(self.pid, f'r{n}', actual)
        task = self.add('review-task', {'purpose': 'take', 'role': 'director', 'route_profile_id': 'apilio-gemini-director-v1',
                                        'state': 'completed', 'target': ref(takes[0])}, 'review_service')
        self.store.reserve(self.pid, 'r-review', 1000, 'apilio_quota', object_id=task['object_id'], budget_key='apilio')
        record = self.q.generation_record(self.viewer, self.pid)
        rows = record['rows']
        self.assertEqual([r['status'] for r in rows], ['成功', '失败', '结果不明', '成功'])
        first = rows[0]
        self.assertEqual((first['what'], first['model'], first['location'], first['media']),
                         ('片子', 'seedance_2_5', {'scene': 'S03-DOOR', 'shot': 'S03-010A', 'take': 1}, ref(takes[0])))
        self.assertEqual(first['cost'], {'amount': 56, 'unit': 'hf_credit', 'settled': True})
        self.assertEqual(rows[1]['cost'], {'amount': 0, 'unit': 'hf_credit', 'settled': True})
        self.assertEqual(rows[2]['cost'], {'amount': 60, 'unit': 'hf_credit', 'settled': False})
        self.assertEqual((rows[3]['what'], rows[3]['model'], rows[3]['location']['shot']), ('导演看片', 'apilio-gemini-director-v1', 'S03-010A'))
        self.assertTrue(all(isinstance(r['time'], str) for r in rows))
        self.assertEqual(record['totals']['hf_owner'], {'unit': 'hf_credit', 'settled': 56, 'unsettled_max': 60, 'count': 3})
        self.assertEqual(record['by_model']['seedance_2_5']['count'], 3)
        self.assertNotIn('PRIVATE_', json.dumps(record))
        worker = self.auth.authenticate(self.auth.provision_token('worker', 'worker', [self.pid], 300))
        with self.assertRaises(DomainError):
            self.q.generation_record(worker, self.pid)

    def test_worker_and_scoped_reviewer_cannot_use_viewer_as_a_scope_escape(self):
        record = self.add('scene',{'content':'Private scene.'})
        token = self.auth.provision_token('judge','reviewer',[self.pid],300,
            targets=[record['object_id']],review_task_id='task_1')
        reviewer = self.auth.authenticate(token)
        worker = self.auth.authenticate(self.auth.provision_token('worker','worker',[self.pid],300))
        for actor in (reviewer,worker):
            with self.assertRaises(DomainError):
                self.q.project(actor,self.pid)
            with self.assertRaises(DomainError):
                self.q.artifact(actor,self.pid,record['object_id'])


class ConfirmedQueriesTests(unittest.TestCase):
    def test_real_human_confirmation_becomes_historical_after_reopen_and_new_cut(self):
        d = test_decisions.DecisionTests()
        d.setUp()
        self.addCleanup(d.doCleanups)
        q = Queries(d.store,d.f.auth,d.f.flow)
        d.store.set_budget(d.pid, 10, 'synthetic_unit')
        envelope = d.service.request(d.actor, d.pid, d.request(purpose='envelope', proposed_limit=20,
            budget_unit='synthetic_unit', idempotency_key='budget-view'))
        evidence = q.artifact(d.actor, d.pid, envelope['object_id'])['details']['evidence']
        self.assertEqual(evidence, {'budget_before': {'ceiling': 10, 'spent': 0, 'reserved': 0, 'unit': 'synthetic_unit', 'budget_key':'legacy'},
                                   'proposed_limit': 20, 'budget_unit': 'synthetic_unit', 'budget_key':'legacy'})
        pending = d.service.request(d.actor,d.pid,d.request())
        final_evidence = q.artifact(d.actor, d.pid, pending['object_id'])['details']['evidence']
        self.assertEqual(final_evidence['media'], d.ref(d.media).model_dump())
        self.assertEqual(final_evidence['cut'], d.ref(d.cut).model_dump())
        self.assertNotIn('policy_hash', final_evidence)
        d.service.decide(d.human,d.pid,d.answer(pending),origin='https://studio.example')
        final = d.store.list_objects(d.pid,kind='final')[0]
        self.assertEqual(q.project(d.actor,d.pid)['confirmed_finals'],[d.ref(final).model_dump()])
        lock = d.store.list_objects(d.pid,kind='picture-lock')[0]
        d.f.flow.reopen(d.actor,d.pid,d.ref(lock),[d.ref(d.cut)],'Revise the cut timing.')
        self.assertFalse(q.project(d.actor,d.pid)['confirmed_finals'])
        new_cut = d.c.service.create(d.actor,d.pid,d.c.request(idempotency_key='revised',expected_revision=1,intent='New ending.'))
        view = q.project(d.actor,d.pid)
        self.assertEqual(view['current_cuts'],[d.ref(new_cut).model_dump()])
        self.assertFalse(view['confirmed_finals'])
        self.assertTrue(q.artifact(d.actor,d.pid,final['object_id'])['stale'])
        self.assertEqual(q.artifact(d.actor,d.pid,d.cut['object_id'],1)['object_ref'],d.ref(d.cut).model_dump())


if __name__ == '__main__':
    unittest.main()


class CompletionTreeTests(unittest.TestCase):
    """The 1080p completion sits beside its draft in the shot folder."""
    @classmethod
    def setUpClass(cls):
        from production.tests.test_submissions import FalCompletionTests
        FalCompletionTests.setUpClass()
        cls.videos = (FalCompletionTests.draft_video, FalCompletionTests.complete_video)

    def test_the_completion_is_named_after_its_draft_as_its_正片(self):
        from production.tests.test_submissions import FalCompletionTests
        fal = FalCompletionTests()
        fal.draft_video, fal.complete_video = self.videos
        fal.setUp()
        self.addCleanup(fal.doCleanups)
        full = fal.completed(fal.takes[0])
        viewer = fal.f.auth.authenticate(fal.f.auth.provision_token('viewer', 'viewer', [fal.pid], 300))
        tree = Queries(fal.store, fal.f.auth, fal.f.flow).project_tree(viewer, fal.pid)
        folder = 'shot:' + fal.shot['object_id']
        names = {i['object_ref']['object_id']: i['name'] for i in tree['items'] if i['folder'] == folder}
        self.assertEqual(names[fal.takes[0]['object_id']], '第 1 条')
        self.assertEqual(names[full['object_id']], '第 1 条 · 正片')
