"""Service-owned candidate preparation; no generation or independent approval.

Creative content is read from immutable project revisions. Provider references
remain pinned media identities until the worker resolves their bytes. Compilation
is outside write transactions; authorization and exact dependency roots are checked
again when the immutable pending candidate is persisted.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from production import playbook
from production import prompt as prompts
from production.asset_methods import AssetMethods, ImageDefinition
from production.auth import AuthService, Principal
from production.context import ContextService, _view, context_body, related_history
from production.contracts import (
    Contract,
    DomainError,
    ObjectRef,
    PrepareRequest,
    content_hash,
)
from production.output_contract import validate_policy, validate_request
from production.projects import AssetDraft, AssetSelectionDraft
from production.store import Store
from production.workflow import Workflow


def _ref(obj: dict[str, Any]) -> dict[str, Any]:
    return {key: obj[key] for key in ('object_id', 'revision', 'digest')}


# Records a person or agent writes about earlier work; a new one arriving mid-compile means prepare again.
WRITTEN_NOTES = frozenset({'feedback', 'agent-report', 'pickup', 'observation', 'review-receipt', 'human-take-selection', 'repair'})


# An @tag: word characters joined by single hyphens (HF kebab names).
TAG_RE = r'@([A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)*)'

class VideoSettings(Contract):
    model: str
    aspect_ratio: str
    resolution: str
    # (owner 2026-09-26): the writer's whole prompt, sent verbatim plus only the additions
    # production/prompt.py allows; `look` names the world's look when the registry holds more than one.
    prompt: str | None = None
    look: str | None = None
    # Cards written before 2026-09-27 carry sections instead; they are read only to say what to write now.
    writer_sections: list[dict[str, str]] | None = None
    # which manuals the writer used and what this version changes.
    playbook_version: str | None = None
    change_note: str | None = None


class VideoRoute(Contract):
    job_type: str
    tasks: list[str]
    aspect_ratios: list[str]
    resolutions: list[str]
    max_references: int
    capability_role: str
    output_contract: dict[str, Any]
    timing_capability_role: str
    timing_modes: list[str]
    mode: str
    legacy_duration_route: dict[str, str]
    # a fal route whose takes are 480p drafts; the owner's pick is completed to 1080p.
    draft: bool = False


# a hand-placed previs or turnaround video an asset may carry (Higgsfield `--video-references`).
VIDEO_REFERENCE_TYPES = ('video/mp4', 'video/quicktime')
# fal job types and their queue endpoints (production/provider_fal.py); a fal request carries no `mode`.
FAL_VIDEO_ENDPOINTS = {'fal_seedance_2_5': 'bytedance/seedance-2.5/reference-to-video'}


def route_defaults(config: Any, method_id: str) -> dict[str, Any]:
    """The model and the default resolution of a video method's route: what a card may leave out. The default is the
    route's first listed resolution (owner 2026-09-29: the Higgsfield route releases 1080p first, then 720p).
    The quote prices with the same values the compiler sends."""
    try:
        routes = config.section('video_routes')
        profile = routes['profiles'][routes['method_routes'][method_id]]
        found: dict[str, Any] = {'model': profile['job_type']}
        if profile['resolutions']:
            found['resolution'] = profile['resolutions'][0]
        return found
    except (KeyError, TypeError, DomainError):
        return {}


class Compiler:
    def __init__(self, store: Store, auth: AuthService, workflow: Workflow, context: ContextService,
                 asset_methods: AssetMethods, *, route_profiles: Mapping[str, dict[str, Any]]) -> None:
        self.store, self.auth, self.workflow, self.context = store, auth, workflow, context
        self.asset_methods = asset_methods
        self.route_profiles = json.loads(json.dumps(dict(route_profiles)))

    def _video_route(self, applicable: dict[str, Any], settings: VideoSettings, task: str) -> tuple[str, VideoRoute, bool]:
        config = self.workflow.config
        try:
            routes = config.section('video_routes')
            profile_id = routes['method_routes'][applicable['method_id']]
            raw = routes['profiles'][profile_id]
            if self.route_profiles.get(profile_id) != raw:
                raise ValueError('Video route differs from the runtime config')
            route = VideoRoute.model_validate(raw)
            validate_policy(route.output_contract, modality="video", resolutions=route.resolutions)
            capability = config.section(route.capability_role)
            parameters = {p['name']: p for p in capability['params']}
            timing = config.section(route.timing_capability_role)
            audio = parameters.get('generate_audio', {})
            if audio.get('type') != 'boolean' or type(audio.get('default')) is not bool:
                raise ValueError('Pinned capability needs an explicit boolean audio default')
            if route.job_type in FAL_VIDEO_ENDPOINTS:
                if (capability['job_type'] != route.job_type or capability['type'] != 'video'
                        or capability.get('endpoint') != FAL_VIDEO_ENDPOINTS[route.job_type]
                        or settings.model != route.job_type or task not in route.tasks
                        or not {'prompt', 'duration', 'resolution', 'aspect_ratio'} <= parameters.keys()
                        or settings.aspect_ratio not in route.aspect_ratios or settings.resolution not in route.resolutions
                        or not set(route.resolutions) <= set(parameters['resolution']['enum'])
                        or not set(route.aspect_ratios) <= set(parameters['aspect_ratio']['enum'])
                        or parameters['duration'].get('type') != 'integer' or route.mode != 'reference'
                        or route.draft and (parameters.get('draft', {}).get('type') != 'boolean' or route.resolutions != ['480p'])
                        or not 0 < route.max_references <= min(30, int(capability.get('max_references', 0)))
                        or timing['job_type'] != route.job_type or task not in timing['tasks']
                        or not set(route.timing_modes) <= set(timing['timing_modes'])
                        or route.legacy_duration_route != {'backend': 'fal', 'model': route.job_type,
                                                           'endpoint': FAL_VIDEO_ENDPOINTS[route.job_type]}):
                    raise ValueError('fal video route capabilities do not cover this task')
                return profile_id, route, audio['default']
            if (capability['job_type'] != route.job_type or capability['type'] != 'video'
                    or settings.model != route.job_type or task not in route.tasks
                    or not {'prompt', 'duration', 'mode', 'resolution', 'aspect_ratio', 'image_references'} <= parameters.keys()
                    or settings.aspect_ratio not in route.aspect_ratios or settings.resolution not in route.resolutions
                    or not set(route.resolutions) <= set(parameters['resolution']['enum'])
                    or not set(route.aspect_ratios) <= set(parameters['aspect_ratio']['enum'])
                    or route.mode not in parameters['mode']['enum'] or route.mode != 'omni_reference'
                    or parameters['duration'].get('type') != 'integer'
                    or parameters['image_references'].get('type') != 'array'
                    or not 0 < route.max_references <= 30  # Higgsfield takes 30 images (model get seedance_2_5 rules)
                    or timing['job_type'] != route.job_type or task not in timing['tasks']
                    or not set(route.timing_modes) <= set(timing['timing_modes'])
                    or set(route.legacy_duration_route) != {'backend', 'model', 'endpoint'}
                    or route.legacy_duration_route['backend'] != 'higgsfield'
                    or route.legacy_duration_route['model'] != route.job_type):
                raise ValueError('Video route capabilities do not cover this task')
            return profile_id, route, audio['default']
        except (KeyError, TypeError, ValueError, DomainError) as exc:
            released = locals().get('route')
            if isinstance(released, VideoRoute) and (settings.model != released.job_type or task not in released.tasks
                    or settings.aspect_ratio not in released.aspect_ratios or settings.resolution not in released.resolutions):
                # Say what the card may ask for (simulated run: a 1080p card got only 'absent or incompatible').
                raise DomainError('unsupported_route',
                    f"The released video route takes model {released.job_type}, tasks {', '.join(released.tasks)}, "
                    f"aspect {', '.join(released.aspect_ratios)}, resolution {', '.join(released.resolutions)}; this card asks for "
                    f"{settings.model} {task} {settings.aspect_ratio} {settings.resolution}",
                    repair='Set _production model, aspect_ratio and resolution to released values') from exc
            raise DomainError('unsupported_route', 'Video route, parameter or timing capability is absent or incompatible') from exc

    def _manuals_taken(self, actor: Principal, project_id: str, shot: dict[str, Any], task: str, settings: Any,
                       db: sqlite3.Connection, *, needs_note: bool = True) -> list[str]:
        """Whether the manuals reached this writer and this version says what it changes, as advice (Q6: HF neither blocks nor reminds, so the desk shows "手册不是最新" instead of refusing)."""
        current, missing = playbook.version(), []
        if not playbook.fetched(self.store, project_id, actor.credential_id, current, conn=db):
            missing.append(f'fetch the manuals first (cli playbook {project_id} --dir <folder>); this credential has not taken {current}')
        if settings.playbook_version != current:
            missing.append(f'set _production.playbook_version to {current}, the manuals you wrote with')
        # a note is asked for only when the card text changed from the version before; an
        # unchanged card (a reshoot) needs none, and a note may repeat (HF re-fires identical prompts).
        if needs_note and not self._change_note(project_id, shot, settings, db) and self._card_changed(project_id, shot, db):
            missing.append('set _production.change_note: what this version of the card changes')
        return ['The manuals should reach the writer: ' + m for m in missing]

    def _card_changed(self, project_id: str, shot: dict[str, Any], db: sqlite3.Connection) -> bool:
        """Whether this card version's text differs from the previous version (the first version counts as changed).
        The manuals version and the note itself are not the card's text."""
        if shot['revision'] <= 1:
            return True
        def text(obj: dict[str, Any]) -> Any:
            content = copy.deepcopy(obj['body'].get('content'))
            if isinstance(content, dict) and isinstance(content.get('_production'), dict):
                for key in ('change_note', 'playbook_version'):
                    content['_production'].pop(key, None)
            return content
        before = self.store.get_object(project_id, shot['object_id'], revision=shot['revision'] - 1, conn=db)
        return text(before) != text(shot)

    def _defaults(self, actor: Principal, project_id: str, applicable: dict[str, Any], db: sqlite3.Connection) -> dict[str, Any]:
        """What a card may leave out: the method's route model and its default resolution
        (the fal 480p draft route), 16:9 (owner 2026-09-01), and the manuals version this credential took."""
        defaults: dict[str, Any] = {'aspect_ratio': '16:9', **route_defaults(self.workflow.config, applicable['method_id'])}
        if playbook.fetched(self.store, project_id, actor.credential_id, playbook.version(), conn=db):
            defaults['playbook_version'] = playbook.version()
        return defaults

    def _change_note(self, project_id: str, shot: dict[str, Any], settings: VideoSettings, db: sqlite3.Connection) -> str:
        """What this card version changes: the reason of the patch that made it (the guide's way to revise one section),
        else `_production.change_note`."""
        for repair in self.store.list_objects(project_id, kind='repair', conn=db):
            result = repair['body'].get('result') or {}
            if (repair['author'] == 'patch_service' and result.get('object_id') == shot['object_id']
                    and result.get('revision') == shot['revision'] and str(repair['body'].get('observed_defect', '')).strip()):
                return str(repair['body']['observed_defect']).strip()
        return (settings.change_note or '').strip()

    def _video(self, actor: Principal, project_id: str, request: PrepareRequest) -> dict[str, Any]:
        with self.store.transaction(write=False) as db:
            applicable = self.workflow.applicable_method(project_id, request.method_selection, request.task, conn=db)
            obj = self.store.get_object(project_id, request.target.object_id, revision=request.target.revision, conn=db)
            if obj['kind'] != 'shot' or applicable['target']['object_id'] != obj['object_id'] or applicable['target']['revision'] != obj['revision']:
                raise DomainError('unsupported_method', 'Video and stress probes require an authored shot and its own selected method')
            try:
                card = copy.deepcopy(obj['body']['content'])
                written_settings = card.pop('_production', None) or {}
                settings = VideoSettings.model_validate({**self._defaults(actor, project_id, applicable, db), **written_settings})
            except (KeyError, AttributeError, TypeError, ValidationError) as exc:
                raise DomainError('missing_prerequisite', 'Shot _production is malformed (model, aspect_ratio, resolution, prompt)') from exc
            written = (settings.prompt or '').strip()
            if not written:
                raise DomainError('missing_prerequisite', 'Write the whole prompt in _production.prompt'
                                  + (' (this card has writer_sections, the form used before 2026-09-27)' if settings.writer_sections else ''),
                                  repair='Put the full prompt text the writer wrote in _production.prompt')
            advice = self._manuals_taken(actor, project_id, obj, request.task, settings, db)
            profile_id, route, generate_audio = self._video_route(applicable, settings, request.task)
            graph = self.workflow.pinned_graph(project_id, request.target, conn=db)
            roots = [request.target, request.method_selection, *request.inputs]
            evidence = self._current(project_id, roots, db)
            objects = [node['object'] for node in graph['nodes']]
            scenes = [o for o in objects if o['kind'] == 'scene']
            if len(scenes) != 1 or not isinstance(scenes[0]['body']['content'], str):
                # the scene is the writer's context, not something the platform sends.
                advice.append(f'The card depends on {len(scenes)} scenes; one authored scene keeps the desk and the shot list in order')
            scope = {project_id, *(o['object_id'] for o in objects)}
            selections = {o['object_id']: o for o in objects if o['kind'] == 'asset'
                          and isinstance(o['body'].get('content'), dict) and o['body']['content'].get('type') == 'asset-selection'}
            for ref in request.inputs:
                selected_input = self.store.get_object(project_id, ref.object_id, revision=ref.revision, conn=db)
                if selected_input['kind'] != 'asset' or selected_input['body']['content'].get('type') != 'asset-selection':
                    raise DomainError('invalid_input', 'Video inputs must be asset selections, never original video or raw references')
                selections[selected_input['object_id']] = selected_input
            # A selection must be included only when it targets the card or its scene: the scope Shoot._inputs collects
            # (re-audit 2026-09-27, a project-level selection stopped every order and no order could add it).
            required = {request.target.object_id, *(o['object_id'] for o in scenes)}
            for current in self.store.list_objects(project_id, kind='asset', conn=db):
                content = current['body'].get('content', {})
                target_ref = content.get('target', {}) if isinstance(content, dict) else {}
                # only a selection made for the current version of the card or its scene
                # applies; one made for an older scene or card version is history, not a missing input. So is one
                # naming an older version of an asset (a new picture or descriptor; bug hunt 2026-09-27).
                picked = [r for r in (content.get('selected') or {}).values() if isinstance(r, dict)] if isinstance(content, dict) else []
                if (isinstance(content, dict) and content.get('type') == 'asset-selection'
                        and target_ref.get('object_id') in required and current['object_id'] not in selections
                        and target_ref.get('revision') == self.store.get_object(project_id, target_ref['object_id'], conn=db)['revision']
                        and all(r.get('revision') == self.store.get_object(project_id, r['object_id'], conn=db)['revision']
                                for r in picked)):
                    raise DomainError('missing_prerequisite', 'Include the exact applicable asset selections in shot dependencies or inputs')
            assets: dict[tuple[str, str], tuple[AssetDraft, dict[str, Any]]] = {}
            for selection in selections.values():
                try:
                    choice = AssetSelectionDraft.model_validate(selection['body']['content'])
                    if choice.target.object_id not in scope:
                        raise ValueError('Unrelated asset selection')
                    selected_target = self.store.get_object(project_id, choice.target.object_id, conn=db)
                    if selected_target['revision'] != choice.target.revision or choice.target.digest and choice.target.digest != selected_target['digest']:
                        raise DomainError('stale_input', 'Selection target changed')
                    dependency_ids = {(r['object_id'], r['revision']) for r in selection['body'].get('dependencies', [])}
                    for role, ref in choice.selected.items():
                        selected_obj = self.store.get_object(project_id, ref.object_id, revision=ref.revision, conn=db)
                        asset = AssetDraft.model_validate(selected_obj['body']['content'])
                        key = (role, asset.tag.lstrip('@'))
                        if (asset.role != role or key in assets or (ref.object_id, ref.revision) not in dependency_ids
                                or ref.digest and ref.digest != selected_obj['digest']):
                            raise ValueError('Ambiguous or unbound selected asset')
                        assets[key] = (asset, selected_obj)
                except (KeyError, TypeError, ValueError) as exc:
                    raise DomainError('invalid_input', 'Asset selections have missing, conflicting or unrelated bindings') from exc
            material, direction = (card.get(k, {}) for k in ('The material', 'Direction'))
            try:
                raw_seconds = material['the running time in seconds']
                seconds = float(raw_seconds)
                if isinstance(raw_seconds, bool) or not math.isfinite(seconds) or seconds <= 0 or not seconds.is_integer():
                    raise ValueError('Duration must be a positive integer')
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise DomainError('invalid_input', 'Video duration must match the provider integer contract') from exc
            parameters = {'resolution': settings.resolution, 'aspect_ratio': settings.aspect_ratio,
                          'duration': int(seconds), 'mode': route.mode, 'generate_audio': generate_audio}
            if route.job_type in FAL_VIDEO_ENDPOINTS:
                # fal has no `mode`; a draft route asks for the 480p draft that carries a draft id.
                del parameters['mode']
                if route.draft:
                    parameters['draft'] = True
            validate_request(parameters, route.output_contract)
            # Named looks, one per world (frozen decisions): the look asset's tag names it.
            looks = {tag: (asset, asset_obj) for (role, tag), (asset, asset_obj) in assets.items() if role == 'look'}
            look_texts: dict[str, str] = {}
            for tag, (asset, _) in looks.items():
                definition = asset.definition
                text = definition if isinstance(definition, str) else definition.get('description')
                if not isinstance(text, str) or not text.strip():
                    advice.append(f'Selected look @{tag} has no description, so nothing of it is pasted')
                    continue
                look_texts[tag] = text
            look_name = (settings.look or '').lstrip('@') or None
            if look_name is not None and look_name not in look_texts:
                advice.append(f'_production.look names @{look_name}; the selected looks are '
                              + (', '.join('@' + t for t in look_texts) or 'none') + ', so no look is pasted')
                look_name = None
                if len(look_texts) == 1:
                    look_texts = {}  # the only look would be pasted by default; the card asked for another
            named = look_name or (next(iter(look_texts)) if len(look_texts) == 1 else None)
            timing = direction.get('timing mode') or 'timed'
            mode = applicable['definition'].get('timing_selections', {}).get(timing, {})
            combo = {'reference': 'references-only', 'timing': mode.get('module'), 'acting': 'local-d3'}
            allowed = applicable['definition'].get('allowed_combinations', [])
            named_definition = looks[named][0].definition if named is not None else None
            if isinstance(named_definition, dict):
                combo['visual'] = named_definition.get('visual_treatment')
                fits = combo in allowed
            else:
                # HF has no style element in any official job; the style, if any, is a text line (HF_CANONICAL.md §6).
                fits = any({k: v for k, v in c.items() if k != 'visual'} == combo for c in allowed)
            if timing not in route.timing_modes:
                # Re-audit 2026-09-27: the timing mode is never sent to the provider, so it is advice.
                advice.append(f'Timing mode {timing} is not one of the route\'s ({", ".join(route.timing_modes)}); nothing sent depends on it')
            if not fits:
                # the look/visual-treatment pairing is craft, so advice.
                advice.append('The look and timing are not one of the method\'s listed combinations: ' + json.dumps(combo, ensure_ascii=False))
            # The tags the writer used pick the images (HF: one image per element, a state is its own element).
            kinds = {'world': 'place', 'state': 'state', 'look': 'style', 'delivery': 'prop'}
            found: dict[str, dict[str, Any]] = {}
            images: dict[str, dict[str, Any]] = {}
            shared: list[str] = []
            for tag in prompts.tags(written):
                owners = [(role, assets[(role, tag)]) for role in ('visual', 'world', 'delivery', 'state', 'look')
                          if (role, tag) in assets]
                if len(owners) > 1:
                    # Before 2026-09-27 a state could share its base's tag (the assembler then sent the base only).
                    shared.append(f"@{tag} names selected {', '.join(role for role, _ in owners)} assets; the {owners[0][0]} "
                                  f"one is sent. Give the others their own tag (a state is its own element, e.g. @{tag}_wet)")
                if not owners:
                    continue
                owner, (asset, asset_obj) = owners[0]
                definition = asset.definition
                descriptor = None if owner == 'look' else (definition if isinstance(definition, str) else definition.get('descriptor'))
                entry: dict[str, Any] = {'descriptor': descriptor if isinstance(descriptor, str) and descriptor.strip() else None,
                                         'image': None}
                if asset.media_refs:
                    if len(asset.media_refs) != 1:
                        raise DomainError('missing_prerequisite', f'Select one exact reference image for @{tag}')
                    ref = asset.media_refs[0]
                    media = self.store.get_object(project_id, ref.object_id, revision=ref.revision, conn=db)
                    media_type = media['body'].get('media_type') if media['kind'] == 'media' else None
                    moving = media_type in VIDEO_REFERENCE_TYPES
                    if moving and route.job_type in FAL_VIDEO_ENDPOINTS:
                        # video references go to Higgsfield; the fal draft route sends images only.
                        raise DomainError('unsupported_route', f'@{tag} is a video; video references need the Higgsfield route '
                                          '(turn 样片模式 off for this film)')
                    if media_type not in ('image/png', 'image/jpeg', 'image/webp') and not moving:
                        raise DomainError('invalid_media', 'Video reference slots accept only verified images or videos')
                    if ref.digest and ref.digest != media['digest']:
                        raise DomainError('stale_input', 'Selected image fingerprint mismatch')
                    if any(i['object_ref']['object_id'] == ref.object_id for i in images.values()):
                        advice.append(f'@{tag} uses the same image as another tag; fal gets it twice')
                    data = self.asset_methods.media.read(project_id, ref.object_id, revision=ref.revision)
                    kind = kinds.get(owner) or ('prop' if asset.category == 'prop' else 'place' if asset.category == 'environment' else 'person')
                    images[tag] = {'asset': _ref(asset_obj), 'object_ref': _ref(media), 'sha256': media['body']['sha256'],
                                   'role': kind, 'media_type': media['body']['media_type'], 'byte_length': len(data)}
                    entry['video' if moving else 'image'] = tag
                found[tag] = entry
            project = self.store.get_object(project_id, project_id, conn=db)
            binding = project['body'].get('binding') if project['body'].get('binding') in ('every', 'first') else self.workflow.config.binding
            root_refs = [_ref(self.store.get_object(project_id, ref.object_id, revision=ref.revision, conn=db)) for ref in roots]
            note = self._change_note(project_id, obj, settings, db)
        W = settings.prompt or ''
        chosen = {named: look_texts[named]} if named is not None else {}
        # Higgsfield prompts number references HF's way, `<<<image_N>>>` / `<<<video_N>>>`; fal's are `@ImageN`.
        style = 'at' if route.job_type in FAL_VIDEO_ENDPOINTS else 'hf'
        sent, numbered, additions = prompts.build(W, found, look_texts, named, seconds, binding, style)
        constants = prompts.constants(found, chosen, seconds, style)
        problems = prompts.check(W, additions, sent, numbered, constants)
        if problems:
            raise DomainError('rule_violation', 'The prompt is not the writer text plus allowed additions: ' + '; '.join(problems))
        if len(numbered) > route.max_references:
            raise DomainError('invalid_input', f'{len(numbered)} reference images; the route takes at most {route.max_references}')
        if route.job_type not in FAL_VIDEO_ENDPOINTS and not numbered:
            # Higgsfield's omni_reference needs at least one reference; a shot with none is text-to-video.
            parameters['mode'] = 't2v'
        references = [{**images[i['tag']], 'n': i['n'], 'tag': i['tag']} for i in numbered]
        additions_sha = hashlib.sha256(json.dumps(additions, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        authorship = {'playbook_version': settings.playbook_version, 'change_note': note,
                      'advice': [*advice, *shared, *prompts.advice(W, found, seconds)]}
        return {'task': request.task, 'target': _ref(obj), 'method_selection': applicable['selection'],
                'method_id': applicable['method_id'], 'release_id': applicable['release_id'], 'profile_id': profile_id,
                'profile_hash': content_hash(self.route_profiles[profile_id]), 'job_type': route.job_type,
                'prompt': sent, 'parameters': parameters,
                'references': references, 'dependency_roots': root_refs, 'input_manifest': evidence,
                'writer_text': W, 'additions': additions, 'prompt_constants': constants, 'binding': binding,
                'writer_sha256': hashlib.sha256(W.encode()).hexdigest(), 'additions_sha256': additions_sha,
                'authorship': authorship, 'selected_assets': [_ref(value[1]) for value in assets.values()],
                'wire_provenance': {'source_sha256': hashlib.sha256(W.encode()).hexdigest(),
                    'transform': 'writer text plus the allowed additions (production/prompt.py)',
                    'wire_sha256': hashlib.sha256(sent.encode()).hexdigest()}, 'accepted': False}

    def _scope(self, actor: Principal, project_id: str) -> str:
        return f'{actor.actor_id}:{actor.credential_id}:{project_id}:prepare'

    def _previous(self, actor: Principal, project_id: str, request: PrepareRequest,
                  conn: sqlite3.Connection) -> dict[str, Any] | None:
        row = conn.execute('SELECT request_hash,result FROM idempotency WHERE scope=? AND key=?',
                           (self._scope(actor, project_id), request.idempotency_key)).fetchone()
        if row is None:
            return None
        if row['request_hash'] != content_hash(request.model_dump()):
            raise DomainError('idempotency_conflict', 'Prepare key already has different input')
        return self.store.decode_replay(row['result'])

    def _current(self, project_id: str, refs: list[ObjectRef], conn: sqlite3.Connection) -> list[dict[str, Any]]:
        evidence = {}
        for ref in refs:
            graph = self.workflow.pinned_graph(project_id, ref, conn=conn)
            if graph['stale']:
                raise DomainError('stale_input', 'Preparation input or its dependencies changed')
            for node in graph['nodes']:
                key = (node['ref']['object_id'], node['ref']['revision'], node['frozen_selection'])
                evidence[key] = {'object_ref': node['ref'], 'frozen_selection': node['frozen_selection']}
        return list(evidence.values())

    def _context(self, actor: Principal, project_id: str, request: PrepareRequest,
                 roots: list[ObjectRef], conn: sqlite3.Connection) -> dict[str, Any]:
        context = self.context.get(actor, project_id, target=request.target, task=request.task,
                                   method=request.method_selection, conn=conn)
        evidence = self._current(project_id, roots, conn)
        context['preparation_inputs'] = []
        for item in evidence:
            ref = item['object_ref']
            obj = self.store.get_object(project_id, ref['object_id'], revision=ref['revision'], conn=conn)
            context['preparation_inputs'].append({**item, 'kind': obj['kind'], 'body': context_body(obj)})
        # Qualification collections remain full preparation/authority evidence.
        # Their automatically added sample ancestry is not a new creative-history
        # seed: every later shot must not re-review all qualification executions.
        # Explicit creative inputs (including an explicitly selected sample) still
        # carry their own full history and generated-reference provenance.
        creative_roots = [request.target, request.method_selection, *request.inputs,
                          *([request.mask] if request.mask else [])]
        context['history_roots'] = [item['object_ref'] for item in self._current(project_id, creative_roots, conn)]
        relevant = {ref['object_id'] for ref in context['history_roots']}
        related = [_view(obj) for obj in related_history(self.store.list_objects(project_id, conn=conn), relevant)]
        context['prior_results'] = related
        context.pop('context_hash')
        context['context_hash'] = content_hash(context)
        return self.context._bounded(context)

    def prepare(self, actor: Principal, project_id: str, request: PrepareRequest) -> dict[str, Any]:
        with self.store.transaction(write=False) as db:
            self.auth.authorize(actor, project_id, 'prepare', conn=db)
            previous = self._previous(actor, project_id, request, db)
            if previous is not None:
                return previous
            target = self.store.get_object(project_id, request.target.object_id, revision=request.target.revision, conn=db)
            if request.expected_revision != target['revision']:
                raise DomainError('revision_conflict', 'Prepare guard differs from target revision', current_revision=target['revision'])
            roots = [request.target, request.method_selection, *request.inputs, *([request.mask] if request.mask else [])]
            self._current(project_id, roots, db)
            context = self._context(actor, project_id, request, roots, db)
            if 'prepare' not in context['progression']['allowed']:
                raise DomainError('missing_prerequisite', 'Preparation prerequisites are incomplete',
                                  repair=', '.join(context['progression']['missing']))
        if request.task in ('image', 'image-edit'):
            # the manuals (LIRA) reach the image writer too; the definition carries the
            # version it wrote with and what this version changes, like a shot card's _production.
            with self.store.transaction(write=False) as db:
                asset = self.store.get_object(project_id, request.target.object_id, revision=request.target.revision, conn=db)
                try:
                    definition = ImageDefinition.model_validate(AssetDraft.model_validate(asset['body']['content']).definition)
                except (ValidationError, KeyError, TypeError) as exc:
                    raise DomainError('invalid_input', 'Asset definition does not match the image authoring schema') from exc
                advice = self._manuals_taken(actor, project_id, asset, request.task, definition, db, needs_note=False)
            compiled = self.asset_methods.compile(project_id, request.target, request.method_selection,
                                                  request.task, inputs=request.inputs, mask=request.mask)
            compiled['authorship'] = {'playbook_version': definition.playbook_version, 'change_note': (definition.change_note or '').strip(),
                                      'advice': [*advice, *compiled.pop('advice', [])]}
        elif request.task in ('shot', 'stress'):
            compiled = self._video(actor, project_id, request)
        else:
            raise DomainError('unsupported_method', 'Cuts use the cut assembly operation')
        body = {'target': compiled['target'], 'task': request.task, 'release_id': compiled['release_id'],
                'method_id': compiled['method_id'], 'method_selection': compiled['method_selection'],
                'context_hash': context['context_hash'], 'context': context,
                'request': {'job_type': compiled['job_type'],
                            'params': {'prompt': compiled['prompt'], **compiled['parameters']},
                            'references': compiled['references']},
                'compilation': compiled, 'dependencies': compiled['dependency_roots'],
                'input_manifest': compiled['input_manifest'], 'gate_status': 'pending', 'accepted': False}
        with self.store.transaction() as db:
            self.auth.authorize(actor, project_id, 'prepare', conn=db)
            previous = self._previous(actor, project_id, request, db)
            if previous is not None:
                return previous
            self.workflow.config.require_method(compiled['method_id'], task=request.task)
            self._current(project_id, [ObjectRef.model_validate(ref) for ref in body['dependencies']], db)
            fresh = self._context(actor, project_id, request, roots, db)
            def notes(ctx: dict[str, Any]) -> set[tuple[str, int]]:
                return {(r['object_id'], r['revision']) for r in ctx.get('prior_results', []) if r.get('kind') in WRITTEN_NOTES}
            # Something written about this card's own inputs while it compiled (feedback, a pick, a report) was not
            # seen by the writer: prepare again. Machine progress (jobs, takes, other cards) is not such a note.
            if 'prepare' not in fresh['progression']['allowed'] or notes(fresh) - notes(context):
                raise DomainError('stale_input', 'Production context changed during compilation')
            # The card and every input it compiled from were just re-checked as current (`_current` above). Other work
            # moving on meanwhile (another card of the same shoot order, jobs, neighbours) is not a reason to refuse:
            # record the context as it stands now (dry run: the second card of a two-card order was refused).
            body['context'], body['context_hash'] = fresh, fresh['context_hash']
            return self.store.run_idempotent(self._scope(actor, project_id), request.idempotency_key, request.model_dump(),
                lambda conn: self.store.create_object(project_id, 'candidate', body, 'compiler_service', conn=conn), conn=db)
