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
