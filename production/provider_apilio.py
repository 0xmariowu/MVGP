"""apilio images: the owner's chosen channel for every asset image.

`gpt-image-2.5-sunburst` through apilio's OpenAI-compatible endpoints: `generations` (JSON) without references,
`edits` (multipart, one repeated `image` field per reference, in the frozen order) with them. The call is
synchronous: the answer is the image URL (2026-09-25 probe: `data[0].url`, host webstatic.aiproxy.vip), which the
worker downloads through the same bounded, host-allowlisted path as every other result. apilio has no job
listing, so a call that times out after sending is an unknown outcome for the operator, never resent and never
closed as absent. The key is read from the environment at call time and never logged or journaled.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from production.contracts import DomainError
from production.provider_types import ProviderReceipt, ResolvedReference

JOB_TYPES = frozenset({'apilio_gpt_image_2_5'})
ENDPOINTS = {'generations': 'https://api.apilio.ai/v1/images/generations', 'edits': 'https://api.apilio.ai/v1/images/edits'}
MEDIA_TYPES = frozenset({'image/png', 'image/jpeg', 'image/webp'})
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
# (method, url, headers, json_body, form, files, timeout) -> (status, bytes); injected in tests and rehearsals.
Transport = Callable[[str, str, dict[str, str], Any, Any, Any, float], tuple[int, bytes]]


def _http(method: str, url: str, headers: dict[str, str], json_body: Any, form: Any, files: Any, timeout: float) -> tuple[int, bytes]:
    import httpx
    with httpx.Client(follow_redirects=False, timeout=httpx.Timeout(timeout, connect=20)) as client:
        with client.stream(method, url, headers=headers, json=json_body, data=form, files=files) as response:
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    raise DomainError('unknown_outcome', 'apilio response exceeded its bound after the call was sent')
            return response.status_code, bytes(body)


class ApilioImages:
    can_list = False

    def __init__(self, capabilities: Mapping[str, dict[str, Any]], *, transport: Transport | None = None,
                 live_enabled: bool = False, timeout: float = 240, key_env: str = 'APILIO_AI_KEY') -> None:
        if not capabilities or not set(capabilities) <= JOB_TYPES or not 0 < timeout <= 600:
            raise ValueError('Explicit apilio image capabilities and a bounded timeout are required')
        if type(live_enabled) is not bool or (live_enabled and transport is not None):
            raise ValueError('Live enablement cannot be combined with an injected transport')
        self._capabilities = json.loads(json.dumps(dict(capabilities)))
        for name, doc in self._capabilities.items():
            if doc.get('job_type') != name or not isinstance(doc.get('models'), dict) or not isinstance(doc.get('sizes'), dict):
                raise ValueError('apilio capability needs its job type, model per resolution and pixel sizes')
        self.fake, self.transport = transport is not None, transport or _http
        self.live_enabled, self.timeout, self.key_env = live_enabled, float(timeout), key_env

    def capabilities(self, job_type: str) -> dict[str, Any]:
        return json.loads(json.dumps(self._capabilities[job_type]))

    def _isolation(self) -> dict[str, str]:
        return {}

    def _plan(self, request: dict[str, Any]) -> tuple[str, str, str]:
        doc = self._capabilities.get(request.get('job_type'))
        params = request.get('params') if isinstance(request.get('params'), dict) else {}
        prompt = params.get('prompt')
        if doc is None or not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
            raise DomainError('invalid_input', 'apilio image request needs its job type and a bounded prompt')
        if set(params) - {'prompt', 'aspect_ratio', 'resolution'}:
            raise DomainError('invalid_input', 'apilio image request carries settings its route does not take')
        model = doc['models'].get(params.get('resolution'))
        size = doc['sizes'].get(f"{params.get('aspect_ratio')}|{params.get('resolution')}")
        if not isinstance(model, str) or not isinstance(size, str) or not re.fullmatch(r'\d{3,4}x\d{3,4}', size):
            raise DomainError('unsupported_route', 'apilio image aspect/resolution has no pinned model and size')
        return prompt, model, size

    def _parameters(self, request: dict[str, Any], *, has_references: bool) -> list[str]:
        self._plan(request)
        return []

    def _references(self, request: dict[str, Any], resolved: list[ResolvedReference]) -> list[str]:
        if len(resolved) > 4:
            raise DomainError('invalid_input', 'apilio image route takes at most four references')
        paths = []
        for ref in resolved:
            data = Path(ref.path).read_bytes()
            if ref.media_type not in MEDIA_TYPES or hashlib.sha256(data).hexdigest() != ref.sha256:
                raise DomainError('invalid_media', 'A reference image differs from its recorded bytes')
            paths.append(str(ref.path))
        return paths

    def submit(self, intent: dict[str, Any], resolved: list[ResolvedReference], *,
               evidence_sink: Callable[[dict[str, Any]], None] | None = None) -> ProviderReceipt:
        if not self.fake and not self.live_enabled:
            raise DomainError('unsupported_route', 'Live apilio images require explicit service enablement; no request sent')
        if intent.get('operation') != 'submit' or intent.get('cost', {}).get('mode') != ('fake' if self.fake else 'live'):
            raise DomainError('forbidden', 'Adapter mode differs from service-authorized intent')
        request = intent['request']
        prompt, model, size = self._plan(request)
        self._references(request, resolved)
        key = 'fake' if self.fake else os.environ.get(self.key_env, '')
        if not key:
            raise DomainError('missing_prerequisite', 'The apilio key is not in the worker environment; no request sent')
        headers = {'Authorization': f'Bearer {key}'}
        try:
            if resolved:
                files = [('image', (f'reference-{n}{Path(r.path).suffix or ".png"}', Path(r.path).read_bytes(), r.media_type))
                         for n, r in enumerate(resolved, 1)]
                status, body = self.transport('POST', ENDPOINTS['edits'], headers, None,
                                              {'model': model, 'prompt': prompt, 'size': size, 'n': '1'}, files, self.timeout)
            else:
                status, body = self.transport('POST', ENDPOINTS['generations'], headers,
                                              {'model': model, 'prompt': prompt, 'size': size, 'n': 1}, None, None, self.timeout)
        except DomainError:
            raise
        except Exception as exc:  # noqa: BLE001 -- the paid call may have run; never resend, the operator settles it.
            raise DomainError('unknown_outcome', 'apilio image call did not answer after sending; no automatic retry') from exc
        if evidence_sink is not None:
            evidence_sink({'provider': 'apilio', 'status': status, 'bytes': len(body),
                           'sha256': hashlib.sha256(body).hexdigest(), 'body': body[:65536].decode('utf-8', 'replace')})
        if status >= 500:
            raise DomainError('unknown_outcome', f'apilio answered {status}; the image may still be charged; no automatic retry')
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise DomainError('unknown_outcome', 'apilio answered with unreadable data; no automatic retry') from exc
        if status >= 400 or data.get('error'):
            message = str((data.get('error') or {}).get('message') or f'HTTP {status}')[:300]
            return ProviderReceipt(f'apilio-refused-{hashlib.sha256(body).hexdigest()[:24]}', request['job_type'], 'refused',
                                   'failed', {'error': message}, {}, {})
        first = (data.get('data') or [{}])[0] if isinstance(data.get('data'), list) else {}
        url = first.get('url') if isinstance(first, dict) else None
        parsed = urlsplit(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise DomainError('unknown_outcome', 'apilio answered without a usable image URL; no automatic retry')
        job_id = 'apilio-' + hashlib.sha256(url.encode()).hexdigest()[:32]
        return ProviderReceipt(job_id, request['job_type'], 'completed', 'succeeded',
                               {'model': data.get('model'), 'usage': data.get('usage'), 'created': data.get('created'),
                                'result_host': parsed.hostname}, {}, {}, result_url=url,
                               settled_cost=self._quota(request['job_type'], data.get('usage')))

    def _quota(self, job_type: str, usage: Any) -> int | None:
        """apilio quota = (input + output x completion_ratio) x model_ratio, from the pinned ratios and the answer's
        own token counts (2026-09-25 probe: 35 in, 1105 out -> 16663 for one 2048x1152 image)."""
        ratios = self._capabilities[job_type].get('quota') or {}
        model, completion = ratios.get('model_ratio'), ratios.get('completion_ratio')
        tokens_in = usage.get('input_tokens') if isinstance(usage, dict) else None
        tokens_out = usage.get('output_tokens') if isinstance(usage, dict) else None
        if not all(type(v) in (int, float) and v > 0 for v in (model, completion)) or not all(
                type(v) is int and v >= 0 for v in (tokens_in, tokens_out)):
            return None  # the hold stays until the operator settles it
        return round((tokens_in + tokens_out * completion) * model)

    def get(self, job_id: str, expected_request: dict[str, Any], **_: Any) -> ProviderReceipt:
        # A synchronous call has no job to ask about later; a job left mid-call belongs to the operator.
        raise DomainError('unknown_outcome', 'apilio image calls cannot be polled; the operator settles this job')

    def list_recent(self, job_type: str) -> list[dict[str, Any]]:
        raise DomainError('unsupported_route', 'apilio has no job listing; the operator settles unknown image jobs')
