"""Bounded immutable uploads. Auth is enforced by the calling project service.

Only server-resolved paths reach ffprobe; uploaded logical paths are labels.
Blob publication precedes metadata publication; orphaned blobs are not readable
through an API object ID and can be collected by an operator's later maintenance.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import warnings
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path
from typing import IO, Any

from PIL import Image, UnidentifiedImageError

from production.contracts import DomainError, ObjectRef, safe_logical_path
from production.store import Store

IMAGE_FORMATS = {'image/png': 'PNG', 'image/jpeg': 'JPEG', 'image/webp': 'WEBP'}
AV_FORMATS = {'video/mp4': {'mov', 'mp4'}, 'video/quicktime': {'mov'},
              'audio/wav': {'wav'}, 'audio/mpeg': {'mp3'}, 'audio/mp4': {'mov', 'mp4'}}
TEXT_TYPES = {'application/json', 'text/plain', 'text/markdown'}
PROBE_OUTPUT_BYTES = 262144
PROBE_ERROR_BYTES = 16384
PROBE_MAX_STREAMS = 32
PROBE_TIMEOUT_SECONDS = 20


def _probe_output(command: list[str]) -> bytes:
    """Drain both pipes with fixed bounds; no thread-unsafe preexec resource hook.

    FFmpeg's max_alloc limits individual allocations, not total process RSS.
    These output/probe/thread limits supplement the deployment's memory boundary.
    """
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as process:
        def drain(stream: IO[bytes], limit: int) -> tuple[bytes, bool]:
            result = bytearray()
            while chunk := stream.read(8192):
                if len(result) + len(chunk) > limit:
                    process.kill()
                    return b'', True
                result.extend(chunk)
            return bytes(result), False

        assert process.stdout is not None and process.stderr is not None
        with ThreadPoolExecutor(max_workers=2) as readers:
            output = readers.submit(drain, process.stdout, PROBE_OUTPUT_BYTES)
            errors = readers.submit(drain, process.stderr, PROBE_ERROR_BYTES)
            try:
                process.wait(timeout=PROBE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise
            stdout, stdout_overflow = output.result()
            _, stderr_overflow = errors.result()
            if stdout_overflow or stderr_overflow:
                raise ValueError('Media probe output exceeded bounds')
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, command)
            return stdout


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _directory(path: Path) -> None:
    if path.is_symlink():
        raise DomainError('invalid_media', 'Storage directory is a symlink')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise DomainError('invalid_media', 'Invalid storage directory')


# Bounds constrain measurements, not upload duration or provider capabilities.
# Unknown metadata remains nullable so generated-output policy can fail closed.
MEASUREMENT_LIMIT = 2**31 - 1


def _positive_measurement(value: Any) -> float | None:
    if type(value) not in (str, int, float) or len(str(value)) > 64:
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and 0 < number <= MEASUREMENT_LIMIT else None
    except ValueError:
        return None


def _ratio(value: Any, separator: str) -> Fraction | None:
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,10}'+re.escape(separator)+r'[0-9]{1,10}', value):
        return None
    numerator, denominator = map(int, value.split(separator))
    if not 0 < numerator <= MEASUREMENT_LIMIT or not 0 < denominator <= MEASUREMENT_LIMIT:
        return None
    return Fraction(numerator, denominator)


def _rotation(stream: dict[str, Any]) -> tuple[float | None, str]:
    def angle(value: Any) -> float | None:
        if type(value) not in (str, int, float) or len(str(value)) > 64:
            return None
        try:
            number = float(value)
            if not math.isfinite(number) or abs(number) > MEASUREMENT_LIMIT or number % 90:
                return None
            return number % 360
        except ValueError:
            return None
    tags, side = stream.get('tags', {}), stream.get('side_data_list', [])
    if not isinstance(tags, dict) or not isinstance(side, list) or len(side) > PROBE_MAX_STREAMS or any(not isinstance(v, dict) for v in side):
        return None, 'invalid-transform-metadata'
    matrices = [v for v in side if v.get('side_data_type') == 'Display Matrix' or 'rotation' in v or 'displaymatrix' in v]
    tagged = angle(tags['rotate']) if 'rotate' in tags else None
    if 'rotate' in tags and tagged is None:
        return None, 'invalid-rotation-tag'
    if not matrices:
        return (tagged, 'rotation-tag') if tagged is not None else (0.0, 'absent-identity')
    if len(matrices) != 1:
        return None, 'conflicting-display-matrices'
    entry = matrices[0]
    text = entry.get('displaymatrix')
    if not isinstance(text, str) or len(text) > 2048:
        return None, 'unverified-display-matrix'
    rows = [re.fullmatch(r'[0-9a-fA-F]{8}:\s+(-?[0-9]{1,11})\s+(-?[0-9]{1,11})\s+(-?[0-9]{1,11})', line.strip())
            for line in text.strip().splitlines()]
    if len(rows) != 3 or any(row is None for row in rows):
        return None, 'invalid-display-matrix'
    values = [int(v) for row in rows if row is not None for v in row.groups()]
    # Only pure cardinal rotations are currently measured. Never silently drop
    # reflections, shear, scale, perspective or translation in a display matrix.
    cardinal = {(65536, 0, 0, 65536): 0.0, (0, -65536, 65536, 0): 90.0,
                (-65536, 0, 0, -65536): 180.0, (0, 65536, -65536, 0): 270.0}
    rotation = cardinal.get((values[0], values[1], values[3], values[4]))
    if values[2:9:3] != [0, 0, 1073741824] or values[6:8] != [0, 0] or rotation is None:
        return None, 'unsupported-display-transform'
    if ('rotation' in entry and angle(entry['rotation']) != rotation
            or tagged is not None and tagged != rotation):
        return None, 'conflicting-rotation-metadata'
    return rotation, 'display-matrix'


def _video_measurements(stream: dict[str, Any], width: int, height: int) -> dict[str, Any]:
    duration = _positive_measurement(stream.get('duration'))
    duration_source = 'stream-duration' if duration is not None else 'unknown'
    if 'duration' not in stream or stream['duration'] == 'N/A':
        ticks, base = stream.get('duration_ts'), _ratio(stream.get('time_base'), '/')
        if type(ticks) is int and 0 < ticks <= 2**63 - 1 and base is not None:
            duration = _positive_measurement(float(ticks * base))
            duration_source = 'stream-ticks' if duration is not None else 'unknown'
    sar = _ratio(stream.get('sample_aspect_ratio'), ':')
    sar_source = 'stream' if sar else 'unknown'
    if 'sample_aspect_ratio' not in stream or stream['sample_aspect_ratio'] == 'N/A':
        # Playback geometry, not an invented encoded tag: ffplay's
        # calculate_display_rect defaults unspecified SAR to square pixels.
        # Real Chromium also displays the retained untagged H264 at1280x720.
        # Malformed explicit ratios still remain unknown below.
        sar, sar_source = Fraction(1, 1), 'playback-default-square'
    rotation, rotation_source = _rotation(stream)
    display_width = display_height = None
    if sar is not None and rotation is not None:
        display_width, display_height = float(width * sar), float(height)
        if rotation in (90, 270):
            display_width, display_height = display_height, display_width
        if max(display_width, display_height) > MEASUREMENT_LIMIT:
            display_width = display_height = None
    return {'video_duration': duration, 'video_duration_source': duration_source,
            'sample_aspect_ratio': f'{sar.numerator}:{sar.denominator}' if sar else None,
            'sample_aspect_ratio_source': sar_source,
            'rotation_degrees': rotation, 'rotation_source': rotation_source,
            'display_width': display_width, 'display_height': display_height,
            'display_geometry_status': 'verified' if display_width is not None else 'unknown'}


class MediaStore:
    def __init__(self, store: Store, root: str | Path, *, max_bytes: int = 536870912,
                 max_pixels: int = 40_000_000) -> None:
        self.store = store
        self.root = Path(root).absolute()
        if max_bytes <= 0 or max_pixels <= 0:
            raise ValueError('Positive media bounds required')
        self.max_bytes, self.max_pixels = max_bytes, max_pixels
        for path in [self.root, self.root / 'staging', self.root / 'objects']:
            _directory(path)

    def _probe(self, path: Path, media_type: str) -> dict[str, Any]:
        if media_type in IMAGE_FORMATS:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('error', Image.DecompressionBombWarning)
                    with Image.open(path) as im:
                        if im.format != IMAGE_FORMATS[media_type] or im.width * im.height > self.max_pixels:
                            raise ValueError('Image type or dimensions mismatch')
                        result = {'width': im.width, 'height': im.height, 'format': im.format}
                        im.verify()
                        return result
            except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError,
                    Image.DecompressionBombWarning) as exc:
                raise DomainError('invalid_media', 'Image failed type/dimension verification') from exc
        if media_type in TEXT_TYPES:
            try:
                text = path.read_text(encoding='utf-8')
                if media_type == 'application/json':
                    json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
                return {'encoding': 'utf-8'}
            except (UnicodeError, ValueError) as exc:
                raise DomainError('invalid_media', 'Invalid text or JSON upload') from exc
        try:
            output = _probe_output([
                'ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                '-max_alloc', '67108864', '-probesize', '5000000',
                '-analyzeduration', '5000000', '-max_streams', str(PROBE_MAX_STREAMS),
                '-threads', '1', '-show_entries',
                ('format=format_name,duration:stream=codec_type,width,height,duration,duration_ts,time_base,sample_aspect_ratio:'
                 'stream_disposition=attached_pic:stream_tags=rotate:stream_side_data=side_data_type,rotation,displaymatrix'),
                '-of', 'json', str(path),
            ])
            data = json.loads(output)
            formats = set(data.get('format', {}).get('format_name', '').split(','))
            streams = data.get('streams', [])
            if not isinstance(streams, list) or len(streams) > PROBE_MAX_STREAMS:
                raise ValueError('Media stream count exceeded bounds')
            videos = [s for s in streams if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')]
            audios = [s for s in streams if s.get('codec_type') == 'audio']
            duration = float(data.get('format', {}).get('duration', 0))
            if not formats.intersection(AV_FORMATS[media_type]) or not math.isfinite(duration) or duration <= 0:
                raise ValueError('Container or duration mismatch')
            if media_type.startswith('video/') and not videos:
                raise ValueError('Video track required')
            if media_type.startswith('audio/') and (not audios or videos):
                raise ValueError('Audio-only media required')
            result = {'duration': duration, 'container_duration': duration, 'has_audio': bool(audios), 'has_video': bool(videos)}
            if videos:
                width, height = int(videos[0]['width']), int(videos[0]['height'])
                if width <= 0 or height <= 0 or width * height > self.max_pixels:
                    raise ValueError('Video dimensions out of bounds')
                result.update(width=width, height=height, **_video_measurements(videos[0], width, height))
            return result
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            raise DomainError('invalid_media', 'Media failed container/track verification') from exc

    def put(self, project_id: str, chunks: Iterable[bytes], media_type: str, author: str, *,
            logical_path: str = 'uploads/media', expected_size: int | None = None,
            expected_hash: str | None = None, derivative_of: dict[str, Any] | None = None,
            idempotency_key: str | None = None,
            publish: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> dict[str, Any]:
        # Trusted service callback rechecks permission at metadata commit, after IO.
        if publish is not None and idempotency_key is not None:
            raise ValueError("Publication callback must own its idempotency transaction")
        safe_logical_path(logical_path)
        if media_type not in IMAGE_FORMATS and media_type not in AV_FORMATS and media_type not in TEXT_TYPES:
            raise DomainError('invalid_media', 'Unsupported upload media type')
        self.store.get_object(project_id, project_id)
        parent = None
        if derivative_of is not None:
            ref = ObjectRef.model_validate(derivative_of)
            parent = self.store.get_object(project_id, ref.object_id, revision=ref.revision)
            if parent['kind'] != 'media' or (ref.digest is not None and parent['digest'] != ref.digest):
                raise DomainError('stale_input', 'Derivative source does not match recorded media')
            derivative_of = {'object_id': ref.object_id, 'revision': ref.revision, 'digest': parent['digest']}
        _directory(self.root / 'staging')
        fd, name = tempfile.mkstemp(prefix='upload-', dir=self.root / 'staging')
        temporary = Path(name)
        try:
            size, h = 0, hashlib.sha256()
            with os.fdopen(fd, 'wb') as stream:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise DomainError('invalid_media', 'Upload chunks must be bytes')
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise DomainError('invalid_media', 'Upload exceeds configured byte limit')
                    stream.write(chunk)
                    h.update(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            digest = h.hexdigest()
            if size == 0 or (expected_size is not None and size != expected_size) or (expected_hash is not None and digest != expected_hash):
                raise DomainError('invalid_media', 'Upload size or fingerprint mismatch')
            probe = self._probe(temporary, media_type)
            target = self.root / 'objects' / digest[:2] / digest
            _directory(target.parent)
            if target.is_symlink():
                raise DomainError('invalid_media', 'Blob path is a symlink')
            if target.exists():
                if not target.is_file() or _digest(target) != digest:
                    raise DomainError('invalid_media', 'Existing blob is not intact')
            else:
                os.chmod(temporary, 0o400)
                try:
                    os.link(temporary, target)  # atomic no-clobber publication on the same volume
                except FileExistsError:
                    if target.is_symlink() or _digest(target) != digest:
                        raise DomainError('invalid_media', 'Concurrent blob publication failed integrity') from None
                directory_fd = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            metadata = {
                'sha256': digest, 'size': size, 'media_type': media_type,
                'logical_path': logical_path, 'probe': probe, 'derivative_of': derivative_of,
            }
            if publish is not None:
                return publish(metadata)
            if idempotency_key is not None:
                return self.store.run_idempotent(
                    f'media:{project_id}:{author}', idempotency_key, metadata,
                    lambda db: self.store.create_object(project_id, 'media', metadata, author, conn=db))
            return self.store.create_object(project_id, 'media', metadata, author)
        finally:
            temporary.unlink(missing_ok=True)

    def path_for(self, project_id: str, media_id: str, *, revision: int | None = None) -> Path:
        """Server use only: authenticate project access before returning any byte range."""
        obj = self.store.get_object(project_id, media_id, revision=revision)
        if obj['kind'] != 'media':
            raise DomainError('invalid_media', 'Object is not media')
        digest = obj['body']['sha256']
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise DomainError('invalid_media', 'Invalid recorded media digest')
        target = self.root / 'objects' / digest[:2] / digest
        if any(p.is_symlink() for p in (self.root, self.root/'objects', target.parent, target)):
            raise DomainError('invalid_media', 'Storage path is a symlink')
        if not target.is_file() or target.stat().st_size != obj['body']['size'] or _digest(target) != digest:
            raise DomainError('invalid_media', 'Media integrity check failed')
        return target

    def read(self, project_id: str, media_id: str, *, revision: int | None = None) -> bytes:
        return self.path_for(project_id, media_id, revision=revision).read_bytes()
