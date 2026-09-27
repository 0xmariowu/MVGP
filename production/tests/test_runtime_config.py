"""The one runtime config replaces the release snapshots."""
import json
import unittest
from pathlib import Path

from production.contracts import DomainError, content_hash
from production.runtime_config import DEFAULT_PATH, RuntimeConfig

RELEASE_97_COST = Path(__file__).with_name('data_release97_execution_policy.json')


class RuntimeConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = RuntimeConfig.load()
        self.raw = json.loads(DEFAULT_PATH.read_text())

    def test_live_cut_policy_and_method_hashes_stay_compatible(self):
        message = ('Live records store these hashes, so any byte change in the hashed content '
                   'breaks existing cuts/methods.')
        self.assertEqual(content_hash(self.raw['cut_policy']),
                         '6045ac10406e5fe47b8df49e04f2c209e14f2c1842e76287276f3a6cbc463dc6', message)
        expected_methods = {
            'mvgp-image-edit-v1': 'e3ab38c1a152efa1f9167b667fd97281e62e24262ab11569c29c4c0dc33fbad5',
            'mvgp-image-generate-v1': 'fc870b217ef0a9f1d91e3d921050d0791e007bb0dda445282116835da0402b77',
            'mvgp-video-v1': 'a82d79c3e7a087ab08f709cbca40978da4f01dab3545581b10297921aee0fafc',
            'mvgp-video-hf-v1': '5388db5a30330062ad9c38f4a84a97cff47749f53b2a6dbd1d7a19c1d2db204d',
        }
        self.assertEqual({name: content_hash(document) for name, document in self.raw['methods'].items()},
                         expected_methods, message)

    def test_the_cost_policy_is_the_live_release_97_document(self):
        expected = json.loads(RELEASE_97_COST.read_text())
        # The allowed retry lets a completion that provably cost nothing run once more.
        expected['operations']['complete-draft']['fal_seedance_2_5_complete']['max_attempts'] = 2
        # (owner 2026-09-27): Higgsfield takes are 1080p, 12 credits/s, held at 30 s.
        expected['operations']['submit']['seedance_2_5'].update(estimated_cost=120, reservation=360)
        expected['notes'].append(self.config.section('execution_policy')['notes'][-1])
        self.assertEqual(json.loads(self.config.document('execution_policy')), expected)
        for operation, entries in expected['operations'].items():
            self.assertEqual(self.config.section('execution_policy')['operations'][operation], entries)

    def test_documents_answer_by_their_old_role_names(self):
        video = json.loads(self.config.document('video_routes'))
        profile = video['profiles'][video['method_routes']['mvgp-video-v1']]
        self.assertEqual(profile['job_type'], 'fal_seedance_2_5')
        self.assertEqual(json.loads(self.config.document(profile['capability_role']))['max_references'], 30)
        reader = json.loads(self.config.document('review_routes'))
        self.assertEqual(reader['profiles'][reader['role_routes']['observer']]['role'], 'observer')
        self.assertEqual(set(json.loads(self.config.document('methods'))['methods']),
                         {'mvgp-image-edit-v1', 'mvgp-image-generate-v1', 'mvgp-video-v1', 'mvgp-video-hf-v1'})
        # the Higgsfield method is 1080p Seedance 2.5 with 30 references.
        hf = video['profiles'][video['method_routes']['mvgp-video-hf-v1']]
        self.assertEqual((hf['job_type'], hf['resolutions'], hf['max_references']), ('seedance_2_5', ['1080p'], 30))
        self.assertEqual(json.loads(self.config.document(hf['capability_role']))['job_type'], 'seedance_2_5')
        self.assertEqual(json.loads(self.config.document('cut_policy'))['policy_id'], 'local-cut-assembly-v1')
        with self.assertRaises(DomainError) as absent:
            self.config.document('rules')
        self.assertEqual(absent.exception.code, 'not_found')
        # Owner 2026-09-27 after the probe: the image number goes on a tag's first mention only.
        self.assertEqual((self.config.label, self.config.binding), ('lean-v1', 'first'))

    def test_a_copy_never_changes_the_config(self):
        cost = self.config.section('execution_policy')
        cost['operations'].clear()
        self.assertTrue(self.config.section('execution_policy')['operations'])

    def test_only_enabled_methods_run_and_only_for_their_task(self):
        self.assertEqual(self.config.require_method('mvgp-video-v1', task='shot')['task'], 'shot')
        self.config.require_method('mvgp-video-v1', task='stress')
        for method, task in (('mvgp-unknown', None), ('mvgp-video-v1', 'image')):
            with self.subTest(method=method), self.assertRaises(DomainError) as refused:
                self.config.require_method(method, task=task)
            self.assertEqual(refused.exception.code, 'unsupported_method')

    def test_a_malformed_config_does_not_load(self):
        for change in ({'binding': 'sometimes'}, {'cost': []}, {'label': ''}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                RuntimeConfig({**self.raw, **change})
        partial = dict(self.raw)
        partial.pop('reader')
        with self.assertRaises(ValueError):
            RuntimeConfig(partial)

    def test_set_replaces_one_document_for_tests_and_rehearsals(self):
        routes = self.config.section('video_routes')
        routes['profiles'] = {}
        self.config.set('video_routes', routes)
        self.assertEqual(self.config.section('video_routes')['profiles'], {})
        self.config.set('video_capability', {'job_type': 'x'})
        self.assertEqual(self.config.section('video_capability'), {'job_type': 'x'})


class LiveConfigTests(unittest.TestCase):
    """What production runs (moved from the release-override tests)."""
    def setUp(self):
        self.config = RuntimeConfig.load()
        self.cost = self.config.section('execution_policy')['operations']

    def test_shots_are_fal_480p_drafts_the_compiler_and_output_check_accept(self):
        from types import SimpleNamespace
        from typing import Any, cast

        from production.compiler import Compiler, VideoSettings
        from production.output_contract import validate_policy
        routes = self.config.section('video_routes')
        compiler = Compiler(cast(Any, None), cast(Any, None), cast(Any, SimpleNamespace(config=self.config)), cast(Any, None),
                            cast(Any, None), route_profiles=routes['profiles'])
        for task in ('shot', 'stress'):
            profile_id, route, audio = compiler._video_route({'release_id': 'lean-v1', 'method_id': 'mvgp-video-v1'},
                VideoSettings(model='fal_seedance_2_5', aspect_ratio='16:9', resolution='480p'), task)
            self.assertEqual((profile_id, route.draft, audio), ('fal-seedance25-draft-v2', True, True))
        validate_policy(route.output_contract, modality='video', resolutions=['1080p'])  # the completion is measured too
        for role in ('fal_video_capability', 'fal_complete_capability'):
            self.assertRegex(self.config.section(role)['input_schema_sha256'], r'^[0-9a-f]{64}$')

    def test_the_shot_route_takes_as_many_references_as_fal_and_the_old_profile_is_unchanged(self):
        # Re-audit 2026-09-27: the cap was 9 while fal takes 30 and HF exceeded 9 in 3 of 12 projects (census 2026-09-27).
        # A new profile id carries it, so candidates that froze draft-v1's hash still verify.
        routes = self.config.section('video_routes')
        profile = routes['profiles'][routes['method_routes']['mvgp-video-v1']]
        capability = self.config.section(profile['capability_role'])
        self.assertEqual((profile['max_references'], capability['max_references']), (30, 30))
        self.assertEqual(routes['profiles']['fal-seedance25-draft-v1']['max_references'], 9)

    def test_fal_takes_and_completions_are_billed_to_the_fal_envelope_in_usd_micro(self):
        draft, complete = self.cost['submit']['fal_seedance_2_5'], self.cost['complete-draft']['fal_seedance_2_5_complete']
        for policy in (draft, complete):
            self.assertEqual((policy['budget_key'], policy['budget_unit']), ('fal_owner', 'usd_micro'))
        rate = self.config.section('fal_video_capability')['usd_micros_per_second']['480p']
        self.assertEqual(draft['reservation'], 30 * rate)  # held at the longest take fal makes
        self.assertEqual(complete['max_attempts'], 2)  # a second try only after one that provably cost nothing

    def test_images_go_through_apilio_with_one_attempt(self):
        from production.asset_methods import ImageRoute
        routes = self.config.section('image_routes')
        self.assertEqual(set(routes['method_routes'].values()), {'apilio-gpt-image25-v1'})
        profile = ImageRoute.model_validate(routes['profiles']['apilio-gpt-image25-v1'])
        capability = self.config.section(profile.capability_role)
        for a in profile.aspect_ratios:
            for r in profile.resolutions:
                self.assertIn(f'{a}|{r}', capability['sizes'])
        self.assertTrue(self.config.section(profile.reference_edit_evidence_role))
        policy = self.cost['submit']['apilio_gpt_image_2_5']
        self.assertEqual((policy['budget_key'], policy['max_attempts']), ('apilio_owner_10466', 1))

    def test_cut_output_is_1920x1080(self):
        output = self.config.section('cut_policy')['output']
        self.assertEqual((output['width'], output['height']), (1920, 1080))

    def test_every_priced_operation_names_its_budget_and_loads_as_the_submission_service_reads_it(self):
        from production.submissions import CostPolicy, LocalPolicy
        for op, entries in self.cost.items():
            if entries.get('mode') == 'local':
                LocalPolicy.model_validate(entries)
                continue
            for job_type, policy in entries.items():
                with self.subTest(op=op, job_type=job_type):
                    CostPolicy.model_validate(policy)
                    if policy.get('mode') == 'live':
                        self.assertTrue(policy.get('budget_key') and policy.get('budget_unit'))

    def test_the_config_names_key_variables_never_key_values(self):
        def walk(value, path=''):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key.lower() in ('key', 'api_key', 'apikey', 'secret', 'token', 'authorization'):
                        self.fail(f'secret-looking field {path}.{key}')
                    walk(item, f'{path}.{key}')
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    walk(item, f'{path}[{i}]')
            elif isinstance(value, str):
                self.assertNotRegex(value, r'(?i)\b(sk-|bearer\s+[a-z0-9])', path)
        walk(json.loads(DEFAULT_PATH.read_text()))


if __name__ == '__main__':
    unittest.main()
