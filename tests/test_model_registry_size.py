"""NS-109-1 등록부 용량 칸(DB v29)·저장·실측 기록 함수. 임시 데이터 폴더만 쓴다."""
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

db.init()
GB = 1024 ** 3


class SizeTests(unittest.TestCase):
    def test_1_migration(self):
        with db.connect() as conn:
            self.assertGreaterEqual(conn.execute("PRAGMA user_version").fetchone()[0], 29)
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(models)")}
        self.assertTrue({"size_bytes", "size_source"} <= cols)

    def test_2_upsert_manual_and_keep(self):
        e = R.upsert("checkpoints", "a.safetensors", {"size_bytes": 5 * GB})
        self.assertEqual((e["size_bytes"], e["size_source"]), (5 * GB, "manual"))   # 크기만 있어도 유지
        self.assertEqual(R.get_entry("checkpoints", "a.safetensors")["size_bytes"], 5 * GB)
        e = R.upsert("checkpoints", "a.safetensors", {"notes": "x"})   # 다른 필드만 바꿔도 크기 그대로
        self.assertEqual((e["size_bytes"], e["size_source"]), (5 * GB, "manual"))
        e = R.upsert("checkpoints", "a.safetensors", {"size_bytes": None, "notes": ""})
        self.assertIsNone(e)   # 크기를 비우면 빈 항목이라 지워진다

    def test_3_auto_source(self):
        e = R.upsert("loras", "b.safetensors", {"size_bytes": 7, "size_source": "auto"})
        self.assertEqual(e["size_source"], "auto")
        with self.assertRaises(R.RegistryError):
            R.upsert("loras", "b.safetensors", {"size_bytes": 7, "size_source": "measured"})

    def test_4_invalid_values(self):
        for bad in (0, -1, 1.5, "100", True, R.MAX_SIZE_BYTES + 1):
            with self.assertRaises(R.RegistryError, msg=repr(bad)):
                R.upsert("checkpoints", "c.safetensors", {"size_bytes": bad})
        self.assertIsNone(R.get_entry("checkpoints", "c.safetensors"))

    def test_5_record_measured(self):
        R.upsert("vae", "v.safetensors", {"size_bytes": 100, "notes": "n"})
        changed = R.record_measured_sizes("vae", {"v.safetensors": 200, "none.safetensors": 300, "bad": -5})
        self.assertEqual(changed, [{"kind": "vae", "filename": "v.safetensors",
                                    "size_bytes": 200, "size_source": "measured"}])
        e = R.get_entry("vae", "v.safetensors")
        self.assertEqual((e["size_bytes"], e["size_source"], e["notes"]), (200, "measured", "n"))
        self.assertIsNone(R.get_entry("vae", "none.safetensors"))   # 행을 새로 만들지 않는다
        self.assertEqual(R.record_measured_sizes("vae", {"v.safetensors": 200}), [])   # 같으면 변화 없음
        self.assertEqual(len(R.record_measured_sizes("vae", {"v.safetensors": 250})), 1)   # 다르면 덮어씀

    def test_6_measured_overwrites_when_source_differs(self):
        R.upsert("vae", "w.safetensors", {"size_bytes": 50, "size_source": "auto"})
        self.assertEqual(len(R.record_measured_sizes("vae", {"w.safetensors": 50})), 1)   # 값 같아도 출처가 실측으로
        self.assertEqual(R.get_entry("vae", "w.safetensors")["size_source"], "measured")


if __name__ == "__main__":
    unittest.main()
