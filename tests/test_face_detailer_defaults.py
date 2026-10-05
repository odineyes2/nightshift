"""NS-34-7: Face Detailer 수정 탭·셋팅 탭 검사 — python tests/test_face_detailer_defaults.py

- GET/PUT /api/face-detailer-defaults/{family}: 베이스 모델별 저장·검증·권한(관리자만 저장)
- 저장된 기본값으로 만든 spec(마법사 faceDetailerSpec과 같은 모양)이 FaceDetailer 노드에 들어가는지
- POST /api/face-detailer/apply: 수정 탭 값이 붙은 워크플로우의 FaceDetailer 노드에 반영되는지
- 고친 JS 문법(node --check)
"""
import asyncio
import os
import shutil
import subprocess
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
import workflow_builder  # noqa: E402
import workflow_builder_unet  # noqa: E402

admin = auth.ensure_admin() or auth.authenticate("admin", os.environ["NIGHTSHIFT_ADMIN_PASSWORD"])
bob = auth.register("bob", "bob@example.com", "Bob-Passw0rd-xyz!")
with A.db.connect() as conn:
    conn.execute("UPDATE users SET status='active'")
TOKENS = {"admin": auth.create_session(admin["id"]), "bob": auth.create_session(bob["id"]), None: None}


async def call(method, url, who=None, json=None):
    cookies = {auth.SESSION_COOKIE: TOKENS[who]} if TOKENS[who] else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=A.app), base_url="http://test",
                                 headers={"x-requested-with": "nightshift"}, cookies=cookies) as client:
        return await client.request(method, url, json=json)


def req(*a, **k):
    return asyncio.run(call(*a, **k))


# 저장 전에는 로더 형태별 빌더 기본값
r = req("GET", "/api/face-detailer-defaults/anima?architecture=unet", "bob")
assert r.status_code == 200, r.text
assert r.json()["values"]["steps"] == 30 and r.json()["values"]["sampler_name"] == "er_sde"
assert r.json()["values"]["guide_size"] == 512
assert req("GET", "/api/face-detailer-defaults/sdxl", "bob").json()["values"]["steps"] == 20

# 권한: 로그인 안 함 401, 일반 회원 403, 관리자만 저장
body = {"values": {"steps": "25", "cfg": 6.5, "sampler_name": "dpmpp_2m", "denoise": 0.4}}
assert req("PUT", "/api/face-detailer-defaults/sdxl", None, body).status_code == 401
assert req("PUT", "/api/face-detailer-defaults/sdxl", "bob", body).status_code == 403
r = req("PUT", "/api/face-detailer-defaults/sdxl", "admin", body)
assert r.status_code == 200, r.text
assert r.json()["saved"] == {"steps": 25, "cfg": 6.5, "sampler_name": "dpmpp_2m", "denoise": 0.4}

# 검증: 범위·형·알 수 없는 칸·family 형식
for bad in ({"denoise": 1.5}, {"steps": 0}, {"steps": "abc"}, {"foo": 1}, {"sampler_name": "a b;"}):
    r = req("PUT", "/api/face-detailer-defaults/sdxl", "admin", {"values": bad})
    assert r.status_code == 400, (bad, r.text)
assert req("PUT", "/api/face-detailer-defaults/bad%20name", "admin", body).status_code == 400

# 베이스 모델별로 따로 저장된다
values = req("GET", "/api/face-detailer-defaults/sdxl", "bob").json()["values"]
assert values["steps"] == 25 and values["sampler_name"] == "dpmpp_2m" and values["scheduler"] == "simple"
assert req("GET", "/api/face-detailer-defaults/anima?architecture=unet", "bob").json()["values"]["steps"] == 30

# 마법사: 저장된 기본값을 spec.face_detailer로 넘기면 FaceDetailer 노드가 그 값을 쓴다
spec = {"base": "txt2img", "checkpoint": "a.safetensors", "positive": "(prompt)",
        "face_detailer": {**values, "enabled": True}}
fd = next(n for n in workflow_builder.build_workflow(spec).values() if n["class_type"] == "FaceDetailer")
assert fd["inputs"]["steps"] == 25 and fd["inputs"]["cfg"] == 6.5 and fd["inputs"]["denoise"] == 0.4
assert fd["inputs"]["sampler_name"] == "dpmpp_2m"
uspec = {"base": "face_detailer", "checkpoint": "u.safetensors", "clip": "c.safetensors", "vae": "v.safetensors",
         "face_detailer": req("GET", "/api/face-detailer-defaults/anima?architecture=unet", "bob").json()["values"]}
uspec["face_detailer"]["positive"] = "face"
ufd = next(n for n in workflow_builder_unet.build_workflow(uspec).values() if n["class_type"] == "FaceDetailer")
assert ufd["inputs"]["steps"] == 30 and ufd["inputs"]["sampler_name"] == "er_sde"

# 빈 값을 저장하면 빌더 기본값으로 되돌린다
assert req("PUT", "/api/face-detailer-defaults/sdxl", "admin", {"values": {}}).status_code == 200
assert req("GET", "/api/face-detailer-defaults/sdxl", "bob").json()["values"]["steps"] == 20

# 수정 탭: 붙은 워크플로우의 FaceDetailer 노드만 고친다
wf = workflow_builder.build_workflow(spec)
r = req("POST", "/api/face-detailer/apply", "bob", {"workflow": wf, "values": {"steps": 12, "bbox_threshold": "0.3"}})
assert r.status_code == 200, r.text
out = r.json()["workflow"]
fd = next(n for n in out.values() if n["class_type"] == "FaceDetailer")
assert fd["inputs"]["steps"] == 12 and fd["inputs"]["bbox_threshold"] == 0.3
assert fd["inputs"]["cfg"] == 6.5 and isinstance(fd["inputs"]["image"], list)  # 나머지 값·배선은 그대로
assert [n for n in out.values() if n["class_type"] == "KSampler"] == \
    [n for n in wf.values() if n["class_type"] == "KSampler"]  # 메인 KSampler는 그대로
assert req("POST", "/api/face-detailer/apply", "bob",
           {"workflow": {"1": {"class_type": "KSampler", "inputs": {}}}, "values": {"steps": 5}}).status_code == 400
assert req("POST", "/api/face-detailer/apply", "bob", {"workflow": wf, "values": {"cfg": -1}}).status_code == 400

# JS 문법·연결
tools = (ROOT / "static/js/04-newjob-tools.js").read_text(encoding="utf-8")
wizard = (ROOT / "static/js/06-wizard.js").read_text(encoding="utf-8")
assert "renderFaceDetailerPanel(parsed)" in wizard and "faceDetailerSpec(wizard.familyId, architecture)" in wizard
assert "/api/face-detailer/apply" in tools and 'id="fd-panel"' in (ROOT / "static/index.html").read_text(encoding="utf-8")
node = shutil.which("node")
if node:
    for f in ("04-newjob-tools.js", "06-wizard.js"):
        subprocess.run([node, "--check", str(ROOT / "static/js" / f)], check=True)
else:
    print("node 없음 — node --check 건너뜀")
print("ok")
