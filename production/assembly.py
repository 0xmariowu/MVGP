"""Assemble the owner's current picks into one cut request.

Read-only. The film is every shot that has a live take request, in shot-label order, each shot's latest human
pick at its full length with its own sound. Cuts.create still validates lineage, freshness and bounds.

A picked fal draft (480p) plays as its 1080p completion once that exists. While the
completion is queued or being made, or not yet ordered inside the draft's seven days, the shot is `completing`
and the film waits; a failed completion or an expired draft plays the draft.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from production.contracts import DomainError, content_hash
from production.queries import creation_order, current_picks
from production.store import Store

LABEL = re.compile(r'^S(\d{1,3})-(\d{3})([A-Z]?)$')


def _ref(obj: dict[str, Any]) -> dict[str, Any]:
    return {key: obj[key] for key in ('object_id', 'revision', 'digest')}


def is_probe(shot: dict[str, Any]) -> bool:
    """A stress-test card: an S01-901A-style number or a card filed under probes/ (AGENT_GUIDE)."""
    content = shot['body'].get('content')
    label = content.get('shot') if isinstance(content, dict) else None
    return bool(isinstance(label, str) and re.search(r'-9\d\d[A-Z]$', label)) or '/probes/' in '/' + str(shot['body'].get('logical_path') or '')


def _label(shot: dict[str, Any]) -> tuple[str, tuple[int, int, str]]:
    content = shot['body'].get('content')
    label = content.get('shot') if isinstance(content, dict) else None
    match = LABEL.fullmatch(label) if isinstance(label, str) else None
    if match is None:
        raise DomainError('invalid_input', 'Every shot in the film needs a shot label like S01-010A to be put in order')
    return str(label), (int(match[1]), int(match[2]), str(match[3]))


def owner_picks(store: Store, pid: str, *, conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """The owner's current pick per shot id (latest verified human selection; a withdrawal has no take)."""
    human = [o for o in store.list_objects(pid, kind='human-take-selection', conn=conn)
             if o['author'] == 'decision_service' and o['body'].get('verified_human_session') is True]
    order = creation_order(conn, pid, [o['object_id'] for o in human])
    return current_picks([{'details': {'shot': o['body'].get('shot'), 'take': o['body'].get('take') or {}}}
                          for o in sorted(human, key=lambda o: order[o['object_id']])])


MAKING = frozenset({'queued', 'dispatching', 'submitted', 'running', 'unknown'})


def completions(store: Store, pid: str, *, conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Per draft take id: the state of its 1080p completion job and the completion take once it exists."""
    targets = {i['object_id']: i['body']['target']['object_id'] for i in store.list_objects(pid, kind='dispatch-intent', conn=conn)
               if i['author'] == 'submission_service' and i['body'].get('operation') == 'complete-draft'}
    found: dict[str, dict[str, Any]] = {}
    for job in store.list_objects(pid, kind='job', conn=conn):
        draft = targets.get((job['body'].get('intent') or {}).get('object_id'))
        attempt = job['body'].get('attempt') or 1
        # a completion may be tried again; the latest attempt is the take's state.
        if draft is not None and attempt >= found.get(draft, {}).get('attempt', 0):
            found[draft] = {'state': job['body'].get('state'), 'result': job['body'].get('result'), 'attempt': attempt}
    return found


def assemble(store: Store, pid: str, *, conn: sqlite3.Connection, now: float | None = None) -> dict[str, Any]:
    requests = [o for o in store.list_objects(pid, kind='decision-request', conn=conn)
                if o['author'] == 'decision_service' and o['body'].get('purpose') == 'take'
                and o['body'].get('state') != 'superseded']
    offered: dict[str, list[dict[str, Any]]] = {}
    for request in requests:
        offered.setdefault(request['body']['target']['object_id'], []).extend(request['body']['evidence'].get('takes', []))
    # Stress-test cards are tests, never film shots (HF keeps them in their own test folder; dry run: a stress
    # order's desk offer made the film wait for a pick of the test card). Same rule as the project page's 测试.
    shots = [s for s in (store.get_object(pid, shot_id, conn=conn) for shot_id in offered) if not is_probe(s)]
    ordered = sorted(((shot, *_label(shot)) for shot in shots), key=lambda row: row[2])
    labels = [label for _, label, _ in ordered]
    if len(set(labels)) != len(labels):
        raise DomainError('invalid_input', 'Two shots in the film share one shot label')
    picks = owner_picks(store, pid, conn=conn)
    completed = completions(store, pid, conn=conn)
    segments, missing, named, completing = [], [], [], []
    for shot, label, _ in ordered:
        pick = picks.get(shot['object_id'])
        if pick is None:
            # No pick or a withdrawn one. A pick made before the card was edited still stands: HF plays the owner's
            # picks until he picks again (dry run: every live pick predated a card edit, so no film was cut).
            missing.append(label)
            continue
        take = pick['details']['take']
        media = store.get_object(pid, take['object_id'], revision=take['revision'], conn=conn)
        duration = media['body'].get('probe', {}).get('duration')
        if media['kind'] != 'media' or media['digest'] != take['digest'] or not isinstance(duration, (int, float)):
            missing.append(label)
            continue
        output = (media['body'].get('provenance') or {}).get('provider_output') or {}
        if output.get('draft_id'):
            completion = completed.get(take['object_id'])
            result = (completion or {}).get('result')
            if completion and completion['state'] == 'succeeded' and result:
                full = store.get_object(pid, result['object_id'], revision=result['revision'], conn=conn)
                if full['kind'] == 'media' and full['body'].get('completes') == take:
                    take, duration = result, full['body'].get('probe', {}).get('duration', duration)
            elif (completion and completion['state'] in MAKING) or (
                    completion is None and (now is None or output.get('draft_expires_at', 0) > now)):
                completing.append(label)
                continue
        segments.append({'take': take, 'start_seconds': 0.0, 'end_seconds': float(duration)})
        picked = pick['details']['take']
        number = next((n for n, t in enumerate(offered[shot['object_id']], 1) if t == picked), None)
        named.append(f'{label} take {number}' if number else label)
    complete = bool(ordered) and not missing and not completing
    return {'complete': complete, 'missing': missing, 'completing': completing,
            'segments': segments if complete else [], 'sound_inputs': [],
            'intent': 'Owner picks in shot order: ' + ', '.join(named) + '.',
            'picks_hash': content_hash({'segments': segments, 'missing': missing})}
