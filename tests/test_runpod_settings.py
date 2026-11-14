"""RunPod GPU 설정 라우트와 생성 순서를 격리 데이터·가짜 응답으로 확인한다."""
import os
import io
import json
import secrets
import sys
import tempfile
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

import app as A  # noqa: E402
import db  # noqa: E402
import runpod_api as R  # noqa: E402
import runpod_settings as S  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class RunPodSettingsTests(unittest.TestCase):
    def setUp(self):
        S.FILE.unlink(missing_ok=True)

    @classmethod
    def setUpClass(cls):
        db.init()
        A.auth.ensure_admin()
        user = A.auth.register("member", "member@example.test", PASSWORD)
        with db.connect() as conn:
            conn.execute("UPDATE users SET status='active' WHERE id=?", (user["id"],))
        cls.admin = TestClient(A.app)
        cls.member = TestClient(A.app)
        headers = {"X-Requested-With": "nightshift"}
        for client, name in ((cls.admin, "admin"), (cls.member, "member")):
            response = client.post("/api/auth/login", json={"username": name, "password": PASSWORD}, headers=headers)
            assert response.status_code == 200, response.text
        cls.headers = headers

    @classmethod
    def tearDownClass(cls):
        cls.admin.close()
        cls.member.close()
        TMP.cleanup()

    def test_routes_permissions_validation_and_effective_tiers(self):
        path = "/api/settings/runpod-gpus"
        self.assertEqual(self.member.get(path).status_code, 403)
        self.assertEqual(self.member.put(path, json={}, headers=self.headers).status_code, 403)
        self.assertEqual(self.member.get(path + "/catalog").status_code, 403)
        # 등급 카드는 회원 누구나 본다(자기 RunPod 계정에 파드를 만든다) — 자기 키가 없으면 api_key=False(관리자 키를 빌려 주지 않는다)
        member_tiers = self.member.get("/api/runpod/tiers")
        self.assertEqual(member_tiers.status_code, 200, member_tiers.text)
        self.assertFalse(member_tiers.json()["api_key"])
        self.assertEqual(self.member.post("/api/pods/runpod-create", json={"tier": "image"}, headers=self.headers).status_code, 400)
        baseline = self.admin.get(path).json()["families"]
        self.assertFalse(baseline["image"]["configured"])
        self.assertEqual(baseline["image"]["gpus"], R.RUNPOD_TIERS["image"]["gpus"])
        for invalid in ({"image": {"gpus": [], "clouds": ["SECURE"]}},
                        {"image": {"gpus": ["G", "G"], "clouds": ["SECURE"]}},
                        {"image": {"gpus": ["G"], "clouds": ["INVALID"]}},
                        {"other": {"gpus": ["G"], "clouds": ["SECURE"]}}):
            self.assertEqual(self.admin.put(path, json=invalid, headers=self.headers).status_code, 400)
        saved = {"image": {"gpus": ["Custom A", "NVIDIA GeForce RTX 5080", "Custom B"],
                           "clouds": ["COMMUNITY"]}}
        response = self.admin.put(path, json=saved, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["families"]["image"]["configured"])
        self.assertEqual(S.load(), saved)
        tiers = self.admin.get("/api/runpod/tiers").json()["tiers"]
        self.assertEqual(tiers[0]["gpus"], ["Custom A", "Custom B"])
        self.assertEqual(tiers[0]["clouds"], ["COMMUNITY"])
        self.assertFalse(tiers[1]["configured"])

    def test_catalog_fallback_and_creation_order(self):
        S.save({"image": {"gpus": ["Custom A", "NVIDIA GeForce RTX 5080", "Custom B"],
                          "clouds": ["COMMUNITY"]}})
        with patch.object(R, "RUNPOD_API_KEY", ""):
            catalog = self.admin.get("/api/settings/runpod-gpus/catalog")
        self.assertEqual(catalog.status_code, 200, catalog.text)
        self.assertTrue(catalog.json()["error"])
        self.assertIn("Custom A", [g["id"] for g in catalog.json()["gpus"]])
        self.assertIsNone(next(g for g in catalog.json()["gpus"] if g["id"] == "Custom A")["securePrice"])
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        requests = []
        def fake_urlopen(request, timeout=None):
            requests.append(request)
            return Response(json.dumps({"data": {"gpuTypes": [{"id": "New GPU", "displayName": "New GPU",
                                                      "memoryInGb": 48, "secureCloud": True,
                                                      "communityCloud": False, "securePrice": 0.65,
                                                      "communityPrice": 0.45}]}}).encode())
        with patch.object(R, "RUNPOD_API_KEY", secrets.token_urlsafe(12)), patch.object(R.urllib.request, "urlopen", fake_urlopen):
            live = self.admin.get("/api/settings/runpod-gpus/catalog").json()
        self.assertIsNone(live["error"])
        self.assertEqual(live["gpus"][0]["memoryInGb"], 48)
        self.assertEqual(live["gpus"][0]["securePrice"], 0.65)
        self.assertEqual(live["gpus"][0]["communityPrice"], 0.45)
        self.assertEqual(requests[0].full_url, R.RUNPOD_GRAPHQL_URL)
        self.assertIn(b"gpuTypes", requests[0].data)
        self.assertIn(b"securePrice communityPrice", requests[0].data)
        def fake_urlopen_without_prices(request, timeout=None):
            return Response(json.dumps({"data": {"gpuTypes": [{"id": "No Price GPU"}]}}).encode())
        with patch.object(R, "RUNPOD_API_KEY", secrets.token_urlsafe(12)), patch.object(R.urllib.request, "urlopen", fake_urlopen_without_prices):
            missing = self.admin.get("/api/settings/runpod-gpus/catalog")
        self.assertEqual(missing.status_code, 200, missing.text)
        self.assertIsNone(missing.json()["error"])
        self.assertIsNone(missing.json()["gpus"][0]["securePrice"])
        self.assertIsNone(missing.json()["gpus"][0]["communityPrice"])
        seen = []
        error = "HTTP 400: There are no longer any instances available with the requested specifications."
        def fake_rest(method, path, timeout=0, body=None, *, key):
            seen.append(body)
            return None, error
        with patch.object(R, "_rest", fake_rest):
            pod, reason = R.create_pod("test", "image", key="k")
        self.assertIsNone(pod)
        self.assertIn("재고 없음", reason)
        self.assertEqual([item["gpu"]["id"] for item in seen], ["Custom A", "Custom B"] * 2)
        self.assertEqual([item["cloud"] for item in seen], ["COMMUNITY"] * 4)
        self.assertEqual([item.get("dataCenterIds") for item in seen], [["AP-JP-1"]] * 2 + [None] * 2)
        self.assertTrue(all(item["gpu"]["minCudaVersion"] == "13.0" for item in seen))

    def test_cloud_priority_survives_reload_and_drives_creation(self):
        path = "/api/settings/runpod-gpus"
        saved = {"image": {"gpus": ["Priority GPU"], "clouds": ["COMMUNITY", "SECURE"]}}
        response = self.admin.put(path, json=saved, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["families"]["image"]["clouds"], saved["image"]["clouds"])
        self.assertEqual(self.admin.get(path).json()["families"]["image"]["clouds"], saved["image"]["clouds"])
        seen = []
        def fake_rest(method, path, timeout=0, body=None, *, key):
            seen.append(body["cloud"])
            return None, "HTTP 400: There are no longer any instances available with the requested specifications."
        with patch.object(R, "_rest", fake_rest):
            pod, _reason = R.create_pod("test", "image", key="k")
        self.assertIsNone(pod)
        self.assertEqual(seen, ["COMMUNITY", "COMMUNITY", "SECURE", "SECURE"])


if __name__ == "__main__":
    unittest.main()
