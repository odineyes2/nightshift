"""NS-72-1 krea.2 빌더의 spec["clip"] 지원과 /api/build-workflow 경로 — python tests/test_workflow_builder_krea2.py"""
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
import workflow_builder_krea2 as K  # noqa: E402

admin = auth.ensure_admin() or auth.authenticate("admin", os.environ["NIGHTSHIFT_ADMIN_PASSWORD"])
TOKEN = auth.create_session(admin["id"])
A.object_info_pod = lambda pod_id: {"id": "mock"}
A.fetch_comfy_object_info = lambda force=False, pod=None: ("http://mock", {
    "KSampler": {"input": {"required": {"sampler_name": [["euler"]], "scheduler": [["simple"]]}}}})


def clip_name(wf):
    (n,) = [n for n in wf.values() if n["class_type"] == "CLIPLoader"]
    return n["inputs"]["clip_name"]


SPEC = {"checkpoint": "krea2.safetensors", "positive": "p"}
assert clip_name(K.build_workflow(SPEC)) == K.CLIP_NAME  # 없으면 기본값
assert clip_name(K.build_workflow({**SPEC, "clip": "  "})) == K.CLIP_NAME
assert clip_name(K.build_workflow({**SPEC, "clip": "my_te.safetensors"})) == "my_te.safetensors"


async def post(spec):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=A.app), base_url="http://test",
                                 headers={"x-requested-with": "nightshift"},
                                 cookies={auth.SESSION_COOKIE: TOKEN}) as client:
        return await client.post("/api/build-workflow", json=spec)


r = asyncio.run(post({**SPEC, "architecture": "krea2", "clip": "my_te.safetensors"}))
assert r.status_code == 200, r.text
assert clip_name(r.json()["workflow"]) == "my_te.safetensors"
print("ok")
