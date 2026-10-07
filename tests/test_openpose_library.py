"""NS-41-4 Library "이 포즈로 생성" — 고른 장을 입력 이미지 풀로 복사(주인 확인·재사용·GIF는 PNG)·danbooru_prompt 반환, 화면 진입 함수. 임시 데이터 폴더만 쓴다."""
import asyncio
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
os.environ.pop("NIGHTSHIFT_INPUT_IMAGES_DIR", None)
os.environ.setdefault("NIGHTSHIFT_ADMIN_USER", "admin")
os.environ.setdefault("NIGHTSHIFT_ADMIN_PASSWORD", "Test-Passw0rd-xyz!")
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")

import app as A
import auth
import input_assets
import pose_library as P
from fastapi import HTTPException
from PIL import Image


def img(fmt="PNG", size=(300, 400)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buf, fmt)
    return buf.getvalue()


def req(uid="u1", role="user"):
    user = {"id": uid, "role": role}
    auth.current_user.set(user)   # 미들웨어가 하는 일 — 입력 이미지 풀이 회원별 폴더를 고른다
    return SimpleNamespace(state=SimpleNamespace(user=user))


def call(*args):
    try:
        return 200, asyncio.run(A.library_pose_to_input(*args))
    except HTTPException as e:
        return e.status_code, e.detail


class ToInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pose = P.add_pose("u1", "앉기", "", "sitting, 1girl", [(img(), ""), (img("GIF", (100, 50)), "")])

    def test_owner_only(self):
        first = self.pose["images"][0]["id"]
        self.assertEqual(call(req("u2"), self.pose["id"], first)[0], 404)
        self.assertEqual(call(req(), self.pose["id"], 99999)[0], 404)
        self.assertEqual(call(req(), 99999, first)[0], 404)
        self.assertEqual(call(req("adm", "admin"), self.pose["id"], first)[0], 200)

    def test_copy_chosen_image_and_reuse(self):
        second = self.pose["images"][1]
        code, out = call(req(), self.pose["id"], second["id"])
        self.assertEqual(code, 200, out)
        self.assertEqual(out["danbooru_prompt"], "sitting, 1girl")
        self.assertEqual(out["name"], f"library_pose_{self.pose['id']}_{second['id']}.png")   # GIF → PNG
        path = input_assets.resolve_input_image(out["name"])
        self.assertIn(str(Path("users") / "u1"), str(path))   # 그 회원의 풀
        with Image.open(path) as im:
            self.assertEqual((im.format, im.size), ("PNG", (100, 50)))   # 고른 그 장
        before = len(input_assets.list_input_images())
        self.assertEqual(call(req(), self.pose["id"], second["id"]), (200, out))   # 다시 넣으면 재사용
        self.assertEqual(len(input_assets.list_input_images()), before)

    def test_ui_entry_points(self):
        lib = (ROOT / "static/js/17c-library.js").read_text(encoding="utf-8")
        wiz = (ROOT / "static/js/06-wizard.js").read_text(encoding="utf-8")
        html = (ROOT / "static/index.html").read_text(encoding="utf-8")
        self.assertIn("/to-input", lib)
        self.assertEqual(lib.count("startOpenPoseWizard("), 1)   # 그리드·상세·라이트박스가 한 함수로
        for marker in ("library-card-pose-btn", "library-item-pose-btn", "library-lightbox-pose-btn"):
            self.assertIn(marker, lib + html, marker)
        self.assertNotIn('<button class="library-card"', lib)   # 카드 안에 버튼을 넣으려고 div로
        self.assertIn("async function startOpenPoseWizard(stored, prompt)", wiz)
        body = wiz[wiz.index("async function startOpenPoseWizard"):]
        self.assertIn("wizard.post.openpose = true", body)
        apply = wiz[wiz.index("wizardPendingPose"):]
        self.assertTrue(re.search(r'data-name="pose_image"', apply))
        self.assertIn("suggestPoseSize", apply)
        self.assertIn(".enhance-raw", apply)


if __name__ == "__main__":
    unittest.main(verbosity=2)
