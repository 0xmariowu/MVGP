"""Stand-ins for the paid providers in a rehearsal. They spend nothing and call nothing.

`FakeFal` and `FakeApilio` answer the exact HTTP calls `production/provider_fal.py` and
`production/provider_apilio.py` make (same URLs, same answer shapes as the 2026-09-25 probes), so the real
adapters, the worker's paid path, the cost settlement and the download/probe/conformance checks all run.
`MediaServer` stands in for the result hosts: it makes each result file locally with ffmpeg at the size, length
and sound the request asked for, and serves it to the worker's real downloader.

Only the standard library and ffmpeg: the tests run under plain `python3`.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from urllib.parse import urlsplit

FAL_MEDIA = 'v3b.fal.media'
APILIO_MEDIA = 'webstatic.aiproxy.vip'
HOSTS = frozenset({FAL_MEDIA, APILIO_MEDIA})
# fal's frame sizes for 16:9 (probe: the 480p draft is 854x480, the completion 1920x1080).
VIDEO_SIZES = {'480p': (854, 480), '720p': (1280, 720), '1080p': (1920, 1080)}
IMAGE_SIZES = {'2048x1152': (2048, 1152)}


class MediaServer:
    """Result URLs on the providers' own hosts, served from files made on demand."""

    def __init__(self, root: Path, ffmpeg: str | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ffmpeg = ffmpeg or shutil.which('ffmpeg') or 'ffmpeg'
        self.files: dict[str, tuple[Path, str]] = {}
        self.lock = threading.Lock()

    def video(self, host: str, name: str, *, size: tuple[int, int], seconds: int, audio: bool = True) -> str:
        path = self.root / f'video-{size[0]}x{size[1]}-{seconds}s-{"a" if audio else "m"}.mp4'
        with self.lock:
            if not path.exists():
                command = [self.ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i', f'testsrc2=s={size[0]}x{size[1]}:r=24:d={seconds}']
                if audio:
                    command += ['-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}', '-c:a', 'aac', '-shortest']
                subprocess.run([*command, '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', str(path)],
                               check=True, timeout=120)
        return self._publish(host, name, path, 'video/mp4')

    def image(self, host: str, name: str, *, size: tuple[int, int]) -> str:
        path = self.root / f'image-{size[0]}x{size[1]}.png'
        with self.lock:
            if not path.exists():
                subprocess.run([self.ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i', f'testsrc2=s={size[0]}x{size[1]}:d=1',
                                '-frames:v', '1', str(path)], check=True, timeout=60)
        return self._publish(host, name, path, 'image/png')

    def _publish(self, host: str, name: str, path: Path, media_type: str) -> str:
        url = f'https://{host}/rehearsal/{name}'
        self.files[url] = (path, media_type)
        return url

    SPEC = re.compile(r'https://[a-z0-9.-]+/rehearsal/video-(\d{2,4})x(\d{2,4})-(\d{1,2})s-([am])\.mp4')

    @contextmanager
    def serve(self, url: str, host: str, ip: str, timeout: float) -> Iterator[Any]:
        """The worker downloader's transport, never the network: a URL this server published, or a clip named by its
        size, length and sound (the fake Higgsfield CLI runs in its own process and names its clips this way)."""
        url = url.split('#')[0]
        spec = self.SPEC.fullmatch(url)
        if url not in self.files and spec:
            self.video(urlsplit(url).hostname or '', url.rsplit('/', 1)[-1], size=(int(spec[1]), int(spec[2])),
                       seconds=int(spec[3]), audio=spec[4] == 'a')  # publishes this very URL
        path, media_type = self.files[url]
        data = path.read_bytes()
        yield SimpleNamespace(media_type=media_type, chunks=[data[i:i + 1 << 20] for i in range(0, len(data), 1 << 20)])


class FakeFal:
    """fal's queue for Seedance 2.5: CDN upload, draft submit, status, response and draft completion."""

    APP = 'https://queue.fal.run/bytedance/seedance-2.5'

    def __init__(self, media: MediaServer, *, refuse: set[str] | None = None, log: Path | None = None) -> None:
        self.media, self.refuse = media, refuse or set()
        # Every JSON body fal receives, one line each: the rehearsal checks what was sent.
        self.log = Path(log) if log else None
        self.requests: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    def __call__(self, method: str, url: str, headers: dict[str, str], json_body: Any, data: bytes | None,
                 timeout: float) -> tuple[int, bytes]:
        with self.lock:
            self.calls.append((method, url))
            if url.startswith('https://rest.fal.ai/storage/auth/token'):
                return 200, json.dumps({'token': 'rehearsal', 'token_type': 'Bearer', 'base_url': 'https://v3.fal.media'}).encode()
            if url == 'https://v3.fal.media/files/upload':
                name = hashlib.sha256(data or b'').hexdigest()[:16]
                return 200, json.dumps({'access_url': f'https://{FAL_MEDIA}/rehearsal/ref-{name}.png'}).encode()
            if method == 'POST' and url in (f'{self.APP}/reference-to-video', f'{self.APP}/draft/complete'):
                body = json_body or {}
                if self.log is not None:
                    with self.log.open('a') as out:
                        out.write(json.dumps({'url': url, 'body': body}, ensure_ascii=False) + '\n')
                if 'prompt' in body and any(word in body['prompt'] for word in self.refuse):
                    return 422, json.dumps({'detail': [{'msg': 'rehearsal refusal'}]}).encode()
                if url.endswith('/draft/complete') and self._draft(body.get('draft_id')) is None:
                    return 422, json.dumps({'detail': [{'msg': 'unknown draft_id'}]}).encode()
                rid = str(uuid.uuid4())
                self.requests[rid] = {'url': url, 'body': body, 'polls': 0}
                return 200, json.dumps({'status': 'IN_QUEUE', 'request_id': rid, 'status_url': f'{self.APP}/requests/{rid}/status',
                                        'response_url': f'{self.APP}/requests/{rid}'}).encode()
            match = re.fullmatch(re.escape(self.APP) + r'/requests/([0-9a-f-]{36})(/status)?', url)
            if method == 'GET' and match and match[1] in self.requests:
                request = self.requests[match[1]]
                if match[2]:
                    request['polls'] += 1
                    # Like fal: 202 while queued or rendering, 200 once done.
                    done = request['polls'] >= 2
                    return (200 if done else 202), json.dumps({'status': 'COMPLETED' if done else 'IN_PROGRESS'}).encode()
                return 200, json.dumps(self._result(match[1], request)).encode()
            return 404, json.dumps({'detail': 'not a fal call the platform makes'}).encode()

    def _result(self, rid: str, request: dict[str, Any]) -> dict[str, Any]:
        body = request['body']
        if request['url'].endswith('/draft/complete'):
            draft = self._draft(body['draft_id'])
            url = self.media.video(FAL_MEDIA, f'{rid}.mp4', size=VIDEO_SIZES['1080p'], seconds=draft['seconds'], audio=draft['audio'])
            return {'video': {'url': url, 'content_type': 'video/mp4'}, 'seed': draft['seed'], 'draft_id': None}
        seconds, audio = int(body.get('duration', 5)), bool(body.get('generate_audio', True))
        resolution = '480p' if body.get('draft') else body.get('resolution', '720p')
        url = self.media.video(FAL_MEDIA, f'{rid}.mp4', size=VIDEO_SIZES[resolution], seconds=seconds, audio=audio)
        seed = int(hashlib.sha256(rid.encode()).hexdigest()[:8], 16)
        draft_id = None
        if body.get('draft'):
            # Like fal's, the id outlives this process (a restarted worker still completes it): it carries its draft.
            draft_id = 'draft_' + base64.urlsafe_b64encode(json.dumps({'seconds': seconds, 'audio': audio, 'seed': seed}).encode()).decode()
        return {'video': {'url': url, 'content_type': 'video/mp4'}, 'seed': seed, 'draft_id': draft_id}

    @staticmethod
    def _draft(draft_id: Any) -> dict[str, Any] | None:
        try:
            value = json.loads(base64.urlsafe_b64decode(str(draft_id).removeprefix('draft_').encode()))
            return value if isinstance(value, dict) and {'seconds', 'audio', 'seed'} <= value.keys() else None
        except ValueError:
            return None


class FakeApilio:
    """apilio's images endpoints: one PNG per call at the pinned size, with token counts like the probe's."""

    def __init__(self, media: MediaServer) -> None:
        self.media, self.calls = media, []

    def __call__(self, method: str, url: str, headers: dict[str, str], json_body: Any, form: Any, files: Any,
                 timeout: float) -> tuple[int, bytes]:
        self.calls.append((method, url, len(files or [])))
        if method != 'POST' or url not in ('https://api.apilio.ai/v1/images/generations', 'https://api.apilio.ai/v1/images/edits'):
            return 404, b'{"error": {"message": "not an apilio call the platform makes"}}'
        size = (json_body or form or {}).get('size', '2048x1152')
        width, height = (int(v) for v in str(size).split('x'))
        url = self.media.image(APILIO_MEDIA, f'{uuid.uuid4()}.png', size=(width, height))
        return 200, json.dumps({'created': 0, 'model': (json_body or form or {}).get('model'), 'data': [{'url': url}],
                                'usage': {'input_tokens': 35, 'output_tokens': 1105, 'total_tokens': 1140}}).encode()


def fake_reader(endpoint: str, wire: bytes | dict, headers: dict[str, str], timeout: float, maximum: int, *,
                model: str) -> Any:
    """The source reader's Gemini call (production/reader.py): one visual observation, and every question answered
    `uncertain` with that observation as evidence, in the generateContent envelope the reader checks."""
    payload = json.loads(wire) if isinstance(wire, (bytes, bytearray)) else wire
    texts = [p.get('text', '') for c in payload.get('contents', []) for p in c.get('parts', []) if isinstance(p, dict)]
    prompt = next((t for t in texts if 'Questions: ' in t), '')
    questions = json.loads(prompt.rsplit('Questions: ', 1)[1]) if prompt else []
    output = {'observations': [{'input_id': 'original', 'timestamp_seconds': 0.5, 'modality': 'visual',
                                'fact': 'Rehearsal reader: a shape crosses the frame from left to right.', 'interpretation': None}],
              'answers': [{'question_index': n, 'verdict': 'uncertain', 'evidence_indices': [0],
                           'explanation': 'Rehearsal reader: nothing is judged.'} for n in range(len(questions))],
              'uncertainty': ['This is the rehearsal reader; it watched nothing.']}
    envelope = {'modelVersion': model, 'usageMetadata': {'promptTokenCount': 1000, 'candidatesTokenCount': 100,
                'promptTokensDetails': [{'modality': 'VIDEO', 'tokenCount': 900}, {'modality': 'TEXT', 'tokenCount': 100}]},
                'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': json.dumps(output)}]}}]}
    return SimpleNamespace(status_code=200, body=json.dumps(envelope).encode())
