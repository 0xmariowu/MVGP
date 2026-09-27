"""Permissioned creative revisions; draft content never confers review authority.

Asset/source schemas are local platform representations, not new HF craft rules.
Legacy scene text and shot cards remain verbatim. Exact selections are separate
artifacts, so producing a costume alternative cannot silently replace a voice.
"""
from __future__ import annotations

import builtins
import sqlite3
from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import (
    Field,
    JsonValue,
    StringConstraints,
    ValidationError,
    model_validator,
)

from production.auth import AuthService, Principal
from production.contracts import (
    ArtifactRevision,
    Contract,
    DomainError,
    MethodSelection,
    ObjectRef,
    ProjectCreate,
    Text,
    UploadRequest,
    new_id,
)
from production.media import MediaStore
from production.store import Store

AssetRole = Literal["look", "world", "visual", "state", "behavior", "voice", "delivery"]
# Higgsfield element categories (production/HF_CANONICAL.md §2); optional so earlier records stay valid.
ElementCategory = Literal["character", "environment", "prop"]


class AssetDraft(Contract):
    type: Literal["asset"]
    role: AssetRole
    tag: Annotated[str, StringConstraints(pattern=r"^@[A-Za-z][A-Za-z0-9_-]{0,127}$")]
    definition: dict[str, JsonValue] | str
    media_refs: list[ObjectRef] = Field(default_factory=list)
    base_identity: ObjectRef | None = None
    category: ElementCategory | None = None

    @model_validator(mode="after")
    def identity_for_state(self) -> AssetDraft:
        if self.role in ("state", "delivery") and self.base_identity is None:
            raise ValueError("State/delivery drafts need an explicit base identity")
        return self


class AssetSelectionDraft(Contract):
    type: Literal["asset-selection"]
    target: ObjectRef
    selected: dict[AssetRole, ObjectRef]

    @model_validator(mode="after")
    def has_selection(self) -> AssetSelectionDraft:
        if not self.selected:
            raise ValueError("A selection must name at least one exact asset revision")
        return self


class SourceDraft(Contract):
    type: Literal["source-understanding"]
    source: ObjectRef
    start_seconds: Annotated[float, Field(ge=0)]
    end_seconds: Annotated[float, Field(gt=0)]
    observed_facts: list[Text]
    uncertain_interpretations: list[Text]
    adaptation_scope: Text
    # the reader's observation of this same source clip (Gemini watched it first).
    observation: ObjectRef | None = None

    @model_validator(mode="after")
    def ordered_range(self) -> SourceDraft:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("Source end must follow start")
        return self


class MethodDraft(Contract):
    method_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]
    rationale: Text
    target: ObjectRef


def draft_schemas() -> dict[str, Any]:
    models: dict[str, type[Contract]] = {
        "asset": AssetDraft, "asset-selection": AssetSelectionDraft,
        "source-understanding": SourceDraft, "method": MethodDraft,
    }
    return {name: model.model_json_schema() for name, model in models.items()}


class Projects:
    def __init__(self, store: Store, auth: AuthService, media: MediaStore, *, label: str,
                 default_envelopes: tuple[tuple[str, str, int], ...] = ()) -> None:
        if not label:
            raise ValueError("A runtime config label is required")
        # A new project's `release_id` is the runtime config label, kept only as a label.
        self.store, self.auth, self.media, self.label = store, auth, media, label
        # (owner: "先不要搞什么批预算这个事儿，就我自己用"): every new project gets its
        # spending envelopes at creation, one per (budget_key, unit, ceiling); spend is still recorded per project.
        self.default_envelopes = tuple(default_envelopes)

    def _scope(self, actor: Principal, project_id: str, operation: str) -> str:
        return f"{actor.actor_id}:{actor.credential_id}:{project_id}:{operation}"

    def create(self, actor: Principal, request: ProjectCreate) -> dict[str, Any]:
        with self.store.transaction() as db:
            self.auth.authorize(actor, None, "create-project", conn=db)
            def make(conn: sqlite3.Connection) -> dict[str, Any]:
                project_id = new_id("project")
                body = {"title": request.title, "branch": request.branch, "brief": request.brief,
                        "release_id": self.label,
                        # the 复刻先让 Gemini 看原片 switch applies (on by default for recreation).
                        "reader_switch": True}
                project = self.store.create_project(project_id, body, actor.actor_id, conn=conn)
                self.auth.grant_created_project(actor, project_id, conn=conn)
                for budget_key, unit, ceiling in self.default_envelopes:
                    self.store.set_budget(project_id, ceiling, unit, budget_key=budget_key, conn=conn)
                return project
            return self.store.run_idempotent(self._scope(actor, "new", "create-project"), request.idempotency_key,
                                             request.model_dump(), make, conn=db)

    def _ref(self, project_id: str, ref: ObjectRef, conn: sqlite3.Connection) -> tuple[dict[str, Any], dict[str, Any]]:
        obj = self.store.get_object(project_id, ref.object_id, revision=ref.revision, conn=conn)
        if ref.digest is not None and ref.digest != obj["digest"]:
            raise DomainError("stale_input", "Dependency digest does not match its revision", object_ref=ref.object_id)
        return {"object_id": ref.object_id, "revision": ref.revision, "digest": obj["digest"]}, obj

    def _content(self, project_id: str, request: ArtifactRevision, conn: sqlite3.Connection) -> tuple[Any, list[dict[str, Any]]]:
        """Validate typed drafts without treating nested prose as trusted metadata."""
        content = request.content
        refs = list(request.dependencies)
        try:
            if request.kind == "asset":
                if not isinstance(content, dict):
                    raise ValueError("Assets require a typed asset or selection document")
                if content.get("type") == "asset-selection":
                    selected = AssetSelectionDraft.model_validate(content)
                    refs.append(selected.target)
                    for role, ref in selected.selected.items():
                        _, obj = self._ref(project_id, ref, conn)
                        asset = AssetDraft.model_validate(obj["body"].get("content"))
                        if obj["kind"] != "asset" or asset.role != role:
                            raise ValueError("Selected asset role does not match its slot")
                        refs.append(ref)
                else:
                    asset = AssetDraft.model_validate(content)
                    for media in asset.media_refs:
                        _, obj = self._ref(project_id, media, conn)
                        if obj["kind"] != "media":
                            raise ValueError("Asset media references must name uploaded media")
                        refs.append(media)
                    if asset.base_identity:
                        _, obj = self._ref(project_id, asset.base_identity, conn)
                        base = AssetDraft.model_validate(obj["body"].get("content"))
                        expected = "voice" if asset.role == "delivery" else "visual"
                        if obj["kind"] != "asset" or base.role != expected:
                            raise ValueError("Variant base does not match visual/voice identity")
                        refs.append(asset.base_identity)
            elif request.kind == "source-understanding":
                source = SourceDraft.model_validate(content)
                _, obj = self._ref(project_id, source.source, conn)
                duration = obj["body"].get("probe", {}).get("duration", 0)
                if obj["kind"] != "media" or source.end_seconds > duration:
                    raise ValueError("Source range exceeds verified media duration")
                refs.append(source.source)
                if source.observation is not None:
                    _, seen = self._ref(project_id, source.observation, conn)
                    watched = seen["body"].get("source") or {}
                    if (seen["kind"] != "observation" or seen["author"] != "reader_service"
                            or seen["body"].get("status") != "succeeded"
                            or (watched.get("object_id"), watched.get("revision")) != (source.source.object_id, source.source.revision)):
                        raise ValueError("The observation must be the reader's succeeded reading of this same source")
                    refs.append(source.observation)
            elif request.kind == "method":
                method = MethodDraft.model_validate(content)
                refs.append(method.target)
        except (ValidationError, ValueError) as exc:
            raise DomainError("invalid_input", "Creative draft does not match its type", field="content",
                              repair="Use the published draft schema and compatible referenced roles") from exc
        dependencies = {}
        for ref in refs:
            pinned, _ = self._ref(project_id, ref, conn)
            # One version per input: a reference the platform derives (a selection's target, a method's card) comes
            # after the author's list and wins over an older copy of it passed back unchanged (simulated run: a
            # rebound selection kept the old scene revision in its dependencies and stayed stale).
            dependencies[ref.object_id] = pinned
        return content, list(dependencies.values())

    def _write(self, actor: Principal, project_id: str, request: ArtifactRevision, artifact_id: str | None,
               conn: sqlite3.Connection) -> dict[str, Any]:
        previous = None
        if artifact_id:
            from production.workflow import Workflow
            Workflow.guard_mutation(self.store, project_id, artifact_id, conn)
            previous = self.store.get_object(project_id, artifact_id, conn=conn)
            if previous["kind"] != request.kind or previous["body"].get("logical_path") != request.logical_path:
                raise DomainError("invalid_input", "Artifact kind and logical path are stable")
            if request.expected_revision != previous["revision"]:
                raise DomainError("revision_conflict", "Artifact changed", current_revision=previous["revision"])
        elif request.expected_revision != 0:
            raise DomainError("revision_conflict", "New artifacts expect revision zero", current_revision=0)
        for item in self.store.list_objects(project_id, conn=conn):
            if item["object_id"] != artifact_id and item["body"].get("logical_path") == request.logical_path:
                raise DomainError("revision_conflict", "Logical path already belongs to an object", object_ref=item["object_id"])
        content, dependencies = self._content(project_id, request, conn)
        if previous and request.kind == "asset":
            before, after = previous["body"]["content"], content
            if before.get("type") != after.get("type") or (before.get("type") == "asset" and
                    (before.get("role") != after.get("role") or before.get("tag") != after.get("tag"))):
                raise DomainError("invalid_input", "Asset channel and identity tag are stable; create a variant instead")
        if any(ref["object_id"] == artifact_id for ref in dependencies):
            raise DomainError("invalid_input", "An artifact cannot depend on itself")
        body = {"logical_path": request.logical_path, "parent_path": str(PurePosixPath(request.logical_path).parent),
                "content": content, "dependencies": dependencies}
        if previous:
            return self.store.append_revision(project_id, previous["object_id"], request.expected_revision, body, actor.actor_id, conn=conn)
        return self.store.create_object(project_id, request.kind, body, actor.actor_id, conn=conn)

    def revise(self, actor: Principal, project_id: str, request: ArtifactRevision, *, artifact_id: str | None = None) -> dict[str, Any]:
        with self.store.transaction() as db:
            self.auth.authorize(actor, project_id, "revise-artifact", conn=db)
            payload = {"artifact_id": artifact_id, **request.model_dump()}
            return self.store.run_idempotent(self._scope(actor, project_id, "revise-artifact"), request.idempotency_key,
                                             payload, lambda conn: self._write(actor, project_id, request, artifact_id, conn), conn=db)

    def select_method(self, actor: Principal, project_id: str, request: MethodSelection) -> dict[str, Any]:
        """Record a choice; released gates separately decide applicability.

        First creation guards target.revision; later changes guard the method
        artifact's revision. The target reference must independently be current.
        The response includes the next method revision to use as the write guard.
        """
        with self.store.transaction() as db:
            self.auth.authorize(actor, project_id, "select-method", conn=db)
            def choose(conn: sqlite3.Connection) -> dict[str, Any]:
                pinned, target = self._ref(project_id, request.target, conn)
                current = self.store.get_object(project_id, target["object_id"], conn=conn)
                if current["revision"] != request.target.revision:
                    raise DomainError("stale_input", "Method target is no longer current")
                path = f"methods/{target['object_id']}.json"
                old = next((x for x in self.store.list_objects(project_id, kind="method", conn=conn)
                            if x["body"].get("logical_path") == path), None)
                expected = old["revision"] if old else target["revision"]
                if request.expected_revision != expected:
                    raise DomainError("revision_conflict", "Method selection changed", current_revision=expected)
                draft = ArtifactRevision(idempotency_key=request.idempotency_key, expected_revision=old["revision"] if old else 0,
                                         kind="method", logical_path=path,
                                         content={"method_id": request.method_id, "rationale": request.rationale, "target": pinned})
                result = self._write(actor, project_id, draft, old["object_id"] if old else None, conn)
                return {**result, "mutation_guard": {"expected_revision": result["revision"], "target": pinned}}
            return self.store.run_idempotent(self._scope(actor, project_id, "select-method"), request.idempotency_key,
                                             request.model_dump(), choose, conn=db)

    def get(self, actor: Principal, project_id: str, object_id: str, *, revision: int | None = None) -> dict[str, Any]:
        with self.store.transaction(write=False) as db:
            self.auth.authorize(actor, project_id, "artifacts", conn=db)
            return self.store.get_object(project_id, object_id, revision=revision, conn=db)

    def list(self, actor: Principal, project_id: str, *, kind: str | None = None) -> list[dict[str, Any]]:
        with self.store.transaction(write=False) as db:
            self.auth.authorize(actor, project_id, "artifacts", conn=db)
            return self.store.list_objects(project_id, kind=kind, conn=db)

    def tree(self, actor: Principal, project_id: str) -> builtins.list[dict[str, Any]]:
        objects = self.list(actor, project_id)
        return sorted(({"object_id": x["object_id"], "revision": x["revision"], "kind": x["kind"],
                        "logical_path": x["body"]["logical_path"],
                        "parent_path": str(PurePosixPath(x["body"]["logical_path"]).parent)}
                       for x in objects if "logical_path" in x["body"]), key=lambda x: x["logical_path"])

    def upload(self, actor: Principal, project_id: str, request: UploadRequest, chunks: Iterable[bytes]) -> dict[str, Any]:
        self.auth.authorize(actor, project_id, "upload")
        def publish(metadata: dict[str, Any]) -> dict[str, Any]:
            with self.store.transaction() as db:
                self.auth.authorize(actor, project_id, "upload", conn=db)
                def save(conn: sqlite3.Connection) -> dict[str, Any]:
                    if request.source_cut:
                        ref, cut = self._ref(project_id, request.source_cut, conn)
                        if cut["kind"] != "cut" or cut["author"] != "cut_service":
                            raise DomainError("invalid_input", "Finishing upload must reference a service-issued cut")
                        metadata["source_cut"] = ref
                        metadata["dependencies"] = [ref]
                    return self.store.create_object(project_id, "media", metadata, actor.actor_id, conn=conn)
                return self.store.run_idempotent(self._scope(actor, project_id, "upload"), request.idempotency_key,
                                                 request.model_dump(), save, conn=db)
        return self.media.put(project_id, chunks, request.media_type, actor.actor_id,
                              logical_path=request.logical_path, expected_size=request.byte_length,
                              expected_hash=request.sha256, publish=publish)
