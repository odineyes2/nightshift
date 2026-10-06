"""NS-35-1 베이스 모델 목록(DB v17)과 추가·변경·삭제, NS-38-1 복수 베이스 모델(v18 연결 표). 임시 데이터 폴더만 쓴다."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
sys.path.insert(0, str(ROOT / "server"))

import db
import model_registry as R

# v16까지만 올린 DB에 목록에 없는 값을 쓰는 등록부 항목을 넣고, 그 뒤 v17을 돌린다.
_all = db.MIGRATIONS
db.MIGRATIONS = [m for m in _all if m[0] <= 16]
db.init()
with db.connect() as conn:
    for fn, base in (("old1.safetensors", "Legacy"), ("old2.safetensors", "legacy"), ("x.safetensors", "sdxl")):
        conn.execute("INSERT INTO models(kind, filename, base_model, updated_at) VALUES('checkpoints', ?, ?, ?)",
                     (fn, base, db.now_iso()))
db.MIGRATIONS = _all
db._initialized = False
db.init()

DEFAULTS = ["SD 1.5", "SDXL", "Illustrious", "Pony", "NoobAI", "Flux", "Wan 2.2", "Wan 2.1", "Qwen-Image",
            "Z-Image", "krea.2", "MiniMax-H3", "기타"]


def names():
    return [b["name"] for b in R.list_base_models()]


class BaseModelTests(unittest.TestCase):
    def test_1_migration(self):
        got = R.list_base_models()
        self.assertEqual([b["name"] for b in got[:13]], DEFAULTS)
        rest = [b["name"].lower() for b in got[13:]]
        self.assertEqual(rest, ["legacy"])   # 대소문자만 다른 값은 하나로
        self.assertEqual({b["name"]: b["count"] for b in got}["SDXL"], 1)
        # v18: 예전 base_model 한 칸이 연결 표로 옮겨지고 사본 칸도 그대로다
        e = R.get_entry("checkpoints", "old2.safetensors")
        self.assertEqual((e["base_model"], e["base_models"]), ("legacy", ["legacy"]))
        with db.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.MIGRATIONS[-1][0])
            row = conn.execute("SELECT position, workflow_types_json, sweet_json FROM base_models WHERE name='SDXL'").fetchone()
        self.assertEqual(tuple(row), (2, None, "{}"))

    def test_shared_vae_two_bases(self):
        R.add_base_model("Anima2")
        R.upsert("diffusion_models", "anima2.safetensors", {"base_model": "Anima2"})
        R.upsert("diffusion_models", "qwen.safetensors", {"base_models": ["Qwen-Image"]})
        R.upsert("text_encoders", "te.safetensors", {"base_models": ["Anima2"]})
        e = R.upsert("vae", "shared_vae.safetensors", {"base_models": ["Anima2", " qwen-image ", "ANIMA2", ""]})
        self.assertEqual((e["base_model"], e["base_models"]), ("Anima2", ["Anima2", "qwen-image"]))
        self.assertEqual(R.get_entry("vae", "shared_vae.safetensors")["base_models"], ["Anima2", "qwen-image"])
        self.assertEqual(R.unet_image_parts("anima2")["vae"], "shared_vae.safetensors")
        self.assertEqual(R.unet_image_parts("qwen-image")["vae"], "shared_vae.safetensors")
        g = R.checkpoint_groups()
        self.assertEqual(g["anima2"]["unet_image"], {"clip": "te.safetensors", "vae": "shared_vae.safetensors"})
        self.assertEqual(g["qwen-image"]["unet_image_missing"], ["clip"])
        # 체크포인트 하나가 두 계열 모두에 들어간다
        R.upsert("checkpoints", "both.safetensors", {"base_models": ["SDXL", "Pony"]})
        g = R.checkpoint_groups()
        self.assertIn("both.safetensors", g["sdxl"]["checkpoints"])
        self.assertIn("both.safetensors", g["pony"]["checkpoints"])
        # LoRA base_ids
        R.upsert("loras", "l.safetensors", {"base_models": ["Anima2", "Qwen-Image"], "trigger_keyword": "t"})
        t = R.lora_triggers()["l.safetensors"]
        self.assertEqual((t["base_id"], t["base_ids"]), ("anima2", ["anima2", "qwen-image"]))
        # 사용 수·이름 변경·삭제 거부
        counts = {b["name"]: b["count"] for b in R.list_base_models()}
        self.assertEqual(counts["Anima2"], 4)
        R.rename_base_model("Anima2", "Anima3")
        self.assertEqual(R.get_entry("vae", "shared_vae.safetensors")["base_models"], ["Anima3", "qwen-image"])
        self.assertEqual(R.lora_triggers()["l.safetensors"]["base_ids"], ["anima3", "qwen-image"])
        with self.assertRaises(R.RegistryError) as cm:
            R.delete_base_model("Anima3")
        self.assertIn("4개", str(cm.exception))
        # 하나만 빼면 나머지는 남고, 다 빼면 다른 정보가 없을 때 행이 사라진다
        e = R.upsert("vae", "shared_vae.safetensors", {"base_models": ["qwen-image"]})
        self.assertEqual(e["base_models"], ["qwen-image"])
        self.assertIsNone(R.upsert("vae", "shared_vae.safetensors", {"base_models": []}))
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM model_base_models WHERE filename='shared_vae.safetensors'")
                             .fetchone()[0], 0)
        with self.assertRaises(R.RegistryError):
            R.upsert("vae", "x.safetensors", {"base_models": "SDXL"})

    def test_add_and_duplicate(self):
        R.add_base_model("  NewBase ")
        self.assertEqual(names()[-1], "NewBase")
        with self.assertRaises(R.RegistryError):
            R.add_base_model("newbase")
        with self.assertRaises(R.RegistryError):
            R.add_base_model("   ")
        with self.assertRaises(R.RegistryError):
            R.add_base_model("x" * 101)

    def test_rename_updates_registry(self):
        R.add_base_model("Ren")
        R.upsert("checkpoints", "ren.safetensors", {"base_model": "ren"})
        with self.assertRaises(R.RegistryError):
            R.rename_base_model("Ren", "sdxl")
        R.rename_base_model("Ren", "REN")   # 대소문자만 바꾸기는 된다
        R.rename_base_model("REN", "Renamed")
        self.assertIn("Renamed", names())
        self.assertNotIn("REN", names())
        self.assertEqual(R.get_entry("checkpoints", "ren.safetensors")["base_model"], "Renamed")
        g = R.checkpoint_groups()["renamed"]
        self.assertEqual((g["label"], g["checkpoints"]), ("Renamed", ["ren.safetensors"]))
        with self.assertRaises(R.RegistryError):
            R.rename_base_model("없는이름", "아무거나")

    def test_delete(self):
        R.add_base_model("Del")
        R.upsert("loras", "d.safetensors", {"base_model": "del"})
        with self.assertRaises(R.RegistryError) as cm:
            R.delete_base_model("Del")
        self.assertIn("1개", str(cm.exception))
        R.upsert("loras", "d.safetensors", {"base_model": ""})
        R.delete_base_model("Del")
        self.assertNotIn("Del", names())

    def test_anima_unet_image(self):
        R.add_base_model("Anima")
        R.upsert("diffusion_models", "anima.safetensors", {"base_model": "Anima"})
        R.upsert("text_encoders", "qwen_3_06b.safetensors", {"base_model": "Anima"})
        R.upsert("vae", "qwen_image_vae.safetensors", {"base_model": "Anima"})
        self.assertEqual(R.checkpoint_groups()["anima"]["unet_image"],
                         {"clip": "qwen_3_06b.safetensors", "vae": "qwen_image_vae.safetensors"})

    def test_z_order(self):
        """NS-38-3 PUT /api/base-models/order — 관리자만, 목록 전체가 한 번씩 있어야 하고 마법사 1단계가 그 순서를 따른다."""
        import asyncio
        import json
        from types import SimpleNamespace
        os.environ.setdefault("NIGHTSHIFT_ADMIN_USER", "admin")
        os.environ.setdefault("NIGHTSHIFT_ADMIN_PASSWORD", "Test-Passw0rd-xyz!")
        os.chdir(ROOT / "server")
        import app as A
        from fastapi import HTTPException

        def put(names, role="admin"):
            async def body():
                return json.dumps({"names": names}).encode()
            req = SimpleNamespace(state=SimpleNamespace(user={"id": "u", "role": role}), body=body)
            try:
                return 200, asyncio.run(A.reorder_base_models(req))
            except HTTPException as e:
                return e.status_code, e.detail

        R.upsert("checkpoints", "o_sdxl.safetensors", {"base_models": ["SDXL"]})
        R.upsert("checkpoints", "o_pony.safetensors", {"base_models": ["Pony"]})
        cur = names()
        new = list(reversed(cur))
        self.assertEqual(put(new, role="user")[0], 403)
        self.assertEqual(put(new[1:])[0], 400)                 # 빠진 이름
        self.assertEqual(put(new + ["없는이름"])[0], 400)       # 모르는 이름
        self.assertEqual(put(new + [new[0]])[0], 400)          # 중복
        self.assertEqual(put("SDXL")[0], 400)
        code, out = put([n.upper() for n in new])               # 대소문자는 무시하고 저장된 이름을 돌려준다
        self.assertEqual((code, out["names"]), (200, new))
        self.assertEqual(names(), new)
        ids = list(R.checkpoint_groups())
        self.assertLess(ids.index("pony"), ids.index("sdxl"))   # 뒤집었으니 Pony가 SDXL보다 앞


if __name__ == "__main__":
    unittest.main()
