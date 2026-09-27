"""MVGP wire contracts. Creative JSON is data, never service authority.

Nested card/scene content intentionally retains the legacy vocabulary. Its
method-specific shape belongs to the released compiler/gates, not this transport.
Only server-owned object envelopes carry authors, hashes, review or job state.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]
BudgetKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Text = Annotated[str, StringConstraints(min_length=1, max_length=200000)]
Revision = Annotated[int, Field(ge=1)]
Branch = Literal["original", "recreation"]
TaskKind = Literal["image", "image-edit", "stress", "shot", "cut"]
ArtifactKind = Literal["brief", "script", "scene", "shot", "asset", "source-understanding", "expectation", "method", "feedback", "finishing"]
ReviewRole = Literal["standards", "director"]
JobState = Literal["queued", "dispatching", "submitted", "running", "succeeded", "failed", "cancelled", "unknown"]
ERROR_CODES = frozenset(["invalid_input", "unauthorized", "forbidden", "not_found", "revision_conflict", "idempotency_conflict", "missing_prerequisite", "stale_input", "rule_violation", "unsupported_method", "unsupported_route", "review_required", "insufficient_context", "budget_exceeded", "attempt_limit", "release_mismatch", "unknown_outcome", "invalid_media", "locked", "provider_failure"])


class DomainError(Exception):
    """Stable public failure; detail must never contain credentials/provider bodies."""
    def __init__(self, code: str, message: str, *, field: str | None = None,
                 object_ref: str | None = None, rule_id: str | None = None,
                 source: str | None = None, current_revision: int | None = None,
                 repair: str | None = None) -> None:
        if code not in ERROR_CODES:
            raise ValueError("Unknown domain error code")
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = {"field": field, "object_ref": object_ref, "rule_id": rule_id,
                            "source": source, "current_revision": current_revision, "repair": repair}

    def as_dict(self) -> dict[str, Any]:
        return dict(code=self.code, message=self.message, **self.details)


def canonical_json(value: Any) -> str:
    """Deterministic local JSON encoding, not a claim of RFC 8785 equivalence."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    def check(item: Any) -> None:
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("Canonical objects require string keys")
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError("Expected JSON-compatible content")
    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def new_id(prefix: str = "obj") -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", prefix):
        raise ValueError("Invalid identifier prefix")
    return f"{prefix}_{uuid4().hex}"


def safe_logical_path(value: str) -> str:
    """Validate a label, never resolve it as a physical storage path."""
    if (not value or len(value) > 512 or any(ord(c) < 32 for c in value)
            or any(c in value for c in "\\:%")
            or any(part in ("", ".", "..") for part in value.split("/"))):
        raise ValueError("Expected a relative logical path without traversal or encoding")
    return value


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ObjectRef(Contract):
    object_id: Identifier
    revision: Revision
    digest: Digest | None = None


class Mutation(Contract):
    idempotency_key: Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]


class Update(Mutation):
    expected_revision: Revision


class ProjectCreate(Mutation):
    expected_revision: Annotated[int, Field(ge=0, le=0)] = 0
    title: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    branch: Branch
    brief: str = ""


class ArtifactRevision(Mutation):
    expected_revision: Annotated[int, Field(ge=0)]
    kind: ArtifactKind
    logical_path: str
    content: dict[str, JsonValue] | str
    dependencies: list[ObjectRef] = Field(default_factory=list, max_length=256)

    _logical_path = field_validator("logical_path")(safe_logical_path)


class UploadRequest(Mutation):
    logical_path: str
    media_type: Literal["image/png", "image/jpeg", "image/webp", "video/mp4", "video/quicktime", "audio/wav", "audio/mpeg", "audio/mp4", "application/json", "text/plain", "text/markdown"]
    byte_length: Annotated[int, Field(gt=0, le=2_147_483_648)]
    sha256: Digest
    source_cut: ObjectRef | None = None

    _logical_path = field_validator("logical_path")(safe_logical_path)


class MethodSelection(Update):
    method_id: Identifier
    rationale: Text
    target: ObjectRef


class PrepareRequest(Update):
    task: TaskKind
    target: ObjectRef
    method_selection: ObjectRef
    inputs: list[ObjectRef] = Field(default_factory=list, max_length=256)
    mask: ObjectRef | None = None

    @model_validator(mode="after")
    def valid_mask(self) -> PrepareRequest:
        if self.mask is not None and self.task != "image-edit":
            raise ValueError("Masks apply only to image-edit tasks")
        return self


class SubmitRequest(Update):
    candidate_id: Identifier


class ObserveRequest(Update):
    media_id: Identifier
    questions: Annotated[list[Text], Field(min_length=1, max_length=100)]
    reader: Literal["video", "audio", "image"]
    source_offset_seconds: Annotated[float, Field(ge=0)] = 0.0
    time_scale: Annotated[float, Field(gt=0, le=100)] = 1.0


class CutSegment(Contract):
    take: ObjectRef
    start_seconds: Annotated[float, Field(ge=0)]
    end_seconds: Annotated[float, Field(gt=0)]

    @model_validator(mode="after")
    def ordered(self) -> CutSegment:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("Trim end must follow start")
        return self


class CutRequest(Update):
    segments: Annotated[list[CutSegment], Field(min_length=1, max_length=1000)]
    sound_inputs: list[ObjectRef] = Field(default_factory=list, max_length=128)
    intent: Text


class RenderCutRequest(Update):
    cut: ObjectRef


class DecisionRequest(Update):
    target: ObjectRef
    purpose: Literal["final", "envelope", "take", "shot-plan"]
    rationale: Text
    shot: ObjectRef | None = None
    takes: Annotated[list[ObjectRef], Field(min_length=1, max_length=16)] | None = None
    # A proposed absolute envelope, not permission or a current balance mutation.
    proposed_limit: Annotated[int, Field(ge=0)] | None = None
    budget_unit: Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")] | None = None
    budget_key: BudgetKey | None = None

    @model_validator(mode="after")
    def concrete_decision(self) -> DecisionRequest:
        if self.purpose == "take":
            if self.shot is None or self.takes is None:
                raise ValueError("Take decisions require a shot and offered takes")
            if self.target != self.shot:
                raise ValueError("Take decision target must be the exact shot")
            refs = [(ref.object_id, ref.revision, ref.digest) for ref in self.takes]
            if len(refs) != len(set(refs)):
                raise ValueError("Offered takes must be distinct")
        elif self.model_fields_set & {"shot", "takes"}:
            raise ValueError("Only take decisions can contain shot and takes")
        if self.purpose == "envelope":
            if self.proposed_limit is None or self.budget_unit is None:
                raise ValueError("Envelope decisions require a proposed limit and unit")
        elif self.model_fields_set & {"proposed_limit", "budget_unit", "budget_key"}:
            raise ValueError("Only envelope decisions can contain budget fields")
        return self


class HumanDecision(Mutation):
    """Only the independently authenticated human endpoint may accept this body."""
    request_id: Identifier
    target_hash: Digest
    # "undo" takes back a 再拍一批 / 都不行 answer while the card is unchanged.
    choice: Literal["confirm", "decline", "undo"]
    csrf_token: Annotated[str, StringConstraints(min_length=16, max_length=256)]
    selected_take: ObjectRef | None = None
    reason: Annotated[str, StringConstraints(max_length=2000)] | None = None


class FeedbackRequest(Update):
    target: ObjectRef
    text: Text
    playback_seconds: Annotated[float, Field(ge=0)] | None = None


class AuthorReport(Update):
    target: ObjectRef
    observation: Text
    evidence: list[ObjectRef] = Field(default_factory=list, max_length=128)


class SelectTakeRequest(Update):
    shot: ObjectRef
    take: ObjectRef
    rationale: Text


class PatchRequest(Update):
    source_pickup: ObjectRef | None = None
    target: ObjectRef
    creative_path: Annotated[list[str], Field(min_length=1, max_length=16)]
    value: JsonValue
    reason: Text

    @field_validator("source_pickup")
    @classmethod
    def exact_pickup(cls, value: ObjectRef | None) -> ObjectRef | None:
        if value is not None and value.digest is None:
            raise ValueError("Source pickup requires an exact immutable fingerprint")
        return value

    @field_validator("creative_path")
    @classmethod
    def creative_only(cls, value: list[str]) -> list[str]:
        if value[0] != "content" or any(not x or len(x) > 256 for x in value):
            raise ValueError("Patches are scoped to creative content only")
        return value


class BatchRequest(Update):
    candidate_ids: Annotated[list[Identifier], Field(min_length=1, max_length=100)]

    @field_validator("candidate_ids")
    @classmethod
    def per_version_cap(cls, value: list[str]) -> list[str]:
        if any(count > 4 for count in Counter(value).values()):
            raise ValueError("Each candidate version is limited to 4 takes per batch request")
        return value


class ProjectObject(Contract):
    project_id: Identifier
    revision: Revision
    title: str
    branch: Branch
    release_hash: Digest


class ArtifactObject(Contract):
    artifact_id: Identifier
    project_id: Identifier
    revision: Revision
    kind: ArtifactKind
    logical_path: str
    content: dict[str, JsonValue] | str
    digest: Digest
    author_id: Identifier
    dependencies: list[ObjectRef]


class CandidateObject(Contract):
    candidate_id: Identifier
    project_id: Identifier
    revision: Revision
    target: ObjectRef
    task: TaskKind
    method_id: Identifier
    release_hash: Digest
    digest: Digest
    inputs: list[ObjectRef]
    # Read-only actual submission data; never a SubmitRequest input.
    request: dict[str, JsonValue]


class JobObject(Contract):
    job_id: Identifier
    project_id: Identifier
    candidate_id: Identifier | None = None
    state: JobState
    current: bool
    result: ObjectRef | None = None
    error_code: str | None = None


class QueryRequest(Contract):
    project_id: Identifier | None = None
    object_id: Identifier | None = None


OPERATIONS: dict[str, type[Mutation]] = {
    "create-project": ProjectCreate, "revise-artifact": ArtifactRevision,
    "upload": UploadRequest, "select-method": MethodSelection,
    "prepare": PrepareRequest, "submit": SubmitRequest, "observe": ObserveRequest,
    "create-cut": CutRequest, "render-cut": RenderCutRequest,
    "request-decision": DecisionRequest, "feedback": FeedbackRequest,
    "report": AuthorReport, "select-take": SelectTakeRequest,
    "patch": PatchRequest, "batch": BatchRequest,
}  # no AI review, qualification, composition or lesson operations


def parse_operation(operation: str, body: Any) -> Mutation:
    model = OPERATIONS.get(operation)
    if model is None:
        raise DomainError("invalid_input", "Unknown author operation", field="operation")
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_context=False, include_url=False)
        first = errors[0]
        field = ".".join(str(part) for part in first["loc"])
        raise DomainError("invalid_input", "Request does not match the operation schema", field=field,
                          repair="Read discovery for the required fields and types") from None


def discovery() -> dict[str, Any]:
    return {"version": 1, "operations": {
        name: {"mutation": True, "synchronous_paid_work": False,
               "schema": model.model_json_schema()}
        for name, model in OPERATIONS.items()
    }, "queries": {name: {"mutation": False, "paid_work": False,
                           "schema": QueryRequest.model_json_schema()}
                   for name in ("projects", "context", "artifacts", "media", "candidates", "jobs", "reviews", "cuts", "methods")}}
