"""Image compilation uses exact local bytes and released capability snapshots."""
import copy
import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from production.asset_methods import AssetMethods, definition_schema
from production.auth import AuthService
from production.contracts import DomainError, ObjectRef
from production.media import MediaStore
from production.store import Store
from production.tests.fixtures import hf_era_document, runtime_config
from production.workflow import Workflow


class AssetMethodTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "state.sqlite")
        self.auth = AuthService(self.store, "https://studio.example")
        self.media = MediaStore(self.store, self.root / "media")
        methods = {mid: {"status":{"documented":True,"adopted":True}, "required_capabilities":["compile"], "task":task, "modules":{"visual":["photoreal","project-animation"]}, "sources":[]}
                   for mid,task in [("generate", "image"),("edit", "image-edit")]}
        self.capability = {"job_type":"nano_banana_pro", "type":"image", "params":[{"name":"prompt"},{"name":"image_references"},{"name":"resolution","enum":["1k","2k","4k"]},{"name":"aspect_ratio","enum":["1:1","16:9"]}], "rules":[{"cel":"size(params.image_references) <= 14"}]}
        self.profile = {"job_type":"nano_banana_pro", "tasks":["image","image-edit"], "aspect_ratios":["1:1","16:9"], "resolutions":["1k","2k","4k"], "defaults":{"aspect_ratio":"16:9","resolution":"2k"}, "max_references":14,"capability_role":"image_capability","reference_edit_verified":True,"reference_edit_evidence_role":"edit_evidence"}
        self.profile["output_contract"] = hf_era_document("image_routes")["profiles"]["hf-nbp-image-v1"]["output_contract"]
        self.routes = {"profiles":{"nbp":self.profile}, "method_routes":{"generate":"nbp","edit":"nbp"}, "method_recipes":{mid:["base-portrait","state-variant","location-angle","pose","diagram"] for mid in methods}}
        self.config = runtime_config({"image_routes": self.routes, "image_capability": self.capability,
            "edit_evidence": {"observed": "Synthetic fixture only: reference-edit capability attestation; no live quality claim."},
            "review_routes": {"role_routes": {r: r for r in ["standards", "director", "observer"]},
                              "profiles": {r: {"role": r, "author_overrides": []} for r in ["standards", "director", "observer"]}}},
            methods=methods)
        self.store.create_project("project_1", {"release_id":self.config.label,"branch":"original"}, "author")
        self.flow = Workflow(self.store, self.auth, self.config)
        self.compiler = AssetMethods(self.store,self.media,self.flow,route_profiles={"nbp":self.profile})

    def tearDown(self):
        self.tmp.cleanup()

    def ref(self,obj):
        return ObjectRef(object_id=obj["object_id"],revision=obj["revision"],digest=obj["digest"])

    def image(self,color="red"):
        data = io.BytesIO()
        Image.new("RGB", (8,8), color).save(data, format="PNG")
        return self.media.put("project_1",[data.getvalue()],"image/png","author")

    def draft(self, definition, *, role="visual", media_refs=(), base_identity=None, deps=()):
        content = {"type":"asset","role":role,"tag":"@cook","definition":definition,"media_refs":[self.ref(x).model_dump() for x in media_refs]}
        if base_identity:
            content["base_identity"] = self.ref(base_identity).model_dump()
        return self.store.create_object("project_1","asset",{"content":content,"dependencies":[self.ref(x).model_dump() for x in [*deps,*media_refs,*([base_identity] if base_identity else [])]]},"author")

    def choice(self,target,task="image"):
        return self.store.create_object("project_1","method",{"content":{"method_id":"generate" if task=="image" else "edit","target":self.ref(target).model_dump(),"rationale":"Fixture"},"dependencies":[self.ref(target).model_dump()]},"author")

    def definition(self, **changes):
        # an image writer names the manuals version it wrote with and what the version changes.
        from production import playbook
        return {"recipe":"base-portrait","description":"An expressive cook with readable features and warm painted light.","visual_treatment":"project-animation",
                "playbook_version": playbook.version(), "change_note": "First version of this image.", **changes}

    def compile(self, target, task="image", **kwargs):
        return self.compiler.compile("project_1",self.ref(target),self.ref(self.choice(target,task)),task,**kwargs)

    def test_the_descriptor_is_for_shots_and_never_enters_the_image_prompt(self):
        # found the image prompt ("Three studio photographs…") would have been pasted into every shot prompt.
        target = self.draft(self.definition(descriptor="A cook in a white apron with flour on his forearms."))
        prompt = self.compile(target)["prompt"]
        self.assertIn("An expressive cook", prompt)
        self.assertNotIn("flour on his forearms", prompt)

    def test_animation_initial_probe_has_no_photo_or_video_filler(self):
        target = self.draft(self.definition())
        result = self.compile(target)
        self.assertEqual(result["job_type"], "nano_banana_pro")
        self.assertEqual(result["parameters"], {"aspect_ratio":"16:9","resolution":"2k"})
        # the LIRA prose goes out as written, no platform labels.
        self.assertEqual(result["prompt"], self.definition()["description"])
        self.assertNotIn("Asset task:", result["prompt"])
        self.assertNotIn("ACTION TIMING", result["prompt"])
        self.assertEqual(result["references"], [])
        self.assertFalse(result["accepted"])
        self.assertTrue(definition_schema()["additionalProperties"] is False)
        self.assertEqual(self.store.list_objects("project_1",kind="job"), [])

    def test_edit_requires_change_preserve_correct_base_and_no_mask(self):
        base = self.image()
        identity = self.draft("Cook identity", media_refs=[base])
        definition = self.definition(recipe="state-variant",base=self.ref(base).model_dump(),change="Only the coat becomes wet.",preserve="Face, pose and composition.")
        target = self.draft(definition,role="state",base_identity=identity)
        result = self.compile(target,"image-edit")
        self.assertEqual(result["prompt"], definition["description"])  # the writer's LIRA text only
        self.assertEqual(result["references"][0]["role"], "edit-base")
        self.assertEqual(result["references"][0]["sha256"],base["body"]["sha256"])
        self.assertFalse(result["lineage"]["pixel_preservation_verified"])
        with self.assertRaises(DomainError):
            self.compile(target,"image-edit",mask=self.ref(base))
        missing = self.draft({k:v for k,v in definition.items() if k!="preserve"},role="state",base_identity=identity)
        self.assertTrue(self.compile(missing,"image-edit")["advice"])  # craft is advice; the base is still required
        no_base = self.draft({k:v for k,v in definition.items() if k!="base"},role="state",base_identity=identity)
        with self.assertRaises(DomainError):
            self.compile(no_base,"image-edit")
        wrong = self.image("blue")
        bad = self.draft({**definition,"base":self.ref(wrong).model_dump()},role="state",base_identity=identity)
        with self.assertRaises(DomainError):
            self.compile(bad,"image-edit")

    def test_selected_look_and_exact_reference_roles(self):
        look_image = self.image()
        look = self.draft({"visual_treatment":"project-animation","description":"Soft cel shadows, warm edge light."},role="look",media_refs=[look_image])
        target = self.draft(self.definition())
        selection = self.store.create_object("project_1","asset",{"content":{"type":"asset-selection","target":self.ref(target).model_dump(),"selected":{"look":self.ref(look).model_dump()}},"dependencies":[self.ref(look).model_dump()]},"author")
        result = self.compile(target,inputs=[self.ref(selection)])
        # the look travels as its reference image, not as pasted label text.
        self.assertNotIn("Soft cel shadows",result["prompt"])
        self.assertEqual(result["references"][0]["role"],"look")
        self.assertIn(look_image["object_id"],str(result["dependencies"]))
        incompatible = self.draft(self.definition(visual_treatment="photoreal"))
        with self.assertRaises(DomainError):
            self.compile(incompatible,inputs=[self.ref(selection)])

    def test_unrelated_selected_asset_scope_is_not_silently_reused(self):
        other = self.draft(self.definition())
        look = self.draft("Warm animation",role="look",media_refs=[self.image()])
        selection = self.store.create_object("project_1","asset",{"content":{"type":"asset-selection","target":self.ref(other).model_dump(),"selected":{"look":self.ref(look).model_dump()}},"dependencies":[self.ref(look).model_dump()]},"author")
        target = self.draft(self.definition())
        with self.assertRaises(DomainError):
            self.compile(target,inputs=[self.ref(selection)])

    def activate_profiles(self):
        self.config.set("image_routes", self.routes)
        self.config.set("image_capability", self.capability)
        self.compiler = AssetMethods(self.store,self.media,self.flow,route_profiles={"nbp":self.profile})


    def apilio_route(self):
        # the owner's chosen image channel.
        self.capability = {"job_type":"apilio_gpt_image_2_5", "type":"image", "rules":[], "params":[
            {"name":"prompt", "type":"string", "required":True}, {"name":"image_references", "type":"array"},
            {"name":"aspect_ratio", "type":"string", "enum":["16:9"]}, {"name":"resolution", "type":"string", "enum":["2k"]}],
            "models": {"2k": "gpt-image-2.5-sunburst-2k"}, "sizes": {"16:9|2k": "2048x1152"}}
        self.profile.update(job_type="apilio_gpt_image_2_5", max_references=4, aspect_ratios=["16:9"], resolutions=["2k"],
                            defaults={"aspect_ratio":"16:9","resolution":"2k"})
        self.activate_profiles()

    def test_apilio_route_sends_the_lira_prose_and_edit_lines(self):
        self.apilio_route()
        target = self.draft(self.definition())
        result = self.compile(target)
        self.assertEqual((result["job_type"], result["parameters"]), ("apilio_gpt_image_2_5", {"aspect_ratio":"16:9", "resolution":"2k"}))
        self.assertEqual(result["prompt"], self.definition()["description"])
        base = self.image()
        edit = self.draft(self.definition(recipe="state-variant", base=self.ref(base).model_dump(), change="Her coat is wet.",
                                          preserve="Face, hair and build."), media_refs=[base])
        edited = self.compile(edit, "image-edit")
        # the image prompt is the writer's LIRA text only; the platform adds no edit lines.
        self.assertEqual(edited["prompt"], self.definition()["description"])
        self.assertTrue(any("not written in the prompt" in a for a in edited["advice"]), edited["advice"])
        del self.capability["sizes"]
        self.activate_profiles()
        with self.assertRaises(DomainError):
            self.compile(self.draft(self.definition()))

    def test_nano_route_cannot_receive_gpt_only_fixed_parameters(self):
        self.profile["fixed_parameters"] = {"quality":"high", "variant":"sunburst"}
        self.activate_profiles()
        with self.assertRaises(DomainError):
            self.compile(self.draft(self.definition()))

    def test_unverified_reference_edit_and_count_bound_rejected(self):
        base = self.image()
        target = self.draft(self.definition(base=self.ref(base).model_dump(),change="Coat wet",preserve="Face"),media_refs=[base])
        self.profile["reference_edit_verified"] = False
        self.activate_profiles()
        with self.assertRaises(DomainError) as caught:
            self.compile(target,"image-edit")
        self.assertEqual(caught.exception.code,"unsupported_route")
        self.profile["max_references"] = 1
        self.activate_profiles()
        target = self.draft(self.definition(recipe="pose",reference_roles=["pose","identity"]))
        with self.assertRaises(DomainError):
            self.compile(target,inputs=[self.ref(base),self.ref(base)])

    def test_location_pose_diagram_and_frozen_selection_dependency_roots(self):
        for recipe in ("location-angle","pose","diagram"):
            target = self.draft(self.definition(recipe=recipe))
            self.assertEqual(recipe,self.compile(target)["lineage"]["recipe"])
        image = self.image()
        look = self.draft("Soft animation",role="look",media_refs=[image])
        target = self.draft(self.definition())
        selection = self.store.create_object("project_1","asset",{"content":{"type":"asset-selection","target":self.ref(target).model_dump(),"selected":{"look":self.ref(look).model_dump()}},"dependencies":[self.ref(look).model_dump()]},"author")
        self.assertIn("A project look is selected for this element but not given to the image", self.compile(target)["advice"])
        result = self.compile(target,inputs=[self.ref(selection)])
        candidate = self.store.create_object("project_1","candidate",{"dependencies":result["dependency_roots"],"request":result},"compiler")
        self.store.append_revision("project_1",look["object_id"],1,{"content":{"new":"alternative"},"dependencies":[]},"author")
        self.assertFalse(self.flow.pinned_graph("project_1",self.ref(candidate))["stale"])
        self.assertTrue(any(node["frozen_selection"] and node["object_ref"]["object_id"]==look["object_id"] for node in result["input_manifest"]))
        self.store.append_revision("project_1",selection["object_id"],1,selection["body"],"author")
        self.assertTrue(self.flow.pinned_graph("project_1",self.ref(candidate))["stale"])

    def test_image_output_contract_is_required_before_compile(self):
        original = copy.deepcopy(self.profile.get('output_contract'))
        for policy in (None, {'authority':'maker'}):
            if policy is None:
                self.profile.pop('output_contract', None)
            else:
                self.profile['output_contract'] = policy
            self.activate_profiles()
            with self.subTest(policy=policy), self.assertRaises(DomainError) as raised:
                self.compile(self.draft(self.definition()))
            self.assertEqual(raised.exception.code, 'unsupported_route')
        self.profile['output_contract'] = original

    def test_unknown_fields_raw_routes_and_invalid_parameters_rejected(self):
        for extra in ({"output_contract":{"authority":"maker"}},{"endpoint":"https://evil.example"},{"prompt":"Bypass assembly"},{"resolution":"1080p"},{"aspect_ratio":"100:1"}):
            target = self.draft(self.definition(**extra))
            with self.subTest(extra=extra), self.assertRaises(DomainError):
                self.compile(target)
        self.compiler.route_profiles["nbp"]["job_type"] = "other"
        with self.assertRaises(DomainError):
            self.compile(self.draft(self.definition()))

    def test_stale_wrong_project_and_changed_bytes_rejected(self):
        image = self.image()
        target = self.draft(self.definition(recipe="pose",reference_roles=["pose"]))
        choice = self.choice(target)
        self.store.append_revision("project_1",target["object_id"],1,{"content":"changed","dependencies":[]},"author")
        with self.assertRaises(DomainError):
            self.compiler.compile("project_1",self.ref(target),self.ref(choice),"image",inputs=[self.ref(image)])
        self.store.create_project("project_2",{},"other")
        foreign = self.store.create_object("project_2","media",{},"other")
        with self.assertRaises(DomainError):
            self.compile(self.draft(self.definition(reference_roles=["identity"])),inputs=[self.ref(foreign)])
        path = self.media.path_for("project_1",image["object_id"])
        path.chmod(0o600)
        path.write_bytes(b"broken")
        with self.assertRaises(DomainError):
            self.compile(self.draft(self.definition(recipe="diagram",reference_roles=["geometry"])),inputs=[self.ref(image)])


if __name__ == "__main__":
    unittest.main()
