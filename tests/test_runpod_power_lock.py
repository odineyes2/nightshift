"""워커별 RunPod 전원 잠금(NS-21) 검사 — 겹친 켜기 요청은 409, 파드는 하나만. 실행: python tests/test_runpod_power_lock.py"""
import asyncio
import os
import sys
import tempfile
import time
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
from fastapi import HTTPException  # noqa: E402

created, terminated, notes = [], [], []


def fake_create(name, tier, env, entrypoint):
    notes.append(A.pod_registry.get_pod(name.split("-", 1)[1])["note"])
    time.sleep(1)
    created.append(name)
    return {"id": f"new{len(created)}"}, None


A.runpod_api.RUNPOD_API_KEY = "x"
A.runpod_api.create_pod = fake_create
A.runpod_api.terminate_pod = lambda rp: terminated.append(rp)
A._log_runpod_sessions_quietly = lambda: None
A.ensure_runtime = lambda pod: None
A.poke_scheduler = lambda: None
A.admin_only = lambda request: {"id": "admin", "role": "admin"}
A.pod_or_404 = lambda user, pod_id: A.pod_registry.get_pod(pod_id)
A._public_base_url = lambda request: None

w = A.pod_registry.create_pod({"kind": "comfyui", "url": "", "name": "w", "enabled": False,
                               "note": "runpod:off", "runpod_tier": "image"})


async def call(action):
    try:
        return 200, await A.runpod_power(w["id"], action, None)
    except HTTPException as e:
        return e.status_code, e.detail


async def main():
    # 동시 켜기 두 번 → 하나만 만든다. 켜는 중의 끄기도 409
    start = asyncio.gather(call("start"), call("start"))
    await asyncio.sleep(0.3)
    stop_mid = await call("stop")
    codes = sorted(c for c, _ in await start)
    assert codes == [200, 409], codes
    assert stop_mid[0] == 409, stop_mid
    assert created == [f"nightshift-{w['id']}"] and notes == ["runpod:creating"], (created, notes)
    p = A.pod_registry.get_pod(w["id"])
    assert "new1" in p["url"] and p["note"] == "runpod:new1"
    # 잠금이 풀린 뒤 끄기는 새 파드를 지운다
    code, _ = await call("stop")
    assert code == 200 and terminated == ["new1"], (code, terminated)
    assert A.pod_registry.get_pod(w["id"])["url"] == ""

asyncio.run(main())
print("ok")
