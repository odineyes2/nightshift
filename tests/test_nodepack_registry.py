"""노드팩·얼굴 탐지 모델 등록부 종류와 "노드팩 없음" 대기 사유 검사 — python tests/test_nodepack_registry.py"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

tmp = tempfile.mkdtemp()
os.environ.update(NIGHTSHIFT_DATA_DIR=tmp, NIGHTSHIFT_OUTPUT_DIR=tmp + "/out", NIGHTSHIFT_ASSETS_DIR=tmp + "/a",
                  NIGHTSHIFT_ADMIN_USER="admin", NIGHTSHIFT_ADMIN_PASSWORD="Test-Passw0rd-xyz!")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import app
import model_registry as reg

ns = vars(app)

# 종류 등록
assert "custom_nodes" in reg.KIND_IDS and "ultralytics" in reg.KIND_IDS
assert ns["MODEL_LIST_SOURCES"]["ultralytics"] == ("UltralyticsDetectorProvider", "model_name")

# 노드팩 주소·폴더 이름 검증(셸 스크립트에 들어가는 값)
reg.upsert("custom_nodes", "ComfyUI-Impact-Pack", {"download_url": "https://github.com/ltdrdata/ComfyUI-Impact-Pack"})
for bad_url in ("https://gitlab.com/a/b", "https://github.com/a/b;rm -rf /", "http://github.com/a/b",
                "https://github.com/a/b'c", "https://github.com/a"):
    try:
        reg.upsert("custom_nodes", "X", {"download_url": bad_url})
        raise AssertionError(bad_url)
    except reg.RegistryError:
        pass
for bad_dir in ("../x", "a/b", "a b", ".hidden"):
    try:
        reg.upsert("custom_nodes", bad_dir, {"download_url": "https://github.com/a/b"})
        raise AssertionError(bad_dir)
    except reg.RegistryError:
        pass
# 다른 종류는 github이 아니어도 된다
reg.upsert("ultralytics", "bbox/face_yolov8m.pt",
           {"download_url": "https://huggingface.co/Bingsu/adetailer/resolve/main/face_yolov8m.pt"})

# 모의 object_info — FaceDetailer가 없고 탐지 모델 목록은 비었다
wf = {"1": {"class_type": "FaceDetailer", "inputs": {}},
      "2": {"class_type": "UltralyticsDetectorProvider", "inputs": {"model_name": "bbox/face_yolov8m.pt"}}}
(ns["JOBS_DIR"] / "fd.json").write_text(json.dumps(wf), encoding="utf-8")
info = {"UltralyticsDetectorProvider": {"input": {"required": {"model_name": [[]]}}}}
ns["fetch_comfy_object_info"] = lambda f=False, p=None: (None, info)
pod = {"id": "p1", "name": "p1", "kind": ns["pod_registry"].DEFAULT_KIND}
job = {"id": "j1", "template_id": "none", "workflow_filename": "fd.json", "options": {}}
missing = ns["job_missing_on_pod"](job, pod)
assert "노드팩 없음 (ComfyUI-Impact-Pack)" in missing, missing
assert "bbox/face_yolov8m.pt" in missing, missing
assert ns["missing_node_label"]("Foo") == "노드 Foo"

# 자동 받기는 노드팩을 빼고 탐지 모델만 받는다
got = []
ns["_auto_fetch_run"] = lambda job_id, pod, todo, owner_id: got.append(todo)
ns["jobs"]["j1"] = dict(job)
ns["_maybe_auto_fetch"]("j1", pod, missing)
for _ in range(50):
    if got:
        break
    time.sleep(0.05)
assert got == [["bbox/face_yolov8m.pt"]], got

# 탐지 모델은 ultralytics 종류로 받기 대상이 된다
entry = ns["_registry_entry_for"]("bbox/face_yolov8m.pt")
assert entry and entry["kind"] == "ultralytics" and entry["download_url"]
print("ok")
