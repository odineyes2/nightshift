"""다운로더 모델 크기 수집과 로그인한 회원의 크기 조회 API를 격리해 검사한다."""
import asyncio
import importlib.util
import os
import secrets
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for part in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{part}_DIR"] = str(Path(TMP.name) / part.lower())
PASSWORD = secrets.token_urlsafe(18)
os.environ["NIGHTSHIFT_ADMIN_USER"] = "admin"
os.environ["NIGHTSHIFT_ADMIN_PASSWORD"] = PASSWORD
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")

import app as A
import db
from fastapi.testclient import TestClient


class ModelSizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        A.auth.ensure_admin()
        cls.user = A.auth.register("size-member", "size-member@example.test", PASSWORD)
        with db.connect() as conn:
            conn.execute("UPDATE users SET status='active' WHERE id=?", (cls.user["id"],))
        cls.client = TestClient(A.app)
        response = cls.client.post("/api/auth/login", json={"username": "size-member", "password": PASSWORD},
                                   headers={"X-Requested-With": "nightshift"})
        assert response.status_code == 200, response.text

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        TMP.cleanup()

    def test_node_collects_relative_sizes_and_requires_token(self):
        base = Path(TMP.name) / "models"
        (base / "nested").mkdir(parents=True)
        (base / "nested" / "ok.safetensors").write_bytes(b"a" * 13)
        (base / "ignore.txt").write_bytes(b"x")
        web = types.SimpleNamespace(json_response=lambda body, status=200: types.SimpleNamespace(body=body, status=status))

        class Routes:
            def get(self, _path):
                return lambda fn: fn
            def post(self, _path):
                return lambda fn: fn

        fake = {
            "aiohttp": types.SimpleNamespace(web=web),
            "folder_paths": types.SimpleNamespace(get_folder_paths=lambda name: [str(base)] if name == "checkpoints" else [],
                                                 models_dir=str(base.parent)),
            "server": types.SimpleNamespace(PromptServer=types.SimpleNamespace(instance=types.SimpleNamespace(routes=Routes()))),
        }
        node = ROOT / "templates" / "comfy_nodes" / "nightshift_downloader" / "__init__.py"
        with patch.dict(sys.modules, fake):
            spec = importlib.util.spec_from_file_location("ns_size_node", node)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        with patch.object(module, "_read_token", return_value=secrets.token_urlsafe(16)) as token:
            request = types.SimpleNamespace(headers={})
            self.assertEqual(asyncio.run(module.model_sizes(request)).status, 401)
            request.headers["X-Nightshift-Token"] = token.return_value
            result = asyncio.run(module.model_sizes(request))
        self.assertEqual(result.status, 200)
        self.assertEqual(result.body["sizes"]["checkpoints"], {"nested/ok.safetensors": 13})
        self.assertNotIn("ignore.txt", result.body["sizes"]["checkpoints"])

    def test_api_merges_visible_pods_and_empty_case(self):
        path = "/api/models/sizes"
        self.assertEqual(TestClient(A.app).get(path).status_code, 401)
        owner = self.user["id"]
        pods = [
            {"id": "size-a", "owner_id": owner, "kind": "comfyui", "enabled": True, "url": "https://a.example.test"},
            {"id": "size-b", "owner_id": owner, "kind": "comfyui", "enabled": True, "url": "https://b.example.test"},
            {"id": "size-off", "owner_id": owner, "kind": "comfyui", "enabled": False, "url": "https://off.example.test"},
            {"id": "size-other", "owner_id": "someone-else", "kind": "comfyui", "enabled": True, "url": "https://other.example.test"},
        ]
        def list_pods(owner_id=None):
            return [p for p in pods if p["owner_id"] == owner_id]
        def call_node(pod, method, route):
            self.assertEqual((method, route), ("GET", "/nightshift/models/sizes"))
            if pod["id"] == "size-a":
                return {"sizes": {"checkpoints": {"nested/a.safetensors": 10}}}
            return {"sizes": {"checkpoints": {"nested/a.safetensors": 20, "b.ckpt": 4}}}

        A._model_sizes_cache.clear()
        with patch.object(A.pod_registry, "list_pods", side_effect=list_pods), \
             patch.object(A.model_download, "call_node", side_effect=call_node) as mocked:
            result = self.client.get(path)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json(), {"sizes": {"checkpoints": {"nested/a.safetensors": 20, "b.ckpt": 4}}})
            self.assertEqual(mocked.call_count, 2)
            self.assertEqual(self.client.get(path).json(), result.json())
            self.assertEqual(mocked.call_count, 2)
            pods.clear()
            self.assertEqual(self.client.get(path).json(), {"sizes": {}})
            self.assertEqual(mocked.call_count, 2)
        A._model_sizes_cache.clear()
        pods.append({"id": "size-old", "owner_id": owner, "kind": "comfyui", "enabled": True,
                     "url": "https://old.example.test"})
        with patch.object(A.pod_registry, "list_pods", side_effect=list_pods), \
             patch.object(A.model_download, "call_node", side_effect=A.model_download.DownloadError("구버전", 404)):
            self.assertEqual(self.client.get(path).json(), {"sizes": {}})


if __name__ == "__main__":
    unittest.main()
