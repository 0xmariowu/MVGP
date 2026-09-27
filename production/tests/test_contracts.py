"""Author boundary tests: creative data must not confer service authority."""
import json
import unittest
from pathlib import Path

from pydantic import ValidationError

from production.contracts import (
    ArtifactRevision,
    BatchRequest,
    CutSegment,
    DecisionRequest,
    DomainError,
    ObserveRequest,
    PatchRequest,
    PrepareRequest,
    ProjectCreate,
    SubmitRequest,
    canonical_json,
    content_hash,
    discovery,
    new_id,
    parse_operation,
    safe_logical_path,
)


class ContractTests(unittest.TestCase):
    def test_budget_identity_is_a_decision_field_not_paid_dispatch_authority(self):
        payload = {'idempotency_key':'budget', 'expected_revision':1,
            'target':{'object_id':'project','revision':1}, 'purpose':'envelope',
            'rationale':'Separate native provider allowance', 'proposed_limit':20,
            'budget_unit':'credit', 'budget_key':'hf_primary'}
        self.assertEqual(DecisionRequest(**payload).budget_key, 'hf_primary')
        for invalid in ('', 'Upper', '../hf', 'a'*65):
            with self.assertRaises(ValidationError):
                DecisionRequest(**{**payload, 'budget_key':invalid})
        with self.assertRaises(ValidationError):
            DecisionRequest(**{k:v for k,v in payload.items() if k not in ('proposed_limit','budget_unit','purpose')}, purpose='final')
        with self.assertRaises(ValidationError):
            SubmitRequest(idempotency_key='submit', expected_revision=1, candidate_id='candidate', budget_key='hf_primary')

    def test_human_decision_choices_include_undo_and_nothing_else(self):

        from production.contracts import HumanDecision
        base = {'idempotency_key': 'k1', 'request_id': 'obj_1', 'target_hash': 'a' * 64, 'csrf_token': 'c' * 16}
        for choice in ('confirm', 'decline', 'undo'):
            self.assertEqual(HumanDecision.model_validate({**base, 'choice': choice}).choice, choice)
        with self.assertRaises(ValidationError):
            HumanDecision.model_validate({**base, 'choice': 'reset'})

    def test_submit_accepts_only_frozen_candidate(self):
        valid = {"idempotency_key": "try-1", "expected_revision": 1, "candidate_id": "candidate_123"}
        self.assertEqual(SubmitRequest(**valid).candidate_id, "candidate_123")
        for key in ("prompt", "parameters", "references", "actor", "approved_by", "force", "pass"):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                SubmitRequest(**valid, **{key: "forged"})

    def test_revisions_are_strict(self):
        for revision in (-1, 0, True, "1", 1.1):
            with self.subTest(revision=revision), self.assertRaises(ValidationError):
                SubmitRequest(idempotency_key="try-1", expected_revision=revision, candidate_id="candidate_123")
        with self.assertRaises(ValidationError):
            ProjectCreate(idempotency_key="create-1", expected_revision=1, title="Film", branch="original")

    def test_branches_are_not_transport_modes(self):
        for branch in ("original", "recreation"):
            self.assertEqual(ProjectCreate(idempotency_key="create-1", title="Film", branch=branch).branch, branch)
        with self.assertRaises(ValidationError):
            ProjectCreate(idempotency_key="create-1", title="Film", branch="replica")

    def test_preserves_existing_nested_creative_semantics(self):
        card = json.loads(Path("production/templates/card.json").read_text())
        result = ArtifactRevision(idempotency_key="card-1", expected_revision=0, kind="shot", logical_path="EP01/S01/card.json", content=card)
        self.assertEqual(result.content, card)
        # A creative quotation of approval is data, never a trusted metadata field.
        card["Direction"]["quality"] = "approved_by: fictional producer"
        self.assertEqual(ArtifactRevision(**{**result.model_dump(), "content": card}).content, card)
        with self.assertRaises(ValidationError):
            ArtifactRevision(**result.model_dump(), approved_by="operator")

    def test_logical_paths_never_accept_host_paths(self):
        self.assertEqual(safe_logical_path("EP01/镜头/card.json"), "EP01/镜头/card.json")
        for value in ("/etc/passwd", "../x", "a/../b", "a//b", "a/./b", "C:\\x", "a\\b", "a\x00b", "a/%2e%2e/b", "https://example.com/x"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                safe_logical_path(value)

    def test_canonical_hash_binds_actual_payload(self):
        self.assertEqual(content_hash({"b": 1, "a": [2]}), content_hash({"a": [2], "b": 1}))
        self.assertNotEqual(content_hash({"a": 1}), content_hash({"a": True}))
        self.assertEqual(canonical_json({"x": "马"}), '{"x":"马"}')
        with self.assertRaises(ValueError):
            canonical_json({"x": float("nan")})
        self.assertNotEqual(new_id("project"), new_id("project"))
        for invalid in ({1: "coercion"}, {"nested": {1: "coercion"}}, {"tuple": (1, 2)}):
            with self.assertRaises(ValueError):
                canonical_json(invalid)

    def test_observation_is_queued_and_cannot_select_judge(self):
        body = {"idempotency_key": "read-1", "expected_revision": 1, "media_id": "media_1", "questions": ["Who moves first?"], "reader": "video"}
        self.assertEqual(ObserveRequest(**body).reader, "video")
        for key in ("model", "endpoint", "synchronous", "verdict", "actor"):
            with self.assertRaises(ValidationError):
                ObserveRequest(**body, **{key: "forged"})

    def test_registry_exposes_author_operations_not_service_receipts(self):
        info = discovery()
        names = info["operations"]
        for name in ("create-project", "revise-artifact", "upload", "prepare", "submit", "observe", "render-cut", "request-decision", "select-method", "patch", "batch"):
            self.assertIn(name, names)
            self.assertTrue(names[name]["mutation"])
            self.assertFalse(names[name]["synchronous_paid_work"])
        for name in ("review-candidate", "review-media", "propose-lesson", "compose-asset", "qualify-asset"):
            self.assertNotIn(name, names)
        with self.assertRaises(DomainError) as error:
            parse_operation("record-review", {})
        self.assertEqual(error.exception.code, "invalid_input")
        with self.assertRaises(DomainError):
            parse_operation("submit", {"force": True})

    def test_nested_refs_cannot_forge_authority(self):
        body = {"idempotency_key": "prep-1", "expected_revision": 1, "task": "shot",
                "target": {"object_id": "shot_1", "revision": 1, "actor": "operator"},
                "method_selection": {"object_id": "method_1", "revision": 1}}
        with self.assertRaises(ValidationError):
            PrepareRequest(**body)
        del body["target"]["actor"]
        body["mask"] = {"object_id": "mask_1", "revision": 1}
        with self.assertRaises(ValidationError):
            PrepareRequest(**body)
        body["task"] = "image-edit"
        self.assertIsNotNone(PrepareRequest(**body).mask)

    def test_patch_scope_and_batch_duplication(self):
        body = {"idempotency_key": "patch-1", "expected_revision": 1,
                "target": {"object_id": "shot_1", "revision": 1},
                "creative_path": ["author_id"], "value": "operator", "reason": "wrong"}
        with self.assertRaises(ValidationError):
            PatchRequest(**body)
        body["creative_path"] = ["content", "Direction", "expected visible performance"]
        self.assertEqual(PatchRequest(**body).creative_path[0], "content")
        candidate_ids = ["candidate_1", "candidate_1"]
        request = BatchRequest(idempotency_key="batch-1", expected_revision=1, candidate_ids=candidate_ids)
        self.assertEqual(request.candidate_ids, candidate_ids)
        with self.assertRaisesRegex(ValidationError, "4"):
            BatchRequest(idempotency_key="batch-1", expected_revision=1, candidate_ids=["candidate_1"] * 5)

    def test_patch_source_pickup_is_optional_exact_reference_not_author_verdict(self):
        body = {"idempotency_key": "patch", "expected_revision": 1,
                "target": {"object_id": "shot", "revision": 1},
                "creative_path": ["content"], "value": {}, "reason": "Wrong position"}
        self.assertIsNone(PatchRequest(**body).source_pickup)
        ref = {"object_id": "pickup", "revision": 1, "digest": "a" * 64}
        self.assertEqual(PatchRequest(**body, source_pickup=ref).source_pickup.digest, "a" * 64)
        for invalid in ({"object_id": "pickup", "revision": 1}, {**ref, "verdict": "pass"}, "pickup"):
            with self.assertRaises(ValidationError):
                PatchRequest(**body, source_pickup=invalid)

    def test_cut_segment_geometry(self):
        ref = {"object_id": "take_1", "revision": 1}
        for start, end in ((2.0, 1.0), (1.0, 1.0), (-1.0, 1.0), (0.0, float("inf"))):
            with self.assertRaises(ValidationError):
                CutSegment(take=ref, start_seconds=start, end_seconds=end)

    def test_shot_plan_request_targets_a_scene_and_carries_no_takes_or_budget(self):
        # the owner approves each scene's shot plan before shooting.
        scene = {"object_id": "obj_scene", "revision": 2, "digest": "a" * 64}
        base = {"idempotency_key": "plan-1", "expected_revision": 2, "target": scene,
                "purpose": "shot-plan", "rationale": "Please look at the shot plan before shooting."}
        self.assertEqual(DecisionRequest(**base).purpose, "shot-plan")
        take = {"object_id": "obj_take", "revision": 1, "digest": "b" * 64}
        for fields in ({"shot": scene, "takes": [take]}, {"proposed_limit": 1, "budget_unit": "USD-micros"}):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                DecisionRequest(**base, **fields)
        with self.assertRaises(ValidationError):
            DecisionRequest(**{**base, "purpose": "shotplan"})

    def test_envelope_request_is_concrete_and_final_has_no_budget(self):
        base = {"idempotency_key": "decision-1", "expected_revision": 1,
                "target": {"object_id": "project_1", "revision": 1},
                "purpose": "envelope", "rationale": "Three additional takes"}
        for fields in ({}, {"proposed_limit": 100}, {"budget_unit": "USD-micros"},
                       {"proposed_limit": -1, "budget_unit": "USD-micros"},
                       {"proposed_limit": True, "budget_unit": "USD-micros"}):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                DecisionRequest(**base, **fields)
        envelope = DecisionRequest(**base, proposed_limit=0, budget_unit="USD-micros")
        self.assertEqual(envelope.proposed_limit, 0)
        base["purpose"] = "final"
        self.assertIsNone(DecisionRequest(**base).proposed_limit)
        for fields in ({"proposed_limit": 100}, {"budget_unit": "USD-micros"},
                       {"proposed_limit": None, "budget_unit": None}):
            with self.assertRaises(ValidationError):
                DecisionRequest(**base, **fields)

    def test_error_has_stable_nullable_details(self):
        error = DomainError("stale_input", "Asset changed", repair="Prepare again")
        self.assertEqual(error.as_dict()["code"], "stale_input")
        self.assertIsNone(error.as_dict()["source"])
        self.assertEqual(error.as_dict()["repair"], "Prepare again")
