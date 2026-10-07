"""NS-48-1 Library 게시물 추가 모달의 '갤러리' 모드와 피커 여러 장 선택 — 화면 정적 검사."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static/index.html").read_text(encoding="utf-8")
LIB = (ROOT / "static/js/17c-library.js").read_text(encoding="utf-8")
PICK = (ROOT / "static/js/03-newjob-refs-inputs.js").read_text(encoding="utf-8")


class GalleryPickUi(unittest.TestCase):
    def test_html(self):
        self.assertIn('data-mode="gallery"', HTML)
        self.assertIn('id="pose-add-gallery-btn"', HTML)
        self.assertIn('id="input-image-picker-done"', HTML)

    def test_library_sends_output_name(self):
        self.assertIn("fd.append('output_name'", LIB)
        self.assertIn("multiple: true", LIB)
        self.assertIn("openInputImageGalleryPicker(", LIB)
        # 모달을 열 때 고른 목록을 비운다
        self.assertIn("poseAddGalleryNames = [];", LIB)

    def test_picker_single_mode_kept(self):
        # 옵션 없는 호출은 한 장 클릭 → onPick(name) → 닫기 그대로
        self.assertIn("await inputImageGalleryOnPick(el.dataset.name);", PICK)
        self.assertIn("opts.multiple ?", PICK)

    def test_node_check(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node 없음")
        for f in ("static/js/17c-library.js", "static/js/03-newjob-refs-inputs.js"):
            r = subprocess.run([node, "--check", str(ROOT / f)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
