"""One provider interface for the worker, routed by the job type of each request.

Higgsfield (the dormant fallback), apilio images and fal Seedance drafts each implement the worker's provider
calls: `_parameters`, `_references`, `_isolation`, `submit`, `get`, `list_recent`. A job always goes to the
adapter that owns its job type, so a job made on one provider is never polled or settled on another. An
adapter that cannot list its jobs (`can_list` false) leaves an unknown outcome to the operator; it is never
closed as absent.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from production.contracts import DomainError


class ProviderRouter:
    def __init__(self, adapters: Mapping[str, Any]) -> None:
        if not adapters:
            raise ValueError('At least one provider adapter is required')
        modes = {bool(a.fake) for a in adapters.values()}
        if len(modes) != 1:
            raise ValueError('Fake and live adapters cannot share one worker')
        self.adapters = dict(adapters)
        self.fake = modes.pop()
        self.live_enabled = all(bool(getattr(a, 'live_enabled', False)) for a in adapters.values())
        self.timeout = max(float(a.timeout) for a in adapters.values())

    def _for(self, job_type: Any) -> Any:
        adapter = self.adapters.get(job_type) if isinstance(job_type, str) else None
        if adapter is None:
            raise DomainError('unsupported_route', 'No provider adapter is configured for this job type')
        return adapter

    def _parameters(self, request: dict[str, Any], *, has_references: bool) -> list[str]:
        return self._for(request.get('job_type'))._parameters(request, has_references=has_references)

    def _references(self, request: dict[str, Any], resolved: list[Any]) -> list[str]:
        return self._for(request.get('job_type'))._references(request, resolved)

    def _isolation(self) -> dict[str, str]:
        for adapter in {id(a): a for a in self.adapters.values()}.values():
            adapter._isolation()
        return {}

    def submit(self, intent: dict[str, Any], resolved: list[Any], **kwargs: Any) -> Any:
        return self._for(intent.get('request', {}).get('job_type')).submit(intent, resolved, **kwargs)

    def get(self, job_id: str, expected_request: dict[str, Any], **kwargs: Any) -> Any:
        return self._for(expected_request.get('job_type')).get(job_id, expected_request, **kwargs)

    def can_list(self, job_type: Any) -> bool:
        adapter = self.adapters.get(job_type) if isinstance(job_type, str) else None
        return adapter is not None and bool(getattr(adapter, 'can_list', True))

    def list_recent(self, job_type: str) -> list[dict[str, Any]]:
        if not self.can_list(job_type):
            raise DomainError('unsupported_route', 'This provider cannot list its jobs; the operator settles them')
        return self._for(job_type).list_recent(job_type)

    def capabilities(self, job_type: str) -> dict[str, Any]:
        return self._for(job_type).capabilities(job_type)
