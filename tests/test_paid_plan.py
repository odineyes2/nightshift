"""유료 회원(경로 2) 검사 — 관리자가 기간·한도를 정하면 charchat 작업이 관리자 파드에서 돌고, 관리자 파드 정보는 회원에게 보이지 않는다.
가짜 RunPod만 쓴다(실제 파드를 만들지 않음). 실행: python tests/test_paid_plan.py"""
import asyncio
import os
import secrets
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for part in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{part}_DIR"] = str(Path(TMP.name) / part.lower())
PASSWORD = secrets.token_urlsafe(18)
os.environ["NIGHTSHIFT_ADMIN_USER"] = "admin"
os.environ["NIGHTSHIFT_ADMIN_PASSWORD"] = PASSWORD
os.environ.pop("NIGHTSHIFT_PUBLIC_URL", None)
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")

import app as A  # noqa: E402
import auto_power_settings as AP  # noqa: E402
import db  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

R, P, ST = A.runpod_api, A.paid_plan, A.service_tokens
ADMIN_KEY, MEMBER_KEY = "env-admin-key", "rpa_member_key"
R.RUNPOD_API_KEY = ADMIN_KEY
created = []


def fake_create(name, tier, env=None, entrypoint=None, *, key):
    created.append((name, tier, key))
    return {"id": f"rp{len(created)}", "name": name}, None


R.create_pod = fake_create
R.terminate_pod = lambda rp, key: None
R.get_runpod_info = lambda url, key: {}
A._log_runpod_sessions_quietly = lambda: None
A.ensure_runtime = lambda pod: None
A.poke_scheduler = lambda: None
A.save_state = lambda: None
H = {"X-Requested-With": "nightshift"}
NOW = datetime(2026, 11, 15, 3, 0, tzinfo=timezone.utc)


def login(name):
    client = TestClient(A.app)
    assert client.post("/api/auth/login", json={"username": name, "password": PASSWORD}, headers=H).status_code == 200
    return client


class PaidPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        A.auth.ensure_admin()
        cls.admin_id = A.auth.admin_id()
        cls.member_id = A.auth.register("reader", "reader@example.test", PASSWORD)["id"]
        with db.connect() as conn:
            conn.execute("UPDATE users SET status='active' WHERE id=?", (cls.member_id,))
        cls.bot = A.auth.get_user(ST.connect("charchat")["user_id"])
        cls.admin, cls.member = login("admin"), login("reader")
        cls.admin_worker = A.pod_registry.create_pod({"kind": "comfyui", "url": "", "name": "adm-img", "enabled": False,
                                                      "note": "runpod:off", "runpod_tier": "image", "owner_id": cls.admin_id})

    def setUp(self):
        created.clear()
        A.jobs.clear()
        AP.FILE.unlink(missing_ok=True); AP.STATE_FILE.unlink(missing_ok=True)
        A._member_auto_path().unlink(missing_ok=True)
        with db.connect() as conn:
            conn.execute("DELETE FROM paid_usage")
            conn.execute("UPDATE users SET paid_until=NULL, paid_daily_images=50, paid_daily_videos=5, runpod_api_key='' WHERE id=?",
                         (self.member_id,))
        A.pod_registry.update_pod(self.admin_worker["id"], {"enabled": False, "url": "", "note": "runpod:off"})
        for pod in A.pod_registry.list_pods(self.member_id):
            A.pod_registry.delete_pod(pod["id"]) if len(A.pod_registry.list_pods(A.pod_registry.ALL)) > 1 else None

    def set_plan(self, **body):
        response = self.admin.put(f"/api/admin/users/{self.member_id}/plan", json=body, headers=H)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["plan"]

    def make_job(self, count=1, tier=None):
        """charchat-bot이 회원 대신 보낸 작업(create_job을 미들웨어와 같은 문맥으로)."""
        async def run():
            u = A.auth.current_user.set(ST.acting_user(self.bot, str(self.member_id)))
            v = A.auth.current_via_service.set(self.bot["id"])
            try:
                return await A.create_job(A.resolve_template("seed_batch"), b'{"1": {"class_type": "SaveImage", "inputs": {}}}',
                                          "w.json", None, None, {"seed_count": str(count)}, user=A.auth.current_user.get(),
                                          parse_inline_options=False, runpod_tier=tier)
            finally:
                A.auth.current_via_service.reset(v)
                A.auth.current_user.reset(u)
        job = asyncio.run(run())
        with A.lock:
            A.jobs[job["id"]]["status"] = "queued"
        return A.jobs[job["id"]]

    def test_admin_sets_plan_with_validation(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        plan = self.set_plan(paid_until=tomorrow, daily_images=20, daily_videos=2)
        self.assertEqual((plan["paid"], plan["paid_until"], plan["daily_images"], plan["daily_videos"]), (True, tomorrow, 20, 2))
        for bad in ({"paid_until": "2026/12/31"}, {"paid_until": "2026-02-30"}, {"daily_images": -1}, {"daily_videos": "5"}):
            self.assertEqual(self.admin.put(f"/api/admin/users/{self.member_id}/plan", json=bad, headers=H).status_code, 400, bad)
        self.assertEqual(self.member.put(f"/api/admin/users/{self.member_id}/plan", json={"paid_until": None}, headers=H).status_code, 403)
        self.assertEqual(self.admin.put(f"/api/admin/users/{self.admin_id}/plan", json={"paid_until": tomorrow}, headers=H).status_code, 400)
        listed = next(u for u in self.admin.get("/api/admin/users").json()["users"] if u["id"] == self.member_id)
        self.assertTrue(listed["plan"]["paid"])
        self.assertFalse(self.set_plan(paid_until=None)["paid"])
        self.assertFalse(self.set_plan(paid_until=(date.today() - timedelta(days=1)).isoformat())["paid"])   # 기간이 지났다

    def test_paid_job_runs_on_admin_pods_and_starts_admin_worker_immediately(self):
        self.set_plan(paid_until=(date.today() + timedelta(days=30)).isoformat())
        job = self.make_job(count=2)
        self.assertEqual(job["billing"], "admin")
        self.assertEqual(ST.pod_owner_ids_for(job), [self.admin_id])          # 관리자 파드에서만
        self.assertEqual(P.plan(self.member_id)["images_today"], 2)
        self.assertEqual(A._member_auto_power_check(NOW), {})                  # 회원 워커는 켜지 않는다
        # 관리자 자동 켜기는 꺼져 있어도 유료 작업이 오면 바로 켠다 — 관리자 키로
        self.assertEqual(A._auto_power_on_check(NOW)["action"], "started")
        self.assertEqual([(c[1], c[2]) for c in created], [("image", ADMIN_KEY)])
        self.assertNotIn(ADMIN_KEY, self.member.get("/api/auth/runpod-status").text)
        AP.save({"paid_immediate": False})                                    # 끄면 기존 규칙(자동 켜기 꺼짐)대로
        A.pod_registry.update_pod(self.admin_worker["id"], {"enabled": False, "url": "", "note": "runpod:off"})
        self.assertEqual(A._auto_power_on_check(NOW)["action"], "off")

    def test_over_limit_goes_to_member_key_or_waits_and_is_promoted_next_day(self):
        self.set_plan(paid_until=(date.today() + timedelta(days=30)).isoformat(), daily_images=3)
        self.assertEqual(self.make_job(count=2)["billing"], "admin")
        over = self.make_job(count=2)                                         # 2+2 > 3 → 관리자 파드 아님
        self.assertIsNone(over["billing"])
        self.assertEqual(ST.pod_owner_ids_for(over), [self.member_id])
        self.assertEqual(A._member_auto_power_check(NOW), {})                  # 키가 없으면 기다린다
        self.member.put("/api/auth/secrets", json={"runpod_api_key": MEMBER_KEY}, headers=H)
        self.assertEqual(A._member_auto_power_check(NOW), {f"{self.member_id}:image": "started"})   # 키가 있으면 경로 1
        self.assertEqual(created[-1][2], MEMBER_KEY)
        # 다음 날: 한도가 다시 생겨 대기 중인(아직 배정 안 된) 작업을 관리자 파드로 옮긴다
        with A.lock:
            over["pod_id"] = None
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        with patch.object(P, "today", lambda: tomorrow):
            self.assertEqual(A._paid_promote_waiting(), 1)
        self.assertEqual(over["billing"], "admin")

    def test_member_becoming_paid_promotes_waiting_jobs_and_delete_refunds(self):
        waiting = self.make_job()
        self.assertIsNone(waiting["billing"])
        self.assertEqual(A._paid_promote_waiting(), 0)
        self.set_plan(paid_until=(date.today() + timedelta(days=7)).isoformat())
        self.assertEqual(A._paid_promote_waiting(), 1)
        self.assertEqual((waiting["billing"], P.plan(self.member_id)["images_today"]), ("admin", 1))
        self.assertEqual(self.member.delete(f"/api/jobs/{waiting['id']}", headers=H).status_code, 200)
        self.assertEqual(P.plan(self.member_id)["images_today"], 0)            # 돌기 전에 지우면 한도를 돌려준다

    def test_member_never_sees_admin_pod_details(self):
        self.set_plan(paid_until=(date.today() + timedelta(days=7)).isoformat())
        job = self.make_job()
        with A.lock:   # 관리자 파드에 배정돼 돌았다고 치고
            job.update(pod_id=self.admin_worker["id"], pod_name="adm-img", pod_gpu="NVIDIA RTX A4000", pod_cost_per_hr=0.25,
                       status="failed", waiting_reason="https://abc123-8188.proxy.runpod.net 연결 실패")
        (A.LOGS_DIR / f"{job['id']}.log").write_text("POST https://abc123-8188.proxy.runpod.net/prompt failed\n", encoding="utf-8")
        mine = next(j for j in self.member.get("/api/jobs").json()["jobs"] if j["id"] == job["id"])
        self.assertEqual((mine["pod_id"], mine["pod_name"], mine["pod_gpu"], mine["pod_cost_per_hr"]), (None, "관리자 워커", None, None))
        self.assertNotIn("proxy.runpod.net", mine["waiting_reason"])
        log = self.member.get(f"/api/jobs/{job['id']}/log").json()["log"]
        self.assertNotIn("proxy.runpod.net", log)
        self.assertIn("[관리자 워커]", log)
        whole = self.member.get("/api/jobs").text + self.member.get("/api/pods").text + self.member.get("/api/auth/runpod-status").text
        for secret in (ADMIN_KEY, "abc123", "adm-img"):
            self.assertNotIn(secret, whole)
        self.assertEqual(self.member.post(f"/api/jobs/{job['id']}/move", json={"pod_id": self.admin_worker["id"]}, headers=H).status_code, 404)
        # 관리자는 그대로 본다
        theirs = next(j for j in self.admin.get("/api/jobs").json()["jobs"] if j["id"] == job["id"])
        self.assertEqual(theirs["pod_name"], "adm-img")
        self.assertIn("proxy.runpod.net", self.admin.get(f"/api/jobs/{job['id']}/log").json()["log"])

    def test_status_shows_plan_to_member(self):
        self.set_plan(paid_until=(date.today() + timedelta(days=7)).isoformat(), daily_images=10)
        self.make_job(count=3)
        plan = self.member.get("/api/auth/runpod-status").json()["plan"]
        self.assertEqual((plan["paid"], plan["images_today"], plan["daily_images"]), (True, 3, 10))

    def test_direct_nightshift_jobs_are_never_paid(self):
        self.set_plan(paid_until=(date.today() + timedelta(days=7)).isoformat())

        async def run():
            member = A.auth.get_user(self.member_id)
            return await A.create_job(A.resolve_template("seed_batch"), b'{"1": {"class_type": "SaveImage", "inputs": {}}}',
                                      "w.json", None, None, {}, user=member, parse_inline_options=False)
        self.assertIsNone(asyncio.run(run())["billing"])


if __name__ == "__main__":
    unittest.main()
