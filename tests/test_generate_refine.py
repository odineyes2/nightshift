"""서비스 연동 보정(POST /api/generate/refine: 얼굴 보정·2배 업스케일) 검사 — 실제 RunPod·ComfyUI를 부르지 않는다.
실행: python tests/test_generate_refine.py"""

import base64
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
os.environ.pop("NIGHTSHIFT_INPUT_IMAGES_DIR", None)
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")

import app as A  # noqa: E402
import db  # noqa: E402
import input_assets  # noqa: E402
import model_registry  # noqa: E402

A.runpod_api.RUNPOD_API_KEY = "x"
A.runpod_api.create_pod = lambda *a, **k: (_ for _ in ()).throw(AssertionError("RunPod 호출"))
A.runpod_api.get_runpod_info = lambda url: {}

ANIMA_PARTS = {"clip": "qwen_3_06b_base.safetensors", "vae": "qwen_image_vae.safetensors"}


def png(width=1344, height=768):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (40, 60, 90)).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


class GenerateRefineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        with db.connect() as conn:
            for name, role in (("refine-admin", "admin"), ("refine-bot", "user")):
                conn.execute("INSERT INTO users(username,email,password_hash,role,status,created_at) VALUES(?,?,?,?,?,?)",
                             (name, f"{name}@example.test", "unused", role, "active", db.now_iso()))
            cls.ids = {row["username"]: row["id"] for row in conn.execute("SELECT id,username FROM users")}
        for kind, filename, base in (("checkpoints", "waiIllustriousSDXL_v170.safetensors", "Illustrious"),
                                     ("diffusion_models", "anima-base-v1.0.safetensors", "Anima"),
                                     ("diffusion_models", "krea2_turbo_fp8_scaled.safetensors", "krea.2")):
            model_registry.upsert(kind, filename, {"base_model": base})

    def setUp(self):
        with db.connect() as conn:
            conn.execute("DELETE FROM service_tokens")
        A.jobs.clear()
        self.admin = TestClient(A.app, cookies={A.auth.SESSION_COOKIE: A.auth.create_session(self.ids["refine-admin"])})
        self.client = TestClient(A.app)
        self.addCleanup(self.admin.close)
        self.addCleanup(self.client.close)
        issued = self.admin.post("/api/admin/service-tokens", headers={"X-Requested-With": "nightshift"},
                                 json={"user_id": self.ids["refine-bot"], "name": "refine"})
        self.assertEqual(issued.status_code, 200, issued.text)
        self.bearer = {"Authorization": f"Bearer {issued.json()['token']}"}
        parts = patch.object(model_registry, "unet_image_parts",
                             lambda gid: dict(ANIMA_PARTS) if gid == "anima" else None)
        parts.start()
        self.addCleanup(parts.stop)

    def submit(self, **body):
        with patch.object(A, "poke_scheduler", lambda: None):
            return self.client.post("/api/generate/refine", headers=self.bearer,
                                    json={"op": "face", "model": "checkpoints:waiIllustriousSDXL_v170.safetensors",
                                          "prompt": "1girl, silver hair", "image": png(), "seed": 7, **body})

    def job_workflow(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        job = A.jobs[response.json()["job_id"]]
        self.assertEqual((job["template_id"], job["owner_id"]), ("input_image_batch", self.ids["refine-bot"]))
        self.assertNotEqual(job.get("runpod_tier"), "video")
        self.assertTrue(input_assets.input_dir_for(self.ids["refine-bot"]).joinpath(job["options"]["input_image"]).is_file())
        workflow = json.loads((A.JOBS_DIR / job["workflow_filename"]).read_text(encoding="utf-8"))
        classes = [node["class_type"] for node in workflow.values()]
        loaders = [n for n in workflow.values() if n["class_type"] == "LoadImage"]
        self.assertEqual([n["_meta"]["title"] for n in loaders], ["input_image"])
        ids = set(workflow)
        for node in workflow.values():
            for value in node["inputs"].values():
                if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                    self.assertIn(value[0], ids)
        return workflow, classes

    def test_bad_requests(self):
        for bad in ({"op": "sharpen"}, {"model": "checkpoints:none.safetensors"}, {"image": "not-base64!"},
                    {"prompt": 3}, {"op": "upscale", "image": png(2400, 1200)}):
            self.assertIn(self.submit(**bad).status_code, (400, 404), bad)
        self.assertEqual(A.jobs, {})

    def test_face_on_sdxl_uses_same_checkpoint(self):
        workflow, classes = self.job_workflow(self.submit())
        self.assertIn("FaceDetailer", classes)
        self.assertNotIn("KSampler", classes)
        ckpt = next(n for n in workflow.values() if n["class_type"] == "CheckpointLoaderSimple")
        self.assertEqual(ckpt["inputs"]["ckpt_name"], "waiIllustriousSDXL_v170.safetensors")

    def test_face_on_krea_falls_back_to_anima(self):
        response = self.submit(model="diffusion_models:krea2_turbo_fp8_scaled.safetensors")
        workflow, classes = self.job_workflow(response)
        self.assertEqual(response.json()["model"], "diffusion_models:anima-base-v1.0.safetensors")
        unet = next(n for n in workflow.values() if n["class_type"] == "UNETLoader")
        self.assertEqual(unet["inputs"]["unet_name"], "anima-base-v1.0.safetensors")
        self.assertIn("FaceDetailer", classes)

    def test_upscale_on_anima_uses_model_then_usdu(self):
        response = self.submit(op="upscale", model="diffusion_models:anima-base-v1.0.safetensors")
        workflow, classes = self.job_workflow(response)
        self.assertEqual((response.json()["width"], response.json()["height"]), (2688, 1536))
        self.assertEqual(classes.count("UltimateSDUpscaleNoUpscale"), 1)
        upscaler = next(n for n in workflow.values() if n["class_type"] == "UpscaleModelLoader")
        self.assertEqual(upscaler["inputs"]["model_name"], "4x_foolhardy_Remacri.safetensors")
        scale = next(n for n in workflow.values() if n["class_type"] == "ImageScaleBy")
        self.assertEqual(scale["inputs"]["scale_by"], 0.5)
        usdu = next(n for n in workflow.values() if n["class_type"] == "UltimateSDUpscaleNoUpscale")
        self.assertEqual(usdu["inputs"]["denoise"], 0.25)

    def test_upscale_on_krea_does_not_redraw(self):
        workflow, classes = self.job_workflow(self.submit(op="upscale", model="diffusion_models:krea2_turbo_fp8_scaled.safetensors", prompt=""))
        self.assertIn("ImageUpscaleWithModel", classes)
        self.assertFalse({"UltimateSDUpscaleNoUpscale", "UNETLoader", "KSampler"} & set(classes))

    def test_faces_redraws_each_face_with_its_own_lora(self):
        model_registry.upsert("loras", "actor_a.safetensors", {"base_model": "Illustrious"})
        model_registry.upsert("loras", "actor_b.safetensors", {"base_model": "Illustrious"})
        model_registry.upsert("loras", "anima_only.safetensors", {"base_model": "Anima"})
        faces = [{"prompt": "1girl, silver hair", "loras": [{"name": "actor_a.safetensors", "strength": 0.9}]},
                 {"prompt": "1boy, red hair", "loras": [{"name": "actor_b.safetensors"}]}]
        workflow, classes = self.job_workflow(self.submit(op="faces", prompt="", faces=faces))
        self.assertEqual(classes.count("DetailerForEach"), 2)
        self.assertEqual(classes.count("ImpactSEGSOrderedFilter"), 1)   # 큰 얼굴 2개 고르기
        ranges = [n["inputs"] for n in workflow.values() if n["class_type"] == "ImpactSEGSRangeFilter"]
        self.assertEqual([(r["target"], r["min_value"], r["max_value"]) for r in ranges], [("x1", 0, 587), ("x1", 588, 1344 * 4)])
        self.assertNotIn("KSampler", classes)
        loras = [n["inputs"] for n in workflow.values() if n["class_type"] == "LoraLoaderModelOnly"]
        self.assertEqual([(n["lora_name"], n["strength_model"]) for n in loras],
                         [("actor_a.safetensors", 0.9), ("actor_b.safetensors", 1.0)])
        details = [n["inputs"] for n in workflow.values() if n["class_type"] == "DetailerForEach"]
        self.assertEqual(details[1]["image"][0], next(k for k, n in workflow.items() if n["inputs"] is details[0]))
        texts = [n["inputs"]["text"] for n in workflow.values() if n["class_type"] == "CLIPTextEncode"]
        self.assertTrue(any("silver hair" in t for t in texts) and any("red hair" in t for t in texts))
        for bad in ({"faces": []}, {"faces": [{"prompt": ""}]}, {"faces": [{"prompt": "a"}] * 4},
                    {"faces": [{"prompt": "a", "loras": [{"name": "none.safetensors"}]}]},
                    {"faces": [{"prompt": "a", "loras": [{"name": "anima_only.safetensors"}]}]},
                    {"faces": [{"prompt": "a", "loras": [{"name": "actor_a.safetensors", "strength": 9}]}]},
                    {"faces": faces, "model": "diffusion_models:krea2_turbo_fp8_scaled.safetensors"}):
            self.assertEqual(self.submit(op="faces", prompt="", **bad).status_code, 400, bad)

    def test_people_redraws_each_person_from_the_left_with_its_own_lora(self):
        # 2단계 두 인물: LoRA 없이 그린 그림에서 사람 모양 마스크를 따고, 큰 사람부터 인원수만큼 골라 왼쪽부터 그 인물로 다시 그린다.
        model_registry.upsert("loras", "actor_a.safetensors", {"base_model": "Illustrious"})
        model_registry.upsert("loras", "actor_b.safetensors", {"base_model": "Illustrious"})
        people = [{"prompt": "1boy, red hair, armor", "loras": [{"name": "actor_a.safetensors", "strength": 0.7}]},
                  {"prompt": "1girl, silver hair, robe", "loras": [{"name": "actor_b.safetensors", "strength": 0.7}]}]
        workflow, classes = self.job_workflow(self.submit(op="people", prompt="", people=people, denoise=0.65))
        self.assertIn("SegmDetectorSEGS", classes)
        self.assertNotIn("BboxDetectorSEGS", classes)
        self.assertEqual([n["inputs"]["model_name"] for n in workflow.values() if n["class_type"] == "UltralyticsDetectorProvider"],
                         ["segm/person_yolov8m-seg.pt"])
        picks = [n["inputs"] for n in workflow.values() if n["class_type"] == "ImpactSEGSOrderedFilter"]
        self.assertEqual([(p["target"], p["take_start"], p["take_count"]) for p in picks],
                         [("area(=w*h)", 0, 2), ("x1", 0, 1), ("x1", 1, 1)])
        details = [n["inputs"] for n in workflow.values() if n["class_type"] == "DetailerForEach"]
        self.assertEqual([(d["denoise"], d["guide_size"]) for d in details], [(0.65, 1024), (0.65, 1024)])
        for bad in ({"people": []}, {"people": people, "denoise": 1.5}, {"people": people, "denoise": True}):
            self.assertEqual(self.submit(op="people", prompt="", **bad).status_code, 400, bad)

    def test_generate_takes_registered_loras(self):
        model_registry.upsert("loras", "actor_a.safetensors", {"base_model": "Illustrious"})
        body = {"preset": "checkpoints:waiIllustriousSDXL_v170.safetensors", "prompt": "1girl", "aspect": "16:9", "seed": 3}
        with patch.object(A, "poke_scheduler", lambda: None):
            ok = self.client.post("/api/generate", headers=self.bearer,
                                  json={**body, "loras": [{"name": "actor_a.safetensors", "strength": 0.8}]})
            bad = self.client.post("/api/generate", headers=self.bearer, json={**body, "loras": [{"name": "x.safetensors"}]})
        self.assertEqual((ok.status_code, bad.status_code), (200, 400), ok.text)
        job = A.jobs[ok.json()["job_id"]]
        workflow = json.loads((A.JOBS_DIR / job["workflow_filename"]).read_text(encoding="utf-8"))
        lora = next(n for n in workflow.values() if n["class_type"] == "LoraLoader")
        self.assertEqual((lora["inputs"]["lora_name"], lora["inputs"]["strength_model"]), ("actor_a.safetensors", 0.8))
        sampler = next(n for n in workflow.values() if n["class_type"] == "KSampler")
        self.assertEqual(workflow[sampler["inputs"]["model"][0]]["class_type"], "LoraLoader")

    def test_generate_regions_split_the_picture_left_to_right(self):
        body = {"preset": "checkpoints:waiIllustriousSDXL_v170.safetensors", "prompt": "1girl, 1boy, castle", "aspect": "16:9", "seed": 3}
        with patch.object(A, "poke_scheduler", lambda: None):
            ok = self.client.post("/api/generate", headers=self.bearer, json={**body, "regions": ["1girl, silver hair", "1boy, red hair"]})
            bad = [self.client.post("/api/generate", headers=self.bearer, json={**body, **extra}).status_code
                   for extra in ({"regions": ["only one"]}, {"regions": ["a", ""]}, {"regions": ["a"] * 4},
                                 {"regions": ["a", "b"], "preset": "diffusion_models:krea2_turbo_fp8_scaled.safetensors"})]
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(bad, [400, 400, 400, 400])
        job = A.jobs[ok.json()["job_id"]]
        workflow = json.loads((A.JOBS_DIR / job["workflow_filename"]).read_text(encoding="utf-8"))
        areas = [n["inputs"] for n in workflow.values() if n["class_type"] == "ConditioningSetArea"]
        self.assertEqual([(a["x"], a["width"], a["height"]) for a in areas], [(0, 672, 768), (672, 672, 768)])
        self.assertEqual({a["strength"] for a in areas}, {1.0})
        with patch.object(A, "poke_scheduler", lambda: None):
            weak = self.client.post("/api/generate", headers=self.bearer, json={**body, "regions": ["a", "b"], "region_strength": 0.5})
            wrong = self.client.post("/api/generate", headers=self.bearer, json={**body, "regions": ["a", "b"], "region_strength": 2})
        self.assertEqual(wrong.status_code, 400)
        weak_flow = json.loads((A.JOBS_DIR / A.jobs[weak.json()["job_id"]]["workflow_filename"]).read_text(encoding="utf-8"))
        self.assertEqual({n["inputs"]["strength"] for n in weak_flow.values() if n["class_type"] == "ConditioningSetArea"}, {0.5})
        texts = [workflow[a["conditioning"][0]]["inputs"]["text"] for a in areas]
        self.assertEqual(texts, ["1girl, silver hair", "1boy, red hair"])
        sampler = next(n for n in workflow.values() if n["class_type"] == "KSampler")
        self.assertEqual(workflow[sampler["inputs"]["positive"][0]]["class_type"], "ConditioningCombine")
        main = next(n for n in workflow.values() if n["_meta"]["title"] == "main_prompt")   # 실행 템플릿이 다시 넣는 메인 프롬프트는 그대로
        self.assertIn("castle", main["inputs"]["text"])
        for node in workflow.values():
            if node["class_type"] in ("ConditioningSetArea", "ConditioningCombine") or node["_meta"]["title"].startswith("칸"):
                self.assertFalse(any(word in node["_meta"]["title"] for word in ("main_prompt", "negative_prompt", "KSampler", "Save", "latent")))

    def test_hosted_upscaler_upload_download_and_auto_install_address(self):
        import hosted_models
        name = "4x-UltraSharp.safetensors"
        put = self.admin.put("/api/hosted-models/upscale_models/" + name,
                             content=b"upscaler-bytes", headers={"X-Requested-With": "nightshift"})
        self.assertEqual(put.status_code, 200, put.text)
        url = hosted_models.url("https://ns.example", "upscale_models", name)
        self.assertEqual(self.client.get(url.removeprefix("https://ns.example")).content, b"upscaler-bytes")
        wrong_kind = url.removeprefix("https://ns.example").replace("/upscale_models/", "/loras/")
        self.assertEqual(self.client.get(wrong_kind).status_code, 404)
        calls = []
        with patch.dict(os.environ, {"NIGHTSHIFT_PUBLIC_URL": "https://ns.example"}), \
                patch.object(A.model_download, "call_node", lambda pod, method, route, body: calls.append(body)):
            result = A.asyncio.run(A._start_model_downloads({"id": self.ids["refine-admin"]}, {"id": "p"}, [name]))
        self.assertEqual(result, ([name], [], []))
        self.assertEqual((calls[0]["url"], calls[0]["folder"], calls[0]["headers"]), (url, "upscale_models", {}))

    def test_hosted_lora_upload_download_and_auto_install_address(self):
        import hosted_models
        csrf = {"X-Requested-With": "nightshift"}
        put = self.admin.put("/api/hosted-models/loras/studio_test.safetensors?base_model=Illustrious&trigger=test_actor",
                             content=b"lora-bytes", headers=csrf)
        self.assertEqual(put.status_code, 200, put.text)
        entry = model_registry.get_entry("loras", "studio_test.safetensors")
        self.assertEqual((entry["trigger_keyword"], entry["base_model"]), ("test_actor", "Illustrious"))
        self.assertEqual(self.client.put("/api/hosted-models/loras/x.safetensors", content=b"x", headers=self.bearer).status_code, 403)
        self.assertEqual(self.admin.put("/api/hosted-models/loras/..x.safetensors", content=b"x", headers=csrf).status_code, 400)
        url = hosted_models.url("https://ns.example", "loras", "studio_test.safetensors")
        path = url.removeprefix("https://ns.example")
        self.assertEqual(self.client.get(path).content, b"lora-bytes")   # 로그인 없이 파일별 열쇠로
        self.assertEqual(self.client.get(path.replace("studio_test", "studio_other")).status_code, 404)
        self.assertEqual(self.client.get("/api/hosted-models/loras/" + "0" * 40 + "/studio_test.safetensors").status_code, 404)
        calls = []
        with patch.dict(os.environ, {"NIGHTSHIFT_PUBLIC_URL": "https://ns.example"}), \
                patch.object(A.model_download, "call_node", lambda pod, method, route, body: calls.append(body)):
            started, no_url, failed = A.asyncio.run(A._start_model_downloads({"id": self.ids["refine-admin"]}, {"id": "p"}, ["studio_test.safetensors"]))
        self.assertEqual((started, no_url, failed), (["studio_test.safetensors"], [], []))
        self.assertEqual((calls[0]["url"], calls[0]["folder"]), (url, "loras"))

    def test_train_saves_images_and_queues_a_lora_train_job(self):
        import hosted_models
        images = [{"image": png(1024, 1024), "caption": f"1girl, pose {i}"} for i in range(8)]
        body = {"name": "studio_test_actor", "trigger": "test_actor", "model": "checkpoints:waiIllustriousSDXL_v170.safetensors",
                "images": images, "epochs": 6}
        with patch.object(A, "poke_scheduler", lambda: None):
            ok = self.client.post("/api/generate/train", headers=self.bearer, json=body)
            bad = [self.client.post("/api/generate/train", headers=self.bearer, json={**body, **extra}).status_code
                   for extra in ({"images": images[:7]}, {"name": "bad name"}, {"trigger": "bad trigger"},
                                 {"model": "diffusion_models:krea2_turbo_fp8_scaled.safetensors"}, {"epochs": 99},
                                 {"images": [{**images[0], "caption": "a" + chr(10) + "b"}] * 8})]
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(bad, [400, 400, 400, 400, 400, 400])
        job = A.jobs[ok.json()["job_id"]]
        self.assertEqual((job["template_id"], job["owner_id"]), ("lora_train", self.ids["refine-bot"]))
        manifest = json.loads(Path(job["options"]["train_manifest"]).read_text(encoding="utf-8"))
        self.assertEqual((manifest["name"], manifest["trigger"], manifest["checkpoint"], manifest["epochs"], len(manifest["images"])),
                         ("studio_test_actor", "test_actor", "waiIllustriousSDXL_v170.safetensors", 6, 8))
        self.assertEqual(manifest["images"][3]["caption"], "1girl, pose 3")
        folder = input_assets.input_dir_for(self.ids["refine-bot"])
        self.assertTrue(all(folder.joinpath(item["file"]).is_file() for item in manifest["images"]))
        workflow = json.loads((A.JOBS_DIR / job["workflow_filename"]).read_text(encoding="utf-8"))
        self.assertEqual([next(iter(n["inputs"].values())) for n in workflow.values()],
                         ["waiIllustriousSDXL_v170.safetensors", "bbox/face_yolov8m.pt"])   # 바탕 모델 + 얼굴 탐지 모델(자동 설치되게)
        # 끝나면 러너가 보관 폴더의 결과를 등록부에 적는다.
        hosted_models.path("loras", "studio_test_actor.safetensors").parent.mkdir(parents=True, exist_ok=True)
        hosted_models.path("loras", "studio_test_actor.safetensors").write_bytes(b"trained")
        A.register_trained_lora(job)
        entry = model_registry.get_entry("loras", "studio_test_actor.safetensors")
        self.assertEqual((entry["trigger_keyword"], entry["base_models"], entry["size_bytes"]), ("test_actor", ["Illustrious"], 7))
        with patch.object(A, "poke_scheduler", lambda: None):
            self.assertEqual(self.client.post("/api/generate/train", headers=self.bearer, json=body).status_code, 409)   # 같은 이름

    def test_private_loras_are_only_for_their_owner(self):
        import lora_access
        with db.connect() as conn:
            conn.execute("INSERT INTO users(username,email,password_hash,role,status,created_at) VALUES('other','o@example.test','x','user','active',?)", (db.now_iso(),))
            other = conn.execute("SELECT id FROM users WHERE username='other'").fetchone()[0]
        for name in ("mine.safetensors", "theirs.safetensors", "shared.safetensors", "legacy.safetensors"):
            model_registry.upsert("loras", name, {"base_model": "Illustrious"})
        lora_access.set_access("mine.safetensors", self.ids["refine-bot"], "private")
        lora_access.set_access("theirs.safetensors", other, "private")
        lora_access.set_access("shared.safetensors", self.ids["refine-admin"], "public")
        body = {"preset": "checkpoints:waiIllustriousSDXL_v170.safetensors", "prompt": "1girl", "aspect": "16:9", "seed": 3}
        with patch.object(A, "poke_scheduler", lambda: None):
            codes = {name: self.client.post("/api/generate", headers=self.bearer, json={**body, "loras": [{"name": name}]}).status_code
                     for name in ("mine.safetensors", "theirs.safetensors", "shared.safetensors", "legacy.safetensors")}
            face = self.submit(op="faces", prompt="", faces=[{"prompt": "a", "loras": [{"name": "theirs.safetensors"}]}])
        self.assertEqual(codes, {"mine.safetensors": 200, "theirs.safetensors": 400, "shared.safetensors": 200, "legacy.safetensors": 200})
        self.assertEqual(face.status_code, 400)
        self.assertTrue(lora_access.can_use("theirs.safetensors", self.ids["refine-admin"]))   # 관리자는 언제나
        # 워커 자동 설치도 주인이 아닌 작업에는 비공개 LoRA를 보내지 않는다.
        import hosted_models
        for name in ("theirs.safetensors", "mine.safetensors"):
            hosted_models.path("loras", name).parent.mkdir(parents=True, exist_ok=True)
            hosted_models.path("loras", name).write_bytes(b"x")
        with patch.dict(os.environ, {"NIGHTSHIFT_PUBLIC_URL": "https://ns.example"}),                 patch.object(A.model_download, "call_node", lambda pod, method, route, body: None):
            started, no_url, failed = A.asyncio.run(A._start_model_downloads({"id": self.ids["refine-bot"]}, {"id": "p"},
                                                                             ["theirs.safetensors", "mine.safetensors"]))
        self.assertEqual((started, [f["name"] for f in failed]), (["mine.safetensors"], ["theirs.safetensors"]))

    def test_train_visibility_public_is_admin_only(self):
        import lora_access
        images = [{"image": png(1024, 1024), "caption": "1girl"} for _ in range(8)]
        body = {"name": "pub_try", "trigger": "pub_try", "model": "checkpoints:waiIllustriousSDXL_v170.safetensors", "images": images}
        with patch.object(A, "poke_scheduler", lambda: None):
            self.assertEqual(self.client.post("/api/generate/train", headers=self.bearer, json={**body, "visibility": "public"}).status_code, 403)
            self.assertEqual(self.client.post("/api/generate/train", headers=self.bearer, json={**body, "visibility": "x"}).status_code, 400)
            job_id = self.client.post("/api/generate/train", headers=self.bearer, json=body).json()["job_id"]
        import hosted_models
        hosted_models.path("loras", "pub_try.safetensors").write_bytes(b"t")
        A.register_trained_lora(A.jobs[job_id])
        self.assertEqual(tuple(lora_access.lookup("pub_try.safetensors")), (self.ids["refine-bot"], "private"))

    def test_service_route_allowed(self):
        self.assertTrue(any(m == "POST" and p.fullmatch("/api/generate/train") for m, p in A.SERVICE_TOKEN_ROUTES))

    def test_service_route_allowed_refine(self):
        self.assertTrue(any(m == "POST" and p.fullmatch("/api/generate/refine") for m, p in A.SERVICE_TOKEN_ROUTES))


if __name__ == "__main__":
    unittest.main()
