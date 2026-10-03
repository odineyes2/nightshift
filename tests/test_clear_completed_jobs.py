"""NS-28 완료 청소 API: 임시 데이터만 사용하고 서버 수명주기는 시작하지 않는다."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
os.environ["NIGHTSHIFT_ADMIN_USER"] = "admin"
os.environ["NIGHTSHIFT_ADMIN_PASSWORD"] = "Test-Passw0rd-xyz!"
os.environ.pop("NIGHTSHIFT_PUBLIC_URL", None)
sys.path.insert(0, str(ROOT / "server"))

import app as A
import httpx

A.db.init()
ADMIN = A.auth.ensure_admin()
ALICE = A.auth.register("alice", "alice@example.test", os.environ["NIGHTSHIFT_ADMIN_PASSWORD"])
BOB = A.auth.register("bob", "bob@example.test", os.environ["NIGHTSHIFT_ADMIN_PASSWORD"])
with A.db.connect() as conn:
    conn.execute("UPDATE users SET status='active'")


class ClearCompletedTests(unittest.TestCase):
    def setUp(self):
        self.user = ALICE
        A.jobs.clear()
        for status in ("done", "failed", "interrupted", "pending", "queued", "running"):
            self.add(status, status)
        self.add("other-owner", owner="bob")
        self.add("other-pod", pod="p2")
        self.add("other-project", project=2)
        self.add("unassigned", project=None)
        self.add("already-deleted", deleted=True)

    def add(self, jid, status="done", owner="alice", pod="p1", project=1, deleted=False):
        owner_id = ALICE["id"] if owner == "alice" else BOB["id"]
        A.jobs[jid] = dict(id=jid, status=status, owner_id=owner_id, pod_id=pod,
                          project_id=project, deleted=deleted, queued_at=A.now_iso())

    def call(self, query=""):
        # 인증 미들웨어는 유지하고 임시 관리자 세션을 사용한다.
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=A.app), base_url="http://test", headers={"x-requested-with": "nightshift"}) as client:
                login = await client.post("/api/auth/login", json={"username": self.user["username"], "password": os.environ["NIGHTSHIFT_ADMIN_PASSWORD"]})
                self.assertEqual(login.status_code, 200, login.text)
                return await client.post("/api/jobs/clear-completed" + query)
        return asyncio.run(run())

    def deleted(self):
        return {jid for jid, job in A.jobs.items() if job.get("deleted")}

    def test_done_only_scope_repeat_and_persistence(self):
        response = self.call("?done_only=true&pod_id=p1&project_id=1")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"cleared": 1})
        self.assertEqual(self.deleted(), {"done", "already-deleted"})
        self.assertTrue(A.jobs["done"]["deleted_at"])
        self.assertTrue(A.db.load_jobs()["done"]["deleted"])
        self.assertEqual(self.call("?done_only=true&pod_id=p1&project_id=1").json(), {"cleared": 0})

    def test_unassigned(self):
        self.assertEqual(self.call("?done_only=true&project_id=unassigned").json(), {"cleared": 1})
        self.assertEqual(self.deleted(), {"unassigned", "already-deleted"})

    def test_admin_matches_all_owners(self):
        self.user = ADMIN
        self.assertEqual(self.call("?done_only=true&pod_id=p1&project_id=1").json(), {"cleared": 2})
        self.assertEqual(self.deleted(), {"done", "other-owner", "already-deleted"})

    def test_legacy_default(self):
        self.assertEqual(self.call("?pod_id=p1&project_id=1").json(), {"cleared": 3})
        self.assertEqual(self.deleted(), {"done", "failed", "interrupted", "already-deleted"})

    def test_whole_scope_and_empty(self):
        self.assertEqual(self.call("?done_only=true").json(), {"cleared": 4})
        self.assertNotIn("other-owner", self.deleted())
        self.assertEqual(self.call("?done_only=true&pod_id=missing").json(), {"cleared": 0})

    def test_invalid_parameter_preserves_jobs(self):
        for query in ("?done_only=invalid", "?done_only=true&project_id=invalid"):
            self.assertIn(self.call(query).status_code, (400, 422))
            self.assertEqual(self.deleted(), {"already-deleted"})

    def test_unauthenticated(self):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=A.app), base_url="http://test", headers={"x-requested-with": "nightshift"}) as client:
                return await client.post("/api/jobs/clear-completed?done_only=true")
        self.assertEqual(asyncio.run(run()).status_code, 401)
        self.assertEqual(self.deleted(), {"already-deleted"})


if __name__ == "__main__":
    unittest.main()
