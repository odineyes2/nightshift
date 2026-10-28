"""외부 서비스 연동(전용 계정·토큰 교체·해제)과 프리셋 없는 생성 모델 API를 격리된 실제 앱 요청으로 확인한다."""

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
import model_registry


class ServiceConnectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        with db.connect() as conn:
            for name, role in (("connect-admin", "admin"), ("connect-member", "user")):
                conn.execute("INSERT INTO users(username,email,password_hash,role,status,created_at) VALUES(?,?,?,?,?,?)",
                             (name, f"{name}@example.test", "unused", role, "active", db.now_iso()))
            cls.ids = {row["username"]: row["id"] for row in conn.execute("SELECT id,username FROM users")}
        model_registry.upsert("checkpoints", "article-xl.safetensors", {
            "base_models": ["SDXL"], "sweet": {"steps": 24, "cfg": 5.0, "positive_prefix": "masterpiece"}})
        model_registry.upsert("loras", "style.safetensors", {"base_models": ["SDXL"]})

    def setUp(self):
        with db.connect() as conn:
            conn.execute("DELETE FROM service_tokens")
        A.jobs.clear()
        self.write = {"X-Requested-With": "nightshift"}
        self.admin = TestClient(A.app, cookies={A.auth.SESSION_COOKIE: A.auth.create_session(self.ids["connect-admin"])})
        self.member = TestClient(A.app, cookies={A.auth.SESSION_COOKIE: A.auth.create_session(self.ids["connect-member"])})
        self.client = TestClient(A.app)
        for client in (self.admin, self.member, self.client):
            self.addCleanup(client.close)

    def connect(self):
        response = self.admin.post("/api/admin/service-tokens/connect", headers=self.write, json={"service": "moai"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def bearer(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_connect_creates_bot_rotates_and_disconnects(self):
        first = self.connect()
        self.assertEqual(first["username"], "moai-bot")
        self.assertTrue(first["token"].startswith("nst_"))
        with db.connect() as conn:
            bot = conn.execute("SELECT role,status FROM users WHERE username='moai-bot'").fetchone()
        self.assertEqual((bot["role"], bot["status"]), ("user", "active"))
        # 무작위 비밀번호라 사람이 이 계정으로 로그인할 수 없다.
        login = self.client.post("/api/auth/login", headers=self.write, json={"username": "moai-bot", "password": "moai-bot"})
        self.assertNotEqual(login.status_code, 200)
        self.assertEqual(self.client.get("/api/generate/capacity", headers=self.bearer(first["token"])).status_code, 200)
        # 다시 연동하면 같은 계정에 새 토큰이 나오고 이전 토큰은 폐기된다.
        second = self.connect()
        self.assertEqual(second["user_id"], first["user_id"])
        self.assertEqual(self.client.get("/api/generate/capacity", headers=self.bearer(first["token"])).status_code, 401)
        self.assertEqual(self.client.get("/api/generate/capacity", headers=self.bearer(second["token"])).status_code, 200)
        # 토큰으로는 연동 API를 부를 수 없다(관리자 API 차단).
        self.assertEqual(self.client.post("/api/admin/service-tokens/connect", json={"service": "moai"},
                                          headers=self.bearer(second["token"])).status_code, 403)
        response = self.admin.post("/api/admin/service-tokens/disconnect", headers=self.write, json={"service": "moai"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["revoked"], 1)
        self.assertEqual(self.client.get("/api/generate/capacity", headers=self.bearer(second["token"])).status_code, 401)

    def test_connect_requires_admin_and_valid_service(self):
        self.assertEqual(self.member.post("/api/admin/service-tokens/connect", headers=self.write,
                                          json={"service": "moai"}).status_code, 403)
        self.assertEqual(self.client.post("/api/admin/service-tokens/connect", headers=self.write,
                                          json={"service": "moai"}).status_code, 401)
        for bad in ("Moai", "../x", "", None):
            self.assertEqual(self.admin.post("/api/admin/service-tokens/connect", headers=self.write,
                                             json={"service": bad}).status_code, 400)
        # 같은 이름의 계정이 관리자이면 토큰을 붙이지 않는다.
        with db.connect() as conn:
            conn.execute("INSERT INTO users(username,email,password_hash,role,status,created_at) VALUES(?,?,?,?,?,?)",
                         ("other-bot", "other-bot@example.test", "unused", "admin", "active", db.now_iso()))
        self.assertEqual(self.admin.post("/api/admin/service-tokens/connect", headers=self.write,
                                         json={"service": "other"}).status_code, 409)

    def test_models_list_and_generate_without_preset(self):
        token = self.connect()["token"]
        listed = self.client.get("/api/generate/models", headers=self.bearer(token))
        self.assertEqual(listed.status_code, 200, listed.text)
        ids = [m["id"] for m in listed.json()["models"]]
        self.assertIn("checkpoints:article-xl.safetensors", ids)
        self.assertNotIn("loras:style.safetensors", ids)
        self.assertIn("16:9", listed.json()["aspects"])
        with patch.object(A, "fetch_comfy_object_info", side_effect=AssertionError("ComfyUI 호출")) as comfy, \
             patch.object(A.runpod_api, "get_runpod_info", side_effect=AssertionError("RunPod 호출")) as runpod:
            for body, status in (({"preset": "checkpoints:missing.safetensors", "prompt": "x"}, 404),
                                 ({"preset": "loras:style.safetensors", "prompt": "x"}, 404),
                                 ({"preset": "checkpoints:article-xl.safetensors", "prompt": "x", "count": 5}, 400),
                                 ({"preset": "checkpoints:article-xl.safetensors", "prompt": "x", "aspect": "2:1"}, 400)):
                response = self.client.post("/api/generate", headers=self.bearer(token), json=body)
                self.assertEqual(response.status_code, status, response.text)
            response = self.client.post("/api/generate", headers=self.bearer(token), json={
                "preset": "checkpoints:article-xl.safetensors", "prompt": "abstract data flow",
                "aspect": "16:9", "count": 1, "seed": 7})
            self.assertEqual(response.status_code, 200, response.text)
            comfy.assert_not_called()
            runpod.assert_not_called()
        job = A.jobs[response.json()["job_id"]]
        self.assertEqual((job["template_id"], job["status"]), ("seed_batch", "queued"))
        self.assertEqual(job["options"]["main_prompt"], "masterpiece, abstract data flow")
        workflow = json.loads((A.JOBS_DIR / job["workflow_filename"]).read_text(encoding="utf-8"))
        nodes = {node["class_type"]: node["inputs"] for node in workflow.values()}
        self.assertEqual(nodes["CheckpointLoaderSimple"]["ckpt_name"], "article-xl.safetensors")
        self.assertEqual((nodes["EmptyLatentImage"]["width"], nodes["EmptyLatentImage"]["height"]), (1344, 768))
        self.assertEqual((nodes["KSampler"]["steps"], nodes["KSampler"]["seed"]), (24, 7))


if __name__ == "__main__":
    unittest.main()
