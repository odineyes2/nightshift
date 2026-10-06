"""NS-38-4 베이스 모델 세부 설정 — 소속 모델 보기·추가·제외와 UNet 구성 점검. 임시 데이터 폴더만 쓴다."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
os.environ.setdefault("NIGHTSHIFT_ADMIN_USER", "admin")
os.environ.setdefault("NIGHTSHIFT_ADMIN_PASSWORD", "Test-Passw0rd-xyz!")
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")

import db
import model_registry as R

db.init()
R.add_base_model("Anima")
import app as A
from fastapi import HTTPException


def req(role="admin", body=None):
    async def read():
        return json.dumps(body or {}).encode()
    return SimpleNamespace(state=SimpleNamespace(user={"id": "u", "role": role}), body=read)


def call(fn, *args, **kw):
    try:
        out = fn(*args, **kw)
        return 200, asyncio.run(out) if asyncio.iscoroutine(out) else out
    except HTTPException as e:
        return e.status_code, e.detail


class DetailTests(unittest.TestCase):
    def test_1_detail_parts(self):
        R.upsert("diffusion_models", "anima.safetensors", {"base_models": ["Anima"]})
        R.upsert("loras", "a_lora.safetensors", {"base_models": ["Anima", "SDXL"]})
        code, d = call(A.base_model_detail, req(), "anima")
        self.assertEqual(code, 200)
        self.assertEqual(d["name"], "Anima")
        self.assertEqual(d["members"], {"diffusion_models": ["anima.safetensors"], "loras": ["a_lora.safetensors"]})
        asm = d["assembly"]
        self.assertEqual(asm["unet"], ["anima.safetensors"])
        self.assertEqual(asm["parts"]["vae"], {"filename": "qwen_image_vae.safetensors", "source": "default"})
        self.assertEqual(asm["missing"], [])
        self.assertFalse(asm["dedicated"])
        # 등록부 지정이 코드 기본값보다 우선한다
        R.upsert("text_encoders", "my_te.safetensors", {"base_models": ["Anima"]})
        asm = R.base_model_detail("Anima")["assembly"]
        self.assertEqual(asm["parts"]["clip"], {"filename": "my_te.safetensors", "source": "registry"})
        self.assertEqual(call(A.base_model_detail, req("user"), "Anima")[0], 403)

    def test_2_missing_and_checkpoint(self):
        R.upsert("diffusion_models", "q.safetensors", {"base_models": ["Qwen-Image"]})
        asm = R.base_model_detail("Qwen-Image")["assembly"]
        self.assertEqual(asm["missing"], ["clip", "vae"])
        R.upsert("checkpoints", "s.safetensors", {"base_models": ["SDXL"]})
        self.assertIsNone(R.base_model_detail("SDXL")["assembly"])
        self.assertTrue(R.base_model_detail("krea.2") is not None)
        R.upsert("diffusion_models", "k.safetensors", {"base_models": ["krea.2"]})
        self.assertTrue(R.base_model_detail("krea.2")["assembly"]["dedicated"])

    def test_3_membership(self):
        def put(body, role="admin"):
            return call(A.set_base_model_member, req(role, body))
        body = {"name": "Anima", "kind": "vae", "filename": "shared_vae.safetensors", "member": True}
        self.assertEqual(put(body, "user")[0], 403)
        self.assertEqual(put({**body, "name": "없는이름"})[0], 400)
        self.assertEqual(put({**body, "kind": "nope"})[0], 400)
        R.upsert("vae", "shared_vae.safetensors", {"base_models": ["Qwen-Image"]})
        code, out = put(body)
        self.assertEqual((code, out["entry"]["base_models"]), (200, ["Qwen-Image", "Anima"]))
        # 빼면 연결만 풀린다 — 다른 소속은 그대로
        self.assertEqual(put({**body, "member": False})[1]["entry"]["base_models"], ["Qwen-Image"])
        self.assertEqual(put({**body, "member": False})[0], 400)   # 이미 빠졌다
        # 마지막 소속을 빼면 빈 목록, 다른 정보(메모)가 있으면 행은 남는다
        R.upsert("vae", "shared_vae.safetensors", {"notes": "메모"})
        out = put({**body, "name": "Qwen-Image", "member": False})[1]["entry"]
        self.assertEqual((out["base_models"], out["base_model"]), ([], ""))
        self.assertIsNotNone(R.get_entry("vae", "shared_vae.safetensors"))
        # 다른 정보가 없으면 행이 지워진다
        R.upsert("vae", "lone.safetensors", {"base_models": ["Anima"]})
        self.assertIsNone(put({**body, "filename": "lone.safetensors", "member": False})[1]["entry"])
        self.assertIsNone(R.get_entry("vae", "lone.safetensors"))

    def test_4_workflow_types(self):
        def put(body, role="admin"):
            return call(A.set_base_model_workflow_types, req(role, body))
        families = lambda: call(A.get_base_model_families, req(), "auto")[1]
        R.upsert("diffusion_models", "anima.safetensors", {"base_models": ["Anima"]})
        # NULL(자동)이면 workflow_types가 None — 마법사는 지금과 같은 호환 규칙만 쓴다
        self.assertIsNone(families()["anima"]["workflow_types"])
        self.assertIsNone(R.base_model_detail("Anima")["workflow_types"])
        self.assertEqual(put({"name": "Anima", "types": ["txt2img"]}, "user")[0], 403)
        self.assertEqual(put({"name": "Anima", "types": ["nope"]})[0], 400)
        self.assertEqual(put({"name": "Anima", "types": ["txt2img", "txt2img"]})[0], 400)
        self.assertEqual(put({"name": "Anima", "types": "txt2img"})[0], 400)
        self.assertEqual(put({"name": "없는이름", "types": []})[0], 400)
        code, out = put({"name": "anima", "types": ["face_detailer", "txt2img", "hires_fix"]})
        self.assertEqual((code, out["types"]), (200, ["face_detailer", "txt2img", "hires_fix"]))
        self.assertEqual(families()["anima"]["workflow_types"], ["face_detailer", "txt2img", "hires_fix"])
        self.assertEqual(R.base_model_detail("Anima")["workflow_types"], ["face_detailer", "txt2img", "hires_fix"])
        # null로 자동으로 되돌린다
        self.assertEqual(put({"name": "Anima", "types": None}), (200, {"types": None}))
        self.assertIsNone(families()["anima"]["workflow_types"])

    def test_5_sweet(self):
        def put(body, role="admin"):
            return call(A.set_base_model_sweet, req(role, body))
        R.upsert("checkpoints", "s.safetensors", {"base_models": ["SDXL"]})
        R.add_base_model("SDXL") if "SDXL" not in [b["name"] for b in R.list_base_models()] else None
        families = lambda: call(A.get_base_model_families, req(), "auto")[1]
        self.assertEqual(families()["sdxl"]["sweet"], {})
        good = {"cfg": 4.5, "steps": "30", "sampler_name": "euler_ancestral", "positive_prefix": "masterpiece",
                "negative": "lowres, bad hands", "prompt_tips": "태그로 쓰세요", "scheduler": None}
        self.assertEqual(put({"name": "SDXL", "sweet": good}, "user")[0], 403)
        self.assertEqual(put({"name": "SDXL", "sweet": good})[0], 400)   # 모르는 칸
        del good["scheduler"]
        for bad in ({"steps": 0}, {"steps": 2.5}, {"steps": True}, {"cfg": -1}, {"cfg": "x"},
                    {"sampler_name": "euler; rm"}, {"negative": "x" * 2001}, {"prompt_tips": 3}):
            self.assertEqual(put({"name": "SDXL", "sweet": bad})[0], 400, bad)
        self.assertEqual(put({"name": "없는이름", "sweet": {}})[0], 400)
        self.assertEqual(put({"name": "SDXL", "sweet": "x"})[0], 400)
        code, out = put({"name": "sdxl", "sweet": {**good, "positive_prefix": ""}})
        self.assertEqual(code, 200)
        self.assertEqual(out["sweet"], {"cfg": 4.5, "steps": 30, "sampler_name": "euler_ancestral",
                                        "negative": "lowres, bad hands", "prompt_tips": "태그로 쓰세요"})
        sweet = families()["sdxl"]["sweet"]
        self.assertEqual(sweet, out["sweet"])
        self.assertEqual(R.base_model_detail("SDXL")["sweet"], out["sweet"])
        # 마법사처럼 spec에 넣으면 KSampler·negative_prompt 노드에 들어간다
        import workflow_builder
        spec = {"base": "txt2img", "checkpoint": "s.safetensors", "positive": "(prompt)",
                **{k: sweet[k] for k in ("cfg", "steps", "sampler_name", "negative")}}
        wf = workflow_builder.build_workflow(spec)
        ks = next(n for n in wf.values() if n["class_type"] == "KSampler")["inputs"]
        self.assertEqual((ks["cfg"], ks["steps"], ks["sampler_name"]), (4.5, 30, "euler_ancestral"))
        neg = next(n for n in wf.values() if n.get("_meta", {}).get("title") == "negative_prompt")
        self.assertEqual(neg["inputs"]["text"], "lowres, bad hands")


if __name__ == "__main__":
    unittest.main()
