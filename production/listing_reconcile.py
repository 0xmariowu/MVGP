"""Settle a timed-out native create from the provider's own job listing.

A create whose CLI call timed out has an unknown outcome: the request may or may not have reached
Higgsfield. The CLI is killed at its timeout (never above MAX_CREATE_SECONDS), so a create that did
arrive is listed with `created_at` inside [attempt - LEAD, attempt + MAX_CREATE_SECONDS + TAIL].
Measured 2026-09-24 on 42 live takes: listed 11-67 s after the attempt record.

Only an exact match (same model, prompt and duration) that no platform job owns can be adopted. A
listing captured after the window closed and reaching back past its start, with no such match,
proves the create never arrived; the job then closes as `provider_absent`. Nothing is ever
resubmitted and no billing record changes here.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from production.auth import SYSTEM_PROJECT
from production.contracts import DomainError
from production.store import Store

LEAD_SECONDS = 60
MAX_CREATE_SECONDS = 120  # HFProvider refuses a longer CLI timeout.
TAIL_SECONDS = 60


def now() -> datetime:
    return datetime.now(UTC)


def timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(UTC)


def window(attempt_created_at: str) -> tuple[datetime, datetime]:
    t = timestamp(attempt_created_at)
    return t - timedelta(seconds=LEAD_SECONDS), t + timedelta(seconds=MAX_CREATE_SECONDS + TAIL_SECONDS)


def listed_jobs(raw: Any) -> list[dict[str, Any]]:
    """The comparable fields of a `generate list --json` page; prompts are kept only as hashes."""
    items = raw.get('items') if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise DomainError('provider_failure', 'Provider listing has no job list')
    jobs = []
    for item in items:
        params = item.get('params') if isinstance(item, dict) else None
        if not isinstance(params, dict) or not all(isinstance(item.get(k), str) for k in ('id', 'created_at', 'job_type')):
            raise DomainError('provider_failure', 'Provider listing item is malformed')
        prompt = params.get('prompt') if isinstance(params.get('prompt'), str) else ''
        duration = params.get('duration')
        jobs.append({'id': item['id'], 'created_at': item['created_at'], 'job_type': item['job_type'],
                     'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                     'duration': duration if isinstance(duration, int) and not isinstance(duration, bool) and duration > 0 else None})
    return jobs


def inspect(store: Store, pid: str, job_id: str, db: sqlite3.Connection, *, cost_mode: str = 'live') -> dict[str, Any]:
    """The exact inactive unknown native create, its attempt, intent and listing window, or refuse."""
    current = store.get_object(pid, job_id, conn=db)
    jb = current['body']
    if current['kind'] != 'job' or not jb.get('attempt_id'):
        raise DomainError('forbidden', 'Only an inactive exact unknown native generation may be reconciled')
    actual = store.get_object(pid, jb['attempt_id'], conn=db)
    ab = actual['body']
    intent = store.get_object(pid, (ab.get('intent') or {}).get('object_id', ''), conn=db) if ab.get('intent') else None
    if (intent is None or current['author'] != 'worker_service' or jb.get('state') != 'unknown'
            or jb.get('lease') is not None or jb.get('remote_job_id') or jb.get('pending_result')
            or jb.get('last_receipt') or jb.get('result')
            or actual['kind'] != 'provider-attempt' or actual['author'] != 'worker_service' or actual['revision'] != 1
            or ab.get('job_id') != current['object_id'] or ab.get('intent') != jb.get('intent')
            or intent['kind'] != 'dispatch-intent' or intent['author'] != 'submission_service'
            or ab.get('intent') != _ref(intent) or intent['revision'] != 1
            or ab.get('request') != intent['body'].get('request')
            or intent['body'].get('operation') != 'submit'
            or intent['body'].get('cost', {}).get('mode') != cost_mode):
        raise DomainError('forbidden', 'Only an inactive exact unknown native generation may be reconciled')
    request = intent['body'].get('request') or {}
    params = request.get('params') or {}
    if not isinstance(params.get('prompt'), str):
        raise DomainError('forbidden', 'Only a prompted native generation can be matched against a listing')
    reservation = db.execute('SELECT * FROM reservations WHERE project_id=? AND reservation_id=?',
                             (pid, jb.get('reservation_id'))).fetchone()
    if not reservation or reservation['state'] != 'unknown' or reservation['object_id'] != intent['object_id']:
        raise DomainError('revision_conflict', 'Retain the unknown accounting hold before reconciliation')
    start, end = window(actual['created_at'])
    return {'job': current, 'attempt': actual, 'intent': intent, 'start': start, 'end': end,
            'job_type': request.get('job_type'), 'prompt_sha256': hashlib.sha256(params['prompt'].encode()).hexdigest(),
            'duration': params.get('duration') or None}


def matches(seen: dict[str, Any], listed: list[dict[str, Any]]) -> set[str]:
    return {item['id'] for item in listed if seen['start'] <= timestamp(item['created_at']) <= seen['end']
            and item['job_type'] == seen['job_type'] and item['prompt_sha256'] == seen['prompt_sha256']
            and item['duration'] == seen['duration']}


def covers(seen: dict[str, Any], listed: list[dict[str, Any]], captured_at: str) -> bool:
    """The page was captured after the window closed and reaches back past its start."""
    return timestamp(captured_at) >= seen['end'] and any(timestamp(item['created_at']) < seen['start'] for item in listed)


def owned(store: Store, db: sqlite3.Connection, *, except_job: str) -> set[str]:
    """Provider job ids any platform job already holds, across every project on this store."""
    ids = set()
    for (pid,) in db.execute('SELECT project_id FROM projects').fetchall():
        for job in store.list_objects(pid, kind='job', conn=db):
            if job['object_id'] != except_job and job['body'].get('remote_job_id'):
                ids.add(job['body']['remote_job_id'])
    return ids


def rivals(store: Store, pid: str, seen: dict[str, Any], db: sqlite3.Connection) -> dict[str, int]:
    """Other jobs, in any project on this store, creating the same prompt whose provider job is not yet known.

    Sibling takes share one prompt, so while one of them is mid-create (or also unknown) a listed
    match could be its job rather than this one. `pid` is this job's project.
    """
    counts = {'creating': 0, 'unknown': 0}
    projects = [row[0] for row in db.execute('SELECT project_id FROM projects').fetchall()]
    for other, job in ((p, j) for p in projects for j in store.list_objects(p, kind='job', conn=db)):
        body = job['body']
        if job['object_id'] == seen['job']['object_id'] or body.get('remote_job_id'):
            continue
        state = body.get('state')
        if state not in ('dispatching', 'unknown'):
            continue
        try:
            intent = store.get_object(other, body['intent']['object_id'], conn=db)
        except (DomainError, KeyError, TypeError):
            continue
        request = intent['body'].get('request') or {}
        prompt = (request.get('params') or {}).get('prompt')
        if (isinstance(prompt, str) and request.get('job_type') == seen['job_type']
                and hashlib.sha256(prompt.encode()).hexdigest() == seen['prompt_sha256']):
            counts['creating' if state == 'dispatching' else 'unknown'] += 1
    return counts


def decide(seen: dict[str, Any], listed: list[dict[str, Any]], captured_at: str, taken: set[str],
           rival: dict[str, int]) -> tuple[str, str | None]:
    """('adopt', id) | ('absent', None) | ('wait', why) | ('operator', why) for the automatic path."""
    if rival['creating']:
        return 'wait', 'a sibling with the same prompt is still creating'
    unowned = matches(seen, listed) - taken
    if len(unowned) > 1 or unowned and rival['unknown']:
        return 'operator', 'more than one unknown create could own the listed job'
    if unowned:
        return 'adopt', next(iter(unowned))
    if covers(seen, listed, captured_at):
        return 'absent', None
    if timestamp(captured_at) >= seen['end']:
        return 'operator', 'the listing page does not reach back past the attempt window'
    return 'wait', 'the attempt window is still open'


def apply(store: Store, pid: str, seen: dict[str, Any], *, remote_job_id: str | None, unowned: set[str],
          evidence_sha256: str, reason: str, author: str, db: sqlite3.Connection,
          listing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Record the reconciliation and close (absent) or re-arm (adopted) the job."""
    current, actual = seen['job'], seen['attempt']
    record = store.create_object(SYSTEM_PROJECT, 'generation-provider-reconciliation', {
        'project_id': pid, 'job': _ref(current), 'attempt': _ref(actual),
        'evidence_sha256': evidence_sha256, 'window': {'start': seen['start'].isoformat(), 'end': seen['end'].isoformat()},
        'remote_job_id': remote_job_id, 'unowned_matches': sorted(unowned), 'reason': reason,
        'billing_changed': False, 'automatic_retry': False, **({'listing': listing} if listing else {})}, author, conn=db)
    marker = _ref(record)
    if remote_job_id is None:
        changes = {'state': 'failed', 'last_error': 'provider_absent', 'provider_reconciliation': marker}
    else:
        changes = {'remote_job_id': remote_job_id, 'next_poll_at': 0, 'provider_reconciliation': marker}
    updated = store.append_revision(pid, current['object_id'], current['revision'], {**current['body'], **changes},
                                    current['author'], conn=db)
    return {'job': _ref(updated), 'state': updated['body']['state'], 'remote_job_id': remote_job_id,
            'billing_changed': False, 'automatic_retry': False, 'evidence': marker}


def _ref(obj: dict[str, Any]) -> dict[str, Any]:
    return {k: obj[k] for k in ('object_id', 'revision', 'digest')}


__all__ = ['LEAD_SECONDS', 'MAX_CREATE_SECONDS', 'TAIL_SECONDS', 'apply', 'covers', 'decide', 'inspect',
           'listed_jobs', 'matches', 'now', 'owned', 'rivals', 'timestamp', 'window']
