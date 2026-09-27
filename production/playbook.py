"""The writer's manuals, served with a version.

Higgsfield attaches its skills to the project and Claude writes every prompt with them
(briefs-clean/cully-hill-boys.txt:22-27, hell-grind.txt:61-66). Here the same manuals ship inside the
release under `production/playbooks/`; `manifest.json` pins each copy's sha256 and names its source. The
version is the manifest's hash, so any change to any manual is a new version.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from production.contracts import DomainError

ROOT = Path(__file__).resolve().parent / 'playbooks'


def manifest() -> dict[str, Any]:
    return json.loads((ROOT / 'manifest.json').read_text())


def version() -> str:
    return 'pb-' + hashlib.sha256((ROOT / 'manifest.json').read_bytes()).hexdigest()[:12]


def files() -> list[dict[str, str]]:
    """Every manual with its text; a copy that differs from the manifest is refused, never served."""
    out = []
    for row in manifest()['files']:
        data = (ROOT / row['name']).read_bytes()
        if hashlib.sha256(data).hexdigest() != row['sha256']:
            raise DomainError('release_mismatch', f"Manual {row['name']} differs from the playbook manifest")
        out.append({'name': row['name'], 'purpose': row['purpose'], 'sha256': row['sha256'], 'text': data.decode()})
    return out


FETCHED = 'playbook.fetched'


def hand_over(store: Any, principal: Any, pid: str, *, conn: Any) -> dict[str, Any]:
    """Serve the manuals and record who took which version: the server's own proof that they were handed over
    (owner 2026-09-24 "别嘴上说交了，结果没交出去")."""
    current, served = version(), files()
    if principal.role == 'agent':  # only a writer's fetch is evidence; viewers may read the manuals without a record
        store.append_event(pid, FETCHED, {'version': current, 'actor_id': principal.actor_id,
                                          'credential_id': principal.credential_id, 'role': principal.role}, conn=conn)
    return {'version': current, 'files': served}


def fetched(store: Any, pid: str, credential_id: str, wanted: str, *, conn: Any) -> bool:
    """Whether this credential took this version of the manuals in this project."""
    row = conn.execute("SELECT 1 FROM events WHERE project_id=? AND kind=? AND json_extract(body,'$.credential_id')=? "
                       "AND json_extract(body,'$.version')=? LIMIT 1", (pid, FETCHED, credential_id, wanted)).fetchone()
    return row is not None
