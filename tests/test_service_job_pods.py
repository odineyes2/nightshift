"""서비스 연동 계정(<서비스>-bot)의 작업은 관리자 파드에서 돌고 그 결과물을 받는다. 일반 회원은 자기 파드만."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")

import app as A
import db
import service_tokens


class Healthy:
    def health(self, pod):
        return {"ok": True}


class ServiceJobPodTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        with db.connect() as conn:
            for name, role in (("pods-admin", "admin"), ("pods-member", "user")):
                conn.execute("INSERT INTO users(username, email, password_hash, role, status, created_at) VALUES(?,?,?,?,?,?)",
                             (name, f"{name}@example.test", "unused", role, "active", db.now_iso()))
            # 이름만 -bot인 일반 회원은 서비스 계정이 아니다.
            conn.execute("INSERT INTO users(username, email, password_hash, role, status, created_at) VALUES(?,?,?,?,?,?)",
                         ("fake-bot", "fake-bot@example.test", "unused", "user", "active", db.now_iso()))
            ids = {row["username"]: row["id"] for row in conn.execute("SELECT id, username FROM users")}
        cls.admin, cls.member, cls.fake = ids["pods-admin"], ids["pods-member"], ids["fake-bot"]
        cls.bot = service_tokens.connect("charchat")["user_id"]
        cls.pods = {cls.admin: [{"id": "adminpod", "kind": "comfyui", "enabled": True, "owner_id": cls.admin, "name": "a"}],
                    cls.member: [{"id": "memberpod", "kind": "comfyui", "enabled": True, "owner_id": cls.member, "name": "m"}]}

    def test_owner_rule(self):
        self.assertTrue(service_tokens.is_service_user(self.bot))
        self.assertFalse(service_tokens.is_service_user(self.fake))
        self.assertEqual(service_tokens.pod_owner_ids_for_job(self.member), [self.member])
        self.assertIn(self.admin, service_tokens.pod_owner_ids_for_job(self.bot))
        self.assertEqual(service_tokens.pod_owner_ids_for_job(None), [None])

    def schedule(self, owner):
        job = {"id": f"job-{owner}", "status": "queued", "owner_id": owner, "queued_at": "1", "deleted": False}
        dispatched = []
        with patch.dict(A.jobs, {job["id"]: job}, clear=True), \
             patch.object(A.pod_registry, "list_pods", side_effect=lambda o=None: list(self.pods.get(o, []))), \
             patch.object(A, "driver_for", return_value=Healthy()), \
             patch.object(A, "job_missing_on_pod", return_value=[]), \
             patch.object(A, "_snapshot_pod_into_job"), patch.object(A, "save_state"), \
             patch.object(A, "dispatch_job", side_effect=lambda job_id, pod_id: dispatched.append(pod_id)):
            A.schedule_once()
        return dispatched, job

    def test_service_job_runs_on_admin_pod(self):
        dispatched, job = self.schedule(self.bot)
        self.assertEqual(dispatched, ["adminpod"])
        self.assertIsNone(job["waiting_reason"])

    def test_member_and_lookalike_keep_own_pods_only(self):
        self.assertEqual(self.schedule(self.member)[0], ["memberpod"])
        dispatched, job = self.schedule(self.fake)
        self.assertEqual(dispatched, [])
        self.assertIn("워커가 없어요", job["waiting_reason"])

    def test_outputs_of_service_job_are_pulled_from_admin_pod(self):
        jobs = {"svc": {"owner_id": self.bot}, "mine": {"owner_id": self.member}}
        pods = {"adminpod": {"owner_id": self.admin}, "memberpod": {"owner_id": self.member}}
        with patch.dict(A.jobs, jobs, clear=True), patch.object(A.pod_registry, "get_pod", side_effect=pods.get):
            self.assertEqual(A._sync_key_mapper({"subfolder": "svc", "key": "svc/a.png"}, "adminpod"), "svc/a.png")
            self.assertIsNone(A._sync_key_mapper({"subfolder": "svc", "key": "svc/a.png"}, "memberpod"))
            self.assertIsNone(A._sync_key_mapper({"subfolder": "mine", "key": "mine/a.png"}, "adminpod"))


if __name__ == "__main__":
    unittest.main()
