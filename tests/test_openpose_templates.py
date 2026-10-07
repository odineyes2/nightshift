"""OpenPose 포즈 이미지 주입·제출 검사(NS-41-2). python tests/test_openpose_templates.py"""
import ast
import csv
import importlib.util
import io
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_template(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "templates" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def workflow():
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": "in.png"}, "_meta": {"title": "input_image"}},
        "2": {"class_type": "LoadImage", "inputs": {"image": "x.png"}, "_meta": {"title": "pose_image"}},
        "3": {"class_type": "DWPreprocessor", "inputs": {"image": ["2", 0]}},
    }


def test_templates(tmp):
    os.environ["NIGHTSHIFT_INPUT_IMAGES_DIR"] = str(tmp)
    (tmp / "a.png").write_bytes(b"a")
    (tmp / "b.png").write_bytes(b"b")
    for name in ("seed_batch", "csv_batch", "input_image_batch", "input_image_csv_batch"):
        mod = load_template(name)
        uploads = []
        mod.upload_image_to_comfy = lambda url, path: uploads.append(path.name) or f"up_{path.name}"
        wf = workflow()
        mod.apply_pose_image(wf, "http://c", "a.png")
        assert wf["2"]["inputs"]["image"] == "up_a.png", name
        assert wf["1"]["inputs"]["image"] == "in.png", name  # img2img 입력은 그대로
        wf = workflow()
        mod.apply_pose_image(wf, "http://c", "")  # 포즈 없으면 아무것도 안 함
        assert wf["2"]["inputs"]["image"] == "x.png" and uploads == ["a.png"], name
        plain = {"9": {"class_type": "KSampler", "inputs": {}}}
        mod.apply_pose_image(plain, "http://c", "a.png")  # 포즈 노드 없는 워크플로우는 건너뜀
        assert uploads == ["a.png"], name


def test_template_wiring():
    # 행의 pose_image가 우선, 비면 POSE_IMAGE 옵션
    for name in ("csv_batch", "input_image_csv_batch"):
        src = (ROOT / "templates" / f"{name}.py").read_text(encoding="utf-8")
        assert 'apply_pose_image(workflow, comfy_url, row.get("pose_image") or env("POSE_IMAGE"))' in src, name
    for name in ("seed_batch", "input_image_batch"):
        src = (ROOT / "templates" / f"{name}.py").read_text(encoding="utf-8")
        assert 'apply_pose_image(workflow, comfy_url, env("POSE_IMAGE"))' in src, name


def test_upload_cache(tmp):
    # 같은 파일은 한 번만 올린다(모의 /upload/image)
    mod = load_template("seed_batch")
    calls = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"name": "up.png"}).encode()

    mod.urllib.request.urlopen = lambda req, timeout=0: calls.append(req.full_url) or Resp()
    for _ in range(3):
        wf = workflow()
        mod.apply_pose_image(wf, "http://c", "a.png")
        assert wf["2"]["inputs"]["image"] == "up.png"
    assert calls == ["http://c/upload/image"]


def test_manifest():
    manifest = json.loads((ROOT / "templates" / "manifest.json").read_text(encoding="utf-8"))
    by_id = {t["id"]: t for t in manifest}
    assert "pose_batch" not in by_id and "pose_csv_batch" not in by_id
    for tid in ("seed_batch", "csv_batch", "input_image_batch", "input_image_csv_batch"):
        opts = {o["name"]: o for o in by_id[tid]["options"]}
        assert opts["pose_image"]["type"] == "input_image_optional", tid
    for tid in ("csv_batch", "input_image_csv_batch"):
        assert "pose_image" in {c["name"] for c in by_id[tid]["csv_columns"]}, tid
    wf_src = (ROOT / "server" / "app_parts" / "09-workflows.py").read_text(encoding="utf-8")
    assert "openpose_cn" not in wf_src


class HTTPException(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status


class InputAssetError(Exception):
    pass


def submit_validators(tmp):
    src = (ROOT / "server" / "app_parts" / "10-job-submit.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    keep = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("validate_pose_image", "validate_flat_image_csv_rows")]

    def resolve_input_image(name):
        if not (tmp / name).is_file():
            raise InputAssetError("없는 파일")
        return tmp / name

    ns = {"json": json, "csv": csv, "io": io, "HTTPException": HTTPException,
          "InputAssetError": InputAssetError, "resolve_input_image": resolve_input_image}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "10-job-submit", "exec"), ns)
    return ns["validate_pose_image"]


def expect_400(fn, *args):
    try:
        fn(*args)
    except HTTPException as e:
        assert e.status == 400
        return
    raise AssertionError("400이 나와야 한다")


def test_submit(tmp):
    check = submit_validators(tmp)
    wf = json.dumps(workflow()).encode()
    plain = json.dumps({"9": {"class_type": "KSampler"}}).encode()
    check(plain, None, "")  # ControlNet 없는 작업은 그대로
    expect_400(check, wf, None, "")  # 시드: 포즈 없음
    expect_400(check, wf, None, "nope.png")  # 없는 파일
    check(wf, None, "a.png")
    rows = "main_prompt,pose_image\nx,a.png\ny,\n".encode()
    expect_400(check, wf, rows, "")  # 빈 행에 기본값 없음
    check(wf, rows, "b.png")  # 기본값이 메움
    check(wf, "main_prompt,pose_image\nx,a.png\ny,b.png\n".encode(), "")  # 행별 포즈
    expect_400(check, wf, "main_prompt,pose_image\nx,nope.png\n".encode(), "a.png")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_templates(tmp)
        test_template_wiring()
        test_upload_cache(tmp)
        test_manifest()
        test_submit(tmp)
    print("ok")
