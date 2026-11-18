"""회원 워커 자동 켜기(07d) 검사 — 키를 저장하면 꺼진 워커 기록, 연동 작업이 대기하면 그 회원 키로 파드, 하루 상한·실패 다시 시도·실패 이유.
가짜 RunPod만 쓴다(실제 파드를 만들지 않음). 실행: python tests/test_member_auto_power.py"""
import os
import secrets
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
import db  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

R = A.runpod_api
R.RUNPOD_API_KEY = "env-admin-key"
MEMBER_KEY = "rpa_member_own_key"
created, fail = [], {"err": None}


def fake_create(name, tier, env=None, entrypoint=None, *, key):
    if fail["err"]:
        return None, fail["err"]
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


class MemberAutoPowerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        A.auth.ensure_admin()
        cls.admin_id = A.auth.admin_id()
        cls.member_id = A.auth.register("reader", "reader@example.test", PASSWORD)["id"]
        cls.nokey_id = A.auth.register("nokey", "nokey@example.test", PASSWORD)["id"]
        with db.connect() as conn:
            conn.execute("UPDATE users SET status='active' WHERE id IN (?, ?)", (cls.member_id, cls.nokey_id))
        cls.bot_id = A.auth.get_user(A.service_tokens.connect("charchat")["user_id"])["id"]
        cls.member = login("reader")

    def setUp(self):
        created.clear(); fail["err"] = None
        A.jobs.clear()
        A._member_auto_path().unlink(missing_ok=True)
        for pod in A.pod_registry.list_pods(self.member_id) + A.pod_registry.list_pods(self.nokey_id):
            try:
                A.pod_registry.update_pod(pod["id"], {"enabled": False, "url": "", "note": "runpod:off"})   # 켜진 워커는 지워지지 않는다
                A.pod_registry.delete_pod(pod["id"])
            except A.pod_registry.PodError:   # 마지막 워커는 지울 수 없다 — 자동 켜기 대상에서만 뺀다
                A.pod_registry.update_pod(pod["id"], {"runpod_tier": ""})

    def queue(self, owner, n=1, via=True, tier=None):
        for _ in range(n):
            jid = f"j{len(A.jobs)}"
            A.jobs[jid] = {"id": jid, "status": "queued", "pod_id": None, "deleted": False, "owner_id": owner,
                           "via_service": self.bot_id if via else None, "runpod_tier": tier, "queued_at": NOW.isoformat()}

    def save_key(self, key=MEMBER_KEY):
        response = self.member.put("/api/auth/secrets", json={"runpod_api_key": key}, headers=H)
        self.assertEqual(response.status_code, 200, response.text)

    def test_saving_key_prepares_an_off_image_worker(self):
        self.assertEqual(A._member_workers(self.member_id, "image"), [])
        self.save_key()
        workers = A._member_workers(self.member_id, "image")
        self.assertEqual(len(workers), 1)
        w = workers[0]
        self.assertEqual((w["owner_id"], w["enabled"], w["url"], w["note"], w["auto_power_off"], w["auto_off_minutes"]),
                         (self.member_id, False, "", "runpod:off", True, 15))
        self.assertEqual(created, [])          # 파드는 만들지 않는다(돈이 들지 않는다)
        self.save_key()                        # 다시 저장해도 하나만
        self.assertEqual(len(A._member_workers(self.member_id, "image")), 1)

    def test_waiting_charchat_job_starts_member_pod_with_member_key(self):
        self.save_key()
        self.queue(self.member_id)
        self.assertEqual(A._member_auto_power_check(NOW), {f"{self.member_id}:image": "started"})
        self.assertEqual(len(created), 1)
        self.assertEqual((created[0][1], created[0][2]), ("image", MEMBER_KEY))   # 회원 자기 키로
        worker = A._member_workers(self.member_id, "image")[0]
        self.assertTrue(worker["enabled"])
        self.assertEqual(A._member_auto_power_check(NOW)[f"{self.member_id}:image"], "on")   # 켜져 있으면 또 만들지 않는다
        self.assertEqual(len(created), 1)

    def test_video_job_gets_its_own_video_worker(self):
        self.save_key()
        self.queue(self.member_id, tier="video")
        self.assertEqual(A._member_auto_power_check(NOW), {f"{self.member_id}:video": "started"})
        self.assertEqual(created[0][1], "video")
        self.assertEqual(len(A._member_workers(self.member_id, "video")), 1)

    def test_who_is_not_auto_started(self):
        self.queue(self.nokey_id)                       # 키 없는 회원은 키를 넣을 때까지 기다린다
        self.queue(self.admin_id)                       # 관리자는 07c가 맡는다
        self.save_key()
        self.queue(self.member_id, via=False)           # nightshift를 직접 쓰는 작업은 회원이 워커를 관리한다
        self.assertEqual(A._member_auto_power_check(NOW), {})
        self.assertEqual(created, [])
        self.assertEqual(A.pod_registry.list_pods(self.nokey_id), [])

    def test_daily_limit_and_failure_backoff_with_reason(self):
        self.save_key()
        self.queue(self.member_id)
        fail["err"] = "파드를 만들지 못했어요 — SECURE·A4000: RunPod가 거절했어요(HTTP 402): insufficient balance"
        self.assertEqual(A._member_auto_power_check(NOW)[f"{self.member_id}:image"], "failed")
        status = self.member.get("/api/auth/runpod-status").json()
        self.assertIn("잔액이 부족", status["tiers"]["image"]["last_error"])
        self.assertTrue(status["tiers"]["image"]["retry_after"])
        self.assertEqual(status["tiers"]["image"]["waiting"], 1)
        self.assertEqual(A._member_auto_power_check(NOW + timedelta(minutes=5))[f"{self.member_id}:image"], "backoff")
        fail["err"] = None
        moment = NOW + timedelta(minutes=11)
        self.assertEqual(self.member.put("/api/auth/runpod-auto-limit", json={"daily": 3}, headers=H).status_code, 200)
        for _ in range(3):   # 회원이 정한 하루 3번
            self.assertEqual(A._member_auto_power_check(moment)[f"{self.member_id}:image"], "started")
            w = A._member_workers(self.member_id, "image")[0]
            A.pod_registry.update_pod(w["id"], {"enabled": False, "url": "", "note": "runpod:off"})   # 자동 끄기가 껐다고 치고
            moment += timedelta(minutes=1)
        self.assertEqual(A._member_auto_power_check(moment)[f"{self.member_id}:image"], "limit")
        self.assertIsNone(self.member.get("/api/auth/runpod-status").json()["tiers"]["image"]["last_error"])   # 성공하면 지운다

    def off(self):
        w = A._member_workers(self.member_id, "image")[0]
        A.pod_registry.update_pod(w["id"], {"enabled": False, "url": "", "note": "runpod:off"})

    def test_member_sets_daily_start_limit(self):
        self.save_key()
        status = self.member.get("/api/auth/runpod-status").json()
        self.assertEqual((status["daily_limit"], status["daily_limit_custom"]), (10, False))   # 기본 10번
        for bad in ({"daily": -1}, {"daily": 101}, {"daily": "5"}, {"daily": True}, {"other": 1}):
            self.assertEqual(self.member.put("/api/auth/runpod-auto-limit", json=bad, headers=H).status_code, 422, bad)
        r = self.member.put("/api/auth/runpod-auto-limit", json={"daily": 1}, headers=H)
        self.assertEqual((r.json()["daily_limit"], r.json()["daily_limit_custom"]), (1, True))
        now = datetime.now(timezone.utc)
        self.queue(self.member_id)
        self.assertEqual(A._member_auto_power_check(now)[f"{self.member_id}:image"], "started")
        self.off()
        self.assertEqual(A._member_auto_power_check(now)[f"{self.member_id}:image"], "limit")
        image = self.member.get("/api/auth/runpod-status").json()["tiers"]["image"]
        self.assertEqual((image["starts_today"], image["limit_reached"], image["waiting"]), (1, True, 1))   # 화면이 이유를 보여 줄 수 있다
        self.member.put("/api/auth/runpod-auto-limit", json={"daily": 0}, headers=H)   # 무제한
        for _ in range(12):
            self.assertEqual(A._member_auto_power_check(now)[f"{self.member_id}:image"], "started")
            self.off()
        self.assertFalse(self.member.get("/api/auth/runpod-status").json()["tiers"]["image"]["limit_reached"])
        r = self.member.put("/api/auth/runpod-auto-limit", json={"daily": None}, headers=H)   # 기본값으로
        self.assertEqual((r.json()["daily_limit"], r.json()["daily_limit_custom"]), (10, False))
        self.assertEqual(A._member_auto_power_check(now)[f"{self.member_id}:image"], "limit")   # 오늘 13번 > 10
        admin = login("admin")
        self.assertEqual(admin.put("/api/auth/runpod-auto-limit", json={"daily": 5}, headers=H).status_code, 400)

    def test_manual_worker_on_counts_as_running(self):
        self.save_key()
        A.pod_registry.create_pod({"kind": "comfyui", "url": "https://example.com", "name": "mine", "enabled": True,
                                   "owner_id": self.member_id})
        self.queue(self.member_id)
        self.assertEqual(A._member_auto_power_check(NOW)[f"{self.member_id}:image"], "on")
        self.assertEqual(created, [])

    def test_member_jobs_never_borrow_admin_pods(self):
        job = {"owner_id": self.member_id, "via_service": self.bot_id}
        self.assertEqual(A.service_tokens.pod_owner_ids_for(job), [self.member_id])
        self.assertIn(self.admin_id, A.service_tokens.pod_owner_ids_for({"owner_id": self.bot_id}))   # 연동 계정 자신의 옛 작업만


if __name__ == "__main__":
    unittest.main()
