"""One-time config migration for the lean platform.

Reads the live api/worker configs and the store (read-only) and writes the new configs to a separate output
folder, never over the inputs:
- api: `personal` + `owner_member_ids` become `owner` {issuer, audience, subjects of the owner's member records};
  `hidden_projects` = every production project minus those with an enabled owner membership, so the owner's
  desk shows exactly what it shows today.
- worker: the keys of removed features (review_enabled, composition_enabled, listen_command) are dropped; `hf`
  stays (Higgsfield is the shooting route again).
- both: the release keys (release_id, deployment_root, runtime_files, storage.activation_revision) are dropped;
  runtime.json in the code dir replaces the release.
Both outputs are validated against the code's own config models before anything is written.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ''):  # run as a script: import the repo this file lives in
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from studio.idle_check import connect_ro  # noqa: E402

SYSTEM_PROJECT = 'platform_system'
WORKER_DROPPED = ('review_enabled', 'composition_enabled', 'listen_command')
RELEASE_KEYS = ('release_id', 'deployment_root', 'runtime_files')


def _current(conn: sqlite3.Connection, project: str, kind: str) -> list[tuple[str, dict[str, Any]]]:
    rows = conn.execute(
        "SELECT o.object_id, r.body FROM objects o JOIN revisions r ON o.project_id=r.project_id AND"
        " o.object_id=r.object_id AND o.current_revision=r.revision WHERE o.project_id=? AND o.kind=?", (project, kind))
    return [(oid, json.loads(body)) for oid, body in rows]


def _storage(storage: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in storage.items() if k != 'activation_revision'}


def migrate(api: dict[str, Any], worker: dict[str, Any], conn: sqlite3.Connection) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    personal = api.get('personal')
    owners = list(api.get('owner_member_ids') or [])
    if not personal or not owners:
        raise SystemExit('The live api config has no personal block or owner member ids to migrate from')
    members = dict(_current(conn, SYSTEM_PROJECT, 'member'))
    subjects = []
    for member in owners:
        body = members.get(member)
        if body is None or body.get('enabled') is not True or body.get('issuer') != personal['issuer']:
            raise SystemExit(f'Owner member {member[:14]}… has no enabled member record from the configured issuer')
        subjects.append(body['subject'])
    visible = {body['project_id'] for _, body in _current(conn, SYSTEM_PROJECT, 'project-membership')
               if body.get('member_id') in owners and body.get('enabled') is True}
    projects = [p for (p,) in conn.execute('SELECT project_id FROM projects ORDER BY project_id') if p != SYSTEM_PROJECT]
    hidden = [p for p in projects if p not in visible]
    new_api = {k: v for k, v in api.items() if k not in ('personal', 'owner_member_ids', *RELEASE_KEYS)}
    new_api['storage'] = _storage(api['storage'])
    new_api['owner'] = {'issuer': personal['issuer'], 'audience': personal['audience'], 'subjects': sorted(set(subjects))}
    new_api['hidden_projects'] = hidden
    new_worker = {k: v for k, v in worker.items() if k not in (*WORKER_DROPPED, *RELEASE_KEYS)}
    new_worker['storage'] = _storage(worker['storage'])
    report = {'projects': len(projects), 'visible_before': sorted(visible), 'hidden': len(hidden),
              'owner_subjects': len(set(subjects)), 'worker_keys_dropped': [k for k in WORKER_DROPPED if k in worker],
              'release_keys_dropped': sorted({k for k in RELEASE_KEYS if k in api or k in worker}
                                             | ({'storage.activation_revision'} if 'activation_revision' in api['storage'] else set()))}
    return new_api, new_worker, report


def validate(api: dict[str, Any], worker: dict[str, Any]) -> None:
    from production.server import ServerConfiguration
    from production.worker import WorkerConfiguration
    ServerConfiguration.model_validate(api)
    WorkerConfiguration.model_validate(worker)


def _write(path: Path, value: dict[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=1, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--api', required=True)
    ap.add_argument('--worker', required=True)
    ap.add_argument('--db', required=True)
    ap.add_argument('--out-dir', required=True)
    args = ap.parse_args(argv)
    out = Path(args.out_dir).resolve()
    if out in {Path(args.api).resolve().parent, Path(args.worker).resolve().parent}:
        raise SystemExit('Write the migrated configs to a separate folder, never over the live ones')
    conn = connect_ro(args.db)
    try:
        api, worker, report = migrate(json.load(open(args.api)), json.load(open(args.worker)), conn)
    finally:
        conn.close()
    validate(api, worker)
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write(out / 'api.json', api)
    _write(out / 'worker.json', worker)
    json.dump(report, sys.stdout, indent=1)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
