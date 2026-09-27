"""Batch atomicity uses real compilation, submission, job and selection services."""
import copy
import io
import subprocess
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import MagicMock, PropertyMock, patch

from PIL import Image
from pydantic import ValidationError

from production.batches import Batches
from production.contracts import BatchRequest, DomainError, ObjectRef, SelectTakeRequest
from production.jobs import Download, Jobs, SafeDownloader
from production.provider_types import ProviderReceipt
from production.reviews import Reviews
from production.tests import test_submissions
from production.worker import Worker


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.s = test_submissions.SubmissionTests()
        self.s.setUp()
        self.addCleanup(self.s.doCleanups)
        self.f, self.store, self.pid, self.actor = self.s.f, self.s.store, self.s.pid, self.s.actor
        self.reviews = Reviews(self.store,self.f.auth,self.f.flow,self.f.media)
        self.service = Batches(self.store,self.f.auth,self.f.flow,self.s.service,self.reviews)
        self.candidates = [self.s.candidate]
        self.worker = self.f.auth.authenticate(self.f.auth.provision_token('batch_worker','worker',[self.pid],300))

    def second(self):
        target = self.f.draft(self.f.definition(description='A different worried character'))
        method = self.f.choice(target,'image')
        candidate = self.s.c.compiler.prepare(self.actor,self.pid,self.s.c.request(target,method=method,key='second'))
        self.candidates.append(candidate)
        return candidate

    def request(self,key='batch',candidates=None):
        return BatchRequest(idempotency_key=key,expected_revision=self.store.get_object(self.pid,self.pid)['revision'],
            candidate_ids=[c['object_id'] for c in (self.candidates if candidates is None else candidates)])

    def assert_empty(self):
        for kind in ('batch','job','dispatch-intent','provider-attempt'):
            self.assertEqual(self.store.list_objects(self.pid,kind=kind),[])
        self.assertEqual(self.store.budget(self.pid)['reserved'],0)

    def finish(self,child,*,success=True,raw=None,mime='image/png'):
        if raw is None:
            file = io.BytesIO()
            Image.new('RGB',(2048,1152),'red').save(file,format='PNG')
            raw = file.getvalue()
        @contextmanager
        def download(url,host,ip,timeout):
            yield Download(mime,[raw])
        downloader = SafeDownloader({'cdn.example'},resolver=lambda host:['8.8.8.8'],transport=download)
        jobs = Jobs(self.store,self.f.auth,self.s.service,self.f.media,downloader=downloader,lease_seconds=60)
        claim = jobs.claim(self.worker,self.pid,child['job']['object_id'])
        dispatch = jobs.begin_dispatch(self.worker,self.pid,self.f.ref(claim['job']),claim['fence'])
        job_type = dispatch['intent']['body']['request']['job_type']
        remote_id = 'remote-'+child['job']['object_id']
        receipt = ProviderReceipt(remote_id,job_type,'completed' if success else 'failed','succeeded' if success else 'failed',
            {'id':remote_id,'status':'completed' if success else 'failed'}, {}, {},
            'https://cdn.example/result' if success else None,8)
        received = jobs.record_receipt(self.worker,self.pid,self.f.ref(dispatch['job']),dispatch['fence'],receipt)
        return jobs.download_result(self.worker,self.pid,self.f.ref(received),dispatch['fence']) if success else received

    def native_candidates(self, *, same_unit=True):
        image_cost = {**copy.deepcopy(self.s.cost), 'budget_key':'hf_stills'}
        video_cost = {**copy.deepcopy(self.s.cost), 'budget_key':'hf_motion',
                      'budget_unit':'credit' if same_unit else 'native_atoms'}
        self.s.policy['operations']['submit'] = {'nano_banana_pro':image_cost, 'seedance_2_5':video_cost}
        self.f.config.set('execution_policy', self.s.policy)
        target, selection, method, _ = self.s.c.video()
        video = self.s.c.compiler.prepare(self.actor, self.pid,
            self.s.c.request(target, task='stress', method=method, inputs=[selection], key='native-video'))
        image = self.s.c.compiler.prepare(self.actor, self.pid,
            self.s.c.request(self.s.target, method=self.s.method, key='native-image'))
        self.candidates = [image, video]
        self.store.set_budget(self.pid, 20, 'credit', budget_key='hf_stills')
        self.store.set_budget(self.pid, 15, video_cost['budget_unit'], budget_key='hf_motion')

    def test_same_unit_different_accounts_keep_independent_totals(self):
        self.native_candidates()
        batch = self.service.create(self.actor, self.pid, self.request())
        decision = batch['body']['budget_decision']
        self.assertEqual(decision, {'buckets':[
            {'budget_key':'hf_motion','unit':'credit','reservation_total':10,'available_before':15},
            {'budget_key':'hf_stills','unit':'credit','reservation_total':10,'available_before':20}]})
        self.assertEqual(self.store.budget(self.pid)['reserved'], 0)
        self.assertEqual(self.store.budget(self.pid, budget_key='hf_stills')['reserved'], 10)
        self.assertEqual(self.store.budget(self.pid, budget_key='hf_motion')['reserved'], 10)
        self.assertEqual(self.service.get(self.actor, self.pid, batch['object_id'])['budget_decision'], decision)
        self.assertEqual(self.service.create(self.actor, self.pid, self.request()), batch)

    def test_unlike_units_are_never_added_to_one_batch_total(self):
        self.native_candidates(same_unit=False)
        batch = self.service.create(self.actor, self.pid, self.request())
        decision = batch['body']['budget_decision']
        self.assertNotIn('reservation_total', decision)
        self.assertNotIn('unit', decision)
        self.assertEqual([(x['budget_key'], x['unit'], x['reservation_total']) for x in decision['buckets']],
                         [('hf_motion','native_atoms',10), ('hf_stills','credit',10)])
        self.assertEqual([c['cost']['budget_key'] for c in batch['body']['children']], ['hf_stills','hf_motion'])

    def test_one_exhausted_native_account_preflights_zero_children(self):
        self.native_candidates()
        self.store.set_budget(self.pid, 9, 'credit', budget_key='hf_motion')
        # Other accounts have ample same-unit money; none may cover the shortfall.
        with patch.object(self.s.service, 'submit', wraps=self.s.service.submit) as submit:
            with self.assertRaises(DomainError) as error:
                self.service.create(self.actor, self.pid, self.request())
            self.assertEqual(error.exception.code, 'budget_exceeded')
            self.assertEqual(submit.call_count, 0)
        self.assert_empty()
        self.assertTrue(all(row['reserved'] == 0 for row in self.store.list_budgets(self.pid)))
        self.store.set_budget(self.pid, 20, 'credit', budget_key='hf_motion')
        self.assertEqual(self.service.create(self.actor, self.pid, self.request())['body']['count'], 2)

    def test_native_same_account_aggregate_must_fit_before_any_enqueue(self):
        self.native_candidates()
        image = self.candidates[0]
        second = self.second()
        self.store.set_budget(self.pid, 15, 'credit', budget_key='hf_stills')
        with patch.object(self.s.service, 'submit', wraps=self.s.service.submit) as submit:
            with self.assertRaises(DomainError) as error:
                self.service.create(self.actor, self.pid, self.request(candidates=[image, second]))
            self.assertEqual(error.exception.code, 'budget_exceeded')
            self.assertEqual(submit.call_count, 0)
        self.assert_empty()
        self.assertEqual(self.store.budget(self.pid, budget_key='hf_stills')['reserved'], 0)

    def test_competing_batches_cannot_spend_the_same_native_envelopes(self):
        self.native_candidates()
        barrier = threading.Barrier(2)
        requests = [self.request(key) for key in ('one', 'two')]
        def create(request):
            barrier.wait()
            try:
                self.service.create(self.actor, self.pid, request)
                return 'ok'
            except DomainError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, requests))
        self.assertCountEqual(results, ['ok', 'budget_exceeded'])
        self.assertEqual(len(self.store.list_objects(self.pid, kind='batch')), 1)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='job')), 2)
        self.assertEqual(self.store.budget(self.pid, budget_key='hf_stills')['reserved'], 10)
        self.assertEqual(self.store.budget(self.pid, budget_key='hf_motion')['reserved'], 10)

    def test_final_enqueue_failure_rolls_back_every_native_account(self):
        self.native_candidates()
        original = self.s.service.submit
        calls = 0
        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise DomainError('review_required', 'Second independent review no longer current')
            return original(*args, **kwargs)
        with patch.object(self.s.service, 'submit', side_effect=fail_second), self.assertRaises(DomainError):
            self.service.create(self.actor, self.pid, self.request())
        self.assertEqual(calls, 2)
        self.assert_empty()
        self.assertTrue(all(row['reserved'] == 0 for row in self.store.list_budgets(self.pid)))
        self.assertEqual(self.service.create(self.actor, self.pid, self.request())['body']['count'], 2)

    def test_single_native_account_still_has_explicit_identity(self):
        self.native_candidates()
        batch = self.service.create(self.actor, self.pid, self.request(candidates=[self.candidates[0]]))
        self.assertEqual(batch['body']['budget_decision'], {'buckets':[
            {'budget_key':'hf_stills','unit':'credit','reservation_total':10,'available_before':20}]})

    def test_native_account_partial_result_and_other_hold_survive(self):
        self.native_candidates()
        batch = self.service.create(self.actor, self.pid, self.request())
        first = self.finish(batch['body']['children'][0])
        result = self.service.get(self.actor, self.pid, batch['object_id'])
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(result['terminal'])
        self.assertEqual(result['children'][0]['result'], first['body']['result'])
        self.assertEqual(self.store.budget(self.pid, budget_key='hf_stills')['spent'], 8)
        self.assertEqual(self.store.budget(self.pid, budget_key='hf_motion')['reserved'], 10)
        self.assertEqual(self.store.budget(self.pid)['spent'], 0)

    def test_manifest_precedes_unified_children_and_replay_does_not_duplicate(self):
        self.second()
        original = self.s.service.submit
        def submit(*args,**kwargs):
            batches = self.store.list_objects(self.pid,kind='batch',conn=kwargs['conn'])
            self.assertEqual(len(batches),1)
            self.assertEqual(batches[0]['body']['state'],'authorized')
            self.assertEqual(batches[0]['body']['budget_decision']['reservation_total'],20)
            self.assertEqual(len(batches[0]['body']['children']),2)
            return original(*args,**kwargs)
        with patch.object(self.s.service,'submit',side_effect=submit) as unified:
            batch = self.service.create(self.actor,self.pid,self.request())
            self.assertEqual(unified.call_count,2)
        self.assertEqual(batch,self.service.create(self.actor,self.pid,self.request()))
        self.assertEqual(batch['body']['budget_decision']['budget_key'], 'legacy')
        self.assertEqual(batch['body']['budget_decision']['buckets'], [{'budget_key':'legacy', 'unit':'credit',
            'reservation_total':20, 'available_before':30}])
        self.assertEqual(self.store.budget(self.pid)['reserved'],20)
        self.assertEqual(len(self.store.list_objects(self.pid,kind='job')),2)
        self.assertEqual(len({c['job']['object_id'] for c in batch['body']['children']}),2)
        self.assertEqual(len({c['reservation_id'] for c in batch['body']['children']}),2)
        self.assertEqual(batch['body']['children'][0]['resolution'],self.s.candidate['body']['request']['params']['resolution'])
        self.assertTrue(batch['body']['support']['within_request_repeated_candidate_supported'])
        self.assertFalse(batch['body']['support']['one_take_per_candidate'])
        with self.assertRaises(DomainError):
            self.service.create(self.actor,self.pid,self.request(candidates=[self.s.candidate]))
        self.assertEqual(self.store.list_objects(self.pid,kind='provider-attempt'),[])

    def test_concurrent_replay_has_one_complete_batch(self):
        barrier = threading.Barrier(2)
        request = self.request()
        def call(_):
            barrier.wait()
            return self.service.create(self.actor,self.pid,request)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first,second = list(pool.map(call,[1,2]))
        self.assertEqual(first,second)
        self.assertEqual(len(self.store.list_objects(self.pid,kind='batch')),1)
        self.assertEqual(len(self.store.list_objects(self.pid,kind='job')),1)
        self.assertEqual(self.store.budget(self.pid)['reserved'],10)

    def test_changed_candidate_is_explicit_and_keeps_previous_attempt_identity(self):
        first = self.service.create(self.actor,self.pid,self.request())
        self.finish(first['body']['children'][0],success=False)
        target = self.store.append_revision(self.pid,self.s.target['object_id'],1,self.s.target['body'],'author')
        method = self.f.choice(target,'image')
        candidate = self.s.c.compiler.prepare(self.actor,self.pid,self.s.c.request(target,method=method,key='changed'))
        second = self.service.create(self.actor,self.pid,self.request('changed',candidates=[candidate]))
        child = second['body']['children'][0]
        self.assertEqual(child['relation'],'changed-candidate')
        self.assertEqual(len(child['previous_attempts']),1)
        self.assertNotEqual(child['candidate'],first['body']['children'][0]['candidate'])
        self.assertEqual(self.service.get(self.actor,self.pid,second['object_id'])['children'][0]['attempt'],1)

    def test_aggregate_shortfall_queues_nothing_even_when_one_take_fits(self):
        self.second()
        self.store.set_budget(self.pid,15,'credit')
        with self.assertRaises(DomainError) as error:
            self.service.create(self.actor,self.pid,self.request())
        self.assertEqual(error.exception.code,'budget_exceeded')
        self.assert_empty()

    def test_denial_during_second_final_enqueue_rolls_back_first_and_manifest(self):
        second = self.second()
        original = self.s.service._generation
        calls = 0
        def check(pid,candidate,db):
            nonlocal calls
            if candidate['object_id']==second['object_id']:
                calls += 1
                if calls==2:
                    raise DomainError('review_required','Independent review changed at final enqueue')
            return original(pid,candidate,db)
        with patch.object(self.s.service,'_generation',side_effect=check),self.assertRaises(DomainError):
            self.service.create(self.actor,self.pid,self.request())
        self.assert_empty()
        # Rolled-back child and batch idempotency records cannot poison a retry.
        self.assertEqual(self.service.create(self.actor,self.pid,self.request())['body']['count'],2)

    def test_partial_success_preserves_results_and_distinct_provider_attempts(self):
        self.second()
        batch = self.service.create(self.actor,self.pid,self.request())
        first = self.finish(batch['body']['children'][0])
        self.finish(batch['body']['children'][1],success=False)
        result = self.service.get(self.actor,self.pid,batch['object_id'])
        self.assertEqual(result['status'],'partial')
        self.assertTrue(result['terminal'])
        self.assertEqual(result['counts'],{'succeeded':1,'failed':1})
        self.assertEqual(result['children'][0]['result'],first['body']['result'])
        self.assertFalse(result['accepted'])
        self.assertEqual(len(self.store.list_objects(self.pid,kind='provider-attempt')),2)
        self.assertEqual(self.service.create(self.actor,self.pid,self.request()),batch)
        self.assertEqual(len(self.store.list_objects(self.pid,kind='provider-attempt')),2)
        self.assertEqual(self.store.budget(self.pid)['spent'],16)

    def test_rerun_retains_candidate_allowance_and_changed_candidate_resets_it(self):
        batch = self.service.create(self.actor,self.pid,self.request())
        self.finish(batch['body']['children'][0],success=False)
        rerun = self.service.create(self.actor,self.pid,self.request('rerun'))
        self.assertEqual(rerun['body']['children'][0]['relation'],'rerun')
        self.finish(rerun['body']['children'][0],success=False)
        # each batch has the candidate's allowance, so a third batch is a third reshoot.
        third = self.service.create(self.actor,self.pid,self.request('third'))
        self.assertEqual(self.service.get(self.actor,self.pid,third['object_id'])['children'][0]['attempt'],1)
        self.store.set_budget(self.pid,100,'credit')  # the three reshoots spent the fixture's 30 credits
        target = self.store.append_revision(self.pid,self.s.target['object_id'],1,self.s.target['body'],'author')
        method = self.f.choice(target,'image')
        changed = self.s.c.compiler.prepare(self.actor,self.pid,self.s.c.request(target,method=method,key='changed'))
        admitted = self.service.create(self.actor,self.pid,self.request('changed',candidates=[changed]))
        self.assertEqual(self.service.get(self.actor,self.pid,admitted['object_id'])['children'][0]['attempt'],1)
        self.assertEqual(len(self.store.list_objects(self.pid,kind='batch')),4)

    def test_four_repeated_candidates_have_independent_jobs_and_one_budget_decision(self):
        candidate = self.s.four_take_shot()
        request = self.request(candidates=[candidate] * 4)
        batch = self.service.create(self.actor, self.pid, request)
        children = batch['body']['children']
        self.assertEqual(batch['body']['count'], 4)
        self.assertEqual([c['candidate'] for c in children], [self.f.ref(candidate).model_dump()] * 4)
        self.assertEqual([c['idempotency_key'] for c in children], [f'{batch["object_id"]}-{i}' for i in range(4)])
        self.assertEqual(len({c['job']['object_id'] for c in children}), 4)
        self.assertEqual(len({c['reservation_id'] for c in children}), 4)
        self.assertEqual(batch['body']['budget_decision']['buckets'], [
            {'budget_key':'legacy', 'unit':'credit', 'reservation_total':40, 'available_before':100}])
        self.assertEqual(self.store.budget(self.pid)['reserved'], 40)
        self.assertEqual(self.service.get(self.actor, self.pid, batch['object_id'])['counts'], {'queued':4})
        self.assertEqual(self.service.create(self.actor, self.pid, request), batch)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='job')), 4)
        for child in children:
            self.finish(child, success=False)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='provider-attempt')), 4)
        # another batch of the same candidate is a reshoot with its own four takes.
        again = self.service.create(self.actor, self.pid, self.request('fifth', candidates=[candidate]))
        self.assertEqual(again['body']['children'][0]['relation'], 'rerun')
        self.assertEqual(self.service.get(self.actor, self.pid, again['object_id'])['children'][0]['attempt'], 1)
        self.assertEqual(self.service.create(self.actor, self.pid, self.request('fifth', candidates=[candidate])), again)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='batch')), 2)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='job')), 5)
        changed = self.s.revised_shot_candidate()
        self.assertEqual(changed['body']['target']['object_id'], candidate['body']['target']['object_id'])
        admitted = self.service.create(self.actor, self.pid, self.request('new-version', candidates=[changed]))
        self.assertEqual(self.service.get(self.actor, self.pid, admitted['object_id'])['children'][0]['attempt'], 1)

    def test_repeated_candidates_still_require_the_aggregate_budget(self):
        candidate = self.s.four_take_shot()
        self.store.set_budget(self.pid, 39, 'credit')
        with self.assertRaises(DomainError) as error:
            self.service.create(self.actor, self.pid, self.request(candidates=[candidate] * 4))
        self.assertEqual(error.exception.code, 'budget_exceeded')
        self.assert_empty()

    def dispatch(self, jobs, child):
        claim = jobs.claim(self.worker, self.pid, child['job']['object_id'])
        return jobs.begin_dispatch(self.worker, self.pid, self.f.ref(claim['job']), claim['fence'])

    def test_same_batch_takes_dispatch_with_active_siblings(self):
        candidate = self.s.four_take_shot()
        batch = self.service.create(self.actor, self.pid, self.request(candidates=[candidate] * 4))
        children = batch['body']['children']
        jobs = Jobs(self.store, self.f.auth, self.s.service, self.f.media, clock=lambda: 1000, lease_seconds=60)
        first = self.dispatch(jobs, children[0])
        # The first create has no receipt yet, but its dispatch lease is live.
        second = self.dispatch(jobs, children[1])
        for child, prior, state in zip(children[2:], (first, second), ('submitted', 'running')):
            jobs.record_receipt(self.worker, self.pid, self.f.ref(prior['job']), prior['fence'],
                ProviderReceipt('remote-' + state, 'seedance_2_5', state, state, {}, {}, {}))
            self.dispatch(jobs, child)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='provider-attempt')), 4)
        self.assertEqual(self.store.budget(self.pid)['reserved'], 40)

    def test_unknown_batch_sibling_never_blocks_the_other_takes(self):
        # Owner 2026-09-24: one stuck take must not hold up the rest of its shot.
        candidate = self.s.four_take_shot()
        batch = self.service.create(self.actor, self.pid, self.request(candidates=[candidate] * 4))
        children = batch['body']['children']
        jobs = Jobs(self.store, self.f.auth, self.s.service, self.f.media)
        first = self.dispatch(jobs, children[0])
        second = self.dispatch(jobs, children[1])
        jobs.record_receipt(self.worker, self.pid, self.f.ref(first['job']), first['fence'],
            ProviderReceipt('remote-first', 'seedance_2_5', 'running', 'running', {}, {}, {}))
        jobs.record_unknown(self.worker, self.pid, self.f.ref(second['job']), second['fence'])
        for child in children[2:]:
            self.dispatch(jobs, child)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='provider-attempt')), 4)
        self.assertEqual(self.store.get_object(self.pid, second['job']['object_id'])['body']['state'], 'unknown')
        self.assertEqual(self.store.budget(self.pid)['reserved'], 40)

    def test_expired_or_missing_batch_dispatch_lease_does_not_block_a_sibling(self):
        candidate = self.s.four_take_shot()
        batch = self.service.create(self.actor, self.pid, self.request(candidates=[candidate] * 2))
        first, second = batch['body']['children']
        jobs = Jobs(self.store, self.f.auth, self.s.service, self.f.media, clock=lambda: 1000, lease_seconds=60)
        dispatched = self.dispatch(jobs, first)
        current = self.store.get_object(self.pid, dispatched['job']['object_id'])
        self.store.append_revision(self.pid, current['object_id'], current['revision'],
            {**current['body'], 'lease':None}, 'worker_service')
        self.dispatch(jobs, second)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='provider-attempt')), 2)
        self.assertEqual(self.store.budget(self.pid)['reserved'], 20)

    def test_other_batch_and_single_stay_blocked_at_dispatch(self):
        candidate = self.s.four_take_shot()
        first = self.service.create(self.actor, self.pid, self.request(candidates=[candidate]))
        other = self.service.create(self.actor, self.pid, self.request('other', candidates=[candidate]))
        single = self.s.service.submit(self.actor, self.pid, self.s.request('single', candidate))
        jobs = Jobs(self.store, self.f.auth, self.s.service, self.f.media)
        dispatched = self.dispatch(jobs, first['body']['children'][0])
        claims = [jobs.claim(self.worker, self.pid, oid) for oid in
                  (other['body']['children'][0]['job']['object_id'], single['object_id'])]
        for state in ('dispatching', 'submitted', 'running', 'unknown'):
            current = self.store.get_object(self.pid, dispatched['job']['object_id'])
            self.store.append_revision(self.pid, current['object_id'], current['revision'],
                {**current['body'], 'state':state}, 'worker_service')
            for claim in claims:
                with self.subTest(state=state, job=claim['job']['object_id']), self.assertRaises(DomainError) as error:
                    jobs.begin_dispatch(self.worker, self.pid, self.f.ref(claim['job']), claim['fence'])
                self.assertEqual(error.exception.code, 'unknown_outcome')
            with self.assertRaises(DomainError) as error:
                self.service.create(self.actor, self.pid, self.request('new-' + state, candidates=[candidate]))
            self.assertEqual(error.exception.code, 'unknown_outcome')
            with self.assertRaises(DomainError) as error:
                self.s.service.submit(self.actor, self.pid, self.s.request('new-' + state, candidate))
            self.assertEqual(error.exception.code, 'unknown_outcome')
        self.assertEqual(len(self.store.list_objects(self.pid, kind='provider-attempt')), 1)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='batch')), 2)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='job')), 3)
        self.assertEqual(self.store.budget(self.pid)['reserved'], 30)

    def test_a_revised_card_fires_a_new_batch_while_the_old_version_is_unresolved(self):
        # HF-style: a stuck take of the old
        # prompt version does not hold up four takes of the revised version.
        candidate = self.s.four_take_shot()
        first = self.service.create(self.actor, self.pid, self.request(candidates=[candidate]))
        jobs = Jobs(self.store, self.f.auth, self.s.service, self.f.media)
        dispatched = self.dispatch(jobs, first['body']['children'][0])
        jobs.record_unknown(self.worker, self.pid, self.f.ref(dispatched['job']), dispatched['fence'])
        revised = self.s.revised_shot_candidate()
        batch = self.service.create(self.actor, self.pid, self.request('revised', candidates=[revised] * 4))
        for child in batch['body']['children']:
            self.dispatch(jobs, child)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='provider-attempt')), 5)

    def test_inflight_single_blocks_queued_batch(self):
        candidate = self.s.four_take_shot()
        single = self.s.service.submit(self.actor, self.pid, self.s.request('single', candidate))
        batch = self.service.create(self.actor, self.pid, self.request(candidates=[candidate] * 2))
        jobs = Jobs(self.store, self.f.auth, self.s.service, self.f.media)
        claim = jobs.claim(self.worker, self.pid, single['object_id'])
        jobs.begin_dispatch(self.worker, self.pid, self.f.ref(claim['job']), claim['fence'])
        for child in batch['body']['children']:
            with self.assertRaises(DomainError) as error:
                self.dispatch(jobs, child)
            self.assertEqual(error.exception.code, 'unknown_outcome')
        self.assertEqual(len(self.store.list_objects(self.pid, kind='provider-attempt')), 1)

    def test_worker_dispatches_four_siblings_in_one_cycle(self):
        candidate = self.s.four_take_shot()
        batch = self.service.create(self.actor, self.pid, self.request(candidates=[candidate] * 4))
        # Hold every create until all four have reached the provider. A sequential
        # lineage guard cannot pass this barrier, even with four worker slots.
        barrier = threading.Barrier(4, timeout=10)
        def submit(intent, refs, **kwargs):
            barrier.wait()
            remote = f'remote-{intent["attempt"]}'
            return ProviderReceipt(remote, 'seedance_2_5', 'submitted', 'submitted', {}, {}, {})
        provider = MagicMock(fake=True, timeout=1)
        provider.submit.side_effect = submit
        downloader = SafeDownloader({'cdn.example'}, transport=MagicMock())
        jobs = Jobs(self.store, self.f.auth, self.s.service, self.f.media, downloader=downloader)
        worker = Worker(jobs, provider, self.worker, [self.pid], concurrency=4)
        outcomes = worker.run_once()
        self.assertCountEqual([outcome['job_id'] for outcome in outcomes],
                              [child['job']['object_id'] for child in batch['body']['children']])
        self.assertEqual([outcome['state'] for outcome in outcomes], ['submitted'] * 4)
        self.assertTrue(all(outcome['action'] == 'dispatch' for outcome in outcomes))
        self.assertEqual(provider.submit.call_count, 4)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='provider-attempt')), 4)
        self.assertEqual(self.store.budget(self.pid)['reserved'], 40)

    def test_create_cap_leaves_the_other_slots_for_other_work(self):
        # Owner 2026-09-24: director reviews run together; uploads stay two at a time.
        candidate = self.s.four_take_shot()
        batch = self.service.create(self.actor, self.pid, self.request(candidates=[candidate] * 4))
        provider = MagicMock(fake=True, timeout=1)
        provider.submit.side_effect = lambda intent, refs, **kwargs: ProviderReceipt(
            f'remote-{intent["attempt"]}', 'seedance_2_5', 'submitted', 'submitted', {}, {}, {})
        downloader = SafeDownloader({'cdn.example'}, transport=MagicMock())
        jobs = Jobs(self.store, self.f.auth, self.s.service, self.f.media, downloader=downloader)
        worker = Worker(jobs, provider, self.worker, [self.pid], concurrency=8, create_concurrency=2)
        self.assertEqual(len(worker.run_once()), 2)
        self.assertEqual(provider.submit.call_count, 2)
        self.assertEqual(len(worker.run_once()), 2)
        self.assertEqual(provider.submit.call_count, 4)
        self.assertEqual(len(batch['body']['children']), 4)
        with self.assertRaises(ValueError):
            Worker(jobs, provider, self.worker, [self.pid], concurrency=2, create_concurrency=3)

    def test_create_cap_counts_only_paid_creates(self):
        candidate = self.s.four_take_shot()
        self.service.create(self.actor, self.pid, self.request(candidates=[candidate] * 4))
        provider = MagicMock(fake=True, timeout=1)
        provider.submit.side_effect = lambda intent, refs, **kwargs: ProviderReceipt(
            f'remote-{intent["attempt"]}', 'seedance_2_5', 'submitted', 'submitted', {}, {}, {})
        jobs = Jobs(self.store, self.f.auth, self.s.service, self.f.media,
                    downloader=SafeDownloader({'cdn.example'}, transport=MagicMock()))
        worker = Worker(jobs, provider, self.worker, [self.pid], concurrency=8, create_concurrency=2)
        seen = []
        local = {'body': {'operation': 'render-cut', 'cost': {'mode': 'local'}}}
        with patch.object(Worker, 'operations', new_callable=PropertyMock, return_value=['render-cut']),\
                patch.object(worker, '_intent', return_value=local),\
                patch.object(worker, '_process', side_effect=lambda pid, job: seen.append(job['object_id']) or {}):
            worker.run_once()
        # Not creates: none is held back by the create cap.
        self.assertEqual(len(seen), 4)

    def test_stale_revoked_and_viewer_cannot_create_or_replay(self):
        viewer = self.f.auth.authenticate(self.f.auth.provision_token('viewer','viewer',[self.pid],300))
        with self.assertRaises(DomainError):
            self.service.create(viewer,self.pid,self.request())
        batch = self.service.create(self.actor,self.pid,self.request())
        self.assertEqual(self.service.get(viewer,self.pid,batch['object_id'])['status'],'pending')
        self.store.append_revision(self.pid,self.s.target['object_id'],1,self.s.target['body'],'author')
        with self.assertRaises(DomainError):
            self.service.create(self.actor,self.pid,self.request('stale'))
        self.assertTrue(self.service.get(viewer,self.pid,batch['object_id'])['stale'])
        self.f.auth.revoke(self.actor.credential_id)
        with self.assertRaises(DomainError):
            self.service.create(self.actor,self.pid,self.request())
        self.assertEqual(len(self.store.list_objects(self.pid,kind='job')),1)

    def test_count_and_resolution_cannot_be_client_overrides(self):
        values = self.request().model_dump()
        for change in ({'take_count':4},{'takes':4},{'resolution':'4k'}, {'candidate_ids':[]},
                       {'candidate_ids':['candidate_'+str(i) for i in range(101)]}):
            with self.assertRaises(ValidationError):
                BatchRequest(**{**values,**change})

    def test_per_version_batch_cap_allows_four_repeats_but_rejects_five(self):
        values = self.request().model_dump()
        candidate_id = self.s.candidate['object_id']
        for count in (2, 4):
            self.assertEqual(BatchRequest(**{**values, 'candidate_ids':[candidate_id] * count}).candidate_ids,
                             [candidate_id] * count)
        ids = [f'candidate_{i}' for i in range(25)] * 4
        self.assertEqual(BatchRequest(**{**values, 'candidate_ids':ids}).candidate_ids, ids)
        for ids in ([candidate_id] * 5, [candidate_id, 'another'] * 4 + [candidate_id]):
            with self.assertRaisesRegex(ValidationError, 'candidate version.*4'):
                BatchRequest(**{**values, 'candidate_ids':ids})

    def test_exact_video_selection_delivery_lineage_and_stale_revision(self):
        self.s.policy['operations']['submit']['seedance_2_5'] = self.s.cost
        self.f.config.set('execution_policy', self.s.policy)
        target,selection,method,_ = self.s.c.video()
        candidate = self.s.c.compiler.prepare(self.actor,self.pid,self.s.c.request(target,task='stress',method=method,inputs=[selection],key='video'))
        batch = self.service.create(self.actor,self.pid,self.request('video',candidates=[candidate]))
        file = self.f.root/'batch-video.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=red:s=1920x1080:r=24:d=4',
            '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=4',
            '-c:v','libx264','-threads','1','-pix_fmt','yuv420p','-c:a','aac','-shortest',str(file)],check=True,capture_output=True)
        completed = self.finish(batch['body']['children'][0],raw=file.read_bytes(),mime='video/mp4')
        take = ObjectRef(**completed['body']['result'])
        request = SelectTakeRequest(idempotency_key='select',expected_revision=1,shot=self.f.ref(target),take=take,
            rationale='Keep this exact generated performance for the next review.')
        chosen = self.service.select_take(self.actor,self.pid,batch['object_id'],request)
        self.assertFalse(chosen['body']['accepted'])
        derivative = self.f.media.put(self.pid,[file.read_bytes()],'video/mp4','worker_service',derivative_of=take.model_dump())
        lineage = self.service.delivery_lineage(self.actor,self.pid,self.f.ref(batch),self.f.ref(chosen),self.f.ref(derivative))
        self.assertEqual(lineage['selected_take'],take.model_dump())
        self.assertEqual(lineage['candidate'],self.f.ref(candidate).model_dump())
        self.assertTrue(lineage['identity_lineage_verified'])
        self.assertFalse(lineage['artistic_equivalence_verified'])
        fresh = self.f.media.put(self.pid,[file.read_bytes()],'video/mp4','worker_service')
        with self.assertRaises(DomainError):
            self.service.delivery_lineage(self.actor,self.pid,self.f.ref(batch),self.f.ref(chosen),self.f.ref(fresh))
        with self.assertRaises(DomainError):
            self.service.select_take(self.actor,self.pid,batch['object_id'],request.model_copy(update={'take':self.f.ref(fresh),'idempotency_key':'wrong'}))
        self.store.append_revision(self.pid,target['object_id'],1,target['body'],'author')
        with self.assertRaises(DomainError):
            self.service.delivery_lineage(self.actor,self.pid,self.f.ref(batch),self.f.ref(chosen))


class IndependentBatchTests(unittest.TestCase):
    def test_batch_with_an_unreviewed_candidate_queues_every_take(self):
        # Owner decision 2026-09-23: reviews are advice; an unreviewed candidate does not block its batch.
        fixture = test_submissions.IndependentSubmissionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        f,pid = fixture.f,fixture.pid
        target = f.draft(f.definition(description='A second independently reviewed character'))
        method = f.choice(target,'image')
        candidate = fixture.c.compiler.prepare(fixture.actor,pid,fixture.c.request(target,method=method,key='unreviewed'))
        reviews = Reviews(f.store,f.auth,f.flow,f.media)
        service = Batches(f.store,f.auth,f.flow,fixture.service,reviews)
        request = BatchRequest(idempotency_key='independent',expected_revision=f.store.get_object(pid,pid)['revision'],
            candidate_ids=[fixture.candidate['object_id'],candidate['object_id']])
        service.create(fixture.actor,pid,request)
        self.assertEqual(len(f.store.list_objects(pid,kind='job')),2)
        self.assertEqual(len(f.store.list_objects(pid,kind='batch')),1)
