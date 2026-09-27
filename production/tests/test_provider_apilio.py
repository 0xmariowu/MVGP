"""apilio image adapter: no key in fake mode, exact references, never resent."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from production.contracts import DomainError
from production.provider_apilio import ENDPOINTS, ApilioImages
from production.provider_types import ResolvedReference
from production.provider_router import ProviderRouter

CAPABILITY = {'apilio_gpt_image_2_5': {'job_type': 'apilio_gpt_image_2_5', 'type': 'image',
                                       'models': {'2k': 'gpt-image-2.5-sunburst-2k'}, 'sizes': {'16:9|2k': '2048x1152'},
                                       'quota': {'model_ratio': 2.5, 'completion_ratio': 6}}}


def intent(**params):
    return {'operation': 'submit', 'cost': {'mode': 'fake'},
            'request': {'job_type': 'apilio_gpt_image_2_5',
                        'params': {'prompt': 'A grey character sheet of one woman.', 'aspect_ratio': '16:9', 'resolution': '2k', **params}}}


class ApilioImageTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.answer = (200, json.dumps({'data': [{'url': 'https://webstatic.aiproxy.vip/out/a.png'}], 'model': 'm',
                                        'usage': {'total_tokens': 1}}).encode())
        def transport(method, url, headers, json_body, form, files, timeout):
            self.calls.append({'url': url, 'json': json_body, 'form': form, 'files': files, 'auth': headers.get('Authorization')})
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer
        self.adapter = ApilioImages(CAPABILITY, transport=transport)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.refs = []
        for n in range(2):
            path = Path(tmp.name) / f'r{n}.png'
            path.write_bytes(b'\x89PNG fake ' + bytes([n]))
            self.refs.append(ResolvedReference({'object_id': f'o{n}', 'revision': 1}, path, hashlib.sha256(path.read_bytes()).hexdigest(), 'image/png'))

    def test_generation_without_references_is_json_with_the_pinned_model_and_size(self):
        receipt = self.adapter.submit(intent(), [])
        call, = self.calls
        self.assertEqual(call['url'], ENDPOINTS['generations'])
        self.assertEqual((call['json']['model'], call['json']['size']), ('gpt-image-2.5-sunburst-2k', '2048x1152'))
        self.assertEqual(call['auth'], 'Bearer fake')  # fake mode never reads the real key
        self.assertEqual((receipt.state, receipt.result_url), ('succeeded', 'https://webstatic.aiproxy.vip/out/a.png'))
        self.assertNotIn('url', json.dumps(receipt.raw_receipt).replace('result_host', ''))

    def test_the_image_settles_its_quota_from_the_answers_own_token_counts(self):
        self.answer = (200, json.dumps({'data': [{'url': 'https://webstatic.aiproxy.vip/out/a.png'}],
                                        'usage': {'input_tokens': 35, 'output_tokens': 1105}}).encode())
        self.assertEqual(self.adapter.submit(intent(), []).settled_cost, 16662)  # (35 + 1105 x 6) x 2.5, rounded half-even
        self.answer = (200, json.dumps({'data': [{'url': 'https://webstatic.aiproxy.vip/out/a.png'}], 'usage': {}}).encode())
        self.assertIsNone(self.adapter.submit(intent(), []).settled_cost)  # no counts: the operator settles

    def test_edit_sends_each_reference_in_order_as_a_repeated_image_field(self):
        self.adapter.submit(intent(), self.refs)
        call, = self.calls
        self.assertEqual(call['url'], ENDPOINTS['edits'])
        self.assertEqual([f[0] for f in call['files']], ['image', 'image'])
        self.assertEqual([f[1][1] for f in call['files']], [r.path.read_bytes() for r in self.refs])

    def test_changed_reference_bytes_or_unknown_settings_are_refused_before_sending(self):
        self.refs[0].path.write_bytes(b'changed')
        with self.assertRaises(DomainError):
            self.adapter.submit(intent(), self.refs)
        with self.assertRaises(DomainError):
            self.adapter.submit(intent(quality='low'), [])
        with self.assertRaises(DomainError):
            self.adapter.submit(intent(aspect_ratio='1:1'), [])
        self.assertEqual(self.calls, [])

    def test_a_lost_answer_is_unknown_never_resent_or_listed(self):
        self.answer = TimeoutError('read timed out')
        with self.assertRaises(DomainError) as lost:
            self.adapter.submit(intent(), [])
        self.assertEqual(lost.exception.code, 'unknown_outcome')
        self.answer = (502, b'bad gateway')
        with self.assertRaises(DomainError) as gateway:
            self.adapter.submit(intent(), [])
        self.assertEqual(gateway.exception.code, 'unknown_outcome')
        with self.assertRaises(DomainError):
            self.adapter.get('apilio-x', intent()['request'])
        router = ProviderRouter({'apilio_gpt_image_2_5': self.adapter})
        self.assertFalse(router.can_list('apilio_gpt_image_2_5'))
        with self.assertRaises(DomainError):
            router.list_recent('apilio_gpt_image_2_5')

    def test_a_refusal_is_a_failed_take_not_an_unknown_one(self):
        self.answer = (400, json.dumps({'error': {'message': 'content policy'}}).encode())
        receipt = self.adapter.submit(intent(), [])
        self.assertEqual((receipt.state, receipt.result_url), ('failed', None))

    def test_live_needs_enablement_and_its_key(self):
        live = ApilioImages(CAPABILITY, live_enabled=False)
        with self.assertRaises(DomainError):
            live.submit({**intent(), 'cost': {'mode': 'live'}}, [])
        with self.assertRaises(ValueError):
            ApilioImages(CAPABILITY, transport=lambda *a: (200, b''), live_enabled=True)


if __name__ == '__main__':
    unittest.main()
