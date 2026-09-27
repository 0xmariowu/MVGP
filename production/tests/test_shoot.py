"""One call shoots a scene; the worker offers the takes on the desk."""
import copy
import subprocess
import time
import unittest

from production.decisions import Decisions
from production.projects import Projects
from production.shoot import KIND, AutoShoot, Shoot, ShootRequest
from production.tests import test_batches


class ShootTests(unittest.TestCase):
    def setUp(self):
        self.b = test_batches.BatchTests()
        self.b.setUp()
        self.addCleanup(self.b.doCleanups)
        self.s, self.f, self.store, self.pid, self.actor = self.b.s, self.b.f, self.b.store, self.b.pid, self.b.actor
        self.s.policy['operations']['submit']['seedance_2_5'] = {**self.s.cost, 'max_attempts': 4}
        # Release 79 still declares a shot-plan owner gate; nothing reads it now.
        self.s.policy['owner_gates'] = {'shot_plan': 'required'}
        self.f.config.set('execution_policy', self.s.policy)
        self.card, self.selection, self.method, _ = self.s.c.video()
        self.store.set_budget(self.pid, 100, 'credit')
        projects = Projects(self.store, self.f.auth, self.f.media, label='lean-v1')
        self.shoot = Shoot(self.store, self.f.auth, projects, self.s.c.compiler, self.b.service)
        self.decisions = Decisions(self.store, self.f.auth, self.f.flow, None)
        film = self.f.auth.authenticate(self.f.auth.provision_token('shoot_service_agent', 'agent', [self.pid], 300))
        self.auto = AutoShoot(film, self.store, self.shoot, self.decisions, self.f.media)

    def order(self, key='shoot', **values):
        return self.shoot.order(self.actor, self.pid, ShootRequest(idempotency_key=key, cards=[self.f.ref(self.card)],
                                                                    task='stress', **values))

    def finish_all(self, success=True, order_id=None):
        # Same shape as a real Seedance take: an MP4 with sound at the released 1080p.
        file = self.f.root / 'shoot-take.mp4'
        if not file.exists():
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=1920x1080:r=24:d=4',
                '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=4', '-c:v', 'libx264', '-threads', '1',
                '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(file)], check=True, capture_output=True)
        order = self.store.get_object(self.pid, order_id) if order_id else self.store.list_objects(self.pid, kind=KIND)[0]
        batch = self.store.get_object(self.pid, order['body']['cards'][0]['batch']['object_id'])
        for n, child in enumerate(batch['body']['children']):
            self.b.finish(child, success=success if isinstance(success, bool) else success[n], raw=file.read_bytes(), mime='video/mp4')

    def run_until(self, ending, limit=200):
        for _ in range(limit):
            state = self.auto.advance(self.pid)
            if state.endswith(ending):
                return state
            time.sleep(0.01)
        self.fail(f'never reached {ending}; last state {state}')

    def test_one_call_fires_four_takes_and_the_worker_puts_them_on_the_desk(self):
        placed = self.order()
        self.assertEqual([c['stage'] for c in placed['cards']], ['firing'])
        self.assertEqual(len(self.store.list_objects(self.pid, kind='job')), 4)
        self.assertEqual(self.order(), placed)  # the same call again changes nothing
        self.assertEqual(self.auto.advance(self.pid).split(': ')[1], 'firing')  # takes still being made
        self.finish_all()
        self.assertTrue(self.auto.advance(self.pid).endswith('on-desk'))  # no listening step
        requests = [r for r in self.store.list_objects(self.pid, kind='decision-request') if r['body']['purpose'] == 'take']
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(requests[0]['body']['evidence']['takes']), 4)
        self.assertEqual(requests[0]['body']['rationale'], '4 条拍好了')
        self.assertEqual(self.auto.advance(self.pid), 'idle')

    def test_a_quote_prices_the_order_and_says_when_the_envelope_is_short(self):
        # (audit: "quote spend before shoot"): read-only, the numbers the worker settles with.
        events = len(self.store.events(self.pid))
        quote = self.shoot.quote(self.actor, self.pid, [self.f.ref(self.card)])
        row, = quote['cards']
        self.assertEqual((row['seconds'], row['takes'], row['per_take'], row['drafts'], row['held']), (4, 4, 8, 32, 40))
        self.assertEqual(quote['envelope']['left'], 100)
        self.assertEqual(quote['advice'], [])
        self.store.set_budget(self.pid, 30, 'credit')
        short = self.shoot.quote(self.actor, self.pid, [self.f.ref(self.card)])
        self.assertTrue(any('预算不够' in a for a in short['advice']), short['advice'])
        self.assertEqual(len(self.store.list_objects(self.pid, kind='job')), 0)
        self.assertEqual(len(self.store.events(self.pid)), events + 1)  # only the budget change above
        # in Higgsfield credits a take is priced at the pinned 12 credits/s x its seconds.
        self.s.policy['operations']['submit']['seedance_2_5'] = {**self.s.cost, 'budget_unit': 'hf_credit', 'max_attempts': 4}
        self.f.config.set('execution_policy', self.s.policy)
        row, = self.shoot.quote(self.actor, self.pid, [self.f.ref(self.card)])['cards']
        self.assertEqual((row['per_take'], row['drafts']), (48, 192))

    def test_an_order_shoots_on_higgsfield_unless_the_film_is_in_sample_mode(self):
        """(owner 2026-09-27): Higgsfield at 1080p is the shooting route; 样片模式 sends a film's
        orders to fal's 480p drafts. A config without the Higgsfield method keeps its own released method."""
        from production import switches
        self.assertEqual(self.shoot.video_method(self.pid, 'mvgp-video-v1'), 'mvgp-video-v1')  # the test world
        methods = dict(self.f.config.section('methods')['methods'])
        self.f.config.set('methods', {'methods': {**methods, 'mvgp-video-hf-v1': methods['mvgp-video-v1']}})
        self.assertEqual(self.shoot.video_method(self.pid, 'mvgp-video-v1'), 'mvgp-video-hf-v1')
        self.store.create_object(self.pid, switches.KIND, {'switches': {'sample_mode': True}, 'by': 'owner'},
                                 switches.AUTHOR, object_id=switches.object_id(self.pid))
        self.assertEqual(self.shoot.video_method(self.pid, 'mvgp-video-v1'), 'mvgp-video-v1')
        self.assertEqual(self.shoot.video_method(self.pid, 'mvgp-video-hf-v1'), 'mvgp-video-hf-v1')  # named: kept
        placed = self.order()
        order = self.store.get_object(self.pid, placed['object_id'] if 'object_id' in placed else
                                      self.store.list_objects(self.pid, kind=KIND)[0]['object_id'])
        self.assertEqual(order['body']['method_id'], 'mvgp-video-v1')

    def test_the_reviewers_notes_and_the_writers_answers_travel_with_the_order(self):
        # (Q6): recorded and shown, never a gate.
        review = {'reviewer': 'fresh reviewer agent', 'playbook_version': 'pb-96b8791e3a5d', 'notes': [
            {'line': 'cinedance:336', 'note': 'The first frame is empty.', 'answer': 'FIRST FRAME AND SPATIAL BLOCKING', 'shot': 'S02-020A'}]}
        placed = self.order(key='reviewed', review=review)
        self.assertEqual(placed['cards'][0]['stage'], 'firing')
        order = self.store.get_object(self.pid, placed['order']['object_id'])
        self.assertEqual(order['body']['review'], review)
        self.assertEqual(self.order(key='reviewed', review=review), placed)
        plain = self.order(key='plain')
        self.assertNotIn('review', self.store.get_object(self.pid, plain['order']['object_id'])['body']['request'])
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):  # a finding must name its manual line
            ShootRequest(idempotency_key='x', cards=[self.f.ref(self.card)], review={'reviewer': 'r', 'notes': [{'note': 'n', 'answer': 'a'}]})

    # ---- 再拍一批 without a sentence re-fires the same card by itself
    def on_desk(self, key='shoot'):
        placed = self.order(key=key)
        self.finish_all(order_id=placed['order']['object_id'])
        self.run_until('on-desk')
        return next(r for r in self.store.list_objects(self.pid, kind='decision-request')
                    if r['body']['purpose'] == 'take' and r['body']['target']['object_id'] == self.card['object_id'])

    def rebatch(self, request, reason='再拍一批：（没写原因）'):
        """The owner's answer as the desk records it (decisions.py writes these two records)."""
        answered = self.store.append_revision(self.pid, request['object_id'], request['revision'],
                                              {**request['body'], 'state': 'declined'}, 'decision_service')
        return self.store.create_object(self.pid, 'human-receipt', {'decision': self.f.ref(answered).model_dump(),
            'target': request['body']['target'], 'purpose': 'take', 'choice': 'decline', 'human_actor': 'owner',
            'verified_human_session': True, 'reason': reason, 'accepted': False,
            'dependencies': [request['body']['target']]}, 'decision_service')

    def cutoff(self):
        return self.store.events(self.pid)[-1]['sequence']

    def auto_with(self, cutoff):
        film = self.f.auth.authenticate(self.f.auth.provision_token('shoot_service_agent_2', 'agent', [self.pid], 300))
        return AutoShoot(film, self.store, self.shoot, self.decisions, self.f.media, reshoot_after_event=cutoff)

    def test_a_reasonless_rebatch_after_the_cutoff_fires_the_same_candidate_once(self):
        request = self.on_desk()
        auto = self.auto_with(self.cutoff())
        self.rebatch(request)
        candidate = self.store.list_objects(self.pid, kind=KIND)[0]['body']['cards'][0]['candidate']
        self.assertIn('firing', auto.advance(self.pid))
        jobs = self.store.list_objects(self.pid, kind='job')
        self.assertEqual(len(jobs), 8)
        batches = self.store.list_objects(self.pid, kind='batch')
        self.assertEqual({c['candidate']['object_id'] for b in batches for c in b['body']['children']}, {candidate['object_id']})
        auto.advance(self.pid)
        auto.advance(self.pid)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='job')), 8)  # once per answer

    def test_old_answers_answers_with_a_sentence_changed_cards_and_no_cutoff_never_fire(self):
        request = self.on_desk()
        self.rebatch(request)
        for auto in (self.auto_with(self.cutoff()), self.auto):  # answered before the cutoff; no cutoff configured
            auto.advance(self.pid)
            self.assertEqual(len(self.store.list_objects(self.pid, kind='job')), 4)
        other, _, _, _ = self.s.c.video()
        self.card = other
        request = self.on_desk(key='second-card')
        self.store.set_budget(self.pid, 1000, 'credit')  # money is not what stops this one
        auto = self.auto_with(self.cutoff())
        self.rebatch(request, reason='再拍一批：车开得太快了')  # a sentence: the agent patches the card
        auto.advance(self.pid)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='job')), 8)

    def test_a_changed_card_or_an_undone_answer_never_fires(self):
        request = self.on_desk()
        auto = self.auto_with(self.cutoff())
        self.rebatch(request)
        self.store.set_budget(self.pid, 1000, 'credit')  # money is not what stops this one
        card = self.store.get_object(self.pid, self.card['object_id'])
        self.store.append_revision(self.pid, card['object_id'], card['revision'], card['body'], 'author')  # the agent revised it
        auto.advance(self.pid)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='job')), 4)
        other, _, _, _ = self.s.c.video()
        self.card = other
        request = self.on_desk(key='second-card')
        self.store.set_budget(self.pid, 1000, 'credit')  # money is not what stops this one
        auto = self.auto_with(self.cutoff())
        self.rebatch(request)
        answered = self.store.get_object(self.pid, request['object_id'])
        self.store.append_revision(self.pid, answered['object_id'], answered['revision'], {**answered['body'], 'state': 'pending'},
                                   'decision_service')  # 撤销 puts the batch back on the desk
        auto.advance(self.pid)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='job')), 8)

    def test_one_answer_fires_once_even_after_an_undo_and_a_new_answer(self):
        # Bug hunt 2026-09-27: 再拍一批, 撤销, 再拍一批 again is one reshoot (4 takes), not two (the old receipt fired too).
        request = self.on_desk()
        auto = self.auto_with(self.cutoff())
        self.store.set_budget(self.pid, 1000, 'credit')
        self.rebatch(request)
        answered = self.store.get_object(self.pid, request['object_id'])
        undone = self.store.append_revision(self.pid, answered['object_id'], answered['revision'],
                                            {**answered['body'], 'state': 'pending'}, 'decision_service')
        self.rebatch(undone)
        auto.advance(self.pid)
        auto.advance(self.pid)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='job')), 8)  # the first batch + one reshoot

    def test_a_waiting_reshoot_the_owner_undoes_never_fires(self):
        # Bug hunt 2026-09-27: an order waiting on an earlier take re-read only the card, not the owner's answer.
        request = self.on_desk()
        auto = self.auto_with(self.cutoff())
        self.store.set_budget(self.pid, 1000, 'credit')
        job = self.store.list_objects(self.pid, kind='job')[0]
        self.store.append_revision(self.pid, job['object_id'], job['revision'],
                                   {**job['body'], 'state': 'unknown', 'remote_job_id': 'still-out'}, 'worker_service')
        self.rebatch(request)
        self.assertIn('waiting', auto.advance(self.pid))
        answered = self.store.get_object(self.pid, request['object_id'])
        self.store.append_revision(self.pid, answered['object_id'], answered['revision'],
                                   {**answered['body'], 'state': 'pending'}, 'decision_service')  # 撤销
        job = self.store.get_object(self.pid, job['object_id'])
        self.store.append_revision(self.pid, job['object_id'], job['revision'], {**job['body'], 'state': 'failed'}, 'worker_service')
        self.assertIn('stopped (undone)', auto.advance(self.pid))
        self.assertEqual(len(self.store.list_objects(self.pid, kind='job')), 4)

    def test_a_reshoot_waiting_at_a_deploy_still_fires_after_it(self):
        # Bug hunt 2026-09-27: the cutoff is for new orders; an order already made and waiting must not be dropped.
        request = self.on_desk()
        auto = self.auto_with(self.cutoff())
        self.store.set_budget(self.pid, 1000, 'credit')
        job = self.store.list_objects(self.pid, kind='job')[0]
        self.store.append_revision(self.pid, job['object_id'], job['revision'],
                                   {**job['body'], 'state': 'unknown', 'remote_job_id': 'still-out'}, 'worker_service')
        self.rebatch(request)
        self.assertIn('waiting', auto.advance(self.pid))
        after_deploy = self.auto_with(self.cutoff())  # the new release's cutoff is later than the answer
        job = self.store.get_object(self.pid, job['object_id'])
        self.store.append_revision(self.pid, job['object_id'], job['revision'], {**job['body'], 'state': 'failed'}, 'worker_service')
        self.assertIn('firing', after_deploy.advance(self.pid))
        self.assertEqual(len(self.store.list_objects(self.pid, kind='job')), 8)

    def test_a_reshoot_stops_on_the_budget_and_waits_for_an_earlier_take(self):
        request = self.on_desk()
        auto = self.auto_with(self.cutoff())
        self.rebatch(request)
        self.store.set_budget(self.pid, self.store.budget(self.pid)['spent'] + 5, 'credit')
        self.assertIn('stopped', auto.advance(self.pid))
        order = next(o for o in self.store.list_objects(self.pid, kind=KIND) if 'reshoot_of' in o['body']['request'])
        self.assertEqual(order['body']['cards'][0]['code'], 'budget_exceeded')
        self.assertEqual(len(self.store.list_objects(self.pid, kind='job')), 4)

    def test_a_reshoot_waits_while_a_take_of_the_same_request_is_still_out(self):
        request = self.on_desk()
        auto = self.auto_with(self.cutoff())
        job = self.store.list_objects(self.pid, kind='job')[0]
        self.store.append_revision(self.pid, job['object_id'], job['revision'],
                                   {**job['body'], 'state': 'unknown', 'remote_job_id': 'still-out'}, 'worker_service')
        self.rebatch(request)
        self.assertIn('waiting', auto.advance(self.pid))
        self.assertEqual(len(self.store.list_objects(self.pid, kind='job')), 4)
        job = self.store.get_object(self.pid, job['object_id'])
        self.store.append_revision(self.pid, job['object_id'], job['revision'], {**job['body'], 'state': 'failed'}, 'worker_service')
        self.assertIn('firing', auto.advance(self.pid))
        self.assertEqual(len(self.store.list_objects(self.pid, kind='job')), 8)

    def test_failed_takes_are_left_out_and_nothing_is_fired_again(self):
        self.order()
        self.finish_all(success=[True, False, True, False])
        self.run_until('on-desk')
        request = next(r for r in self.store.list_objects(self.pid, kind='decision-request') if r['body']['purpose'] == 'take')
        self.assertEqual(len(request['body']['evidence']['takes']), 2)
        self.assertEqual(len(self.store.list_objects(self.pid, kind='job')), 4)

    def test_every_take_failing_stops_the_order_with_a_plain_reason(self):
        self.order()
        self.finish_all(success=False)
        self.assertTrue(self.auto.advance(self.pid).endswith('stopped (no take)'))
        order = self.store.list_objects(self.pid, kind=KIND)[0]
        self.assertIn('Every take failed', order['body']['cards'][0]['reason'])
        self.assertEqual(order['body']['cards'][0]['code'], 'takes_failed')
        self.assertEqual(self.auto.advance(self.pid), 'idle')

    def test_a_stopped_card_has_a_plain_chinese_reason_for_the_owner(self):

        from production.shoot import OWNER_REASONS, owner_reason
        self.assertEqual(owner_reason({'code': 'budget_exceeded'}), OWNER_REASONS['budget_exceeded'])
        self.assertTrue(owner_reason({}))
        for text in OWNER_REASONS.values():
            self.assertRegex(text, r'[\u4e00-\u9fff]')

    def test_a_card_edited_after_the_order_stops_it(self):
        self.order()
        body = copy.deepcopy(self.card['body'])
        body['content']['Direction']['expected visible performance'] = 'The box slides and tips over.'
        self.store.append_revision(self.pid, self.card['object_id'], self.card['revision'], body, 'author')
        self.assertTrue(self.auto.advance(self.pid).endswith('stopped (card changed)'))

    def test_a_scene_edit_leaves_a_written_card_shootable(self):
        # the card carries its whole prompt; a selection made for the old scene is left out.
        scene_ref = next(d for d in self.card['body']['dependencies']
                         if self.store.get_object(self.pid, d['object_id'])['kind'] == 'scene')
        scene = self.store.get_object(self.pid, scene_ref['object_id'])
        chosen = copy.deepcopy(self.selection['body'])
        chosen['content']['target'] = scene_ref
        chosen['logical_path'] = 'assets/scene-selection.json'
        chosen['dependencies'] = [*chosen['dependencies'], scene_ref]
        self.store.create_object(self.pid, 'asset', chosen, 'author')
        self.store.append_revision(self.pid, scene['object_id'], scene['revision'],
                                   {**scene['body'], 'content': scene['body']['content'] + '\nLate light.\n'}, 'author')
        placed = self.order(key='after-scene-edit')['cards'][0]
        self.assertEqual(placed['stage'], 'firing', placed.get('reason'))

    def test_a_project_level_selection_or_an_odd_timing_mode_never_stops_an_order(self):
        # Re-audit 2026-09-27: the compiler asked for selections the order never collects, and refused a
        # timing mode that is never sent.
        project = self.store.get_object(self.pid, self.pid)
        chosen = copy.deepcopy(self.selection['body'])
        chosen['content']['target'] = {k: project[k] for k in ('object_id', 'revision', 'digest')}
        chosen['logical_path'] = 'assets/project-selection.json'
        self.store.create_object(self.pid, 'asset', chosen, 'author')
        body = copy.deepcopy(self.card['body'])
        body['content']['Direction']['timing mode'] = 'freeform'
        body['content']['shot'] = 'S02-040A'
        card = self.store.create_object(self.pid, 'shot', body, 'author')
        placed = self.shoot.order(self.actor, self.pid, ShootRequest(idempotency_key='project-selection', cards=[self.f.ref(card)],
                                                                      task='stress'))['cards'][0]
        self.assertEqual(placed['stage'], 'firing', placed.get('reason'))
        candidate = self.store.get_object(self.pid, placed['candidate']['object_id'])
        advice = candidate['body']['compilation']['authorship']['advice']
        self.assertTrue(any('Timing mode freeform' in a for a in advice), advice)

    def test_the_prompts_tags_pick_the_assets_with_no_selection_written(self):
        # HF has no binding object: a tag in the prompt is the reference (hell-grind:59).
        body = copy.deepcopy(self.card['body'])
        body['content']['shot'] = 'S02-030A'
        card = self.store.create_object(self.pid, 'shot', body, 'author')  # nobody wrote a selection for this card
        placed = self.shoot.order(self.actor, self.pid, ShootRequest(idempotency_key='tags', cards=[self.f.ref(card)], task='stress'))
        self.assertEqual(placed['cards'][0]['stage'], 'firing', placed['cards'][0].get('reason'))
        candidate = self.store.get_object(self.pid, placed['cards'][0]['candidate']['object_id'])
        self.assertEqual([r['tag'] for r in candidate['body']['request']['references']], ['loc_demo'])
        self.assertIn('STYLE: Clean animation contours', candidate['body']['request']['params']['prompt'])  # the only look
        derived = [o for o in self.store.list_objects(self.pid, kind='asset')
                   if o['author'] == 'shoot_service' and o['body']['content']['target']['object_id'] == card['object_id']]
        self.assertEqual(sorted(next(iter(o['body']['content']['selected'])) for o in derived), ['look', 'world'])

    def test_a_released_shot_plan_gate_does_not_hold_an_order(self):
        # Higgsfield has no shot-plan approval step.
        placed = self.order(key='gated')
        self.assertEqual(placed['cards'][0]['stage'], 'firing')
        self.assertEqual(len(self.store.list_objects(self.pid, kind='job')), 4)

    def test_a_card_that_fails_its_checks_is_reported_and_costs_nothing(self):
        self.store.set_budget(self.pid, 0, 'credit')
        placed = self.order(key='no-budget')
        self.assertEqual(placed['cards'][0]['stage'], 'stopped')
        self.assertTrue(placed['cards'][0]['reason'])
        self.assertEqual(self.store.list_objects(self.pid, kind='job'), [])
        self.assertEqual(self.auto.advance(self.pid), 'idle')

    def test_an_unchanged_card_can_be_shot_again_and_a_repeated_click_pays_once(self):
        # HF re-fires identical prompts (42/48/52% of batches in trigger/red-flag/oneiric).
        first = self.order(key='first')
        self.assertEqual(self.order(key='first'), first)  # the same click again: the same order, nothing new
        self.assertEqual(len(self.store.list_objects(self.pid, kind='job')), 4)
        again = self.order(key='second')  # another order of the same unchanged card
        self.assertEqual(again['cards'][0]['stage'], 'firing', again['cards'][0].get('reason'))
        self.assertEqual(len(self.store.list_objects(self.pid, kind='job')), 8)

    def test_a_card_still_being_made_never_holds_up_another(self):
        first = self.order(key='first')
        other, _, _, _ = self.s.c.video()
        placed = self.shoot.order(self.actor, self.pid, ShootRequest(idempotency_key='second', cards=[self.f.ref(other)], task='stress'))
        self.assertEqual(placed['cards'][0]['stage'], 'firing')
        self.finish_all(order_id=first['order']['object_id'])  # only the first card's takes are back
        state = self.auto.advance(self.pid)
        self.assertIn('on-desk', state)  # the first card moved on
        self.assertIn('firing', state)     # while the second card is still being made

    def test_an_unexpected_error_on_one_card_is_recorded_and_the_order_survives(self):
        original = self.shoot.fire
        def boom(*args, **kwargs):
            raise KeyError('surprise')
        self.shoot.fire = boom
        try:
            placed = self.order(key='boom')
        finally:
            self.shoot.fire = original
        self.assertEqual(placed['cards'][0]['stage'], 'stopped')
        self.assertIn('Unexpected KeyError', placed['cards'][0]['reason'])
        self.assertEqual(len(self.store.list_objects(self.pid, kind=KIND)), 1)

    def test_an_unknown_take_is_waited_for_then_left_out_with_a_note(self):
        from unittest.mock import patch
        self.order()
        order = self.store.list_objects(self.pid, kind=KIND)[0]
        batch = self.store.get_object(self.pid, order['body']['cards'][0]['batch']['object_id'])
        self.finish_all(success=[True, True, True, False])
        stuck = self.store.get_object(self.pid, batch['body']['children'][3]['job']['object_id'])
        self.store.append_revision(self.pid, stuck['object_id'], stuck['revision'], {**stuck['body'], 'state': 'unknown'}, 'worker_service')
        with patch('production.shoot.time.time', return_value=1000.0):
            self.assertTrue(self.auto.advance(self.pid).endswith('firing'))
        with patch('production.shoot.time.time', return_value=1000.0 + 899):
            self.assertTrue(self.auto.advance(self.pid).endswith('firing'))
        with patch('production.shoot.time.time', return_value=1000.0 + 901):
            self.assertTrue(self.auto.advance(self.pid).endswith('on-desk'))
        request = next(r for r in self.store.list_objects(self.pid, kind='decision-request') if r['body']['purpose'] == 'take')
        self.assertEqual(len(request['body']['evidence']['takes']), 3)
        self.assertIn('第 4 条结果不明', request['body']['rationale'])


if __name__ == '__main__':
    unittest.main()
