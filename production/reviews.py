"""Version-bound maker reports and selections, never independent acceptance.

Historical feedback remains readable after edits. Current selection is derived
from dependencies at read time; neither a label nor an author's assertion closes
an independent issue. Only the receipt issuer may append its resolution record.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from production.auth import AuthService, Principal
from production.contracts import (
    AuthorReport,
    DomainError,
    FeedbackRequest,
    ObjectRef,
    SelectTakeRequest,
    content_hash,
)
from production.media import MediaStore
from production.store import Store
from production.workflow import Workflow


def _ref(obj: dict[str, Any]) -> dict[str, Any]:
    return {k: obj[k] for k in ('object_id', 'revision', 'digest')}


def repair_comparisons(store: Store, workflow: Workflow, pid: str, returned: ObjectRef,
                       conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Exact service repair facts; historical failure does not grant freshness."""
    def exact(value: dict[str, Any], kind: str, author: str) -> dict[str, Any]:
        ref = ObjectRef.model_validate(value)
        obj = store.get_object(pid, ref.object_id, revision=ref.revision, conn=conn)
        if ref.digest != obj['digest'] or obj['kind'] != kind or obj['author'] != author:
            raise DomainError('invalid_input', 'Repair comparison has an invalid service evidence binding')
        return obj
    result = []
    for lineage in store.list_objects(pid, kind='repair-lineage', conn=conn):
        lb = lineage['body']
        if (lineage['author'] != 'patch_service' or lb.get('returned_take') != returned.model_dump()
                or not lb.get('source_pickup')):
            continue
        repair = exact(lb['repair'], 'repair', 'patch_service')
        rb = repair['body']
        pickup = exact(lb['source_pickup'], 'pickup', 'review_service')
        pb = pickup['body']
        failed = exact(lb['failed_take'], 'media', 'worker_service')
        receipt = exact(pb['receipt'], 'review-receipt', 'review_service')
        if (lineage['revision'] != 1 or repair['revision'] != 1 or pickup['revision'] != 1
                or rb.get('source_pickup') != _ref(pickup) or rb.get('failed_take') != _ref(failed)
                or pb.get('target') != _ref(failed) or receipt['body'].get('target') != _ref(failed)
                or receipt['body'].get('verdict') not in ('fail', 'uncertain')
                or receipt['body'].get('purpose') != 'take' or returned.model_dump() == _ref(failed)
                or not pb.get('expected_information')
                or any(ref not in lb.get('dependencies', []) for ref in (_ref(repair), returned.model_dump()))):
            raise DomainError('invalid_input', 'Repair comparison does not bind the original failed take and finding')
        for media_ref, target in ((ObjectRef(**_ref(failed)), rb['base']), (returned, rb['result'])):
            origin = workflow.media_origin(pid, media_ref, conn=conn)
            if origin['kind'] != 'candidate' or origin['authority']['body']['target'] != target:
                raise DomainError('invalid_input', 'Repair comparison names the wrong producer or creative revision')
        result.append({'source_pickup': _ref(pickup), 'repair': _ref(repair), 'repair_lineage': _ref(lineage),
            'failed_take': _ref(failed), 'returned_take': returned.model_dump(), 'source_receipt': _ref(receipt),
            'defect': {'expected_information': pb['expected_information'], 'playback_seconds': pb.get('playback_seconds'),
                       'evidence': pb.get('evidence', []), 'findings': receipt['body'].get('structured_verdict', {})},
            'repair_intent': {key: rb[key] for key in ('observed_defect', 'creative_path', 'desired_change', 'diff') if key in rb}})
    return sorted(result, key=lambda item: item['repair_lineage']['object_id'])


class Reviews:
    def __init__(self, store: Store, auth: AuthService, workflow: Workflow, media: MediaStore) -> None:
        self.store, self.auth, self.workflow, self.media = store, auth, workflow, media

    def _target(self, pid: str, ref: ObjectRef, expected: int, db: sqlite3.Connection) -> dict[str, Any]:
        obj = self.store.get_object(pid, ref.object_id, revision=ref.revision, conn=db)
        if ref.digest is None or obj['digest'] != ref.digest or expected != ref.revision:
            raise DomainError('stale_input', 'Feedback and review records require the exact target revision and hash')
        return obj

    def _time(self, pid: str, target: dict[str, Any], seconds: float | None) -> None:
        if seconds is None:
            return
        if target['kind'] != 'media':
            raise DomainError('invalid_input', 'Playback feedback must name actual rendered media, not a cut plan')
        self.media.path_for(pid, target['object_id'], revision=target['revision'])
        duration = target['body']['probe'].get('duration')
        if not isinstance(duration, (float, int)) or seconds > duration:
            raise DomainError('invalid_input', 'Feedback timestamp lies outside actual media')

    def feedback(self, actor: Principal, pid: str, request: FeedbackRequest) -> dict[str, Any]:
        with self.store.transaction() as db:
            self.auth.authorize(actor, pid, 'feedback', conn=db)
            def save(conn: sqlite3.Connection) -> dict[str, Any]:
                target = self._target(pid, request.target, request.expected_revision, conn)
                self._time(pid, target, request.playback_seconds)
                return self.store.create_object(pid, 'feedback', {
                    'target': _ref(target), 'content': request.text, 'playback_seconds': request.playback_seconds,
                    'attribution': 'agent-reported-user-feedback', 'reported_by': actor.actor_id,
                    'human_identity_verified': False, 'accepted': False, 'dependencies': [_ref(target)],
                }, actor.actor_id, conn=conn)
            return self.store.run_idempotent(f'{pid}:{actor.credential_id}:feedback', request.idempotency_key,
                                             request.model_dump(), save, conn=db)

    def report(self, actor: Principal, pid: str, request: AuthorReport) -> dict[str, Any]:
        with self.store.transaction() as db:
            self.auth.authorize(actor, pid, 'report', conn=db)
            def save(conn: sqlite3.Connection) -> dict[str, Any]:
                target = self._target(pid, request.target, request.expected_revision, conn)
                evidence = []
                for ref in request.evidence:
                    obj = self._target(pid, ref, ref.revision, conn)
                    if obj['kind'] == 'media':
                        self.media.path_for(pid, obj['object_id'], revision=obj['revision'])
                    evidence.append(_ref(obj))
                return self.store.create_object(pid, 'agent-report', {
                    'target': _ref(target), 'observation': request.observation, 'evidence': evidence,
                    'attribution': 'maker-assessment', 'accepted': False,
                    'dependencies': [_ref(target), *evidence],
                }, actor.actor_id, conn=conn)
            return self.store.run_idempotent(f'{pid}:{actor.credential_id}:report', request.idempotency_key,
                                             request.model_dump(), save, conn=db)

    def select_take(self, actor: Principal, pid: str, request: SelectTakeRequest) -> dict[str, Any]:
        with self.store.transaction() as db:
            self.auth.authorize(actor, pid, 'select-take', conn=db)
            def save(conn: sqlite3.Connection) -> dict[str, Any]:
                shot = self._target(pid, request.shot, request.shot.revision, conn)
                take = self._target(pid, request.take, request.take.revision, conn)
                if self.store.get_object(pid, shot['object_id'], conn=conn)['revision'] != shot['revision']:
                    raise DomainError('stale_input', 'Take selection must name the current shot intent')
                if shot['kind'] != 'shot' or take['kind'] != 'media' or not take['body']['probe'].get('has_video'):
                    raise DomainError('invalid_input', 'Select a real video take for a shot')
                self.workflow.guard_mutation(self.store, pid, shot['object_id'], conn)
                graph = self.workflow.pinned_graph(pid, ObjectRef(**_ref(take)), conn=conn)
                if graph['stale'] or _ref(shot) not in [n['ref'] for n in graph['nodes']]:
                    raise DomainError('stale_input', 'Take must derive from the current shot; historical output remains viewable')
                origin = self.workflow.media_origin(pid, ObjectRef(**_ref(take)), conn=conn)
                if (take['author'] != 'worker_service' or origin['kind'] != 'candidate'
                        or origin['authority']['body']['target'] != _ref(shot)):
                    raise DomainError('invalid_input', 'Selection needs exact service generation lineage')
                self.media.path_for(pid, take['object_id'], revision=take['revision'])
                existing = [s for s in self.store.list_objects(pid, kind='take-selection', conn=conn)
                            if s['body']['shot']['object_id'] == shot['object_id']]
                guard = existing[0]['revision'] if existing else shot['revision']
                if request.expected_revision != guard:
                    raise DomainError('revision_conflict', 'Take selection changed', current_revision=guard)
                body: dict[str, Any] = {'shot': _ref(shot), 'take': _ref(take), 'rationale': request.rationale,
                        'attribution': 'maker-selection', 'accepted': False,
                        'dependencies': [_ref(shot), _ref(take)]}
                if existing:
                    old = existing[0]
                    return self.store.append_revision(pid, old['object_id'], old['revision'], body, actor.actor_id, conn=conn)
                return self.store.create_object(pid, 'take-selection', body, actor.actor_id, conn=conn)
            return self.store.run_idempotent(f'{pid}:{actor.credential_id}:select-take', request.idempotency_key,
                                             request.model_dump(), save, conn=db)

    def record_pickup(self, pid: str, receipt_ref: ObjectRef, *, expected_information: str,
                      evidence: list[ObjectRef], playback_seconds: float | None = None,
                      conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Internal receipt-issuer hook, not an author operation.

        Keep the actual cut/media target and exact independent finding. Recording
        a returned repair later adds evidence; only a new receipt resolves it.
        """
        with self.store._using(conn) as db:
            receipt = self._target(pid, receipt_ref, receipt_ref.revision, db)
            if (receipt['kind'] != 'review-receipt' or receipt['author'] != 'review_service'
                    or receipt['body'].get('verdict') not in ('fail', 'uncertain')):
                raise DomainError('forbidden', 'Pickup requires a service-issued unresolved review receipt')
            if not expected_information.strip() or not evidence:
                raise DomainError('invalid_input', 'Pickup requires concrete expected audience information and evidence')
            target_ref = ObjectRef.model_validate(receipt['body']['target'])
            target = self._target(pid, target_ref, target_ref.revision, db)
            self._time(pid, target, playback_seconds)
            refs = [_ref(self._target(pid, ref, ref.revision, db)) for ref in evidence]
            body = {'receipt': _ref(receipt), 'target': _ref(target), 'playback_seconds': playback_seconds,
                    'expected_information': expected_information, 'evidence': refs, 'status': 'unresolved',
                    'dependencies': [_ref(receipt), _ref(target), *refs]}
            return self.store.run_idempotent(f'{pid}:pickup', content_hash(body), body,
                lambda active: self.store.create_object(pid, 'pickup', body, 'review_service', conn=active), conn=db)

    def list(self, actor: Principal, pid: str) -> list[dict[str, Any]]:
        with self.store.transaction(write=False) as db:
            self.auth.authorize(actor, pid, 'reviews', conn=db)
            kinds = {'feedback', 'agent-report', 'observation', 'take-selection', 'review-receipt', 'pickup', 'repair-lineage', 'pickup-resolution'}
            results = []
            for obj in self.store.list_objects(pid, conn=db):
                if obj['kind'] not in kinds:
                    continue
                if obj['kind'] == 'pickup-resolution':
                    results.append({'object': obj, 'current': False, 'current_unverified': True,
                                    'stale_reasons': [{'code': 'review_required'}], 'human_accepted': False})
                    continue  # Queries has the current-receipt checker; this list is historical evidence.
                graph = self.workflow.pinned_graph(pid, ObjectRef(**_ref(obj)), conn=db)
                results.append({'object': obj, 'current': not graph['stale'], 'stale_reasons': graph['reasons'],
                                'human_accepted': False})
            return results
