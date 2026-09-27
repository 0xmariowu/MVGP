"""Image-only compilation from authored drafts and released route capabilities.

AssetDraft.definition uses ImageDefinition: recipe, description, visual_treatment,
optional aspect_ratio/resolution, base, CHANGE/PRESERVE text and reference_roles.
The actual look comes from exact asset selections in inputs. Initial look probes
need no prior selected look. All reference slots name media objects, never URLs.
No provider call, upload, generation job, video assembler or photo-style filler.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError, model_validator

from production.contracts import Contract, DomainError, ObjectRef, Text, content_hash
from production.media import MediaStore
from production.output_contract import validate_policy, validate_request
from production.projects import AssetDraft, AssetSelectionDraft
from production.store import Store
from production.workflow import Workflow

ReferenceRole = Literal["look", "identity", "state", "world", "pose", "geometry", "composition-source"]


class ImageDefinition(Contract):
    recipe: Literal["base-portrait", "state-variant", "location-angle", "pose", "diagram"]
    description: Text
    visual_treatment: Literal["photoreal", "project-animation"]
    aspect_ratio: str | None = None
    resolution: str | None = None
    base: ObjectRef | None = None
    change: Text | None = None
    preserve: Text | None = None
    reference_roles: list[ReferenceRole] = Field(default_factory=list)
    # the manuals reach the image writer too (LIRA), like a shot card's writer.
    playbook_version: str | None = None
    change_note: str | None = None
    # The element's descriptor, pasted word for word into every shot prompt (HF: an element is a descriptor plus one
    # image, 7 of 12 projects; production/HF_CANONICAL.md §7). `description` is the image prompt, never sent with a shot.
    descriptor: Text | None = None


class ImageRoute(Contract):
    # apilio_gpt_image_2_5: the owner's chosen channel for every image.
    job_type: Literal["nano_banana_pro", "apilio_gpt_image_2_5"]
    tasks: list[Literal["image", "image-edit"]]
    aspect_ratios: Annotated[list[str], Field(min_length=1)]
    resolutions: Annotated[list[str], Field(min_length=1)]
    defaults: dict[Literal["aspect_ratio", "resolution"], str]
    fixed_parameters: dict[Literal["quality", "variant"], str] = Field(default_factory=dict)
    max_references: Annotated[int, Field(ge=0, le=14)]
    capability_role: str
    output_contract: dict[str, Any]
    reference_edit_verified: bool = False
    reference_edit_evidence_role: str | None = None
    max_reference_bytes: Annotated[int, Field(gt=0, le=128 * 1024 * 1024)] = 32 * 1024 * 1024

    @model_validator(mode="after")
    def valid_defaults(self) -> ImageRoute:
        if (set(self.defaults) != {"aspect_ratio", "resolution"}
                or self.defaults["aspect_ratio"] not in self.aspect_ratios
                or self.defaults["resolution"] not in self.resolutions):
            raise ValueError("Image defaults must be explicit members of the allowed parameters")
        if self.reference_edit_verified and not self.reference_edit_evidence_role:
            raise ValueError("Reference-conditioned edits require released capability evidence")
        # The Higgsfield GPT Image route with its fixed quality/variant went with the fallback.
        if self.fixed_parameters:
            raise ValueError("No image route takes fixed settings")
        if self.job_type == "apilio_gpt_image_2_5" and self.max_references > 4:
            raise ValueError("The apilio image route takes at most four references")
        return self


def definition_schema() -> dict[str, Any]:
    return ImageDefinition.model_json_schema()


class AssetMethods:
    def __init__(self, store: Store, media: MediaStore, workflow: Workflow, *,
                 route_profiles: Mapping[str, dict[str, Any]]) -> None:
        self.store, self.media, self.workflow = store, media, workflow
        self.route_profiles = json.loads(json.dumps(dict(route_profiles)))

    def _object(self, project_id: str, ref: ObjectRef, conn: sqlite3.Connection) -> dict[str, Any]:
        obj = self.store.get_object(project_id, ref.object_id, revision=ref.revision, conn=conn)
        if ref.digest is not None and ref.digest != obj["digest"]:
            raise DomainError("stale_input", "Image input digest does not match its revision")
        return obj

    def _route(self, applicable: dict[str, Any], recipe: str, task: str) -> tuple[str, ImageRoute]:
        config = self.workflow.config
        try:
            document = config.section("image_routes")
            profile_id = document["method_routes"][applicable["method_id"]]
            pinned = document["profiles"][profile_id]
            if self.route_profiles.get(profile_id) != pinned:
                raise ValueError("Route profile differs from released configuration")
            profile = ImageRoute.model_validate(pinned)
            validate_policy(profile.output_contract, modality="image", resolutions=profile.resolutions)
            if task not in profile.tasks or recipe not in document["method_recipes"][applicable["method_id"]]:
                raise ValueError("Recipe/task is outside released method scope")
            capability = config.section(profile.capability_role)
            parameters = {entry["name"]: entry for entry in capability["params"]}
            expected = {"prompt", "resolution", "aspect_ratio", "image_references"}
            if profile.job_type == "apilio_gpt_image_2_5" and not all(
                    f"{a}|{r}" in capability.get("sizes", {}) for a in profile.aspect_ratios for r in profile.resolutions):
                raise ValueError("Every released apilio aspect/resolution needs a pinned pixel size")
            if (capability["job_type"] != profile.job_type or capability["type"] != "image"
                    or len(parameters) != len(capability["params"]) or set(parameters) != expected
                    or not set(profile.resolutions) <= set(parameters["resolution"]["enum"])
                    or not set(profile.aspect_ratios) <= set(parameters["aspect_ratio"]["enum"])
                    or (profile.job_type == "nano_banana_pro" and
                        "size(params.image_references) <= 14" not in [rule.get("cel") for rule in capability.get("rules", [])])):
                raise ValueError("Route is incompatible with the pinned provider contract")
            if profile.job_type == "apilio_gpt_image_2_5" and (
                    parameters["image_references"].get("type") != "array"
                    or parameters["prompt"].get("type") != "string"):
                raise ValueError("apilio image reference/prompt types changed")
            if task == "image-edit":
                if not profile.reference_edit_verified or not profile.reference_edit_evidence_role:
                    raise ValueError("Reference-conditioned image editing is unverified")
                if not config.section(profile.reference_edit_evidence_role):
                    raise ValueError("Edit capability evidence is empty")
            return profile_id, profile
        except (KeyError, TypeError, ValueError, ValidationError, DomainError) as exc:
            raise DomainError("unsupported_route", "Image route or capability is absent, incompatible or unverified") from exc

    def compile(self, project_id: str, target: ObjectRef, method_selection: ObjectRef, task: str, *,
                inputs: Sequence[ObjectRef] = (), mask: ObjectRef | None = None,
                conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        """Trusted compiler call. Returned references need worker-side byte resolution.

        Reference-conditioned editing is not pixel preservation. Masked provider
        edits remain unsupported; deterministic masked composition is separate.
        """
        if task not in ("image", "image-edit"):
            raise DomainError("unsupported_method", "Asset compiler accepts only image tasks")
        if mask is not None:
            raise DomainError("unsupported_route", "No validated masked image generation route is implemented")
        with self.store._using(conn, write=False) as db:
            applicable = self.workflow.applicable_method(project_id, method_selection, task, conn=db)
            obj = self._object(project_id, target, db)
            graph = self.workflow.pinned_graph(project_id, target, conn=db)
            if obj["kind"] != "asset" or graph["stale"]:
                raise DomainError("stale_input", "Image target must be a current asset draft")
            selected_target = applicable["target"]
            if selected_target["object_id"] != target.object_id or selected_target["revision"] != target.revision:
                raise DomainError("unsupported_method", "Image method belongs to another target")
            try:
                asset = AssetDraft.model_validate(obj["body"]["content"])
                definition = ImageDefinition.model_validate(asset.definition)
            except ValidationError as exc:
                raise DomainError("invalid_input", "Asset definition does not match the image authoring schema") from exc
            if asset.role in ("voice", "delivery"):
                raise DomainError("unsupported_method", "Voice identity and delivery are not image-generation targets")
            profile_id, profile = self._route(applicable, definition.recipe, task)
            # recipe, visual treatment and CHANGE / PRESERVE are the writer's craft, so advice;
            # an edit still needs the image it edits (what is sent).
            advice: list[str] = []
            if task == "image-edit" and not definition.base:
                raise DomainError("missing_prerequisite", "An image edit needs the base image it edits")
            if task == "image-edit" and not (definition.change and definition.preserve):
                advice.append("LIRA writes an edit as what changes and what stays exactly; the prompt is sent as written")
            unsent = [name for name, value in (("change", definition.change), ("preserve", definition.preserve))
                      if value and value.strip() not in definition.description]
            if task == "image-edit" and unsent:
                advice.append(f"{' and '.join(unsent)} is recorded on the element but not written in the prompt; "
                              "the platform sends only the writer's text")
            if task == "image" and any((definition.base, definition.change, definition.preserve)):
                advice.append("Edit controls (base, change, preserve) are not used by a new image")
            if definition.recipe == "state-variant" and task != "image-edit":
                advice.append("A state variant is usually an edit of its base (LIRA); this one is a new image")
            visual_modules = applicable["definition"].get("modules", {}).get("visual", [])
            explicit_media = set(visual_modules) & {"photoreal", "project-animation"}
            if ((explicit_media and definition.visual_treatment not in explicit_media)
                    or (definition.visual_treatment == "project-animation" and "lira-directional-sheet" in visual_modules)):
                advice.append(f"The method lists {', '.join(sorted(explicit_media)) or 'a directional sheet'}; this image is "
                              f"{definition.visual_treatment}")
            aspect = definition.aspect_ratio or profile.defaults["aspect_ratio"]
            resolution = definition.resolution or profile.defaults["resolution"]
            if aspect not in profile.aspect_ratios or resolution not in profile.resolutions:
                raise DomainError("invalid_input", "Image parameters are outside the released route")
            parameters: dict[str, str] = {"aspect_ratio": aspect, "resolution": resolution}
            validate_request(parameters, profile.output_contract)
            references: list[dict[str, Any]] = []
            dependencies: dict[tuple[str, int], dict[str, Any]] = {}
            input_manifest: dict[tuple[str, int, bool], dict[str, Any]] = {}
            roots = [target, method_selection, *inputs]
            look_text: list[str] = []
            selected_bases: set[tuple[str, int]] = set()
            byte_total = 0
            def include_graph(ref: ObjectRef) -> None:
                closure = self.workflow.pinned_graph(project_id, ref, conn=db)
                if closure["stale"]:
                    raise DomainError("stale_input", "Image input or its dependencies changed")
                for node in closure["nodes"]:
                    bound = node["ref"]
                    dependencies[(bound["object_id"], bound["revision"])] = bound
                    input_manifest[(bound["object_id"], bound["revision"], node["frozen_selection"])] = {"object_ref": bound, "frozen_selection": node["frozen_selection"]}
            def add_media(ref: ObjectRef, role: str, *, frozen: bool = False) -> None:
                nonlocal byte_total
                media = self._object(project_id, ref, db)
                if not frozen:
                    include_graph(ref)
                if media["kind"] != "media" or media["body"]["media_type"] not in ("image/png", "image/jpeg", "image/webp"):
                    raise DomainError("invalid_media", "Image references require verified image media")
                pinned_media = {"object_id": ref.object_id, "revision": ref.revision, "digest": media["digest"]}
                dependencies[(ref.object_id, ref.revision)] = pinned_media
                input_manifest[(ref.object_id, ref.revision, frozen)] = {"object_ref": pinned_media, "frozen_selection": frozen}
                data = self.media.read(project_id, ref.object_id, revision=ref.revision)
                digest = hashlib.sha256(data).hexdigest()
                if digest != media["body"]["sha256"]:
                    raise DomainError("invalid_media", "Image reference bytes changed")
                byte_total += len(data)
                if byte_total > profile.max_reference_bytes:
                    raise DomainError("insufficient_context", "Image references exceed the released byte bound")
                references.append({"object_ref": {"object_id": ref.object_id, "revision": ref.revision, "digest": media["digest"]},
                                   "sha256": digest, "role": role, "media_type": media["body"]["media_type"], "byte_length": len(data)})
            include_graph(target)
            include_graph(method_selection)
            media_inputs: list[ObjectRef] = []
            scope_ids = {project_id, *(node["ref"]["object_id"] for node in graph["nodes"])}
            supplied_ids = {ref.object_id for ref in inputs}
            for item in self.store.list_objects(project_id, kind="asset", conn=db):
                content = item["body"].get("content", {})
                if (isinstance(content, dict) and content.get("type") == "asset-selection"
                        and content.get("target", {}).get("object_id") in scope_ids
                        and "look" in content.get("selected", {}) and item["object_id"] not in supplied_ids):
                    advice.append("A project look is selected for this element but not given to the image")
            for input_ref in inputs:
                input_obj = self._object(project_id, input_ref, db)
                include_graph(input_ref)
                if input_obj["kind"] == "media":
                    media_inputs.append(input_ref)
                    continue
                try:
                    selection = AssetSelectionDraft.model_validate(input_obj["body"]["content"])
                except ValidationError as exc:
                    raise DomainError("invalid_input", "Image inputs must be image media or exact asset selections") from exc
                selection_target = self._object(project_id, selection.target, db)
                if selection.target.object_id not in scope_ids:
                    raise DomainError("invalid_input", "Asset selection belongs to an unrelated target")
                if self.store.get_object(project_id, selection.target.object_id, conn=db)["revision"] != selection_target["revision"]:
                    raise DomainError("stale_input", "Asset selection target changed")
                for role, selected_ref in selection.selected.items():
                    selected = self._object(project_id, selected_ref, db)
                    selected_asset = AssetDraft.model_validate(selected["body"]["content"])
                    if role == "look":
                        style = selected_asset.definition
                        if isinstance(style, dict) and style.get("visual_treatment", definition.visual_treatment) != definition.visual_treatment:
                            advice.append("The image's visual treatment differs from the selected project look")
                        look_text.append(style if isinstance(style, str) else json.dumps(style, ensure_ascii=False, sort_keys=True))
                    for ref in selected_asset.media_refs:
                        if role in ("visual", "state"):
                            selected_bases.add((ref.object_id, ref.revision))
                        if role in ("look", "world", "visual", "state"):
                            add_media(ref, "identity" if role == "visual" else role, frozen=True)
            if len(media_inputs) != len(definition.reference_roles):
                raise DomainError("invalid_input", "Each direct image input needs an explicit reference role")
            for ref, direct_role in zip(media_inputs, definition.reference_roles, strict=True):
                add_media(ref, direct_role)
            if definition.base:
                allowed_bases = {(ref.object_id, ref.revision) for ref in asset.media_refs}
                if asset.base_identity:
                    base_asset = AssetDraft.model_validate(self._object(project_id, asset.base_identity, db)["body"]["content"])
                    allowed_bases = {(ref.object_id, ref.revision) for ref in base_asset.media_refs}
                elif not allowed_bases:
                    allowed_bases = selected_bases
                if (definition.base.object_id, definition.base.revision) not in allowed_bases:
                    raise DomainError("invalid_input", "Edit base is not the explicit identity or selected base asset")
                if (definition.base.object_id, definition.base.revision) not in dependencies:
                    roots.append(definition.base)
                add_media(definition.base, "edit-base")
                references.insert(0, references.pop())
            if len(references) > profile.max_references:
                raise DomainError("invalid_input", "Image reference count exceeds the released route")
            # the image prompt is the writer's LIRA text only (lira-image-prompts.md:159-164);
            # the platform adds nothing, not even CHANGE / PRESERVE lines. The look and the reference roles stay
            # recorded in the references and lineage.
            del look_text
            root_refs = [self._object(project_id, ref, db) for ref in roots]
            return {"task": task, "target": {"object_id": obj["object_id"], "revision": obj["revision"], "digest": obj["digest"]}, "method_selection": applicable["selection"],
                    "method_id": applicable["method_id"], "release_id": applicable["release_id"],
                    "profile_id": profile_id, "profile_hash": content_hash(self.route_profiles[profile_id]), "job_type": profile.job_type,
                    "prompt": definition.description, "parameters": parameters, "advice": advice,
                    "references": references, "dependencies": list(dependencies.values()),
                    "dependency_roots": [{"object_id": item["object_id"], "revision": item["revision"], "digest": item["digest"]} for item in root_refs],
                    "input_manifest": list(input_manifest.values()),
                    "lineage": {"recipe": definition.recipe, "base": dependencies[(definition.base.object_id, definition.base.revision)] if definition.base else None,
                                "edit_semantics": "reference-conditioned" if task == "image-edit" else None, "pixel_preservation_verified": False},
                    "provenance": {"sources": applicable["definition"].get("sources", []), "authored_fields": sorted(definition.model_fields_set),
                                   "rules": [rule["id"] for rule in applicable["rules"]]}, "accepted": False}
