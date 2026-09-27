"""The rehearsal fakes answer the real adapters exactly; run with the production venv."""
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from production.provider_apilio import ApilioImages
from production.provider_fal import FalSeedance
from production.provider_types import ResolvedReference
from tools.rehearsal.fakes import FakeApilio, FakeFal, MediaServer

FAL = {'fal_seedance_2_5': {'job_type': 'fal_seedance_2_5', 'type': 'video', 'endpoint': 'bytedance/seedance-2.5/reference-to-video',
                            'max_references': 9, 'usd_micros_per_second': {'480p': 207741},
                            'params': [{'name': 'prompt', 'type': 'string'}, {'name': 'duration', 'type': 'integer'},
                                       {'name': 'resolution', 'type': 'string'}, {'name': 'aspect_ratio', 'type': 'string'},
                                       {'name': 'generate_audio', 'type': 'boolean'}, {'name': 'draft', 'type': 'boolean'}]},
       'fal_seedance_2_5_complete': {'job_type': 'fal_seedance_2_5_complete', 'type': 'video',
                                     'endpoint': 'bytedance/seedance-2.5/draft/complete', 'max_references': 0,
                                     'usd_micros_per_second': {'1080p': 1149656},
                                     'params': [{'name': 'draft_id', 'type': 'string'}, {'name': 'resolution', 'type': 'string'},
                                                {'name': 'duration', 'type': 'integer'}, {'name': 'aspect_ratio', 'type': 'string'},
                                                {'name': 'generate_audio', 'type': 'boolean'}]}}
APILIO = {'apilio_gpt_image_2_5': {'job_type': 'apilio_gpt_image_2_5', 'type': 'image', 'models': {'2k': 'm'},
                                   'sizes': {'16:9|2k': '2048x1152'}, 'quota': {'model_ratio': 2.5, 'completion_ratio': 6}}}


def probe(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type,width,height', '-of', 'json', str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)['streams']


class FakeProviderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.media = MediaServer(self.root / 'media')

    def fetch(self, url):
        with self.media.serve(url, '', '', 5) as response:
            path = self.root / url.rsplit('/', 1)[-1]
            path.write_bytes(b''.join(response.chunks))
            return response.media_type, path

    def test_a_draft_then_its_completion_run_through_the_real_fal_adapter(self):
        log = self.root / 'fal-requests.jsonl'
        fal = FalSeedance(FAL, transport=FakeFal(self.media, log=log), timeout=5)
        draft = {'job_type': 'fal_seedance_2_5', 'references': [{'object_id': 'cart', 'revision': 1}],
                 'params': {'prompt': 'A cart passes.', 'duration': 4,
                 'resolution': '480p', 'aspect_ratio': '16:9', 'generate_audio': True, 'draft': True}}
        # A shot sends at least one reference image (text-to-video stays off until the probe).
        image = self.root / 'cart.png'
        image.write_bytes(b'\x89PNG fake cart')
        cart = ResolvedReference({'object_id': 'cart', 'revision': 1}, image, hashlib.sha256(image.read_bytes()).hexdigest(), 'image/png')
        sent = fal.submit({'operation': 'submit', 'cost': {'mode': 'fake'}, 'request': draft}, [cart])
        self.assertEqual(fal.get(sent.job_id, draft).state, 'running')
        done = fal.get(sent.job_id, draft)
        self.assertEqual((done.state, done.settled_cost), ('succeeded', 4 * 207741))
        media_type, path = self.fetch(done.result_url)
        self.assertEqual(media_type, 'video/mp4')
        # The rehearsal keeps every body fal received: exactly what the adapter sent.
        sent_bodies = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([b['url'].rsplit('/', 1)[-1] for b in sent_bodies], ['reference-to-video'])
        self.assertEqual(sent_bodies[0]['body']['prompt'], 'A cart passes.')
        self.assertEqual({(s['codec_type'], s.get('width'), s.get('height')) for s in probe(path)},
                         {('video', 854, 480), ('audio', None, None)})
        complete = {'job_type': 'fal_seedance_2_5_complete', 'references': [], 'params': {'draft_id': done.raw_receipt['draft_id'],
                    'resolution': '1080p', 'duration': 4, 'aspect_ratio': '16:9', 'generate_audio': True}}
        fal = FalSeedance(FAL, transport=FakeFal(self.media), timeout=5)  # a restarted worker still completes the draft
        sent = fal.submit({'operation': 'complete-draft', 'cost': {'mode': 'fake'}, 'request': complete}, [])
        fal.get(sent.job_id, complete)
        full = fal.get(sent.job_id, complete)
        self.assertEqual((full.state, full.raw_receipt['seed']), ('succeeded', done.raw_receipt['seed']))
        self.assertIn(('video', 1920, 1080), {(s['codec_type'], s.get('width'), s.get('height')) for s in probe(self.fetch(full.result_url)[1])})

    def test_an_image_runs_through_the_real_apilio_adapter(self):
        apilio = ApilioImages(APILIO, transport=FakeApilio(self.media))
        request = {'job_type': 'apilio_gpt_image_2_5', 'params': {'prompt': 'A grey sheet.', 'aspect_ratio': '16:9', 'resolution': '2k'}}
        receipt = apilio.submit({'operation': 'submit', 'cost': {'mode': 'fake'}, 'request': request}, [])
        self.assertEqual((receipt.state, receipt.settled_cost), ('succeeded', 16662))
        media_type, path = self.fetch(receipt.result_url)
        self.assertEqual((media_type, probe(path)[0]['width'], probe(path)[0]['height']), ('image/png', 2048, 1152))

    def test_the_fake_reader_answers_every_question_in_the_envelope_the_reader_checks(self):
        from production import reader
        from tools.rehearsal.fakes import fake_reader
        wire = json.dumps({'contents': [{'parts': [{'text': 'Observe. Questions: ' + json.dumps(['Who moves?', 'Which way?'])}]}]}).encode()
        answer = fake_reader(reader.ENDPOINT, wire, {}, 5, 1 << 20, model=reader.MODEL)
        envelope = json.loads(answer.body)
        self.assertEqual((answer.status_code, envelope['modelVersion']), (200, reader.MODEL))
        output = reader.Observations.model_validate(reader._observation_json(envelope['candidates'][0]['content']['parts'][0]['text']))
        self.assertEqual([a.question_index for a in output.answers], [0, 1])

    def test_the_media_server_serves_only_what_it_published(self):
        with self.assertRaises(KeyError), self.media.serve('https://v3b.fal.media/other.mp4', '', '', 5):
            pass


if __name__ == '__main__':
    unittest.main()
