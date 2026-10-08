"""NS-54-1 등록부 모델 단위 스윗 포인트 — db v22·upsert 검사·PUT /api/models. 임시 데이터 폴더만 쓴다."""
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
import app as A
from fastapi import HTTPException


def put(body, role="admin"):
    async def read():
        return json.dumps(body).encode()
    r = SimpleNamespace(state=SimpleNamespace(user={"id": "u", "role": role}), body=read)
    try:
        return 200, asyncio.run(A.put_model_registry_entry(r))
    except HTTPException as e:
        return e.status_code, e.detail


class ModelSweetTests(unittest.TestCase):
    def test_1_column(self):
        with db.connect() as conn:
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(models)").fetchall()]
            self.assertGreaterEqual(conn.execute("PRAGMA user_version").fetchone()[0], 22)
        self.assertIn("sweet_json", cols)

    def test_2_roundtrip(self):
        ck = {"cfg": "5.5", "steps": 28, "sampler_name": "dpmpp_2m", "scheduler": "karras",
              "positive_prefix": " masterpiece ", "negative": "lowres", "clip_skip": 2}
        for kind in ("checkpoints", "diffusion_models"):
            e = R.upsert(kind, "m.safetensors", {"sweet": ck})
            want = {"cfg": 5.5, "steps": 28, "sampler_name": "dpmpp_2m", "scheduler": "karras",
                    "positive_prefix": "masterpiece", "negative": "lowres", "clip_skip": 2}
            self.assertEqual(e["sweet"], want)
            self.assertEqual(R.get_entry(kind, "m.safetensors")["sweet"], want)
        lo = {"strength": 0.8, "strength_min": 0.6, "strength_max": 0.9, "strength_clip": 1}
        R.upsert("loras", "l.safetensors", {"sweet": lo})
        self.assertEqual(R.get_entry("loras", "l.safetensors")["sweet"], {**lo, "strength_clip": 1.0})
        # 다른 칸만 바꾸면 sweet는 그대로
        R.upsert("loras", "l.safetensors", {"notes": "메모"})
        self.assertEqual(R.get_entry("loras", "l.safetensors")["sweet"]["strength"], 0.8)

    def test_3_reject(self):
        bad = [("checkpoints", {"cfg": 101}), ("checkpoints", {"cfg": "x"}), ("checkpoints", {"steps": 0}),
               ("checkpoints", {"steps": 1.5}), ("checkpoints", {"sampler_name": "euler; rm"}),
               ("checkpoints", {"clip_skip": 13}), ("checkpoints", {"strength": 1}),
               ("diffusion_models", {"nope": 1}), ("loras", {"strength": 11}), ("loras", {"cfg": 5}),
               ("loras", {"strength_min": 0.9, "strength_max": 0.5}), ("loras", "x")]
        for kind, sweet in bad:
            self.assertEqual(put({"kind": kind, "filename": "bad.safetensors", "sweet": sweet})[0], 400, sweet)
        self.assertIsNone(R.get_entry("checkpoints", "bad.safetensors"))

    def test_4_empty_rule(self):
        R.upsert("checkpoints", "only.safetensors", {"sweet": {"cfg": 7}})
        self.assertIsNotNone(R.get_entry("checkpoints", "only.safetensors"))
        self.assertIsNone(R.upsert("checkpoints", "only.safetensors", {"sweet": {"cfg": ""}}))
        self.assertIsNone(R.get_entry("checkpoints", "only.safetensors"))

    def test_5_other_kinds(self):
        e = R.upsert("vae", "v.safetensors", {"notes": "n", "sweet": {"cfg": 7, "아무거나": 1}})
        self.assertEqual(e["sweet"], {})
        self.assertIsNone(R.upsert("vae", "w.safetensors", {"sweet": {"cfg": 7}}))

    def test_6_api(self):
        body = {"kind": "loras", "filename": "api.safetensors", "sweet": {"strength": "0.7"}}
        self.assertEqual(put(body, "user")[0], 403)
        code, out = put(body)
        self.assertEqual((code, out["entry"]["sweet"]), (200, {"strength": 0.7}))
        self.assertEqual([e["sweet"] for e in R.list_entries() if e["filename"] == "api.safetensors"],
                         [{"strength": 0.7}])


if __name__ == "__main__":
    unittest.main()
