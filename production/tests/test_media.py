"""Oracle: SPEC immutable project media; upload/type/traversal boundaries."""
import hashlib
import io
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from production.contracts import DomainError
from production.media import (
    PROBE_MAX_STREAMS,
    PROBE_OUTPUT_BYTES,
    MediaStore,
    _probe_output,
)
from production.store import Store


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'db.sqlite')
        self.store.create_project('p1', {}, 'operator')
        self.store.create_project('p2', {}, 'operator')
        self.media = MediaStore(self.store, self.root / 'blobs', max_bytes=4096)
        out = io.BytesIO()
        Image.new('RGB', (32, 16), 'red').save(out, format='PNG')
        self.png = out.getvalue()

    def tearDown(self):
        self.temp.cleanup()

    def put(self, **kwargs):
        return self.media.put('p1', [self.png], 'image/png', 'agent', **kwargs)

    def test_roundtrip_metadata_and_hash(self):
        obj = self.put(logical_path='assets/face.png')
        self.assertEqual(obj['body']['probe']['width'], 32)
        self.assertEqual(obj['body']['probe']['height'], 16)
        self.assertEqual(obj['body']['sha256'], hashlib.sha256(self.png).hexdigest())
        self.assertEqual(self.media.read('p1', obj['object_id']), self.png)
        with self.assertRaises(DomainError):
            self.media.read('p2', obj['object_id'])

    def test_invalid_type_and_size_never_publish(self):
        for chunks, kind in [([b'fake'], 'image/png'), ([self.png], 'image/jpeg'), ([b'x'*4097], 'text/plain')]:
            with self.assertRaises(DomainError):
                self.media.put('p1', chunks, kind, 'agent')
        self.assertEqual(self.store.list_objects('p1', kind='media'), [])
        self.assertEqual(list((self.root / 'blobs' / 'staging').iterdir()), [])

    def test_declared_hash_and_length_are_verified(self):
        with self.assertRaises(DomainError):
            self.put(expected_size=1)
        with self.assertRaises(DomainError):
            self.put(expected_hash='0'*64)
        obj = self.put(expected_size=len(self.png), expected_hash=hashlib.sha256(self.png).hexdigest())
        self.assertEqual(obj['body']['size'], len(self.png))

    def test_interrupted_upload_and_path_never_publish(self):
        def broken():
            yield self.png[:10]
            raise OSError('interrupted')
        with self.assertRaises(OSError):
            self.media.put('p1', broken(), 'image/png', 'agent')
        with self.assertRaises(ValueError):
            self.put(logical_path='../secret.png')
        self.assertEqual(self.store.list_objects('p1', kind='media'), [])
        self.assertEqual(list((self.root / 'blobs' / 'staging').iterdir()), [])

    def test_dedup_and_integrity(self):
        a, b = self.put(), self.put()
        self.assertNotEqual(a['object_id'], b['object_id'])
        self.assertEqual(self.media.path_for('p1', a['object_id']), self.media.path_for('p1', b['object_id']))
        path = self.media.path_for('p1', a['object_id'])
        path.chmod(0o600)
        path.write_bytes(b'corrupt')
        with self.assertRaises(DomainError):
            self.media.read('p1', a['object_id'])

    def test_symlink_blob_is_rejected(self):
        digest = hashlib.sha256(self.png).hexdigest()
        prefix = self.root / 'blobs' / 'objects' / digest[:2]
        prefix.mkdir()
        outside = self.root / 'outside'
        outside.write_bytes(b'secret')
        (prefix / digest).symlink_to(outside)
        with self.assertRaises(DomainError):
            self.put()
        self.assertEqual(outside.read_bytes(), b'secret')

    def test_derivative_preserves_exact_source_revision(self):
        a = self.put()
        b = self.put(derivative_of={'object_id': a['object_id'], 'revision': 1, 'digest': a['digest']})
        self.assertEqual(b['body']['derivative_of']['digest'], a['digest'])
        with self.assertRaises(DomainError):
            self.put(derivative_of={'object_id': a['object_id'], 'revision': 1, 'digest': '0'*64})

    def test_upload_idempotency(self):
        first = self.put(idempotency_key='same-upload')
        second = self.put(idempotency_key='same-upload')
        self.assertEqual(first, second)
        with self.assertRaises(DomainError):
            self.put(idempotency_key='same-upload', logical_path='changed/path')
        self.assertEqual(len(self.store.list_objects('p1', kind='media')), 1)

    def test_publication_rechecks_authority_after_streaming(self):
        authorized = True
        def chunks():
            nonlocal authorized
            yield self.png
            authorized = False
        def publish(metadata):
            self.assertEqual(metadata['probe']['width'], 32)
            if not authorized:
                raise DomainError('forbidden', 'Permission revoked during upload')
            return self.store.create_object('p1', 'media', metadata, 'agent')
        with self.assertRaises(DomainError) as error:
            self.media.put('p1', chunks(), 'image/png', 'agent', publish=publish)
        self.assertEqual(error.exception.code, 'forbidden')
        self.assertEqual(self.store.list_objects('p1', kind='media'), [])
        self.assertEqual(list((self.root / 'blobs' / 'staging').iterdir()), [])

    def test_real_av_container_is_probed(self):
        target = self.root / 'generated.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'color=c=blue:s=64x48:r=12', '-f', 'lavfi', '-i',
                        'sine=frequency=440:sample_rate=8000', '-t', '0.5',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(target)], check=True)
        media = MediaStore(self.store, self.root / 'video-blobs', max_bytes=65536)
        obj = media.put('p1', [target.read_bytes()], 'video/mp4', 'agent')
        probe = obj['body']['probe']
        self.assertEqual((probe['width'], probe['height']), (64, 48))
        self.assertTrue(probe['has_audio'])
        self.assertAlmostEqual(probe['duration'], 0.5, delta=0.1)
        with self.assertRaises(DomainError):
            media.put('p1', [target.read_bytes()], 'audio/wav', 'agent')

    def video_fixture(self, name, *, sar="1:1", audio_seconds="2"):
        target = self.root / name
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                        'color=c=blue:s=64x48:r=12:d=0.5', '-f', 'lavfi', '-i',
                        'sine=frequency=440:sample_rate=8000:duration='+audio_seconds,
                        '-vf', 'setsar='+sar.replace(':', '/'), '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                        '-c:a', 'aac', str(target)], check=True, capture_output=True, timeout=20)
        return target

    def test_short_video_stream_does_not_inherit_long_audio_duration(self):
        target = self.video_fixture('short-video.mp4')
        probe = self.media._probe(target, 'video/mp4')
        self.assertAlmostEqual(probe['duration'], 2.0, delta=0.1)
        self.assertEqual(probe['duration'], probe['container_duration'])
        self.assertAlmostEqual(probe['video_duration'], 0.5, delta=0.02)
        self.assertEqual(probe['sample_aspect_ratio'], '1:1')
        self.assertEqual(probe['rotation_degrees'], 0)
        self.assertEqual(probe['rotation_source'], 'absent-identity')
        self.assertEqual((probe['display_width'], probe['display_height']), (64, 48))

    def test_real_anamorphic_display_geometry_and_rotation_are_preserved(self):
        original = self.video_fixture('anamorphic.mp4', sar='2:1')
        probe = self.media._probe(original, 'video/mp4')
        self.assertEqual((probe['width'], probe['height']), (64, 48))
        self.assertEqual(probe['sample_aspect_ratio'], '2:1')
        self.assertEqual((probe['display_width'], probe['display_height']), (128, 48))
        rotated = self.root / 'rotated.mp4'
        help_text = subprocess.run(['ffmpeg', '-h', 'full'], capture_output=True, text=True, timeout=20, check=True).stdout
        before = ['-display_rotation', '90'] if '-display_rotation' in help_text else []
        after = [] if before else ['-metadata:s:v:0', 'rotate=90']
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', *before, '-i', str(original),
                        '-c', 'copy', *after, str(rotated)],
                       check=True, capture_output=True, timeout=20)
        actual = self.media._probe(rotated, 'video/mp4')
        self.assertEqual(abs(actual['rotation_degrees']), 90)
        self.assertEqual(actual['rotation_source'], 'display-matrix')
        self.assertEqual((actual['display_width'], actual['display_height']), (48, 128))
        self.assertAlmostEqual(actual['video_duration'], 0.5, delta=0.02)

    def test_missing_or_invalid_stream_measurements_remain_unknown(self):
        base = {'format': {'format_name': 'mov', 'duration': '20'},
                'streams': [{'codec_type': 'video', 'width': 64, 'height': 48}]}
        for changes in ({'duration': 'N/A', 'sample_aspect_ratio': '0:1'},
                        {'duration': 'NaN', 'sample_aspect_ratio': '1:0'},
                        {'duration': '-1', 'sample_aspect_ratio': '999999999999:1'},
                        {'duration': 'Infinity', 'sample_aspect_ratio': 'broken'}):
            with self.subTest(changes=changes):
                fixture = {**base, 'streams': [{**base['streams'][0], **changes}]}
                with patch('production.media._probe_output', return_value=json.dumps(fixture).encode()):
                    actual = self.media._probe(self.root / 'unused', 'video/mp4')
                self.assertEqual(actual['container_duration'], 20)
                self.assertIsNone(actual['video_duration'])
                self.assertIsNone(actual['sample_aspect_ratio'])
                self.assertIsNone(actual['display_width'])
                self.assertIsNone(actual['display_height'])

    def test_absent_sar_has_explicit_square_pixel_playback_provenance(self):
        for extra in ({}, {'sample_aspect_ratio': 'N/A'}):
            fixture = {'format': {'format_name': 'mov', 'duration': '5.05'},
                       'streams': [{'codec_type': 'video', 'width': 1280, 'height': 720,
                                    'duration': '5.041667', **extra}]}
            with self.subTest(extra=extra), patch('production.media._probe_output', return_value=json.dumps(fixture).encode()):
                probe = self.media._probe(self.root / 'unused', 'video/mp4')
            self.assertEqual(probe['sample_aspect_ratio'], '1:1')
            self.assertEqual(probe['sample_aspect_ratio_source'], 'playback-default-square')
            self.assertEqual((probe['display_width'], probe['display_height']), (1280, 720))
            self.assertEqual(probe['display_geometry_status'], 'verified')
        # Missing duration never borrows the container or requested duration.
        del fixture['streams'][0]['duration']
        with patch('production.media._probe_output', return_value=json.dumps(fixture).encode()):
            self.assertIsNone(self.media._probe(self.root / 'unused', 'video/mp4')['video_duration'])

    def test_real_h264_without_sar_uses_playback_default_without_changing_bytes(self):
        target = self.video_fixture('unspecified-sar.mp4', sar='0:1')
        before = target.read_bytes()
        raw = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                              '-show_entries', 'stream=sample_aspect_ratio', '-of', 'json', str(target)],
                             check=True, capture_output=True, timeout=20)
        self.assertNotIn('sample_aspect_ratio', json.loads(raw.stdout)['streams'][0])
        probe = self.media._probe(target, 'video/mp4')
        self.assertEqual((probe['display_width'], probe['display_height']), (64, 48))
        self.assertEqual(probe['sample_aspect_ratio_source'], 'playback-default-square')
        self.assertEqual(target.read_bytes(), before)

    def test_stream_ticks_are_a_separate_timing_source_not_container_fallback(self):
        fixture = {'format': {'format_name': 'mov', 'duration': '20'},
                   'streams': [{'codec_type': 'video', 'width': 64, 'height': 48,
                                'duration_ts': 6000, 'time_base': '1/12000', 'sample_aspect_ratio': '1:1'}]}
        with patch('production.media._probe_output', return_value=json.dumps(fixture).encode()):
            actual = self.media._probe(self.root / 'unused', 'video/mp4')
        self.assertEqual(actual['video_duration'], 0.5)
        self.assertEqual(actual['video_duration_source'], 'stream-ticks')
        fixture['streams'][0]['time_base'] = '1/0'
        with patch('production.media._probe_output', return_value=json.dumps(fixture).encode()):
            actual = self.media._probe(self.root / 'unused', 'video/mp4')
        self.assertIsNone(actual['video_duration'])
        self.assertEqual(actual['video_duration_source'], 'unknown')

    def test_conflicting_and_non_rotation_matrices_never_become_identity(self):
        matrix = ('\n00000000: 0 -65536 0\n00000001: 65536 0 0\n00000002: 0 0 1073741824\n')
        variants = [
            {'tags': {'rotate': '180'}, 'side_data_list': [{'side_data_type': 'Display Matrix', 'rotation': 90, 'displaymatrix': matrix}]},
            {'side_data_list': [{'side_data_type': 'Display Matrix', 'rotation': 90, 'displaymatrix': matrix.replace('-65536', '-32768')}]},
            {'side_data_list': [{'side_data_type': 'Display Matrix', 'rotation': 90}]},
            {'side_data_list': [{'side_data_type': 'Display Matrix', 'rotation': 0, 'displaymatrix': matrix.replace('1073741824', '0')}]},
            {'tags': {'rotate': 'NaN'}},
            {'tags': {'rotate': '45'}},
        ]
        for transform in variants:
            fixture = {'format': {'format_name': 'mov', 'duration': '1'},
                       'streams': [{'codec_type': 'video', 'width': 64, 'height': 48,
                                    'sample_aspect_ratio': '1:1', 'duration': '1', **transform}]}
            with self.subTest(transform=transform), patch('production.media._probe_output', return_value=json.dumps(fixture).encode()):
                actual = self.media._probe(self.root / 'unused', 'video/mp4')
            self.assertIsNone(actual['rotation_degrees'])
            self.assertNotEqual(actual['rotation_source'], 'absent-identity')
            self.assertIsNone(actual['display_width'])
            self.assertEqual(actual['display_geometry_status'], 'unknown')

    def test_non_media_object_cannot_be_served(self):
        with self.assertRaises(DomainError):
            self.media.read('p1', 'p1')

    def test_large_wav_comment_is_not_emitted_by_real_probe(self):
        def chunk(tag, data):
            return tag + struct.pack('<I', len(data)) + data + (b'\0' if len(data) % 2 else b'')
        comment = b'Untrusted large comment\n' * 45000 + b'\0'
        body = (b'WAVE' + chunk(b'fmt ', struct.pack('<HHIIHH', 1, 1, 8000, 16000, 2, 16))
                + chunk(b'LIST', b'INFO' + chunk(b'ICMT', comment)) + chunk(b'data', b'\0' * 1600))
        wav = b'RIFF' + struct.pack('<I', len(body)) + body
        media = MediaStore(self.store, self.root / 'wav-blobs', max_bytes=2_000_000)
        outputs = []
        def inspect(command):
            output = _probe_output(command)
            outputs.append(output)
            return output
        with patch('production.media._probe_output', side_effect=inspect):
            obj = media.put('p1', [wav], 'audio/wav', 'agent')
        self.assertTrue(obj['body']['probe']['has_audio'])
        self.assertLess(len(outputs[0]), 2048)
        self.assertNotIn(b'Untrusted large comment', outputs[0])
        self.assertNotIn(b'"tags"', outputs[0])

    def test_excessive_probe_output_is_killed_without_publication(self):
        real_popen = subprocess.Popen
        # Actual child and pipe draining, not a fake oversized CompletedProcess.
        def producer(_command, **kwargs):
            return real_popen([sys.executable, '-c',
                               f'import sys; sys.stdout.buffer.write(b"x" * {PROBE_OUTPUT_BYTES * 4})'],
                              **kwargs)
        with patch('production.media.subprocess.Popen', side_effect=producer), self.assertRaises(DomainError) as error:
            self.media.put('p1', [b'probe-fixture'], 'audio/wav', 'agent')
        self.assertEqual(error.exception.code, 'invalid_media')
        self.assertEqual(self.store.list_objects('p1', kind='media'), [])
        self.assertEqual(list((self.root / 'blobs' / 'staging').iterdir()), [])

    def test_excessive_stream_count_never_publishes(self):
        probe = {'format': {'format_name': 'wav', 'duration': '1'},
                 'streams': [{'codec_type': 'audio'}] * (PROBE_MAX_STREAMS + 1)}
        with patch('production.media._probe_output', return_value=json.dumps(probe).encode()), self.assertRaises(DomainError):
            self.media.put('p1', [b'probe-fixture'], 'audio/wav', 'agent')
        self.assertEqual(self.store.list_objects('p1', kind='media'), [])


if __name__ == '__main__':
    unittest.main()
