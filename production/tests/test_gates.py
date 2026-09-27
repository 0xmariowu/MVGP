"""Preflight tests use actual candidates, never author-provided gate summaries."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from production.contracts import DomainError, ObjectRef, content_hash
from production.gates import Gates
from production.tests import test_compiler


class GatesTests(unittest.TestCase):
    def setUp(self):
        self.c = test_compiler.CompilerTests()
        self.c.setUp()
        self.addCleanup(self.c.doCleanups)
        self.f = self.c.f
        self.target = self.f.draft(self.f.definition())

    def gate(self):
        return Gates(self.f.store, self.f.flow, self.f.media)

    def candidate(self, **kwargs):
        return self.c.compiler.prepare(self.c.actor, 'project_1', self.c.request(self.target, **kwargs))

    def evaluate(self, obj):
        return self.gate().evaluate('project_1', self.f.ref(obj))

    def shotlist_result(self, scene_content, *, scene_dependency=True):
        target, selection, method, _ = self.c.video()
        scene = self.f.store.get_object('project_1', target['body']['dependencies'][0]['object_id'])
        scene_content = scene['body']['content'].split('\n## Coverage draft', 1)[0] + '\n' + scene_content
        scene = self.f.store.append_revision('project_1', scene['object_id'], scene['revision'],
            {**scene['body'], 'content': scene_content}, 'author')
        planning = self.f.store.create_object('project_1', 'asset', {
            'content': 'Scene planning reference', 'dependencies': [self.f.ref(scene).model_dump()]}, 'author')
        target = self.f.store.append_revision('project_1', target['object_id'], target['revision'], {
            **target['body'], 'content': {**target['body']['content'], 'shot': 'S01-020A'},
            'dependencies': [self.f.ref(planning).model_dump(), *([self.f.ref(scene).model_dump()] if scene_dependency else [])]}, 'author')
        method = self.f.store.append_revision('project_1', method['object_id'], method['revision'], {
            **method['body'], 'content': {**method['body']['content'], 'target': self.f.ref(target).model_dump()},
            'dependencies': [self.f.ref(target).model_dump()]}, 'author')
        selection = self.f.store.append_revision('project_1', selection['object_id'], selection['revision'], {
            **selection['body'], 'content': {**selection['body']['content'], 'target': self.f.ref(target).model_dump()}}, 'author')
        # An indirect scene satisfies compiler prerequisites. The gate still
        # requires the card itself to declare that scene dependency.
        candidate = self.c.compiler.prepare(self.c.actor, 'project_1', self.c.request(
            target, task='shot', method=method, inputs=[selection]))
        gate = Gates(self.f.store, self.f.flow, self.f.media)
        result = gate.evaluate('project_1', self.f.ref(candidate))
        checks = [check for check in result['checks'] if check['rule_id'] == 'authority.shotlist']
        self.assertEqual(len(checks), 1, result)
        return result, checks[0], scene

    def test_a_video_reference_and_a_shot_with_no_reference_pass_on_higgsfield(self):
        """(rehearsal 2026-09-27): images and videos are numbered apart, so a video's n
        counts videos; a Higgsfield shot with no reference goes as t2v. Both passed the compiler and were refused here."""
        target, selection, method, _ = self.c.video(look=False, world_video=True)
        candidate = self.c.compiler.prepare(self.c.actor, 'project_1', self.c.request(target, task='shot', method=method,
                                                                                    inputs=[selection], key='gate-video'))
        result = self.evaluate(candidate)
        self.assertTrue(result['mechanical_pass'], result['blocking'])
        def no_tags(card):
            material = card['The material']
            for field, value in list(material.items()):
                if isinstance(value, str) and '@' in value:
                    material[field] = value.replace('@', '')
                elif isinstance(value, list):
                    material[field] = []
            card['_production']['prompt'] = 'FIRST FRAME\nAn empty white test room, nobody in it.\n\nCAMERA\nLocked off, 35mm.\n\n4s. No music.'
        target, selection, method, _ = self.c.video(look=False, card_edit=no_tags)
        candidate = self.c.compiler.prepare(self.c.actor, 'project_1', self.c.request(target, task='shot', method=method,
                                                                                    inputs=[selection], key='gate-t2v'))
        self.assertEqual((candidate['body']['request']['params']['mode'], candidate['body']['request']['references']), ('t2v', []))
        result = self.evaluate(candidate)
        self.assertTrue(result['mechanical_pass'], result['blocking'])

    def test_shotlist_coverage_draft_passes(self):
        result, check, scene = self.shotlist_result('## Coverage draft\n020A,8s: The box crosses the room.\n')
        self.assertTrue(result['mechanical_pass'], result['blocking'])
        self.assertEqual(check['status'], 'pass')
        self.assertEqual(check['evidence'][0]['scene'], self.f.ref(scene).model_dump())

    def test_shotlist_table_passes(self):
        result, check, _ = self.shotlist_result('## 镜头清单\n| 镜头号 | 景别 |\n| S01-020A | Wide |\n')
        self.assertTrue(result['mechanical_pass'], result['blocking'])
        self.assertEqual(check['status'], 'pass')

    def test_shotlist_preliminary_coverage_table_passes(self):
        # The original film's scene (authored before the rule) names its shot list "Preliminary coverage".
        result, check, _ = self.shotlist_result('## Preliminary coverage\n| S01-010A | Two-shot |\n| S01-020A | Listens |\n')
        self.assertTrue(result['mechanical_pass'], result['blocking'])
        self.assertEqual(check['status'], 'pass')

    def test_shotlist_preliminary_coverage_without_the_shot_is_advice(self):
        result, check, _ = self.shotlist_result('## Preliminary coverage\n| S01-010A | Two-shot |\n')
        self.assertEqual(check['status'], 'advisory')
        self.assertTrue(result['mechanical_pass'], result['blocking'])

    def test_shotlist_unlisted_shot_is_advice_with_scene_repair(self):
        result, check, scene = self.shotlist_result('## Coverage draft\n030A,8s: Another shot.\n')
        self.assertTrue(result['mechanical_pass'], result['blocking'])
        self.assertEqual(check['status'], 'advisory')
        self.assertEqual(check['message'], 'Scene shot list does not include this shot')
        repair = check['evidence'][-1]['repair']
        self.assertIn(scene['object_id'], repair)
        self.assertIn(f'revision {scene["revision"]}', repair)

    def test_shotlist_missing_scene_dependency_is_advice(self):
        result, check, _ = self.shotlist_result('## Coverage draft\n020A,8s: Listed elsewhere.\n', scene_dependency=False)
        self.assertTrue(result['mechanical_pass'], result['blocking'])
        self.assertEqual(check['status'], 'advisory')
        self.assertEqual(check['message'], 'Shot card declares no scene artifact')
        self.assertEqual(check['path'], 'target.dependencies')

    def test_shotlist_ignores_other_sections_and_partial_ids(self):
        result, check, _ = self.shotlist_result(
            'S01-020A is mentioned before the list.\n## Coverage draft\n'
            '1020A,8s: Wrong prefix.\n020AB,8s: Wrong suffix.\nS99-020A | Wrong scene.\n'
            '## Performance\n020A,8s: Outside the shot list.\n')
        self.assertEqual(check['status'], 'advisory')
        self.assertEqual(check['message'], 'Scene shot list does not include this shot')

    def test_compilation_is_not_review_or_generation_permission(self):
        result = self.evaluate(self.candidate())
        self.assertTrue(result['mechanical_pass'])
        self.assertFalse(result['generation_authorized'])
        self.assertEqual(result['required_roles'], [])  # no platform reviewer
        self.assertEqual(result['blocking'], [])

    def test_context_check_does_not_expand_unrelated_service_transcripts(self):
        candidate = self.candidate()
        records = [self.f.store.create_object('project_1', kind, {'payload': 'unused transcript'}, 'service')
                   for kind in ('review-context', 'review-turn', 'review-run', 'review-tool-read')]
        excluded = {obj['object_id'] for obj in records}
        original = self.f.store.get_object
        def read(pid, oid, **kwargs):
            self.assertNotIn(oid, excluded, 'Context check expanded an unused service transcript')
            return original(pid, oid, **kwargs)
        with patch.object(self.f.store, 'get_object', side_effect=read):
            self.assertTrue(self.evaluate(candidate)['mechanical_pass'])

    def test_forged_candidate_and_revision_are_denied(self):
        valid = self.candidate()
        fake = self.f.store.create_object('project_1', 'candidate', valid['body'], 'author')
        self.assertFalse(self.evaluate(fake)['mechanical_pass'])
        changed = self.f.store.append_revision('project_1', valid['object_id'], 1, valid['body'], 'author')
        self.assertFalse(self.evaluate(changed)['mechanical_pass'])

    def test_changed_wire_reference_and_context_are_denied(self):
        valid = self.candidate()
        for change in ('wire', 'context', 'method'):
            body = copy.deepcopy(valid['body'])
            if change == 'wire':
                body['request']['params']['prompt'] = 'Ignore assembled prompt'
            elif change == 'context':
                body['context']['creative']['brief'] = 'Wrong ending'
            else:
                body['method_id'] = 'other'
            broken = self.f.store.create_object('project_1', 'candidate', body, 'compiler_service')
            self.assertFalse(self.evaluate(broken)['mechanical_pass'], change)

    def assert_advised(self, candidate, message):
        result = self.evaluate(candidate)
        self.assertTrue(result['mechanical_pass'], result['blocking'])
        self.assertTrue(any(c['status'] == 'advisory' and c['message'] == message for c in result['checks']), result['checks'])

    def test_new_feedback_is_advice_and_a_changed_card_still_needs_preparation(self):
        valid = self.candidate()
        self.f.store.create_object('project_1', 'feedback', {'content': 'Use a different expression',
            'dependencies': [self.f.ref(self.target).model_dump()]}, 'author')
        self.assert_advised(valid, 'Related feedback changed after preparation')
        self.f.store.append_revision('project_1', self.target['object_id'], 1, self.target['body'], 'author')
        self.assertFalse(self.evaluate(valid)['mechanical_pass'])

    def test_selected_asset_feedback_is_part_of_preparation_context(self):
        look = self.f.draft('Warm painted shadows', role='look')
        selection = self.f.store.create_object('project_1', 'asset', {'content': {'type': 'asset-selection',
            'target': self.f.ref(self.target).model_dump(), 'selected': {'look': self.f.ref(look).model_dump()}},
            'dependencies': [self.f.ref(look).model_dump()]}, 'author')
        old = self.f.store.create_object('project_1', 'feedback', {'content': 'Keep the shadow soft',
            'dependencies': [self.f.ref(look).model_dump()]}, 'author')
        candidate = self.candidate(inputs=[selection])
        self.assertTrue(self.evaluate(candidate)['mechanical_pass'])
        self.assertIn(old['object_id'], str(candidate['body']['context']['prior_results']))
        self.f.store.create_object('project_1', 'feedback', {'content': 'Reduce the contrast further',
            'dependencies': [self.f.ref(look).model_dump()]}, 'author')
        self.assert_advised(candidate, 'Related feedback changed after preparation')

    def test_missing_or_tampered_actual_bytes_are_denied(self):
        image = self.f.image()
        self.target = self.f.draft(self.f.definition(recipe='pose', reference_roles=['identity']))
        valid = self.candidate(inputs=[image])
        self.assertTrue(self.evaluate(valid)['mechanical_pass'])
        path = self.f.media.path_for('project_1', image['object_id'])
        path.chmod(0o600)
        path.write_bytes(b'not the original image')
        self.assertFalse(self.evaluate(valid)['mechanical_pass'])

    def test_feedback_on_generated_take_is_advice(self):
        candidate = self.candidate()
        media = self.f.store.create_object('project_1', 'media', {'dependencies': [self.f.ref(candidate).model_dump()]}, 'worker_service')
        self.f.store.create_object('project_1', 'feedback', {'content': 'The expression failed',
            'dependencies': [self.f.ref(media).model_dump()]}, 'author')
        self.assert_advised(candidate, 'Related feedback changed after preparation')

    def test_real_dispatch_media_feedback_is_advice_and_repair_refreshes(self):
        original = self.candidate()
        intent = self.f.store.create_object('project_1', 'dispatch-intent', {
            'dependencies': [self.f.ref(original).model_dump()], 'private': 'PRIVATE_INTENT'}, 'submission_service')
        media = self.f.store.create_object('project_1', 'media', {
            'dependencies': [self.f.ref(intent).model_dump()], 'media_type': 'video/mp4'}, 'worker_service')
        feedback = self.f.store.create_object('project_1', 'feedback', {
            'content': 'The cart returned behind the marker.',
            'dependencies': [self.f.ref(media).model_dump()]}, 'review_feedback_service')
        self.assert_advised(original, 'Related feedback changed after preparation')
        repaired = self.candidate(key='repair-with-feedback')
        self.assertTrue(self.evaluate(repaired)['mechanical_pass'])
        self.assertIn(feedback['object_id'], str(repaired['body']['context']['prior_results']))
        self.assertNotIn('PRIVATE_INTENT', str(repaired['body']['context']))
        self.f.store.append_revision('project_1', feedback['object_id'], 1,
            {**feedback['body'], 'content': 'Still behind after the cut.'}, 'review_feedback_service')
        self.assert_advised(repaired, 'Related feedback changed after preparation')

    def shot(self, **video):
        target, selection, method, _ = self.c.video(**video)
        return self.c.compiler.prepare(self.c.actor, 'project_1', self.c.request(target, task='shot', method=method, inputs=[selection]))

    def forged(self, candidate, change):
        body = copy.deepcopy(candidate['body'])
        change(body)
        body['request']['params']['prompt'] = body['compilation']['prompt']
        body['compilation']['wire_provenance']['wire_sha256'] = hashlib.sha256(body['compilation']['prompt'].encode()).hexdigest()
        return self.evaluate(self.f.store.create_object('project_1', 'candidate', body, 'compiler_service'))

    def test_an_old_candidate_is_checked_by_its_stored_prompt_alone(self):
        # candidates prepared before 2026-09-27 keep their stored prompt; a reshoot still fires.
        candidate = self.shot()
        def old(body):
            for key in ('writer_text', 'additions', 'prompt_constants', 'binding', 'writer_sha256', 'additions_sha256'):
                del body['compilation'][key]
            body['compilation']['assembly'] = {'prompt': '# S02-020A\n\nassembled', 'gate_errors': ['[资产] old finding']}
        result = self.forged(candidate, old)
        self.assertTrue(result['mechanical_pass'], result['blocking'])
        body = copy.deepcopy(candidate['body'])
        old(body)
        body['request']['params']['prompt'] = body['compilation']['prompt'] = body['compilation']['prompt'] + ' Extra.'
        changed = self.evaluate(self.f.store.create_object('project_1', 'candidate', body, 'compiler_service'))
        self.assertTrue(any(c['rule_id'] == 'prompt.provenance' for c in changed['blocking']), changed['blocking'])
        bad_ref = ObjectRef(object_id=candidate['object_id'], revision=1, digest='f'*64)
        self.assertFalse(self.gate().evaluate('project_1', bad_ref)['mechanical_pass'])

    def test_only_the_writer_text_plus_allowed_additions_goes_out(self):
        # (owner 2026-09-26 "必须100%做到"): one platform sentence more is a send fault.
        candidate = self.shot()
        self.assertTrue(self.evaluate(candidate)['mechanical_pass'])
        def extra_sentence(body):
            body['compilation']['prompt'] += ' Cinematic masterpiece.'
        def forged_addition(body):
            compiled = body['compilation']
            compiled['additions'].append({'kind': 'look', 'at': len(compiled['writer_text']), 'text': '\n\nSTYLE: made up'})
            compiled['additions_sha256'] = hashlib.sha256(json.dumps(compiled['additions'], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            compiled['prompt'] += '\n\nSTYLE: made up'
        def rewritten_writer(body):
            body['compilation']['writer_text'] = body['compilation']['writer_text'].replace('locked off', 'handheld')
        for change in (extra_sentence, forged_addition, rewritten_writer):
            with self.subTest(change=change.__name__):
                result = self.forged(candidate, change)
                self.assertFalse(result['mechanical_pass'])
                self.assertTrue(any(c['rule_id'] == 'prompt.provenance' for c in result['blocking']), result['blocking'])

    def test_an_oversized_prompt_is_a_send_fault_measured_in_bytes(self):
        # The fal adapter refuses over 65,536 UTF-8 bytes; 22,000 Chinese characters are 66,000 bytes.
        from production.gates import PROMPT_MAX_BYTES
        def long(card):
            card['_production']['prompt'] += '\n' + '雨' * (PROMPT_MAX_BYTES // 3 + 1)
        result = self.evaluate(self.shot(card_edit=long))
        self.assertTrue(any(c['rule_id'] == 'prompt.size' for c in result['blocking']), result['blocking'])

    def test_stress_probe_does_not_require_its_own_prior_success(self):
        target, selection, method, _ = self.c.video()
        candidate = self.c.compiler.prepare(self.c.actor, 'project_1', self.c.request(target, task='stress', method=method, inputs=[selection]))
        result = self.evaluate(candidate)
        self.assertTrue(result['mechanical_pass'], result['blocking'])
        self.assertFalse(any(check['status'] == 'deny' for check in result['checks']))
        self.assertFalse(result['generation_authorized'])

    def test_compiler_advice_is_a_warning_never_a_denial(self):

        target, selection, method, _ = self.c.video(look=False)
        candidate = self.c.compiler.prepare(self.c.actor, 'project_1', self.c.request(target, task='shot', method=method, inputs=[selection]))
        body = copy.deepcopy(candidate['body'])
        body['compilation']['authorship']['advice'] = ['No voice text selected for @ann']
        fake = self.f.store.create_object('project_1', 'candidate', body, 'compiler_service')
        result = self.evaluate(fake)
        self.assertTrue(result['mechanical_pass'], result['blocking'])
        self.assertTrue(any(c['rule_id'] == 'writer.advice' and c['status'] == 'warning' and 'voice text' in c['message']
                            for c in result['checks']))

    def test_the_released_duration_window_is_a_hard_limit(self):
        # Bug hunt 2026-09-24: Higgsfield's duration window must block, not advise.
        timing = {'job_type': 'seedance_2_5', 'tasks': ['shot', 'stress'], 'timing_modes': ['timed', 'stages', 'ordinal'],
                  'duration_contract': {'min': 5, 'max': 30, 'integer': True}}
        target, selection, method, _ = self.c.video()
        self.f.config.set('video_timing', timing)
        self.c.activate()
        short = self.c.compiler.prepare(self.c.actor, 'project_1', self.c.request(target, task='shot', method=method, inputs=[selection], key='short'))
        self.assertEqual(short['body']['request']['params']['duration'], 4)
        result = self.evaluate(short)
        self.assertFalse(result['mechanical_pass'])
        self.assertTrue(any(c['rule_id'] == 'duration.request' and c['status'] == 'deny' for c in result['blocking']))
        self.f.config.set('video_timing', {**timing, 'duration_contract': {'min': 4, 'max': 30, 'integer': True}})
        self.c.activate()
        inside = self.c.compiler.prepare(self.c.actor, 'project_1', self.c.request(target, task='shot', method=method, inputs=[selection], key='inside'))
        self.assertTrue(self.evaluate(inside)['mechanical_pass'])

    def stress_context(self, task='stress'):
        target, selection, method, _ = self.c.video()
        scene_ref = target['body']['dependencies'][0]
        other = self.f.store.create_object('project_1', 'shot', {
            'content': {'Direction': {'expected visible performance': 'Independent probe.'}},
            'dependencies': [scene_ref]}, 'author')
        candidate = self.c.compiler.prepare(self.c.actor, 'project_1',
            self.c.request(target, task=task, method=method, inputs=[selection]))
        return target, scene_ref, other, candidate

    def current(self, body):
        with self.f.store._using(None, write=False) as db:
            self.gate()._context_current('project_1', body, db)

    def test_unrelated_probe_edits_do_not_invalidate_standalone_stress(self):
        _, _, other, candidate = self.stress_context()
        self.current(candidate['body'])
        self.f.store.append_revision('project_1', other['object_id'], 1,
            {**other['body'], 'content': 'Repaired unrelated probe'}, 'author')
        self.current(candidate['body'])
        self.assertTrue(self.evaluate(candidate)['mechanical_pass'])

    def test_stress_target_and_scene_changes_invalidate_feedback_is_advice(self):
        target, scene_ref, _, candidate = self.stress_context()
        for changed in ('target', 'scene', 'feedback'):
            with self.subTest(changed=changed), self.f.store._using(None, write=True) as db:
                db.execute('SAVEPOINT relevant_change')
                if changed == 'feedback':
                    self.f.store.create_object('project_1', 'feedback', {
                        'content': 'Hat is missing', 'dependencies': [self.f.ref(target).model_dump()]}, 'author', conn=db)
                else:
                    oid = target['object_id'] if changed == 'target' else scene_ref['object_id']
                    obj = self.f.store.get_object('project_1', oid, conn=db)
                    self.f.store.append_revision('project_1', oid, obj['revision'], obj['body'], 'author', conn=db)
                if changed == 'feedback':
                    advice = self.gate()._context_current('project_1', candidate['body'], db)
                    self.assertEqual(advice, [('Related feedback changed after preparation', 'context.prior_results')])
                elif changed == 'scene':  # the scene is the writer's context, frozen for the card
                    self.gate()._context_current('project_1', candidate['body'], db)
                else:
                    with self.assertRaises(DomainError):
                        self.gate()._context_current('project_1', candidate['body'], db)
                db.execute('ROLLBACK TO relevant_change')
                db.execute('RELEASE relevant_change')

    def test_neighbor_changes_are_advice_for_formal_and_legacy_contexts(self):
        _, _, other, candidate = self.stress_context(task='shot')
        for task in ('shot', 'stress'):
            body = copy.deepcopy(candidate['body'])
            body['task'] = body['context']['task'] = task
            body['context']['neighbors'].pop('scope', None)
            body['context']['context_hash'] = content_hash({k: v for k, v in body['context'].items() if k != 'context_hash'})
            body['context_hash'] = body['context']['context_hash']
            self.current(body)
            with self.f.store._using(None, write=True) as db:
                db.execute('SAVEPOINT neighbor_change')
                self.f.store.append_revision('project_1', other['object_id'], 1, other['body'], 'author', conn=db)
                advice = self.gate()._context_current('project_1', body, db)
                self.assertEqual(advice, [('Neighboring shot plan changed', 'context.neighbors')])
                db.execute('ROLLBACK TO neighbor_change')
                db.execute('RELEASE neighbor_change')
