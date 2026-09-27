"""Atomic groups of unified submissions; no provider or separate reservation path.

BatchRequest lists immutable candidates, one independent take per list entry.
Repeating one candidate four times expresses the library's one-version/four-take
rule (legacy --takes=4). Each candidate may appear at most four times per request;
reruns retain that candidate version's released attempt allowance. A new candidate
version gets a fresh allowance. HF does not impose a universal batch count.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Any

from production.auth import AuthService, Principal
from production.contracts import (
    BatchRequest,
    DomainError,
    ObjectRef,
    SelectTakeRequest,
    SubmitRequest,
    new_id,
)
from production.reviews import Reviews
from production.store import Store
from production.submissions import Submissions
from production.workflow import Workflow

SERVICE = 'batch_service'
SUPPORT = {
    'one_take_per_candidate': False, 'max_candidates': 100,
    'within_request_repeated_candidate_supported': True,
    'count_authority': 'Explicit candidate list, at most four entries per candidate version, released per-candidate attempt limits and separate native-account spending envelopes.',
    'resolution_authority': 'Exact compiled candidate and its released route; batch cannot override resolution.',
    'legacy_difference': 'Legacy --takes=4 maps to four repeated candidate IDs. This API has no take-count field; reruns retain the same candidate version allowance.',
}


def _ref(obj: dict[str, Any]) -> dict[str, Any]:
    return {k: obj[k] for k in ('object_id', 'revision', 'digest')}


class Batches:
    def __init__(self, store: Store, auth: AuthService, workflow: Workflow,
                 submissions: Submissions, reviews: Reviews) -> None:
        if (submissions.store is not store or submissions.auth is not auth or submissions.workflow is not workflow
                or reviews.store is not store or reviews.auth is not auth or reviews.workflow is not workflow):
            raise ValueError('Batch services must share transactions, credentials and workflow authority')
        self.store, self.auth, self.workflow = store, auth, workflow
        self.submissions, self.reviews = submissions, reviews

    def create(self, actor: Principal, pid: str, request: BatchRequest) -> dict[str, Any]:
        with self.store.transaction() as db:
            self.auth.authorize(actor, pid, 'batch', conn=db)
            self.auth.authorize(actor, pid, 'submit', conn=db)
            def enqueue(conn: sqlite3.Connection) -> dict[str, Any]:
                project = self.store.get_object(pid, pid, conn=conn)
                if project['revision'] != request.expected_revision:
                    raise DomainError('revision_conflict', 'Batch project revision changed', current_revision=project['revision'])
                planned = []
                buckets: dict[str, dict[str, Any]] = {}
                batch_id = new_id('batch')
                prior = self.store.list_objects(pid, kind='dispatch-intent', conn=conn)
                for index, oid in enumerate(request.candidate_ids):
                    candidate = self.store.get_object(pid, oid, conn=conn)
                    if candidate['kind'] != 'candidate' or candidate['author'] != 'compiler_service' or candidate['revision'] != 1:
                        raise DomainError('forbidden', 'Batch children must be immutable compiler candidates')
                    # Both preflight and final enqueue use the unified service;
                    # the batch never supplies a reviewer, verdict or raw prompt.
                    self.submissions._stress_tested(pid, candidate, conn)
                    data = self.submissions._generation(pid, candidate, conn)
                    cost = self.submissions._policy('submit', data['route_key'])
                    if data['release_id'] != project['body']['release_id']:
                        raise DomainError('release_mismatch', 'Batch child release differs')
                    key = cost['budget_key']
                    if key not in buckets:
                        budget = self.store.budget(pid, budget_key=key, conn=conn)
                        buckets[key] = {'budget_key':key, 'unit':budget['unit'], 'reservation_total':0,
                                        'available_before':budget['ceiling']-budget['spent']-budget['reserved']}
                    if cost['budget_unit'] != buckets[key]['unit']:
                        raise DomainError('release_mismatch', 'Batch child native account unit differs')
                    buckets[key]['reservation_total'] += cost['reservation']
                    previous = [p for p in prior if p['author'] == 'submission_service'
                                and p['body'].get('operation') == 'submit'
                                and p['body'].get('target', {}).get('object_id') == data['target']['object_id']]
                    relation = 'rerun' if any(p['body'].get('candidate') == _ref(candidate) for p in previous) else 'changed-candidate' if previous else 'initial'
                    planned.append({'candidate': _ref(candidate), 'target': data['target'], 'resolution': data['request']['params']['resolution'],
                        'route_key': data['route_key'], 'method_id': data['method_id'], 'review': data['review'],
                        'cost': cost, 'relation': relation, 'previous_attempts': [_ref(p) for p in previous],
                        'idempotency_key': f'{batch_id}-{index}'})
                if any(bucket['reservation_total'] > bucket['available_before'] for bucket in buckets.values()):
                    raise DomainError('budget_exceeded', 'Entire batch exceeds an unreserved native account budget')
                decision: dict[str, Any] = {'buckets':[buckets[key] for key in sorted(buckets)]}
                # Preserve the historical single-legacy projection only. A native
                # account, even alone, must not become an unlabelled global total.
                if set(buckets) == {'legacy'}:
                    decision.update(buckets['legacy'])
                body = {'release_id': project['body']['release_id'], 'support': SUPPORT,
                    'children': planned, 'count': len(planned), 'budget_decision': decision,
                    'state': 'authorized', 'accepted': False, 'dependencies': [p['candidate'] for p in planned]}
                # The complete authorization manifest precedes every child intent
                # in this transaction. Any failure rolls back it and all children.
                batch = self.store.create_object(pid, 'batch', body, SERVICE, object_id=batch_id, conn=conn)
                children = []
                for item in planned:
                    child = self.submissions.submit(actor, pid, SubmitRequest(idempotency_key=item['idempotency_key'],
                        expected_revision=item['candidate']['revision'], candidate_id=item['candidate']['object_id']), conn=conn,
                        batch=batch_id)
                    children.append({**item, 'job': _ref(child), 'reservation_id': child['body']['reservation_id']})
                return self.store.append_revision(pid, batch_id, batch['revision'],
                    {**body, 'children': children, 'state': 'queued'}, SERVICE, conn=conn)
            return self.store.run_idempotent(f'{pid}:{actor.credential_id}:batch', request.idempotency_key,
                request.model_dump(), enqueue, conn=db)

    def _batch(self, pid: str, oid: str, db: sqlite3.Connection) -> dict[str, Any]:
        batch = self.store.get_object(pid, oid, conn=db)
        if batch['kind'] != 'batch' or batch['author'] != SERVICE or batch['body'].get('state') != 'queued':
            raise DomainError('forbidden', 'Expected a service-issued complete batch manifest')
        return batch

    def _children(self, pid: str, batch: dict[str, Any], db: sqlite3.Connection) -> list[dict[str, Any]]:
        children = []
        for item in batch['body']['children']:
            original = self.store.get_object(pid, item['job']['object_id'], revision=item['job']['revision'], conn=db)
            job = self.store.get_object(pid, original['object_id'], conn=db)
            if original['digest'] != item['job']['digest'] or job['author'] not in ('submission_service', 'worker_service'):
                raise DomainError('forbidden', 'Batch child job provenance differs')
            intent_ref = job['body']['intent']
            intent = self.store.get_object(pid, intent_ref['object_id'], revision=intent_ref['revision'], conn=db)
            if (intent['digest'] != intent_ref['digest'] or intent['author'] != 'submission_service'
                    or intent['body'].get('candidate') != item['candidate']):
                raise DomainError('stale_input', 'Child no longer names its authorized candidate')
            children.append({'candidate': item['candidate'], 'target': item['target'], 'job': _ref(job),
                'attempt': intent['body']['attempt'], 'dispatch_intent': _ref(intent), 'resolution': item['resolution'],
                'relation': item['relation'], 'state': job['body']['state'], 'result': job['body'].get('result'),
                'current': job['body'].get('current'), 'last_error': job['body'].get('last_error')})
        return children

    def get(self, actor: Principal, pid: str, batch_id: str) -> dict[str, Any]:
        with self.store.transaction(write=False) as db:
            self.auth.authorize(actor, pid, 'jobs', conn=db)
            batch = self._batch(pid, batch_id, db)
            children = self._children(pid, batch, db)
            counts = Counter(c['state'] for c in children)
            terminal = sum(counts.get(s, 0) for s in ('succeeded', 'failed', 'cancelled')) == len(children)
            status = 'succeeded' if counts.get('succeeded') == len(children) else 'partial' if counts.get('succeeded') else 'failed' if terminal else 'pending'
            graph = self.workflow.pinned_graph(pid, ObjectRef(**_ref(batch)), conn=db)
            return {'batch': _ref(batch), 'support': SUPPORT, 'status': status, 'terminal': terminal,
                'counts': dict(counts), 'children': children, 'budget_decision': batch['body']['budget_decision'],
                'stale': graph['stale'], 'accepted': False}

    def _member(self, pid: str, batch: dict[str, Any], take: ObjectRef, db: sqlite3.Connection) -> dict[str, Any]:
        matches = [c for c in self._children(pid, batch, db) if c['state'] == 'succeeded' and c['current'] is not False
                   and c['result'] == take.model_dump()]
        if len(matches) != 1:
            raise DomainError('invalid_input', 'Select one exact successful take from this batch')
        if self.workflow.pinned_graph(pid, take, conn=db)['stale']:
            raise DomainError('stale_input', 'Batch take is no longer current')
        return matches[0]

    def select_take(self, actor: Principal, pid: str, batch_id: str, request: SelectTakeRequest) -> dict[str, Any]:
        # The immutable membership check cannot select for the user. Reviews owns
        # its own atomic current-take/shot/lock checks and selection idempotency.
        with self.store.transaction(write=False) as db:
            self.auth.authorize(actor, pid, 'select-take', conn=db)
            member = self._member(pid, self._batch(pid, batch_id, db), request.take, db)
            if member['target'] != request.shot.model_dump():
                raise DomainError('invalid_input', 'Selected shot differs from the batch child target')
        return self.reviews.select_take(actor, pid, request)

    def delivery_lineage(self, actor: Principal, pid: str, batch_ref: ObjectRef,
                         selection_ref: ObjectRef, media_ref: ObjectRef | None = None) -> dict[str, Any]:
        """Read exact source identity; neither upscale execution nor quality approval."""
        with self.store.transaction(write=False) as db:
            self.auth.authorize(actor, pid, 'references', conn=db)
            batch = self._batch(pid, batch_ref.object_id, db)
            if _ref(batch) != batch_ref.model_dump():
                raise DomainError('stale_input', 'Batch manifest version differs')
            selection = self.store.get_object(pid, selection_ref.object_id, revision=selection_ref.revision, conn=db)
            if (selection['kind'] != 'take-selection' or selection['digest'] != selection_ref.digest
                    or self.workflow.pinned_graph(pid, selection_ref, conn=db)['stale']):
                raise DomainError('stale_input', 'Selection is absent, changed or stale')
            take = ObjectRef.model_validate(selection['body']['take'])
            member = self._member(pid, batch, take, db)
            if selection['body']['shot'] != member['target']:
                raise DomainError('invalid_input', 'Selection does not name this batch target')
            final = media_ref or take
            current = final
            chain = []
            seen = set()
            while True:
                media = self.store.get_object(pid, current.object_id, revision=current.revision, conn=db)
                if media['kind'] != 'media' or media['digest'] != current.digest:
                    raise DomainError('invalid_media', 'Delivery media fingerprint differs')
                self.reviews.media.path_for(pid, current.object_id, revision=current.revision)
                if self.workflow.pinned_graph(pid, current, conn=db)['stale']:
                    raise DomainError('stale_input', 'Delivery media changed')
                chain.append(_ref(media))
                if current == take:
                    break
                key = (current.object_id, current.revision)
                if key in seen or len(chain) >= 16 or not media['body'].get('derivative_of'):
                    raise DomainError('invalid_media', 'Delivery must derive from the selected take; fresh generation is not an upscale')
                seen.add(key)
                current = ObjectRef.model_validate(media['body']['derivative_of'])
            return {'batch': _ref(batch), 'selection': _ref(selection), 'selected_take': take.model_dump(),
                'delivery_media': final.model_dump(), 'candidate': member['candidate'], 'job': member['job'],
                'derivative_chain': chain, 'identity_lineage_verified': True,
                'artistic_equivalence_verified': False, 'finishing_verified': False, 'accepted': False}
