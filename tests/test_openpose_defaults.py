"""NS-41-1 OpenPose 기본값 API·등록부 후보 순서·xinsir 판정·워크플로우 유형 — python tests/test_openpose_defaults.py"""
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
import model_registry as R  # noqa: E402

admin = auth.ensure_admin() or auth.authenticate("admin", os.environ["NIGHTSHIFT_ADMIN_PASSWORD"])
bob = auth.register("bob", "bob@example.com", "Bob-Passw0rd-xyz!")
with A.db.connect() as conn:
    conn.execute("UPDATE users SET status='active'")
TOKENS = {"admin": auth.create_session(admin["id"]), "bob": auth.create_session(bob["id"])}


async def call(method, url, who="admin", json=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=A.app), base_url="http://test",
                                 headers={"x-requested-with": "nightshift"},
                                 cookies={auth.SESSION_COOKIE: TOKENS[who]}) as client:
        return await client.request(method, url, json=json)


def run(*a, **k):
    return asyncio.run(call(*a, **k))


# 등록부: Illustrious(체크포인트형)와 Anima(UNet형)
for base in ("Illustrious", "Anima"):
    if base not in {b["name"] for b in R.list_base_models()}:
        R.add_base_model(base)
R.upsert("checkpoints", "wai.safetensors", {"base_models": ["Illustrious"]})
R.upsert("diffusion_models", "anima.safetensors", {"base_models": ["Anima"]})
R.upsert("controlnet", "controlnet-openpose-sdxl-1.0.safetensors",
         {"base_models": ["Illustrious", "Anima"], "page_url": "https://huggingface.co/xinsir/controlnet-openpose-sdxl-1.0"})
R.upsert("controlnet", "openpose_pre.safetensors", {"base_models": ["Illustrious"]})
R.upsert("controlnet", "a_pose.safetensors", {"base_models": ["Illustrious"], "tags": ["OpenPose"]})
R.upsert("controlnet", "depth.safetensors", {"base_models": ["Illustrious"]})
ill, anima = R.base_id("Illustrious"), R.base_id("Anima")

groups = run("GET", "/api/base-model-families?pod_id=auto").json()
assert groups[ill]["controlnets"]["openpose"] == [
    "openpose_pre.safetensors", "a_pose.safetensors", "controlnet-openpose-sdxl-1.0.safetensors"], groups[ill]
assert "controlnets" not in groups[anima]

# 워크플로우 유형: openpose는 체크포인트형 family 전용 post
types = run("GET", "/api/workflow-types").json()
op = next(t for t in types["post"] if t["id"] == "openpose")
assert op["family_kind"] == "checkpoints" and op["requires_node"] == "DWPreprocessor"
assert "openpose" in A.workflow_type_ids()

# 기본값: 빌더 값(첨부 JSON 기준)
r = run("GET", f"/api/controlnet-defaults/{ill}").json()
assert r["values"]["strength"] == 0.8 and r["values"]["end_percent"] == 0.8 and r["values"]["resolution"] == 1024
assert r["values"]["scale_stick_for_xinsr_cn"] == "disable" and r["saved"] == {}

# 저장: 관리자만, 검증, 저장된 control_net_name이 후보 1순위
values = {"strength": 0.6, "detect_face": False, "control_net_name": "controlnet-openpose-sdxl-1.0.safetensors"}
assert run("PUT", f"/api/controlnet-defaults/{ill}", who="bob", json={"values": values}).status_code == 403
for bad in ({"strength": 3}, {"start_percent": 0.9, "end_percent": 0.2}, {"nope": 1}, {"detect_hand": "yes"}):
    assert run("PUT", f"/api/controlnet-defaults/{ill}", json={"values": bad}).status_code == 400, bad
assert run("PUT", f"/api/controlnet-defaults/{ill}", json={"values": values}).status_code == 200
r = run("GET", f"/api/controlnet-defaults/{ill}").json()
assert r["values"]["strength"] == 0.6 and r["values"]["detect_face"] == "disable" and r["values"]["end_percent"] == 0.8
groups = run("GET", "/api/base-model-families?pod_id=auto").json()
assert groups[ill]["controlnets"]["openpose"][0] == "controlnet-openpose-sdxl-1.0.safetensors"
assert run("PUT", f"/api/controlnet-defaults/{ill}", json={"values": {}}).json()["saved"] == {}

# xinsir 판정: 파일명 또는 등록부 URL
assert A.is_xinsir_controlnet("controlnet-openpose-sdxl-1.0.safetensors")
assert A.is_xinsir_controlnet("xinsir_pose.safetensors")
assert not A.is_xinsir_controlnet("openpose_pre.safetensors")


def build(cn):
    spec = {"checkpoint": "wai.safetensors", "positive": "1girl", "controlnet": {"enabled": True, **cn}}
    r = run("POST", "/api/build-workflow", json=spec)
    assert r.status_code == 200, r.text
    wf = r.json()["workflow"]
    return next(n for n in wf.values() if n["class_type"] == "DWPreprocessor")["inputs"]


assert build({"control_net_name": "controlnet-openpose-sdxl-1.0.safetensors"})["scale_stick_for_xinsr_cn"] == "enable"
assert build({"control_net_name": "openpose_pre.safetensors"})["scale_stick_for_xinsr_cn"] == "disable"
# 사용자가 정한 값이 우선
assert build({"control_net_name": "controlnet-openpose-sdxl-1.0.safetensors",
              "dw": {"scale_stick_for_xinsr_cn": "disable"}})["scale_stick_for_xinsr_cn"] == "disable"
# UNet형은 거절
r = run("POST", "/api/build-workflow", json={"architecture": "unet", "checkpoint": "anima.safetensors", "clip": "c",
                                             "vae": "v", "positive": "x",
                                             "controlnet": {"enabled": True, "control_net_name": "openpose_pre.safetensors"}})
assert r.status_code == 400

# 수정 탭: 붙은 워크플로우의 세 노드에 나눠 반영
wf = {"1": {"class_type": "ControlNetApplyAdvanced", "inputs": {"strength": 0.8}},
      "2": {"class_type": "DWPreprocessor", "inputs": {"resolution": 1024}},
      "3": {"class_type": "ControlNetLoader", "inputs": {"control_net_name": "a"}},
      "4": {"class_type": "KSampler", "inputs": {"seed": 1}}}
r = run("POST", "/api/controlnet/apply", json={"workflow": wf, "values": {
    "strength": 0.3, "resolution": 512, "control_net_name": "openpose_pre.safetensors"}}).json()["workflow"]
assert r["1"]["inputs"] == {"strength": 0.3} and r["2"]["inputs"] == {"resolution": 512}
assert r["3"]["inputs"]["control_net_name"] == "openpose_pre.safetensors" and r["4"]["inputs"] == {"seed": 1}
assert run("POST", "/api/controlnet/apply", json={"workflow": {"4": wf["4"]}, "values": {"strength": 1}}).status_code == 400

print("ok")
