"""real source frames, exact VFR time mapping, finite local extraction."""
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from production.contracts import DomainError, ObjectRef
from production.frame_evidence import FrameEvidence, FramePolicy, _run
from production.media import MediaStore
from production.store import Store


class FrameEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        root = Path(cls.tmp.name)
        cls.videos = {}
        for name, frames, audio, offset in [('vfr', 8, True, 7), ('tiny', 2, False, 0), ('one', 1, False, 0)]:
            path = root / f'{name}.mp4'
            argv = ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=120x80:rate=10:duration=2']
            if audio:
                argv += ['-f', 'lavfi', '-i', 'sine=frequency=440:duration=2']
            argv += ['-vf', 'setpts=if(lt(N\\,3)\\,N\\,3+(N-3)*3)', '-frames:v', str(frames),
                     '-fps_mode', 'vfr', '-c:v', 'libx264', '-pix_fmt', 'yuv420p']
            argv += ['-c:a', 'aac', '-shortest'] if audio else ['-an']
            argv += ['-output_ts_offset', str(offset), str(path)]
            subprocess.run(argv, check=True, timeout=15)
            cls.videos[name] = path.read_bytes()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root/'store.sqlite')
        self.store.create_project('p1', {}, 'operator')
        self.store.create_project('p2', {}, 'operator')
        self.media = MediaStore(self.store, self.root/'media')
        self.policy = FramePolicy()
        self.extractor = FrameEvidence(self.media, policy=self.policy)

    def put(self, name='vfr'):
        obj = self.media.put('p1', [self.videos[name]], 'video/mp4', 'upload_service')
        return ObjectRef(object_id=obj['object_id'], revision=obj['revision'], digest=obj['digest'])

    def test_vfr_actual_first_last_and_source_relative_pts(self):
        ref = self.put()
        frames = self.extractor.extract('p1', ref)
        self.assertEqual([f.frame_index for f in frames], [0, 1, 2, 4, 5, 7])
        self.assertGreater(frames[0].source_seconds, 0)  # Audio begins before video.
        for frame, delta in zip(frames, [0, .1, .2, .6, .9, 1.5], strict=True):
            self.assertAlmostEqual(frame.source_seconds, 7-frame.timestamp_origin_seconds+delta, places=6)
        self.assertEqual(frames[0].timestamp_origin, 'format.start_time')
        self.assertEqual(frames[0].source_pts * frames[0].time_base_numerator / frames[0].time_base_denominator, 7)
        self.assertTrue(all(f.source_sha256 == hashlib.sha256(self.videos['vfr']).hexdigest() for f in frames))
        self.assertTrue(all(f.sampled and not f.exhaustive for f in frames))
        for frame in frames:
            with Image.open(io.BytesIO(frame.png)) as image:
                self.assertEqual(image.format, 'PNG')
                self.assertEqual(image.size, (120, 80))
                self.assertGreater(len(image.getcolors(20000)), 5)
        # A separate exact frame extraction checks last-frame identity, not just count.
        original = self.media.path_for('p1', ref.object_id)
        expected = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(original), '-vf', 'select=eq(n\\,7)',
            '-fps_mode', 'passthrough', '-frames:v', '1', '-f', 'image2pipe', '-c:v', 'png', '-'],
            check=True, capture_output=True, timeout=15).stdout
        self.assertEqual(Image.open(io.BytesIO(expected)).tobytes(), Image.open(io.BytesIO(frames[-1].png)).tobytes())

    def test_tiny_sources_no_duplicate_or_invented_frames(self):
        for name, count in [('tiny', 2), ('one', 1)]:
            ref = self.put(name)
            result = self.extractor.extract('p1', ref)
            self.assertEqual([f.frame_index for f in result], list(range(count)))
            self.assertEqual(result[0].source_seconds, 0)

    def test_source_audio_and_bytes_unchanged_and_no_publication(self):
        for name, audio in [('vfr', True), ('tiny', False)]:
            ref = self.put(name)
            before = self.store.list_objects('p1')
            self.extractor.extract('p1', ref)
            self.assertEqual(self.media.read('p1', ref.object_id), self.videos[name])
            self.assertEqual(self.store.list_objects('p1'), before)
            self.assertEqual(self.store.get_object('p1', ref.object_id)['body']['probe']['has_audio'], audio)

    def test_resize_is_bounded_and_never_upscales(self):
        result = FrameEvidence(self.media, policy=replace(self.policy, max_width=60)).extract('p1', self.put())
        self.assertTrue(all((f.width, f.height) == (60, 40) for f in result))

    def test_wrong_project_digest_missing_digest_and_nonvideo_fail_before_decode(self):
        ref = self.put()
        text = self.media.put('p1', [b'not video'], 'text/plain', 'upload_service')
        cases = [('p2', ref), ('p1', ref.model_copy(update={'digest': '0'*64})),
                 ('p1', ref.model_copy(update={'digest': None})),
                 ('p1', ObjectRef(object_id=text['object_id'], revision=1, digest=text['digest']))]
        with patch('production.frame_evidence._run') as run:
            for pid, item in cases:
                with self.assertRaises(DomainError):
                    self.extractor.extract(pid, item)
            run.assert_not_called()

    def test_corrupt_source_rejected_before_decode(self):
        ref = self.put()
        path = self.media.path_for('p1', ref.object_id)
        path.chmod(0o600)
        path.write_bytes(b'x'*len(self.videos['vfr']))
        with patch('production.frame_evidence._run') as run:
            with self.assertRaises(DomainError):
                self.extractor.extract('p1', ref)
            run.assert_not_called()

    def test_finite_source_frame_pixel_and_output_limits(self):
        ref = self.put()
        for overrides in [{'max_source_bytes': 10}, {'max_source_pixels': 100},
                          {'max_decoded_frames': 3}, {'max_probe_bytes': 20},
                          {'max_png_bytes': 20}, {'max_total_png_bytes': 20}]:
            with self.subTest(overrides=overrides), self.assertRaises(DomainError):
                FrameEvidence(self.media, policy=replace(self.policy, **overrides)).extract('p1', ref)

    def test_policy_and_binary_inputs_cannot_supply_filters_or_urls(self):
        for overrides in [{'max_frames': 7}, {'max_frames': 1}, {'max_frames': True},
                          {'timeout_seconds': float('inf')}, {'max_width': 0}, {'max_probe_bytes': 2**40}]:
            with self.assertRaises(ValueError):
                replace(self.policy, **overrides)
        for binary in ['https://bad/ffmpeg', 'relative/ffmpeg', '/missing/ffmpeg']:
            with self.assertRaises(ValueError):
                FrameEvidence(self.media, policy=self.policy, ffmpeg=Path(binary))

    def test_malformed_or_missing_timestamps_never_invent_coverage(self):
        ref = self.put()
        for probe in [b'not json', b'{}', b'[]', b'{"streams":[null],"frames":[]}', json.dumps({'streams': [{'time_base': '1/10'}],
                        'frames': [{'width': 120, 'height': 80}]}).encode()]:
            with patch('production.frame_evidence._run', return_value=probe), self.assertRaises(DomainError):
                self.extractor.extract('p1', ref)

    def test_portrait_extreme_ratio_respects_height_cap(self):
        path = self.root/'portrait.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=32x1200:r=1:d=1',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)], check=True, timeout=15)
        obj = self.media.put('p1', [path.read_bytes()], 'video/mp4', 'upload_service')
        ref = ObjectRef(object_id=obj['object_id'], revision=1, digest=obj['digest'])
        frame = self.extractor.extract('p1', ref)[0]
        self.assertEqual(frame.height, 960)
        self.assertLess(frame.width, 32)

    def test_png_integrity_count_and_decoded_pts_range(self):
        ref = self.put('tiny')
        png = self.extractor.extract('p1', ref)[0].png
        for raw, expected in [(png[:-1], 1), (png, 2), (png+png, 1), (b'not-png', 1)]:
            with self.assertRaises(DomainError):
                self.extractor._pngs(raw, expected)
        for values in [[0, -1], [0, 0], [0, 99999]]:
            raw = json.dumps({'streams': [{'time_base': '1/10'}], 'format': {'start_time': '0'},
                'frames': [{'width': 120, 'height': 80, 'pts': value} for value in values]}).encode()
            with patch('production.frame_evidence._run', return_value=raw), self.assertRaises(DomainError):
                self.extractor.extract('p1', ref)

    def test_runner_timeout_and_stdout_stderr_caps_are_bounded(self):
        started = time.monotonic()
        with self.assertRaises(DomainError):
            _run([sys.executable, '-c', 'import time; time.sleep(3)'], deadline=time.monotonic()+.05, limit=100)
        self.assertLess(time.monotonic()-started, 2)
        for code in ['print("x"*10000)', 'import sys; sys.stderr.write("x"*20000)']:
            with self.assertRaises(DomainError):
                _run([sys.executable, '-c', code], deadline=time.monotonic()+2, limit=100)
