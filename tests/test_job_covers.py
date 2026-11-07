"""NS-121 잡 목록 대표 결과물: 임시 데이터만 사용하고 서버 수명주기는 시작하지 않는다."""
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


class JobCoverTests(unittest.TestCase):
    def setUp(self):
        A.jobs.clear()
        with A.db.connect() as conn:
            conn.execute("DELETE FROM assets")
        for jid in ("mixed", "video-only", "trashed", "no-assets"):
            self.add(jid)
        self.add("running", "running")
        self.add("failed", "failed")
        self.add("bobs", owner=BOB)
        A.save_state()  # assets.job_id FK — 잡 행이 먼저 있어야 한다
        self.asset("mixed/old.png", "mixed", "image", "2026-01-01T00:00:00")
        self.asset("mixed/new.png", "mixed", "image", "2026-01-02T00:00:00", nsfw=1)
        self.asset("mixed/newest.mp4", "mixed", "video", "2026-01-03T00:00:00")
        self.asset("video-only/a.mp4", "video-only", "video", "2026-01-01T00:00:00")
        self.asset("trashed/gone.png", "trashed", "image", "2026-01-02T00:00:00", deleted=True)
        self.asset("trashed/kept.mp4", "trashed", "video", "2026-01-01T00:00:00")
        self.asset("running/partial.png", "running", "image", "2026-01-01T00:00:00")
        self.asset("failed/partial.png", "failed", "image", "2026-01-01T00:00:00")
        self.asset("bobs/secret.png", "bobs", "image", "2026-01-01T00:00:00")

    def add(self, jid, status="done", owner=ALICE):
        A.jobs[jid] = dict(id=jid, status=status, owner_id=owner["id"], pod_id=None,
                           project_id=None, deleted=False, queued_at=A.now_iso())

    def asset(self, path, job_id, kind, created_at, nsfw=0, deleted=False):
        with A.db.connect() as conn:
            conn.execute("INSERT INTO assets(path, kind, job_id, created_at, nsfw, deleted_at) VALUES(?,?,?,?,?,?)",
                         (path, kind, job_id, created_at, nsfw, created_at if deleted else None))

    def listed(self, user=ALICE):
        async def run():
            cookies = {A.auth.SESSION_COOKIE: A.auth.create_session(user["id"])}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=A.app), base_url="http://test", headers={"x-requested-with": "nightshift"}, cookies=cookies) as client:
                return await client.get("/api/jobs")
        response = asyncio.run(run())
        self.assertEqual(response.status_code, 200, response.text)
        return {j["id"]: j for j in response.json()["jobs"]}

    def test_image_comes_before_newer_video(self):
        self.assertEqual(self.listed()["mixed"]["cover"], {"path": "mixed/new.png", "kind": "image", "nsfw": 1})

    def test_video_is_the_fallback(self):
        self.assertEqual(self.listed()["video-only"]["cover"], {"path": "video-only/a.mp4", "kind": "video", "nsfw": 0})

    def test_deleted_asset_is_skipped(self):
        self.assertEqual(self.listed()["trashed"]["cover"]["path"], "trashed/kept.mp4")

    def test_no_assets_is_null(self):
        job = self.listed()["no-assets"]
        self.assertIn("cover", job)
        self.assertIsNone(job["cover"])

    def test_only_done_jobs_have_cover(self):
        jobs = self.listed()
        self.assertIsNone(jobs["running"]["cover"])
        self.assertIsNone(jobs["failed"]["cover"])

    def test_other_members_job_is_not_listed(self):
        self.assertNotIn("bobs", self.listed())
        self.assertNotIn("mixed", self.listed(BOB))
        self.assertEqual(self.listed(BOB)["bobs"]["cover"]["path"], "bobs/secret.png")

    def test_admin_sees_every_cover_and_owner(self):
        jobs = self.listed(ADMIN)
        self.assertEqual(jobs["bobs"]["cover"]["path"], "bobs/secret.png")
        self.assertEqual(jobs["bobs"]["owner_name"], "bob")

    def test_cover_stays_out_of_live_and_saved_jobs(self):
        for user in (ALICE, ADMIN):
            self.assertIsNotNone(self.listed(user)["mixed"]["cover"])
        self.assertTrue(all("cover" not in job for job in A.jobs.values()))
        A.save_state()
        self.assertTrue(all("cover" not in job for job in A.db.load_jobs().values()))


if __name__ == "__main__":
    unittest.main()
