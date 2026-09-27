"""Finite local sampling of verified video bytes; never exhaustive comprehension.

This service-only extractor takes an exact MediaStore reference, not an author
path/filter. It publishes nothing and makes no network/model calls. Timestamps
are decoded PTS relative to the container playback origin, not frame-number/FPS
estimates. Original-speed video remains the primary observation input.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, fields
from fractions import Fraction
from pathlib import Path
from typing import IO, Any

from PIL import Image

from production.contracts import DomainError, ObjectRef
from production.media import MediaStore


@dataclass(frozen=True)
class FramePolicy:
    """Explicit LOCAL resource limits, not model/cinematography recommendations."""
    max_frames: int = 6
    max_width: int = 960
    max_height: int = 960
    max_source_bytes: int = 536870912
    max_source_pixels: int = 40000000
    max_decoded_frames: int = 30000
    max_probe_bytes: int = 8388608
    max_png_bytes: int = 4194304
    max_total_png_bytes: int = 25165824
    timeout_seconds: int = 60

    def __post_init__(self) -> None:
        ceilings = {'max_frames': 6, 'max_width': 1920, 'max_height': 1920,
                        'max_source_bytes': 1073741824, 'max_source_pixels': 40000000,
                        'max_decoded_frames': 60000, 'max_probe_bytes': 16777216,
                        'max_png_bytes': 16777216, 'max_total_png_bytes': 67108864,
                        'timeout_seconds': 120}
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or not 1 <= value <= ceilings[field.name]:
                raise ValueError('Frame policy requires finite bounded positive integers')
        if self.max_frames < 2:
            raise ValueError('Sampling must permit both source endpoints')


@dataclass(frozen=True)
class FrameSample:
    png: bytes
    source_seconds: float
    source_sha256: str
    frame_index: int
    source_pts: int
    time_base_numerator: int
    time_base_denominator: int
    timestamp_origin_seconds: float
    timestamp_origin_numerator: int
    timestamp_origin_denominator: int
    width: int
    height: int
    timestamp_origin: str = 'format.start_time'
    sampled: bool = True
    exhaustive: bool = False


def _run(argv: list[str], *, deadline: float, limit: int) -> bytes:
    """Bound both pipes and total extraction wall time; never execute a shell."""
    if deadline <= time.monotonic():
        raise DomainError('invalid_media', 'Frame extraction timed out')
    try:
        with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE) as process:
            def drain(stream: IO[bytes], cap: int) -> tuple[bytes, bool]:
                result = bytearray()
                while chunk := stream.read(8192):
                    if len(result) + len(chunk) > cap:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        return b'', True
                    result.extend(chunk)
                return bytes(result), False

            assert process.stdout is not None and process.stderr is not None
            with ThreadPoolExecutor(max_workers=2) as readers:
                output = readers.submit(drain, process.stdout, limit)
                errors = readers.submit(drain, process.stderr, 16384)
                try:
                    process.wait(timeout=max(.001, deadline-time.monotonic()))
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise DomainError('invalid_media', 'Frame extraction timed out') from None
                raw, overflow = output.result()
                _, error_overflow = errors.result()
                if overflow or error_overflow:
                    raise DomainError('invalid_media', 'Frame extraction output exceeds local bounds')
                if process.returncode:
                    raise DomainError('invalid_media', 'Video frame decoding failed')
                return raw
    except OSError as exc:
        raise DomainError('invalid_media', 'Local frame decoder unavailable') from exc


def _binary(value: str | Path | None, name: str) -> str:
    candidate = Path(value) if value is not None else Path(shutil.which(name) or '')
    if not candidate.is_absolute() or not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise ValueError('Frame decoder must be an existing absolute executable')
    return str(candidate.resolve(strict=True))


class FrameEvidence:
    """Construct with service-owned policy; caller must authorize source access.

    extract returns immutable FrameSamples; only the fenced worker may publish
    PNGs/observations. Missing timestamps or exceeded bounds reject the entire
    sample, with no invented timing or silently incomplete endpoint coverage.
    """
    def __init__(self, media: MediaStore, *, policy: FramePolicy,
                 ffmpeg: str | Path | None = None, ffprobe: str | Path | None = None):
        self.media, self.policy = media, policy
        self.ffmpeg, self.ffprobe = _binary(ffmpeg, 'ffmpeg'), _binary(ffprobe, 'ffprobe')

    def _source(self, pid: str, ref: ObjectRef, snapshot: Path, deadline: float) -> dict[str, Any]:
        obj = self.media.store.get_object(pid, ref.object_id, revision=ref.revision)
        body = obj['body']
        if not ref.digest or obj['digest'] != ref.digest:
            raise DomainError('stale_input', 'Exact source digest required')
        if obj['kind'] != 'media' or body.get('media_type') not in ('video/mp4', 'video/quicktime'):
            raise DomainError('invalid_media', 'Frame evidence requires verified video media')
        if body['size'] > self.policy.max_source_bytes:
            raise DomainError('invalid_media', 'Source exceeds local byte limit')
        probe = body['probe']
        if probe['width'] * probe['height'] > self.policy.max_source_pixels:
            raise DomainError('invalid_media', 'Source exceeds local pixel limit')
        path = self.media.path_for(pid, ref.object_id, revision=ref.revision)
        # Decode a private verified snapshot: path replacement after validation
        # cannot make FFmpeg consume different bytes or an author-selected path.
        digest, count = hashlib.sha256(), 0
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as source, snapshot.open('xb') as out:
                while chunk := source.read(1048576):
                    count += len(chunk)
                    if count > self.policy.max_source_bytes or time.monotonic() > deadline:
                        raise DomainError('invalid_media', 'Source snapshot exceeds local limits')
                    digest.update(chunk)
                    out.write(chunk)
        except OSError as exc:
            raise DomainError('invalid_media', 'Source snapshot could not be verified') from exc
        if count != body['size'] or digest.hexdigest() != body['sha256']:
            raise DomainError('invalid_media', 'Source changed before extraction')
        return body

    def _timestamps(self, path: Path, body: dict[str, Any], deadline: float) -> tuple[list[int], Fraction, Fraction]:
        raw = _run([self.ffprobe, '-v', 'error', '-protocol_whitelist', 'file,pipe',
                    '-max_alloc', '67108864', '-probesize', '5000000', '-analyzeduration', '5000000',
                    '-max_streams', '32', '-threads', '1', '-select_streams', 'v:0', '-show_frames',
                    '-show_entries', ('format=start_time:stream=time_base:stream_disposition=attached_pic:'
                    'frame=pts,width,height'), '-of', 'json', str(path)],
                   deadline=deadline, limit=self.policy.max_probe_bytes)
        try:
            data = json.loads(raw)
            streams, frames = data['streams'], data['frames']
            if not isinstance(streams, list) or not isinstance(frames, list):
                raise TypeError('Invalid frame list')
            if len(streams) != 1 or streams[0].get('disposition', {}).get('attached_pic', 0):
                raise ValueError('Video track required')
            base_text, origin_text = streams[0]['time_base'], data['format']['start_time']
            if any(not isinstance(value, str) or len(value) > 64 for value in (base_text, origin_text)):
                raise ValueError('Missing or oversized time basis')
            base = Fraction(base_text)
            origin = Fraction(origin_text)
            if base <= 0 or len(str(origin)) > 100 or not 1 <= len(frames) <= self.policy.max_decoded_frames:
                raise ValueError('Invalid timing or frame count')
            pts: list[int] = []
            duration = Fraction(str(body['probe']['duration']))
            for frame in frames:
                value = frame['pts']
                if type(value) is not int or type(frame['width']) is not int or type(frame['height']) is not int:
                    raise ValueError('Invalid decoded frame')
                if min(frame['width'], frame['height']) <= 0 or frame['width']*frame['height'] > self.policy.max_source_pixels:
                    raise ValueError('Frame dimensions exceed policy')
                seconds = value * base - origin
                if not 0 <= seconds <= duration or (pts and value <= pts[-1]):
                    raise ValueError('Nonmonotonic or out-of-range timestamp')
                pts.append(value)
            return pts, base, origin
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError, ZeroDivisionError) as exc:
            raise DomainError('invalid_media', 'Decoded source timestamps are missing or outside local bounds') from exc

    def _pngs(self, raw: bytes, expected: int) -> list[bytes]:
        results, start, cursor = [], 0, 0
        while cursor < len(raw):
            if raw[cursor:cursor+8] != b'\x89PNG\r\n\x1a\n':
                raise DomainError('invalid_media', 'Decoder did not return PNG evidence')
            cursor += 8
            while True:
                if cursor+12 > len(raw):
                    raise DomainError('invalid_media', 'Incomplete decoded PNG')
                length = struct.unpack('>I', raw[cursor:cursor+4])[0]
                kind = raw[cursor+4:cursor+8]
                cursor += length+12
                if cursor > len(raw) or cursor-start > self.policy.max_png_bytes:
                    raise DomainError('invalid_media', 'PNG evidence exceeds local bounds')
                if kind == b'IEND':
                    results.append(raw[start:cursor])
                    start = cursor
                    break
            if len(results) > expected:
                raise DomainError('invalid_media', 'Unexpected decoded frame count')
        if len(results) != expected:
            raise DomainError('invalid_media', 'Source endpoints were not completely decoded')
        return results

    def extract(self, project_id: str, source: ObjectRef) -> tuple[FrameSample, ...]:
        """Sample actual first/last and evenly spaced decoded frame indices.

        source_seconds = source_pts * time_base - format.start_time. Keep all
        operands so time mapping can be checked without rounding float seconds.
        Audio/video bytes are never modified; sampling does not verify motion.
        """
        deadline = time.monotonic()+self.policy.timeout_seconds
        with tempfile.TemporaryDirectory(prefix='mvgp-frame-evidence-') as temporary:
            path = Path(temporary)/'source.mp4'
            body = self._source(project_id, source, path, deadline)
            pts, base, origin = self._timestamps(path, body, deadline)
            count = min(self.policy.max_frames, len(pts))
            indices = [0] if count == 1 else [i*(len(pts)-1)//(count-1) for i in range(count)]
            select = '+'.join(f'eq(n\\,{index})' for index in indices)
            scale = f"scale=w='min(iw,{self.policy.max_width})':h='min(ih,{self.policy.max_height})':force_original_aspect_ratio=decrease"
            raw = _run([self.ffmpeg, '-v', 'error', '-nostdin', '-protocol_whitelist', 'file,pipe',
                        '-max_alloc', '67108864', '-threads', '1', '-i', str(path), '-map', '0:v:0',
                        '-an', '-sn', '-dn', '-vf', f'select={select},{scale}', '-fps_mode', 'passthrough',
                        '-frames:v', str(count), '-threads', '1', '-f', 'image2pipe', '-c:v', 'png', 'pipe:1'],
                       deadline=deadline, limit=self.policy.max_total_png_bytes)
            result = []
            for index, png in zip(indices, self._pngs(raw, count), strict=True):
                try:
                    with Image.open(io.BytesIO(png)) as image:
                        width, height = image.size
                        if image.format != 'PNG' or not 0 < width <= self.policy.max_width or not 0 < height <= self.policy.max_height:
                            raise ValueError('Unexpected output dimensions')
                        image.verify()
                except (OSError, ValueError) as exc:
                    raise DomainError('invalid_media', 'Decoded PNG verification failed') from exc
                result.append(FrameSample(png, float(pts[index]*base-origin), body['sha256'], index,
                    pts[index], base.numerator, base.denominator, float(origin), origin.numerator, origin.denominator, width, height))
            if time.monotonic() > deadline:
                raise DomainError('invalid_media', 'Frame extraction timed out')
            return tuple(result)
