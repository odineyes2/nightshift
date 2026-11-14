"""대기열 자동 켜기 검사 — 가짜 RunPod만 쓴다(실제 파드를 만들지 않음). 실행: python tests/test_auto_power_on.py"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

tmp = tempfile.mkdtemp()
for k in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{k}_DIR"] = os.path.join(tmp, k.lower())
os.environ.setdefault("NIGHTSHIFT_ADMIN_USER", "admin")
os.environ.setdefault("NIGHTSHIFT_ADMIN_PASSWORD", "Test-Passw0rd-xyz!")
os.environ.pop("NIGHTSHIFT_PUBLIC_URL", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
os.chdir(Path(__file__).resolve().parent.parent / "server")

import app as A  # noqa: E402
import auto_power_settings as S  # noqa: E402

created, fail = [], {"err": None}


def fake_create(name, tier, env, entrypoint, key=""):
    if fail["err"]:
        return None, fail["err"]
    created.append((name, tier))
    return {"id": f"rp{len(created)}"}, None


A.runpod_api.RUNPOD_API_KEY = "x"
A.runpod_api.create_pod = fake_create
A.runpod_api.terminate_pod = lambda rp, key: None
A.runpod_api.get_runpod_info = lambda url, key: {}
A._log_runpod_sessions_quietly = lambda: None
A.ensure_runtime = lambda pod: None
A.poke_scheduler = lambda: None
A.save_state = lambda: None
A.load_templates_map = lambda: {"claude": {"pod_kinds": ["claude_writer"]}}
NOW = datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)


class AutoPowerOnTests(unittest.TestCase):
    def setUp(self):
        created.clear(); fail["err"] = None
        for path in (S.FILE, S.STATE_FILE):
            path.unlink(missing_ok=True)
        for pod in A.pod_registry.list_pods(A.pod_registry.ALL):
            A.pod_registry.update_pod(pod["id"], {"enabled": False, "url": "", "note": "runpod:off"})
        self.worker = A.pod_registry.create_pod({"kind": "comfyui", "url": "", "name": f"img{len(A.pod_registry.list_pods(A.pod_registry.ALL))}",
                                                 "enabled": False, "note": "runpod:off", "runpod_tier": "image", "auto_power_off": False})
        A.jobs.clear()

    def queue(self, count, hours_ago=0, template="seed_batch"):
        for i in range(count):
            A.jobs[f"j{len(A.jobs)}"] = {"id": f"j{len(A.jobs)}", "status": "queued", "pod_id": None, "deleted": False,
                                         "template_id": template, "queued_at": (NOW - timedelta(hours=hours_ago)).isoformat()}

    def enable(self, **extra):
        S.save({"enabled": True, **extra})

    def test_off_by_default_and_counts_only_image_jobs(self):
        self.queue(12)
        self.assertEqual(A._auto_power_on_check(NOW)["action"], "off")
        self.enable()
        A.jobs.clear(); self.queue(12, template="claude")
        self.assertEqual(A._auto_power_on_check(NOW)["waiting"], 0)
        self.assertEqual(created, [])

    def test_starts_one_image_worker_at_threshold_and_turns_on_auto_off(self):
        self.enable(min_jobs=10)
        self.queue(9)
        decision = A._auto_power_on_check(NOW)
        self.assertEqual((decision["action"], decision["waiting"]), ("wait", 9))
        self.assertIn("9/10", decision["reason"])
        self.queue(1)
        self.assertEqual(A._auto_power_on_check(NOW)["action"], "started")
        self.assertEqual(created, [(f"nightshift-{self.worker['id']}", "image")])
        pod = A.pod_registry.get_pod(self.worker["id"])
        self.assertTrue(pod["enabled"]); self.assertTrue(pod["auto_power_off"]); self.assertEqual(pod["note"], "runpod:rp1")
        # 이미 켜져 있으면 더 켜지 않는다
        self.queue(20)
        self.assertEqual(A._auto_power_on_check(NOW)["action"], "wait")
        self.assertEqual(len(created), 1)

    def test_old_jobs_start_even_below_the_count(self):
        self.enable(min_jobs=10, max_wait_hours=24)
        self.queue(2, hours_ago=23)
        self.assertEqual(A._auto_power_on_check(NOW)["action"], "wait")
        A.jobs.clear(); self.queue(2, hours_ago=25)
        self.assertEqual(A._auto_power_on_check(NOW)["action"], "started")

    def test_daily_limit(self):
        self.enable(min_jobs=1, daily_limit=1)
        self.queue(1)
        self.assertEqual(A._auto_power_on_check(NOW)["action"], "started")
        A.pod_registry.update_pod(self.worker["id"], {"enabled": False, "url": "", "note": "runpod:off"})
        decision = A._auto_power_on_check(NOW + timedelta(hours=1))
        self.assertEqual(decision["action"], "wait"); self.assertIn("1번을 다 썼어요", decision["reason"])
        self.assertEqual(len(created), 1)

    def test_failure_backs_off_and_leaves_worker_off(self):
        self.enable(min_jobs=1)
        self.queue(1)
        fail["err"] = "GPU 없음"
        self.assertEqual(A._auto_power_on_check(NOW)["action"], "failed")
        pod = A.pod_registry.get_pod(self.worker["id"])
        self.assertEqual((pod["enabled"], pod["note"]), (False, "runpod:off"))
        fail["err"] = None
        waiting = A._auto_power_on_check(NOW + timedelta(minutes=5))
        self.assertEqual(waiting["action"], "wait"); self.assertIn("GPU 없음", waiting["reason"])
        self.assertEqual(A._auto_power_on_check(NOW + timedelta(minutes=11))["action"], "started")

    def test_specific_worker_and_validation(self):
        other = A.pod_registry.create_pod({"kind": "comfyui", "url": "", "name": "other", "enabled": False, "note": "runpod:off", "runpod_tier": "image"})
        self.enable(min_jobs=1, pod_id=other["id"])
        self.queue(1)
        A._auto_power_on_check(NOW)
        self.assertEqual(created, [(f"nightshift-{other['id']}", "image")])
        for bad in ({"min_jobs": 0}, {"daily_limit": 99}, {"enabled": "yes"}, {"unknown": 1}, {"max_wait_hours": True}):
            with self.assertRaises(ValueError):
                S.validate(bad)

    def test_settings_api_reports_status(self):
        A.admin_only = lambda request: {"id": 1, "role": "admin"}
        self.enable(min_jobs=5)
        self.queue(3)
        data = A.get_auto_power_settings(None)
        self.assertEqual(data["settings"]["min_jobs"], 5)
        self.assertEqual(data["status"]["waiting"], 3)
        self.assertIn(self.worker["id"], [c["id"] for c in data["candidates"]])
        self.assertNotIn("pod", data["status"])


class ServiceStatusTests(unittest.TestCase):
    def test_service_status_is_read_only_summary_and_token_route(self):
        A.me = lambda request: {"id": 9, "role": "user"}
        S.save({"enabled": True, "min_jobs": 7})
        A.jobs.clear()
        A.jobs["x"] = {"id": "x", "status": "queued", "pod_id": None, "deleted": False, "template_id": "seed_batch", "queued_at": NOW.isoformat()}
        data = A.generate_auto_power_status(None)
        self.assertEqual({k: data[k] for k in ("enabled", "waiting", "min_jobs")}, {"enabled": True, "waiting": 1, "min_jobs": 7})
        self.assertEqual(set(data), {"enabled", "waiting", "min_jobs", "max_wait_hours", "worker_on", "limit_reached"})
        self.assertTrue(any(method == "GET" and pattern.match("/api/generate/auto-power") for method, pattern in A.SERVICE_TOKEN_ROUTES))
        self.assertFalse(any(pattern.match("/api/settings/auto-power") for _, pattern in A.SERVICE_TOKEN_ROUTES))


if __name__ == "__main__":
    unittest.main()
