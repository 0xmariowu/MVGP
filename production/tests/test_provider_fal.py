"""fal Seedance 2.5 adapter: fixed hosts, uploads before paying, never resent."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from production.contracts import DomainError
from production.provider_fal import (
    DRAFT_DAYS,
    ENDPOINTS,
    QUEUE,
    TEXT_TO_VIDEO,
    UPLOAD,
    UPLOAD_TOKEN,
    FalSeedance,
    input_schema_hash,
)
from production.provider_types import ResolvedReference
from production.provider_router import ProviderRouter

RID = '01a0d7fa-efe3-79b2-b20f-62cc6d49e741'
DRAFT = 'draft_gAAAAABqtkRbZ51Fhw-Q_=='
PARAMS = [{'name': 'prompt', 'type': 'string'}, {'name': 'duration', 'type': 'integer', 'enum': list(range(4, 31))},
          {'name': 'resolution', 'type': 'string', 'enum': ['480p', '720p', '1080p']},
          {'name': 'aspect_ratio', 'type': 'string', 'enum': ['16:9', '9:16', '1:1']},
          {'name': 'generate_audio', 'type': 'boolean', 'default': True}, {'name': 'draft', 'type': 'boolean'}]
CAPABILITY = {
    'fal_seedance_2_5': {'job_type': 'fal_seedance_2_5', 'type': 'video', 'endpoint': ENDPOINTS['fal_seedance_2_5'],
                         'max_references': 9, 'params': PARAMS, 'usd_micros_per_second': {'480p': 207741}},
    'fal_seedance_2_5_complete': {'job_type': 'fal_seedance_2_5_complete', 'type': 'video',
                                  'endpoint': ENDPOINTS['fal_seedance_2_5_complete'], 'max_references': 0,
                                  'params': [{'name': 'draft_id', 'type': 'string'},
                                             {'name': 'resolution', 'type': 'string', 'enum': ['1080p']}] + PARAMS[1:2] + PARAMS[3:5]}}


def draft_request(refs=0, **params):
    return {'job_type': 'fal_seedance_2_5', 'references': [{'object_id': f'o{n}', 'revision': 1} for n in range(refs)],
            'params': {'prompt': '@Image1 turns to camera. Total 4s.', 'duration': 4, 'resolution': '480p',
                       'aspect_ratio': '16:9', 'generate_audio': True, 'draft': True, **params}}


def complete_request():
    return {'job_type': 'fal_seedance_2_5_complete', 'references': [],
            'params': {'draft_id': DRAFT, 'resolution': '1080p', 'duration': 4, 'aspect_ratio': '16:9', 'generate_audio': True}}


def intent(request, operation='submit', mode='fake'):
    return {'operation': operation, 'cost': {'mode': mode}, 'request': request}


class FalAdapterTests(unittest.TestCase):
    def setUp(self):
        self.calls, self.answers = [], {}
        def transport(method, url, headers, json_body, data, timeout):
            self.calls.append({'method': method, 'url': url, 'json': json_body, 'data': data, 'auth': headers.get('Authorization')})
            answer = self.answers.get(url) or self.answers.get(method)
            if isinstance(answer, Exception):
                raise answer
            return answer
        self.answers[UPLOAD_TOKEN] = (200, json.dumps({'token': 'cdn-t', 'token_type': 'Bearer'}).encode())
        self.answers[UPLOAD] = (200, json.dumps({'access_url': 'https://v3b.fal.media/files/b/x/ref.png'}).encode())
        self.answers['POST'] = (200, json.dumps({'status': 'IN_QUEUE', 'request_id': RID,
                                                 'status_url': f'{QUEUE}bytedance/seedance-2.5/requests/{RID}/status',
                                                 'response_url': f'{QUEUE}bytedance/seedance-2.5/requests/{RID}'}).encode())
        self.status_url = f'{QUEUE}bytedance/seedance-2.5/requests/{RID}/status'
        self.response_url = f'{QUEUE}bytedance/seedance-2.5/requests/{RID}'
        self.adapter = FalSeedance(CAPABILITY, transport=transport, clock=lambda: 1_000_000)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.refs = []
        for n in range(2):
            path = Path(tmp.name) / f'r{n}.png'
            path.write_bytes(b'\x89PNG fake ' + bytes([n]))
            self.refs.append(ResolvedReference({'object_id': f'o{n}', 'revision': 1}, path,
                                               hashlib.sha256(path.read_bytes()).hexdigest(), 'image/png'))

    def paid(self):
        return [c for c in self.calls if c['url'].startswith(QUEUE) and c['method'] == 'POST']

    def test_a_draft_uploads_references_in_order_then_sends_one_480p_draft(self):
        receipt = self.adapter.submit(intent(draft_request(2)), self.refs)
        uploads = [c['data'] for c in self.calls if c['url'] == UPLOAD]
        self.assertEqual(uploads, [r.path.read_bytes() for r in self.refs])
        call, = self.paid()
        self.assertEqual(call['url'], QUEUE + 'bytedance/seedance-2.5/reference-to-video')
        self.assertEqual(call['json']['duration'], '4')  # fal takes the duration as a string
        self.assertEqual((call['json']['draft'], call['json']['resolution']), (True, '480p'))
        self.assertEqual(call['json']['image_urls'], ['https://v3b.fal.media/files/b/x/ref.png'] * 2)
        self.assertEqual(call['auth'], 'Key fake')  # fake mode never reads the real key
        self.assertEqual((receipt.job_id, receipt.state), (RID, 'submitted'))
        self.assertNotIn('fal.media', json.dumps(receipt.raw_receipt))

    def test_polling_reports_running_then_the_take_with_seed_draft_id_and_expiry(self):
        self.answers[self.status_url] = (202, json.dumps({'status': 'IN_QUEUE', 'queue_position': 3}).encode())
        self.assertEqual(self.adapter.get(RID, draft_request()).state, 'running')  # fal answers 202 while queued
        self.answers[self.status_url] = (202, json.dumps({'status': 'IN_PROGRESS'}).encode())
        self.assertEqual(self.adapter.get(RID, draft_request()).state, 'running')
        self.answers[self.status_url] = (200, json.dumps({'status': 'COMPLETED'}).encode())
        self.answers[self.response_url] = (200, json.dumps({'video': {'url': 'https://v3b.fal.media/files/b/v.mp4',
            'content_type': 'video/mp4', 'file_size': 420318}, 'seed': 1953829483, 'draft_id': DRAFT}).encode())
        evidence = []
        receipt = self.adapter.get(RID, draft_request(), evidence_sink=evidence.append)
        self.assertEqual((receipt.state, receipt.result_url), ('succeeded', 'https://v3b.fal.media/files/b/v.mp4'))
        self.assertEqual((receipt.raw_receipt['seed'], receipt.raw_receipt['draft_id']), (1953829483, DRAFT))
        self.assertEqual(receipt.raw_receipt['draft_expires_at'], 1_000_000 + DRAFT_DAYS * 86400 - 6 * 3600)
        self.assertEqual(receipt.settled_cost, 4 * 207741)  # pinned 480p price x 4 s (the probe paid 830962 micros)
        self.assertNotIn('fal.media/files', json.dumps(receipt.raw_receipt) + json.dumps(evidence))

    def test_a_completion_sends_only_the_draft_id_at_1080p(self):
        receipt = self.adapter.submit(intent(complete_request()), [])
        call, = self.paid()
        self.assertEqual(call['url'], QUEUE + 'bytedance/seedance-2.5/draft/complete')
        self.assertEqual(call['json'], {'draft_id': DRAFT, 'resolution': '1080p'})
        self.assertEqual(receipt.state, 'submitted')
        with self.assertRaises(DomainError):
            self.adapter.submit(intent({**complete_request(), 'params': {**complete_request()['params'], 'draft_id': 'x'}}), [])
        with self.assertRaises(DomainError):  # a completion takes no references
            self.adapter.submit(intent({**complete_request(), 'references': [{'object_id': 'o0', 'revision': 1}]}), self.refs[:1])

    def test_foreign_hosts_are_never_trusted(self):
        answer = json.loads(self.answers['POST'][1])
        self.answers['POST'] = (200, json.dumps({**answer, 'status_url': 'https://evil.example/status'}).encode())
        with self.assertRaises(DomainError) as foreign:
            self.adapter.submit(intent(draft_request(2)), self.refs)
        self.assertEqual(foreign.exception.code, 'unknown_outcome')
        self.answers[self.status_url] = (200, json.dumps({'status': 'COMPLETED'}).encode())
        self.answers[self.response_url] = (200, json.dumps({'video': {'url': 'https://evil.example/v.mp4'}, 'seed': 1,
                                                            'draft_id': DRAFT}).encode())
        with self.assertRaises(DomainError):
            self.adapter.get(RID, draft_request())
        self.answers[UPLOAD] = (200, json.dumps({'access_url': 'https://evil.example/ref.png'}).encode())
        self.calls.clear()
        self.assertEqual(self.adapter.submit(intent(draft_request(1)), self.refs[:1]).state, 'failed')
        self.assertEqual(self.paid(), [])

    def test_a_failed_upload_fails_the_take_without_a_paid_request(self):
        self.answers[UPLOAD] = TimeoutError('upload stalled')
        receipt = self.adapter.submit(intent(draft_request(1)), self.refs[:1])
        self.assertEqual((receipt.state, receipt.provider_status), ('failed', 'upload_failed'))
        self.assertEqual(self.paid(), [])

    def test_a_lost_submit_is_unknown_never_resent_or_listed(self):
        for answer in (TimeoutError('read timed out'), (502, b'bad gateway'), (200, b'not json')):
            self.answers['POST'] = answer
            with self.subTest(answer=answer), self.assertRaises(DomainError) as lost:
                self.adapter.submit(intent(draft_request(2)), self.refs)
            self.assertEqual(lost.exception.code, 'unknown_outcome')
        router = ProviderRouter({'fal_seedance_2_5': self.adapter})
        self.assertFalse(router.can_list('fal_seedance_2_5'))
        with self.assertRaises(DomainError):
            router.list_recent('fal_seedance_2_5')

    def test_a_refusal_or_failed_render_is_a_failed_take(self):
        self.answers['POST'] = (422, json.dumps({'detail': [{'msg': 'content policy'}]}).encode())
        refused = self.adapter.submit(intent(draft_request(2)), self.refs)
        self.assertEqual((refused.state, refused.settled_cost), ('failed', 0))
        self.answers[self.status_url] = (200, json.dumps({'status': 'COMPLETED'}).encode())
        self.answers[self.response_url] = (422, json.dumps({'detail': 'flagged'}).encode())
        receipt = self.adapter.get(RID, draft_request())
        self.assertEqual((receipt.state, receipt.result_url, receipt.settled_cost), ('failed', None, None))  # ran: the operator settles
        self.answers[self.response_url] = (503, b'{}')
        with self.assertRaises(DomainError) as later:
            self.adapter.get(RID, draft_request())
        self.assertEqual(later.exception.code, 'provider_failure')

    def test_settings_outside_the_pinned_capability_are_refused_before_sending(self):
        for request in (draft_request(quality='high'), draft_request(duration='4'), draft_request(duration=31),
                        draft_request(aspect_ratio='21:9'), draft_request(resolution='720p'),
                        {**draft_request(), 'job_type': 'fal_other'}):
            with self.subTest(request=request), self.assertRaises(DomainError):
                self.adapter.submit(intent(request), [])
        self.refs[0].path.write_bytes(b'changed')
        with self.assertRaises(DomainError):
            self.adapter.submit(intent(draft_request(1)), self.refs[:1])
        self.assertEqual(self.calls, [])

    def test_live_needs_enablement_and_its_key(self):
        live = FalSeedance(CAPABILITY, live_enabled=False)
        with self.assertRaises(DomainError):
            live.submit(intent(draft_request(), mode='live'), [])
        with self.assertRaises(ValueError):
            FalSeedance(CAPABILITY, transport=lambda *a: (200, b''), live_enabled=True)
        with self.assertRaises(ValueError):
            FalSeedance({'fal_seedance_2_5': {**CAPABILITY['fal_seedance_2_5'], 'endpoint': 'other/model'}})


    def test_an_empty_shot_is_stopped_in_plain_chinese_until_text_to_video_is_enabled(self):
        # zero images go to fal's text-to-video endpoint only behind the capability flag.
        for capability in (CAPABILITY, {**CAPABILITY, 'fal_seedance_2_5': {**CAPABILITY['fal_seedance_2_5'],
                                        'text_to_video': {'enabled': False, 'endpoint': TEXT_TO_VIDEO}}}):
            adapter = FalSeedance(capability, transport=lambda *a: self.fail('nothing may be sent'))
            with self.assertRaises(DomainError) as stopped:
                adapter._parameters(draft_request(0), has_references=False)
            self.assertEqual(stopped.exception.code, 'unsupported_route')
            self.assertIn('没有参考图', stopped.exception.message)
            with self.assertRaises(DomainError):
                adapter.submit(intent(draft_request(0)), [])
        self.assertEqual(self.paid(), [])

    def test_with_text_to_video_enabled_an_empty_shot_sends_one_text_request(self):
        enabled = {**CAPABILITY, 'fal_seedance_2_5': {**CAPABILITY['fal_seedance_2_5'],
                                                      'text_to_video': {'enabled': True, 'endpoint': TEXT_TO_VIDEO}}}
        self.adapter = FalSeedance(enabled, transport=self.adapter.transport, clock=lambda: 1_000_000)
        receipt = self.adapter.submit(intent(draft_request(0)), [])
        call, = self.paid()
        self.assertEqual(call['url'], QUEUE + 'bytedance/seedance-2.5/text-to-video')
        self.assertNotIn('image_urls', call['json'])
        self.assertFalse([c for c in self.calls if c['url'] in (UPLOAD, UPLOAD_TOKEN)])
        self.assertEqual(receipt.raw_receipt['endpoint'], TEXT_TO_VIDEO)
        # Turning the flag off later never strands a request already sent: polling does not re-check it.
        self.answers[self.status_url] = (202, json.dumps({'status': 'IN_PROGRESS'}).encode())
        self.assertEqual(FalSeedance(CAPABILITY, transport=self.adapter.transport).get(RID, draft_request(0)).state, 'running')
        with self.assertRaises(ValueError):
            FalSeedance({'fal_seedance_2_5': {**CAPABILITY['fal_seedance_2_5'],
                                              'text_to_video': {'enabled': True, 'endpoint': 'other/text-to-video'}}})

    def test_the_live_capability_has_text_to_video_on_at_its_fixed_endpoint(self):
        # Switched on for the live probe (owner 2026-09-27); before that it was off.
        from production.runtime_config import RuntimeConfig
        flag = RuntimeConfig.load().section('fal_video_capability')['text_to_video']
        self.assertEqual((flag['enabled'], flag['endpoint']), (True, TEXT_TO_VIDEO))

    def test_the_pinned_input_schema_hash_ignores_key_order_only(self):
        doc = {'components': {'schemas': {'Seedance25ReferenceToVideoInput': {'b': 1, 'a': {'enum': ['480p']}}}}}
        same = {'components': {'schemas': {'Seedance25ReferenceToVideoInput': {'a': {'enum': ['480p']}, 'b': 1}}}}
        other = {'components': {'schemas': {'Seedance25ReferenceToVideoInput': {'a': {'enum': ['720p']}, 'b': 1}}}}
        self.assertEqual(input_schema_hash(doc, 'fal_seedance_2_5'), input_schema_hash(same, 'fal_seedance_2_5'))
        self.assertNotEqual(input_schema_hash(doc, 'fal_seedance_2_5'), input_schema_hash(other, 'fal_seedance_2_5'))


if __name__ == '__main__':
    unittest.main()
