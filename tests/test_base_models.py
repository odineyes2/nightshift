"""NS-35-1 베이스 모델 목록(DB v17)과 추가·변경·삭제. 임시 데이터 폴더만 쓴다."""
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


if __name__ == "__main__":
    unittest.main()
