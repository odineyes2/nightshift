"""부팅 감시(NS-18) 검사 — 가짜 runpod_api·health로 다섯 경우를 본다. 실행: python tests/test_boot_watch.py"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

tmp = tempfile.mkdtemp()
for k in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{k}_DIR"] = os.path.join(tmp, k.lower())
os.environ.setdefault("NIGHTSHIFT_ADMIN_USER", "admin")
os.environ.setdefault("NIGHTSHIFT_ADMIN_PASSWORD", "Test-Passw0rd-xyz!")
os.environ.pop("NIGHTSHIFT_PUBLIC_URL", None)
os.environ["RUNPOD_BOOT_TIMEOUT_MIN"] = "15"   # 아래 시각들은 15분 기준이다(기본값은 30분, NS-34)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
os.chdir(Path(__file__).resolve().parent.parent / "server")

import app as A  # noqa: E402

T0 = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
healthy = set()      # ComfyUI가 응답하는 RunPod 파드 id
created, terminated = [], []
n = [0]


def fake_create(name, tier, env, entrypoint):
    n[0] += 1
    created.append(name)
    return {"id": f"new{n[0]}"}, None


A.runpod_api.RUNPOD_API_KEY = "x"
A.runpod_api.get_runpod_info = lambda url: {"status": "RUNNING", "last_started_at": "2026-10-01 00:00:00.000 +0000 UTC",
                                             "pod_name": "someone-else"}
A.runpod_api.create_pod = fake_create
A.runpod_api.terminate_pod = lambda rp: terminated.append(rp)
A.driver_for = lambda pod: type("D", (), {"health": staticmethod(
    lambda p, t=None: {"ok": A.runpod_api.extract_pod_id(p["url"]) in healthy})})
A._log_runpod_sessions_quietly = lambda: None
A.ensure_runtime = lambda pod: None


def worker(rp, tier="image"):
    return A.pod_registry.create_pod({"kind": "comfyui", "url": f"https://{rp}-8188.proxy.runpod.net", "name": rp,
                                      "enabled": True, "note": f"runpod:{rp}", "runpod_tier": tier})


managed = worker("aaa")
ok = worker("bbb")
healthy.add("bbb")
manual = worker("ccc", tier="")   # 손으로 등록한 워커(이름도 nightshift-가 아님)

# 1) 15분 전 — 그대로 둔다
assert A._boot_watch_check(T0 + timedelta(minutes=10)) == {}
assert not terminated and not created

# 2) 15분 후 첫 실패 — 지우고 한 번 다시 만든다. 4) 정상 파드·5) 손으로 등록한 워커는 건드리지 않는다
assert A._boot_watch_check(T0 + timedelta(minutes=16)) == {managed["id"]: "retried"}, created
assert terminated == ["aaa"] and created == [f"nightshift-{managed['id']}"]
p = A.pod_registry.get_pod(managed["id"])
assert p["note"] == "runpod:new1:boot_retry" and p["enabled"] and "new1" in p["url"]

# 3) 다시 만든 파드도 실패 — 지우고 워커는 꺼서 boot_failed
assert A._boot_watch_check(T0 + timedelta(minutes=40)) == {managed["id"]: "failed"}
assert terminated == ["aaa", "new1"] and len(created) == 1
p = A.pod_registry.get_pod(managed["id"])
assert p["note"] == "runpod:boot_failed" and not p["enabled"] and p["url"] == ""

# 그 뒤로는 손대지 않는다(정상·손으로 등록한 워커도 그대로)
assert A._boot_watch_check(T0 + timedelta(minutes=60)) == {}
assert A.pod_registry.get_pod(ok["id"])["note"] == "runpod:bbb"
assert A.pod_registry.get_pod(manual["id"])["note"] == "runpod:ccc"
print("ok")
