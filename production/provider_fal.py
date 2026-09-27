"""fal Seedance 2.5: four 480p draft takes per card, the owner's pick completed to 1080p.

Two job types. `fal_seedance_2_5` sends a reference-to-video request (`draft: true` gives a 480p take plus a
`draft_id` valid for seven days on the same account); `fal_seedance_2_5_complete` sends that `draft_id` to
`draft/complete` for the 1080p take with the same frames and seed (2026-09-25
provider probe). Both run on fal's queue: the submit answers a request id, the worker polls the
status and then the response. Every URL is built from fixed hosts; a status or response URL fal hands back must
equal the one built from its request id.

Reference images go to the fal CDN (rest.fal.ai token, v3.fal.media upload) before the paid request. A failed
upload sends nothing paid, so it is a failed take. After the paid request is sent, a lost answer is an unknown
outcome for the operator: fal's listing shows a request only after it ends (probe), so an in-flight request can
never be closed as absent, and the adapter does not list (`can_list` false). Media and reference URLs stay out of
receipts; the result URL goes only to the worker's private download record. `FAL_AI_TOKEN` is read at call time
and never logged or journaled.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from production.contracts import DomainError
from production.provider_types import ProviderReceipt, ResolvedReference

APP = 'bytedance/seedance-2.5'
ENDPOINTS = {'fal_seedance_2_5': f'{APP}/reference-to-video', 'fal_seedance_2_5_complete': f'{APP}/draft/complete'}
# a shot with no reference image goes to text-to-video, only when the pinned capability
# enables it (`text_to_video.enabled`, off until the live probe; fal OpenAPI endpoint_id=…/text-to-video).
TEXT_TO_VIDEO = f'{APP}/text-to-video'
NO_IMAGES = ('这个镜头没有参考图。纯文字出片（fal 的 text-to-video）还没打开，所以什么都没发出去，也没花钱。'
             '给镜头加一张参考图再拍；或者等试拍确认纯文字出片可用、打开这个开关以后再拍。')
QUEUE = 'https://queue.fal.run/'
UPLOAD_TOKEN = 'https://rest.fal.ai/storage/auth/token?storage_type=fal-cdn-v3'
UPLOAD = 'https://v3.fal.media/files/upload'
REQUEST_ID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
DRAFT_ID = re.compile(r'draft_[A-Za-z0-9_=-]{1,8186}')
# Draft ids last seven days from creation (fal schema); the receipt keeps a margin because the worker
# sees completion a poll interval after fal does.
DRAFT_DAYS, DRAFT_MARGIN_SECONDS = 7, 6 * 3600
MEDIA_TYPES = frozenset({'image/png', 'image/jpeg', 'image/webp'})
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_REFERENCE_BYTES = 30 * 1024 * 1024
MAX_PROMPT_BYTES = 65536  # UTF-8; preflight uses the same bound (production/gates.py)
DRAFT_PARAMS = frozenset({'prompt', 'duration', 'resolution', 'aspect_ratio', 'generate_audio', 'draft'})
# A completion sends only draft_id and resolution; the rest are the draft's settings, kept so the 1080p
# result is measured against the same duration, ratio and audio.
COMPLETE_PARAMS = frozenset({'draft_id', 'resolution', 'duration', 'aspect_ratio', 'generate_audio'})
# (method, url, headers, json_body, data, timeout) -> (status, bytes); injected in tests and rehearsals.
Transport = Callable[[str, str, dict[str, str], Any, bytes | None, float], tuple[int, bytes]]


def _http(method: str, url: str, headers: dict[str, str], json_body: Any, data: bytes | None, timeout: float) -> tuple[int, bytes]:
    import httpx
    with httpx.Client(follow_redirects=False, timeout=httpx.Timeout(timeout, connect=20)) as client:
        with client.stream(method, url, headers=headers, json=json_body, content=data) as response:
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    raise DomainError('unknown_outcome', 'fal response exceeded its bound')
            return response.status_code, bytes(body)


def _media_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    host = parsed.hostname or ''
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port
            or not (host == 'fal.media' or host.endswith('.fal.media'))):
        return None
    return value


class FalSeedance:
    can_list = False

    def __init__(self, capabilities: Mapping[str, dict[str, Any]], *, transport: Transport | None = None,
                 live_enabled: bool = False, timeout: float = 120, key_env: str = 'FAL_AI_TOKEN',
                 clock: Callable[[], float] = time.time) -> None:
        if not capabilities or not set(capabilities) <= set(ENDPOINTS) or not 0 < timeout <= 600:
            raise ValueError('Explicit fal capabilities and a bounded timeout are required')
        if type(live_enabled) is not bool or (live_enabled and transport is not None):
            raise ValueError('Live enablement cannot be combined with an injected transport')
        self._capabilities = json.loads(json.dumps(dict(capabilities)))
        for name, doc in self._capabilities.items():
            params = doc.get('params') if isinstance(doc.get('params'), list) else []
            if (doc.get('job_type') != name or doc.get('type') != 'video' or doc.get('endpoint') != ENDPOINTS[name]
                    or not all(isinstance(p, dict) and isinstance(p.get('name'), str) for p in params)):
                raise ValueError('fal capability needs its job type, video type, fixed endpoint and params')
            text = doc.get('text_to_video')
            if text is not None and (name != 'fal_seedance_2_5' or not isinstance(text, dict)
                                     or type(text.get('enabled')) is not bool or text.get('endpoint') != TEXT_TO_VIDEO):
                raise ValueError('fal text_to_video needs an explicit enabled flag and the fixed endpoint')
        self.fake, self.transport = transport is not None, transport or _http
        self.live_enabled, self.timeout, self.key_env, self.clock = live_enabled, float(timeout), key_env, clock

    def capabilities(self, job_type: str) -> dict[str, Any]:
        if job_type not in self._capabilities:
            raise DomainError('unsupported_route', 'Model was not pinned by this service')
        return json.loads(json.dumps(self._capabilities[job_type]))

    def _isolation(self) -> dict[str, str]:
        return {}

    def _key(self) -> str:
        key = 'fake' if self.fake else os.environ.get(self.key_env, '')
        if not key:
            raise DomainError('missing_prerequisite', 'The fal key is not in the worker environment; no request sent')
        return key

    def _text_to_video(self) -> bool:
        return bool((self._capabilities.get('fal_seedance_2_5', {}).get('text_to_video') or {}).get('enabled'))

    def _parameters(self, request: dict[str, Any], *, has_references: bool, sending: bool = True) -> list[str]:
        if not isinstance(request, dict) or set(request) != {'job_type', 'params', 'references'}:
            raise DomainError('unsupported_route', 'Only the compiled request schema is accepted')
        job_type, params = request['job_type'], request['params']
        if job_type not in self._capabilities or not isinstance(params, dict):
            raise DomainError('unsupported_route', 'Unsupported model or raw provider parameter')
        allowed = DRAFT_PARAMS if job_type == 'fal_seedance_2_5' else COMPLETE_PARAMS
        if set(params) - allowed:
            raise DomainError('unsupported_route', 'fal request carries settings its route does not take')
        spec = {p['name']: p for p in self._capabilities[job_type]['params']}
        for name, value in params.items():
            definition = spec.get(name)
            kind = {'string': str, 'integer': int, 'boolean': bool}.get((definition or {}).get('type'))
            if kind is None or type(value) is not kind or ('enum' in definition and value not in definition['enum']):
                raise DomainError('unsupported_route', f'fal parameter {name} is outside the pinned capability')
        if job_type == 'fal_seedance_2_5':
            prompt = params.get('prompt')
            if not isinstance(prompt, str) or not prompt.strip() or '\x00' in prompt or len(prompt.encode()) > MAX_PROMPT_BYTES:
                raise DomainError('invalid_input', 'fal draft request needs a bounded prompt')
            if not {'duration', 'resolution', 'aspect_ratio', 'generate_audio'} <= params.keys():
                raise DomainError('invalid_input', 'fal draft request needs explicit duration, resolution, ratio and audio')
            if params.get('draft') is True and params['resolution'] != '480p':
                raise DomainError('invalid_input', 'A fal draft is 480p')
            if sending and not has_references and not self._text_to_video():
                raise DomainError('unsupported_route', NO_IMAGES)
        else:
            if has_references or not DRAFT_ID.fullmatch(str(params.get('draft_id'))) or params.get('resolution') != '1080p':
                raise DomainError('invalid_input', 'A fal completion takes one draft id, 1080p and no references')
        return []

    def _references(self, request: dict[str, Any], resolved: list[ResolvedReference]) -> list[str]:
        declared = request.get('references')
        limit = int(self._capabilities[request['job_type']].get('max_references', 0))
        if not isinstance(declared, list) or len(declared) != len(resolved) or len(resolved) > limit:
            raise DomainError('invalid_media', 'Resolved media must exactly match the frozen reference order and limit')
        paths = []
        for ref in resolved:
            data = Path(ref.path).read_bytes()
            if (ref.media_type not in MEDIA_TYPES or len(data) > MAX_REFERENCE_BYTES
                    or hashlib.sha256(data).hexdigest() != ref.sha256):
                raise DomainError('invalid_media', 'A reference image differs from its recorded bytes')
            paths.append(str(ref.path))
        return paths

    def _upload(self, ref: ResolvedReference, key: str) -> str:
        status, body = self.transport('POST', UPLOAD_TOKEN, {'Authorization': f'Key {key}'}, {}, None, 60)
        token = json.loads(body) if status == 200 else {}
        if not isinstance(token, dict) or not isinstance(token.get('token'), str) or not isinstance(token.get('token_type'), str):
            raise ValueError('fal CDN token refused')
        data = Path(ref.path).read_bytes()
        if hashlib.sha256(data).hexdigest() != ref.sha256:
            raise DomainError('invalid_media', 'A reference image changed before upload')
        status, body = self.transport('POST', UPLOAD, {'Authorization': f"{token['token_type']} {token['token']}",
                                                       'Content-Type': ref.media_type,
                                                       'X-Fal-File-Name': f'reference{Path(ref.path).suffix or ".png"}'},
                                      None, data, 300)
        url = _media_url((json.loads(body) if status == 200 else {}).get('access_url'))
        if url is None:
            raise ValueError('fal CDN upload gave no usable URL')
        return url

    def _wire(self, request: dict[str, Any], image_urls: list[str]) -> dict[str, Any]:
        params = request['params']
        if request['job_type'] == 'fal_seedance_2_5_complete':
            return {'draft_id': params['draft_id'], 'resolution': '1080p'}
        wire = {'prompt': params['prompt'], 'duration': str(params['duration']), 'resolution': params['resolution'],
                'aspect_ratio': params['aspect_ratio'], 'generate_audio': params['generate_audio'],
                'draft': params.get('draft', False)}
        if image_urls:
            wire['image_urls'] = image_urls
        return wire

    def submit(self, intent: dict[str, Any], resolved: list[ResolvedReference], *,
               evidence_sink: Callable[[dict[str, Any]], None] | None = None) -> ProviderReceipt:
        if not self.fake and not self.live_enabled:
            raise DomainError('unsupported_route', 'Live fal requires explicit service enablement; no request sent')
        if intent.get('operation') not in ('submit', 'complete-draft') or intent.get('cost', {}).get('mode') != ('fake' if self.fake else 'live'):
            raise DomainError('forbidden', 'Adapter mode differs from service-authorized intent')
        request = intent['request']
        self._parameters(request, has_references=bool(resolved))
        self._references(request, resolved)
        key = self._key()
        try:
            image_urls = [self._upload(ref, key) for ref in resolved]
        except DomainError:
            raise
        except Exception as exc:  # noqa: BLE001 -- nothing paid was sent; the take failed before the request.
            digest = hashlib.sha256(f'{type(exc).__name__}:{time.time_ns()}'.encode()).hexdigest()[:24]
            return ProviderReceipt(f'fal-upload-failed-{digest}', request['job_type'], 'upload_failed', 'failed',
                                   {'error': 'reference upload to the fal CDN failed; no paid request was sent'}, {}, {},
                                   settled_cost=0)
        endpoint = TEXT_TO_VIDEO if request['job_type'] == 'fal_seedance_2_5' and not image_urls else ENDPOINTS[request['job_type']]
        try:
            status, body = self.transport('POST', QUEUE + endpoint,
                                          {'Authorization': f'Key {key}'}, self._wire(request, image_urls), None, self.timeout)
        except DomainError:
            raise
        except Exception as exc:  # noqa: BLE001 -- the paid request may have run; never resend, the operator settles it.
            raise DomainError('unknown_outcome', 'fal submit did not answer after sending; no automatic retry') from exc
        if evidence_sink is not None:
            evidence_sink({'provider': 'fal', 'stage': 'submit', 'status': status, 'bytes': len(body),
                           'sha256': hashlib.sha256(body).hexdigest(), 'body': body[:65536].decode('utf-8', 'replace')})
        if status >= 500:
            raise DomainError('unknown_outcome', f'fal answered {status} to the submit; no automatic retry')
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise DomainError('unknown_outcome', 'fal answered the submit with unreadable data; no automatic retry') from exc
        if status >= 400:
            # A request fal refuses at the queue never ran, so it costs nothing.
            return ProviderReceipt(f'fal-refused-{hashlib.sha256(body).hexdigest()[:24]}', request['job_type'], 'refused',
                                   'failed', {'error': _detail(data)}, {}, {}, settled_cost=0)
        request_id = data.get('request_id') if isinstance(data, dict) else None
        if (not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id)
                or data.get('status_url') not in (None, _status_url(request_id))
                or data.get('response_url') not in (None, _response_url(request_id))):
            raise DomainError('unknown_outcome', 'fal submit answer has no request id or foreign queue URLs; no automatic retry')
        return ProviderReceipt(request_id, request['job_type'], str(data.get('status') or 'IN_QUEUE')[:32], 'submitted',
                               {'request_id': request_id, 'endpoint': endpoint,
                                'references_uploaded': len(image_urls)}, {}, {})

    def get(self, job_id: str, expected_request: dict[str, Any], *,
            evidence_sink: Callable[[dict[str, Any]], None] | None = None) -> ProviderReceipt:
        if not isinstance(job_id, str) or not REQUEST_ID.fullmatch(job_id):
            raise DomainError('invalid_input', 'Invalid fal request identity')
        self._parameters(expected_request, has_references=bool(expected_request.get('references')), sending=False)
        job_type, key = expected_request['job_type'], self._key()
        status, body = self._read(_status_url(job_id), key, evidence_sink, 'status')
        # fal answers 202 while a request is queued or rendering and 200 once it is done (live 2026-09-25: every
        # in-flight poll read as a failure before this).
        state = json.loads(body).get('status') if status in (200, 202) else None
        if state in ('IN_QUEUE', 'IN_PROGRESS'):
            return ProviderReceipt(job_id, job_type, state, 'running', {'request_id': job_id, 'status': state}, {}, {})
        if state != 'COMPLETED':
            raise DomainError('provider_failure', f'fal status answered {status}; the worker polls again')
        status, body = self._read(_response_url(job_id), key, evidence_sink, 'response')
        if status >= 500:
            raise DomainError('provider_failure', f'fal response answered {status}; the worker polls again')
        data = json.loads(body)
        if status >= 400 or not isinstance(data, dict):
            return ProviderReceipt(job_id, job_type, 'error', 'failed', {'request_id': job_id, 'error': _detail(data)}, {}, {})
        video = data.get('video') if isinstance(data.get('video'), dict) else {}
        url = _media_url(video.get('url'))
        seed, draft_id = data.get('seed'), data.get('draft_id')
        if (url is None or video.get('content_type') not in (None, 'video/mp4') or type(seed) is not int
                or (draft_id is not None and not DRAFT_ID.fullmatch(str(draft_id)))):
            raise DomainError('provider_failure', 'fal result has no usable video on a fal media host')
        drafted = job_type == 'fal_seedance_2_5' and expected_request['params'].get('draft') is True
        if drafted and draft_id is None:
            raise DomainError('provider_failure', 'fal draft finished without its draft id')
        raw: dict[str, Any] = {'request_id': job_id, 'status': 'COMPLETED', 'seed': seed, 'result_host': urlsplit(url).hostname,
                               'file_size': video.get('file_size') if type(video.get('file_size')) is int else None}
        if drafted:
            raw.update(draft_id=draft_id,
                       draft_expires_at=int(self.clock()) + DRAFT_DAYS * 86400 - DRAFT_MARGIN_SECONDS)
        # fal bills tokens that grow with the seconds rendered; the pinned per-second price (2026-09-25 probe:
        # 480p draft 0.830962 USD for 4 s, 1080p completion 4.59862353 USD for 4 s) settles the take's cost.
        rate = (self._capabilities[job_type].get('usd_micros_per_second') or {}).get(expected_request['params'].get('resolution'))
        seconds = expected_request['params'].get('duration')
        cost = rate * seconds if type(rate) is int and type(seconds) is int and rate > 0 and seconds > 0 else None
        raw['cost_basis'] = 'pinned usd_micros_per_second x seconds' if cost is not None else None
        return ProviderReceipt(job_id, job_type, 'COMPLETED', 'succeeded', raw, {}, {}, result_url=url, settled_cost=cost)

    def _read(self, url: str, key: str, evidence_sink: Callable[[dict[str, Any]], None] | None, stage: str) -> tuple[int, bytes]:
        try:
            status, body = self.transport('GET', url, {'Authorization': f'Key {key}'}, None, None, min(self.timeout, 60))
            json.loads(body)
        except DomainError:
            raise
        except Exception as exc:  # noqa: BLE001 -- a read is free; the worker polls again.
            raise DomainError('provider_failure', f'fal {stage} read failed; the worker polls again') from exc
        if evidence_sink is not None:
            evidence_sink({'provider': 'fal', 'stage': stage, 'status': status, 'bytes': len(body),
                           'sha256': hashlib.sha256(body).hexdigest(), 'body': _without_urls(body)})
        return status, body

    def list_recent(self, job_type: str) -> list[dict[str, Any]]:
        # A request in flight is missing from fal's listing (probe), so a listing can never prove absence.
        raise DomainError('unsupported_route', 'fal requests cannot be settled from a listing; the operator settles them')


def _status_url(request_id: str) -> str:
    return f'{QUEUE}{APP}/requests/{request_id}/status'


def _response_url(request_id: str) -> str:
    return f'{QUEUE}{APP}/requests/{request_id}'


def _detail(data: Any) -> str:
    detail = data.get('detail') if isinstance(data, dict) else data
    return json.dumps(detail, ensure_ascii=False)[:300] if not isinstance(detail, str) else detail[:300]


def _without_urls(body: bytes) -> str:
    # Journal the answer, not the signed media address.
    return re.sub(r'https://[^\s"\']+', '<url>', body[:65536].decode('utf-8', 'replace'))


def input_schema_hash(openapi: dict[str, Any], job_type: str) -> str:
    """Hash of the endpoint's input schema from fal's OpenAPI document; the pinned capability records it, so a
    release build can tell when fal changed the inputs (`https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=`)."""
    schemas = openapi['components']['schemas']
    name = {'fal_seedance_2_5': 'Seedance25ReferenceToVideoInput', 'fal_seedance_2_5_complete': 'Seedance25DraftCompleteInput'}[job_type]
    return hashlib.sha256(json.dumps(schemas[name], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
