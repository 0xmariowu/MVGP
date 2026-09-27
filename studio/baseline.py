"""Read-only baseline of what must never change across a deploy.

Per project: count and content digest of the owner's records (picks, receipts, cuts, finals, picture locks,
notes); reservations by budget key and state; budgets; and the set of projects the owner's desk shows.
`compare` checks two baselines on the projects that existed in the first one only, so rehearsal and loop
projects created later never count as a difference.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from typing import Any

try:
    from studio.idle_check import LIVE_DB, STUDIO, connect_ro
except ImportError:  # run as a script from studio/
    from idle_check import LIVE_DB, STUDIO, connect_ro  # type: ignore[no-redef]

SYSTEM_PROJECT = 'platform_system'
OWNER_KINDS = ('human-take-selection', 'human-receipt', 'cut', 'final', 'picture-lock', 'owner-note')


def _visible_by_members(conn: sqlite3.Connection, members: list[str]) -> list[str]:
    """Today's rule (production/auth.py:300-318): a member session sees projects with an enabled membership."""
    rows = conn.execute(
        "SELECT r.body FROM objects o JOIN revisions r ON o.project_id=r.project_id AND o.object_id=r.object_id"
        " AND o.current_revision=r.revision WHERE o.project_id=? AND o.kind='project-membership'"
        " AND r.author='member_project_service'", (SYSTEM_PROJECT,))
    seen = set()
    for (raw,) in rows:
        body = json.loads(raw)
        if body.get('member_id') in members and body.get('enabled') is True:
            seen.add(body['project_id'])
    return sorted(seen)


def _visible_by_hidden(conn: sqlite3.Connection, hidden: list[str]) -> list[str]:
    """Lean rule: the owner sees every production project except `hidden_projects`."""
    return sorted(p for (p,) in conn.execute('SELECT project_id FROM projects') if p != SYSTEM_PROJECT and p not in hidden)


def take(conn: sqlite3.Connection, config: dict[str, Any]) -> dict[str, Any]:
    projects: dict[str, Any] = {}
    for (pid,) in conn.execute('SELECT project_id FROM projects ORDER BY project_id'):
        if pid == SYSTEM_PROJECT:
            continue
        entry: dict[str, Any] = {'records': {}}
        for kind in OWNER_KINDS:
            rows = conn.execute(
                "SELECT o.object_id, o.current_revision, r.digest FROM objects o JOIN revisions r"
                " ON o.project_id=r.project_id AND o.object_id=r.object_id AND o.current_revision=r.revision"
                " WHERE o.project_id=? AND o.kind=? ORDER BY o.object_id", (pid, kind)).fetchall()
            digest = hashlib.sha256(json.dumps(rows).encode()).hexdigest()
            entry['records'][kind] = {'count': len(rows), 'digest': digest}
        entry['budgets'] = [list(r) for r in conn.execute(
            'SELECT budget_key, unit, ceiling, reserved, spent FROM budgets WHERE project_id=? ORDER BY budget_key', (pid,))]
        entry['reservations'] = [list(r) for r in conn.execute(
            'SELECT budget_key, state, count(*), sum(amount), sum(coalesce(actual,0)) FROM reservations'
            ' WHERE project_id=? GROUP BY budget_key, state ORDER BY budget_key, state', (pid,))]
        projects[pid] = entry
    if 'hidden_projects' in config:
        visible = _visible_by_hidden(conn, list(config['hidden_projects']))
        rule = 'hidden_projects'
    else:
        visible = _visible_by_members(conn, list(config.get('owner_member_ids') or []))
        rule = 'enabled_memberships'
    return {'projects': projects, 'visible': visible, 'visible_rule': rule}


def compare(before: dict[str, Any], after: dict[str, Any], *, ignore_reservations: set[str] = frozenset()) -> list[str]:
    """Differences on the projects that existed in `before`. Budget keys in `ignore_reservations` (intended
    settlements) are skipped for reservations and budgets only; the owner's records are always compared."""
    diffs: list[str] = []
    for pid, b in before['projects'].items():
        a = after['projects'].get(pid)
        if a is None:
            diffs.append(f'{pid}: project missing')
            continue
        for kind, rec in b['records'].items():
            if a['records'].get(kind) != rec:
                diffs.append(f'{pid}: {kind} changed {rec} -> {a["records"].get(kind)}')
        for field in ('budgets', 'reservations'):
            keep = lambda rows: [r for r in rows if r[0] not in ignore_reservations]  # noqa: E731
            if keep(a[field]) != keep(b[field]):
                diffs.append(f'{pid}: {field} changed')
    old_visible = set(before['visible'])
    new_visible = {p for p in after['visible'] if p in before['projects']}
    if old_visible != new_visible:
        diffs.append(f'visible set changed: +{sorted(new_visible - old_visible)} -{sorted(old_visible - new_visible)}')
    return diffs


def _path(db: str) -> str:
    """Accept a plain path or a read-only `file:` URI and return the file path; connect_ro picks the mode."""
    return db[len('file:'):].split('?', 1)[0] if db.startswith('file:') else db


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ['compare']:
        ap = argparse.ArgumentParser(prog='baseline.py compare')
        ap.add_argument('before')
        ap.add_argument('after')
        ap.add_argument('--ignore-budget-key', action='append', default=[])
        args = ap.parse_args(argv[1:])
        diffs = compare(json.load(open(args.before)), json.load(open(args.after)),
                        ignore_reservations=set(args.ignore_budget_key))
        print('\n'.join(diffs) if diffs else 'equal')
        return 1 if diffs else 0
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--db', default=LIVE_DB, help='database path or read-only file: URI')
    ap.add_argument('--config', default=str(STUDIO / 'config/live/api.json'))
    args = ap.parse_args(argv)
    config = json.load(open(args.config))
    conn = connect_ro(_path(args.db))
    try:
        json.dump(take(conn, config), sys.stdout, indent=1, sort_keys=True)
    finally:
        conn.close()
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
