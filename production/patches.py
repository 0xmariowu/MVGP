"""Base-bound creative repairs; exact scope is recorded without rewriting takes.

A path is a list of literal JSON member names or canonical array indices, rooted
at content. Existing values are replaced; replace a containing block to explicitly
add/remove members. Root replacement is broader restaging. Prompts, routes, rules,
receipts and generated media are never patch targets. The compiler still decides
whether revised creative inputs can prepare a candidate; a patch is not approval.
"""
from __future__ import annotations

import copy
import re
import sqlite3
from typing import Any

from pydantic import ValidationError

from production.auth import AuthService, Principal
from production.compiler import VideoSettings
from production.contracts import (
    ArtifactRevision,
    DomainError,
    ObjectRef,
    PatchRequest,
    content_hash,
)
from production.projects import Projects
from production.store import Store
from production.workflow import Workflow

SERVICE_AUTHOR = 'patch_service'
CREATIVE_KINDS = frozenset({'brief', 'script', 'scene', 'shot', 'asset', 'source-understanding',
                           'expectation', 'method', 'feedback', 'finishing'})
SHOT_BLOCKS = frozenset({'shot', 'The material', 'Direction', 'Camera', 'Edit', 'ACTING TASK',
                         'Audio', '_production'})


def _ref(obj: dict[str, Any]) -> dict[str, Any]:
    return {key: obj[key] for key in ('object_id', 'revision', 'digest')}


def _part(container: Any, part: str) -> str | int:
    if isinstance(container, dict) and part in container:
        return part
    if isinstance(container, list) and re.fullmatch(r'0|[1-9][0-9]*', part):
        index = int(part)
        if index < len(container):
            return index
    raise DomainError('invalid_input', 'Creative path must name an existing member', field='creative_path',
                      repair='Inspect the exact revision; replace a containing block to add or remove members')


def _replace(content: Any, parts: list[str], value: Any) -> tuple[Any, Any]:
    if any(p in ('.', '..') or '\\' in p or '\x00' in p for p in parts):
        raise DomainError('invalid_input', 'Invalid creative path segment', field='creative_path')
    changed = copy.deepcopy(content)
    if not parts:
        return copy.deepcopy(value), copy.deepcopy(content)
    parent = changed
    for part in parts[:-1]:
        parent = parent[_part(parent, part)]
    key = _part(parent, parts[-1])
    old = copy.deepcopy(parent[key])
    parent[key] = copy.deepcopy(value)
    return changed, old


def _exact(store: Store, pid: str, ref: ObjectRef, conn: sqlite3.Connection) -> dict[str, Any]:
    if ref.digest is None:
        raise DomainError('invalid_input', 'Repair evidence requires an exact immutable fingerprint')
    obj = store.get_object(pid, ref.object_id, revision=ref.revision, conn=conn)
    if obj['digest'] != ref.digest:
        raise DomainError('stale_input', 'Repair evidence fingerprint does not match')
    return obj


def _video_take(store: Store, workflow: Workflow, pid: str, ref: ObjectRef,
                expected: dict[str, Any], conn: sqlite3.Connection) -> dict[str, Any]:
    obj = _exact(store, pid, ref, conn)
    if (obj['kind'] != 'media' or obj['author'] != 'worker_service'
            or not obj['body'].get('media_type', '').startswith('video/')
            or obj['body'].get('probe', {}).get('has_video') is not True):
        raise DomainError('invalid_media', 'Pickup repair requires actual service-generated video')
    workflow.pinned_graph(pid, ref, conn=conn)  # Validate history without requiring the failed base to remain current.
    origin = workflow.media_origin(pid, ref, conn=conn)
    if origin['kind'] != 'candidate' or origin['authority']['body'].get('target') != expected:
        raise DomainError('invalid_input', 'Failed/returned take producer must target the exact repair revision')
    return obj


def _pickup_source(store: Store, workflow: Workflow, pid: str, ref: ObjectRef,
                   base: dict[str, Any], conn: sqlite3.Connection) -> tuple[dict[str, Any], dict[str, Any]]:
    pickup = _exact(store, pid, ref, conn)
    body = pickup['body']
    if (pickup['kind'] != 'pickup' or pickup['author'] != 'review_service' or pickup['revision'] != 1
            or body.get('status') != 'unresolved'):
        raise DomainError('forbidden', 'Repair source must be an immutable unresolved service pickup')
    try:
        receipt = _exact(store, pid, ObjectRef.model_validate(body['receipt']), conn)
        take_ref = ObjectRef.model_validate(body['target'])
    except (KeyError, ValidationError) as exc:
        raise DomainError('invalid_input', 'Pickup evidence bindings are incomplete') from exc
    if (receipt['kind'] != 'review-receipt' or receipt['author'] != 'review_service' or receipt['revision'] != 1
            or receipt['body'].get('verdict') not in ('fail', 'uncertain')
            or receipt['body'].get('target') != take_ref.model_dump()
            or _ref(receipt) not in body.get('dependencies', [])
            or take_ref.model_dump() not in body.get('dependencies', [])
            or take_ref.model_dump() not in receipt['body'].get('dependencies', [])):
        raise DomainError('forbidden', 'Pickup requires an exact independent failed/uncertain receipt on its failed take')
    return pickup, _video_take(store, workflow, pid, take_ref, base, conn)


def _bound_lineage(store: Store, workflow: Workflow, pid: str, repair: dict[str, Any],
                   returned: ObjectRef, conn: sqlite3.Connection) -> dict[str, Any]:
    body = repair['body']
    pickup, failed = _pickup_source(store, workflow, pid, ObjectRef.model_validate(body['source_pickup']), body['base'], conn)
    if body.get('failed_take') != _ref(failed):
        raise DomainError('invalid_input', 'Repair failed evidence differs from its source pickup')
    take = _video_take(store, workflow, pid, returned, body['result'], conn)
    lineage = {'repair': _ref(repair), 'source_pickup': _ref(pickup), 'failed_take': _ref(failed),
               'returned_take': _ref(take), 'accepted': False,
               'dependencies': [_ref(repair), _ref(take)]}
    # Historical source/failed refs remain immutable evidence, not freshness roots.
    return store.run_idempotent(f'{pid}:repair-lineage', content_hash(lineage), lineage,
        lambda db: store.create_object(pid, 'repair-lineage', lineage, SERVICE_AUTHOR, conn=db), conn=conn)


def link_generated_repairs(store: Store, workflow: Workflow, pid: str, returned: ObjectRef,
                           *, conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Mandatory worker-publication hook; exact links only, never pickup closure.

    Run inside the media/job publication transaction. Nothing calls a provider or
    accepts an author verdict. Failed historical evidence cannot stale new drafts.
    """
    media = _exact(store, pid, returned, conn)
    if not media['body'].get('media_type', '').startswith('video/'):
        return []
    origin = workflow.media_origin(pid, returned, conn=conn)
    if origin['kind'] != 'candidate':
        return []
    target = origin['authority']['body'].get('target')
    repairs = [r for r in store.list_objects(pid, kind='repair', conn=conn)
               if r['author'] == SERVICE_AUTHOR and r['revision'] == 1
               and r['body'].get('source_pickup') and r['body'].get('result') == target]
    return [_bound_lineage(store, workflow, pid, repair, returned, conn) for repair in repairs]


class Patches:
    def __init__(self, store: Store, auth: AuthService, projects: Projects, workflow: Workflow) -> None:
        self.store, self.auth, self.projects, self.workflow = store, auth, projects, workflow

    def _resolve(self, pid: str, ref: ObjectRef, conn: sqlite3.Connection) -> dict[str, Any]:
        obj = self.store.get_object(pid, ref.object_id, revision=ref.revision, conn=conn)
        if ref.digest is not None and ref.digest != obj['digest']:
            raise DomainError('stale_input', 'Repair reference digest does not match its immutable revision')
        return obj

    @staticmethod
    def _creative(kind: str, before: Any, after: Any) -> None:
        if kind != 'shot':
            return
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise DomainError('invalid_input', 'Shot repairs preserve the authored card structure')
        changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
        if changed - SHOT_BLOCKS:
            raise DomainError('invalid_input', 'Only authored shot blocks may change; constants use their governing scene or asset',
                              repair='Patch the creative scene/asset input, then prepare a new candidate')
        if '_production' in after and before.get('_production') != after['_production']:
            try:
                VideoSettings.model_validate(after['_production'])
            except ValidationError as exc:
                raise DomainError('invalid_input', 'Production controls accept only the published creative settings', field='content._production') from exc

    def apply(self, actor: Principal, project_id: str, request: PatchRequest) -> dict[str, Any]:
        """Atomically revise one creative scope and append its immutable repair record.

        No lock is automatically reopened. Typed references are regenerated while
        independent dependency roots are preserved. Exact downstream graphs become
        stale without rewriting their candidates, reviews or historical results.
        """
        with self.store.transaction() as db:
            self.auth.authorize(actor, project_id, 'patch', conn=db)
            def save(conn: sqlite3.Connection) -> dict[str, Any]:
                base = self._resolve(project_id, request.target, conn)
                current = self.store.get_object(project_id, request.target.object_id, conn=conn)
                if current['revision'] != request.target.revision or request.expected_revision != current['revision']:
                    raise DomainError('revision_conflict', 'Repair base changed', current_revision=current['revision'])
                if base['kind'] not in CREATIVE_KINDS or 'logical_path' not in base['body']:
                    raise DomainError('invalid_input', 'Repair target must be a versioned creative artifact')
                self.workflow.guard_mutation(self.store, project_id, base['object_id'], conn)
                evidence: dict[str, Any] = {}
                if request.source_pickup is not None:
                    pickup, failed = _pickup_source(self.store, self.workflow, project_id, request.source_pickup, _ref(base), conn)
                    self.projects.media.path_for(project_id, failed['object_id'], revision=failed['revision'])
                    evidence = {'source_pickup': _ref(pickup), 'failed_take': _ref(failed)}
                before = base['body']['content']
                changed, old_value = _replace(before, request.creative_path[1:], request.value)
                if content_hash(changed) == content_hash(before):
                    raise DomainError('invalid_input', 'Repair does not change the selected creative scope')
                self._creative(base['kind'], before, changed)
                common = {'idempotency_key': request.idempotency_key, 'expected_revision': base['revision'],
                          'kind': base['kind'], 'logical_path': base['body']['logical_path']}
                try:
                    old_draft = ArtifactRevision(**common, content=before)
                    draft = ArtifactRevision(**common, content=changed)
                except ValidationError as exc:
                    raise DomainError('invalid_input', 'Repaired content must remain a supported creative draft') from exc
                _, derived = self.projects._content(project_id, old_draft, conn)
                derived_keys = {(r['object_id'], r['revision']) for r in derived}
                dependencies = [ObjectRef.model_validate(r) for r in base['body'].get('dependencies', [])
                                if (r['object_id'], r['revision']) not in derived_keys]
                draft = draft.model_copy(update={'dependencies': dependencies})
                artifact = self.projects._write(actor, project_id, draft, base['object_id'], conn)
                pointer = '/' + '/'.join(part.replace('~', '~0').replace('/', '~1') for part in request.creative_path)
                repair = self.store.create_object(project_id, 'repair', {
                    'base': _ref(base), 'result': _ref(artifact), **evidence, 'observed_defect': request.reason,
                    'creative_path': request.creative_path, 'scope': 'restaging' if len(request.creative_path) == 1 else 'section',
                    'desired_change': request.value, 'diff': [{'op': 'replace', 'path': pointer, 'before': old_value, 'after': request.value}],
                    'requested_by': actor.actor_id, 'accepted': False,
                    'dependencies': [_ref(artifact)],
                }, SERVICE_AUTHOR, conn=conn)
                return {'artifact': artifact, 'repair': repair}
            return self.store.run_idempotent(f'{actor.actor_id}:{actor.credential_id}:{project_id}:patch',
                                             request.idempotency_key, request.model_dump(), save, conn=db)

    def _take(self, pid: str, take: ObjectRef, expected: dict[str, Any], conn: sqlite3.Connection) -> dict[str, Any]:
        obj = self._resolve(pid, take, conn)
        if obj['kind'] != 'media' or obj['author'] != 'worker_service':
            raise DomainError('invalid_input', 'Repair take must be a service-generated media result')
        graph = self.workflow.pinned_graph(pid, ObjectRef(**_ref(obj)), conn=conn)
        origin = self.workflow.media_origin(pid, ObjectRef(**_ref(obj)), conn=conn)
        # Historical failure remains usable even when its graph is now stale.
        if origin['kind'] != 'candidate':
            raise DomainError('invalid_input', 'Take requires a direct compiled producer')
        candidate = origin['authority']
        if candidate['body'].get('target') != expected or not any(n['ref'] == expected for n in graph['nodes']):
            raise DomainError('invalid_input', 'Take candidate does not derive from the required repair revision')
        return _ref(obj)

    def link_returned_take(self, project_id: str, repair: ObjectRef, failed_take: ObjectRef, returned_take: ObjectRef,
                           *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Worker-only hook; never exposed as an author operation or acceptance.

        Worker output media must carry exact candidate dependency refs and author
        worker_service. Both candidates must bind their exact old/new creative
        target. Multiple returned takes are separate immutable lineage records.
        """
        with self.store._using(conn) as db:
            record = self._resolve(project_id, repair, db)
            if record['kind'] != 'repair' or record['author'] != SERVICE_AUTHOR:
                raise DomainError('invalid_input', 'Expected a service-authored repair record')
            if record['body'].get('source_pickup'):
                if failed_take.model_dump() != record['body'].get('failed_take'):
                    raise DomainError('invalid_input', 'Failed take must be the repair-bound original evidence')
                return _bound_lineage(self.store, self.workflow, project_id, record, returned_take, db)
            failed = self._take(project_id, failed_take, record['body']['base'], db)
            returned = self._take(project_id, returned_take, record['body']['result'], db)
            body = {'repair': _ref(record), 'failed_take': failed, 'returned_take': returned, 'accepted': False,
                    'dependencies': [_ref(record), failed, returned]}
            digest = content_hash(body)
            return self.store.run_idempotent(f'{project_id}:repair-lineage', digest, body,
                lambda active: self.store.create_object(project_id, 'repair-lineage', body, SERVICE_AUTHOR, conn=active), conn=db)
