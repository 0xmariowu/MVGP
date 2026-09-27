"""The platform's one runtime config: `production/config/runtime.json`, tracked in git.

It replaces the release snapshots. It holds the cost policy, the image and video routes with their provider
capabilities, the cut policy, the reader's observer profile, the enabled methods and the reference binding mode.
It is read once at start; a change ships as a code deploy. The platform uses only its packaged runtime configuration.
Stored records keep their old `release_id` as a label; the frozen cost of every queued intent is still compared
with this cost policy at dispatch, so a price change never re-prices queued work.
"""
from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from production.contracts import DomainError

CODE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = CODE_ROOT / 'production' / 'config' / 'runtime.json'
REQUIRED = frozenset({'label', 'binding', 'cost', 'capabilities', 'routes', 'cut_policy', 'reader', 'methods'})
MAX_BYTES = 4 * 1024 * 1024


class RuntimeConfig:
    def __init__(self, data: Mapping[str, Any], *, code_root: Path = CODE_ROOT) -> None:
        if not isinstance(data, Mapping) or not REQUIRED <= data.keys():
            raise ValueError('Runtime config lacks a required section')
        if data['binding'] not in ('first', 'every') or not isinstance(data['label'], str) or not data['label']:
            raise ValueError('Runtime config binding must be first or every, with a label')
        if not all(isinstance(data[key], dict) for key in REQUIRED - {'label', 'binding'}):
            raise ValueError('Runtime config sections must be JSON objects')
        self._data = copy.deepcopy(dict(data))
        self.code_root = Path(code_root)

    @classmethod
    def load(cls, path: Path = DEFAULT_PATH, **kwargs: Any) -> RuntimeConfig:
        raw = Path(path).read_bytes()
        if len(raw) > MAX_BYTES:
            raise ValueError('Runtime config is too large')
        return cls(json.loads(raw), **kwargs)

    @property
    def label(self) -> str:
        return str(self._data['label'])

    @property
    def binding(self) -> str:
        return str(self._data['binding'])

    def _slot(self, role: str) -> tuple[dict[str, Any], str]:
        if role == 'execution_policy':
            return self._data, 'cost'
        if role == 'cut_policy':
            return self._data, 'cut_policy'
        if role == 'review_routes':
            return self._data, 'reader'
        if role in ('image_routes', 'video_routes'):
            return self._data['routes'], role
        return self._data['capabilities'], role

    def section(self, role: str) -> dict[str, Any]:
        """A copy of one document, by its old release role name."""
        if role == 'methods':
            return {'methods': copy.deepcopy(self._data['methods'])}
        holder, key = self._slot(role)
        if key not in holder:
            raise DomainError('not_found', 'Runtime config section is absent')
        return copy.deepcopy(holder[key])

    def document(self, role: str) -> bytes:
        return json.dumps(self.section(role), ensure_ascii=False).encode('utf-8')

    def set(self, role: str, value: Mapping[str, Any]) -> None:
        """Replace one document (tests and rehearsal copies; production edits the file and redeploys)."""
        if role == 'methods':
            self._data['methods'] = copy.deepcopy(dict(value['methods']))
            return
        holder, key = self._slot(role)
        holder[key] = copy.deepcopy(dict(value))

    def require_method(self, method_id: str, *, task: str | None = None) -> dict[str, Any]:
        method = self._data['methods'].get(method_id)
        if not isinstance(method, dict):
            raise DomainError('unsupported_method', 'Method is not enabled in the runtime config')
        if task is not None and task not in {method.get('task'), *method.get('task_aliases', [])}:
            raise DomainError('unsupported_method', 'Method does not support the requested task')
        return copy.deepcopy(method)
