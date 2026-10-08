"""백틱 빠른 실행창 서버 쪽(NS-52-1) — 워크플로우 분류·옵션 조립과 POST /api/quick-run.
임시 데이터 폴더를 쓰고 파드가 없어 작업은 queued로만 남는다. 실행: python tests/test_quick_run.py"""
import json, os, sys, tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
for k, sub in (("NIGHTSHIFT_DATA_DIR", "data"), ("NIGHTSHIFT_OUTPUT_DIR", "out"), ("NIGHTSHIFT_ASSETS_DIR", "assets"),
               ("NIGHTSHIFT_INPUT_IMAGES_DIR", "inputs")):
    os.environ[k] = str(tmp / sub)
os.environ.update(NIGHTSHIFT_ADMIN_USER="admin", NIGHTSHIFT_ADMIN_PASSWORD="Test-Passw0rd-xyz!", RUNPOD_API_KEY="")
SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER)); os.chdir(SERVER)

import quick_run as Q  # noqa: E402

CKPT = {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {}}}
T2I = {**CKPT, "2": {"class_type": "KSampler", "inputs": {"model": ["1", 0]}}}
POSE_T2I = {**T2I, "5": {"class_type": "LoadImage", "inputs": {"image": ""}, "_meta": {"title": "pose_image"}},
            "6": {"class_type": "DWPreprocessor", "inputs": {"image": ["5", 0]}}}
I2I = {**T2I, "3": {"class_type": "LoadImage", "inputs": {"image": ""}, "_meta": {"title": "input_image"}},
       "4": {"class_type": "VAEEncode", "inputs": {"pixels": ["3", 0]}}}
WAN = {"3": {"class_type": "LoadImage", "inputs": {}}, "4": {"class_type": "WanImageToVideo", "inputs": {"start_image": ["3", 0]}}}
FLF = {"3": {"class_type": "LoadImage", "inputs": {}}, "4": {"class_type": "WanFirstLastFrameToVideo", "inputs": {}}}
MM_T2V = {"1": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {}}}
MM_I2V = {**MM_T2V, "2": {"class_type": "LoadImage", "inputs": {}, "_meta": {"title": "last_frame_image"}},
          "3": {"class_type": "ImageScale", "inputs": {"image": ["2", 0]}}}
OTHER_VIDEO = {"1": {"class_type": "EmptyHunyuanLatentVideo", "inputs": {}}}


def test_classify():
    c = Q.classify_workflow
    assert (c(T2I)["kind"], c(T2I)["template_id"], c(T2I)["openpose"]) == ("t2i", "seed_batch", False)
    p = c(POSE_T2I)
    assert (p["kind"], p["template_id"], p["image_slots"], p["openpose"]) == ("t2i", "seed_batch", [], True)
    assert (c(I2I)["kind"], c(I2I)["template_id"], c(I2I)["image_slots"]) == ("i2i", "input_image_batch", ["input_image"])
    assert (c(WAN)["template_id"], c(WAN)["image_slots"]) == ("wan22_i2v_batch", ["start_image"])
    assert (c(FLF)["template_id"], c(FLF)["image_slots"]) == ("wan22_flf2v_batch", ["start_image", "end_image"])
    assert (c(MM_T2V)["kind"], c(MM_T2V)["template_id"], c(MM_T2V)["image_slots"]) == ("t2v", "minimax_h3_i2v_batch", [])
    assert (c(MM_I2V)["kind"], c(MM_I2V)["image_slots"]) == ("i2v", ["last_frame_image"])
    assert c(OTHER_VIDEO)["unsupported"] and c(OTHER_VIDEO)["template_id"] is None
    assert c("x")["unsupported"]


def test_build():
    b = Q.build_quick_options
    r = b(Q.classify_workflow(T2I), " 1girl ", ["a.png"], {"kind": "position", "mode": "prompt", "tags": "standing"})
    assert r["options"] == {"main_prompt": "1girl, standing"} and r["ignored_images"] == ["a.png"]
    r = b(Q.classify_workflow(T2I), "1girl, standing", [], {"mode": "prompt", "tags": "standing"})
    assert r["options"]["main_prompt"] == "1girl, standing"  # 중복 태그는 다시 붙이지 않는다
    lib = {"kind": "pose", "mode": "openpose", "image": "library_pose_1.png", "tags": "arms up"}
    r = b(Q.classify_workflow(T2I), "1girl", [], lib)  # OpenPose 없는 프리셋 → 포즈·태그 모두 건너뜀
    assert r["pose_skipped"] and r["options"] == {"main_prompt": "1girl"}
    r = b(Q.classify_workflow(POSE_T2I), "1girl", [], lib)
    assert r["options"] == {"main_prompt": "1girl, arms up", "pose_image": "library_pose_1.png"} and not r["pose_skipped"]
    r = b(Q.classify_workflow(FLF), "walk", ["s.png", "e.png"])
    assert r["options"] == {"start_image": "s.png", "end_image": "e.png", "user_prompt": "walk"}
    assert b(Q.classify_workflow(MM_I2V), "walk", ["l.png"])["options"]["last_frame_image"] == "l.png"
    for cls, prompt, images in ((Q.classify_workflow(I2I), "x", []), (Q.classify_workflow(FLF), "x", ["s.png"]),
                                (Q.classify_workflow(T2I), "  ", []), (Q.classify_workflow(OTHER_VIDEO), "x", [])):
        try:
            b(cls, prompt, images)
        except Q.QuickRunError:
            continue
        raise AssertionError((cls, prompt, images))


def test_api():
    import runpod_api
    runpod_api.RUNPOD_API_BASE = "http://127.0.0.1:9"
    runpod_api.RUNPOD_API_KEY = ""
    from fastapi.testclient import TestClient
    import app as A
    H = {"X-Requested-With": "nightshift"}
    (tmp / "inputs").mkdir(parents=True, exist_ok=True)
    (tmp / "inputs" / "in.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    with TestClient(A.app) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "Test-Passw0rd-xyz!"}, headers=H).status_code == 200
        for p in c.get("/api/pods", headers=H).json()["pods"]:   # 기본 파드는 쓰지 않는다
            c.put(f"/api/pods/{p['id']}", json={"enabled": False}, headers=H)
        r = c.post("/api/quick-run", json={"workflow": T2I, "workflow_filename": "t2i.json", "prompt": "1girl",
                                           "images": ["in.png"]}, headers=H)
        assert r.status_code == 200, r.text
        assert r.json()["job"]["status"] == "queued" and r.json()["ignored_images"] == ["in.png"]
        assert r.json()["job"]["options"]["main_prompt"] == "1girl"
        r = c.post("/api/quick-run", json={"workflow": I2I, "workflow_filename": "i2i.json", "prompt": "x",
                                           "images": ["in.png"]}, headers=H)
        assert r.status_code == 200, r.text
        assert r.json()["job"]["template_id"] == "input_image_batch"

        presets = c.get("/api/quick-run/presets", headers=H).json()["presets"]
        assert [p["filename"] for p in presets[:2]] == ["i2i.json", "t2i.json"] and presets[1]["kind"] == "t2i"
        r = c.post("/api/quick-run", json={"workflow_id": presets[1]["id"], "prompt": "again"}, headers=H)
        assert r.status_code == 200, r.text
        presets2 = c.get("/api/quick-run/presets", headers=H).json()["presets"]
        assert presets2[0]["id"] == presets[1]["id"] and len(presets2) == len(presets)  # 맨 앞으로, 새 항목 없음

        for body in ({"workflow": I2I, "prompt": "x"}, {"workflow": T2I, "prompt": " "},
                     {"workflow": OTHER_VIDEO, "prompt": "x"}, {"workflow_id": "nope", "prompt": "x"}):
            r = c.post("/api/quick-run", json=body, headers=H)
            assert r.status_code == 400, (body, r.text)
        r = c.post("/api/quick-run", json={"workflow": POSE_T2I, "prompt": "x"}, headers=H)
        assert r.status_code == 400, r.text  # OpenPose 프리셋인데 포즈 없음 → 서버 검사가 막는다


if __name__ == "__main__":
    test_classify()
    test_build()
    test_api()
    print("ok")
