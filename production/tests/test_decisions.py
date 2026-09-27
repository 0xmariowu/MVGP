"""Final acceptance requires an independent human session and current evidence."""
import unittest

from pydantic import ValidationError

from production.contracts import (
    DecisionRequest,
    DomainError,
    HumanDecision,
)
from production.decisions import Decisions
from production.tests import test_cuts
from production.tests.fixtures import owner_session


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.c = test_cuts.CutTests()
        self.c.setUp()
        self.addCleanup(self.c.doCleanups)
        self.f,self.store,self.pid,self.actor = self.c.f,self.c.store,self.c.pid,self.c.actor
        self.c.c.activate()
        # Release publication changes the project; regenerate its intent before editing.
        target,selection,method,_ = self.c.c.video()
        self.c.candidate = self.c.c.compiler.prepare(self.actor,self.pid,
            self.c.c.request(target,task='stress',method=method,inputs=[selection]).model_copy(update={'idempotency_key':'decision-source'}))
        self.c.take = self.store.create_object(self.pid,'media',{**self.c.take['body'],
            'dependencies':[self.ref(self.c.candidate).model_dump()]},'worker_service')
        self.cut = self.c.service.create(self.actor,self.pid,self.c.request())
        self.media = self.c.render(self.cut)
        for stage in ('sound','cleanup','color','delivery'):
            self.store.create_object(self.pid,'finishing',{'content':{'stage':stage,'status':'completed',
                'notes':'Synthetic verified handoff','cut':self.ref(self.cut).model_dump(),'media':self.ref(self.media).model_dump(),
                'evidence':[self.ref(self.media).model_dump()]},
                'dependencies':[self.ref(self.cut).model_dump(),self.ref(self.media).model_dump()]},'author')
        # A legacy director receipt, as old records carry (no reviewer runs).
        self.receipt = self.store.create_object(self.pid,'review-receipt',{
            'target':self.ref(self.media).model_dump(),'purpose':'cut','role':'director','verdict':'pass','issues':[],
            'route_profile_hash':'0'*64,
            'dependencies':[self.ref(self.media).model_dump()]},'review_service')
        self.now = 1000.0
        self.service = Decisions(self.store,self.f.auth,self.f.flow,self.c.service,receipt_check=self.check,
                                 human_confirmation_enabled=True,clock=lambda:self.now,ttl_seconds=60)
        self.session = owner_session(self.f.auth)
        self.human = self.f.auth.authenticate(self.session['session_token'],channel='cookie')

    def ref(self,obj):
        return self.f.ref(obj)

    def check(self,*args,**kwargs):
        # Explicit trusted fixture; production uses ReviewTasks.valid_receipts.
        return [self.ref(self.receipt).model_dump()]

    def request(self,**values):
        return DecisionRequest(**{'idempotency_key':'ask','expected_revision':1,'target':self.ref(self.media),
            'purpose':'final','rationale':'Please watch this exact complete cut.',**values})

    def answer(self,pending,**values):
        return HumanDecision(**{'idempotency_key':'answer','request_id':pending['object_id'],
            'target_hash':pending['body']['target_hash'],'choice':'confirm','csrf_token':self.session['csrf_token'],**values})

    def take_request(self, **values):
        shot = self.c.candidate['body']['target']
        return self.request(purpose='take', **{'target': shot, 'shot': shot,
            'expected_revision': shot['revision'], 'takes': [self.ref(self.c.take)], **values})

    def test_human_picks_one_offered_take(self):
        from production.context import ContextService
        other = self.store.create_object(self.pid, 'media', self.c.take['body'], 'worker_service')
        pending = self.service.request(self.actor, self.pid,
            self.take_request(takes=[self.ref(self.c.take), self.ref(other)]))
        receipt = self.service.decide(self.human, self.pid,
            self.answer(pending, selected_take=self.ref(other), reason='The movement reads clearly.'), origin='https://studio.example')
        selection, = self.store.list_objects(self.pid, kind='human-take-selection')
        shot, take = pending['body']['target'], self.ref(other).model_dump()
        self.assertEqual(selection['author'], 'decision_service')
        self.assertEqual(selection['body'], {'shot': shot, 'take': take, 'reason': 'The movement reads clearly.',
            'human_receipt': self.ref(receipt).model_dump(), 'verified_human_session': True,
            'dependencies': [shot, take, self.ref(receipt).model_dump()]})
        self.assertTrue(receipt['body']['verified_human_session'])
        self.assertEqual(receipt['body']['purpose'], 'take')
        self.assertEqual(self.store.get_object(self.pid, pending['object_id'])['body']['state'], 'confirmed')
        context = ContextService(self.store, self.f.auth, self.f.flow)
        result = context.get(self.actor, self.pid, target=self.ref(other))
        self.assertTrue(any(row['kind'] == 'human-take-selection' and row['body']['take'] == take
                            for row in result['prior_results']))

    def test_owner_re_picks_and_un_picks_and_the_latest_record_wins(self):
        # (owner 2026-09-24 "我选好了，能否退回"): history is kept, the latest human record wins.
        from production.queries import creation_order, current_picks
        other = self.store.create_object(self.pid, 'media', self.c.take['body'], 'worker_service')
        pending = self.service.request(self.actor, self.pid,
            self.take_request(takes=[self.ref(self.c.take), self.ref(other)]))
        shot_id = pending['body']['target']['object_id']
        def decide(key, **values):
            return self.service.decide(self.human, self.pid, self.answer(pending, idempotency_key=key, **values),
                                       origin='https://studio.example')
        def current():
            with self.store.transaction(write=False) as db:
                rows = self.store.list_objects(self.pid, kind='human-take-selection', conn=db)
                order = creation_order(db, self.pid, [r['object_id'] for r in rows])
            views = [{'details': {'shot': r['body']['shot'], 'take': r['body']['take'] or {}}}
                     for r in sorted(rows, key=lambda r: order[r['object_id']])]
            pick = current_picks(views).get(shot_id)
            return pick and pick['details']['take']
        state = lambda: self.store.get_object(self.pid, pending['object_id'])['body']
        decide('pick-1', selected_take=self.ref(self.c.take))
        self.assertEqual(current(), self.ref(self.c.take).model_dump())
        decide('pick-2', selected_take=self.ref(other), reason='Second one reads better.')
        self.assertEqual((state()['state'], current()), ('confirmed', self.ref(other).model_dump()))
        self.now += 500  # long after the request window: an answered pick can still be changed
        receipt = decide('unpick', choice='decline', reason='取消')
        self.assertFalse(receipt['body']['accepted'])
        self.assertIsNone(current())
        self.assertEqual((state()['state'], state()['expires_at']), ('pending', self.now + 60))
        unpick = max(self.store.list_objects(self.pid, kind='human-take-selection'), key=lambda r: r['created_at'])
        self.assertEqual((unpick['body']['take'], unpick['body']['reason']), (None, '取消'))
        decide('pick-3', selected_take=self.ref(self.c.take))
        self.assertEqual((state()['state'], current()), ('confirmed', self.ref(self.c.take).model_dump()))
        self.assertEqual(len(self.store.list_objects(self.pid, kind='human-take-selection')), 4)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='human-receipt')), 4)

    def test_the_owners_sentence_on_a_rebatch_reaches_the_agent(self):
        # dry run: 再拍一批 / 都不行 recorded the button but dropped the owner's sentence.
        pending = self.service.request(self.actor, self.pid, self.take_request())
        receipt = self.service.decide(self.human, self.pid, self.answer(pending, choice='decline',
                                      reason='再拍一批：她停得太早，要走到门口再停。'), origin='https://studio.example')
        self.assertEqual(receipt['body']['reason'], '再拍一批：她停得太早，要走到门口再停。')
        from production.queries import Queries
        seen = Queries(self.store, self.f.auth, self.f.flow).artifact(self.actor, self.pid, receipt['object_id'])
        self.assertEqual(seen['details']['reason'], '再拍一批：她停得太早，要走到门口再停。')

    def test_withdrawing_a_new_cards_pick_gives_the_shot_back_its_earlier_pick(self):
        # Release 82 rehearsal walk: 撤销选择 on a new round wiped the shot's pick from an earlier round.
        from production.queries import creation_order, current_picks
        other = self.store.create_object(self.pid, 'media', self.c.take['body'], 'worker_service')
        first = self.service.request(self.actor, self.pid, self.take_request(takes=[self.ref(self.c.take)]))
        shot_id = first['body']['target']['object_id']
        def decide(pending, key, **values):
            return self.service.decide(self.human, self.pid, self.answer(pending, idempotency_key=key, **values),
                                       origin='https://studio.example')
        def current():
            with self.store.transaction(write=False) as db:
                rows = self.store.list_objects(self.pid, kind='human-take-selection', conn=db)
                order = creation_order(db, self.pid, [r['object_id'] for r in rows])
            views = [{'details': {'shot': r['body']['shot'], 'take': r['body']['take'] or {}}}
                     for r in sorted(rows, key=lambda r: order[r['object_id']])]
            pick = current_picks(views).get(shot_id)
            return pick and pick['details']['take']
        decide(first, 'round-1', selected_take=self.ref(self.c.take))
        second = self.service.request(self.actor, self.pid, self.take_request(takes=[self.ref(other)], idempotency_key='round-2'))
        decide(second, 'round-2', selected_take=self.ref(other))
        self.assertEqual(current(), self.ref(other).model_dump())
        decide(second, 'undo-round-2', choice='decline', reason='取消')
        self.assertEqual(current(), self.ref(self.c.take).model_dump())  # the earlier round's pick is back
        decide(first, 'undo-round-1', choice='decline', reason='取消')
        self.assertIsNone(current())

    def test_a_pick_change_supersedes_the_pending_final_in_the_same_decision(self):
        # A film offered before the change no longer shows the owner's picks; it must not be confirmable meanwhile.
        final = self.service.request(self.actor, self.pid, self.request())
        pending = self.service.request(self.actor, self.pid, self.take_request(idempotency_key='take-ask'))
        self.service.decide(self.human, self.pid, self.answer(pending, idempotency_key='pick', selected_take=self.ref(self.c.take)),
                            origin='https://studio.example')
        self.assertEqual(self.store.get_object(self.pid, final['object_id'])['body']['state'], 'superseded')
        with self.assertRaises(DomainError):
            self.service.decide(self.human, self.pid, self.answer(final, idempotency_key='late'), origin='https://studio.example')

    def test_declined_take_request_can_still_be_picked(self):
        pending = self.service.request(self.actor, self.pid, self.take_request())
        self.service.decide(self.human, self.pid, self.answer(pending, choice='decline', reason='Try again.'),
                            origin='https://studio.example')
        with self.assertRaises(DomainError):  # nothing to un-pick
            self.service.decide(self.human, self.pid, self.answer(pending, idempotency_key='again', choice='decline',
                                reason='Still no.'), origin='https://studio.example')
        self.service.decide(self.human, self.pid, self.answer(pending, idempotency_key='changed-mind',
                            selected_take=self.ref(self.c.take)), origin='https://studio.example')
        self.assertEqual(self.store.get_object(self.pid, pending['object_id'])['body']['state'], 'confirmed')

    def test_after_the_film_is_confirmed_the_owner_can_still_change_a_pick(self):
        # (replaces test_picture_locked_shot_cannot_be_re_picked): the lock binds the agent;
        # the owner's re-pick or un-pick reopens that shot and the cut so the film is cut again.
        from production.workflow import SERVICE_AUTHOR
        pending = self.service.request(self.actor, self.pid, self.take_request())
        self.service.decide(self.human, self.pid, self.answer(pending, selected_take=self.ref(self.c.take)),
                            origin='https://studio.example')
        cut = self.store.create_object(self.pid, 'cut', {'dependencies': [pending['body']['target']]}, 'cut_service')
        cut_ref = self.ref(cut).model_dump()
        lock = self.store.create_object(self.pid, 'picture-lock', {'state': 'locked', 'cut': cut_ref, 'reopened_targets': [],
                                        'protected': [pending['body']['target'], cut_ref]}, SERVICE_AUTHOR)
        self.service.decide(self.human, self.pid, self.answer(pending, idempotency_key='re-pick', selected_take=self.ref(self.c.take)),
                            origin='https://studio.example')
        body = self.store.get_object(self.pid, lock['object_id'])['body']
        self.assertEqual(body['state'], 'partially-reopened')
        self.assertIn(pending['body']['target']['object_id'], body['reopened_targets'])
        self.assertIn(cut['object_id'], body['reopened_targets'])
        self.service.decide(self.human, self.pid, self.answer(pending, idempotency_key='un-pick', choice='decline', reason='取消'),
                            origin='https://studio.example')
        self.assertEqual(len(self.store.list_objects(self.pid, kind='human-take-selection')), 3)

    def plan_scene(self, *, second_card=True):
        text = ('# S03-DOOR\n## 人物\n- @ann：安\n\n## 镜头清单（preliminary shotlist）\n\n'
                '| 镜头号 | 景别 · 机位 | 上次拍出来 | 一句话 | 秒 | 为什么这样拍 | 复杂度 |\n|---|---|---|---|---|---|---|\n'
                '| S03-010A | Wide · hall side | 她没停下就进门了 | Ann reaches the door. | 6 | 一段一镜：the approach reads in one take | simple |\n'
                '| S03-020A | Insert | | Her hand stops on the handle. | 4 | a critical detail needs an insert close-up | simple |\n')
        scene = self.store.create_object(self.pid, 'scene', {'content': text}, 'author')
        cards = []
        for label, seconds, delivery in (('S03-010A', 6, [{'speaker': 'ann', 'line': '等等。', 'instruction': 'quiet',
                                                           'start seconds': 1.0, 'end seconds': 2.0}]),
                                          ('S03-020A', 4, [])):
            if label == 'S03-020A' and not second_card:
                continue
            content = {'shot': label, 'The material': {'the running time in seconds': seconds,
                           'the lines verbatim': 'ANN: 等等。' if delivery else ''},
                       'Direction': {'the goal of the shot in one line': f'{label} goal',
                                     'The task — what the character does to get what he wants, as a verb': 'to stop',
                                     'The dramaturgy — what changed between the start and the end': 'moving → still'},
                       'Edit': {'how this shot hooks into the next one': 'cut on the stop'},
                       'Audio': {'delivery': delivery}}
            cards.append(self.store.create_object(self.pid, 'shot', {'content': content,
                         'dependencies': [self.ref(scene).model_dump()]}, 'author'))
        return scene, cards

    def test_there_is_no_shot_plan_request_any_more(self):
        # Higgsfield has no shot-plan approval step.
        scene, _ = self.plan_scene()
        with self.assertRaises(DomainError) as caught:
            self.service.request(self.actor, self.pid, self.request(purpose='shot-plan', target=self.ref(scene),
                                 idempotency_key='plan-ask', rationale='Shot plan for S03.'))
        self.assertEqual(caught.exception.code, 'invalid_input')
        self.assertEqual(self.store.list_objects(self.pid, kind='decision-request'), [])

    def test_pick_outside_the_request_is_rejected(self):
        pending = self.service.request(self.actor, self.pid, self.take_request())
        other = self.store.create_object(self.pid, 'media', self.c.take['body'], 'worker_service')
        for selected in (None, self.ref(other), self.ref(self.c.take).model_copy(update={'digest': 'f'*64}),
                         self.ref(self.c.take).model_copy(update={'revision': 2}),
                         self.ref(self.c.take).model_copy(update={'digest': None})):
            with self.subTest(selected=selected), self.assertRaises(DomainError):
                self.service.decide(self.human, self.pid, self.answer(pending, selected_take=selected),
                                    origin='https://studio.example')
        self.assertEqual(self.store.list_objects(self.pid, kind='human-receipt'), [])
        self.assertEqual(self.store.get_object(self.pid, pending['object_id'])['body']['state'], 'pending')

    def test_an_earlier_batch_stays_pickable_after_the_card_moves_on(self):
        # (was test_stale_take_rejected): after 再拍一批 the agent patches the card; the owner
        # may still pick a take of the earlier batch, but 再拍一批 / 都不行 needs the current pending batch.
        shot = self.store.create_object(self.pid, 'shot', {'content': 'Another shot'}, 'author')
        candidate = self.store.create_object(self.pid, 'candidate', {**self.c.candidate['body'],
            'target': self.ref(shot).model_dump()}, 'compiler_service')
        take = self.store.create_object(self.pid, 'media', {**self.c.take['body'],
            'dependencies': [self.ref(candidate).model_dump()]}, 'worker_service')
        with self.assertRaises(DomainError):
            self.service.request(self.actor, self.pid, self.take_request(takes=[self.ref(take)]))
        pending = self.service.request(self.actor, self.pid, self.take_request())
        source = self.c.candidate['body']['target']
        current = self.store.get_object(self.pid, source['object_id'])
        self.store.append_revision(self.pid, current['object_id'], current['revision'], current['body'], 'author')
        with self.assertRaises(DomainError):
            self.service.decide(self.human, self.pid, self.answer(pending, choice='decline', reason='Again.'),
                                origin='https://studio.example')
        self.service.decide(self.human, self.pid, self.answer(pending, selected_take=self.ref(self.c.take)),
                            origin='https://studio.example')
        picks = self.store.list_objects(self.pid, kind='human-take-selection')
        self.assertEqual([p['body']['take'] for p in picks], [self.ref(self.c.take).model_dump()])

    def test_a_day_old_offer_is_still_pickable(self):
        # take requests never expire (the owner may open the desk days later).
        pending = self.service.request(self.actor, self.pid, self.take_request())
        self.now += 10 * 86400
        self.service.decide(self.human, self.pid, self.answer(pending, selected_take=self.ref(self.c.take)),
                            origin='https://studio.example')
        self.assertEqual(self.store.get_object(self.pid, pending['object_id'])['body']['state'], 'confirmed')

    def test_undo_takes_back_a_rebatch_answer_until_the_card_changes(self):
        # (owner: "这个得做啊").
        pending = self.service.request(self.actor, self.pid, self.take_request())
        self.service.decide(self.human, self.pid, self.answer(pending, choice='decline', reason='再拍一批：光太暗'),
                            origin='https://studio.example')
        receipt = self.service.decide(self.human, self.pid, self.answer(pending, choice='undo', idempotency_key='undo-1'),
                                      origin='https://studio.example')
        self.assertEqual(receipt['body']['choice'], 'undo')
        self.assertEqual(self.store.get_object(self.pid, pending['object_id'])['body']['state'], 'pending')
        with self.assertRaises(DomainError):  # nothing to undo now
            self.service.decide(self.human, self.pid, self.answer(pending, choice='undo', idempotency_key='undo-2'),
                                origin='https://studio.example')
        self.service.decide(self.human, self.pid, self.answer(pending, choice='decline', idempotency_key='again', reason='都不行'),
                            origin='https://studio.example')
        source = self.c.candidate['body']['target']
        current = self.store.get_object(self.pid, source['object_id'])
        self.store.append_revision(self.pid, current['object_id'], current['revision'], current['body'], 'author')
        with self.assertRaises(DomainError):  # the agent already acted on the sentence
            self.service.decide(self.human, self.pid, self.answer(pending, choice='undo', idempotency_key='undo-3'),
                                origin='https://studio.example')
        self.service.decide(self.human, self.pid, self.answer(pending, selected_take=self.ref(self.c.take), idempotency_key='pick'),
                            origin='https://studio.example')

    def test_decline_records_no_selection(self):
        pending = self.service.request(self.actor, self.pid, self.take_request())
        receipt = self.service.decide(self.human, self.pid, self.answer(pending, choice='decline', reason='Try again.'),
                                      origin='https://studio.example')
        self.assertFalse(receipt['body']['accepted'])
        self.assertTrue(receipt['body']['verified_human_session'])
        self.assertEqual(self.store.get_object(self.pid, pending['object_id'])['body']['state'], 'declined')
        self.assertEqual(self.store.list_objects(self.pid, kind='human-take-selection'), [])

    def test_take_lineage_is_rechecked_when_human_confirms(self):
        pending = self.service.request(self.actor, self.pid, self.take_request())
        self.store.append_revision(self.pid, self.c.take['object_id'], self.c.take['revision'],
                                   self.c.take['body'], 'worker_service')
        with self.assertRaises(DomainError) as caught:
            self.service.decide(self.human, self.pid, self.answer(pending, selected_take=self.ref(self.c.take)),
                                origin='https://studio.example')
        self.assertEqual(caught.exception.code, 'stale_input')
        self.assertEqual(self.store.list_objects(self.pid, kind='human-receipt'), [])

    def test_replay_returns_same_receipt(self):
        pending = self.service.request(self.actor, self.pid, self.take_request())
        answer = self.answer(pending, selected_take=self.ref(self.c.take))
        receipt = self.service.decide(self.human, self.pid, answer, origin='https://studio.example')
        self.assertEqual(receipt, self.service.decide(self.human, self.pid, answer, origin='https://studio.example'))
        self.assertEqual(len(self.store.list_objects(self.pid, kind='human-take-selection')), 1)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='human-receipt')), 1)
        with self.assertRaises(DomainError) as caught:
            self.service.decide(self.human, self.pid, answer.model_copy(update={'reason': 'Changed'}),
                                origin='https://studio.example')
        self.assertEqual(caught.exception.code, 'idempotency_conflict')

    def test_take_request_requires_shot_and_takes(self):
        valid = self.take_request().model_dump(exclude_unset=True)
        for missing in ('shot', 'takes'):
            with self.subTest(missing=missing), self.assertRaises(ValidationError):
                DecisionRequest(**{key: value for key, value in valid.items() if key != missing})
        for values in ({'shot': None}, {'takes': None}, {'takes': []}, {'takes': valid['takes']*17},
                       {'takes': valid['takes']*2}, {'target': self.ref(self.media)},
                       {'proposed_limit': 1}, {'budget_unit': 'credit'}, {'budget_key': 'hf'},
                       {'purpose': 'final'}, {'purpose': 'envelope', 'proposed_limit': 1, 'budget_unit': 'credit'}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                DecisionRequest(**{**valid, **values})
        with self.assertRaises(ValidationError):
            self.answer({'object_id': 'request', 'body': {'target_hash': 'a'*64}}, reason='x'*2001)

    def test_take_supersession_is_scoped_to_shot(self):
        first = self.service.request(self.actor, self.pid, self.take_request())
        shot = self.store.create_object(self.pid, 'shot', {'content': 'Another shot'}, 'author')
        candidate = self.store.create_object(self.pid, 'candidate', {'target': self.ref(shot).model_dump(),
            'dependencies': [self.ref(shot).model_dump()]}, 'compiler_service')
        take = self.store.create_object(self.pid, 'media', {**self.c.take['body'],
            'dependencies': [self.ref(candidate).model_dump()]}, 'worker_service')
        other = self.service.request(self.actor, self.pid, self.take_request(idempotency_key='other-shot',
            target=self.ref(shot), shot=self.ref(shot), expected_revision=1, takes=[self.ref(take)]))
        self.service.request(self.actor, self.pid, self.take_request(idempotency_key='replacement'))
        self.assertEqual(self.store.get_object(self.pid, first['object_id'])['body']['state'], 'superseded')
        self.assertEqual(self.store.get_object(self.pid, other['object_id'])['body']['state'], 'pending')

    def test_selected_take_only_on_take_confirmation_and_human_session(self):
        pending = self.service.request(self.actor, self.pid, self.take_request())
        answer = self.answer(pending, selected_take=self.ref(self.c.take))
        for actor, origin, request in ((self.actor, 'https://studio.example', answer),
                (self.human, 'https://evil.example', answer),
                (self.human, 'https://studio.example', answer.model_copy(update={'csrf_token': 'x'*32})),
                (self.human, 'https://studio.example', answer.model_copy(update={'choice': 'decline'}))):
            with self.assertRaises(DomainError):
                self.service.decide(actor, self.pid, request, origin=origin)
        final = self.service.request(self.actor, self.pid, self.request(idempotency_key='final'))
        with self.assertRaises(DomainError):
            self.service.decide(self.human, self.pid, self.answer(final, selected_take=self.ref(self.c.take)),
                                origin='https://studio.example')

    def test_human_confirms_once_exact_finished_cut_and_locks_picture(self):
        pending = self.service.request(self.actor,self.pid,self.request())
        receipt = self.service.decide(self.human,self.pid,self.answer(pending),origin='https://studio.example')
        self.assertTrue(receipt['body']['accepted'])
        self.assertEqual(receipt,self.service.decide(self.human,self.pid,self.answer(pending),origin='https://studio.example'))
        self.assertEqual(len(self.store.list_objects(self.pid,kind='final')),1)
        self.assertEqual(len(self.store.list_objects(self.pid,kind='picture-lock')),1)
        with self.assertRaises(DomainError):
            self.service.decide(self.human,self.pid,self.answer(pending,idempotency_key='again'),origin='https://studio.example')

    def test_an_older_pending_final_with_finishing_evidence_still_confirms(self):
        # (review R-11): finals requested before finishing was removed carry
        # 'finishing' and 'receipts' evidence; the owner can still confirm them.
        pending = self.service.request(self.actor,self.pid,self.request())
        older = {**pending['body'], 'evidence': {**pending['body']['evidence'],
                 'finishing': [self.ref(self.cut).model_dump()], 'receipts': []}}
        pending = self.store.append_revision(self.pid, pending['object_id'], pending['revision'], older, 'decision_service')
        receipt = self.service.decide(self.human,self.pid,self.answer(pending),origin='https://studio.example')
        self.assertTrue(receipt['body']['accepted'])
        final, = self.store.list_objects(self.pid,kind='final')
        self.assertIn(self.ref(self.cut).model_dump(), final['body']['dependencies'])

    def test_author_wrong_origin_csrf_and_unisolated_host_cannot_confirm(self):
        pending = self.service.request(self.actor,self.pid,self.request())
        for actor,origin,request in [(self.actor,'https://studio.example',self.answer(pending)),
                (self.human,'https://evil.example',self.answer(pending)),
                (self.human,'https://studio.example',self.answer(pending,csrf_token='x'*32))]:
            with self.assertRaises(DomainError):
                self.service.decide(actor,self.pid,request,origin=origin)
        self.service.human_enabled=False
        with self.assertRaises(DomainError):
            self.service.decide(self.human,self.pid,self.answer(pending),origin='https://studio.example')
        self.assertEqual(self.store.list_objects(self.pid,kind='final'),[])

    def test_supersession_expiry_and_decline_never_produce_final(self):
        first = self.service.request(self.actor,self.pid,self.request())
        second = self.service.request(self.actor,self.pid,self.request(idempotency_key='ask2'))
        with self.assertRaises(DomainError):
            self.service.decide(self.human,self.pid,self.answer(first),origin='https://studio.example')
        declined = self.service.decide(self.human,self.pid,self.answer(second,choice='decline'),origin='https://studio.example')
        self.assertFalse(declined['body']['accepted'])
        third = self.service.request(self.actor,self.pid,self.request(idempotency_key='ask3'))
        self.now += 61
        with self.assertRaises(DomainError):
            self.service.decide(self.human,self.pid,self.answer(third,idempotency_key='expired'),origin='https://studio.example')
        self.assertEqual(self.store.list_objects(self.pid,kind='final'),[])

    def test_new_cut_invalidates_old_request_even_unchanged_video_bytes(self):
        pending = self.service.request(self.actor,self.pid,self.request())
        self.c.service.create(self.actor,self.pid,self.c.request(idempotency_key='cut2',expected_revision=1,intent='New cut intent'))
        with self.assertRaises(DomainError):
            self.service.decide(self.human,self.pid,self.answer(pending),origin='https://studio.example')

    def test_budget_delta_needs_human_and_cannot_erase_commitments(self):
        self.store.set_budget(self.pid,10,'synthetic_unit')
        request = self.request(purpose='envelope',proposed_limit=20,budget_unit='synthetic_unit')
        pending = self.service.request(self.actor,self.pid,request)
        self.assertEqual(self.store.budget(self.pid)['ceiling'],10)
        self.service.decide(self.human,self.pid,self.answer(pending),origin='https://studio.example')
        self.assertEqual(self.store.budget(self.pid)['ceiling'],20)
        self.assertEqual(self.store.list_objects(self.pid,kind='final'),[])

    def test_native_envelope_decisions_do_not_supersede_or_modify_other_accounts(self):
        pending = []
        for key in ('provider_a', 'provider_b'):
            self.store.set_budget(self.pid, 10, 'credit', budget_key=key)
            pending.append(self.service.request(self.actor, self.pid, self.request(
                purpose='envelope', idempotency_key=key, proposed_limit=20, budget_unit='credit', budget_key=key)))
        for key, item in zip(('provider_a','provider_b'), pending, strict=True):
            self.assertEqual(item['body']['evidence']['budget_key'], key)
            self.service.decide(self.human, self.pid, self.answer(item, idempotency_key=key), origin='https://studio.example')
            self.assertEqual(self.store.budget(self.pid, budget_key=key)['ceiling'], 20)
        self.assertEqual(self.store.list_objects(self.pid,kind='final'), [])

    def test_native_envelope_changed_away_and_back_requires_fresh_human_decision(self):
        self.store.set_budget(self.pid, 10, 'credit', budget_key='hf_primary')
        item = self.service.request(self.actor, self.pid, self.request(purpose='envelope',
            proposed_limit=20, budget_unit='credit', budget_key='hf_primary'))
        self.store.set_budget(self.pid, 11, 'credit', budget_key='hf_primary')
        self.store.set_budget(self.pid, 10, 'credit', budget_key='hf_primary')
        with self.assertRaises(DomainError):
            self.service.decide(self.human, self.pid, self.answer(item), origin='https://studio.example')
        self.assertEqual(self.store.budget(self.pid, budget_key='hf_primary')['ceiling'], 10)
