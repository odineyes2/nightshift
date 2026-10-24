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
            self.assertEqual(result.json(), {"sizes": {"checkpoints": {"nested/a.safetensors": 20, "b.ckpt": 4}},
                                             "updated": []})
            self.assertEqual(mocked.call_count, 2)
            self.assertEqual(self.client.get(path).json(), result.json())
            self.assertEqual(mocked.call_count, 2)
            pods.clear()
            self.assertEqual(self.client.get(path).json(), {"sizes": {}, "updated": []})
            self.assertEqual(mocked.call_count, 2)
        A._model_sizes_cache.clear()
        pods.append({"id": "size-old", "owner_id": owner, "kind": "comfyui", "enabled": True,
                     "url": "https://old.example.test"})
        with patch.object(A.pod_registry, "list_pods", side_effect=list_pods), \
             patch.object(A.model_download, "call_node", side_effect=A.model_download.DownloadError("구버전", 404)):
            self.assertEqual(self.client.get(path).json(), {"sizes": {}, "updated": []})

    def _admin(self):
        c = TestClient(A.app)
        self.addCleanup(c.close)
        r = c.post("/api/auth/login", json={"username": "admin", "password": PASSWORD},
                   headers={"X-Requested-With": "nightshift"})
        self.assertEqual(r.status_code, 200, r.text)
        return c

    def test_measured_sizes_update_registry_only_existing_rows(self):
        A.model_registry.upsert("checkpoints", "reg-a.safetensors", {"size_bytes": 5})   # manual
        pod = {"id": "size-reg", "owner_id": self.user["id"], "kind": "comfyui", "enabled": True,
               "url": "https://r.example.test"}
        sizes = {"sizes": {"checkpoints": {"reg-a.safetensors": 99, "pod-only.safetensors": 7}}}
        A._model_sizes_cache.clear()
        with patch.object(A.pod_registry, "list_pods", return_value=[pod]), \
             patch.object(A.model_download, "call_node", return_value=sizes):
            data = self.client.get("/api/models/sizes").json()
        self.assertEqual([(u["filename"], u["size_bytes"]) for u in data["updated"]], [("reg-a.safetensors", 99)])
        entry = A.model_registry.get_entry("checkpoints", "reg-a.safetensors")
        self.assertEqual((entry["size_bytes"], entry["size_source"]), (99, "measured"))
        self.assertIsNone(A.model_registry.get_entry("checkpoints", "pod-only.safetensors"))

    def test_put_models_size_is_manual_and_source_not_client_controlled(self):
        admin = self._admin()
        h = {"X-Requested-With": "nightshift"}
        body = {"kind": "loras", "filename": "put-size.safetensors", "size_bytes": 1234, "size_source": "auto"}
        self.assertEqual(self.client.put("/api/models", json=body, headers=h).status_code, 403)
        r = admin.put("/api/models", json=body, headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["entry"]["size_bytes"], r.json()["entry"]["size_source"]), (1234, "manual"))
        A.model_registry.record_measured_sizes("loras", {"put-size.safetensors": 1234})
        # 값이 그대로인 저장은 출처를 바꾸지 않는다
        r = admin.put("/api/models", json={**body, "notes": "n"}, headers=h)
        self.assertEqual(r.json()["entry"]["size_source"], "measured")
        r = admin.put("/api/models", json={**body, "size_bytes": -1}, headers=h)
        self.assertEqual(r.status_code, 400)

    def test_download_meta_fills_auto_and_done_records_measured(self):
        admin = self._admin()
        h = {"X-Requested-With": "nightshift"}
        pod = {"id": "size-dl", "owner_id": "x", "kind": "comfyui", "enabled": True, "url": "https://d.example.test"}
        A.model_registry.upsert("vae", "manual.safetensors", {"size_bytes": 11})
        statuses = {"downloads": [{"id": "d1", "status": "done", "kind": "vae", "filename": "auto.safetensors",
                                   "downloaded": 4321, "total": 4321}]}
        def call(p, method, route, body=None):
            return statuses if route.endswith("status") else {"id": "d1"}
        with patch.object(A.pod_registry, "get_pod", return_value=pod), \
             patch.object(A.model_download, "call_node", side_effect=call):
            for name in ("auto.safetensors", "manual.safetensors"):
                r = admin.post("/api/models/download", headers=h, json={
                    "pod_id": "size-dl", "url": "https://civitai.com/api/download/models/1", "kind": "vae",
                    "filename": name, "meta": {"size_bytes": 777, "size_source": "manual", "notes": "x"}})
                self.assertEqual(r.status_code, 200, r.text)
            e = A.model_registry.get_entry("vae", "auto.safetensors")
            self.assertEqual((e["size_bytes"], e["size_source"]), (777, "auto"))
            e = A.model_registry.get_entry("vae", "manual.safetensors")
            self.assertEqual((e["size_bytes"], e["size_source"]), (11, "manual"))
            self.assertEqual(admin.get("/api/models/downloads", params={"pod_id": "size-dl"}).status_code, 200)
        e = A.model_registry.get_entry("vae", "auto.safetensors")
        self.assertEqual((e["size_bytes"], e["size_source"]), (4321, "measured"))

    def test_backfill_sizes_admin_only_with_mock_resolver(self):
        h = {"X-Requested-With": "nightshift"}
        A.model_registry.upsert("checkpoints", "bf-ok.safetensors", {"download_url": "https://civitai.com/api/download/models/5"})
        A.model_registry.upsert("checkpoints", "bf-no.safetensors", {"download_url": "https://example.com/x.safetensors"})
        A.model_registry.upsert("checkpoints", "bf-has.safetensors",
                                {"download_url": "https://civitai.com/api/download/models/6", "size_bytes": 3})
        self.assertEqual(self.client.post("/api/models/backfill-sizes", headers=h).status_code, 403)
        admin = self._admin()
        version = {"files": [{"name": "bf-ok.safetensors", "sizeKB": 2, "primary": True},
                             {"name": "other.safetensors", "sizeKB": 9}]}
        with patch.object(A.model_download, "_get_json", return_value=version):
            r = admin.post("/api/models/backfill-sizes", headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["filled"], 1)
        e = A.model_registry.get_entry("checkpoints", "bf-ok.safetensors")
        self.assertEqual((e["size_bytes"], e["size_source"]), (2048, "auto"))
        self.assertIsNone(A.model_registry.get_entry("checkpoints", "bf-no.safetensors")["size_bytes"])
        self.assertEqual(A.model_registry.get_entry("checkpoints", "bf-has.safetensors")["size_source"], "manual")


if __name__ == "__main__":
    unittest.main()
