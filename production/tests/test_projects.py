"""Versioned creative content cannot rewrite service identity or authority."""
import hashlib
import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

from PIL import Image

from production.auth import AuthService
from production.contracts import (
    ArtifactRevision,
    DomainError,
    MethodSelection,
    ObjectRef,
    ProjectCreate,
    UploadRequest,
)
from production.media import MediaStore
from production.projects import Projects
from production.store import Store
from production.workflow import Workflow


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state.sqlite")
        self.auth = AuthService(self.store, "https://studio.example")
        self.media = MediaStore(self.store, Path(self.tmp.name) / "media")
        self.service = Projects(self.store, self.auth, self.media, label="lean-v1")
        self.token = self.auth.provision_token("author_1", "agent", [], 300, allow_create_project=True)
        self.actor = self.auth.authenticate(self.token)
        self.created = self.service.create(self.actor, ProjectCreate(idempotency_key="create-1", title="The Last Bowl", branch="original"))
        self.pid = self.created["object_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, kind, path, content, key="draft-1", revision=0, dependencies=None):
        return ArtifactRevision(idempotency_key=key, expected_revision=revision, kind=kind, logical_path=path, content=content, dependencies=dependencies or [])

    def ref(self, item):
        return {"object_id": item["object_id"], "revision": item["revision"], "digest": item["digest"]}

    def test_creation_stamps_the_config_label_and_replays(self):
        result = self.service.create(self.actor, ProjectCreate(idempotency_key="create-1", title="The Last Bowl", branch="original"))
        self.assertEqual(result, self.created)
        self.assertEqual(result["body"]["release_id"], "lean-v1")  # a label only
        self.assertNotIn("release_hash", result["body"])
        self.assertEqual(result["body"]["branch"], "original")
        self.assertEqual(self.auth.visible_projects(self.actor), [self.pid])
        self.assertNotIn("source", result["body"])

    def test_new_project_is_funded_once_without_an_approval(self):
        # default envelopes at creation; a replay does not set them again.
        funded = Projects(self.store, self.auth, self.media, label="lean-v1",
                          default_envelopes=(("fal_owner", "usd_micro", 200_000_000), ("apilio_owner_10466", "apilio_quota", 5_000_000)))
        project = funded.create(self.actor, ProjectCreate(idempotency_key="create-funded", title="Dream Company test", branch="original"))
        pid = project["object_id"]
        self.assertEqual(self.store.budget(pid, budget_key="fal_owner")["ceiling"], 200_000_000)
        self.assertEqual(self.store.budget(pid, budget_key="apilio_owner_10466")["unit"], "apilio_quota")
        changed = lambda: [e for e in self.store.events(pid) if e["kind"] == "budget.changed"]
        self.assertEqual(len(changed()), 2)
        again = funded.create(self.actor, ProjectCreate(idempotency_key="create-funded", title="Dream Company test", branch="original"))
        self.assertEqual(again["object_id"], pid)
        self.assertEqual(len(changed()), 2)

    def test_card_roundtrip_tree_and_revision_identity(self):
        card = json.loads(Path("production/templates/card.json").read_text())
        request = self.request("shot", "EP01/S01/S01-010/card.json", card)
        first = self.service.revise(self.actor, self.pid, request)
        self.assertEqual(first["body"]["content"], card)
        self.assertEqual(first, self.service.revise(self.actor, self.pid, request))
        card["Direction"]["quality"] = "approved_by: fictional director"
        changed = self.service.revise(self.actor, self.pid, self.request("shot", request.logical_path, card, "change-1", 1), artifact_id=first["object_id"])
        self.assertEqual(changed["object_id"], first["object_id"])
        self.assertEqual(changed["revision"], 2)
        self.assertEqual(changed["author"], "author_1")
        self.assertNotIn("approved_by", changed)
        self.assertEqual(self.service.tree(self.actor, self.pid)[0]["parent_path"], "EP01/S01/S01-010")
        with self.assertRaises(DomainError):
            self.service.revise(self.actor, self.pid, self.request("shot", request.logical_path, {}, "stale", 1), artifact_id=first["object_id"])

    def test_path_kind_and_dependency_guards(self):
        scene = self.service.revise(self.actor, self.pid, self.request("scene", "EP01/S01/scene.md", "## GEO\nA wall."))
        with self.assertRaises(DomainError):
            self.service.revise(self.actor, self.pid, self.request("shot", "EP01/S01/scene.md", {}, "collision"))
        with self.assertRaises(DomainError):
            self.service.revise(self.actor, self.pid, self.request("shot", "EP01/S01/scene.md", {}, "change-kind", 1), artifact_id=scene["object_id"])
        for dep in ({**self.ref(scene), "digest": "f"*64}, {"object_id": "missing", "revision": 1}):
            with self.assertRaises(DomainError):
                self.service.revise(self.actor, self.pid, self.request("shot", "EP01/S01/card.json", {}, "bad-dep", dependencies=[dep]))
        good = self.service.revise(self.actor, self.pid, self.request("shot", "EP01/S01/card.json", {}, "good-dep", dependencies=[self.ref(scene)]))
        self.assertEqual(good["body"]["dependencies"], [self.ref(scene)])

    def test_draft_state_does_not_change_selected_voice(self):
        visual = self.service.revise(self.actor, self.pid, self.request("asset", "assets/hero.json", {"type": "asset", "role": "visual", "tag": "@hero", "definition": "An elderly cook"}))
        voice = self.service.revise(self.actor, self.pid, self.request("asset", "assets/voice.json", {"type": "asset", "role": "voice", "tag": "@hero_voice", "definition": "Warm low voice"}, "voice"))
        selection_content = {"type": "asset-selection", "target": self.ref(visual), "selected": {"visual": self.ref(visual), "voice": self.ref(voice)}}
        selection = self.service.revise(self.actor, self.pid, self.request("asset", "selections/hero.json", selection_content, "select"))
        wet = self.service.revise(self.actor, self.pid, self.request("asset", "assets/hero-wet.json", {"type": "asset", "role": "state", "tag": "@hero_wet", "base_identity": self.ref(visual), "definition": "Wet costume"}, "wet"))
        self.assertEqual(wet["body"]["content"]["role"], "state")
        self.assertEqual(self.service.get(self.actor, self.pid, selection["object_id"])["body"]["content"], selection_content)
        bad = {**selection_content, "selected": {"voice": self.ref(wet)}}
        with self.assertRaises(DomainError):
            self.service.revise(self.actor, self.pid, self.request("asset", "selections/bad.json", bad, "badselection"))

    def test_asset_carries_an_optional_hf_element_category(self):
        # character / environment / prop, as HF elements.
        hero = self.service.revise(self.actor, self.pid, self.request("asset", "assets/hero.json", {"type": "asset", "role": "visual",
            "tag": "@hero", "definition": "An elderly cook", "category": "character"}))
        self.assertEqual(hero["body"]["content"]["category"], "character")
        old = self.service.revise(self.actor, self.pid, self.request("asset", "assets/cup.json", {"type": "asset", "role": "visual",
            "tag": "@cup", "definition": "A chipped cup"}, "cup"))
        self.assertNotIn("category", old["body"]["content"])
        with self.assertRaises(DomainError):
            self.service.revise(self.actor, self.pid, self.request("asset", "assets/bad.json", {"type": "asset", "role": "visual",
                "tag": "@bad", "definition": "?", "category": "creature"}, "bad"))

    def test_source_range_and_branch_are_distinct_from_method(self):
        media = self.store.create_object(self.pid, "media", {"probe": {"duration": 3.0, "has_video": True}, "media_type": "video/mp4"}, "fixture")
        content = {"type": "source-understanding", "source": self.ref(media), "start_seconds": 0.5, "end_seconds": 2.5, "observed_facts": ["Cook offers bowl"], "uncertain_interpretations": ["Recipient may be hungry"], "adaptation_scope": "Keep action, change visual style"}
        source = self.service.revise(self.actor, self.pid, self.request("source-understanding", "source.json", content))
        self.assertEqual(source["body"]["content"]["end_seconds"], 2.5)
        with self.assertRaises(DomainError):
            self.service.revise(self.actor, self.pid, self.request("source-understanding", "invalid-source.json", {**content, "end_seconds": 4.0}, "invalid"))
        target = self.service.get(self.actor, self.pid, self.pid)
        selected = self.service.select_method(self.actor, self.pid, MethodSelection(idempotency_key="method", expected_revision=target["revision"], method_id="cully", rationale="Single dialogue shot", target=self.ref(target)))
        self.assertEqual(selected["body"]["content"]["rationale"], "Single dialogue shot")
        self.assertEqual(self.service.get(self.actor, self.pid, self.pid)["body"]["branch"], "original")

    def test_a_source_understanding_may_cite_the_readers_observation_of_that_clip(self):

        media = self.store.create_object(self.pid, "media", {"probe": {"duration": 3.0, "has_video": True}, "media_type": "video/mp4"}, "fixture")
        other = self.store.create_object(self.pid, "media", {"probe": {"duration": 3.0, "has_video": True}, "media_type": "video/mp4"}, "fixture")
        seen = lambda src, author="reader_service", status="succeeded": self.store.create_object(self.pid, "observation",
            {"source": self.ref(src), "status": status, "observations": ["a"], "dependencies": [self.ref(src)]}, author)
        content = {"type": "source-understanding", "source": self.ref(media), "start_seconds": 0.5, "end_seconds": 2.5,
                   "observed_facts": ["Cook offers bowl"], "uncertain_interpretations": [], "adaptation_scope": "Keep action"}
        good = self.service.revise(self.actor, self.pid, self.request("source-understanding", "s1.json",
                                   {**content, "observation": self.ref(seen(media))}, "s1"))
        self.assertIn(good["body"]["content"]["observation"], good["body"]["dependencies"])
        for n, bad in enumerate((seen(other), seen(media, author="author"), seen(media, status="failed"))):
            with self.subTest(n=n), self.assertRaises(DomainError):
                self.service.revise(self.actor, self.pid, self.request("source-understanding", f"b{n}.json",
                                    {**content, "observation": self.ref(bad)}, f"b{n}"))

    def test_method_choice_concurrent_updates_guard_selection_revision(self):
        target = self.service.revise(self.actor, self.pid, self.request("scene", "scene.md", "Offer the bowl"))
        request = MethodSelection(idempotency_key="initial-method", expected_revision=target["revision"],
                                  method_id="cully", rationale="Dialogue", target=self.ref(target))
        first = self.service.select_method(self.actor, self.pid, request)
        barrier = threading.Barrier(2)
        def change(index):
            barrier.wait()
            try:
                result = self.service.select_method(self.actor, self.pid, MethodSelection(
                    idempotency_key=f"method-{index}", expected_revision=first["revision"],
                    method_id=f"choice_{index}", rationale="A considered alternative", target=self.ref(target)))
                return result["mutation_guard"]["expected_revision"]
            except DomainError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(change, [1, 2]))
        self.assertCountEqual(results, [2, "revision_conflict"])
        self.assertEqual(self.service.get(self.actor, self.pid, target["object_id"])["revision"], 1)

    def test_upload_permissions_checked_after_bytes_before_metadata(self):
        image = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(image, format="PNG")
        data = image.getvalue()
        request = UploadRequest(idempotency_key="upload", logical_path="assets/look.png", media_type="image/png", byte_length=len(data), sha256=hashlib.sha256(data).hexdigest())
        def revoke_midstream():
            yield data
            self.auth.revoke(self.actor.credential_id)
        with self.assertRaises(DomainError):
            self.service.upload(self.actor, self.pid, request, revoke_midstream())
        self.assertEqual(self.store.list_objects(self.pid, kind="media"), [])

    def test_successful_upload_replay_and_viewer_denied_before_reading_bytes(self):
        image = io.BytesIO()
        Image.new("RGB", (8, 8), "blue").save(image, format="PNG")
        data = image.getvalue()
        request = UploadRequest(idempotency_key="upload-ok", logical_path="uploads/look.png", media_type="image/png",
                                byte_length=len(data), sha256=hashlib.sha256(data).hexdigest())
        first = self.service.upload(self.actor, self.pid, request, [data])
        self.assertEqual(first, self.service.upload(self.actor, self.pid, request, [data]))
        self.assertEqual(len(self.store.list_objects(self.pid, kind="media")), 1)
        viewer = self.auth.authenticate(self.auth.provision_token("viewer_1", "viewer", [self.pid], 300))
        def forbidden_stream():
            self.fail("Denied upload consumed bytes")
            yield b"unreachable"
        with self.assertRaises(DomainError):
            self.service.upload(viewer, self.pid, request, forbidden_stream())

    def test_cross_project_dependencies_and_voice_identity_are_not_fungible(self):
        other_project = self.store.create_project("another_project", {}, "other")
        foreign = self.store.create_object(other_project["object_id"], "scene", {}, "other")
        with self.assertRaises(DomainError):
            self.service.revise(self.actor, self.pid, self.request("shot", "foreign/card.json", {}, dependencies=[self.ref(foreign)]))
        visual = self.service.revise(self.actor, self.pid, self.request("asset", "assets/visual.json",
            {"type": "asset", "role": "visual", "tag": "@cook", "definition": "Cook"}, "visual"))
        with self.assertRaises(DomainError):
            self.service.revise(self.actor, self.pid, self.request("asset", "assets/delivery.json",
                {"type": "asset", "role": "delivery", "tag": "@cook_line", "definition": "Urgent whisper", "base_identity": self.ref(visual)}, "delivery"))
        with self.assertRaises(DomainError):
            self.service.revise(self.actor, self.pid, self.request("asset", "assets/visual.json",
                {"type": "asset", "role": "voice", "tag": "@cook", "definition": "Voice"}, "retag", 1), artifact_id=visual["object_id"])

    def test_finishing_upload_pins_service_cut_and_stales_when_cut_changes(self):
        cut = self.store.create_object(self.pid, 'cut', {'dependencies': [], 'label': 'rough-cut'}, 'cut_service')
        image = io.BytesIO()
        Image.new('RGB', (8, 8), 'blue').save(image, format='PNG')
        data = image.getvalue()
        request = UploadRequest(idempotency_key='finishing-upload', logical_path='finishing/reference.png',
            media_type='image/png', byte_length=len(data), sha256=hashlib.sha256(data).hexdigest(),
            source_cut=ObjectRef(**self.ref(cut)))
        uploaded = self.service.upload(self.actor, self.pid, request, [data])
        self.assertEqual(uploaded['body']['source_cut'], self.ref(cut))
        self.assertEqual(uploaded['body']['dependencies'], [self.ref(cut)])
        flow = Workflow(self.store, self.auth, cast(Any, None))
        graph = flow.pinned_graph(self.pid, ObjectRef(**self.ref(uploaded)))
        self.assertFalse(graph['stale'])
        self.assertEqual([n['object'] for n in graph['nodes'] if n['object']['kind'] == 'cut'], [cut])
        self.store.append_revision(self.pid, cut['object_id'], 1, {**cut['body'], 'label': 'revised-cut'}, 'cut_service')
        self.assertTrue(flow.pinned_graph(self.pid, ObjectRef(**self.ref(uploaded)))['stale'])
        # Replay keeps the historical binding, never silently targets the new cut.
        self.assertEqual(self.service.upload(self.actor, self.pid, request, [data]), uploaded)

    def test_finishing_upload_rejects_author_cut_and_wrong_fingerprint(self):
        cut = self.store.create_object(self.pid, 'cut', {'dependencies': []}, 'cut_service')
        forged = self.store.create_object(self.pid, 'cut', {'dependencies': []}, 'author_1')
        data = b'Finishing notes are not a completed film.'
        for index, ref in enumerate([self.ref(forged), {**self.ref(cut), 'digest': 'f' * 64}]):
            request = UploadRequest(idempotency_key=f'invalid-finishing-{index}', logical_path='finishing/notes.txt',
                media_type='text/plain', byte_length=len(data), sha256=hashlib.sha256(data).hexdigest(),
                source_cut=ObjectRef(**ref))
            with self.assertRaises(DomainError):
                self.service.upload(self.actor, self.pid, request, [data])
        self.assertEqual(self.store.list_objects(self.pid, kind='media'), [])

    def test_ordinary_revision_cannot_bypass_service_picture_lock(self):
        scene = self.service.revise(self.actor, self.pid, self.request('scene', 'scene.md', 'Locked scene'))
        self.store.create_object(self.pid, 'picture-lock', {
            'state': 'locked', 'protected': [self.ref(scene)], 'reopened_targets': [],
        }, 'workflow_service')
        with self.assertRaises(DomainError) as error:
            self.service.revise(self.actor, self.pid,
                self.request('scene', 'scene.md', 'Unpermitted change', 'locked-write', 1),
                artifact_id=scene['object_id'])
        self.assertEqual(error.exception.code, 'locked')
        self.assertEqual(self.service.get(self.actor, self.pid, scene['object_id'])['revision'], 1)

    def test_cross_project_no_read_write(self):
        other = self.auth.authenticate(self.auth.provision_token("other", "agent", [], 300))
        with self.assertRaises(DomainError):
            self.service.get(other, self.pid, self.pid)
        with self.assertRaises(DomainError):
            self.service.revise(other, self.pid, self.request("scene", "scene.md", "Attempt"))


if __name__ == "__main__":
    unittest.main()
