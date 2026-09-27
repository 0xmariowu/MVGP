"""Read-only, version-bound author context (no rule catalog).

No model call occurs here. Scene/ending prose remains intact; missing material and
unknown edit order are explicit. Context delivery is not evidence of comprehension.
"""
from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from production.auth import AuthService, Principal
from production.contracts import DomainError, ObjectRef, canonical_json, content_hash
from production.queries import _clean
from production.store import Store
from production.workflow import Workflow


def _ref(obj: dict[str, Any]) -> dict[str, Any]:
    return {key: obj[key] for key in ('object_id', 'revision', 'digest')}


# Creative prose is preserved verbatim. Machine records expose only the facts
# needed to make/review the next version, never wire transcripts or lease data.
CREATIVE_KINDS = frozenset({'brief', 'script', 'scene', 'shot', 'asset', 'expectation',
    'source-understanding', 'method', 'finishing', 'feedback', 'agent-report', 'lesson-proposal'})
MACHINE_FIELDS = {
    'project': ('title', 'brief', 'branch', 'release_id', 'release_hash'),
    'review-context': ('target', 'purpose', 'role', 'release_id', 'method_id', 'method_selection',
                       'route_profile_id', 'route_profile_hash', 'context_hash'),
    'review-turn': ('task_id', 'role', 'status', 'number', 'context_hash', 'request_sha256',
                    'profile_id', 'route_profile_hash', 'failure', 'cost_status'),
    'review-task': ('target', 'manifest', 'purpose', 'role', 'task', 'state', 'verdict', 'receipt_id',
                    'release_id', 'method_id', 'method_selection', 'route_profile_id', 'context_hash', 'attempt'),
    'review-run': ('task_id', 'state', 'started_at', 'tool_calls'),
    'review-tool-read': ('task', 'manifest', 'call_id', 'name', 'status'),
    'dispatch-intent': ('operation', 'target', 'candidate', 'release_id', 'task', 'method_id', 'attempt'),
    'provider-attempt': ('intent', 'job_id'),
    'provider-receipt': ('job_id', 'job_type', 'state', 'attempt', 'critical_adjustments'),
    'cut': ('lineage_target', 'release_id', 'method_id', 'method_selection', 'policy_hash',
            'intent', 'segments', 'sound_inputs', 'duration_seconds', 'sound_mode', 'output',
            'label', 'accepted', 'imported_unverified'),
    'take-selection': ('shot', 'take', 'rationale', 'attribution', 'accepted'),
    'human-take-selection': ('shot', 'take', 'reason', 'human_receipt', 'verified_human_session'),
    'owner-note': ('target', 'take', 'at_seconds', 'text', 'withdrawn', 'verified_human_session'),
    'pickup': ('receipt', 'target', 'playback_seconds', 'expected_information', 'evidence', 'status'),
    'repair': ('base', 'result', 'source_pickup', 'failed_take', 'observed_defect', 'creative_path', 'scope',
               'desired_change', 'diff', 'requested_by', 'accepted'),
    'qualification-set': ('asset', 'samples', 'context_refs', 'previous_set', 'method_id', 'rationale', 'state', 'qualified'),
    'qualification-sample': ('asset', 'asset_references', 'sample', 'sample_sha256', 'context_refs',
                             'release_id', 'method_id', 'task', 'policy_hash'),

    'job': ('state', 'candidate', 'result', 'current', 'non_current_reasons', 'last_error', 'operation', 'cost_status', 'attempt'),
    'media': ('logical_path', 'media_type', 'size', 'sha256', 'probe', 'source_cut', 'derivative_of', 'import_status', 'label', 'current'),
    'take': ('media', 'candidate', 'shot', 'state', 'current'),
    'review': ('target', 'purpose', 'role', 'verdict', 'issues', 'evidence'),
    'review-receipt': ('target', 'purpose', 'role', 'verdict', 'issues', 'advisories', 'evidence', 'structured_verdict',
                       'route_profile_id', 'method_id', 'context_hash', 'release_id', 'consumption'),
    'observation': ('source', 'source_sha256', 'status', 'reader_version', 'route', 'inputs', 'questions',
                    'observations', 'answers', 'uncertainty', 'consumption', 'failure', 'authoritative_review'),
    'candidate': ('target', 'task', 'release_id', 'method_id', 'method_selection', 'context_hash', 'gate_status', 'accepted'),
    'repair-lineage': ('repair', 'source_pickup', 'failed_take', 'returned_take', 'accepted'),
}



RELATED_KINDS = frozenset({'job', 'candidate', 'take', 'media', 'review', 'feedback',
    'agent-report', 'observation', 'review-receipt', 'pickup', 'repair', 'repair-lineage', 'human-take-selection',
    'owner-note'})  # the agent reads the owner's desk notes with the shot's history
CONSUMPTION_SUMMARY = {
    'review-receipt': ('context_hash', 'delivery_mode', 'read_by_model', 'prepared_by_service',
                       'delivered_to_runner', 'delivered_resource_ids', 'requested_resource_ids'),
    'observation': ('video_evidence_available', 'audio_evidence_available', 'audio_evidence_basis',
                    'audio_usage_reported', 'effective_fps', 'sampling_coverage'),
}


def related_history(objects: list[dict[str, Any]], roots: set[str], *,
                    kinds: set[str] | frozenset[str] | None = None,
                    max_nodes: int = 2000) -> list[dict[str, Any]]:
    """Follow production descendants, hiding service dispatch records.

    Inputs must come from one project's store inventory. Return original exact
    records for the caller's projection/freezing. IDs establish relatedness, not
    revision freshness or approval. Bridges count against the same node bound;
    neither a revised intent nor an author-written lookalike grants reachability.
    """
    if type(max_nodes) is not int or max_nodes <= 0:
        raise ValueError('History node bound must be positive')
    allowed = RELATED_KINDS if kinds is None else kinds
    pending = [obj for obj in objects if
               (obj['kind'] != 'dispatch-intent' and obj['kind'] in allowed) or
               (obj['kind'] == 'dispatch-intent' and obj['author'] == 'submission_service' and obj['revision'] == 1)]
    relevant = set(roots)
    traversed: set[str] = set()
    related: list[dict[str, Any]] = []
    while pending:
        matched = [obj for obj in pending if relevant.intersection(
            ref['object_id'] for ref in obj['body'].get('dependencies', []))]
        if not matched:
            break
        traversed.update(obj['object_id'] for obj in matched)
        if len(traversed) > max_nodes:
            raise DomainError('insufficient_context', 'Related production history exceeds context capacity')
        relevant.update(traversed)
        related.extend(obj for obj in matched if obj['kind'] != 'dispatch-intent')
        pending = [obj for obj in pending if obj['object_id'] not in traversed]
    return related


def context_body(obj: dict[str, Any]) -> dict[str, Any]:
    """Public author context, also used by compiler history serialization."""
    body = obj['body']
    if obj['kind'] in CREATIVE_KINDS:
        return body
    fields = MACHINE_FIELDS.get(obj['kind'])
    if fields is None:
        # Future machine kinds cannot silently opt into raw author/model context.
        # The outer _view retains the immutable record identity and deep-read link.
        refs = []
        for value in body.get('dependencies', []):
            ref = ObjectRef.model_validate(value)
            if ref.digest is None:
                raise DomainError('invalid_input', 'Opaque context dependencies require exact fingerprints')
            refs.append(ref.model_dump())
        return {'projection': 'opaque-record', 'dependencies': refs}
    selected = {key: body[key] for key in (*fields, 'dependencies') if key in body}
    if obj['kind'] in CONSUMPTION_SUMMARY and 'consumption' in selected:
        consumption = selected['consumption']
        # Audit proof stays on the immutable receipt. Sending prior coverage
        # proofs as creative evidence recursively expands each subsequent review.
        selected['consumption'] = {key: consumption[key] for key in CONSUMPTION_SUMMARY[obj['kind']]
                                   if key in consumption}
    result = _clean(selected)
    # These allowlisted fields are creative facts or independent conclusions,
    # not machine wire. Do not redact fictional literals or truncate long prose.
    prose_fields = {
        'project': ('title', 'brief'), 'cut': ('intent',), 'take-selection': ('rationale',),
        'pickup': ('expected_information',),
        'repair': ('observed_defect', 'desired_change', 'diff'),
        'review-receipt': ('issues', 'advisories', 'evidence', 'structured_verdict'),
        'observation': ('questions', 'observations', 'answers', 'uncertainty'),
        'qualification-set': ('rationale',),
    }
    for key in prose_fields.get(obj['kind'], ()):
        if key in body:
            result[key] = body[key]
    if obj['kind'] == 'candidate':
        request = body.get('request', {})
        params = request.get('params', {})
        result['request'] = {
            'job_type': request.get('job_type'),
            'params': {key: params[key] for key in ('prompt', 'duration', 'resolution', 'aspect_ratio', 'mode', 'quality', 'variant') if key in params},
            'references': _clean([{key: ref[key] for key in ('object_ref', 'sha256', 'role', 'tag', 'n', 'media_type', 'byte_length') if key in ref}
                                  for ref in request.get('references', [])]),
        }
        # Actual submitted creative text must remain exact, including intentional
        # literal prose. The compiler snapshot/history is not recursively exposed.
        prompt = body.get('compilation', {}).get('assembly', {}).get('prompt')
        if prompt is not None:
            result['assembled_prompt'] = prompt
    return result


def _view(obj: dict[str, Any]) -> dict[str, Any]:
    return {**_ref(obj), 'kind': obj['kind'], 'author': obj['author'], 'body': context_body(obj),
            'link': f"/v1/projects/{obj['project_id']}/artifacts/{obj['object_id']}?revision={obj['revision']}"}


class ContextService:
    def __init__(self, store: Store, auth: AuthService, workflow: Workflow, *,
                 max_bytes: int = 4 * 1024 * 1024) -> None:
        if max_bytes <= 0:
            raise ValueError('Context bound must be positive')
        self.store, self.auth, self.workflow = store, auth, workflow
        self.max_bytes = max_bytes

    def _bounded(self, value: dict[str, Any]) -> dict[str, Any]:
        size = len(canonical_json(value).encode())
        if size > self.max_bytes:
            raise DomainError('insufficient_context', 'Required context exceeds the published input bound',
                              repair='Use a supported scoped target or a larger operator-released context limit; do not truncate required evidence')
        return value

    def methods(self, actor: Principal, project_id: str, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        with self.store._using(conn, write=False) as db:
            self.auth.authorize(actor, project_id, 'methods', conn=db)
            project = self.store.get_object(project_id, project_id, conn=db)
            rid = project['body']['release_id']
            methods = {mid: {**definition, 'platform_enabled': True}
                       for mid, definition in self.workflow.config.section('methods')['methods'].items()}
            return self._bounded({'release_id': rid, 'methods': methods,
                'interpretation': 'The methods enabled in the runtime config; the manuals hold the craft.'})

    def document(self, actor: Principal, project_id: str, role: str) -> dict[str, Any]:
        """A runtime config document by role name. No filesystem path, URL or paid deep-read tool."""
        with self.store.transaction(write=False) as db:
            self.auth.authorize(actor, project_id, 'references', conn=db)
            rid = self.store.get_object(project_id, project_id, conn=db)['body']['release_id']
            data = self.workflow.config.document(role)
            return self._bounded({'release_id': rid, 'role': role, 'sha256': hashlib.sha256(data).hexdigest(),
                                  'text': data.decode('utf-8')})

    def get(self, actor: Principal, project_id: str, *, target: ObjectRef | None = None,
            task: str | None = None, method: ObjectRef | None = None,
            conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        with self.store._using(conn, write=False) as db:
            self.auth.authorize(actor, project_id, 'context', conn=db)
            project = self.store.get_object(project_id, project_id, conn=db)
            rid = project['body']['release_id']
            all_objects = self.store.list_objects(project_id, conn=db)
            graph = self.workflow.pinned_graph(project_id, target, conn=db) if target else None
            objects = [node['object'] for node in graph['nodes']] if graph else [o for o in all_objects if o['kind'] in ('brief', 'script', 'scene', 'expectation')]
            objects = list({(o['object_id'],o['revision']): o for o in objects}.values())
            object_ids = {o['object_id'] for o in objects}
            progression = self.workflow.inspect(actor, project_id, target=target, task=task, method=method, conn=db)
            selected_method = progression.get('method')
            method_choice = None
            if method:
                # Preserve historical choices for a stale candidate without authorizing them.
                choice = self.store.get_object(project_id, method.object_id, revision=method.revision, conn=db)
                if choice['kind'] != 'method' or method.digest and choice['digest'] != method.digest:
                    raise DomainError('stale_input', 'Context method reference is invalid')
                method_choice = _view(choice)
            # No rule catalog or rule excerpts: the writer reads the manuals.
            if method_choice and selected_method is None:
                method_id = method_choice['body']['content']['method_id']
                methods = self.workflow.config.section('methods')['methods']
                if method_id in methods:
                    selected_method = {'method_id':method_id, 'definition':methods[method_id],
                                       'rules':[], 'execution_eligible':False}
            scene_ids = {o['object_id'] for o in objects if o['kind'] == 'scene'}
            neighbors = []
            related: list[dict[str, Any]] = []
            for obj in all_objects:
                dep_ids = {ref['object_id'] for ref in obj['body'].get('dependencies', [])}
                # A standalone probe shares a scene, not an edit sequence. Any
                # deliberately required shot remains in its pinned input graph.
                if task != 'stress' and obj['kind'] == 'shot' and obj['object_id'] not in object_ids and scene_ids.intersection(dep_ids):
                    neighbors.append(_view(obj))
            related = [_view(obj) for obj in related_history(all_objects, object_ids)]
            def of_kind(kind: str) -> list[dict[str, Any]]:
                return [_view(o) for o in objects if o['kind'] == kind]
            selections = [o for o in of_kind('asset') if isinstance(o['body'].get('content'), dict) and o['body']['content'].get('type') == 'asset-selection']
            expectations = of_kind('expectation')
            for obj in objects:
                content = obj['body'].get('content', {})
                if obj['kind'] == 'shot' and isinstance(content, dict):
                    expectations.append({'target': _ref(obj), 'content': content.get('Direction', {}), 'authority': 'creative_intent'})
            media = [{**_view(o), 'media_link':f"/v1/projects/{project_id}/media/{o['object_id']}?revision={o['revision']}"}
                     for o in objects if o['kind'] == 'media']
            limitations = []
            if task in ('shot','cut'):
                if not scene_ids:
                    limitations.append('whole_scene_and_ending_missing')
                if not neighbors:
                    limitations.append('neighboring_shots')
            result = {
                'project': _view(project), 'branch': project['body']['branch'], 'target': target.model_dump() if target else None,
                'task': task, 'release_id': rid, 'progression': progression, 'method': selected_method,
                'available_method_selections': [_view(o) for o in all_objects if o['kind']=='method'
                    and o['body'].get('content',{}).get('target',{}).get('object_id') in object_ids | {project_id}],
                'method_choice': method_choice, 'governing_rules': [], 'sources': {},
                'creative': {'brief': project['body'].get('brief',''), 'scripts':of_kind('script'),
                             'scenes':of_kind('scene'), 'shots':of_kind('shot'), 'expectations':expectations,
                             'source_understanding':of_kind('source-understanding'), 'assets':of_kind('asset'), 'selections':selections},
                'neighbors': {'shots': neighbors, 'cut_order_known': False,
                              'scope': 'explicit_dependencies' if task == 'stress' else 'scene_membership',
                              'note':'Scene membership does not establish edit order.'},
                'prior_results':related, 'media':media, 'limitations':limitations,
                'dependencies': [_ref(o) for o in objects],
                'deep_reads': {'methods':f'/v1/projects/{project_id}/methods'},
                'consumption': {'paid_calls':0, 'contains_actual_media_bytes':False,
                                'comprehension_verified':False},
                'artistic_acceptance':False, 'whole_scene_completeness':'unverified',
            }
            self._bounded(result)
            result['context_hash'] = content_hash(result)
            return self._bounded(result)
