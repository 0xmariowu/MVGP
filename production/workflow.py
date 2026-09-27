"""Dependency readiness and service-owned picture locks, not artistic acceptance.

The runtime config's enabled methods own execution applicability. Structural readiness
only permits preparation; dispatch gates remain mandatory.
Recreation adds source understanding to the relevant closure, not a separate
prompt compiler. A selected asset revision stays pinned while alternatives evolve.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from production.auth import AuthService, Principal
from production.contracts import DomainError, ObjectRef
from production.runtime_config import RuntimeConfig
from production.store import Store

SERVICE_AUTHOR = "workflow_service"
MAX_GRAPH_NODES = 2000
MAX_GRAPH_DEPTH = 128
STATE_CACHE_ENTRIES = 1024
STATE_CACHE_BYTES = 4 * 1024 * 1024


def _ref(obj: dict[str, Any]) -> dict[str, Any]:
    return {"object_id": obj["object_id"], "revision": obj["revision"], "digest": obj["digest"]}


class Workflow:
    def __init__(self, store: Store, auth: AuthService, config: RuntimeConfig) -> None:
        self.store, self.auth, self.config = store, auth, config

    def _qualification_assets(self, pid: str, obj: dict[str, Any], db: sqlite3.Connection) -> set[tuple[str, int, str]]:
        """Only service-bound asset edges inherit fixed-version selection semantics."""
        if obj['author'] != 'qualification_service' or obj['kind'] not in ('qualification-set', 'qualification-sample'):
            return set()
        body = obj['body']
        try:
            if body['method_id'] != 'mvgp-video-v1':
                raise ValueError('Unsupported qualification scope')
            refs = ([body['asset']] if obj['kind'] == 'qualification-set'
                    else [entry['asset'] for entry in body['asset_references']])
            if not refs or body['asset'] not in refs:
                raise ValueError('Qualification subject is absent')
            selected = set()
            for value in refs:
                ref = ObjectRef.model_validate(value)
                if ref.digest is None or value != ref.model_dump() or value not in body['dependencies']:
                    raise ValueError('Qualification edge must match a complete pinned reference')
                asset = self.store.get_object(pid, ref.object_id, revision=ref.revision, conn=db)
                content = asset['body'].get('content', {})
                if (asset['digest'] != ref.digest or asset['kind'] != 'asset' or not isinstance(content, dict) or content.get('type') != 'asset'
                        or content.get('role') not in ('visual', 'state', 'world')):
                    raise ValueError('Qualification may freeze only exact visual/world/state assets')
                selected.add((ref.object_id, ref.revision, ref.digest))
            return selected
        except (KeyError, TypeError, ValueError):
            raise DomainError('invalid_input', 'Service qualification asset edges are malformed') from None

    def pinned_graph(self, project_id: str, target: ObjectRef, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Service-only exact closure; callers authenticate before reading its contents.

        Dependencies are body.dependencies ObjectRefs. Selection-owned descendants
        are frozen alternatives; changing the selection itself still invalidates
        consumers. Completed worker media keeps historical producer dependencies:
        this verifies provenance, not suitability for current creative intent.
        Missing/corrupt refs, explicit invalidations and overflow still fail.
        """
        with self.store._using(conn, write=False) as db:
            return self._pinned_graph(project_id, target, db, lambda ref: self.store.get_object(
                project_id, ref.object_id, revision=ref.revision, conn=db))

    def pinned_states(self, project_id: str, targets: list[ObjectRef], *,
                      conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        """State-only batch; reuse metadata, never graph verdicts or authority.

        Minimal objects stay private to this synchronous snapshot. Full artifact
        readers keep their existing independent mutable bodies and exact checks.
        """
        with self.store._using(conn, write=False) as db:
            cache: dict[tuple[str, int], tuple[dict[str, Any], int]] = {}
            used = 0
            def load(ref: ObjectRef) -> dict[str, Any]:
                nonlocal used
                key = (ref.object_id, ref.revision)
                if key in cache:
                    return cache[key][0]
                obj = self.store.get_object(project_id, ref.object_id, revision=ref.revision, conn=db)
                body = obj['body']
                content = body.get('content', {})
                projected = {name: body[name] for name in ('dependencies', 'method_id', 'asset', 'asset_references') if name in body}
                projected['content'] = ({name: content[name] for name in ('type', 'selected', 'role') if name in content}
                                        if isinstance(content, dict) else content)
                item = {**{name: obj[name] for name in ('object_id', 'revision', 'digest', 'kind', 'author')}, 'body': projected}
                size = len(json.dumps(item, ensure_ascii=False).encode('utf-8'))
                if size <= STATE_CACHE_BYTES:
                    while cache and (len(cache) >= STATE_CACHE_ENTRIES or used + size > STATE_CACHE_BYTES):
                        _, old_size = cache.pop(next(iter(cache)))
                        used -= old_size
                    cache[key] = (item, size)
                    used += size
                return item
            result = []
            for target in targets:
                try:
                    graph = self._pinned_graph(project_id, target, db, load)
                    result.append({'stale': graph['stale'], 'reasons': graph['reasons']})
                except DomainError as exc:
                    result.append({'error': exc.code})
            return result

    def _pinned_graph(self, project_id: str, target: ObjectRef, db: sqlite3.Connection,
                      load: Callable[[ObjectRef], dict[str, Any]]) -> dict[str, Any]:
        nodes: dict[tuple[str, int, bool], dict[str, Any]] = {}
        active: set[tuple[str, int, bool]] = set()
        invalid = {(entry["body"]["target"]["object_id"], entry["body"]["target"]["revision"])
                   for entry in self.store.list_objects(project_id, kind="invalidation", conn=db)
                   if entry["author"] == SERVICE_AUTHOR}
        reasons: list[dict[str, Any]] = []
        def visit(ref: ObjectRef, frozen: bool = False) -> bool:
            key = (ref.object_id, ref.revision, frozen)
            if key in active:
                raise DomainError("invalid_input", "Dependency cycle detected")
            if key in nodes:
                return bool(nodes[key]["stale"])
            if len(active) >= MAX_GRAPH_DEPTH:
                raise DomainError("insufficient_context", "Dependency graph exceeds bounded traversal depth")
            if len(nodes) + len(active) >= MAX_GRAPH_NODES:
                raise DomainError("insufficient_context", "Dependency graph exceeds bounded context capacity")
            obj = load(ref)
            if ref.digest is not None and obj["digest"] != ref.digest:
                raise DomainError("stale_input", "Dependency digest is inconsistent", object_ref=ref.object_id)
            # The exact body above is the graph evidence. Only the current
            # revision number is needed to detect changes in this snapshot.
            current = db.execute("SELECT current_revision FROM objects WHERE project_id=? AND object_id=?",
                                 (project_id, ref.object_id)).fetchone()
            if current is None:
                raise DomainError("not_found", "Object revision not found")
            stale = not frozen and current["current_revision"] != ref.revision
            if stale:
                reasons.append({"code": "revision_changed", "ref": _ref(obj), "current_revision": current["current_revision"]})
            if (ref.object_id, ref.revision) in invalid:
                stale = True
                reasons.append({"code": "service_invalidated", "ref": _ref(obj)})
            body = obj["body"]
            content = body.get("content", {})
            selected_refs = set()
            if obj["kind"] == "asset" and isinstance(content, dict) and content.get("type") == "asset-selection":
                selected_refs = {(value["object_id"], value["revision"]) for value in content.get("selected", {}).values()}
            qualification_refs = self._qualification_assets(project_id, obj, db)
            recorded_media = obj['kind'] == 'media' and obj['author'] == 'worker_service'
            # a shot card carries its whole prompt; its scene is the writer's context, so a
            # scene edit never stales a written card (HF: the shotlist is the truth, hell-grind:103, cully:133).
            scene_ids = ({row[0] for row in db.execute("SELECT object_id FROM objects WHERE project_id=? AND kind='scene'",
                                                        (project_id,)).fetchall()} if obj['kind'] == 'shot' else set())
            active.add(key)
            try:
                for dependency in body.get("dependencies", []):
                    selected = ((dependency["object_id"], dependency["revision"]) in selected_refs
                                or (dependency["object_id"], dependency["revision"], dependency.get("digest")) in qualification_refs
                                or dependency["object_id"] in scene_ids)
                    stale = visit(ObjectRef.model_validate(dependency), frozen or selected or recorded_media) or stale
            except ValidationError as exc:
                raise DomainError("invalid_input", "Stored dependency reference is malformed") from exc
            finally:
                active.remove(key)
            nodes[key] = {"object": obj, "ref": _ref(obj), "frozen_selection": frozen, "stale": stale}
            return stale
        stale = visit(target)
        return {"target": target.model_dump(), "nodes": list(nodes.values()), "stale": stale, "reasons": reasons}
    def media_origin(self, project_id: str, target: ObjectRef, *, conn: sqlite3.Connection | None = None,
                     allow_derivatives: bool = False) -> dict[str, Any]:
        """Resolve direct producer identity, never freshness or permission to use it.

        Service-only caller authenticates and retains full pinned_graph checks.
        Actor-authored source_cut uploads declare a cut origin, not generated-take
        proof. Historical worker media may point directly to one compiler candidate.
        Explicit bindings cannot fall back to a plausible arbitrary ancestor.
        """
        with self.store._using(conn, write=False) as db:
            def resolve(value: Any) -> dict[str, Any]:
                try:
                    reference = value if isinstance(value, ObjectRef) else ObjectRef.model_validate(value)
                    if reference.digest is None:
                        raise ValueError('Missing exact digest')
                except (TypeError, ValueError):
                    raise DomainError('invalid_media', 'Producer requires complete immutable references') from None
                obj = self.store.get_object(project_id, reference.object_id, revision=reference.revision, conn=db)
                if obj['digest'] != reference.digest:
                    raise DomainError('stale_input', 'Producer reference digest differs')
                return obj

            def direct(obj: dict[str, Any]) -> list[dict[str, Any]]:
                values = obj['body'].get('dependencies', [])
                if not isinstance(values, list) or len(values) > MAX_GRAPH_NODES:
                    raise DomainError('invalid_media', 'Producer dependencies are malformed or excessive')
                return [resolve(value) for value in values]

            def candidate(obj: dict[str, Any]) -> None:
                if obj['kind'] != 'candidate' or obj['author'] != 'compiler_service' or obj['revision'] != 1:
                    raise DomainError('invalid_media', 'Producer is not an immutable compiler candidate')

            original = current = resolve(target)
            chain = []
            seen: set[tuple[str, int]] = set()
            while True:
                key = (current['object_id'], current['revision'])
                if key in seen:
                    raise DomainError('invalid_media', 'Media producer cycle detected')
                if len(seen) >= 16:
                    raise DomainError('insufficient_context', 'Media derivative chain exceeds sixteen media records')
                if current['kind'] != 'media':
                    raise DomainError('invalid_media', 'Producer resolution requires media')
                seen.add(key)
                chain.append(_ref(current))
                body = current['body']
                deps = direct(current)
                producers = [obj for obj in deps if obj['kind'] in ('candidate', 'dispatch-intent', 'cut')]
                has_intent = 'provenance' in body
                has_cut = 'source_cut' in body
                has_derivative = body.get('derivative_of') is not None
                if (sum((has_intent, has_cut, has_derivative)) > 1 or 'composition' in body
                        or ('completes' in body and not has_intent)
                        or current['author'] == 'composition_service' or ('assembly_manifest' in body and not has_cut)):
                    raise DomainError('invalid_media', 'Media has contradictory or unsupported producer bindings')
                if has_derivative:
                    if not allow_derivatives or current['author'] not in ('worker_service', 'reader_service') or producers:
                        raise DomainError('invalid_media', 'Derivative origin is not an allowed direct service edge')
                    parent = resolve(body['derivative_of'])
                    if parent['kind'] != 'media' or sum(_ref(obj) == _ref(parent) for obj in deps) != 1:
                        raise DomainError('invalid_media', 'Derivative source is not an exact direct dependency')
                    current = parent
                    continue
                if has_cut:
                    authority = resolve(body['source_cut'])
                    if (authority['kind'] != 'cut' or authority['author'] != 'cut_service'
                            or len(producers) != 1 or _ref(producers[0]) != _ref(authority)
                            or ('assembly_manifest' in body and body['assembly_manifest'] != _ref(authority))):
                        raise DomainError('invalid_media', 'Cut origin is not one exact direct assembly')
                    provenance = {'binding':'source-cut', 'producer_media':_ref(current),
                                  'declared_upload':current['author'] != 'cut_service'}
                    kind = 'cut'
                elif has_intent:
                    proof = body['provenance']
                    if not isinstance(proof, dict) or 'intent' not in proof or current['author'] != 'worker_service':
                        raise DomainError('invalid_media', 'Generation provenance is not service-authored')
                    intent = resolve(proof['intent'])
                    if (intent['kind'] == 'dispatch-intent' and intent['author'] == 'submission_service' and intent['revision'] == 1
                            and intent['body'].get('operation') == 'complete-draft'):
                        # a 1080p completion is the draft take it completes, so its origin is
                        # the draft's origin (same card, same frames and seed).
                        draft = resolve(intent['body'].get('target'))
                        if (len(producers) != 1 or _ref(producers[0]) != _ref(intent) or body.get('completes') != intent['body']['target']
                                or draft['kind'] != 'media' or sum(_ref(obj) == _ref(draft) for obj in direct(intent)) != 1):
                            raise DomainError('invalid_media', 'Completion is not one direct completion of its draft take')
                        chain.append(_ref(intent))
                        current = draft
                        continue
                    if (intent['kind'] != 'dispatch-intent' or intent['author'] != 'submission_service' or intent['revision'] != 1
                            or intent['body'].get('operation') != 'submit' or 'completes' in body or len(producers) != 1
                            or _ref(producers[0]) != _ref(intent)):
                        raise DomainError('invalid_media', 'Generation origin is not one direct submitted intent')
                    ib = intent['body']
                    authority = resolve(ib.get('candidate'))
                    candidate(authority)
                    candidates = [obj for obj in direct(intent) if obj['kind'] == 'candidate']
                    if (len(candidates) != 1 or _ref(candidates[0]) != _ref(authority)
                            or any(field not in ib or field not in authority['body'] or ib[field] != authority['body'][field]
                                   for field in ('target', 'release_id', 'task', 'request'))
                            or not isinstance(ib['request'], dict)
                            or ('requested_parameters' in proof and proof['requested_parameters'] != ib['request'].get('params'))):
                        raise DomainError('invalid_media', 'Submitted intent differs from its exact compiler candidate')
                    resolve(ib['target'])
                    chain.append(_ref(intent))
                    provenance = {'binding':'dispatch-intent', 'producer_media':_ref(current), 'intent':_ref(intent)}
                    kind = 'candidate'
                else:
                    if current['author'] != 'worker_service' or len(producers) != 1:
                        raise DomainError('invalid_media', 'Media lacks a unique direct service producer')
                    authority = producers[0]
                    candidate(authority)
                    provenance = {'binding':'direct-candidate', 'producer_media':_ref(current)}
                    kind = 'candidate'
                chain.append(_ref(authority))
                return {'kind':kind, 'authority':authority, 'media':original, 'chain':chain, 'provenance':provenance}

    def applicable_method(self, project_id: str, selection: ObjectRef, task: str,
                          *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        with self.store._using(conn, write=False) as db:
            graph = self.pinned_graph(project_id, selection, conn=db)
            if graph["stale"]:
                raise DomainError("stale_input", "Method selection or its inputs changed")
            obj = self.store.get_object(project_id, selection.object_id, revision=selection.revision, conn=db)
            if obj["kind"] != "method":
                raise DomainError("unsupported_method", "Expected a recorded method selection")
            project = self.store.get_object(project_id, project_id, conn=db)
            choice = obj["body"]["content"]
            method_id = choice["method_id"]
            method = self.config.require_method(method_id, task=task)
            # No rule catalog: the manuals are the writer's; the platform checks send integrity.
            return {"method_id": method_id, "selection": _ref(obj), "target": choice["target"],
                    "definition": method, "rules": [], "release_id": project["body"]["release_id"]}

    def review_policy(self, project_id: str, operation: str, task: str | None,
                      method_id: str | None = None, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """No platform reviewer is required for any operation: the writer's reviewer
        is advice on the desk, never a gate, so the policy lists no roles."""
        with self.store._using(conn, write=False) as db:
            release_id = self.store.get_object(project_id, project_id, conn=db)["body"]["release_id"]
            return {"operation": operation, "task": task, "method_id": method_id, "required_roles": [],
                    "routes": {}, "release_id": release_id, "precedence": "none"}

    @staticmethod
    def guard_mutation(store: Store, project_id: str, artifact_id: str | None, conn: sqlite3.Connection) -> None:
        """Mandatory default Projects hook. Only service-owned reopen records relax a lock."""
        if artifact_id is None:
            return
        for lock in store.list_objects(project_id, kind="picture-lock", conn=conn):
            body = lock["body"]
            if lock["author"] != SERVICE_AUTHOR or body["state"] not in ("locked", "partially-reopened"):
                continue
            protected = {ref["object_id"] for ref in body["protected"]}
            if artifact_id in protected and artifact_id not in body.get("reopened_targets", []):
                raise DomainError("locked", "Object belongs to a picture-locked cut", object_ref=artifact_id,
                                  repair="Use the scoped repair flow with an explicit reason")

    def inspect(self, actor: Principal, project_id: str, *, target: ObjectRef | None = None,
                task: str | None = None, method: ObjectRef | None = None,
                conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Report missing inputs and permitted preparation, never authorize paid submit."""
        with self.store._using(conn, write=False) as db:
            self.auth.authorize(actor, project_id, "context", conn=db)
            project = self.store.get_object(project_id, project_id, conn=db)
            result: dict[str, Any] = {"branch": project["body"]["branch"], "allowed": [], "missing": [],
                                      "stale": False, "accepted": False, "review_required": [], "review_policy_configured": False}
            for operation in ("revise-artifact", "upload", "select-method", "patch"):
                try:
                    self.auth.authorize(actor, project_id, operation, conn=db)
                    result["allowed"].append(operation)
                except DomainError:
                    pass
            if target is None:
                return result
            graph = self.pinned_graph(project_id, target, conn=db)
            result.update(stale=graph["stale"], stale_reasons=graph["reasons"])
            objects = [node["object"] for node in graph["nodes"]]
            root = self.store.get_object(project_id, target.object_id, revision=target.revision, conn=db)
            kinds = {obj["kind"] for obj in objects}
            try:
                self.guard_mutation(self.store, project_id, target.object_id, db)
            except DomainError:
                result["missing"].append("scoped-reopen")
                result["allowed"] = [op for op in result["allowed"] if op != "revise-artifact"]
            if task is None:
                return result
            if task not in ("shot", "stress", "image", "image-edit", "cut"):
                raise DomainError("invalid_input", "Unknown production task")
            # only money, provider limits and send integrity block; a readiness item is
            # "missing" (blocks prepare) only when it is one of those or the owner's own switch asks for it. Blocking:
            # scoped-reopen (a picture lock), method-selection and its errors (the route that is paid for), review-policy
            # (none configured), and with the owner's 复刻先让 Gemini 看原片 switch on, source-understanding and
            # source-observation. Advice: segment-expectation, whole-scene-intent, source-understanding with the switch off.
            if task in ("shot", "cut") and project["body"]["branch"] == "recreation":
                from production import switches
                reading = switches.current(self.store, self.config, project_id, db)["source_reading"]
                if "source-understanding" not in kinds:
                    (result["missing"] if reading else result.setdefault("advice", [])).append("source-understanding")
                elif task == "shot" and reading:
                    # the understanding must cite the reader's observation of its source
                    # (the owner's 2026-09-22 order: Gemini reads the source first).
                    understood = [o for o in objects if o["kind"] == "source-understanding"]
                    if not any(isinstance(o["body"].get("content"), dict) and o["body"]["content"].get("observation")
                               for o in understood):
                        result["missing"].append("source-observation")
            if task == "shot" or (task == "stress" and root["kind"] == "shot"):
                scenes = [obj for obj in objects if obj["kind"] == "scene" and obj["body"].get("content")]
                if not scenes:
                    result.setdefault("advice", []).append("whole-scene-intent")
                content = root["body"].get("content", {})
                expected = content.get("Direction", {}).get("expected visible performance", "") if isinstance(content, dict) else ""
                if not (isinstance(expected, str) and expected.strip()) and "expectation" not in kinds:
                    # the expected visible performance is the writer's own check, not a gate.
                    result.setdefault("advice", []).append("segment-expectation")
            if method is None:
                result["missing"].append("method-selection")
            else:
                try:
                    applicable = self.applicable_method(project_id, method, task, conn=db)
                    expected_target = applicable["target"]
                    if not any(obj["object_id"] == expected_target["object_id"] and obj["revision"] == expected_target["revision"] for obj in objects):
                        raise DomainError("unsupported_method", "Method belongs to an unrelated target")
                    result["method"] = applicable
                except DomainError as exc:
                    result["missing"].append(exc.code)
            try:
                policy = self.review_policy(project_id, "prepare", task, result.get("method", {}).get("method_id"), conn=db)
                result.update(review_required=policy["required_roles"], review_policy_configured=True, review_policy=policy)
            except DomainError as exc:
                result["missing"].append("review-policy")
                result["review_policy_error"] = exc.as_dict()
            if "method" in result:
                result["pending_rule_checks"] = [rule["id"] for rule in result["method"]["rules"]]
            if not result["missing"] and not result["stale"]:
                try:
                    self.auth.authorize(actor, project_id, "prepare", conn=db)
                    result["allowed"].append("prepare")
                except DomainError:
                    pass
            return result

    def record_picture_lock(self, project_id: str, cut: ObjectRef, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Trusted cut service only, after its checks. Lock is not final acceptance."""
        with self.store._using(conn) as db:
            graph = self.pinned_graph(project_id, cut, conn=db)
            obj = self.store.get_object(project_id, cut.object_id, revision=cut.revision, conn=db)
            if obj["kind"] != "cut" or graph["stale"]:
                raise DomainError("stale_input", "Only a current cut can be picture-locked")
            for existing in self.store.list_objects(project_id, kind="picture-lock", conn=db):
                if existing["author"] == SERVICE_AUTHOR and existing["body"]["cut"] == _ref(obj):
                    if existing["body"]["state"] == "locked":
                        return existing
                    raise DomainError("locked", "A reopened cut requires a new cut revision before locking")
            return self.store.create_object(project_id, "picture-lock", {"cut": _ref(obj), "state": "locked",
                "protected": [node["ref"] for node in graph["nodes"]], "reopened_targets": [],
                "dependencies": [_ref(obj)], "accepted": False}, SERVICE_AUTHOR, conn=db)

    def reopen(self, actor: Principal, project_id: str, lock_ref: ObjectRef, targets: list[ObjectRef], reason: str,
               *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Bounded patch service calls this before repair; never a general unlock endpoint.

        Only listed locked objects become writable. Old finishing/final outputs
        remain readable but are explicitly invalidated. Unrelated cuts stay current.
        """
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000 or not targets:
            raise DomainError("invalid_input", "A bounded target list and repair reason are required")
        with self.store._using(conn) as db:
            self.auth.authorize(actor, project_id, "patch", conn=db)
            lock = self.store.get_object(project_id, lock_ref.object_id, conn=db)
            if (lock["kind"] != "picture-lock" or lock["author"] != SERVICE_AUTHOR
                    or lock["revision"] != lock_ref.revision or (lock_ref.digest and lock["digest"] != lock_ref.digest)):
                raise DomainError("stale_input", "Picture lock changed or is not service-issued")
            return self._reopen(project_id, lock, targets, reason, actor.actor_id, db)

    def reopen_for_owner(self, project_id: str, shot_id: str, reason: str, *, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        """The owner changes a pick after 这版可以: reopen that shot and its cut in every lock
        protecting it, so the pick is recorded and the film is cut again. Called only inside an authorized human
        decision; HF has no platform approval step (HF_CANONICAL.md §8), so the lock binds only the agent."""
        reopened = []
        for lock in self.store.list_objects(project_id, kind="picture-lock", conn=conn):
            body = lock["body"]
            protected = {ref["object_id"] for ref in body["protected"]}
            if (lock["author"] != SERVICE_AUTHOR or body["state"] not in ("locked", "partially-reopened")
                    or shot_id not in protected or shot_id in body.get("reopened_targets", [])):
                continue
            targets = [ObjectRef(**_ref(self.store.get_object(project_id, oid, conn=conn)))
                       for oid in (shot_id, body["cut"]["object_id"]) if oid in protected]
            reopened.append(self._reopen(project_id, lock, targets, reason, "owner_decision", conn))
        return reopened

    def _reopen(self, project_id: str, lock: dict[str, Any], targets: list[ObjectRef], reason: str,
                requested_by: str, db: sqlite3.Connection) -> dict[str, Any]:
        protected = {ref["object_id"] for ref in lock["body"]["protected"]}
        changed: list[dict[str, Any]] = []
        for target in targets:
            obj = self.store.get_object(project_id, target.object_id, conn=db)
            if (target.object_id not in protected or obj["revision"] != target.revision
                    or (target.digest and obj["digest"] != target.digest)):
                raise DomainError("stale_input", "Repair target is not a current member of this lock")
            changed.append(_ref(obj))
        # A reopened card brings its method choice with it: shooting it again re-selects that method, which the
        # agent never writes by hand (simulated run: a reshoot after 这版可以 stopped on the locked method).
        shots = {ref["object_id"] for ref in changed
                 if self.store.get_object(project_id, ref["object_id"], conn=db)["kind"] == "shot"}
        for method in self.store.list_objects(project_id, kind="method", conn=db):
            content = method["body"].get("content") if isinstance(method["body"].get("content"), dict) else {}
            if (method["object_id"] in protected and (content.get("target") or {}).get("object_id") in shots
                    and method["object_id"] not in {ref["object_id"] for ref in changed}):
                changed.append(_ref(method))
        affected_ids = {ref["object_id"] for ref in changed} | {lock["body"]["cut"]["object_id"]}
        invalidated = []
        for obj in self.store.list_objects(project_id, conn=db):
            if obj["kind"] not in ("finishing", "final", "human-receipt"):
                continue
            graph = self.pinned_graph(project_id, ObjectRef(**_ref(obj)), conn=db)
            if affected_ids.intersection(node["ref"]["object_id"] for node in graph["nodes"]):
                record = self.store.create_object(project_id, "invalidation", {"target": _ref(obj),
                    "lock": _ref(lock), "changed_inputs": changed, "reason": reason}, SERVICE_AUTHOR, conn=db)
                invalidated.append(_ref(record))
        body = {**lock["body"], "state": "partially-reopened",
                "reopened_targets": sorted(set(lock["body"]["reopened_targets"]) | {ref["object_id"] for ref in changed})}
        revised = self.store.append_revision(project_id, lock["object_id"], lock["revision"], body, SERVICE_AUTHOR, conn=db)
        reopen = self.store.create_object(project_id, "reopen", {"lock": _ref(revised), "targets": changed,
            "reason": reason, "requested_by": requested_by, "invalidated": invalidated, "accepted": False}, SERVICE_AUTHOR, conn=db)
        return {**reopen, "accepted": False}
