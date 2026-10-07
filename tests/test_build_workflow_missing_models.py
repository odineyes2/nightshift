"""NS-47-1 build-workflow는 파드에 없는 모델을 거절하지 않고 missing_models로 알린다 — python tests/test_build_workflow_missing_models.py"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

tmp = tempfile.mkdtemp()
os.environ.update(NIGHTSHIFT_DATA_DIR=tmp, NIGHTSHIFT_OUTPUT_DIR=tmp + "/out", NIGHTSHIFT_ASSETS_DIR=tmp + "/a",
                  NIGHTSHIFT_ADMIN_USER="admin", NIGHTSHIFT_ADMIN_PASSWORD="Test-Passw0rd-xyz!")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import httpx  # noqa: E402
import app as A  # noqa: E402
import auth  # noqa: E402

admin = auth.ensure_admin() or auth.authenticate("admin", os.environ["NIGHTSHIFT_ADMIN_PASSWORD"])
TOKEN = auth.create_session(admin["id"])

# 모의 파드: ControlNet·LoRA 목록에 다른 파일만 있다
OBJECT_INFO = {
    "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["wai.safetensors"]]}}},
    "ControlNetLoader": {"input": {"required": {"control_net_name": [["depth.safetensors"]]}}},
    "LoraLoader": {"input": {"required": {"lora_name": [["other.safetensors"]]}}},
    "KSampler": {"input": {"required": {"sampler_name": [["euler"]], "scheduler": [["normal"]]}}},
}
A.object_info_pod = lambda pod_id: {"id": "mock"}
A.fetch_comfy_object_info = lambda force=False, pod=None: ("http://mock", OBJECT_INFO)


def build(spec):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=A.app), base_url="http://test",
                                     headers={"x-requested-with": "nightshift"},
                                     cookies={auth.SESSION_COOKIE: TOKEN}) as client:
            return await client.post("/api/build-workflow", json=spec)
    return asyncio.run(go())


spec = {"checkpoint": "wai.safetensors", "positive": "1girl", "sampler_name": "euler",
        "loras": [{"name": "nope.safetensors", "strength": 1}],
        "controlnet": {"enabled": True, "control_net_name": "openpose_pre.safetensors"}}
r = build(spec)
assert r.status_code == 200, r.text
missing = r.json()["missing_models"]
assert {(m["kind"], m["value"]) for m in missing} == {
    ("controlnet", "openpose_pre.safetensors"), ("loras", "nope.safetensors")}, missing
assert r.json()["workflow"]

# 설치된 것만 고르면 비어 있다
r = build({"checkpoint": "wai.safetensors", "positive": "1girl"})
assert r.status_code == 200 and r.json()["missing_models"] == [], r.text

# 받을 수 없는 값(샘플러)은 그대로 거절
r = build({**spec, "sampler_name": "nope"})
assert r.status_code == 400, r.text

print("ok")
