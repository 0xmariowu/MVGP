"""Preflight on immutable compiler output, not author gate claims.

Only money, provider limits and send integrity block a take (owner 2026-09-26): a forged or altered candidate, an unsupported route, the
reference budget/manifest/order, the duration window, the prompt size bound, and
a sent prompt that is not the writer's text plus allowed additions
(production/prompt.py). Those surface as exceptions that stop evaluation. Every
other finding (the writer advice, the shot list) is returned as advice, the way
HF keeps its rules in the writer's self-check, not in the platform. It never
issues generation authority.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any

from production import prompt as prompts
from production.context import RELATED_KINDS, context_body, related_history
from production.contracts import DomainError, ObjectRef, content_hash
from production.media import MediaStore
from production.provider_fal import MAX_PROMPT_BYTES
from production.store import Store
from production.workflow import Workflow

# fal takes at most 30 reference images (fal OpenAPI, runtime.json max_references).
MAX_IMAGES = 30
# A runaway prompt is a send fault. The bound is the fal adapter's own, so preflight stops what the worker would
# refuse; the largest of HF's 21,762 prompts is 52,950 UTF-8 bytes (gens-full.jsonl, measured 2026-09-27).
# fal publishes no prompt limit in its capability document.
PROMPT_MAX_BYTES = MAX_PROMPT_BYTES


def shotlist_check(store: Store, pid: str, body: dict[str, Any], db: sqlite3.Connection) -> dict[str, Any] | None:
    """authority.shotlist for one shot candidate body: its scene's shot-list section names the shot.

    Returns None when the rule does not apply (not a shot task, or the card has no string shot id);
    otherwise {'status': 'pass'|'deny', 'reason', 'evidence'}. Shared by preflight and by the explicit
    re-check of historical takes (production/authority_change.py).
    """
    if body.get('task') != 'shot':
        return None
    target_ref = ObjectRef.model_validate(body['target'])
    target = store.get_object(pid, target_ref.object_id, revision=target_ref.revision, conn=db)
    card = target['body'].get('content')
    if not (isinstance(card, dict) and isinstance(card.get('shot'), str)):
        return None
    shot = card['shot']
    scene = None
    for dependency in target['body'].get('dependencies', []):
        dependency_ref = ObjectRef.model_validate(dependency)
        dependency_obj = store.get_object(pid, dependency_ref.object_id, revision=dependency_ref.revision, conn=db)
        if dependency_obj['kind'] == 'scene':
            scene = dependency_obj
            break
    if scene is None:
        return {'status': 'deny', 'reason': 'Shot card declares no scene artifact',
                'evidence': [{'target': _ref(target), 'shot': shot, 'dependencies': target['body'].get('dependencies', [])}]}
    scene_content = scene['body'].get('content')
    if isinstance(scene_content, dict):
        scene_content = next((scene_content[key] for key in ('content', 'text')
                              if isinstance(scene_content.get(key), str)), None)
    evidence: list[dict[str, Any]] = [{'scene': _ref(scene), 'shot': shot}]
    repair = (f'Update scene {scene["object_id"]} revision {scene["revision"]} '
              f'to list {shot} in a shot-list section, then prepare a new candidate.')
    if not isinstance(scene_content, str):
        return {'status': 'deny', 'reason': 'Scene artifact content is malformed', 'evidence': [*evidence, {'repair': repair}]}
    section: list[str] = []
    in_section = False
    for line in scene_content.splitlines():
        if line.startswith('## '):
            in_section = False
        if re.match(r'^#{1,6}[ \t]+.*(?:镜头清单|Coverage draft|Preliminary coverage)', line):
            in_section = True
        elif in_section:
            section.append(line)
    ids = '|'.join(re.escape(value) for value in (shot, shot.split('-', 1)[-1]))
    listed = bool(shot and re.search(rf'(?<![\w-])(?:{ids})(?=[, |])', '\n'.join(section)))
    if listed:
        return {'status': 'pass', 'reason': 'Scene shot list includes this shot', 'evidence': evidence}
    return {'status': 'deny', 'reason': 'Scene shot list does not include this shot', 'evidence': [*evidence, {'repair': repair}]}


def _ref(obj: dict[str, Any]) -> dict[str, Any]:
    return {key: obj[key] for key in ('object_id', 'revision', 'digest')}


class Gates:
    def __init__(self, store: Store, workflow: Workflow, media: MediaStore) -> None:
        self.store, self.workflow, self.media = store, workflow, media

    def _context_current(self, pid: str, body: dict[str, Any], db: sqlite3.Connection) -> list[tuple[str, str]]:
        """Raise on integrity faults; return (message, field) advice for context that moved on since preparation."""
        advice: list[tuple[str, str]] = []
        context = body['context']
        expected = context['context_hash']
        if body['context_hash'] != expected or content_hash({k: v for k, v in context.items() if k != 'context_hash'}) != expected:
            raise DomainError('stale_input', 'Frozen context fingerprint differs', field='context', rule_id='prompt.provenance')
        project = self.store.get_object(pid, pid, conn=db)
        if context['project']['digest'] != project['digest']:
            advice.append(('Project context changed after preparation', 'context.project'))
        frozen = {(r['object_ref']['object_id'], r['object_ref']['revision']) for r in body['input_manifest'] if r['frozen_selection']}
        for item in context['preparation_inputs']:
            ref = ObjectRef.model_validate(item['object_ref'])
            obj = self.store.get_object(pid, ref.object_id, revision=ref.revision, conn=db)
            if obj['digest'] != ref.digest or context_body(obj) != item['body'] or obj['kind'] != item['kind']:
                raise DomainError('stale_input', 'Prepared context does not match exact input')
            if (ref.object_id, ref.revision) not in frozen and self.store.get_object(pid, ref.object_id, conn=db)['revision'] != ref.revision:
                raise DomainError('stale_input', 'Prepared creative input changed')
        # Filter before expanding immutable payloads. This check uses only shot
        # neighbors and the same history nodes/dispatch bridges as before, not
        # the project's potentially large private review transcripts. Preserve
        # the inventory order consumed by related_history.
        objects = sorted((obj for kind in RELATED_KINDS | {'shot', 'dispatch-intent'}
                          for obj in self.store.list_objects(pid, kind=kind, conn=db)),
                         key=lambda obj: obj['object_id'])
        ids = {r['object_id'] for r in context['dependencies']}
        scene_ids = {s['object_id'] for s in context['creative']['scenes']}
        neighbors = [_ref(o) for o in objects if o['kind'] == 'shot' and o['object_id'] not in ids
                     and scene_ids.intersection(r['object_id'] for r in o['body'].get('dependencies', []))]
        # Only new standalone-probe contexts declare this scope. Historical
        # reviews retain the exact neighboring material they actually consumed.
        if (body['task'] == context['task'] == 'stress'
                and context['neighbors'].get('scope') == 'explicit_dependencies'):
            neighbors = []
        expected_neighbors = [{k: o[k] for k in ('object_id', 'revision', 'digest')} for o in context['neighbors']['shots']]
        if sorted(neighbors, key=str) != sorted(expected_neighbors, key=str):
            advice.append(('Neighboring shot plan changed', 'context.neighbors'))
        relevant = ({r['object_id'] for r in context['history_roots']} if 'history_roots' in context
                    else ids | {item['object_ref']['object_id'] for item in context['preparation_inputs']})
        feedback = [_ref(o) for o in related_history(objects, relevant) if o['kind'] == 'feedback']
        old_feedback = [{k: o[k] for k in ('object_id', 'revision', 'digest')} for o in context['prior_results'] if o['kind'] == 'feedback']
        if sorted(feedback, key=str) != sorted(old_feedback, key=str):
            advice.append(('Related feedback changed after preparation', 'context.prior_results'))
        return advice

    def evaluate(self, pid: str, candidate: ObjectRef, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Trusted service read; API/submit authenticates before calling this method."""
        checks: list[dict[str, Any]] = []
        result: dict[str, Any] = {'candidate': candidate.model_dump(), 'checks': checks,
            'blocking': [], 'required_roles': [], 'mechanical_pass': False, 'generation_authorized': False}
        rules: dict[str, dict[str, Any]] = {}
        def add(rule_id: str, status: str, message: str, path: str, *, source: Any = None, evidence: Any = None,
                hard: bool = False) -> None:
            rule = rules.get(rule_id, {})
            if status == 'deny' and not hard:
                status = 'advisory'
            checks.append({'rule_id': rule_id, 'status': status, 'message': message, 'path': path,
                           'severity': 'deny' if status == 'deny' else rule.get('severity', 'info'),
                           'source': source if source is not None else rule.get('source_refs', ['production/SPEC.md']),
                           'evidence': evidence or []})
        try:
            with self.store._using(conn, write=False) as db:
                graph = self.workflow.pinned_graph(pid, candidate, conn=db)
                obj = self.store.get_object(pid, candidate.object_id, revision=candidate.revision, conn=db)
                if obj['kind'] != 'candidate' or obj['revision'] != 1 or obj['author'] != 'compiler_service':
                    raise DomainError('forbidden', 'Candidate was not issued immutably by the compiler', rule_id='authority.no_bypass')
                if graph['stale']:
                    raise DomainError('stale_input', 'Candidate dependency graph changed', rule_id='version.stale')
                body, compiled = obj['body'], obj['body']['compilation']
                applicable = self.workflow.applicable_method(pid, ObjectRef.model_validate(body['method_selection']), body['task'], conn=db)
                if (body['release_id'] != applicable['release_id'] or body['method_id'] != applicable['method_id']
                        or body['target'] != applicable['target']):
                    raise DomainError('release_mismatch', 'Candidate method/release/target differs', rule_id='method.support')
                for message, field in self._context_current(pid, body, db):
                    add('version.stale', 'deny', message, field)
                self.workflow.guard_mutation(self.store, pid, body['target']['object_id'], db)
                expected_wire = {'job_type': compiled['job_type'], 'params': {'prompt': compiled['prompt'], **compiled['parameters']},
                                 'references': compiled['references']}
                if body['request'] != expected_wire or body['dependencies'] != compiled['dependency_roots'] or body['input_manifest'] != compiled['input_manifest']:
                    raise DomainError('rule_violation', 'Wire request or input closure differs from compilation', rule_id='prompt.provenance', field='request')
                role = 'image_routes' if body['task'] in ('image', 'image-edit') else 'video_routes'
                routes = self.workflow.config.section(role)
                profile = routes['profiles'][compiled['profile_id']]
                if (content_hash(profile) != compiled['profile_hash'] or routes['method_routes'][body['method_id']] != compiled['profile_id']
                        or profile['job_type'] != expected_wire['job_type'] or body['task'] not in profile['tasks']):
                    raise DomainError('unsupported_route', 'Candidate route differs from released profile', rule_id='method.support')
                refs = expected_wire['references']
                if len(refs) > min(profile['max_references'], MAX_IMAGES):
                    raise DomainError('rule_violation', 'Reference budget exceeded', rule_id='refs.budget')
                known_refs = {(r['object_ref']['object_id'], r['object_ref']['revision'], r['object_ref']['digest']) for r in body['input_manifest']}
                for index, reference in enumerate(refs):
                    ref = ObjectRef.model_validate(reference['object_ref'])
                    if (ref.object_id, ref.revision, ref.digest) not in known_refs:
                        raise DomainError('rule_violation', 'Reference is outside prepared inputs', rule_id='refs.manifest')
                    media = self.store.get_object(pid, ref.object_id, revision=ref.revision, conn=db)
                    if media['kind'] != 'media' or media['digest'] != ref.digest or media['body']['sha256'] != reference['sha256']:
                        raise DomainError('invalid_media', 'Reference metadata differs from exact media', rule_id='refs.manifest')
                    actual = self.media.path_for(pid, ref.object_id, revision=ref.revision)
                    if actual.stat().st_size != reference.get('byte_length', reference.get('bytes')):
                        raise DomainError('invalid_media', 'Reference byte length differs', rule_id='refs.manifest')
                    if media['body']['media_type'] != reference['media_type']:
                        raise DomainError('invalid_media', 'Reference modality differs', rule_id='refs.manifest')
                    # Images and videos are numbered apart, each from 1 in order.
                    same = [r for r in refs[:index + 1] if str(r.get('media_type', '')).startswith('video/')
                            == str(reference.get('media_type', '')).startswith('video/')]
                    if 'n' in reference and reference['n'] != len(same):
                        raise DomainError('rule_violation', 'Reference order differs', rule_id='refs.manifest')
                params = expected_wire['params']
                if params['resolution'] not in profile['resolutions'] or params['aspect_ratio'] not in profile['aspect_ratios']:
                    raise DomainError('unsupported_route', 'Critical output parameters exceed released profile')
                if body['task'] in ('shot', 'stress'):
                    # A Higgsfield shot with no reference is text-to-video; with any it is the profile's mode.
                    mode = None if profile['job_type'].startswith('fal_') else 't2v' if not refs else profile['mode']
                    if type(params['duration']) is not int or params['duration'] <= 0 or params.get('mode') != mode:
                        raise DomainError('rule_violation', 'Invalid video duration or mode', rule_id='duration.request')
                    # The released duration window is a provider limit, so it blocks (bug hunt 2026-09-24).
                    if profile.get('timing_capability_role'):
                        timing = self.workflow.config.section(profile['timing_capability_role'])
                        window = timing.get('duration_contract') or {}
                        low, high = window.get('min'), window.get('max')
                        if (isinstance(low, (int, float)) and isinstance(high, (int, float))
                                and not low <= params['duration'] <= high):
                            raise DomainError('rule_violation', f"Duration {params['duration']} s is outside the released "
                                              f'{low}–{high} s window', rule_id='duration.request', field='request.params.duration')
                    sent = params['prompt']
                    if not isinstance(sent, str) or len(sent.encode()) > PROMPT_MAX_BYTES:
                        raise DomainError('rule_violation', f'The prompt is over {PROMPT_MAX_BYTES} bytes (UTF-8), the fal adapter\'s bound',
                                          rule_id='prompt.size', field='request.params.prompt')
                    if hashlib.sha256(sent.encode()).hexdigest() != compiled['wire_provenance']['wire_sha256']:
                        raise DomainError('rule_violation', 'The prompt differs from the one prepared', rule_id='prompt.provenance')
                    if 'writer_text' in compiled:
                        # what goes out is exactly the writer's text plus allowed additions.
                        written, additions = compiled['writer_text'], compiled['additions']
                        if (hashlib.sha256(written.encode()).hexdigest() != compiled['writer_sha256']
                                or hashlib.sha256(json.dumps(additions, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                                != compiled['additions_sha256']):
                            raise DomainError('rule_violation', 'The writer text or its additions changed after preparation',
                                              rule_id='prompt.provenance')
                        # A video reference is numbered @VideoK apart from the images.
                        images = [{'n': r.get('n'), 'tag': r.get('tag'),
                                   **({'media': 'video'} if str(r.get('media_type', '')).startswith('video/') else {})} for r in refs]
                        problems = prompts.check(written, additions, sent, images, compiled['prompt_constants'])
                        if problems:
                            raise DomainError('rule_violation', 'The prompt is not the writer text plus allowed additions: '
                                              + '; '.join(problems), rule_id='prompt.provenance', field='request.params.prompt')
                    # Candidates prepared before 2026-09-27 keep their stored prompt: the sha check above is their proof.
                    # the compiler's craft and writer advice is shown, never a denial.
                    for i, message in enumerate((compiled.get('authorship') or {}).get('advice') or []):
                        add('writer.advice', 'warning', message, f'compilation.authorship.advice.{i}')
                policy = self.workflow.review_policy(pid, 'prepare', body['task'], body['method_id'], conn=db)
                result.update(required_roles=policy['required_roles'], review_policy=policy,
                              context_hash=body['context_hash'], release_id=body['release_id'], method_id=body['method_id'])
                add('authority.candidate', 'pass', 'Immutable candidate, current inputs and exact request verified', 'candidate')
                shotlist = shotlist_check(self.store, pid, body, db)
                if shotlist is not None:
                    add('authority.shotlist', shotlist['status'], shotlist['reason'], 'target.dependencies',
                        evidence=shotlist['evidence'])
        except DomainError as exc:
            add(str(exc.details.get('rule_id') or 'authority.candidate'), 'deny', exc.message,
                str(exc.details.get('field') or 'candidate'), evidence=[exc.as_dict()], hard=True)
        except (KeyError, TypeError, ValueError, OSError) as exc:
            add('authority.candidate', 'deny', 'Malformed or missing required service evidence', 'candidate',
                evidence=[{'type': type(exc).__name__}], hard=True)
        result['blocking'] = [check for check in checks if check['status'] == 'deny']
        result['mechanical_pass'] = not result['blocking']
        return result
