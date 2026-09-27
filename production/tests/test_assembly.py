"""The owner's current picks become one cut request in shot order."""
import unittest

from production.assembly import assemble
from production.contracts import CutRequest, DomainError
from production.tests import test_projects


class AssemblyTests(unittest.TestCase):
    def setUp(self):
        self.p = test_projects.ProjectTests()
        self.p.setUp()
        self.addCleanup(self.p.tearDown)
        self.store, self.pid = self.p.store, self.p.pid
        self.receipt = self.add('human-receipt', {'verified_human_session': True}, 'decision_service')

    def add(self, kind, body, author='author_1', object_id=None):
        return self.store.create_object(self.pid, kind, body, author, object_id=object_id)

    def ref(self, obj):
        return self.p.ref(obj)

    def shot(self, label, durations=(8.0, 9.0)):
        shot = self.add('shot', {'content': {'shot': label}})
        takes = [self.add('media', {'media_type': 'video/mp4', 'probe': {'duration': d, 'has_video': True, 'has_audio': True}},
                          'worker_service') for d in durations]
        request = self.add('decision-request', {'target': self.ref(shot), 'purpose': 'take', 'state': 'pending',
            'evidence': {'shot': self.ref(shot), 'takes': [self.ref(t) for t in takes]}}, 'decision_service')
        return shot, takes, request

    def pick(self, shot, take, object_id=None, author='decision_service'):
        return self.add('human-take-selection', {'shot': self.ref(shot), 'take': take and self.ref(take),
            'human_receipt': self.ref(self.receipt), 'verified_human_session': True}, author, object_id=object_id)

    def run_assembly(self):
        with self.store.transaction(write=False) as db:
            return assemble(self.store, self.pid, conn=db)

    def test_latest_picks_in_shot_order_with_full_takes_and_original_sound(self):
        third, third_takes, _ = self.shot('S01-030A', (5.0,))
        first, first_takes, _ = self.shot('S01-010A')
        second, second_takes, _ = self.shot('S01-020A')
        self.pick(first, first_takes[0], object_id='obj_zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz')
        self.pick(first, first_takes[1], object_id='obj_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')  # re-pick sorts first by id
        self.pick(second, second_takes[0])
        self.pick(third, third_takes[0])
        self.pick(third, None, author='author_1')  # an agent record never changes the owner's pick
        result = self.run_assembly()
        self.assertTrue(result['complete'])
        self.assertEqual(result['missing'], [])
        self.assertEqual(result['segments'], [
            {'take': self.ref(first_takes[1]), 'start_seconds': 0.0, 'end_seconds': 9.0},
            {'take': self.ref(second_takes[0]), 'start_seconds': 0.0, 'end_seconds': 8.0},
            {'take': self.ref(third_takes[0]), 'start_seconds': 0.0, 'end_seconds': 5.0}])
        self.assertEqual(result['sound_inputs'], [])
        self.assertEqual(result['intent'], 'Owner picks in shot order: S01-010A take 2, S01-020A take 1, S01-030A take 1.')
        CutRequest(idempotency_key='auto', expected_revision=1, segments=result['segments'],
                   sound_inputs=result['sound_inputs'], intent=result['intent'])
        self.assertEqual(len(result['picks_hash']), 64)
        self.pick(second, second_takes[1])
        self.assertNotEqual(self.run_assembly()['picks_hash'], result['picks_hash'])

    def draft_shot(self, label, *, expires=10_000):
        """A shot whose takes are fal drafts; the provenance fields are the ones jobs.py records."""
        shot = self.add('shot', {'content': {'shot': label}})
        take = self.add('media', {'media_type': 'video/mp4', 'probe': {'duration': 4.0, 'has_video': True, 'has_audio': True},
                                  'provenance': {'provider_output': {'seed': 7, 'draft_id': 'draft_1', 'draft_expires_at': expires}}},
                        'worker_service')
        self.add('decision-request', {'target': self.ref(shot), 'purpose': 'take', 'state': 'pending',
                                      'evidence': {'shot': self.ref(shot), 'takes': [self.ref(take)]}}, 'decision_service')
        self.pick(shot, take)
        return shot, take

    def completion(self, take, state):
        intent = self.add('dispatch-intent', {'operation': 'complete-draft', 'target': self.ref(take)}, 'submission_service')
        return self.add('job', {'state': state, 'intent': self.ref(intent), 'result': None}, 'submission_service')

    def finish(self, job, take, *, duration=4.02):
        result = self.add('media', {'media_type': 'video/mp4', 'completes': self.ref(take),
                                    'probe': {'duration': duration, 'has_video': True, 'has_audio': True}}, 'worker_service')
        self.store.append_revision(self.pid, job['object_id'], job['revision'],
                                   {**job['body'], 'state': 'succeeded', 'result': self.ref(result)}, 'worker_service')
        return result

    def assembled(self, now):
        with self.store.transaction(write=False) as db:
            return assemble(self.store, self.pid, conn=db, now=now)

    def test_a_picked_draft_waits_for_its_1080p_completion_then_plays_it(self):
        _, take = self.draft_shot('S01-010A')
        waiting = self.assembled(1000)
        self.assertEqual((waiting['complete'], waiting['completing'], waiting['missing']), (False, ['S01-010A'], []))
        job = self.completion(take, 'running')
        self.assertEqual(self.assembled(1000)['completing'], ['S01-010A'])
        full = self.finish(job, take)
        film = self.assembled(1000)
        self.assertTrue(film['complete'])
        self.assertEqual(film['segments'], [{'take': self.ref(full), 'start_seconds': 0.0, 'end_seconds': 4.02}])
        self.assertEqual(film['intent'], 'Owner picks in shot order: S01-010A take 1.')

    def test_a_failed_completion_or_an_expired_draft_plays_the_draft(self):
        _, failed = self.draft_shot('S01-010A')
        self.completion(failed, 'failed')
        _, expired = self.draft_shot('S01-020A', expires=500)
        film = self.assembled(1000)
        self.assertTrue(film['complete'])
        self.assertEqual([s['take'] for s in film['segments']], [self.ref(failed), self.ref(expired)])

    def test_missing_or_withdrawn_picks_leave_the_film_incomplete_and_an_edited_card_keeps_its_pick(self):
        first, first_takes, _ = self.shot('S01-010A')
        second, second_takes, _ = self.shot('S01-020A')
        third, third_takes, _ = self.shot('S01-030A')
        self.pick(first, first_takes[0])
        self.pick(second, second_takes[0])
        self.pick(second, None)  # 取消
        self.pick(third, third_takes[0])
        self.store.append_revision(self.pid, third['object_id'], 1, {'content': {'shot': 'S01-030A', 'note': 'edited'}}, 'author_1')
        result = self.run_assembly()
        self.assertFalse(result['complete'])
        self.assertEqual(result['missing'], ['S01-020A'])  # the edited S01-030A keeps the owner's pick
        self.assertEqual(result['segments'], [])
        self.pick(second, second_takes[1])
        self.assertTrue(self.run_assembly()['complete'])

    def test_stress_test_cards_are_never_film_shots(self):
        # dry run: a stress order's desk offer made the film wait for a pick of the test card.
        first, first_takes, _ = self.shot('S01-010A')
        self.shot('S01-960A')  # a stress-test card with takes on the desk, never picked
        self.pick(first, first_takes[0])
        result = self.run_assembly()
        self.assertTrue(result['complete'])
        self.assertEqual(len(result['segments']), 1)

    def test_superseded_requests_do_not_add_shots_and_unlabelled_shots_stop(self):
        first, first_takes, _ = self.shot('S01-010A')
        _, _, request = self.shot('S01-005A')
        self.store.append_revision(self.pid, request['object_id'], 1, {**request['body'], 'state': 'superseded'}, 'decision_service')
        self.pick(first, first_takes[0])
        self.assertEqual([s['take'] for s in self.run_assembly()['segments']], [self.ref(first_takes[0])])
        self.shot('the newcomer stalls')
        with self.assertRaises(DomainError) as caught:
            self.run_assembly()
        self.assertEqual(caught.exception.code, 'invalid_input')

    def test_empty_project_has_nothing_to_assemble(self):
        result = self.run_assembly()
        self.assertEqual((result['complete'], result['segments'], result['missing']), (False, [], []))


if __name__ == '__main__':
    unittest.main()
