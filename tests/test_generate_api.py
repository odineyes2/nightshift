"""NS-115-3 생성 프리셋과 생성·용량 API를 격리된 실제 앱 요청으로 확인한다."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")

import app as A
import db


WORKFLOW = {
    "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "old positive"}},
    "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "old negative"}},
    "3": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 4}},
    "4": {"class_type": "KSampler", "inputs": {"positive": ["1", 0], "negative": ["2", 0],
                                                "latent_image": ["3", 0], "steps": 20, "seed": 1}},
}


class GenerateHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        with db.connect() as conn:
            for name, role in (("generate-admin", "admin"), ("generate-owner", "user"), ("generate-other", "user")):
                conn.execute("INSERT INTO users(username,email,password_hash,role,status,created_at) VALUES(?,?,?,?,?,?)",
                             (name, f"{name}@example.test", "unused", role, "active", db.now_iso()))
            cls.ids = {row["username"]: row["id"] for row in conn.execute("SELECT id,username FROM users")}

    def setUp(self):
        with db.connect() as conn:
            conn.execute("DELETE FROM generate_presets")
            conn.execute("DELETE FROM service_tokens")
        A.jobs.clear()
        self.admin = TestClient(A.app, cookies={A.auth.SESSION_COOKIE: A.auth.create_session(self.ids["generate-admin"])})
        self.client = TestClient(A.app)
        self.addCleanup(self.admin.close)
        self.addCleanup(self.client.close)
        self.write = {"X-Requested-With": "nightshift"}
        issued = self.admin.post("/api/admin/service-tokens", headers=self.write,
                                 json={"user_id": self.ids["generate-owner"], "name": "generator"})
        self.assertEqual(issued.status_code, 200, issued.text)
        self.bearer = {"Authorization": f"Bearer {issued.json()['token']}"}

    def preset(self):
        body = {"name": "article", "workflow": WORKFLOW, "width": 1024, "height": 1024,
                "steps": 28, "max_count": 3, "aspects": {"1:1": [1024, 1024], "16:9": [1536, 864]},
                "user_ids": [self.ids["generate-owner"]], "enabled": True}
        response = self.admin.post("/api/admin/generate-presets", headers=self.write, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return body

    def test_admin_crud_and_access(self):
        self.assertEqual(self.client.get("/api/generate/capacity").status_code, 401)
        self.assertEqual(self.client.get("/api/admin/generate-presets", headers=self.bearer).status_code, 403)
        body = self.preset()
        self.assertEqual([p["name"] for p in self.client.get("/api/generate/presets", headers=self.bearer).json()["presets"]], ["article"])
        listed = self.admin.get("/api/admin/generate-presets").json()["presets"]
        self.assertEqual(listed[0]["workflow"], body["workflow"])
        body["steps"] = 30
        response = self.admin.put("/api/admin/generate-presets/article", headers=self.write, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["steps"], 30)
        self.assertEqual(self.admin.delete("/api/admin/generate-presets/article", headers=self.write).status_code, 200)
        self.assertEqual(self.client.get("/api/generate/presets", headers=self.bearer).json()["presets"], [])

    def test_generate_limits_queue_and_capacity_without_runpod(self):
        self.preset()
        with patch.object(A.pod_registry, "list_pods", return_value=[{"id": "off", "kind": "comfyui", "enabled": False}]), \
             patch.object(A, "fetch_comfy_object_info", side_effect=AssertionError("ComfyUI 호출")) as comfy, \
             patch.object(A.runpod_api, "get_runpod_info", side_effect=AssertionError("RunPod 호출")) as runpod:
            capacity = self.client.get("/api/generate/capacity", headers=self.bearer)
            self.assertEqual(capacity.status_code, 200, capacity.text)
            self.assertEqual(capacity.json()["pods_online"], 0)
            self.assertEqual(capacity.json()["pods_total"], 1)
            for change in ({"count": 4}, {"count": 0}, {"aspect": "9:16"}):
                body = {"preset": "article", "prompt": "article illustration", **change}
                response = self.client.post("/api/generate", headers=self.bearer, json=body)
                self.assertEqual(response.status_code, 400, response.text)
            response = self.client.post("/api/generate", headers=self.bearer,
                                        json={"preset": "article", "prompt": "article illustration",
                                              "negative_prompt": "letters", "aspect": "16:9", "count": 2, "seed": 42})
            self.assertEqual(response.status_code, 200, response.text)
            job_id = response.json()["job_id"]
            self.assertEqual(response.json()["status"], "queued")
            self.assertEqual(len(A.jobs), 1)
            job = A.jobs[job_id]
            self.assertEqual((job["template_id"], job["owner_id"], job["options"]["seed_count"]),
                             ("seed_batch", self.ids["generate-owner"], 2))
            workflow = json.loads((A.JOBS_DIR / job["workflow_filename"]).read_text(encoding="utf-8"))
            self.assertEqual(workflow["1"]["inputs"]["text"], "article illustration")
            self.assertEqual(workflow["2"]["inputs"]["text"], "letters")
            self.assertEqual(workflow["3"]["inputs"], {"width": 1536, "height": 864, "batch_size": 1})
            self.assertEqual(workflow["4"]["inputs"]["steps"], 28)
            self.assertEqual(self.client.get("/api/generate/capacity", headers=self.bearer).json()["queued_jobs"], 1)
            bypass = self.client.post("/api/generate", headers=self.bearer,
                                      json={"preset": "article", "prompt": "literal --repeat 100", "count": 1})
            self.assertEqual(bypass.status_code, 200, bypass.text)
            self.assertEqual(A.jobs[bypass.json()["job_id"]]["options"]["seed_count"], 1)
            comfy.assert_not_called()
            runpod.assert_not_called()

    def test_forbidden_preset_and_invalid_admin_values(self):
        body = self.preset()
        body["user_ids"] = [self.ids["generate-other"]]
        response = self.admin.put("/api/admin/generate-presets/article", headers=self.write, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post("/api/generate", headers=self.bearer,
                                    json={"preset": "article", "prompt": "x"})
        self.assertEqual(response.status_code, 403)
        body["aspects"] = {"oversize": [4096, 4096]}
        self.assertEqual(self.admin.put("/api/admin/generate-presets/article", headers=self.write, json=body).status_code, 400)


if __name__ == "__main__":
    unittest.main()
