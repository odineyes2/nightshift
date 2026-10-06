"""NS-39-1 Library > Pose 서버 — DB v19·이미지 저장·URL 받기(SSRF 차단)·API·회원별 공개 범위. 임시 데이터 폴더만 쓴다."""
import asyncio
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
os.environ.setdefault("NIGHTSHIFT_ADMIN_USER", "admin")
os.environ.setdefault("NIGHTSHIFT_ADMIN_PASSWORD", "Test-Passw0rd-xyz!")
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")

import db

# 기존 DB(v18)에서 v19가 도는지 보려고 먼저 v18까지만 만든다.
_all = db.MIGRATIONS
db.MIGRATIONS = [m for m in _all if m[0] <= 18]
db.init()
db.MIGRATIONS = _all
db._initialized = False
db.init()

import app as A
import pose_library as P
from fastapi import HTTPException
from PIL import Image
from starlette.datastructures import UploadFile


def png(size=(800, 600)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 100, 50)).save(buf, "PNG")
    return buf.getvalue()


def req(uid="u1", role="user", form=None):
    async def read_form():
        return dict(form or {})
    return SimpleNamespace(state=SimpleNamespace(user={"id": uid, "role": role}), form=read_form)


def upload(data: bytes, name="pose.png"):
    return UploadFile(file=io.BytesIO(data), filename=name)


def call(fn, *args):
    try:
        out = fn(*args)
        return 200, asyncio.run(out) if asyncio.iscoroutine(out) else out
    except HTTPException as e:
        return e.status_code, e.detail


class PoseTests(unittest.TestCase):
    def test_1_migration(self):
        with db.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 19)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(poses)")}
        self.assertTrue({"name", "description", "danbooru_prompt", "image_file", "source_url", "owner_id"} <= cols)

    def test_2_add_file_and_list(self):
        code, a = call(A.add_library_pose, req(form={"name": " 앉기 ", "description": "d", "danbooru_prompt": "sitting",
                                                    "image": upload(png())}))
        self.assertEqual(code, 200, a)
        self.assertEqual((a["name"], a["danbooru_prompt"], a["owner_id"], a["source_url"]), ("앉기", "sitting", "u1", ""))
        thumb = P.image_path(a, True)
        self.assertTrue(P.image_path(a, False).is_file())
        with Image.open(thumb) as t:
            self.assertEqual(max(t.size), P.THUMB_SIZE)
        code, b = call(A.add_library_pose, req(form={"name": "서기", "image": upload(png((100, 100)))}))
        self.assertEqual(code, 200, b)
        items = call(A.list_library_poses, req())[1]["items"]
        self.assertEqual([i["name"] for i in items[:2]], ["서기", "앉기"])   # 최신순
        self.assertEqual(items[0]["thumb_url"], f"/api/library/poses/{b['id']}/thumb")
        self.assertEqual(call(A.get_library_pose_thumb, req(), a["id"])[0], 200)

    def test_3_owner_scope(self):
        code, mine = call(A.add_library_pose, req("u2", form={"name": "u2 것", "image": upload(png())}))
        self.assertEqual(code, 200)
        self.assertEqual([i["name"] for i in call(A.list_library_poses, req("u2"))[1]["items"]], ["u2 것"])
        self.assertNotIn("u2 것", [i["name"] for i in call(A.list_library_poses, req("u1"))[1]["items"]])
        self.assertIn("u2 것", [i["name"] for i in call(A.list_library_poses, req("adm", "admin"))[1]["items"]])
        self.assertEqual(call(A.get_library_pose_image, req("u1"), mine["id"])[0], 404)
        self.assertEqual(call(A.get_library_pose_image, req("adm", "admin"), mine["id"])[0], 200)

    def test_4_rejects(self):
        self.assertEqual(call(A.add_library_pose, req(form={"name": " ", "image": upload(png())}))[0], 400)
        self.assertEqual(call(A.add_library_pose, req(form={"name": "x"}))[0], 400)
        code, msg = call(A.add_library_pose, req(form={"name": "x", "image": upload(b"not an image", "a.png")}))
        self.assertEqual((code, msg), (400, "이미지 파일이 아니에요."))
        for url in ("http://127.0.0.1/a.png", "http://10.0.0.5/a.png", "http://localhost/a.png", "file:///etc/passwd"):
            self.assertEqual(call(A.add_library_pose, req(form={"name": "x", "image_url": url}))[0], 400, url)
        # 리다이렉트로 내부 주소에 가는 것도 막는다
        with self.assertRaises(P.PoseError):
            P._CheckedRedirect().redirect_request(None, None, 302, "", {}, "http://192.168.0.1/x")

    def test_5_add_url(self):
        data = png()

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        opener = SimpleNamespace(open=lambda r, timeout: Resp(data))
        with mock.patch.object(P, "assert_public_url"), mock.patch.object(P.urllib.request, "build_opener",
                                                                          return_value=opener):
            code, a = call(A.add_library_pose, req(form={"name": "URL 포즈", "image_url": "https://example.com/p.png"}))
            self.assertEqual(code, 200, a)
            self.assertEqual(a["source_url"], "https://example.com/p.png")
            self.assertTrue(P.image_path(a, True).is_file())
            # 상한보다 큰 응답은 거절한다
            with mock.patch.object(P, "MAX_BYTES", 10):
                self.assertEqual(call(A.add_library_pose, req(form={"name": "y", "image_url": "https://e.com/b"}))[0], 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
