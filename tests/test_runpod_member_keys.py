"""회원별 RunPod 키 검사 — RunPod 호출은 워커 주인의 계정 설정 키로, 일반 회원에게 관리자(.env) 키를 빌려 주지 않는다.
가짜 RunPod만 쓴다(실제 파드를 만들지 않음). 실행: python tests/test_runpod_member_keys.py"""
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
import auto_power_settings as AP  # noqa: E402
import db  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

R = A.runpod_api
ENV_KEY, MEMBER_KEY = "env-admin-key", "member-own-key"
R.RUNPOD_API_KEY = ENV_KEY

created, terminated, actions = [], [], []   # (이름, 키) / (파드, 키) / (파드, 동작, 키)


def fake_create(name, tier, env=None, entrypoint=None, *, key):
    created.append((name, key))
    return {"id": f"rp{len(created)}", "name": name}, None


R.create_pod = fake_create
R.terminate_pod = lambda rp, key: terminated.append((rp, key))
R.pod_action = lambda rp, action, key: (actions.append((rp, action, key)), ("RUNNING" if action == "start" else "EXITED", None))[1]
R.get_runpod_info = lambda url, key: {}
A._log_runpod_sessions_quietly = lambda: None
A.ensure_runtime = lambda pod: None
A.poke_scheduler = lambda: None
A.save_state = lambda: None
H = {"X-Requested-With": "nightshift"}


def drop(pod_id):
    """마지막 워커는 지울 수 없으니(기본 워커 규칙) 지우지 못하면 꺼 둔다."""
    try:
        A.pod_registry.delete_pod(pod_id)
    except A.pod_registry.PodError:
        A.pod_registry.update_pod(pod_id, {"enabled": False, "url": "", "runpod_tier": None, "note": ""})


def login(name):
    client = TestClient(A.app)
    response = client.post("/api/auth/login", json={"username": name, "password": PASSWORD}, headers=H)
    assert response.status_code == 200, response.text
    return client


class MemberRunPodKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        A.auth.ensure_admin()
        cls.admin_id = A.auth.admin_id()
        cls.member_id = A.auth.register("member", "member@example.test", PASSWORD)["id"]
        cls.other_id = A.auth.register("other", "other@example.test", PASSWORD)["id"]
        with db.connect() as conn:
            conn.execute("UPDATE users SET status='active' WHERE id IN (?, ?)", (cls.member_id, cls.other_id))
        cls.admin, cls.member = login("admin"), login("member")

    @classmethod
    def tearDownClass(cls):
        cls.admin.close()
        cls.member.close()

    def setUp(self):
        created.clear(); terminated.clear(); actions.clear()
        A.auth.set_secrets(self.member_id, {"runpod_api_key": ""})
        A.auth.set_secrets(self.admin_id, {"runpod_api_key": ""})
        A.jobs.clear()

    def test_key_for_owner(self):
        self.assertEqual(R.key_for_owner(self.admin_id), ENV_KEY)        # 관리자: 비어 있으면 .env
        self.assertEqual(R.key_for_owner(None), ENV_KEY)                 # 주인 없음 = 관리자 것
        self.assertEqual(R.key_for_owner(self.member_id), "")            # 일반 회원: .env를 빌려 주지 않는다
        self.assertEqual(R.key_for_owner(999999), "")
        A.auth.set_secrets(self.member_id, {"runpod_api_key": MEMBER_KEY})
        self.assertEqual(R.key_for_owner(self.member_id), MEMBER_KEY)
        A.auth.set_secrets(self.admin_id, {"runpod_api_key": "admin-own"})
        self.assertEqual(R.key_for_owner(self.admin_id), "admin-own")    # 관리자도 자기 키가 있으면 그것
        with db.connect() as conn:
            conn.execute("UPDATE users SET status='disabled' WHERE id=?", (self.member_id,))
        try:
            self.assertEqual(R.key_for_owner(self.member_id), "")        # 정지된 회원의 키는 쓰지 않는다
        finally:
            with db.connect() as conn:
                conn.execute("UPDATE users SET status='active' WHERE id=?", (self.member_id,))

    def test_member_creates_and_powers_own_worker_with_own_key(self):
        response = self.member.post("/api/pods/runpod-create", json={"tier": "image"}, headers=H)
        self.assertEqual(response.status_code, 400, response.text)       # 키가 없으면 만들지 않는다
        self.assertEqual(created, [])
        self.assertFalse(self.member.get("/api/runpod/tiers").json()["api_key"])

        A.auth.set_secrets(self.member_id, {"runpod_api_key": MEMBER_KEY})
        self.assertTrue(self.member.get("/api/runpod/tiers").json()["api_key"])
        response = self.member.post("/api/pods/runpod-create", json={"tier": "image"}, headers=H)
        self.assertEqual(response.status_code, 200, response.text)
        worker = response.json()
        self.assertEqual(worker["owner_id"], self.member_id)
        self.assertEqual(created, [(f"nightshift-{worker['id']}", MEMBER_KEY)])
        self.assertNotIn(MEMBER_KEY, response.text)

        # 끄기(지우기)·켜기도 회원 자기 키로
        response = self.member.post(f"/api/pods/{worker['id']}/runpod/stop", headers=H)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(terminated, [("rp1", MEMBER_KEY)])
        response = self.member.post(f"/api/pods/{worker['id']}/runpod/start", headers=H)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(created[-1][1], MEMBER_KEY)

        # 관리자는 회원 워커를 켤 수 없고(회원 계정 과금), 끄기는 회원 키로 한다
        response = self.admin.post(f"/api/pods/{worker['id']}/runpod/start", headers=H)
        self.assertEqual(response.status_code, 403, response.text)
        response = self.admin.post(f"/api/pods/{worker['id']}/runpod/stop", headers=H)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(terminated[-1][1], MEMBER_KEY)
        drop(worker["id"])

    def test_member_cannot_touch_admin_worker(self):
        w = A.pod_registry.create_pod({"kind": "comfyui", "url": "", "name": "adm", "enabled": False,
                                       "note": "runpod:off", "runpod_tier": "image", "owner_id": self.admin_id})
        A.auth.set_secrets(self.member_id, {"runpod_api_key": MEMBER_KEY})
        response = self.member.post(f"/api/pods/{w['id']}/runpod/start", headers=H)
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(created, [])
        response = self.admin.post(f"/api/pods/{w['id']}/runpod/start", headers=H)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(created, [(f"nightshift-{w['id']}", ENV_KEY)])
        drop(w["id"])

    def test_auto_power_on_only_counts_admin_runnable_jobs_and_admin_workers(self):
        AP.FILE.unlink(missing_ok=True); AP.STATE_FILE.unlink(missing_ok=True)
        mine = A.pod_registry.create_pod({"kind": "comfyui", "url": "", "name": "mw", "enabled": False, "note": "runpod:off",
                                          "runpod_tier": "image", "owner_id": self.member_id})
        AP.save({"enabled": True, "min_jobs": 1})
        now = datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)
        A.jobs["m1"] = {"id": "m1", "status": "queued", "pod_id": None, "deleted": False, "template_id": "seed_batch",
                        "owner_id": self.member_id, "queued_at": now.isoformat()}
        decision = A._auto_power_on_check(now)
        self.assertEqual(decision["waiting"], 0)                          # 회원 작업은 관리자 파드에서 못 돈다
        self.assertEqual(created, [])
        self.assertNotIn(mine["id"], [c["id"] for c in A._auto_power_payload()["candidates"]])   # 회원 워커는 켜지 않는다
        adm = A.pod_registry.create_pod({"kind": "comfyui", "url": "", "name": "aw", "enabled": False, "note": "runpod:off",
                                         "runpod_tier": "image", "owner_id": self.admin_id})
        A.jobs["a1"] = {"id": "a1", "status": "queued", "pod_id": None, "deleted": False, "template_id": "seed_batch",
                        "owner_id": self.admin_id, "queued_at": now.isoformat()}
        self.assertEqual(A._auto_power_on_check(now)["action"], "started")
        self.assertEqual(created, [(f"nightshift-{adm['id']}", ENV_KEY)])
        for pod in (mine, adm):
            drop(pod["id"])

    def test_auto_power_off_ignores_other_members_unassigned_jobs(self):
        A.auth.set_secrets(self.member_id, {"runpod_api_key": MEMBER_KEY})
        w = A.pod_registry.create_pod({"kind": "comfyui", "url": "https://abc-8188.proxy.runpod.net", "name": "mw",
                                       "enabled": True, "note": "runpod:abc", "runpod_tier": "image",
                                       "auto_power_off": True, "auto_off_minutes": 5, "owner_id": self.member_id})
        seen_keys = []
        R.get_runpod_info = lambda url, key: (seen_keys.append(key), {"status": "RUNNING"})[1]
        now = datetime.now(timezone.utc)
        A.jobs["done"] = {"id": "done", "status": "done", "pod_id": w["id"], "deleted": False, "owner_id": self.member_id,
                          "finished_at": (now - timedelta(minutes=30)).isoformat()}
        A.jobs["theirs"] = {"id": "theirs", "status": "queued", "pod_id": None, "deleted": False, "owner_id": self.other_id}
        try:
            self.assertEqual(A._auto_power_off_check(now), [w["id"]])
        finally:
            R.get_runpod_info = lambda url, key: {}
        self.assertEqual(seen_keys[0], MEMBER_KEY)
        self.assertEqual(terminated, [("abc", MEMBER_KEY)])
        drop(w["id"])

    def test_sync_uses_member_account_and_member_workers_only(self):
        A.auth.set_secrets(self.member_id, {"runpod_api_key": MEMBER_KEY})
        adm = A.pod_registry.create_pod({"kind": "comfyui", "url": "https://zzz-8188.proxy.runpod.net", "name": "adm",
                                         "enabled": True, "tags": ["runpod", "auto"], "owner_id": self.admin_id})
        listed = []
        old = R.list_runpod_pods_verbose
        R.list_runpod_pods_verbose = lambda key: (listed.append(key), ([{"id": "mm1", "name": "x", "status": "RUNNING",
                                                                        "ports": ["8188/http"]}], None))[1]
        try:
            response = self.member.post("/api/pods/sync-runpod", json={}, headers=H)
        finally:
            R.list_runpod_pods_verbose = old
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(listed, [MEMBER_KEY])
        added = response.json()["added"]
        self.assertEqual([a["runpod_pod_id"] for a in added], ["mm1"])
        self.assertEqual(A.pod_registry.get_pod(added[0]["id"])["owner_id"], self.member_id)
        self.assertTrue(A.pod_registry.get_pod(adm["id"])["enabled"])   # 회원 동기화가 관리자 워커를 끄지 않는다
        for pod_id in (adm["id"], added[0]["id"]):
            drop(pod_id)

    def test_member_without_key_never_reaches_runpod(self):
        old = R._rest
        hits = []
        R._rest = lambda *a, **k: hits.append(k.get("key")) or (None, "x")
        try:
            dash = self.member.get("/api/runpod/dashboard").json()
            vols = self.member.get("/api/runpod/network-volumes")
        finally:
            R._rest = old
        self.assertFalse(dash["api_key"])
        self.assertEqual(vols.status_code, 424 if A.UPSTREAM_ERROR == 424 else A.UPSTREAM_ERROR)
        self.assertTrue(all(k == "" for k in hits), hits)                 # 관리자 키로 대신 부르지 않는다


if __name__ == "__main__":
    unittest.main()
