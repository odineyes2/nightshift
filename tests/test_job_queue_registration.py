"""NS-53: 사용 안 함 워커를 골라도 작업은 대기 등록되고, 워커가 준비되면 그 워커에만 배정된다.

임시 데이터 폴더만 쓰고, 워커 연결·모델 조회·RunPod·dispatch는 모두 가짜로 바꾼다.
"""
import asyncio
import os
import queue
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
os.environ["NIGHTSHIFT_ADMIN_USER"] = "admin"
os.environ["NIGHTSHIFT_ADMIN_PASSWORD"] = "Test-Passw0rd-xyz!"
os.environ.pop("NIGHTSHIFT_PUBLIC_URL", None)
sys.path.insert(0, str(ROOT / "server"))

import app as A

A.db.init()
A.auth.ensure_admin()
ALICE = A.auth.register("alice", "alice@example.test", os.environ["NIGHTSHIFT_ADMIN_PASSWORD"])
BOB = A.auth.register("bob", "bob@example.test", os.environ["NIGHTSHIFT_ADMIN_PASSWORD"])

ONLINE_CALLS: list[str] = []
DISPATCHED: list[tuple] = []
HEALTH_OK = {"value": False}


def online(name):
    def fake(*args, **kwargs):
        ONLINE_CALLS.append(name)
        raise RuntimeError("오프라인 검사에서는 부르면 안 된다")
    return fake


_object_info = online("object_info")
# NO_POD(파드 없음 자리표시)는 실제 함수도 조회 없이 (None, None)을 돌려준다.
A.fetch_comfy_object_info = lambda force=False, pod=None: (None, None) if pod and pod.get("_none") else _object_info()
A.compute_preflight = online("preflight")
A.runpod_api.get_runpod_info = online("runpod")
A.dispatch_job = lambda job_id, pod_id=None: DISPATCHED.append((job_id, pod_id))
A.poke_scheduler = lambda: None
A.driver_for = lambda pod: SimpleNamespace(health=lambda p: {"ok": HEALTH_OK["value"]})
A.job_missing_on_pod = lambda job, pod: []
A._snapshot_pod_into_job = lambda job, pod: None


def fake_runtime(pod):
    rt = A.pod_runtimes.get(pod["id"])
    if rt is None:
        rt = SimpleNamespace(max_concurrent=1, running=[], queue=queue.Queue(), maintenance=None, auto_run=False)
        A.pod_runtimes[pod["id"]] = rt
    return rt


A.ensure_runtime = fake_runtime

TEMPLATE = A.resolve_template("seed_batch")
SHELL = A.resolve_template("shell_command")


def make_pod(name, owner, enabled, kind="comfyui"):
    return A.pod_registry.create_pod({"name": name, "kind": kind, "url": "http://127.0.0.1:8188",
                                      "owner_id": owner["id"], "enabled": enabled})


OFF = make_pod("로컬 워커", ALICE, enabled=False)
OTHER = make_pod("다른 워커", ALICE, enabled=True)
BOBS = make_pod("남의 워커", BOB, enabled=True)


def create(pod_id, start_paused, template=TEMPLATE, user=ALICE, options=None):
    return asyncio.run(A.create_job(template, b"{}", "wf.json", None, None, options or {}, pod_id,
                                    user=user, start_paused=start_paused))


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        A.jobs.clear()
        ONLINE_CALLS.clear()
        DISPATCHED.clear()
        HEALTH_OK["value"] = False
        A.pod_registry.update_pod(OFF["id"], {**OFF, "enabled": False})
        for rt in A.pod_runtimes.values():
            rt.auto_run = True   # 자동 실행이 켜져 있어도 꺼진 워커에 직접 넣으면 안 된다

    def test_disabled_pod_paused_registers_pending_and_pinned(self):
        job = create(OFF["id"], start_paused=True, options={"checkpoint": "x.safetensors"})
        self.assertEqual(job["status"], "pending")
        self.assertIsNone(job["pod_id"])
        self.assertEqual(job["pinned_pod_id"], OFF["id"])
        self.assertEqual(ONLINE_CALLS, [])
        self.assertEqual(DISPATCHED, [])

    def test_disabled_pod_unpaused_registers_queued(self):
        job = create(OFF["id"], start_paused=False)
        self.assertEqual(job["status"], "queued")
        self.assertIsNone(job["pod_id"])
        self.assertEqual(job["pinned_pod_id"], OFF["id"])
        self.assertEqual(DISPATCHED, [])

    def test_no_pod_still_registers(self):
        self.assertEqual(create(None, start_paused=True)["status"], "pending")
        self.assertEqual(create(None, start_paused=False)["status"], "queued")

    def test_rejections_kept(self):
        for pod_id, template in ((BOBS["id"], TEMPLATE), ("nope", TEMPLATE), (OFF["id"], SHELL)):
            with self.assertRaises(A.HTTPException) as ctx:
                create(pod_id, start_paused=True, template=template)
            self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(A.jobs, {})

    def test_waits_until_pinned_pod_ready(self):
        job = create(OFF["id"], start_paused=False)
        HEALTH_OK["value"] = True
        A.schedule_once()   # 꺼진 지정 워커 — 켜져 있는 다른 워커로 새면 안 된다
        self.assertIsNone(job["pod_id"])
        self.assertIn("지정한 워커", job["waiting_reason"])
        self.assertEqual(DISPATCHED, [])

        A.pod_registry.update_pod(OFF["id"], {**OFF, "enabled": True})
        HEALTH_OK["value"] = False
        A.schedule_once()   # 켰지만 연결 안 됨
        self.assertIsNone(job["pod_id"])
        self.assertIn("연결 안 됨", job["waiting_reason"])

        HEALTH_OK["value"] = True
        A.schedule_once()
        A.schedule_once()
        self.assertEqual(job["pod_id"], OFF["id"])
        self.assertEqual(DISPATCHED, [(job["id"], OFF["id"])])

    def test_enabled_pod_unchanged(self):
        job = create(OTHER["id"], start_paused=True)
        self.assertEqual((job["status"], job["pod_id"], job["pinned_pod_id"]), ("pending", OTHER["id"], OTHER["id"]))


if __name__ == "__main__":
    unittest.main()
