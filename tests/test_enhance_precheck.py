"""전처리 유형 노출과 Prompt Enhance 모델 사전 점검 검사 — python tests/test_enhance_precheck.py"""
import os
import sys
import tempfile
from pathlib import Path

tmp = tempfile.mkdtemp()
os.environ.update(NIGHTSHIFT_DATA_DIR=tmp, NIGHTSHIFT_OUTPUT_DIR=tmp + "/out", NIGHTSHIFT_ASSETS_DIR=tmp + "/a",
                  NIGHTSHIFT_ADMIN_USER="admin", NIGHTSHIFT_ADMIN_PASSWORD="Test-Passw0rd-xyz!")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import app

ns = vars(app)
assert ns["get_workflow_types"]()["pre"][0]["id"] == "prompt_enhance"

M = ns["ENHANCER_MODEL"]


def info(names):
    return {"CLIPLoader": {"input": {"required": {"clip_name": [names]}}}}


ns["fetch_comfy_object_info"] = lambda f=False, p=None: (None, info(["x.safetensors"]))
assert ns["enhancer_model_missing"](None) is True
ns["fetch_comfy_object_info"] = lambda f=False, p=None: (None, info([M]))
assert ns["enhancer_model_missing"](None) is False
ns["fetch_comfy_object_info"] = lambda f=False, p=None: (None, None)
assert ns["enhancer_model_missing"](None) is False   # 꺼진 파드는 판단하지 않는다
print("ok")
