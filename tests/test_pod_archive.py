"""워커 보관 상태의 저장·호환성과 로그인 API 권한을 임시 데이터로 검증한다."""
import copy
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for part in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{part}_DIR"] = str(Path(TMP.name) / part.lower())
os.environ["NIGHTSHIFT_PODS_FILE"] = str(Path(TMP.name) / "pods.json")
LOGIN_CREDENTIAL = secrets.token_urlsafe(18)
os.environ["NIGHTSHIFT_ADMIN_USER"] = "admin"
os.environ["NIGHTSHIFT_ADMIN_PASSWORD"] = LOGIN_CREDENTIAL
os.environ.pop("NIGHTSHIFT_PUBLIC_URL", None)
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")

import app as A
import db
from fastapi.testclient import TestClient

P = A.pod_registry
H = {"X-Requested-With": "nightshift"}
INVALID = (None, 0, 1, -1, 0.0, "", "false", "true", [], {})


class PodArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        A.auth.ensure_admin()
        cls.owner = A.auth.register("owner", "owner@example.test", LOGIN_CREDENTIAL)["id"]
        cls.other = A.auth.register("other", "other@example.test", LOGIN_CREDENTIAL)["id"]
        with db.connect() as conn:
            conn.execute("UPDATE users SET status='active' WHERE id IN (?, ?)", (cls.owner, cls.other))
        cls.clients = {}
        for name in ("admin", "owner", "other"):
            client = TestClient(A.app)  # lifespan 없이 실행: 워커·동기화 스레드를 시작하지 않는다.
            login = {"username": name}
            login["password"] = LOGIN_CREDENTIAL
            response = client.post("/api/auth/login", json=login, headers=H)
            assert response.status_code == 200, response.text
            cls.clients[name] = client

    @classmethod
    def tearDownClass(cls):
        for client in cls.clients.values():
            client.close()

    def setUp(self):
        P._pods.clear()
        A.jobs.clear()
        self.pod = P.create_pod({"name": "worker", "owner_id": self.owner, "enabled": True,
                                 "url": "https://example.test", "auto_power_off": False})
        self.patches = [
            patch.object(A, "ensure_runtime"),
            patch.object(A, "poke_scheduler"),
            patch.object(A, "pod_status", new=AsyncMock(return_value={"connected": False})),
            patch.object(A, "pod_card_data", return_value={}),
            patch.object(A, "_boot_info", return_value={}),
            patch.object(A, "recent_job_images", return_value=[]),
            patch("requests.sessions.Session.request", side_effect=AssertionError("unexpected external request")),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def put(self, name, value):
        return self.clients[name].put(f"/api/pods/{self.pod['id']}", json=value, headers=H)

    def test_legacy_default_and_strict_boolean(self):
        self.assertIs(P.normalize_pod({"name": "old"})["archived"], False)
        P.PODS_FILE.write_text(json.dumps({"pods": [{"id": "legacy", "name": "old"}]}), encoding="utf-8")
        P.load()
        self.assertIs(P.require_pod("legacy")["archived"], False)
        for value in INVALID:
            with self.subTest(value=value):
                with self.assertRaises(P.PodError):
                    P.normalize_pod({"name": "bad", "archived": value})
                before = P.PODS_FILE.read_bytes()
                with self.assertRaises(P.PodError):
                    P.create_pod({"name": "bad", "archived": value})
                self.assertEqual(P.PODS_FILE.read_bytes(), before)
        for value in (True, False):
            pod = P.create_pod({"name": "valid", "archived": value})
            P.load()
            self.assertIs(P.require_pod(pod["id"])["archived"], value)

    def test_archive_restore_and_partial_sync_updates(self):
        pod_id = self.pod["id"]
        archived = P.update_pod(pod_id, {"archived": True})
        for key in self.pod.keys() - {"archived", "updated_at"}:
            self.assertEqual(archived[key], self.pod[key], key)
        P.update_pod(pod_id, {"name": "renamed"})
        for enabled in (False, True):
            P.update_pod(pod_id, {"enabled": enabled, "note": "runpod:sync"})
            P.load()
            self.assertIs(P.require_pod(pod_id)["archived"], True)
            self.assertIs(P.require_pod(pod_id)["enabled"], enabled)
        P.update_pod(pod_id, {"archived": False})
        P.load()
        self.assertIs(P.require_pod(pod_id)["archived"], False)

    def test_all_archived_remain_and_defaults_do_not_change(self):
        second = P.create_pod({"name": "second", "owner_id": self.owner, "enabled": False})
        ids = {self.pod["id"], second["id"]}
        for pod_id in ids:
            P.update_pod(pod_id, {"archived": True})
        P.load()
        self.assertEqual({p["id"] for p in P.list_pods()}, ids)
        self.assertTrue(all(p["archived"] for p in P.list_pods(self.owner)))
        self.assertEqual(P.default_pod()["id"], self.pod["id"])
        self.assertEqual(P.default_pod_for(self.owner)["id"], self.pod["id"])

    def test_api_permissions_validation_and_payloads(self):
        for value in INVALID:
            with self.subTest(value=value):
                before = P.PODS_FILE.read_bytes()
                self.assertEqual(self.put("owner", {"archived": value}).status_code, 400)
                self.assertEqual(P.PODS_FILE.read_bytes(), before)
        self.assertEqual(self.put("other", {"archived": True}).status_code, 404)
        self.assertFalse(P.require_pod(self.pod["id"])["archived"])
        for name, value in (("owner", True), ("admin", False), ("admin", True), ("owner", False)):
            response = self.put(name, {"archived": value})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIs(response.json()["archived"], value)
            P.load()
            self.assertIs(P.require_pod(self.pod["id"])["archived"], value)
            for viewer in ("owner", "admin", "other"):
                for path in ("/api/pods", "/api/pods/summary"):
                    response = self.clients[viewer].get(path)
                    self.assertEqual(response.status_code, 200, response.text)
                    pods = response.json()["pods"]
                    if viewer == "other":
                        self.assertEqual(pods, [])
                    else:
                        self.assertEqual(len(pods), 1)
                        self.assertEqual(pods[0]["id"], self.pod["id"])
                        self.assertIs(pods[0]["archived"], value)
                        self.assertTrue(pods[0]["enabled"])

    def test_api_preserves_jobs_assets_and_worker_settings(self):
        pod_id = self.pod["id"]
        job = {"id": "archive-job", "pod_id": pod_id, "pod_name": self.pod["name"],
               "status": "pending", "owner_id": self.owner}
        A.jobs[job["id"]] = copy.deepcopy(job)
        db.save_jobs(A.jobs)
        with db.connect() as conn:
            conn.execute("INSERT INTO assets(path,kind,job_id,created_at,origin_pod_id) VALUES(?,?,?,?,?)",
                         ("archive.png", "image", job["id"], "2026-11-17", pod_id))
            before_jobs = [tuple(row) for row in conn.execute("SELECT * FROM jobs")]
            before_assets = [tuple(row) for row in conn.execute("SELECT * FROM assets")]
        for archived in (True, False):
            response = self.put("owner", {"archived": archived})
            self.assertEqual(response.status_code, 200, response.text)
            for key in self.pod.keys() - {"archived", "updated_at"}:
                self.assertEqual(response.json()[key], self.pod[key], key)
            self.assertEqual(A.jobs[job["id"]], job)
            with db.connect() as conn:
                self.assertEqual([tuple(row) for row in conn.execute("SELECT * FROM jobs")], before_jobs)
                self.assertEqual([tuple(row) for row in conn.execute("SELECT * FROM assets")], before_assets)


if __name__ == "__main__":
    unittest.main()
