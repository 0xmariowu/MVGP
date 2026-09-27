"""Owner switches: shot-plan approval and the two AI reviews are off unless switched on."""
import unittest

from production import switches
from production.contracts import DomainError
from production.switches import Switches, SwitchesUpdate
from production.tests import test_submissions
from production.tests.fixtures import owner_session

OFF = {'shot_plan_approval': False, 'ai_prompt_review': False, 'ai_director_review': False}
ALL_OFF = {**OFF, 'asset_stress_test': False, 'source_reading': False, 'sample_mode': False}


class ShotPlanSwitchTests(unittest.TestCase):
    def setUp(self):
        self.s = test_submissions.SubmissionTests()
        self.s.setUp()
        self.addCleanup(self.s.doCleanups)
        self.s.four_take_shot()
        self.s.policy['owner_gates'] = {'shot_plan': 'required'}
        self.s.policy['owner_switches'] = OFF
        self.s.f.config.set('execution_policy', self.s.policy)
        self.s.c.activate()
        self.candidate = self.s.c.compiler.prepare(self.s.actor, self.s.pid, self.s.c.request(
            self.s.target, task='stress', method=self.s.method, inputs=[self.s.selection], key='switch-shot'))
        self.session = owner_session(self.s.f.auth)
        self.human = self.s.f.auth.authenticate(self.session['session_token'], channel='cookie')
        self.switches = Switches(self.s.store, self.s.f.auth, self.s.f.flow.config)

    def set(self, key, **values):
        return self.switches.set(self.human, self.s.pid, SwitchesUpdate(
            idempotency_key=key, switches=values, csrf_token=self.session['csrf_token']), origin='https://studio.example')

    def test_off_by_release_default_a_shot_fires_without_a_plan(self):
        self.assertEqual(self.switches.get(self.s.actor, self.s.pid), {'switches': ALL_OFF})
        job = self.s.service.submit(self.s.actor, self.s.pid, self.s.request('no-plan', self.candidate))
        self.assertEqual(job['body']['state'], 'queued')

    @unittest.skip('Review/switch path removed from production; module kept dormant (production/DORMANT.md)')
    def test_switching_on_brings_the_approval_back(self):
        self.assertTrue(self.set('on', shot_plan_approval=True)['switches']['shot_plan_approval'])
        with self.assertRaises(DomainError) as refused:
            self.s.service.submit(self.s.actor, self.s.pid, self.s.request('needs-plan', self.candidate))
        self.assertIn('shot plan', refused.exception.message)
        self.set('off', shot_plan_approval=False)
        self.assertEqual(self.s.service.submit(self.s.actor, self.s.pid, self.s.request('free', self.candidate))['body']['state'], 'queued')

    def test_only_the_owners_session_changes_a_switch(self):
        with self.assertRaises(DomainError):
            self.switches.set(self.s.actor, self.s.pid, SwitchesUpdate(idempotency_key='agent', switches={'asset_stress_test': True},
                              csrf_token=self.session['csrf_token']), origin='https://studio.example')
        with self.assertRaises(DomainError):
            self.switches.set(self.human, self.s.pid, SwitchesUpdate(idempotency_key='csrf', switches={'asset_stress_test': True},
                              csrf_token='x' * 32), origin='https://studio.example')
        self.assertEqual(self.switches.get(self.s.actor, self.s.pid), {'switches': ALL_OFF})

    def test_every_film_keeps_its_own_switch(self):
        # simulated run: a fixed record id let only one project ever set its switch (409 for every other).
        first = self.s.store.get_object(self.s.pid, self.s.pid)
        self.s.store.create_project('project_second', first['body'], 'operator')
        session = owner_session(self.s.f.auth)
        human = self.s.f.auth.authenticate(session['session_token'], channel='cookie')
        def turn_on(pid, key):
            return self.switches.set(human, pid, SwitchesUpdate(idempotency_key=key, switches={'asset_stress_test': True},
                                     csrf_token=session['csrf_token']), origin='https://studio.example')
        self.assertTrue(turn_on(self.s.pid, 'first-film')['switches']['asset_stress_test'])
        self.assertTrue(turn_on('project_second', 'second-film')['switches']['asset_stress_test'])
        self.assertTrue(switches.current(self.s.store, self.s.f.flow.config, 'project_second')['asset_stress_test'])

    def test_a_record_under_the_first_fixed_id_still_reads_and_updates(self):
        self.s.store.create_object(self.s.pid, switches.KIND, {'switches': {'asset_stress_test': True}}, switches.AUTHOR,
                                   object_id=switches.OBJECT_ID)
        self.assertTrue(switches.current(self.s.store, self.s.f.flow.config, self.s.pid)['asset_stress_test'])
        self.assertFalse(self.set('off-again', asset_stress_test=False)['switches']['asset_stress_test'])
        self.assertFalse(switches.current(self.s.store, self.s.f.flow.config, self.s.pid)['asset_stress_test'])

    def test_reading_the_source_first_is_on_for_a_new_recreation_project_and_can_be_turned_off(self):

        first = self.s.store.get_object(self.s.pid, self.s.pid)
        self.s.store.create_project('project_rec', {**first['body'], 'branch': 'recreation', 'reader_switch': True}, 'operator')
        self.s.store.create_project('project_old_rec', {**first['body'], 'branch': 'recreation'}, 'operator')
        releases = self.s.f.flow.config
        self.assertTrue(switches.current(self.s.store, releases, 'project_rec')['source_reading'])
        self.assertFalse(switches.current(self.s.store, releases, 'project_old_rec')['source_reading'])
        self.assertFalse(switches.current(self.s.store, releases, self.s.pid)['source_reading'])
        session = owner_session(self.s.f.auth)
        human = self.s.f.auth.authenticate(session['session_token'], channel='cookie')
        off = self.switches.set(human, 'project_rec', SwitchesUpdate(idempotency_key='reader-off', switches={'source_reading': False},
                                csrf_token=session['csrf_token']), origin='https://studio.example')
        self.assertFalse(off['switches']['source_reading'])

    def test_only_the_stress_test_key_can_be_set(self):
        # the dormant keys toggle nothing, so they are not accepted.
        from pydantic import ValidationError
        for key in ('shot_plan_approval', 'ai_prompt_review', 'ai_director_review'):
            with self.assertRaises(ValidationError):
                SwitchesUpdate(idempotency_key='old', switches={key: True}, csrf_token='x' * 32)

    def test_a_release_cannot_turn_the_stress_test_on_by_default(self):
        self.s.policy['owner_switches'] = {**OFF, 'asset_stress_test': True}
        self.s.f.config.set('execution_policy', self.s.policy)
        self.s.c.activate()
        self.assertFalse(self.switches.get(self.s.actor, self.s.pid)['switches']['asset_stress_test'])

    def test_a_release_without_switch_defaults_keeps_every_step_on(self):
        del self.s.policy['owner_switches']
        self.s.f.config.set('execution_policy', self.s.policy)
        self.s.c.activate()
        current = self.switches.get(self.s.actor, self.s.pid)['switches']
        self.assertEqual({k for k, v in current.items() if not v}, {'asset_stress_test', 'source_reading', 'sample_mode'})  # off unless set (source_reading: an original film)

    def test_the_owner_turns_the_stress_test_switch_on_and_off(self):
        # (owner: 可以保留成一个开关).
        self.assertFalse(self.switches.get(self.s.actor, self.s.pid)['switches']['asset_stress_test'])
        self.assertTrue(self.set('stress-on', asset_stress_test=True)['switches']['asset_stress_test'])
        self.assertTrue(switches.current(self.s.store, self.s.f.flow.config, self.s.pid)['asset_stress_test'])
        self.assertFalse(self.set('stress-off', asset_stress_test=False)['switches']['asset_stress_test'])


if __name__ == '__main__':
    unittest.main()
