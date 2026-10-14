"""NS-42-2 결과 추적 — 결과 PNG의 프롬프트 그래프에서 Library 포즈 게시물 id를 뽑아 색인 때 params_json.pose로 남기고,
갤러리 정보에 "포즈" 링크를 둔다. 임시 데이터 폴더만 쓴다."""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
    (Path(TMP.name) / key.lower()).mkdir(parents=True, exist_ok=True)
os.environ.setdefault("NIGHTSHIFT_ADMIN_USER", "admin")
os.environ.setdefault("NIGHTSHIFT_ADMIN_PASSWORD", "Test-Passw0rd-xyz!")
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")

import db

db.init()

import assets_index as AI
import pose_library as P
from PIL import Image
from PIL.PngImagePlugin import PngInfo


def graph(image_name, title="pose_image"):
    return {
        "1": {"class_type": "KSampler", "inputs": {"seed": 7, "steps": 20, "positive": ["2", 0], "negative": ["3", 0]}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "1girl, standing"}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "bad"}},
        "4": {"class_type": "LoadImage", "inputs": {"image": image_name}, "_meta": {"title": title}},
    }


def png_bytes(info=None):
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, "PNG", pnginfo=info)
    return buf.getvalue()


class PoseResultMeta(unittest.TestCase):
    def test_meta_from_graph_extracts_pose_ids(self):
        meta = AI.meta_from_graph(graph("library_pose_12_34.png"))
        self.assertEqual(json.loads(meta["params_json"])["pose"], {"id": 12, "image_id": 34})
        self.assertEqual(meta["seed"], 7)

    def test_other_images_are_ignored(self):
        for name, title in (("my_pose.png", "pose_image"), ("library_pose_12_34.png", "reference"),
                            ("library_pose_x_34.png", "pose_image")):
            params = json.loads(AI.meta_from_graph(graph(name, title)).get("params_json") or "{}")
            self.assertNotIn("pose", params, (name, title))

    def test_sync_stores_pose_name_or_id_only(self):
        pose = P.add_pose(1, "앉은 포즈", "", "sitting", [(png_bytes(), "")])
        image_id = pose["images"][0]["id"]
        out = Path(os.environ["NIGHTSHIFT_OUTPUT_DIR"])
        for fname, pid, iid in (("a.png", pose["id"], image_id), ("b.png", 9999, 1)):
            info = PngInfo()
            info.add_text("prompt", json.dumps(graph(f"library_pose_{pid}_{iid}.png")))
            (out / fname).write_bytes(png_bytes(info))
        AI.sync(force=True)
        with db.connect() as conn:
            rows = {r["path"]: json.loads(r["params_json"])
                    for r in conn.execute("SELECT path, params_json FROM assets WHERE path IN ('a.png','b.png')")}
        self.assertEqual(rows["a.png"]["pose"], {"id": pose["id"], "image_id": image_id, "name": "앉은 포즈"})
        self.assertEqual(rows["b.png"]["pose"], {"id": 9999, "image_id": 1})   # 지워진 게시물은 id만

    def test_zeroout_negative_is_empty_and_others_unchanged(self):
        krea = {"1": {"class_type": "KSampler", "inputs": {"seed": 1, "positive": ["2", 0], "negative": ["3", 0]}},
                "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "1girl"}},
                "3": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["2", 0]}}}
        meta = AI.meta_from_graph(krea)
        self.assertEqual(meta["prompt"], "1girl")
        self.assertFalse(meta.get("negative_prompt"))
        meta = AI.meta_from_graph(graph("x.png"))
        self.assertEqual((meta["prompt"], meta["negative_prompt"]), ("1girl, standing", "bad"))
        wan = {"1": {"class_type": "KSampler", "inputs": {"seed": 1, "positive": ["4", 0], "negative": ["4", 1]}},
               "4": {"class_type": "WanImageToVideo", "inputs": {"positive": ["2", 0], "negative": ["3", 0]}},
               "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "pos"}},
               "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "neg"}}}
        meta = AI.meta_from_graph(wan)
        self.assertEqual((meta["prompt"], meta["negative_prompt"]), ("pos", "neg"))

    def test_refine_chain_marks_prompt_enhance(self):
        refine = {"1": {"class_type": "KSampler", "inputs": {"seed": 1, "positive": ["2", 0], "negative": ["5", 0]}},
                  "2": {"class_type": "CLIPTextEncode", "inputs": {"text": ["3", 0], "clip": ["9", 0]}},
                  "3": {"class_type": "TextGenerate", "inputs": {"prompt": ["4", 0], "clip": ["9", 0]}},
                  "4": {"class_type": "PrimitiveStringMultiline", "_meta": {"title": "main_prompt"},
                        "inputs": {"value": "1girl"}},
                  "5": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["2", 0]}}}
        meta = AI.meta_from_graph(refine)
        self.assertEqual(meta["prompt"], "1girl")
        self.assertEqual(json.loads(meta["params_json"])["prompt_enhance"], {"kind": "workflow"})
        plain = AI.meta_from_graph(graph("x.png"))
        self.assertNotIn("prompt_enhance", json.loads(plain.get("params_json") or "{}"))

    def test_backfill_fixes_only_equal_negative_rows(self):
        krea = {"1": {"class_type": "KSampler", "inputs": {"seed": 1, "positive": ["2", 0], "negative": ["3", 0]}},
                "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "1girl"}},
                "3": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["2", 0]}}}
        out = Path(os.environ["NIGHTSHIFT_OUTPUT_DIR"])
        for fname, g in (("k.png", krea), ("n.png", graph("x.png"))):
            info = PngInfo()
            info.add_text("prompt", json.dumps(g))
            (out / fname).write_bytes(png_bytes(info))
        AI.sync(force=True)
        with db.connect() as conn:
            conn.execute("UPDATE assets SET negative_prompt=prompt WHERE path='k.png'")
            conn.execute("UPDATE assets SET negative_prompt='keep' WHERE path='n.png'")
            conn.execute("DELETE FROM meta WHERE key=?", (AI._BACKFILL_NEGATIVE_KEY,))
        AI.sync(force=True)
        with db.connect() as conn:
            rows = {r["path"]: r for r in conn.execute(
                "SELECT path, prompt, negative_prompt FROM assets WHERE path IN ('k.png','n.png')")}
        self.assertFalse(rows["k.png"]["negative_prompt"])
        self.assertEqual(rows["k.png"]["prompt"], "1girl")
        self.assertEqual(rows["n.png"]["negative_prompt"], "keep")

    def test_gallery_info_links_to_library(self):
        meta_js = (ROOT / "static/js/10-gallery-meta.js").read_text(encoding="utf-8")
        lib_js = (ROOT / "static/js/17c-library.js").read_text(encoding="utf-8")
        self.assertIn("row('해상도', dims)\n      + poseRow(params.pose)", meta_js)
        self.assertIn("data-pose-link", meta_js)
        self.assertIn("openLibraryPose(Number(poseLink.dataset.poseLink))", meta_js)
        self.assertIn("function openLibraryPose(id, tab = 'pose')", lib_js)
        self.assertIn("게시물을 찾을 수 없어요", lib_js)


if __name__ == "__main__":
    unittest.main()
