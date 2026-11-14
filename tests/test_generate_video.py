"""서비스 연동 루프 영상 생성(POST /api/generate/video)·영상 워커 배정·영상 자동 켜기 검사 — 실제 RunPod·ComfyUI를 부르지 않는다.
실행: python tests/test_generate_video.py"""

import base64
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
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
import auto_power_settings as S  # noqa: E402
import db  # noqa: E402
import input_assets  # noqa: E402

A.runpod_api.RUNPOD_API_KEY = "x"
A.runpod_api.create_pod = lambda *a, **k: (_ for _ in ()).throw(AssertionError("RunPod 호출"))
A.runpod_api.get_runpod_info = lambda url: {}


def png(width=1344, height=768):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (40, 60, 90)).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


class GenerateVideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        with db.connect() as conn:
            for name, role in (("video-admin", "admin"), ("video-bot", "user")):
                conn.execute("INSERT INTO users(username,email,password_hash,role,status,created_at) VALUES(?,?,?,?,?,?)",
                             (name, f"{name}@example.test", "unused", role, "active", db.now_iso()))
            cls.ids = {row["username"]: row["id"] for row in conn.execute("SELECT id,username FROM users")}

    def setUp(self):
        for path in (S.VIDEO_FILE, S.VIDEO_STATE_FILE, S.FILE, S.STATE_FILE):
            path.unlink(missing_ok=True)
        with db.connect() as conn:
            conn.execute("DELETE FROM service_tokens")
        A.jobs.clear()
        self.admin = TestClient(A.app, cookies={A.auth.SESSION_COOKIE: A.auth.create_session(self.ids["video-admin"])})
        self.client = TestClient(A.app)
        self.addCleanup(self.admin.close)
        self.addCleanup(self.client.close)
        self.write = {"X-Requested-With": "nightshift"}
        issued = self.admin.post("/api/admin/service-tokens", headers=self.write,
                                 json={"user_id": self.ids["video-bot"], "name": "loop"})
        self.assertEqual(issued.status_code, 200, issued.text)
        self.bearer = {"Authorization": f"Bearer {issued.json()['token']}"}

    def submit(self, **body):
        with patch.object(A, "poke_scheduler", lambda: None):
            return self.client.post("/api/generate/video", headers=self.bearer,
                                    json={"model": "wan22", "prompt": "she blinks slowly, hair sways", "image": png(), **body})

    def test_refused_until_video_pods_are_enabled(self):
        response = self.submit()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("영상 파드 사용", response.json()["detail"])
        status = self.client.get("/api/generate/video/status", headers=self.bearer).json()
        self.assertEqual((status["video_jobs"], status["enabled"]), (False, False))
        self.assertEqual(A.jobs, {})

    def test_wan22_loop_job_strips_extra_loras_and_targets_video_workers(self):
        S.save({"video_jobs": True}, "video")
        for bad in ({"model": "sora"}, {"prompt": ""}, {"seconds": 9}, {"image": "not-base64!"}):
            self.assertEqual(self.submit(**bad).status_code, 400, bad)
        response = self.submit()
        self.assertEqual(response.status_code, 200, response.text)
        job = A.jobs[response.json()["job_id"]]
        self.assertEqual((job["template_id"], job["runpod_tier"], job["owner_id"]),
                         ("wan22_flf2v_batch", "video", self.ids["video-bot"]))
        options = job["options"]
        self.assertEqual(options["start_image"], options["end_image"])
        self.assertEqual((options["video_width"], options["video_height"], options["text_enhance"]), (720, 416, "off"))
        self.assertTrue(input_assets.input_dir_for(self.ids["video-bot"]).joinpath(options["start_image"]).is_file())
        workflow = json.loads((A.JOBS_DIR / job["workflow_filename"]).read_text(encoding="utf-8"))
        loras = [n["inputs"]["lora_name"] for n in workflow.values() if n["class_type"] == "LoraLoaderModelOnly"]
        self.assertTrue(loras and all("lightx2v" in name for name in loras), loras)
        ids = set(workflow)
        for node in workflow.values():
            for value in node["inputs"].values():
                if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                    self.assertIn(value[0], ids)   # 지운 LoRA를 가리키는 선이 남지 않는다
        unets = sorted(n["inputs"]["unet_name"] for n in workflow.values() if n["class_type"] == "UNETLoader")
        self.assertEqual(unets, ["wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors", "wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors"])
        length = next(n["inputs"]["length"] for n in workflow.values() if n["class_type"] == "WanFirstLastFrameToVideo")
        self.assertEqual(length, 49)

    def test_minimax_loop_job_uses_same_image_as_first_and_last_frame(self):
        S.save({"video_jobs": True}, "video")
        response = self.submit(model="minimax_h3", seconds=3)
        self.assertEqual(response.status_code, 200, response.text)
        job = A.jobs[response.json()["job_id"]]
        self.assertEqual(job["template_id"], "minimax_h3_i2v_batch")
        self.assertEqual(job["options"]["first_frame_image"], job["options"]["last_frame_image"])
        workflow = json.loads((A.JOBS_DIR / job["workflow_filename"]).read_text(encoding="utf-8"))
        titles = {n.get("_meta", {}).get("title") for n in workflow.values()}
        self.assertTrue({"first_frame_image", "last_frame_image"} <= titles)
        scale = next(n["inputs"] for n in workflow.values() if n["class_type"] == "ImageScaleToTotalPixels")
        self.assertAlmostEqual(scale["megapixels"], 0.3, places=1)

    def test_scheduler_sends_video_jobs_only_to_video_workers(self):
        image_pod = {"id": "img", "kind": "comfyui", "enabled": True, "runpod_tier": "image", "name": "img", "owner_id": self.ids["video-bot"]}
        A.jobs["v"] = {"id": "v", "status": "queued", "pod_id": None, "deleted": False, "template_id": "wan22_flf2v_batch",
                       "owner_id": self.ids["video-bot"], "runpod_tier": "video", "queued_at": A.now_iso()}
        with patch.object(A.pod_registry, "list_pods", return_value=[image_pod]), \
             patch.object(A, "driver_for", side_effect=AssertionError("이미지 워커에 연결하면 안 돼요")), \
             patch.object(A, "dispatch_job", side_effect=AssertionError("배정하면 안 돼요")), \
             patch.object(A, "save_state", lambda: None):
            A.schedule_once()
        self.assertIsNone(A.jobs["v"]["pod_id"])
        self.assertIn("영상용 워커", A.jobs["v"]["waiting_reason"])

    def test_video_auto_power_counts_only_video_jobs(self):
        now = datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)
        S.save({"enabled": True, "min_jobs": 1}, "video")
        A.jobs["i"] = {"id": "i", "status": "queued", "pod_id": None, "deleted": False, "template_id": "seed_batch", "queued_at": now.isoformat()}
        self.assertEqual(A._auto_power_on_check(now, "video")["action"], "off")   # 영상 파드 사용이 꺼져 있다
        S.save({"enabled": True, "min_jobs": 1, "video_jobs": True}, "video")
        with patch.object(A.pod_registry, "list_pods", return_value=[]):
            self.assertEqual(A._auto_power_on_check(now, "video")["waiting"], 0)
            A.jobs["v"] = {**A.jobs["i"], "id": "v", "template_id": "wan22_flf2v_batch", "runpod_tier": "video"}
            decision = A._auto_power_on_check(now, "video")
            self.assertEqual((decision["waiting"], decision["action"]), (1, "wait"))
            self.assertIn("영상용 RunPod 워커가 없어요", decision["reason"])
            self.assertEqual(A._auto_power_on_check(now, "image")["waiting"], 1)   # 이미지 쪽은 영상 작업을 세지 않는다
        for bad in ({"video_jobs": "yes"}, {"min_jobs": 0}):
            with self.assertRaises(ValueError):
                S.validate(bad, "video")
        with self.assertRaises(ValueError):
            S.validate({"video_jobs": True})   # 이미지 설정에는 없는 칸

    def test_settings_api_and_service_routes(self):
        response = self.admin.put("/api/settings/auto-power/video", headers=self.write, json={"video_jobs": True, "min_jobs": 2})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["settings"]["video_jobs"], response.json()["settings"]["min_jobs"]), (True, 2))
        self.assertEqual(self.client.get("/api/settings/auto-power/video", headers=self.bearer).status_code, 403)
        self.assertFalse(S.load()["enabled"])   # 이미지 설정은 그대로
        routes = A.SERVICE_TOKEN_ROUTES
        for method, path in (("POST", "/api/generate/video"), ("GET", "/api/generate/video/status"),
                             ("DELETE", "/api/jobs/abc"), ("GET", "/api/output-videos/abc/loop.mp4")):
            self.assertTrue(any(m == method and p.fullmatch(path) for m, p in routes), path)
        self.assertFalse(any(m == "DELETE" and p.fullmatch("/api/output-videos/abc/loop.mp4") for m, p in routes))

    def test_outputs_list_videos_and_token_reads_only_own_video(self):
        with db.connect() as conn:
            conn.execute("DELETE FROM assets")
            conn.execute("DELETE FROM jobs")
            for job_id, owner in (("j1", "video-bot"), ("x", "video-admin")):
                conn.execute("INSERT INTO jobs(id, status, owner_id, data_json) VALUES(?,?,?,?)", (job_id, "done", self.ids[owner], "{}"))
                A.jobs[job_id] = {"id": job_id, "status": "done", "owner_id": self.ids[owner], "deleted": False}
        for name in ("j1/loop_00001.mp4", "x/other.mp4"):
            path = Path(A.OUTPUT_DIR) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes(8) + b"ftypmp42")
        A.assets_index.sync(force=True)
        data = self.client.get("/api/jobs/j1/outputs", headers=self.bearer).json()
        self.assertEqual([v["name"] for v in data["videos"]], ["j1/loop_00001.mp4"])
        self.assertEqual(data["images"], [])
        self.assertEqual(self.client.get("/api/output-videos/j1/loop_00001.mp4", headers=self.bearer).status_code, 200)
        self.assertEqual(self.client.get("/api/output-videos/x/other.mp4", headers=self.bearer).status_code, 403)

if __name__ == "__main__":
    unittest.main()
