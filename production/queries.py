"""Authenticated, read-only Plan / Results / Files projections.

Raw provider/reviewer records are never a viewer API. Historical references stay
fixed; generated files, maker selections and independent/human acceptance remain
separate facts. This projection does not perform or replace a review.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from production import prompt as prompts
from production.auth import AuthService, Principal
from production.contracts import ERROR_CODES, DomainError, ObjectRef, safe_logical_path
from production.review_http import FAILURE_REASONS
from production.shoot import owner_reason
from production.store import Store
from production.workflow import Workflow

PLAN = frozenset({'brief', 'script', 'scene', 'shot', 'asset', 'source-understanding', 'expectation', 'method'})
FIELDS = {
    **dict.fromkeys(PLAN, ('logical_path', 'content')),
    'media': ('logical_path', 'media_type', 'size', 'sha256', 'probe', 'source_cut', 'derivative_of', 'import_status', 'label'),
    'cut': ('intent', 'segments', 'sound_inputs', 'source_methods', 'output', 'duration_seconds', 'method_id', 'label', 'imported_unverified'),
    'job': ('state', 'candidate', 'result', 'current', 'non_current_reasons', 'last_error', 'operation', 'cost_status', 'attempt'),
    'composition-job': ('state', 'result', 'attempt', 'last_error', 'manifest'),
    'asset-composition': ('width', 'height', 'purpose', 'layers'),
    'provider-receipt': ('job_id', 'job_type', 'state', 'settled_cost', 'critical_adjustments'),
    'review-task': ('target', 'purpose', 'role', 'state', 'verdict', 'receipt_id', 'route_profile_id', 'method_id',
                    'appeal_of', 'appeal_reason'),
    'review-run': ('task_id', 'state', 'tool_calls', 'started_at', 'failure'),
    'review-turn': ('task_id', 'role', 'status', 'number', 'request_sha256', 'profile_id', 'failure'),
    'review-receipt': ('target', 'purpose', 'role', 'verdict', 'issues', 'advisories', 'evidence', 'structured_verdict', 'route_profile_id', 'method_id'),
    'observation': ('source', 'status', 'questions', 'observations', 'limitations', 'answers', 'uncertainty', 'consumption', 'returned_model', 'source_sha256', 'failure'),
    'take-selection': ('shot', 'take', 'rationale'),
    'human-take-selection': ('shot', 'take', 'reason', 'human_receipt', 'verified_human_session'),
    # the owner's desk notes (production/owner_notes.py).
    'owner-note': ('target', 'take', 'at_seconds', 'text', 'withdrawn', 'verified_human_session'),
    'agent-report': ('target', 'observation', 'evidence', 'attribution'),
    'feedback': ('target', 'content', 'playback_seconds', 'attribution', 'human_identity_verified'),
    'pickup-resolution': ('source_pickup', 'repair', 'repair_lineage', 'failed_take', 'returned_take', 'receipts', 'policy_hash', 'release_id', 'status', 'accepted'),
    'pickup': ('target', 'playback_seconds', 'expected_information', 'evidence', 'status'),
    'repair': ('base', 'result', 'source_pickup', 'failed_take', 'observed_defect', 'creative_path', 'scope', 'desired_change', 'diff', 'accepted'),
    'repair-lineage': ('repair', 'source_pickup', 'failed_take', 'returned_take'),
    'qualification-set': ('asset', 'samples', 'context_refs', 'previous_set', 'method_id', 'rationale', 'state'),
    'qualification-sample': ('asset', 'sample', 'sample_sha256', 'context_refs', 'method_id'),
    'lesson-proposal': ('target', 'evidence', 'observation', 'proposed_change', 'status', 'source_release_id'),
    'batch': ('state', 'count'),
    'reopen': ('lock', 'targets', 'reason', 'invalidated'),
    'finishing': ('logical_path', 'content'),
    'decision-request': ('target', 'purpose', 'rationale', 'state', 'expires_at', 'human_confirmation_available'),
    'human-receipt': ('target', 'purpose', 'choice', 'reason', 'verified_human_session'),
    'final': ('media', 'human_receipt', 'review_receipts'),
    'picture-lock': ('cut', 'state', 'reopened_targets'),
    'invalidation': ('target', 'reason'),
    'candidate': ('target', 'task', 'method_id', 'method_selection', 'gate_status'),
}
PRIVATE_KEYS = re.compile(r'(?i)(secret|credential|authorization|cookie|token|api.?key|download_reference|data_url|base64|storage_path|blob_path|result_url|signed_url|private_url)')
URL = re.compile(r'https?://[^\s<>"\']+')
REVIEW_FAILURE_CODES = ERROR_CODES | frozenset({
    'dispatch_lease_expired', 'request_journal_mismatch', 'late_response', 'local_cancellation',
    'invalid_review_result', 'incomplete_required_output', 'invalid_required_output',
    'provider_http_error', 'refusal_or_invalid_role', 'response_identity_mismatch'})


def _review_failure(value: Any) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) and value in REVIEW_FAILURE_CODES else 'review_execution_failed'


def _review_diagnostic(value: Any) -> str | None:
    return value if isinstance(value, str) and value in FAILURE_REASONS | {'storage_writer_busy'} else None


def _ref(obj: dict[str, Any]) -> dict[str, Any]:
    return {k: obj[k] for k in ('object_id', 'revision', 'digest')}


def _additions(candidate: dict[str, Any], sent: Any) -> dict[str, Any]:
    """where each platform addition sits in the sent prompt, and whether the sent prompt is
    exactly the writer's text plus those additions (production/prompt.py). Candidates before 2026-09-27 have none."""
    compilation = candidate['body'].get('compilation') or {}
    written, additions = compilation.get('writer_text'), compilation.get('additions')
    if not isinstance(written, str) or not isinstance(additions, list) or not isinstance(sent, str):
        return {}
    spans, shift = [], 0
    try:
        for addition in additions:
            start = addition['at'] + shift
            spans.append([start, start + len(addition['text']), addition['kind']])
            shift += len(addition['text'])
        references = (candidate['body'].get('request') or {}).get('references') or []
        ok = not prompts.check(written, additions, sent, [{'n': r.get('n'), 'tag': r.get('tag')} for r in references],
                               compilation.get('prompt_constants') or {})
    except (KeyError, TypeError, AttributeError):
        spans, ok = [], False
    return {'added': spans, 'additions': len(additions), 'sent_is_writer_plus_additions': ok}


def _clean(value: Any, depth: int = 0) -> Any:
    """Defense in depth for legacy records; service fields also use allowlists."""
    if depth > 30:
        raise DomainError('insufficient_context', 'Requested artifact exceeds viewer nesting bound')
    if isinstance(value, dict):
        return {k: _clean(v, depth+1) for k, v in value.items() if not PRIVATE_KEYS.search(k)}
    if isinstance(value, list):
        return [_clean(v, depth+1) for v in value]
    if isinstance(value, str):
        if value.startswith('data:') or len(value) > 200000:
            return '[Large or embedded content omitted; use its exact media reference.]'
        def redact_url(match: re.Match[str]) -> str:
            try:
                parts = urlsplit(match[0])
            except ValueError:
                return '[invalid URL omitted]'
            if parts.username or parts.password or parts.hostname in ('localhost', '127.0.0.1', '::1'):
                return '[private URL omitted]'
            return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
        text = URL.sub(redact_url, value)
        text = re.sub(r'credential_[0-9a-f]{32}\.[A-Za-z0-9_-]{40,64}', '[credential omitted]', text)
        text = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [redacted]', text)
        text = re.sub(r'(?i)\b(?:sk-[A-Za-z0-9_-]{12,}|(?:API_KEY|TOKEN|SECRET)\s*[=:]\s*\S+)', '[redacted]', text)
        return re.sub(r'(?:file://)?/(?:Users|home|opt|private|var|tmp)/[^\s<>"\']+', '[host path omitted]', text)
    return value


def _pick(value: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: value[key] for key in keys if key in value} if isinstance(value, dict) else {}


def _public_ref(value: Any) -> dict[str, Any]:
    """A reference never embeds a referenced body or a live latest-version lookup."""
    try:
        ref = ObjectRef.model_validate(_pick(value, ('object_id', 'revision', 'digest')))
        return ref.model_dump() if ref.digest else {}
    except (TypeError, ValueError):
        return {}


STAGES = ('开发', '前期', '拍摄', '后期')


def project_stage(db: sqlite3.Connection, pid: str) -> str:
    """HF's four stages (HF_CANONICAL.md §9, 9–10 of 12): Development → Pre-production → Production → Post-production,
    read from what the project holds: a cut → 后期, a shot candidate → 拍摄, assets → 前期."""
    def has(sql: str) -> bool:
        return db.execute(sql, (pid,)).fetchone() is not None
    if has("SELECT 1 FROM objects WHERE project_id=? AND kind='cut' LIMIT 1"):
        return '后期'
    if has("SELECT 1 FROM objects o JOIN revisions r ON r.project_id=o.project_id AND r.object_id=o.object_id AND r.revision=1 "
           "WHERE o.project_id=? AND o.kind='candidate' AND json_extract(r.body,'$.task')='shot' LIMIT 1"):
        return '拍摄'
    if has("SELECT 1 FROM objects WHERE project_id=? AND kind='asset' LIMIT 1"):
        return '前期'
    return '开发'


def _details(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    result = _pick(body, FIELDS[kind])
    if kind in ('human-take-selection', 'owner-note'):
        for key in ('shot', 'take', 'human_receipt', 'target'):
            if key in result and result[key] is not None:
                result[key] = _public_ref(result[key])
    if kind in ('review-run', 'review-turn') and 'failure' in result:
        result['failure'] = _review_failure(result['failure'])
    if kind in ('review-run', 'review-turn') and 'diagnostic' in body:
        result['diagnostic'] = _review_diagnostic(body['diagnostic'])
    if kind == 'review-task' and body.get('appeal_of') is not None:
        result['appeal_of'] = _public_ref(body['appeal_of'])
        result['appeal_authority'] = 'Maker argument; not an approval or a rule.'
    if kind == 'job' and isinstance(body.get('preparation_failure'), dict):
        result['preparation_failure'] = _pick(body['preparation_failure'], ('code', 'provider_called'))
    if kind in ('qualification-set', 'qualification-sample', 'lesson-proposal', 'repair', 'repair-lineage', 'pickup-resolution', 'reopen'):
        for key in ('asset', 'sample', 'previous_set', 'target', 'repair', 'repair_lineage', 'source_pickup', 'failed_take', 'returned_take', 'lock'):
            if key in result and result[key] is not None:
                result[key] = _public_ref(result[key])
        for key in ('samples', 'context_refs', 'evidence', 'receipts', 'targets', 'invalidated'):
            if key in result:
                result[key] = [_public_ref(ref) for ref in result[key]] if isinstance(result[key], list) else []
    if kind in ('qualification-set', 'qualification-sample'):
        result['qualified'] = False  # Enrollment is not the independent derived qualification decision.
        if kind == 'qualification-sample':
            result['asset_references'] = [{**_pick(item, ('sha256', 'role')),
                'asset': _public_ref(item.get('asset')), 'media': _public_ref(item.get('media'))}
                for item in body.get('asset_references', []) if isinstance(item, dict)]
    if kind == 'lesson-proposal':
        result['operative'] = False
    if kind in ('repair-lineage', 'pickup-resolution'):
        result['accepted'] = False
    if kind == 'batch':
        children = []
        for child in body.get('children', []):
            if not isinstance(child, dict):
                continue
            row = {key: child[key] for key in ('resolution', 'relation') if isinstance(child.get(key), str)}
            row.update({key: _public_ref(child[key]) for key in ('candidate', 'target', 'job') if key in child})
            if isinstance(child.get('previous_attempts'), list):
                row['previous_attempts'] = [_public_ref(ref) for ref in child['previous_attempts']]
            children.append(row)
        result['children'] = children
    if kind == 'decision-request':
        evidence = _pick(body.get('evidence'), ('budget_before', 'proposed_limit', 'budget_unit', 'budget_key', 'cut', 'media', 'receipts', 'finishing'))
        if body.get('purpose') == 'envelope':
            before = _pick(evidence.get('budget_before'), ('ceiling', 'spent', 'reserved', 'unit', 'budget_key'))
            result['evidence'] = {key: value for key, value in _pick(evidence, ('proposed_limit', 'budget_unit', 'budget_key')).items()
                if type(value) is (int if key == 'proposed_limit' else str)}
            result['evidence']['budget_before'] = {key: value for key, value in before.items()
                if type(value) is (str if key in ('unit', 'budget_key') else int)}
        elif body.get('purpose') == 'final':
            result['evidence'] = {key: _public_ref(evidence[key]) for key in ('cut', 'media') if key in evidence}
            result['evidence'].update({key: [_public_ref(ref) for ref in evidence[key]]
                for key in ('receipts', 'finishing') if isinstance(evidence.get(key), list)})
        elif body.get('purpose') == 'take':
            evidence = _pick(body.get('evidence'), ('shot', 'takes', 'qualification_status'))
            statuses = []
            for status in evidence.get('qualification_status', []):
                if isinstance(status, dict):
                    statuses.append({'take': _public_ref(status.get('take')),
                        'qualified': status.get('qualified') if type(status.get('qualified')) is bool else None,
                        'reason': status.get('reason') if status.get('reason') in ERROR_CODES | {'not_evaluated', 'not_applicable'} else None})
            result['evidence'] = {'shot': _public_ref(evidence.get('shot')),
                'takes': [_public_ref(ref) for ref in evidence.get('takes', [])], 'qualification_status': statuses}
        elif body.get('purpose') == 'shot-plan':
            # what the owner approves — each shot's meaning, reason, seconds and timed lines.
            shots = body.get('evidence', {}).get('shots', []) if isinstance(body.get('evidence'), dict) else []
            text = lambda v: v if isinstance(v, str) else None
            number = lambda v: v if type(v) in (int, float) else None
            names = body.get('evidence', {}).get('names') if isinstance(body.get('evidence'), dict) else None
            result['evidence'] = {'scene': _public_ref(body.get('evidence', {}).get('scene')),
                                  'names': {k: v for k, v in (names or {}).items() if isinstance(k, str) and isinstance(v, str)}, 'shots': [
                {'shot': text(entry.get('shot')), 'card': _public_ref(entry.get('card')),
                 **{key: text(entry.get(key)) for key in ('setup', 'sentence', 'reason', 'last', 'goal', 'task', 'changes', 'hook')},
                 'seconds': number(entry.get('seconds')),
                 'lines': [{'speaker': text(line.get('speaker')), 'line': text(line.get('line')),
                            'start': number(line.get('start')), 'end': number(line.get('end'))}
                           for line in entry.get('lines', []) if isinstance(line, dict)]}
                for entry in shots if isinstance(entry, dict)]}
    return result


MAKING = frozenset({'queued', 'dispatching', 'submitted', 'running'})


def creation_order(db: sqlite3.Connection, pid: str, object_ids: list[str]) -> dict[str, int]:
    """Store creation sequence of each object (the object.created event), never object_id order."""
    if not object_ids:
        return {}
    rows = db.execute("SELECT json_extract(body,'$.object_id'), sequence FROM events WHERE project_id=? AND kind='object.created' "
                      "AND json_extract(body,'$.object_id') IN (" + ','.join('?' for _ in object_ids) + ')', [pid, *object_ids]).fetchall()
    order = {oid: seq for oid, seq in rows}
    missing = [oid for oid in object_ids if oid not in order]
    if missing:
        raise DomainError('insufficient_context', 'Selection creation order is not recorded')
    return order


def current_picks(selections: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Latest human selection per shot wins; `selections` must already be in creation order. An un-pick has no take."""
    latest: dict[str, dict[str, Any]] = {}
    for view in selections:
        shot = view['details'].get('shot')
        if isinstance(shot, dict) and isinstance(shot.get('object_id'), str):
            latest[shot['object_id']] = view
    return {shot: view for shot, view in latest.items()
            if isinstance(view['details'].get('take'), dict) and view['details']['take'].get('object_id')}


class Queries:
    def __init__(self, store: Store, auth: AuthService, workflow: Workflow) -> None:
        self.store, self.auth, self.workflow = store, auth, workflow
        # no review service checks pickup resolutions any more.
        self.resolution_check = None

    def _link(self, pid: str, obj: dict[str, Any]) -> str:
        return f'/projects/{pid}/objects/{obj["object_id"]}?' + urlencode({'revision': obj['revision']})

    def _objects(self, pid: str, db: sqlite3.Connection) -> list[dict[str, Any]]:
        # Filter kinds before loading bodies: runner contexts may contain huge images.
        kinds = sorted(FIELDS)
        rows = db.execute('SELECT object_id FROM objects WHERE project_id=? AND kind IN ('+
                          ','.join('?' for _ in kinds)+') ORDER BY object_id LIMIT 2001', [pid, *kinds]).fetchall()
        if len(rows) > 2000:
            raise DomainError('insufficient_context', 'Project viewer inventory exceeds its explicit bound')
        return [self.store.get_object(pid, row[0], conn=db) for row in rows]

    def _state(self, pid: str, obj: dict[str, Any], db: sqlite3.Connection) -> dict[str, Any]:
        if obj['kind'] == 'pickup-resolution':
            if self.resolution_check is None:
                return {'current': False, 'stale': None, 'unverified': True,
                        'status': 'resolution-unverified', 'reasons': [{'code': 'review_required'}]}
            return self.resolution_check(pid, ObjectRef(**_ref(obj)), conn=db)
        try:
            graph = self.workflow.pinned_graph(pid, ObjectRef(**_ref(obj)), conn=db)
        except DomainError as exc:
            graph = {'error': exc.code}
        return self._graph_state(obj, graph)

    @staticmethod
    def _graph_state(obj: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
        if 'error' in graph:
            return {'current': False, 'stale': None, 'reasons': [{'code': graph['error']}], 'unverified': True}
        if obj['kind'] in ('job', 'media') and obj['body'].get('current') is False:
            return {'current': False, 'stale': True, 'reasons': [*graph['reasons'], {'code': 'recorded_non_current'}]}
        return {'current': not graph['stale'], 'stale': graph['stale'], 'reasons': graph['reasons']}

    def _confirmed(self, pid: str, obj: dict[str, Any], db: sqlite3.Connection) -> bool:
        if obj['kind'] != 'final' or obj['author'] != 'decision_service' or obj['body'].get('accepted') is not True:
            return False
        if not self._state(pid, obj, db)['current']:
            return False
        try:
            body = obj['body']
            def exact(value: dict[str, Any]) -> dict[str, Any]:
                ref = ObjectRef.model_validate(value)
                found = self.store.get_object(pid, ref.object_id, revision=ref.revision, conn=db)
                if ref.digest != found['digest']:
                    raise ValueError('Invalid confirmation evidence')
                return found
            media, human = exact(body['media']), exact(body['human_receipt'])
            hb = human['body']
            if (media['kind'] != 'media' or not media['body'].get('probe', {}).get('has_video')
                    or human['kind'] != 'human-receipt' or human['author'] != 'decision_service'
                    or hb.get('verified_human_session') is not True or hb.get('purpose') != 'final'
                    or hb.get('choice') != 'confirm' or hb.get('target') != _ref(media)):
                return False
            # the owner's verified confirmation is the acceptance;
            # no AI cut review is required (there is none any more).
            return True
        except (DomainError, ValueError, KeyError, TypeError):
            return False

    def _result_media(self, pid: str, obj: dict[str, Any], db: sqlite3.Connection) -> bool:
        """Display grouping only; never generation, review or acceptance authority.

        Ordinary uploads and observation frames are preparation evidence. Service
        outputs, labelled legacy imports and finishing uploads belong in Results.
        Do not infer any of this from a maker-chosen filename or directory.
        """
        body = obj['body']
        if obj['author'] == 'worker_service' and isinstance(body.get('provenance'), dict):
            return True
        if obj['author'] == 'composition_service' and isinstance(body.get('composition'), dict):
            return True
        if obj['author'] == 'importer_service' and body.get('import_status') == 'imported-unverified':
            return True
        try:
            ref = ObjectRef.model_validate(body.get('source_cut'))
            cut = self.store.get_object(pid, ref.object_id, revision=ref.revision, conn=db)
            return (cut['kind'] == 'cut' and cut['author'] == 'cut_service'
                    and ref.digest == cut['digest'])
        except (DomainError, ValueError, KeyError, TypeError):
            return False

    def _view(self, pid: str, obj: dict[str, Any], db: sqlite3.Connection, *, detail: bool = False,
              resolution_cache: dict[str, dict[str, Any]] | None = None,
              graph_state: dict[str, Any] | None = None) -> dict[str, Any]:
        kind, body = obj['kind'], obj['body']
        if kind not in FIELDS:
            raise DomainError('forbidden', 'This service record is not a public artifact')
        cache = resolution_cache if resolution_cache is not None else {}
        def resolution_state(record: dict[str, Any]) -> dict[str, Any]:
            key = f"{record['object_id']}:{record['revision']}:{record['digest']}"
            if key not in cache:
                cache[key] = self._state(pid, record, db)
            return cache[key]
        state = (resolution_state(obj) if kind == 'pickup-resolution' else
                 self._graph_state(obj, graph_state) if graph_state is not None else self._state(pid, obj, db))
        independent = kind == 'review-receipt' and obj['author'] == 'review_service'
        human = kind == 'human-receipt' and obj['author'] == 'decision_service' and body.get('verified_human_session') is True
        result = {'object_ref': _ref(obj), 'kind': kind, 'link': self._link(pid, obj), **state,
                  'attribution': 'independent-review' if independent else 'human-decision' if human else
                      'human-selection' if kind == 'human-take-selection' and obj['author'] == 'decision_service'
                      and body.get('verified_human_session') is True else
                      'human-note' if kind == 'owner-note' and obj['author'] == 'owner_note_service'
                      and body.get('verified_human_session') is True else 'agent-selection' if kind == 'take-selection' else 'record',
                  'confirmed_final': self._confirmed(pid, obj, db), 'imported_unverified': body.get('import_status') == 'imported-unverified' or body.get('imported_unverified') is True,
                  'status': _clean(state.get('status', body.get('state', body.get('status')))), 'label': _clean(body.get('label')),
                  # When the record was first written (revision 1): the desk card's time and 刚拍好 (design v6).
                  # Later revisions (a pick, a withdrawal) must not make old takes look new.
                  'created_at': next(iter(db.execute('SELECT created_at FROM revisions WHERE project_id=? AND object_id=? '
                                                     'AND revision=1', (pid, obj['object_id'])).fetchone() or ()), None)}
        if kind == 'pickup':
            records = [r for r in self.store.list_objects(pid, kind='pickup-resolution', conn=db)
                       if r['author'] == 'review_service' and r['body'].get('source_pickup') == _ref(obj)]
            checked = [(r, resolution_state(r)) for r in records]
            result['stored_status'] = body.get('status')
            if checked:
                record, resolution = next(((r, st) for r, st in checked if st['current']), checked[-1])
                result['resolution_status'] = resolution['status']
                result['resolution_reasons'] = _clean(resolution.get('reasons', []))
                result['resolution'] = _ref(record)
            elif self.resolution_check is not None:
                pending = self.resolution_check(pid, ObjectRef(**_ref(obj)), conn=db)
                result['resolution_status'] = pending['status']
                result['resolution_reasons'] = _clean(pending.get('reasons', []))
            else:
                result['resolution_status'] = 'unresolved'
        path = body.get('logical_path')
        result['logical_path'] = _clean(path) if isinstance(path, str) else None
        result['display_name'] = _clean(' / '.join(path.split('/')[-2:])) if isinstance(path, str) else kind+' '+obj['object_id'][-8:]
        if kind == 'review-receipt':
            result.update(verdict=body.get('verdict') if independent else 'unverified', issues=_clean(body.get('issues', [])))
        if detail:
            result['details'] = _clean(_details(kind, body))
            if kind == 'review-task' and obj['author'] == 'review_service' and body.get('execution'):
                try:
                    ref = ObjectRef.model_validate(body['execution'])
                    execution = self.store.get_object(pid, ref.object_id, revision=ref.revision, conn=db)
                    if (execution['kind'] == 'review-turn' and execution['author'] == 'review_runner_service'
                            and _ref(execution) == ref.model_dump()
                            and execution['body'].get('task_id') == obj['object_id']
                            and execution['body'].get('context_hash') == body.get('context_hash')
                            and execution['body'].get('status') == body.get('state')):
                        result['details']['execution'] = ref.model_dump()
                        result['details']['failure'] = _review_failure(execution['body'].get('failure'))
                        result['details']['diagnostic'] = _review_diagnostic(execution['body'].get('diagnostic'))
                except (DomainError, ValueError, TypeError):
                    pass  # A broken diagnostic link grants no authority or private fallback.
            if kind == 'candidate':
                request = body.get('request', {})
                result['details']['request'] = _clean({
                    'job_type': request.get('job_type'),
                    'params': {k: v for k, v in request.get('params', {}).items() if k in ('prompt', 'duration', 'resolution', 'aspect_ratio', 'mode', 'quality', 'variant')},
                    'references': [{k: r[k] for k in ('object_ref', 'sha256', 'role', 'tag', 'n', 'media_type', 'byte_length') if k in r}
                                   for r in request.get('references', [])]})
                result['details']['assembled_prompt'] = _clean(body.get('compilation', {}).get('assembly', {}).get('prompt'))
            if kind == 'media':
                result['media_ref'] = _ref(obj)
            if kind == 'job' and body.get('intent'):
                ref = ObjectRef.model_validate(body['intent'])
                intent = self.store.get_object(pid, ref.object_id, revision=ref.revision, conn=db)
                if intent['author'] == 'submission_service' and intent['digest'] == ref.digest:
                    result['details']['dispatch'] = _clean({k: intent['body'][k]
                        for k in ('operation', 'target', 'candidate', 'task', 'method_id') if k in intent['body']})
        return result

    def list_projects(self, actor: Principal, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        with self.store._using(conn, write=False) as db:
            result = []
            for pid in sorted(self.auth.visible_projects(actor, conn=db)):
                obj = self.store.get_object(pid, pid, conn=db)
                result.append({'project_id': pid, 'title': _clean(obj['body'].get('title', pid)),
                               'branch': obj['body'].get('branch'), 'revision': obj['revision'], 'stage': project_stage(db, pid),
                               'created_at': obj['created_at'], 'previous_project_id': None,
                               'superseded_by': []})
            visible = {row['project_id']: row for row in result}
            for row in result:
                obj = self.store.get_object(row['project_id'], row['project_id'], conn=db)
                migration = obj['body'].get('migration')
                if obj['author'] != 'operator' or not isinstance(migration, dict):
                    continue
                ref = _public_ref(migration.get('source_project'))
                parent = ref.get('object_id')
                if not isinstance(parent, str) or parent not in visible or parent == row['project_id']:
                    continue  # Never disclose a project outside this viewer's grants.
                try:
                    source = self.store.get_object(parent, parent, revision=ref['revision'], conn=db)
                except DomainError:
                    continue
                if (source['kind'] == 'project' and _ref(source) == ref
                        and source['body'].get('branch') == row['branch']
                        and migration.get('requires_revalidation') is True
                        and migration.get('copied_approvals') is False):
                    row['previous_project_id'] = parent
                    visible[parent]['superseded_by'].append(row['project_id'])
            return result

    def project(self, actor: Principal, pid: str, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        with self.store._using(conn, write=False) as db:
            self.auth.authorize(actor, pid, 'context', conn=db)
            project = self.store.get_object(pid, pid, conn=db)
            objects = self._objects(pid, db)
            resolution_cache: dict[str, dict[str, Any]] = {}
            ordinary = [obj for obj in objects if obj['kind'] != 'pickup-resolution']
            states = dict(zip((obj['object_id'] for obj in ordinary), self.workflow.pinned_states(
                pid, [ObjectRef(**_ref(obj)) for obj in ordinary], conn=db), strict=True))
            views = [self._view(pid, obj, db, resolution_cache=resolution_cache,
                                graph_state=states.get(obj['object_id'])) for obj in objects]
            preparation_media = {obj['object_id'] for obj in objects
                                 if obj['kind'] == 'media' and not self._result_media(pid, obj, db)}
            preparation_observers: set[str] = set()
            for obj in objects:
                if obj['kind'] == 'observation' and obj['author'] == 'reader_service':
                    if obj['body'].get('source', {}).get('object_id') in preparation_media:
                        preparation_observers.add(obj['object_id'])
                elif obj['kind'] == 'job' and obj['author'] in ('submission_service', 'worker_service'):
                    try:
                        ref = ObjectRef.model_validate(obj['body'].get('intent'))
                        intent = self.store.get_object(pid, ref.object_id, revision=ref.revision, conn=db)
                        if (intent['kind'] == 'dispatch-intent' and intent['author'] == 'submission_service'
                                and ref.digest == intent['digest'] and intent['body'].get('operation') == 'observe'
                                and intent['body'].get('target', {}).get('object_id') in preparation_media):
                            preparation_observers.add(obj['object_id'])
                    except (DomainError, ValueError, TypeError):
                        pass  # Unknown lineage must not be relabelled as source preparation.
            def is_plan(view: dict[str, Any]) -> bool:
                return (view['kind'] in PLAN or view['kind'] == 'candidate'
                        or view['object_ref']['object_id'] in preparation_media | preparation_observers)
            confirmed = [v for v in views if v['confirmed_final']]
            active = [v for v in views if v['current']]
            blockers = [v for v in active if v['status'] in ('failed', 'unknown') or v.get('verdict') in ('fail', 'uncertain')]
            processing = any(v['status'] in ('queued', 'dispatching', 'submitted', 'running', 'initializing') for v in active)
            producing = any(v['kind'] in ('candidate', 'job', 'composition-job') and v['object_ref']['object_id'] not in preparation_observers
                            or v['kind'] == 'media' and v['object_ref']['object_id'] not in preparation_media
                            for v in active)
            phase = 'confirmed' if confirmed else 'review' if any(v['kind'] == 'cut' for v in active) else 'production' if producing else 'preparation'
            return {'project_id': pid, 'title': _clean(project['body'].get('title', pid)), 'branch': project['body'].get('branch'),
                    'phase': phase, 'state': 'blocked' if blockers else 'processing' if processing else 'empty' if not views else 'ready-to-view',
                    'blockers': blockers, 'plan': [v for v in views if is_plan(v)],
                    'results': [v for v in views if not is_plan(v)],
                    'current_candidates': [v['object_ref'] for v in active if v['kind'] == 'candidate'],
                    'current_cuts': [v['object_ref'] for v in active if v['kind'] == 'cut'],
                    'confirmed_finals': [v['object_ref'] for v in confirmed], 'files': {'root': ''},
                    'blockers_scope': 'Object-local failures; execution permission must be checked by the production service.',
                    'progress_basis': 'Recorded work and unresolved evidence; not an artistic acceptance score.'}

    def _shot_source(self, pid: str, shot: dict[str, Any], db: sqlite3.Connection) -> dict[str, Any] | None:
        """The source segment a recreation shot recreates: a source-understanding on the card,
        else one reached through its scene; its range is the source shot."""
        def understanding_of(obj: dict[str, Any]) -> dict[str, Any] | None:
            for dep in obj['body'].get('dependencies', []):
                try:
                    found = self.store.get_object(pid, dep['object_id'], revision=dep['revision'], conn=db)
                except (DomainError, KeyError, TypeError):
                    continue
                if found['kind'] == 'source-understanding' and isinstance(found['body'].get('content'), dict):
                    return found
            return None
        found = understanding_of(shot)
        if found is None:
            for dep in shot['body'].get('dependencies', []):
                try:
                    parent = self.store.get_object(pid, dep['object_id'], revision=dep['revision'], conn=db)
                except (DomainError, KeyError, TypeError):
                    continue
                if parent['kind'] == 'scene':
                    found = understanding_of(parent)
                    if found:
                        break
        if found is None:
            return None
        content = found['body']['content']
        ref = _public_ref(content.get('source'))
        start, end = content.get('start_seconds'), content.get('end_seconds')
        if not ref or not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            return None
        try:
            media = self.store.get_object(pid, ref['object_id'], revision=ref['revision'], conn=db)
        except DomainError:
            return None
        if media['kind'] != 'media' or media['digest'] != ref['digest']:
            return None
        return {'media': ref, 'start_seconds': float(start), 'end_seconds': float(end), 'understanding': _ref(found)}

    def review_feed(self, actor: Principal, pid: str, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Everything the review desk shows, in one read: current requests, human picks, shots, takes being made."""
        with self.store._using(conn, write=False) as db:
            self.auth.authorize(actor, pid, 'context', conn=db)
            self.auth.authorize(actor, pid, 'artifacts', conn=db)
            kinds = ('decision-request', 'human-take-selection', 'batch', 'job', 'shoot-order', 'owner-note')
            rows = db.execute('SELECT object_id FROM objects WHERE project_id=? AND kind IN (?,?,?,?,?,?) ORDER BY object_id LIMIT 2001',
                              [pid, *kinds]).fetchall()
            if len(rows) > 2000:
                raise DomainError('insufficient_context', 'Project viewer inventory exceeds its explicit bound')
            objects = [self.store.get_object(pid, row[0], conn=db) for row in rows]
            requests = [o for o in objects if o['kind'] == 'decision-request']
            states = self.workflow.pinned_states(pid, [ObjectRef(**_ref(o)) for o in requests], conn=db)
            # every batch of a shot stays on the desk (a take request never expires, and an
            # earlier batch stays pickable after 再拍一批); other requests show only while current.
            request_views = [view for view in (self._view(pid, o, db, detail=True, graph_state=state)
                                               for o, state in zip(requests, states, strict=True))
                             if view['current'] or view['details'].get('purpose') == 'take']
            human = [o for o in objects if o['kind'] == 'human-take-selection' and o['author'] == 'decision_service'
                     and o['body'].get('verified_human_session') is True]
            order = creation_order(db, pid, [o['object_id'] for o in human])
            selections = [self._view(pid, o, db, detail=True) for o in sorted(human, key=lambda o: order[o['object_id']])]
            picks = current_picks(selections)
            jobs = {o['object_id']: o for o in objects if o['kind'] == 'job'}
            state = lambda job_id: jobs[job_id]['body'].get('state') if job_id in jobs else None
            making = []
            for batch in (o for o in objects if o['kind'] == 'batch'):
                view = self._view(pid, batch, db, detail=True)
                children = [c for c in view['details'].get('children', []) if isinstance(c.get('job'), dict)]
                if not any(state(c['job'].get('object_id')) in MAKING for c in children):
                    continue
                ids = [c['job']['object_id'] for c in children]
                making.append({'batch': view, 'jobs': {i: state(i) for i in ids},
                               'ready': {i: _public_ref(jobs[i]['body'].get('result')) for i in ids
                                         if state(i) == 'succeeded' and isinstance(jobs[i]['body'].get('result'), dict)}})
            # shots whose order is waiting, being listened to, or stopped with a reason.
            offered = {v['details'].get('target', {}).get('object_id') for v in request_views
                       if v['current'] and v['details'].get('purpose') == 'take'}
            # Only each card's latest order counts, so a stopped order that was re-ordered never lingers.
            orders = [o for o in objects if o['kind'] == 'shoot-order' and o['author'] == 'shoot_service']
            made = creation_order(db, pid, [o['object_id'] for o in orders])
            latest: dict[str, dict[str, Any]] = {}
            for o in sorted(orders, key=lambda o: made[o['object_id']]):
                for c in o['body'].get('cards', []):
                    if isinstance(c.get('card'), dict) and isinstance(c['card'].get('object_id'), str):
                        latest[c['card']['object_id']] = c
            shooting = [{'card': _public_ref(c['card']), 'shot': c.get('shot'), 'stage': c['stage'], 'reason': c.get('reason'),
                         'owner_reason': owner_reason(c) if c.get('stage') == 'stopped' else None}
                        for key, c in latest.items() if key not in offered
                        and c.get('stage') in ('waiting-plan', 'firing', 'listening', 'stopped')]
            targets = [d.get('target') for d in (v['details'] for v in request_views) if d.get('purpose') == 'take']
            targets += [c.get('target') for m in making for c in m['batch']['details'].get('children', [])]
            targets += [c['card'] for c in shooting]
            shots: dict[str, dict[str, Any]] = {}
            sources: dict[str, dict[str, Any]] = {}
            for ref in targets:
                if not isinstance(ref, dict) or f"{ref.get('object_id')}:{ref.get('revision')}" in shots:
                    continue
                try:
                    obj = self.store.get_object(pid, ref['object_id'], revision=ref['revision'], conn=db)
                except (DomainError, KeyError, TypeError):
                    continue  # The desk shows the shot by id.
                if obj['kind'] == 'shot' and obj['digest'] == ref.get('digest'):
                    shots[f"{obj['object_id']}:{obj['revision']}"] = self._view(pid, obj, db, detail=True)
                    source = self._shot_source(pid, obj, db)
                    if source:
                        sources[f"{obj['object_id']}:{obj['revision']}"] = source
            notes = [self._view(pid, o, db, detail=True) for o in objects
                     if o['kind'] == 'owner-note' and o['author'] == 'owner_note_service' and not o['body'].get('withdrawn')]
            return {'project_id': pid, 'requests': request_views, 'selections': selections, 'picks': picks,
                    'shots': shots, 'making': making, 'shooting': shooting, 'notes': notes, 'sources': sources,
                    'completions': self._completions(pid, request_views, db)}

    def _completions(self, pid: str, request_views: list[dict[str, Any]], db: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        """Per offered fal draft take: its 1080p completion — `draft` (not ordered yet),
        `making`, `ready` with the 正片 take, `failed`, or `expired` — and when the draft id runs out."""
        from production.assembly import MAKING as COMPLETING, completions
        found = completions(self.store, pid, conn=db)
        now = time.time()
        # why a pick stays at 480p, from AutoComplete's completion.stopped records.
        stops = {}
        for event in self.store.events(pid, conn=db):
            if event['kind'] == 'completion.stopped' and isinstance(event['body'].get('take'), dict):
                stops[event['body']['take'].get('object_id')] = event['body'].get('code')
        out: dict[str, dict[str, Any]] = {}
        for view in request_views:
            for take in (view['details'].get('evidence') or {}).get('takes') or []:
                if not isinstance(take, dict) or take.get('object_id') in out:
                    continue
                try:
                    media = self.store.get_object(pid, take['object_id'], revision=take['revision'], conn=db)
                except (DomainError, KeyError, TypeError):
                    continue
                output = (media['body'].get('provenance') or {}).get('provider_output') or {}
                if not output.get('draft_id'):
                    continue
                job = found.get(take['object_id'])
                expires = output.get('draft_expires_at')
                stop, note = stops.get(take['object_id']), None
                if job and job['state'] == 'succeeded' and isinstance(job.get('result'), dict):
                    state, result = 'ready', _public_ref(job['result'])
                elif job and job['state'] in COMPLETING:
                    state, result = 'making', None
                elif stop == 'budget_exceeded':
                    state, result, note = 'stopped', None, '预算不够，正片没做'
                elif job:
                    state, result = 'failed', None
                    note = '正片失败，已重试' if stop == 'attempt_limit' or (job.get('attempt') or 1) > 1 else '正片没做成'
                else:
                    expired = isinstance(expires, int) and expires <= now
                    state, result, note = ('expired', None, '样片已过期') if expired else ('draft', None, None)
                out[take['object_id']] = {'state': state, 'take': result, 'expires_at': expires, 'note': note}
        return out

    def project_tree(self, actor: Principal, pid: str, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """The project as the HF project page: 资产 (角色/地点/道具) · 场次 → 镜头 · 重拍 · 测试 · 成片. Grid items are
        images and videos only; text records ride in element details, folder text or the project notes."""
        with self.store._using(conn, write=False) as db:
            self.auth.authorize(actor, pid, 'context', conn=db)
            self.auth.authorize(actor, pid, 'artifacts', conn=db)
            project = self.store.get_object(pid, pid, conn=db)
            kinds = ('script', 'expectation', 'source-understanding', 'asset', 'scene', 'shot', 'candidate', 'media',
                     'human-take-selection', 'decision-request', 'dispatch-intent', 'human-receipt', 'brief', 'shoot-order')
            rows = db.execute('SELECT object_id FROM objects WHERE project_id=? AND kind IN (' + ','.join('?' for _ in kinds)
                              + ') ORDER BY object_id LIMIT 5001', [pid, *kinds]).fetchall()
            if len(rows) > 5000:
                raise DomainError('insufficient_context', 'Project tree exceeds its explicit bound')
            objects = [self.store.get_object(pid, row[0], conn=db) for row in rows]
            order = creation_order(db, pid, [o['object_id'] for o in objects])
            objects.sort(key=lambda o: order[o['object_id']])
            of = lambda kind: [o for o in objects if o['kind'] == kind]
            # HF project skeleton (production/HF_CANONICAL.md §11): ASSETS split into characters / locations /
            # props, scene folders with shot folders, regenerations, test, plus the film. Always present, so a new
            # project opens on the skeleton rather than an empty page.
            folders: list[dict[str, Any]] = [{'id': 'assets', 'parent': None, 'name': '资产'},
                       {'id': 'assets/characters', 'parent': 'assets', 'name': '角色'},
                       {'id': 'assets/locations', 'parent': 'assets', 'name': '地点'},
                       {'id': 'assets/props', 'parent': 'assets', 'name': '道具'}]
            items: list[dict[str, Any]] = []
            notes: list[dict[str, Any]] = []

            def plain(value: Any) -> str | None:
                return value.strip() if isinstance(value, str) and value.strip() else None

            def note(obj: dict[str, Any], name: str, text: Any) -> None:
                notes.append({'object_ref': _ref(obj), 'name': name, 'text': _clean(text if isinstance(text, str)
                              else json.dumps(text, ensure_ascii=False, indent=1))})

            def media_item(obj: dict[str, Any], folder: str, name: str, **details: Any) -> None:
                media_type = str(obj['body'].get('media_type') or '')
                items.append({'object_ref': _ref(obj), 'folder': folder,
                              'kind': 'video' if media_type.startswith('video/') else 'image' if media_type.startswith('image/') else 'file',
                              'name': name, 'media': _ref(obj), 'details': _clean(details)})

            for kind, name in (('script', '剧本'), ('expectation', '看片预期'), ('source-understanding', '原片理解')):
                for obj in of(kind):
                    note(obj, name, obj['body'].get('content'))
            media = {o['object_id']: o for o in of('media')}
            assets = [a for a in of('asset') if isinstance(a['body'].get('content'), dict)
                      and a['body']['content'].get('type', 'asset') == 'asset' and isinstance(a['body']['content'].get('tag'), str)]
            characters = {a['body']['content']['tag'] for a in assets
                          if a['body']['content'].get('role') in ('voice', 'behavior') or a['body']['content'].get('category') == 'character'
                          or '/cast/' in str(a['body'].get('logical_path') or '')}
            elements: dict[str, dict[str, Any]] = {}
            for asset in assets:
                content = asset['body']['content']
                tag, role, definition = content['tag'], content.get('role'), content.get('definition')
                fields = definition if isinstance(definition, dict) else {'descriptor': definition}
                if role == 'look':
                    # The look is a style prefix pasted into every prompt, not an element (HF_CANONICAL.md §6).
                    note(asset, '风格 ' + tag, fields.get('description') or fields.get('descriptor') or definition)
                    continue
                element = elements.setdefault(tag, {'tag': tag, 'images': [], 'descriptor': None, 'voice': None, 'behavior': None})
                if role == 'voice':
                    element['voice'] = plain(fields.get('voice')) or plain(fields.get('description')) or element['voice']
                    continue
                if role == 'behavior':
                    element['behavior'] = plain(fields.get('profile')) or plain(fields.get('description')) or element['behavior']
                    continue
                element['category'] = content.get('category') or element.get('category') or (
                    'environment' if role == 'world' else 'character' if tag in characters else 'prop')
                element['descriptor'] = plain(fields.get('descriptor')) or plain(fields.get('description')) or element['descriptor']
                for ref in content.get('media_refs') or []:
                    found = media.get(ref.get('object_id')) if isinstance(ref, dict) else None
                    if found is not None and found['object_id'] not in {m['object_id'] for m in element['images']}:
                        element['images'].append(found)
            group = {'character': 'assets/characters', 'environment': 'assets/locations', 'prop': 'assets/props'}
            for tag, element in elements.items():
                folder = group[element.get('category') or ('character' if tag in characters else 'prop')]
                details = {key: element[key] for key in ('descriptor', 'voice', 'behavior') if element[key]}
                if not element['images']:
                    items.append({'object_ref': None, 'folder': folder, 'kind': 'placeholder', 'name': tag,
                                  'media': None, 'details': _clean(details)})
                for n, image in enumerate(element['images']):
                    media_item(image, folder, tag if n == 0 else f'{tag} · {n + 1}', element=tag, **details)
            scenes = of('scene')
            for scene in scenes:
                content = scene['body'].get('content')
                heading = next((line[2:].strip() for line in str(content or '').splitlines() if line.startswith('# ')), None)
                folders.append({'id': 'scene:' + scene['object_id'], 'parent': None,
                                'name': heading or scene['body'].get('logical_path') or 'scene', 'text': _clean(content)})
            folders.append({'id': 'tests', 'parent': None, 'name': '测试'})
            folders.append({'id': 'film', 'parent': None, 'name': '成片'})
            human = [o for o in of('human-take-selection') if o['author'] == 'decision_service'
                     and o['body'].get('verified_human_session') is True]
            picks = current_picks([{'details': {'shot': o['body'].get('shot'), 'take': o['body'].get('take') or {}}} for o in human])
            shots = {s['object_id']: s for s in of('shot')}
            scene_ids = {s['object_id'] for s in scenes}
            candidates = {c['object_id']: c for c in of('candidate')}
            stress_shots = {c['body'].get('target', {}).get('object_id') for c in candidates.values() if c['body'].get('task') == 'stress'}
            shot_tasks = {c['body'].get('target', {}).get('object_id') for c in candidates.values() if c['body'].get('task') == 'shot'}
            for shot in shots.values():
                content = shot['body'].get('content')
                label = content.get('shot') if isinstance(content, dict) else None
                parent = next((d['object_id'] for d in shot['body'].get('dependencies', [])
                               if isinstance(d, dict) and d.get('object_id') in scene_ids), None)
                # A probe card is a test whether or not it was shot yet (AGENT_GUIDE: probes use S01-901A-style ids).
                test = ((shot['object_id'] in stress_shots and shot['object_id'] not in shot_tasks)
                        or bool(label and re.search(r'-9\d\d[A-Z]$', label)) or '/probes/' in str(shot['body'].get('logical_path') or ''))
                direction = content.get('Direction') if isinstance(content, dict) else None
                goal = direction.get('the goal of the shot in one line') if isinstance(direction, dict) else None
                folders.append({'id': 'shot:' + shot['object_id'],
                                'parent': 'tests' if test else 'scene:' + parent if parent else None,
                                'name': label or shot['object_id'][-8:], 'text': _clean(plain(goal))})
                # a recreation shot shows the source segment it recreates, first in its folder.
                source = None if test else self._shot_source(pid, shot, db)
                if source:
                    clip = self.store.get_object(pid, source['media']['object_id'], revision=source['media']['revision'], conn=db)
                    media_item(clip, 'shot:' + shot['object_id'],
                               f"原片 {source['start_seconds']:g}–{source['end_seconds']:g} 秒",
                               source=True, source_start=source['start_seconds'], source_end=source['end_seconds'])
            # A worker take depends on its dispatch intent, which names the candidate (production/jobs.py:368).
            intents = {i['object_id']: (i['body'].get('candidate') or {}).get('object_id') for i in of('dispatch-intent')}
            def take_source(take: dict[str, Any]) -> dict[str, Any] | None:
                for dependency in take['body'].get('dependencies', []):
                    oid = dependency.get('object_id') if isinstance(dependency, dict) else None
                    oid = intents.get(oid, oid)
                    if oid in candidates:
                        return candidates[oid]
                return None
            # Every take stays in its own shot folder, numbered within its prompt version as on the desk; a shot
            # with several versions labels them "第 k 版" (bug hunt picks had vanished into 重拍).
            owned: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
            asset_takes: dict[str, int] = {}
            asset_objects = {a['object_id']: a for a in of('asset')}
            for take in media.values():
                if take['author'] != 'worker_service':
                    continue
                source = take_source(take)
                target_id = ((source or {}).get('body', {}).get('target') or {}).get('object_id')
                owner = shots.get(target_id)
                if source is not None and owner is not None:
                    owned.append((take, source, owner))
                elif source is not None and target_id in asset_objects:
                    # a generated asset image lands in its ASSETS folder (HF_CANONICAL.md §1).
                    content = asset_objects[target_id]['body'].get('content') if isinstance(asset_objects[target_id]['body'].get('content'), dict) else {}
                    tag = str(content.get('tag') or target_id[-8:])
                    category = content.get('category') or ('environment' if content.get('role') == 'world' else
                                                          'character' if tag in characters else 'prop')
                    asset_takes[tag] = asset_takes.get(tag, 0) + 1
                    compilation = source['body'].get('compilation') or {}
                    parameters = compilation.get('parameters') if isinstance(compilation.get('parameters'), dict) else {}
                    media_item(take, group.get(category, 'assets/props'), f'{tag} · 第 {asset_takes[tag]} 条', element=tag,
                               prompt=compilation.get('prompt'), model=compilation.get('job_type'),
                               aspect_ratio=parameters.get('aspect_ratio'), resolution=parameters.get('resolution'))
            versions: dict[str, list[str]] = {}
            for _take, source, owner in owned:
                seen = versions.setdefault(owner['object_id'], [])
                if source['object_id'] not in seen:
                    seen.append(source['object_id'])
            number: dict[str, int] = {}
            named: dict[str, tuple[str, str]] = {}
            # A take keeps the number the desk showed: its place in the batch it was offered in (live: fal finishes
            # takes out of order, so finish order said 第 3 条 for the desk's take 1). Takes never offered count on.
            offered_no = {t.get('object_id'): n for d in of('decision-request')
                          if d['author'] == 'decision_service' and d['body'].get('purpose') == 'take'
                          for n, t in enumerate((d['body'].get('evidence') or {}).get('takes') or [], 1) if isinstance(t, dict)}
            for take, source, owner in owned:
                number[source['object_id']] = number.get(source['object_id'], 0) + 1
                kinds = versions[owner['object_id']]
                n = offered_no.get(take['object_id'], number[source['object_id']])
                name = (f"第 {kinds.index(source['object_id']) + 1} 版 · " if len(kinds) > 1 else '') + f"第 {n} 条"
                pick = picks.get(owner['object_id'])
                compilation = source['body'].get('compilation') or {}
                parameters = compilation.get('parameters') if isinstance(compilation.get('parameters'), dict) else {}
                # which manuals the writer used, how many sections are the writer's, what this version changes.
                authorship = compilation.get('authorship') if isinstance(compilation.get('authorship'), dict) else {}
                named[take['object_id']] = ('shot:' + owner['object_id'], name)
                sent = compilation.get('prompt') or (compilation.get('assembly') or {}).get('prompt')  # the text sent
                media_item(take, 'shot:' + owner['object_id'], name,
                           picked=bool(pick and pick['details']['take'] == _ref(take)),
                           shot=owner['body'].get('content', {}).get('shot'),
                           prompt=sent, **_additions(source, sent),
                           model=compilation.get('job_type'), duration=parameters.get('duration'),
                           aspect_ratio=parameters.get('aspect_ratio'), resolution=parameters.get('resolution'),
                           playbook_version=authorship.get('playbook_version'), change_note=authorship.get('change_note'),
                           writer=(f"{authorship['written']}/{authorship['variable']}" if 'written' in authorship else None))
            # a 1080p completion sits beside the draft it completes, as its 正片.
            for full in media.values():
                draft = full['body'].get('completes') if full['author'] == 'worker_service' else None
                if isinstance(draft, dict) and draft.get('object_id') in named:
                    folder, name = named[draft['object_id']]
                    media_item(full, folder, name + ' · 正片', completes=draft['object_id'], resolution='1080p')
            finals = {f['body'].get('target', {}).get('object_id'): f['body'].get('state') for f in of('decision-request')
                      if f['author'] == 'decision_service' and f['body'].get('purpose') == 'final'}
            for film in media.values():
                if film['author'] == 'cut_service' and film['body'].get('source_cut'):
                    media_item(film, 'film', film['body'].get('label') or '成片', final=finals.get(film['object_id']))
            counts: dict[str, int] = {}
            for item in items:
                counts[item['folder']] = counts.get(item['folder'], 0) + 1
            for folder in folders:
                folder['count'] = counts.get(folder['id'], 0)
            # per shot, the shotlist row (cully-hill-boys.txt:35-40) and the version log
            # "version / what changed / verdict" (cully-hill-boys.txt:134, hell-grind.txt:106), from existing records.
            # The latest answer of each take request wins (a pick, then its withdrawal).
            take_receipts = [r for r in of('human-receipt') if r['author'] == 'decision_service' and r['body'].get('purpose') == 'take']
            answered_order = creation_order(db, pid, [r['object_id'] for r in take_receipts])
            receipts = {(r['body'].get('decision') or {}).get('object_id'): r
                        for r in sorted(take_receipts, key=lambda r: answered_order[r['object_id']])}
            take_requests = [d for d in of('decision-request') if d['author'] == 'decision_service' and d['body'].get('purpose') == 'take']
            by_folder: dict[str, dict[str, Any]] = {f['id']: f for f in folders}
            # (Q6): the latest order of each card carries the reviewer's notes and the
            # writer's answers; none shows 没审. The latest candidate's manuals version shows 手册不是最新 when old.
            shoot_orders = [o for o in of('shoot-order') if o['author'] == 'shoot_service']
            made_order = creation_order(db, pid, [o['object_id'] for o in shoot_orders])
            reviews: dict[str, dict[str, Any] | None] = {}
            for o in sorted(shoot_orders, key=lambda o: made_order[o['object_id']]):
                for c in o['body'].get('cards', []):
                    if isinstance(c.get('card'), dict) and isinstance(c['card'].get('object_id'), str):
                        reviews[c['card']['object_id']] = o['body'].get('review') if isinstance(o['body'].get('review'), dict) else None
            from production import playbook
            current_manuals = playbook.version()
            for shot in shots.values():
                folder = by_folder.get('shot:' + shot['object_id'])
                if folder is None:
                    continue
                label = (shot['body'].get('content') or {}).get('shot') if isinstance(shot['body'].get('content'), dict) else None
                ordered_review = reviews.get(shot['object_id'])
                latest = max((c for c in candidates.values() if c['body'].get('target', {}).get('object_id') == shot['object_id']),
                             key=lambda c: c['body']['target'].get('revision', 0), default=None)
                version = ((latest or {}).get('body', {}).get('compilation') or {}).get('authorship', {}).get('playbook_version')\
                    if latest else None
                folder['review'] = _clean({
                    'ordered': shot['object_id'] in reviews, 'reviewed': ordered_review is not None,
                    'reviewer': (ordered_review or {}).get('reviewer'),
                    'notes': [n for n in (ordered_review or {}).get('notes', []) if isinstance(n, dict) and n.get('shot') in (None, label)],
                    'manuals_current': None if latest is None else version == current_manuals})
                content = shot['body'].get('content') if isinstance(shot['body'].get('content'), dict) else {}
                camera = content.get('Camera') if isinstance(content.get('Camera'), dict) else {}
                material = content.get('The material') if isinstance(content.get('The material'), dict) else {}
                direction = content.get('Direction') if isinstance(content.get('Direction'), dict) else {}
                mine = [(take, source) for take, source, owner in owned if owner['object_id'] == shot['object_id']]
                order_of = versions.get(shot['object_id'], [])
                pick = picks.get(shot['object_id'])
                picked = pick['details']['take'] if pick else None
                log = []
                for n, cid in enumerate(order_of, 1):
                    source = candidates[cid]
                    authorship = (source['body'].get('compilation') or {}).get('authorship') or {}
                    refs = [_ref(t) for t, s2 in mine if s2['object_id'] == cid]
                    verdict, reason = None, None
                    for request in take_requests:
                        offered = (request['body'].get('evidence') or {}).get('takes') or []
                        if not any(_public_ref(t) in refs for t in offered):
                            continue
                        receipt = receipts.get(request['object_id'])
                        if receipt is not None:
                            choice = receipt['body'].get('choice')
                            # withdrawing a pick answers decline and puts the batch back to
                            # pending; it is 撤销, not 不行 (audit: queries.py mapped it to 不行).
                            answered = receipt['body'].get('decision') or {}
                            state_after = self.store.get_object(pid, answered['object_id'], revision=answered.get('revision'),
                                                                conn=db)['body'].get('state') if answered.get('object_id') else None
                            verdict = ('选了' if choice == 'confirm' else '撤销' if choice == 'undo' or state_after == 'pending'
                                       else '不行')
                            reason = _clean(receipt['body'].get('reason'))
                    if picked and picked in refs:
                        verdict = '选了'
                    log.append({'version': n, 'change_note': _clean(authorship.get('change_note')), 'takes': len(refs),
                                'verdict': verdict, 'reason': reason})
                folder['shot'] = _clean({'goal': direction.get('the goal of the shot in one line'), 'size': camera.get('shot size'),
                                         'lens': camera.get('lens'), 'duration': material.get('the running time in seconds'),
                                         'versions': len(order_of), 'takes': len(mine),
                                         'status': '已选' if picked else '待选' if mine else '未拍'})
                folder['log'] = log
                if len(order_of) >= 10:
                    # hell-grind.txt:106: ten to fifteen versions, then simplify the shot, not the wording. Advice only.
                    folder['advice'] = 'HF：10–15 版还不成，就简化这个镜头（拆成两个、减动作、换角度），别只改措辞'
            element_counts: dict[str, int] = {}
            for item in items:
                if item['folder'].startswith('assets/'):
                    element_counts[item['folder']] = element_counts.get(item['folder'], 0) + 1
            models = sorted({str((c['body'].get('compilation') or {}).get('job_type')) for c in candidates.values()
                             if (c['body'].get('compilation') or {}).get('job_type')})
            briefs = of('brief')
            shot_folders = [f for f in folders if f['id'].startswith('shot:')]
            # HF's brief outline, majority sections only (HF_CANONICAL.md §7).
            brief = {'stage': project_stage(db, pid), 'logline': _clean(project['body'].get('brief')),
                     'about': _clean(briefs[-1]['body'].get('content')) if briefs else None,
                     'tools': models,
                     'pre_production': {'角色': element_counts.get('assets/characters', 0), '地点': element_counts.get('assets/locations', 0),
                                        '道具': element_counts.get('assets/props', 0)},
                     'production': {'场': len(scenes), '镜头': len(shot_folders), '版本': sum(len(v) for v in versions.values()),
                                    '条': len(owned), '已选': sum(1 for f in shot_folders if (f.get('shot') or {}).get('status') == '已选')},
                     'post_production': {'成片': sum(1 for i in items if i['folder'] == 'film'),
                                         '已定稿': any(i['details'].get('final') == 'confirmed' for i in items if i['folder'] == 'film')}}
            return {'project_id': pid, 'title': _clean(project['body'].get('title', pid)), 'folders': folders, 'items': items,
                    'notes': notes, 'stage': brief['stage'], 'brief': brief}

    def generation_record(self, actor: Principal, pid: str, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Every paid generation of the project: what it made, where it
        belongs, when, whether it worked and what it cost in the account's own unit (settled, else the held maximum)."""
        from production.billing import bill
        with self.store._using(conn, write=False) as db:
            self.auth.authorize(actor, pid, 'context', conn=db)
            self.auth.authorize(actor, pid, 'artifacts', conn=db)
            ledger = bill(self.store, pid, conn=db)
            get = lambda ref: self.store.get_object(pid, ref['object_id'], revision=ref.get('revision'), conn=db)
            jobs = {j['body'].get('intent', {}).get('object_id'): j for j in self.store.list_objects(pid, kind='job', conn=db)
                    if isinstance(j['body'].get('intent'), dict)}
            scenes = {o['object_id']: next((line[2:].strip() for line in str(o['body'].get('content') or '').splitlines()
                                            if line.startswith('# ')), None) for o in self.store.list_objects(pid, kind='scene', conn=db)}
            candidates = {o['object_id']: o for o in self.store.list_objects(pid, kind='candidate', conn=db)}
            intents = {o['object_id']: (o['body'].get('candidate') or {}).get('object_id')
                       for o in self.store.list_objects(pid, kind='dispatch-intent', conn=db)}
            take_numbers: dict[str, int] = {}
            order = creation_order(db, pid, [m['object_id'] for m in self.store.list_objects(pid, kind='media', conn=db)])
            per_shot: dict[str, int] = {}
            for media_id in sorted(order, key=order.__getitem__):
                media = self.store.get_object(pid, media_id, conn=db)
                # A worker take depends on its dispatch intent, which names the candidate (production/jobs.py:368).
                source = next((candidates[intents.get(d['object_id'], d['object_id'])] for d in media['body'].get('dependencies', [])
                               if isinstance(d, dict) and intents.get(d.get('object_id'), d.get('object_id')) in candidates), None)
                shot_id = ((source or {}).get('body', {}).get('target') or {}).get('object_id')
                if media['author'] == 'worker_service' and shot_id:
                    per_shot[shot_id] = per_shot.get(shot_id, 0) + 1
                    take_numbers[media_id] = per_shot[shot_id]

            def where(shot_ref: Any, take_id: str | None = None) -> dict[str, Any]:
                if not isinstance(shot_ref, dict) or not shot_ref.get('object_id'):
                    return {}
                try:
                    shot = self.store.get_object(pid, shot_ref['object_id'], conn=db)
                except DomainError:
                    return {}
                content = shot['body'].get('content') if isinstance(shot['body'].get('content'), dict) else {}
                scene = next((scenes[d['object_id']] for d in shot['body'].get('dependencies', [])
                              if isinstance(d, dict) and d.get('object_id') in scenes), None)
                return {'scene': scene, 'shot': content.get('shot'), 'take': take_numbers.get(take_id) if take_id else None}

            def take_shot(media_ref: Any) -> Any:
                try:
                    media = get(media_ref)
                except (DomainError, KeyError, TypeError):
                    return None
                source = next((candidates[intents.get(d['object_id'], d['object_id'])] for d in media['body'].get('dependencies', [])
                               if isinstance(d, dict) and intents.get(d.get('object_id'), d.get('object_id')) in candidates), None)
                return (source or {}).get('body', {}).get('target')

            JOB_STATUS = {'succeeded': '成功', 'completed': '成功', 'failed': '失败', 'cancelled': '已取消', 'unknown': '结果不明'}
            rows: list[dict[str, Any]] = []
            totals: dict[str, dict[str, Any]] = {}
            by_model: dict[str, dict[str, Any]] = {}
            for line in ledger['rows']:
                target = line['target']
                row: dict[str, Any] = {'time': line['time'], 'what': '其它', 'model': None, 'status': '进行中',
                                       'location': {}, 'media': None,
                                       'cost': {'amount': line['actual'] if line['state'] == 'settled' else line['reserved_max'],
                                                'unit': line['unit'], 'settled': line['state'] == 'settled'}}
                try:
                    obj = self.store.get_object(pid, target['object_id'], conn=db) if target.get('object_id') else None
                except DomainError:
                    obj = None
                body: dict[str, Any] = obj['body'] if obj and isinstance(obj['body'], dict) else {}
                if obj and obj['kind'] == 'dispatch-intent':
                    job = jobs.get(obj['object_id'])
                    operation = str(body.get('operation') or '其它')
                    raw_request = body.get('request')
                    request: dict[str, Any] = raw_request if isinstance(raw_request, dict) else {}
                    row['model'] = request.get('job_type') or body.get('route_key')
                    row['what'] = {'submit': '片子' if 'video' in str(row['model']) or 'seedance' in str(row['model']) else '出图',
                                   'observe': '看片理解', 'render-cut': '成片渲染'}.get(operation, operation)
                    state = (job or {}).get('body', {}).get('state')
                    row['status'] = JOB_STATUS.get(str(state), '进行中')
                    result = (job or {}).get('body', {}).get('result')
                    if isinstance(result, dict):
                        row['media'] = _public_ref(result)
                    shot_ref = body.get('target') if operation == 'submit' else None
                    row['location'] = where(shot_ref, result.get('object_id') if isinstance(result, dict) else None)
                elif obj and obj['kind'] == 'review-task':
                    row['what'] = {'take': '导演看片', 'cut': '导演看成片', 'preflight': '开拍前审查', 'asset': '资产审查'}.get(str(body.get('purpose')), '审查')
                    row['model'] = body.get('route_profile_id')
                    row['status'] = JOB_STATUS.get(str(body.get('state')), '进行中')
                    row['location'] = where(take_shot(body.get('target')))
                row = _clean(row)
                rows.append(row)
                bucket = totals.setdefault(line['budget_key'], {'unit': line['unit'], 'settled': 0, 'unsettled_max': 0, 'count': 0})
                bucket['count'] += 1
                bucket['settled' if row['cost']['settled'] else 'unsettled_max'] += row['cost']['amount'] or 0
                model = by_model.setdefault(row['model'] or '其它', {'count': 0, 'unit': line['unit']})
                model['count'] += 1
            return {'project_id': pid, 'rows': rows, 'totals': totals, 'by_model': by_model}

    def artifact(self, actor: Principal, pid: str, object_id: str, revision: int | None = None, *,
                 conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        with self.store._using(conn, write=False) as db:
            self.auth.authorize(actor, pid, 'artifacts', conn=db)
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', object_id) or revision is not None and (type(revision) is not int or revision < 1):
                raise DomainError('invalid_input', 'Invalid exact artifact reference')
            obj = self.store.get_object(pid, object_id, revision=revision, conn=db)
            return self._view(pid, obj, db, detail=True)

    def tree(self, actor: Principal, pid: str, parent: str = '', *,
             conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        if parent:
            try:
                parent = safe_logical_path(parent)
            except ValueError:
                raise DomainError('invalid_input', 'Invalid logical directory path') from None
        with self.store._using(conn, write=False) as db:
            self.auth.authorize(actor, pid, 'artifacts', conn=db)
            entries: dict[str, dict[str, Any]] = {}
            prefix = parent+'/' if parent else ''
            for obj in self._objects(pid, db):
                path = obj['body'].get('logical_path')
                if not path:
                    if obj['kind'] not in ('media', 'cut', 'candidate'):
                        continue
                    path = f'Results/{obj["kind"]}/{obj["object_id"]}'
                try:
                    path = safe_logical_path(path)
                except (ValueError, DomainError):
                    continue
                if not path.startswith(prefix):
                    continue
                tail = path[len(prefix):]
                name, separator, _ = tail.partition('/')
                child = prefix+name
                if separator:
                    entries[child] = {'name': name, 'path': child, 'type': 'directory'}
                else:
                    entries[child] = {'name': name, 'path': child, 'type': 'file', 'object_ref': _ref(obj), 'link': self._link(pid, obj)}
            return sorted(entries.values(), key=lambda entry: (entry['type'] != 'directory', entry['name']))

    def copy_reference(self, actor: Principal, pid: str, objref: ObjectRef, seconds: float | None = None) -> dict[str, Any]:
        with self.store.transaction(write=False) as db:
            self.auth.authorize(actor, pid, 'artifacts', conn=db)
            obj = self.store.get_object(pid, objref.object_id, revision=objref.revision, conn=db)
            if objref.digest != obj['digest']:
                raise DomainError('stale_input', 'Copy reference must bind the exact version hash')
            self._view(pid, obj, db)
            duration = obj['body'].get('probe', {}).get('duration')
            if seconds is not None and (type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0
                    or obj['kind'] != 'media' or not isinstance(duration, (int, float)) or seconds > duration):
                raise DomainError('invalid_input', 'Playback position is outside the actual media')
            payload = {'project_id': pid, 'object_ref': _ref(obj), 'playback_seconds': seconds,
                       'link': self._link(pid, obj), 'source_mapping': None}
            cutref = obj['body'].get('source_cut')
            if cutref and seconds is not None and obj['author'] == 'cut_service' and obj['body'].get('assembly_manifest') == cutref:
                cut = self.store.get_object(pid, cutref['object_id'], revision=cutref['revision'], conn=db)
                if cut['digest'] == cutref.get('digest') and cut['author'] == 'cut_service':
                    for segment in cut['body']['segments']:
                        start = segment['cut_start_seconds']
                        if start <= seconds < start+segment['duration_seconds']:
                            payload['source_mapping'] = {'object_ref': segment['take'],
                                'source_seconds': segment['start_seconds']+seconds-start,
                                'basis': 'declared-cut-source-time', 'frame_quantized': True, 'exact_frame_match_verified': False}
                            break
            return payload
