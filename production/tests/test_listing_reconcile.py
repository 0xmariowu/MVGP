"""Automatic listing decisions for a timed-out create."""
import unittest
from datetime import UTC, datetime, timedelta

from production import listing_reconcile as lr

T = datetime(2026, 9, 24, 5, 45, 4, tzinfo=UTC)


def seen():
    start, end = lr.window(T.isoformat())
    return {'start': start, 'end': end, 'job_type': 'seedance_2_5', 'prompt_sha256': 'a' * 64, 'duration': 9,
            'job': {'object_id': 'job_x'}}


def item(ident, seconds, prompt='a' * 64):
    return {'id': ident, 'created_at': (T + timedelta(seconds=seconds)).isoformat(), 'job_type': 'seedance_2_5',
            'prompt_sha256': prompt, 'duration': 9}


OLD = item('old', -120, 'b' * 64)
AFTER = (T + timedelta(seconds=181)).isoformat()
EARLY = (T + timedelta(seconds=125)).isoformat()
QUIET = {'creating': 0, 'unknown': 0}


class DecideTests(unittest.TestCase):
    def test_window_is_the_measured_create_lag_plus_the_cli_timeout(self):
        start, end = lr.window(T.isoformat())
        self.assertEqual(end - T, timedelta(seconds=180))
        self.assertEqual(T - start, timedelta(seconds=60))

    def test_absent_only_after_the_window_with_a_page_reaching_past_its_start(self):
        self.assertEqual(lr.decide(seen(), [OLD], AFTER, set(), QUIET), ('absent', None))
        self.assertEqual(lr.decide(seen(), [OLD], EARLY, set(), QUIET)[0], 'wait')
        self.assertEqual(lr.decide(seen(), [item('newer', 30, 'b' * 64)], EARLY, set(), QUIET)[0], 'wait')
        # A page too shallow to prove absence after the window closed goes to the operator, not a silent loop.
        self.assertEqual(lr.decide(seen(), [item('newer', 30, 'b' * 64)], AFTER, set(), QUIET)[0], 'operator')

    def test_a_unique_unowned_match_is_adopted_at_once(self):
        self.assertEqual(lr.decide(seen(), [OLD, item('hf', 40)], EARLY, set(), QUIET), ('adopt', 'hf'))
        self.assertEqual(lr.decide(seen(), [OLD, item('hf', 40)], AFTER, {'hf'}, QUIET), ('absent', None))

    def test_matches_outside_the_window_or_with_another_duration_do_not_count(self):
        late = item('late', 200)
        other = {**item('other', 40), 'duration': 5}
        self.assertEqual(lr.decide(seen(), [OLD, late, other], AFTER, set(), QUIET), ('absent', None))

    def test_a_sibling_still_creating_the_same_prompt_makes_it_wait(self):
        rival = {'creating': 1, 'unknown': 0}
        self.assertEqual(lr.decide(seen(), [OLD, item('hf', 40)], AFTER, set(), rival)[0], 'wait')
        self.assertEqual(lr.decide(seen(), [OLD], AFTER, set(), rival)[0], 'wait')

    def test_ambiguity_goes_to_the_operator(self):
        self.assertEqual(lr.decide(seen(), [OLD, item('a', 30), item('b', 50)], AFTER, set(), QUIET)[0], 'operator')
        self.assertEqual(lr.decide(seen(), [OLD, item('a', 30)], AFTER, set(), {'creating': 0, 'unknown': 1})[0], 'operator')
        # Two stuck siblings and nothing listed: neither create arrived.
        self.assertEqual(lr.decide(seen(), [OLD], AFTER, set(), {'creating': 0, 'unknown': 1}), ('absent', None))

    def test_an_image_listed_with_zero_duration_still_matches_a_request_without_one(self):
        image = {**seen(), 'duration': None}
        listed = lr.listed_jobs({'items': [{'id': 'img', 'created_at': (T + timedelta(seconds=20)).isoformat(),
                                            'job_type': 'seedance_2_5', 'params': {'prompt': 'p', 'duration': 0}}]})
        self.assertIsNone(listed[0]['duration'])
        self.assertEqual(lr.matches({**image, 'prompt_sha256': listed[0]['prompt_sha256']}, listed), {'img'})

    def test_listing_page_keeps_prompt_hashes_only(self):
        jobs = lr.listed_jobs({'items': [{'id': 'x', 'created_at': T.isoformat(), 'job_type': 'seedance_2_5',
                                          'params': {'prompt': 'secret words', 'duration': 9}}]})
        self.assertNotIn('secret words', repr(jobs))
        self.assertEqual(jobs[0]['duration'], 9)
        with self.assertRaises(lr.DomainError):
            lr.listed_jobs({'nothing': []})


if __name__ == '__main__':
    unittest.main()
