"""Read-only check that nothing is in flight before a restart or deploy.

Mirrors the drain rule in production/operations.py `_drained`: a job, review or composition that has not
reached a terminal state is busy, and so is any `held` reservation or an open shoot-order card. A job left
`unknown` that the worker will not touch again is parked: no remote id and no pending result, or its status
reads or downloads used up (production/jobs.py MAX_POLLS, max_downloads 3). It is reported but does not make the
platform busy. Exit 0 = idle, 3 = busy.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

STUDIO = Path(os.environ.get('MVGP_STUDIO') or '/Users/Shared/mvgp-studio').expanduser()
LIVE_DB = str(STUDIO / 'state/live/metadata.sqlite')


def connect_ro(path: str) -> sqlite3.Connection:
    """Read-only connection to a WAL database. `mode=ro` needs the -shm file, which SQLite deletes when the
    last writer closes; then the -wal is gone too, every page is in the main file, and `immutable=1` is exact."""
    if Path(path + '-wal').exists():
        return sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    return sqlite3.connect(f'file:{path}?immutable=1', uri=True)

TERMINAL = {
    'job': {'succeeded', 'failed', 'cancelled'},
    'review-task': {'completed', 'failed', 'cancelled'},
    'review-run': {'completed', 'failed', 'cancelled'},
    'review-turn': {'received', 'tools_completed', 'protocol_repair', 'failed', 'cancelled'},
    'composition-job': {'succeeded', 'failed', 'cancelled'},
}
OPEN_ORDER_STAGES = {'ordering', 'firing', 'listening'}
MAX_POLLS, MAX_DOWNLOADS = 20, 3  # production/jobs.py defaults; this script runs without the platform code


def check(conn: sqlite3.Connection) -> dict[str, Any]:
    busy: list[dict[str, Any]] = []
    parked: list[dict[str, Any]] = []
    rows = conn.execute(
        "SELECT o.project_id, o.object_id, o.kind, r.body FROM objects o JOIN revisions r"
        " ON o.project_id=r.project_id AND o.object_id=r.object_id AND o.current_revision=r.revision"
        " WHERE o.kind IN ('job','review-task','review-run','review-turn','composition-job','shoot-order')")
    for pid, oid, kind, raw in rows:
        body = json.loads(raw)
        item = {'project_id': pid, 'object_id': oid, 'kind': kind}
        if kind == 'shoot-order':
            stages = [c.get('stage') for c in body.get('cards', [])]
            if any(s in OPEN_ORDER_STAGES for s in stages):
                busy.append({**item, 'stages': stages})
            continue
        state = body.get('status' if kind == 'review-turn' else 'state')
        if state in TERMINAL[kind]:
            continue
        if kind == 'job' and state == 'unknown' and (
                not body.get('remote_job_id') and not body.get('pending_result')
                or body.get('pending_result') and body.get('download_count', 0) >= MAX_DOWNLOADS
                or body.get('remote_job_id') and not body.get('pending_result') and body.get('poll_count', 0) >= MAX_POLLS):
            parked.append({**item, 'state': state})
            continue
        busy.append({**item, 'state': state})
    held = [dict(zip(('project_id', 'reservation_id', 'budget_key', 'amount'), r)) for r in conn.execute(
        "SELECT project_id, reservation_id, budget_key, amount FROM reservations WHERE state='held'")]
    return {'idle': not busy and not held, 'busy': busy, 'held': held, 'parked': parked}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db', default=LIVE_DB, help='database file path')
    args = ap.parse_args(argv)
    conn = connect_ro(args.db)
    try:
        result = check(conn)
    finally:
        conn.close()
    json.dump(result, sys.stdout, indent=1)
    sys.stdout.write('\n')
    return 0 if result['idle'] else 3


if __name__ == '__main__':
    raise SystemExit(main())
