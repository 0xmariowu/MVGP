"""Submission authority is atomic, durable and independent of author assertions."""
import copy
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from production.auth import AuthService
from production.contracts import (
    DomainError,
    ObserveRequest,
    RenderCutRequest,
    SubmitRequest,
    content_hash,
)
from production.gates import Gates
from production.jobs import Jobs
from production.submissions import CostPolicy, Submissions
from production.tests import test_compiler, writer_fixture
from production.tests.fixtures import hf_era_document


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.c = test_compiler.CompilerTests()
        self.c.setUp()
        self.addCleanup(self.c.doCleanups)
        self.f, self.actor, self.pid = self.c.f, self.c.actor, 'project_1'
        self.store = self.f.store
        self.cost = {'mode':'fake', 'budget_unit':'credit', 'estimated_cost':8, 'reservation':10,
                     'max_attempts':2, 'live_controls':{'pricing_verified':False, 'isolation_verified':False, 'account_limits_verified':False}}
        self.policy = {'operations':{'submit':{'nano_banana_pro':self.cost},
            'observe':{'image':copy.deepcopy(self.cost)}, 'render-cut':{'mode':'local','max_attempts':3}}}
        self.f.config.set('execution_policy', self.policy)
        routes = self.f.config.section('review_routes')
        routes['profiles']['observer'].update(allowed_input_modalities=['image'], limits={'max_input_bytes_per_turn':100000})
        self.f.config.set('review_routes', routes)
        self.c.activate()
        self.gates = Gates(self.store, self.f.flow, self.f.media)
        self.service = Submissions(self.store, self.f.auth, self.f.flow, self.gates, review_check=self.review)
        self.target = self.f.draft(self.f.definition())
        self.method = self.f.choice(self.target, 'image')
        self.candidate = self.c.compiler.prepare(self.actor, self.pid, self.c.request(self.target, method=self.method))
        self.store.set_budget(self.pid, 30, 'credit')

    def review(self, pid, candidate, policy, conn):
        return {'authorized':True, 'candidate':self.f.ref(candidate).model_dump(),
                'policy_hash':content_hash(policy), 'roles':policy['required_roles'], 'receipts':[]}

    def request(self, key='submit', candidate=None):
        item = candidate or self.candidate
        return SubmitRequest(idempotency_key=key, expected_revision=item['revision'], candidate_id=item['object_id'])

    def intent(self, job):
        return self.store.get_object(self.pid, job['body']['intent']['object_id'])

    def four_take_shot(self):
        self.policy['operations']['submit']['seedance_2_5'] = {**self.cost, 'max_attempts':4}
        self.f.config.set('execution_policy', self.policy)
        self.target, self.selection, self.method, _ = self.c.video()
        self.candidate = self.c.compiler.prepare(self.actor, self.pid,
            self.c.request(self.target, task='stress', method=self.method, inputs=[self.selection], key='four-takes'))
        self.store.set_budget(self.pid, 100, 'credit')
        return self.candidate

    def revised_shot_candidate(self):
        body = copy.deepcopy(self.target['body'])
        body['content']['The material']['the action in one to three sentences'] = 'The box moves slowly and stops against the right wall.'
        body['content']['_production']['change_note'] = 'The box now moves slowly before it stops.'
        # The writer owns the whole prompt: a changed card is rewritten into the prompt by the writer.
        body['content']['_production']['prompt'] = writer_fixture.prompt(body['content'])
        target = self.store.append_revision(self.pid, self.target['object_id'], self.target['revision'], body, 'author')
        method = self.store.create_object(self.pid, 'method', {
            'content':{**self.method['body']['content'], 'target':self.f.ref(target).model_dump()},
            'dependencies':[self.f.ref(target).model_dump()]}, 'author')
        selection = self.store.append_revision(self.pid, self.selection['object_id'], self.selection['revision'], {**self.selection['body'],
            'content':{**self.selection['body']['content'], 'target':self.f.ref(target).model_dump()}}, 'author')
        return self.c.compiler.prepare(self.actor, self.pid,
            self.c.request(target, task='stress', method=method, inputs=[selection], key='revised-shot'))

    def native_candidate(self, key='hf_main', mode='fake'):
        self.cost.update(budget_key=key, mode=mode)
        self.f.config.set('execution_policy', self.policy)
        self.c.activate()
        self.candidate = self.c.compiler.prepare(self.actor, self.pid,
            self.c.request(self.target, method=self.method, key='native-candidate'))

    def test_native_account_is_frozen_and_not_borrowed_from_legacy(self):
        self.native_candidate()
        with self.assertRaises(DomainError):
            self.service.submit(self.actor, self.pid, self.request())
        self.assertEqual(self.store.list_objects(self.pid, kind='job'), [])
        self.store.set_budget(self.pid, 20, 'credit', budget_key='hf_main')
        job = self.service.submit(self.actor, self.pid, self.request())
        intent = self.intent(job)
        self.assertEqual(intent['body']['cost']['budget_key'], 'hf_main')
        self.assertEqual(intent['body']['cost_hash'], content_hash(intent['body']['cost']))
        self.assertEqual(self.store.budget(self.pid)['reserved'], 0)
        self.assertEqual(self.store.budget(self.pid, budget_key='hf_main')['reserved'], 10)
        self.assertEqual(self.service.revalidate(self.pid, self.f.ref(job)), intent)

    def test_a_runtime_config_price_change_refuses_a_queued_intent_and_never_reprices_it(self):
        # the cost frozen in the intent must equal runtime.json's at dispatch.
        job = self.service.submit(self.actor, self.pid, self.request())
        frozen = self.intent(job)['body']['cost']
        before = self.store.budget(self.pid)
        for change in ({'estimated_cost': self.cost['estimated_cost'] + 1, 'reservation': self.cost['reservation'] + 1},
                       {'max_attempts': self.cost['max_attempts'] + 1}):
            with self.subTest(change=change):
                self.f.config.set('execution_policy', {**self.policy, 'operations': {**self.policy['operations'],
                                  'submit': {'nano_banana_pro': {**self.cost, **change}}}})
                with self.assertRaises(DomainError) as error:
                    self.service.revalidate(self.pid, self.f.ref(job))
                self.assertEqual(error.exception.code, 'release_mismatch')
                self.assertEqual(self.intent(job)['body']['cost'], frozen)
                self.assertEqual(self.store.budget(self.pid), before)
        self.f.config.set('execution_policy', self.policy)
        self.assertEqual(self.service.revalidate(self.pid, self.f.ref(job))['body']['cost'], frozen)

    def test_same_unit_reservation_swap_denies_before_dispatch(self):
        self.native_candidate()
        for key in ('hf_main', 'other_account'):
            self.store.set_budget(self.pid, 20, 'credit', budget_key=key)
        job = self.service.submit(self.actor, self.pid, self.request())
        with self.store.transaction() as db:
            db.execute('UPDATE reservations SET budget_key=? WHERE reservation_id=?',
                       ('other_account', job['body']['reservation_id']))
        with self.assertRaises(DomainError) as error:
            self.service.revalidate(self.pid, self.f.ref(job))
        self.assertEqual(error.exception.code, 'budget_exceeded')

    def test_live_cost_requires_explicit_nonlegacy_account(self):
        live = {**self.cost, 'mode':'live', 'live_controls':dict.fromkeys(self.cost['live_controls'], True)}
        for key in (None, 'legacy', '', '../x', True):
            invalid = dict(live)
            if key is not None:
                invalid['budget_key'] = key
            with self.subTest(key=key), self.assertRaises(ValueError):
                CostPolicy.model_validate(invalid)
        self.assertEqual(CostPolicy.model_validate({**live, 'budget_key':'hf_main'}).budget_key, 'hf_main')
        self.assertEqual(CostPolicy.model_validate(self.cost).budget_key, 'legacy')

    def test_historical_fake_cost_hash_is_checked_without_rewriting(self):
        job = self.service.submit(self.actor, self.pid, self.request())
        historical = copy.deepcopy(self.intent(job)['body'])
        historical['cost'].pop('budget_key', None)
        historical['cost_hash'] = content_hash(historical['cost'])
        intent = self.store.create_object(self.pid, 'dispatch-intent', historical, 'submission_service')
        self.store.reserve(self.pid, intent['object_id'], 10, 'credit', object_id=intent['object_id'])
        old_job = self.store.append_revision(self.pid, job['object_id'], job['revision'],
            {**job['body'], 'intent':self.f.ref(intent).model_dump(), 'reservation_id':intent['object_id']}, 'submission_service')
        self.assertEqual(self.service.revalidate(self.pid, self.f.ref(old_job)), intent)
        self.assertNotIn('budget_key', self.store.get_object(self.pid, intent['object_id'])['body']['cost'])
        # Compatibility may add exactly the legacy default for comparison, never ignore a bad stored hash.
        from production.submissions import frozen_cost_matches
        normalized = CostPolicy.model_validate(historical['cost']).model_dump()
        self.assertFalse(frozen_cost_matches(normalized, historical['cost'], '0'*64))
        self.assertFalse(frozen_cost_matches(normalized, normalized, None, legacy_unhashed=True))
        self.assertTrue(frozen_cost_matches(normalized, historical['cost'], None, legacy_unhashed=True))
        named = {**normalized, 'budget_key':'native'}
        self.assertFalse(frozen_cost_matches(named, named, None, legacy_unhashed=True))
        self.assertFalse(frozen_cost_matches({**normalized, 'budget_key':'other'}, historical['cost'], historical['cost_hash']))
        self.assertFalse(frozen_cost_matches({**normalized, 'mode':'live'}, {**historical['cost'], 'mode':'live'},
                                           content_hash({**historical['cost'], 'mode':'live'})))

    def test_duplicate_and_concurrent_submit_have_one_intent_and_reservation(self):
        barrier = threading.Barrier(2)
        def call(_):
            barrier.wait()
            return self.service.submit(self.actor, self.pid, self.request())
        with ThreadPoolExecutor(max_workers=2) as pool:
            a,b = list(pool.map(call, [1,2]))
        self.assertEqual(a,b)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='job')),1)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='dispatch-intent')),1)
        self.assertEqual(self.store.budget(self.pid)['reserved'],10)
        intent = self.intent(a)['body']
        self.assertEqual(intent['request'], self.candidate['body']['request'])
        self.assertEqual(intent['origin']['credential_id'], self.actor.credential_id)
        self.assertNotIn('proof', json.dumps(intent))
        self.assertEqual(a['body']['state'],'queued')

    def test_a_refusing_review_checker_is_never_a_gate_on_submission(self):
        # judgment reviews are advice, as at HF.
        service = Submissions(self.store,self.f.auth,self.f.flow,self.gates,review_check=lambda *args:{'authorized':False})
        job = service.submit(self.actor,self.pid,self.request())
        self.assertEqual(job['body']['state'],'queued')
        self.assertEqual(self.intent(job)['body']['review']['receipts'],[])

    def test_stale_candidate_and_revoked_origin_cannot_dispatch_or_replay(self):
        job = self.service.submit(self.actor,self.pid,self.request())
        self.f.auth.revoke(self.actor.credential_id)
        with self.assertRaises(DomainError):
            self.service.revalidate(self.pid,self.f.ref(job))
        with self.assertRaises(DomainError):
            self.service.submit(self.actor,self.pid,self.request())
        self.assertEqual(self.store.budget(self.pid)['reserved'],10)

    def test_revalidation_after_restart_uses_credential_not_old_process_mac(self):
        job = self.service.submit(self.actor,self.pid,self.request())
        new_auth = AuthService(self.store,'https://studio.example')
        service = Submissions(self.store,new_auth,self.f.flow,self.gates,review_check=self.review)
        self.assertEqual(service.revalidate(self.pid,self.f.ref(job))['object_id'],self.intent(job)['object_id'])
        self.store.append_revision(self.pid,self.target['object_id'],1,self.target['body'],'author')
        with self.assertRaises(DomainError):
            service.revalidate(self.pid,self.f.ref(job))

    def test_budget_failure_and_storage_failure_roll_back_all_intent(self):
        self.store.set_budget(self.pid,9,'credit')
        with self.assertRaises(DomainError):
            self.service.submit(self.actor,self.pid,self.request())
        self.assertEqual(self.store.list_objects(self.pid,kind='job'),[])
        self.assertEqual(self.store.list_objects(self.pid,kind='dispatch-intent'),[])
        self.store.set_budget(self.pid,30,'credit')
        original = self.store.create_object
        def fail(*args,**kwargs):
            if args[1]=='job':
                raise RuntimeError('Injected storage failure')
            return original(*args,**kwargs)
        with patch.object(self.store,'create_object',side_effect=fail),self.assertRaises(RuntimeError):
            self.service.submit(self.actor,self.pid,self.request())
        self.assertEqual(self.store.budget(self.pid)['reserved'],0)
        self.assertEqual(self.store.list_objects(self.pid,kind='dispatch-intent'),[])

    def test_new_candidate_has_its_own_released_attempt_allowance(self):
        first = self.service.submit(self.actor,self.pid,self.request())
        body = copy.deepcopy(self.target['body'])
        body['content']['definition']['description'] = 'A more worried character'
        target = self.store.append_revision(self.pid,self.target['object_id'],1,body,'author')
        method = self.f.choice(target,'image')
        candidate = self.c.compiler.prepare(self.actor,self.pid,self.c.request(target,method=method,key='changed'))
        self.assertNotEqual(candidate['digest'],self.candidate['digest'])
        second = self.service.submit(self.actor,self.pid,self.request('second',candidate))
        third = self.service.submit(self.actor,self.pid,self.request('third',candidate))
        with self.assertRaises(DomainError) as error:
            self.service.submit(self.actor,self.pid,self.request('fourth',candidate))
        self.assertEqual(error.exception.code,'attempt_limit')
        self.assertEqual(self.intent(first)['body']['attempt'],1)
        self.assertEqual(self.intent(second)['body']['attempt'],1)
        self.assertEqual(self.intent(third)['body']['attempt'],2)

    def test_four_takes_per_candidate_and_new_version_of_same_shot(self):
        candidate = self.four_take_shot()
        jobs = [self.service.submit(self.actor, self.pid, self.request(f'take-{i}', candidate)) for i in range(4)]
        self.assertEqual([job['body']['state'] for job in jobs], ['queued'] * 4)
        self.assertEqual([self.intent(job)['body']['attempt'] for job in jobs], [1, 2, 3, 4])
        with self.assertRaises(DomainError) as error:
            self.service.submit(self.actor, self.pid, self.request('fifth', candidate))
        self.assertEqual(error.exception.code, 'attempt_limit')
        self.assertEqual(self.service.submit(self.actor, self.pid, self.request('take-0', candidate)), jobs[0])
        self.assertEqual(self.store.budget(self.pid)['reserved'], 40)
        changed = self.revised_shot_candidate()
        self.assertNotEqual(changed['object_id'], candidate['object_id'])
        self.assertNotEqual(changed['body']['request'], candidate['body']['request'])
        self.assertEqual(changed['body']['target']['object_id'], candidate['body']['target']['object_id'])
        admitted = self.service.submit(self.actor, self.pid, self.request('new-version', changed))
        self.assertEqual(admitted['body']['state'], 'queued')
        self.assertEqual(self.intent(admitted)['body']['attempt'], 1)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='dispatch-intent')), 5)
        self.assertEqual(self.store.budget(self.pid)['reserved'], 50)

    def test_allowance_counts_per_batch_for_batches_and_per_candidate_for_single_submits(self):
        # a reshoot is a new batch of the same candidate (HF re-fires identical prompts in
        # 42–52% of batches); each batch has the candidate's four takes, a single submit keeps the candidate's allowance.
        candidate = self.four_take_shot()
        first = [self.service.submit(self.actor, self.pid, self.request(f'b1-{i}', candidate), batch='batch_one') for i in range(4)]
        self.assertEqual([self.intent(job)['body']['attempt'] for job in first], [1, 2, 3, 4])
        with self.assertRaises(DomainError) as full:
            self.service.submit(self.actor, self.pid, self.request('b1-4', candidate), batch='batch_one')
        self.assertEqual(full.exception.code, 'attempt_limit')
        second = [self.service.submit(self.actor, self.pid, self.request(f'b2-{i}', candidate), batch='batch_two') for i in range(4)]
        self.assertEqual([self.intent(job)['body']['attempt'] for job in second], [1, 2, 3, 4])
        self.assertEqual({self.intent(job)['body']['batch'] for job in second}, {'batch_two'})
        with self.assertRaises(DomainError) as single:
            self.service.submit(self.actor, self.pid, self.request('single', candidate))
        self.assertEqual(single.exception.code, 'attempt_limit')
        # A replayed submit of a batch child is the same job; nothing is reserved twice.
        self.assertEqual(self.service.submit(self.actor, self.pid, self.request('b2-0', candidate), batch='batch_two'), second[0])
        self.assertEqual(self.store.budget(self.pid)['reserved'], 80)

    def test_a_new_batch_of_the_same_candidate_waits_while_an_earlier_take_is_unresolved(self):
        candidate = self.four_take_shot()
        first = self.service.submit(self.actor, self.pid, self.request('b1-0', candidate), batch='batch_one')
        current = self.store.get_object(self.pid, first['object_id'])
        self.store.append_revision(self.pid, current['object_id'], current['revision'],
            {**current['body'], 'state': 'running', 'remote_job_id': 'known-job'}, 'worker_service')
        with self.assertRaises(DomainError) as waits:
            self.service.submit(self.actor, self.pid, self.request('b2-0', candidate), batch='batch_two')
        self.assertEqual(waits.exception.code, 'unknown_outcome')
        self.assertEqual(len(self.store.list_objects(self.pid, kind='dispatch-intent')), 1)

    def test_a_released_shot_plan_owner_gate_no_longer_holds_a_take(self):
        # Higgsfield has no shot-plan approval step.
        self.four_take_shot()
        self.policy['owner_gates'] = {'shot_plan': 'required'}
        self.f.config.set('execution_policy', self.policy)
        self.c.activate()
        candidate = self.c.compiler.prepare(self.actor, self.pid, self.c.request(self.target, task='stress', method=self.method,
                                            inputs=[self.selection], key='gated-shot'))
        self.assertEqual(self.service.submit(self.actor, self.pid, self.request('no-plan', candidate))['body']['state'], 'queued')

    def test_unknown_reservation_remains_held_and_blocks_blind_rerun(self):
        job = self.service.submit(self.actor,self.pid,self.request())
        self.store.append_revision(self.pid,job['object_id'],1,{**job['body'],'state':'unknown'},'worker_service')
        self.store.settle(self.pid,job['body']['reservation_id'],None)
        with self.assertRaises(DomainError):
            self.service.submit(self.actor,self.pid,self.request('retry'))
        self.assertEqual(self.store.budget(self.pid)['reserved'],10)

    def test_observe_paid_and_local_render_cut_have_frozen_manifests(self):
        media = self.f.image()
        request = ObserveRequest(idempotency_key='observe',expected_revision=1,media_id=media['object_id'],questions=['Is the rider ahead?'],reader='image')
        observed = self.service.observe(self.actor,self.pid,request)
        self.assertEqual(self.intent(observed)['body']['request']['questions'],request.questions)
        self.assertEqual(self.store.budget(self.pid)['reserved'],10)
        cut = self.store.create_object(self.pid,'cut',{'segments':[{'take':self.f.ref(media).model_dump(),'start_seconds':0.0,'end_seconds':1.0}],
            'sound_inputs':[], 'dependencies':[self.f.ref(media).model_dump()]},'cut_service')
        rendered = self.service.render_cut(self.actor,self.pid,RenderCutRequest(idempotency_key='render',expected_revision=1,cut=self.f.ref(cut)))
        self.assertIsNone(rendered['body']['reservation_id'])
        self.assertEqual(self.intent(rendered)['body']['request']['manifest'],cut['body'])
        self.assertEqual(self.store.budget(self.pid)['reserved'],10)

    def test_a_recut_film_gets_its_own_render_allowance(self):
        # dry run: renders were counted over the cut's whole life, so after three the film never rendered again.
        media = self.f.image()
        body = {'segments':[{'take':self.f.ref(media).model_dump(),'start_seconds':0.0,'end_seconds':1.0}],
                'sound_inputs':[], 'dependencies':[self.f.ref(media).model_dump()]}
        cut = self.store.create_object(self.pid,'cut',body,'cut_service')
        def render(key, ref):
            job = self.service.render_cut(self.actor,self.pid,RenderCutRequest(idempotency_key=key,expected_revision=ref['revision'],cut=self.f.ref(ref)))
            self.store.append_revision(self.pid,job['object_id'],job['revision'],{**job['body'],'state':'succeeded'},'worker_service')
        for n in range(3):
            render(f'render-{n}', cut)
        with self.assertRaises(DomainError) as spent:
            render('render-4', cut)
        self.assertEqual(spent.exception.code, 'attempt_limit')
        recut = self.store.append_revision(self.pid,cut['object_id'],cut['revision'],{**body,'segments':[{**body['segments'][0],'end_seconds':0.5}]},'cut_service')
        render('render-recut', recut)  # a new cut revision renders again

    def test_concurrent_distinct_requests_cannot_overspend(self):
        self.store.set_budget(self.pid, 15, 'credit')
        barrier = threading.Barrier(2)
        def call(index):
            barrier.wait()
            try:
                return self.service.submit(self.actor,self.pid,self.request(f'race{index}'))['body']['state']
            except DomainError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(call, [1,2]))
        self.assertCountEqual(result, ['queued','budget_exceeded'])
        self.assertEqual(self.store.budget(self.pid)['reserved'],10)
        self.assertEqual(len(self.store.list_objects(self.pid,kind='dispatch-intent')),1)

    def test_credential_expiry_is_checked_at_dispatch_and_reviews_are_not(self):
        job = self.service.submit(self.actor,self.pid,self.request())
        self.service.review_check = lambda *args: {'authorized':False}
        self.service.revalidate(self.pid,self.f.ref(job))
        self.service.review_check = self.review
        self.f.auth.clock = lambda: self.actor.expires_at+1
        with self.assertRaises(DomainError) as error:
            self.service.revalidate(self.pid,self.f.ref(job))
        self.assertEqual(error.exception.code,'unauthorized')
        self.assertEqual(self.store.budget(self.pid)['reserved'],10)

    def test_expired_unclaimed_dispatch_cannot_be_retried_with_new_key(self):
        from production.jobs import Jobs
        now = [self.f.auth.clock()]
        worker = self.f.auth.authenticate(self.f.auth.provision_token('dispatch_worker', 'worker', [self.pid], 300))
        jobs = Jobs(self.store, self.f.auth, self.service, self.f.media, clock=lambda: now[0], lease_seconds=10)
        first = self.service.submit(self.actor, self.pid, self.request('first'))
        claimed = jobs.claim(worker, self.pid, first['object_id'])
        jobs.begin_dispatch(worker, self.pid, self.f.ref(claimed['job']), claimed['fence'])
        now[0] += 11  # No claimant has converted expired dispatching to unknown.
        with self.assertRaises(DomainError) as error:
            self.service.submit(self.actor, self.pid, self.request('lost-response-new-key'))
        self.assertEqual(error.exception.code, 'unknown_outcome')
        self.assertEqual(self.store.get_object(self.pid, first['object_id'])['body']['state'], 'dispatching')
        self.assertEqual(len(self.store.list_objects(self.pid, kind='provider-attempt')), 1)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='job')), 1)
        self.assertEqual(self.store.budget(self.pid)['reserved'], 10)

    def test_queued_single_submission_is_rechecked_at_dispatch_until_first_finishes(self):
        from production.jobs import Jobs
        from production.provider_types import ProviderReceipt
        worker = self.f.auth.authenticate(self.f.auth.provision_token('dispatch_worker', 'worker', [self.pid], 300))
        jobs = Jobs(self.store, self.f.auth, self.service, self.f.media)
        first = self.service.submit(self.actor, self.pid, self.request('first'))
        second = self.service.submit(self.actor, self.pid, self.request('second'))
        claim_a = jobs.claim(worker, self.pid, first['object_id'])
        dispatch_a = jobs.begin_dispatch(worker, self.pid, self.f.ref(claim_a['job']), claim_a['fence'])
        claim_b = jobs.claim(worker, self.pid, second['object_id'])
        with self.assertRaises(DomainError) as error:
            jobs.begin_dispatch(worker, self.pid, self.f.ref(claim_b['job']), claim_b['fence'])
        self.assertEqual(error.exception.code, 'unknown_outcome')
        self.assertEqual(len(self.store.list_objects(self.pid, kind='provider-attempt')), 1)
        jobs.record_receipt(worker, self.pid, self.f.ref(dispatch_a['job']), dispatch_a['fence'],
            ProviderReceipt('remote1', 'nano_banana_pro', 'ip_detected', 'failed', {}, {}, {}))
        dispatched_b = jobs.begin_dispatch(worker, self.pid, self.f.ref(claim_b['job']), claim_b['fence'])
        self.assertEqual(dispatched_b['job']['body']['state'], 'dispatching')
        self.assertEqual(len(self.store.list_objects(self.pid, kind='provider-attempt')), 2)

    def test_known_running_and_inflight_cancel_request_still_block_new_attempt(self):
        for state in ('dispatching', 'submitted', 'running', 'unknown'):
            with self.subTest(state=state):
                # Keep a single first intent; update its state as a trusted worker
                # would so every state is tested without exhausting the allowance.
                if state == 'dispatching':
                    first = self.service.submit(self.actor, self.pid, self.request('first'))
                current = self.store.get_object(self.pid, first['object_id'])
                self.store.append_revision(self.pid, current['object_id'], current['revision'],
                    {**current['body'], 'state': state, 'remote_job_id': 'known-job', 'cancel_requested': True}, 'worker_service')
                with self.assertRaises(DomainError) as error:
                    self.service.submit(self.actor, self.pid, self.request('retry-' + state))
                self.assertEqual(error.exception.code, 'unknown_outcome')
                self.assertEqual(len(self.store.list_objects(self.pid, kind='dispatch-intent')), 1)
        # A different creative target is unaffected by the first target's state.
        other = self.f.draft(self.f.definition(description='A different scene character'))
        method = self.f.choice(other, 'image')
        candidate = self.c.compiler.prepare(self.actor, self.pid, self.c.request(other, method=method, key='other'))
        self.assertEqual(self.service.submit(self.actor, self.pid, self.request('other', candidate))['body']['state'], 'queued')

    def test_an_unresolved_take_blocks_the_same_request_but_not_a_changed_prompt(self):
        # HF-style: each generate is its own job, but the same wire
        # request is never paid twice while its first outcome is unknown (bug hunt).
        first = self.service.submit(self.actor, self.pid, self.request('first'))
        current = self.store.get_object(self.pid, first['object_id'])
        self.store.append_revision(self.pid, current['object_id'], current['revision'],
            {**current['body'], 'state': 'unknown', 'remote_job_id': 'known-job'}, 'worker_service')
        again = self.c.compiler.prepare(self.actor, self.pid, self.c.request(self.target, method=self.method, key='same-card-again'))
        self.assertNotEqual(again['object_id'], self.candidate['object_id'])
        self.assertEqual(again['body']['request'], self.candidate['body']['request'])
        with self.assertRaises(DomainError) as error:
            self.service.submit(self.actor, self.pid, self.request('same-request', again))
        self.assertEqual(error.exception.code, 'unknown_outcome')
        changed = copy.deepcopy(self.target['body'])
        changed['content']['definition']['description'] += ' Now in the evening light.'
        revised = self.store.append_revision(self.pid, self.target['object_id'], self.target['revision'], changed, 'author')
        method = self.f.choice(revised, 'image')
        second = self.c.compiler.prepare(self.actor, self.pid, self.c.request(revised, method=method, key='changed-prompt'))
        self.assertNotEqual(second['body']['request'], self.candidate['body']['request'])
        self.assertEqual(self.service.submit(self.actor, self.pid, self.request('changed', second))['body']['state'], 'queued')

    def test_with_the_stress_switch_on_a_shot_waits_for_ten_stress_takes_per_asset(self):
        # (owner: 可以保留成一个开关).
        from production import switches
        stress = self.four_take_shot()
        shot = self.c.compiler.prepare(self.actor, self.pid, self.c.request(self.target, task='shot', method=self.method,
                                       inputs=[self.selection], key='narrative'))
        tags = sorted({r['tag'] for r in shot['body']['request']['references'] if r.get('role') in ('person', 'place', 'state')})
        self.assertTrue(tags)
        self.store.create_object(self.pid, switches.KIND, {'switches': {'asset_stress_test': True}}, switches.AUTHOR,
                                 object_id=switches.OBJECT_ID)
        with self.assertRaises(DomainError) as refused:
            self.service.submit(self.actor, self.pid, self.request('too-early', shot))
        self.assertEqual(refused.exception.code, 'missing_prerequisite')
        self.assertIn(f'{tags[0]} has 0', refused.exception.message)
        # takes of another image under the same tags (a rebuilt asset) do not count.
        request = copy.deepcopy(stress['body']['request'])
        for r in request['references']:
            r['sha256'] = 'f' * 64
        old = self.store.create_object(self.pid, 'candidate', {'task': 'stress', 'request': request}, 'compiler_service')
        for n in range(10):
            self.store.create_object(self.pid, 'media', {'media_type': 'video/mp4',
                                     'dependencies': [self.f.ref(old).model_dump()]}, 'worker_service')
        with self.assertRaises(DomainError) as still:
            self.service.submit(self.actor, self.pid, self.request('old-image', shot))
        self.assertIn(f'{tags[0]} has 0', still.exception.message)
        for n in range(10):
            intent = self.store.create_object(self.pid, 'dispatch-intent', {'candidate': self.f.ref(stress).model_dump(),
                                              'dependencies': [self.f.ref(stress).model_dump()]}, 'submission_service')
            self.store.create_object(self.pid, 'media', {'media_type': 'video/mp4',
                                     'dependencies': [self.f.ref(intent).model_dump()]}, 'worker_service')
        self.assertEqual(self.service.submit(self.actor, self.pid, self.request('after-ten', shot))['body']['state'], 'queued')

    def test_turning_the_stress_switch_on_never_refuses_a_take_already_queued(self):
        # the worker's re-check of a queued job skips the stress count.
        from production import switches
        self.four_take_shot()
        shot = self.c.compiler.prepare(self.actor, self.pid, self.c.request(self.target, task='shot', method=self.method,
                                       inputs=[self.selection], key='queued-first'))
        job = self.service.submit(self.actor, self.pid, self.request('queued-first', shot))
        self.store.create_object(self.pid, switches.KIND, {'switches': {'asset_stress_test': True}}, switches.AUTHOR,
                                 object_id=switches.OBJECT_ID)
        self.assertEqual(self.service.revalidate(self.pid, self.f.ref(job))['body']['candidate']['object_id'], shot['object_id'])

    def test_live_policy_alone_cannot_enable_network_and_wrong_reader_is_denied(self):
        policy = copy.deepcopy(self.policy)
        policy['operations']['submit']['nano_banana_pro'].update(mode='live',live_controls={
            'pricing_verified':True,'isolation_verified':True,'account_limits_verified':True})
        self.f.config.set('execution_policy', policy)
        self.c.activate()
        candidate = self.c.compiler.prepare(self.actor,self.pid,self.c.request(self.target,method=self.method,key='live'))
        with self.assertRaises(DomainError):
            self.service.submit(self.actor,self.pid,self.request('live',candidate))
        media = self.f.image()
        with self.assertRaises(DomainError):
            self.service.observe(self.actor,self.pid,ObserveRequest(idempotency_key='bad-reader',
                expected_revision=1,media_id=media['object_id'],questions=['What moved?'],reader='video'))
        self.assertEqual(self.store.budget(self.pid)['reserved'],0)

    def test_explicit_policy_required_and_bool_cost_cannot_reserve(self):
        for value in (True,-1,2**63):
            policy = copy.deepcopy(self.policy)
            policy['operations']['submit']['nano_banana_pro']['reservation']=value
            self.f.config.set('execution_policy', policy)
            self.c.activate()
            candidate = self.c.compiler.prepare(self.actor,self.pid,self.c.request(self.target,method=self.method,key=f'cost{value}'))
            with self.assertRaises(DomainError):
                self.service.submit(self.actor,self.pid,self.request(f'submit{value}',candidate))
        self.assertEqual(self.store.list_objects(self.pid,kind='job'),[])


class IndependentSubmissionTests(unittest.TestCase):
    def setUp(self):
        # built on the compiler fixture; no review service.
        self.c = test_compiler.CompilerTests()
        self.c.setUp()
        self.addCleanup(self.c.doCleanups)
        self.f,self.store,self.pid = self.c.f,self.c.f.store,'project_1'
        self.actor = self.c.actor
        self.worker = self.f.auth.authenticate(self.f.auth.provision_token('worker', 'worker', ['project_1'], 300))
        self.cost = {'mode':'fake','budget_unit':'credit','estimated_cost':8,'reservation':10,'max_attempts':3,
                     'live_controls':{'pricing_verified':False,'isolation_verified':False,'account_limits_verified':False}}
        self.f.config.set('execution_policy', {'operations':{
            'submit':{'nano_banana_pro':self.cost},'observe':{'image':self.cost}}})
        self.c.activate()
        self.target = self.f.draft(self.f.definition(recipe='pose',reference_roles=['identity']))
        self.image = self.f.image()
        self.method = self.f.choice(self.target,'image')
        self.candidate = self.c.compiler.prepare(self.actor,self.pid,
            self.c.request(self.target,method=self.method,inputs=[self.image],key='bound-prepare'))
        self.store.set_budget(self.pid,100,'credit')
        self.gates = Gates(self.store,self.f.flow,self.f.media)
        self.service = Submissions(self.store,self.f.auth,self.f.flow,self.gates)
        self.jobs = Jobs(self.store,self.f.auth,self.service,self.f.media)

    def request(self,key='submit'):
        return SubmitRequest(idempotency_key=key,expected_revision=1,candidate_id=self.candidate['object_id'])

    def test_generation_needs_no_review_and_lists_review_roles_as_advice(self):
        # Owner decision 2026-09-23: judgment reviews are advice beside the work, never a generation gate.
        job = self.service.submit(self.actor,self.pid,self.request())
        intent = self.store.get_object(self.pid,job['body']['intent']['object_id'])
        self.assertEqual(intent['body']['review']['receipts'],[])
        claim = self.jobs.claim(self.worker,self.pid,job['object_id'])
        dispatch = self.jobs.begin_dispatch(self.worker,self.pid,self.f.ref(claim['job']),claim['fence'])
        self.assertEqual(dispatch['job']['body']['state'],'dispatching')
        self.assertEqual(self.store.budget(self.pid)['reserved'],10)

    def test_forged_reports_never_enter_the_generation_authority(self):
        forged = [self.f.ref(self.store.create_object(self.pid,kind,{'target':self.f.ref(self.candidate).model_dump(),
            'verdict':'pass','role':'standards','authorized':True,'accepted':True},'author')).model_dump()
            for kind in ('observation','review-receipt','agent-report')]
        job = self.service.submit(self.actor,self.pid,self.request())
        intent = self.store.get_object(self.pid,job['body']['intent']['object_id'])['body']
        self.assertEqual(intent['review']['receipts'],[])
        self.assertFalse(any(ref in intent['dependencies'] for ref in forged))

    def test_feedback_after_queue_is_advice_and_dispatch_goes_ahead(self):
        # HF-style: once a take is ordered it is generated; new feedback is advice for the next version.
        job = self.service.submit(self.actor,self.pid,self.request())
        self.store.create_object(self.pid,'feedback',{'content':'Wrong character identity.',
            'dependencies':[self.f.ref(self.candidate).model_dump()]},'human')
        claim = self.jobs.claim(self.worker,self.pid,job['object_id'])
        self.jobs.begin_dispatch(self.worker,self.pid,self.f.ref(claim['job']),claim['fence'])
        self.assertEqual(len(self.store.list_objects(self.pid,kind='provider-attempt')),1)

    def test_deterministic_gate_still_blocks_a_stale_reference(self):
        changed = copy.deepcopy(self.target['body'])
        changed['content']['definition']['description'] += ' revised'
        current = self.store.append_revision(self.pid,self.target['object_id'],1,changed,'author')
        with self.assertRaises(DomainError) as exc:
            self.service.submit(self.actor,self.pid,self.request())
        self.assertEqual(exc.exception.code,'rule_violation')
        self.assertEqual(self.store.budget(self.pid)['reserved'],0)
        self.method = self.f.choice(current,'image')
        self.candidate = self.c.compiler.prepare(self.actor,self.pid,
            self.c.request(current,method=self.method,inputs=[self.image],key='new-candidate'))
        self.assertEqual(self.service.submit(self.actor,self.pid,self.request('new'))['body']['state'],'queued')

    def test_live_dispatch_rejects_legacy_checker_even_when_service_controls_are_on(self):
        self.cost.update(mode='live',budget_key='hf_main',live_controls={'pricing_verified':True,'isolation_verified':True,'account_limits_verified':True})
        self.f.config.set('execution_policy', {'operations':{'submit':{'nano_banana_pro':self.cost}}})
        self.c.activate()
        self.candidate = self.c.compiler.prepare(self.actor,self.pid,
            self.c.request(self.target,method=self.method,inputs=[self.image],key='live-candidate'))
        calls = []
        def checker(pid,candidate,policy,conn):
            calls.append(1)
            return {'authorized':True,'candidate':self.f.ref(candidate).model_dump(),'policy_hash':content_hash(policy),
                    'roles':policy['required_roles'],'receipts':[]}
        legacy = Submissions(self.store,self.f.auth,self.f.flow,self.gates,review_check=checker,live_enabled=True)
        with self.assertRaises(DomainError):
            legacy.submit(self.actor,self.pid,self.request())
        self.assertEqual(calls,[])
        self.assertEqual(self.store.budget(self.pid)['reserved'],0)

class FalCompletionTests(unittest.TestCase):
    """The owner's picked fal draft is completed to 1080p at most once."""
    @classmethod
    def setUpClass(cls):
        import subprocess
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'draft.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=854x480:r=4:d=4', '-f', 'lavfi',
                            '-i', 'anullsrc=r=8000:cl=mono', '-shortest', '-c:a', 'aac', '-t', '4', '-c:v', 'libx264',
                            '-preset', 'ultrafast', '-threads', '1', '-pix_fmt', 'yuv420p', str(path)], check=True, timeout=30)
            cls.draft_video = path.read_bytes()
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=red:s=1920x1080:r=4:d=4', '-f', 'lavfi',
                            '-i', 'anullsrc=r=8000:cl=mono', '-shortest', '-c:a', 'aac', '-t', '4', '-c:v', 'libx264',
                            '-preset', 'ultrafast', '-threads', '1', '-pix_fmt', 'yuv420p', str(path)], check=True, timeout=30)
            cls.complete_video = path.read_bytes()

    def setUp(self):
        from contextlib import contextmanager
        from production.jobs import Download, SafeDownloader
        self.s = SubmissionTests()
        self.s.setUp()
        self.addCleanup(self.s.doCleanups)
        s, self.now = self.s, getattr(self, 'start', 1000.0)
        self.store, self.pid, self.f = s.store, s.pid, s.f
        draft = {**s.cost, 'max_attempts': 4}
        s.policy['operations']['submit']['fal_seedance_2_5'] = draft
        s.policy['operations']['complete-draft'] = {'fal_seedance_2_5_complete': {**s.cost, 'max_attempts': 1}}
        s.f.config.set('execution_policy', s.policy)
        # A tiny released cut output keeps the cut of a completion cheap.
        cut = hf_era_document('cut_policy')
        cut['output'].update(width=64, height=36, fps=4)
        s.f.config.set('cut_policy', cut)
        target, selection, method, _ = s.c.video(fal=True)
        self.shot = target
        candidate = s.c.compiler.prepare(s.actor, self.pid, s.c.request(target, task='shot', method=method,
                                                                        inputs=[selection], key='fal-draft'))
        self.store.set_budget(self.pid, 100, 'credit')
        s.service.clock = lambda: self.now
        self.worker = self.f.auth.authenticate(self.f.auth.provision_token('worker', 'worker', [self.pid], 300))
        self.video = self.draft_video
        @contextmanager
        def download(url, host, ip, timeout):
            yield Download('video/mp4', [self.video])
        self.jobs = Jobs(self.store, self.f.auth, s.service, self.f.media, clock=lambda: self.now, lease_seconds=20,
                         downloader=SafeDownloader({'v3b.fal.media'}, resolver=lambda host: ['8.8.8.8'], transport=download))
        self.takes = [self.draft_take(candidate, n) for n in range(2)]

    def draft_take(self, candidate, n):
        from production.provider_types import ProviderReceipt
        job = self.s.service.submit(self.s.actor, self.pid, self.s.request(f'fal-{n}', candidate))
        claim = self.jobs.claim(self.worker, self.pid, job['object_id'])
        job = self.jobs.begin_dispatch(self.worker, self.pid, self.f.ref(claim['job']), claim['fence'])['job']
        receipt = ProviderReceipt(f'01a0d7fa-efe3-79b2-b20f-62cc6d49e74{n}', 'fal_seedance_2_5', 'COMPLETED', 'succeeded',
                                  {'request_id': 'r', 'seed': 7, 'draft_id': f'draft_{n}', 'draft_expires_at': int(self.now) + 7 * 86400},
                                  {}, {}, result_url='https://v3b.fal.media/files/v.mp4')
        job = self.jobs.record_receipt(self.worker, self.pid, self.f.ref(job), claim['fence'], receipt)
        job = self.jobs.download_result(self.worker, self.pid, self.f.ref(job), claim['fence'])
        return self.store.get_object(self.pid, job['body']['result']['object_id'])

    def pick(self, take):
        # Same shape as decisions.py writes on a confirmed pick (a withdrawal writes take None).
        receipt = self.store.create_object(self.pid, 'human-receipt', {'choice': 'confirm'}, 'decision_service')
        refs = [self.f.ref(self.shot).model_dump(), *([self.f.ref(take).model_dump()] if take else []), self.f.ref(receipt).model_dump()]
        self.store.create_object(self.pid, 'human-take-selection', {
            'shot': self.f.ref(self.shot).model_dump(), 'take': self.f.ref(take).model_dump() if take else None, 'reason': None,
            'human_receipt': self.f.ref(receipt).model_dump(), 'verified_human_session': True, 'dependencies': refs},
            'decision_service')

    def complete(self, take):
        return self.s.service.complete_draft(self.worker, self.pid, self.f.ref(take))

    def completed(self, take):
        """Pick the take, complete it and bring back its 1080p take through the real job path."""
        from production.provider_types import ProviderReceipt
        self.pick(take)
        job = self.complete(take)
        claim = self.jobs.claim(self.worker, self.pid, job['object_id'])
        job = self.jobs.begin_dispatch(self.worker, self.pid, self.f.ref(claim['job']), claim['fence'])['job']
        receipt = ProviderReceipt('01a0d7fd-15d3-7b00-923a-3cc37b30b3b7', 'fal_seedance_2_5_complete', 'COMPLETED', 'succeeded',
                                  {'request_id': 'c', 'seed': 7}, {}, {}, result_url='https://v3b.fal.media/files/c.mp4')
        job = self.jobs.record_receipt(self.worker, self.pid, self.f.ref(job), claim['fence'], receipt)
        self.video = self.complete_video
        job = self.jobs.download_result(self.worker, self.pid, self.f.ref(job), claim['fence'])
        self.video = self.draft_video
        return self.store.get_object(self.pid, job['body']['result']['object_id'])

    def test_the_current_pick_is_completed_once_with_its_draft_id_and_settings(self):
        self.pick(self.takes[0])
        job = self.complete(self.takes[0])
        intent = self.s.intent(job)['body']
        self.assertEqual(intent['operation'], 'complete-draft')
        self.assertEqual(intent['request'], {'job_type': 'fal_seedance_2_5_complete', 'references': [], 'params': {
            'draft_id': 'draft_0', 'resolution': '1080p', 'duration': 4, 'aspect_ratio': '16:9', 'generate_audio': True}})
        self.assertEqual(intent['origin']['role'], 'agent')  # the maker who ordered the draft stays the authority
        # pick -> withdraw -> pick again: still the same one completion
        self.pick(None)
        self.pick(self.takes[0])
        self.assertEqual(self.complete(self.takes[0])['object_id'], job['object_id'])
        with self.store.transaction() as db:
            self.assertEqual(self.s.service.revalidate(self.pid, self.f.ref(job), conn=db)['object_id'], job['body']['intent']['object_id'])

    def test_only_the_current_pick_and_only_inside_seven_days(self):
        with self.assertRaises(DomainError):
            self.complete(self.takes[0])  # no pick yet
        self.pick(self.takes[1])
        with self.assertRaises(DomainError):
            self.complete(self.takes[0])  # another take is the pick
        agent = self.s.actor
        with self.assertRaises(DomainError):
            self.s.service.complete_draft(agent, self.pid, self.f.ref(self.takes[1]))  # the worker orders completions
        self.now = 1000 + 7 * 86400
        with self.assertRaises(DomainError) as expired:
            self.complete(self.takes[1])
        self.assertEqual(expired.exception.code, 'stale_input')

    def finish_completion(self, job, state, settled):
        """The worker's outcome for a completion job: a fal refusal (settled 0), a failed render (no charge known)."""
        from production.provider_types import ProviderReceipt
        claim = self.jobs.claim(self.worker, self.pid, job['object_id'])
        job = self.jobs.begin_dispatch(self.worker, self.pid, self.f.ref(claim['job']), claim['fence'])['job']
        receipt = ProviderReceipt('fal-refused-' + job['object_id'][-24:] if settled == 0 else '01a0d7fd-15d3-7b00-923a-3cc37b30b3b8',
                                  'fal_seedance_2_5_complete', 'refused' if settled == 0 else 'error', state,
                                  {'error': 'x'}, {}, {}, settled_cost=settled)
        return self.jobs.record_receipt(self.worker, self.pid, self.f.ref(job), claim['fence'], receipt)

    def test_a_completion_that_provably_cost_nothing_is_tried_again_once_more(self):
        # a pick must reach 1080p or say why; a retry only when nothing could have been paid.
        self.s.policy['operations']['complete-draft']['fal_seedance_2_5_complete']['max_attempts'] = 2
        self.f.config.set('execution_policy', self.s.policy)
        self.pick(self.takes[0])
        first = self.complete(self.takes[0])
        self.finish_completion(first, 'failed', 0)  # fal refused it at the queue: it never ran
        second = self.complete(self.takes[0])
        self.assertNotEqual(second['object_id'], first['object_id'])
        self.assertEqual(self.s.intent(second)['body']['attempt'], 2)
        self.finish_completion(second, 'failed', 0)
        with self.assertRaises(DomainError) as spent:
            self.complete(self.takes[0])
        self.assertEqual(spent.exception.code, 'attempt_limit')

    def test_a_completion_with_an_unknown_or_possibly_paid_outcome_is_never_sent_again(self):
        self.s.policy['operations']['complete-draft']['fal_seedance_2_5_complete']['max_attempts'] = 2
        self.f.config.set('execution_policy', self.s.policy)
        self.pick(self.takes[0])
        first = self.complete(self.takes[0])
        self.finish_completion(first, 'failed', None)  # the render failed after fal took it: the charge is not known
        self.assertEqual(self.complete(self.takes[0])['object_id'], first['object_id'])
        self.pick(self.takes[1])
        other = self.complete(self.takes[1])
        claim = self.jobs.claim(self.worker, self.pid, other['object_id'])
        self.jobs.record_unknown(self.worker, self.pid, self.f.ref(claim['job']), claim['fence'])
        self.assertEqual(self.complete(self.takes[1])['object_id'], other['object_id'])

    def test_a_pick_withdrawn_before_dispatch_stops_the_completion(self):
        self.pick(self.takes[0])
        job = self.complete(self.takes[0])
        self.pick(self.takes[1])
        with self.store.transaction() as db, self.assertRaises(DomainError):
            self.s.service.revalidate(self.pid, self.f.ref(job), conn=db)
