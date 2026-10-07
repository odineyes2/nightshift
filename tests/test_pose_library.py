"""NS-39-1·NS-40-1 Library > Pose 서버 — DB v19·v20(복수 이미지)·이미지 저장·URL 받기(SSRF 차단)·API·회원별 공개 범위. 임시 데이터 폴더만 쓴다."""
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

# 기존 DB(v18)에서 v19가 도는지 보려고 먼저 v18까지만, 이어서 v19 게시물을 둔 채 v20이 도는지 본다.
_all = db.MIGRATIONS
for upto in (18, 19):
    db.MIGRATIONS = [m for m in _all if m[0] <= upto]
    db._initialized = False
    db.init()
db.MIGRATIONS = _all
_old = db._open()
_old.execute("INSERT INTO poses(id, name, image_file, source_url, owner_id, created_at)"
             " VALUES (500, '옛 포즈', '500.png', 'https://old.example/p.png', 'u9', 'x')")
_old.close()
db._initialized = False
db.init()

import app as A
import pose_library as P
from fastapi import HTTPException
from PIL import Image
from data_paths import data_dir
from starlette.datastructures import FormData, UploadFile


def png(size=(800, 600)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 100, 50)).save(buf, "PNG")
    return buf.getvalue()


def req(uid="u1", role="user", form=None):
    """form은 dict 또는 같은 키가 여러 번 오는 [(키, 값)] 목록."""
    async def read_form():
        return FormData(list(form.items()) if isinstance(form, dict) else (form or []))
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
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.MIGRATIONS[-1][0])
            cols = {r[1] for r in conn.execute("PRAGMA table_info(poses)")}
        self.assertTrue({"name", "description", "danbooru_prompt", "image_file", "source_url", "owner_id"} <= cols)
        # v19 게시물은 이미지 한 장으로 옮겨지고 파일 이름은 그대로라 옛 주소도 열린다
        (data_dir("library/poses") / "500.png").write_bytes(png())
        (data_dir("library/poses") / "500_thumb.webp").write_bytes(b"t")
        old = P.get_pose(500)
        self.assertEqual(old["image_count"], 1)
        self.assertEqual((old["images"][0]["image_file"], old["images"][0]["thumb_file"], old["source_url"]),
                         ("500.png", "500_thumb.webp", "https://old.example/p.png"))
        admin = req("adm", "admin")
        for fn in (A.get_library_pose_image, A.get_library_pose_thumb):
            self.assertEqual(call(fn, admin, 500)[0], 200)
        self.assertEqual(call(A.get_library_pose_image_n, admin, 500, old["images"][0]["id"])[0], 200)

    def test_2_add_file_and_list(self):
        code, a = call(A.add_library_pose, req(form={"name": " 앉기 ", "description": "d", "danbooru_prompt": "sitting",
                                                    "image": upload(png())}))
        self.assertEqual(code, 200, a)
        self.assertEqual((a["name"], a["danbooru_prompt"], a["owner_id"], a["source_url"]), ("앉기", "sitting", "u1", ""))
        self.assertEqual(a["image_count"], 1)
        thumb = P.image_path(a["images"][0], True)
        self.assertTrue(P.image_path(a["images"][0], False).is_file())
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
            self.assertTrue(P.image_path(a["images"][0], True).is_file())
            # 파일과 URL(줄마다 하나)을 섞어 한 게시물에
            form = [("name", "섞기"), ("image", upload(png())),
                    ("image_url", "https://example.com/a.png\n\nhttps://example.com/b.png")]
            code, m = call(A.add_library_pose, req(form=form))
            self.assertEqual(code, 200, m)
            self.assertEqual([i["source_url"] for i in m["images"]],
                             ["", "https://example.com/a.png", "https://example.com/b.png"])
            # 상한보다 큰 응답은 거절한다
            with mock.patch.object(P, "MAX_BYTES", 10):
                self.assertEqual(call(A.add_library_pose, req(form={"name": "y", "image_url": "https://e.com/b"}))[0], 400)

    def test_6_multiple_and_add_images(self):
        form = [("name", "여러 장")] + [("image", upload(png((100 + i, 100)))) for i in range(3)]
        code, a = call(A.add_library_pose, req(form=form))
        self.assertEqual(code, 200, a)
        self.assertEqual(a["image_count"], 3)
        self.assertEqual([i["position"] for i in a["images"]], [0, 1, 2])
        with Image.open(P.image_path(a["images"][1], False)) as im:
            self.assertEqual(im.size, (101, 100))   # position 순
        first = a["images"][0]
        self.assertEqual(first["thumb_url"], f"/api/library/poses/{a['id']}/images/{first['id']}/thumb")
        self.assertEqual(a["thumb_url"], f"/api/library/poses/{a['id']}/thumb")
        # 덧붙이기 — 주인·admin만, 남은 404
        self.assertEqual(call(A.add_library_pose_images, req("u2", form=[("image", upload(png()))]), a["id"])[0], 404)
        code, b = call(A.add_library_pose_images, req(form=[("image", upload(png()))]), a["id"])
        self.assertEqual((code, b["image_count"], b["images"][-1]["position"]), (200, 4, 3))
        code, b = call(A.add_library_pose_images, req("adm", "admin", form=[("image", upload(png()))]), a["id"])
        self.assertEqual((code, b["image_count"]), (200, 5))
        self.assertEqual(call(A.add_library_pose_images, req(form=[]), a["id"])[0], 400)
        self.assertEqual(call(A.add_library_pose_images, req(form=[("image", upload(png()))]), 99999)[0], 404)
        # 이미지별 주소 — 남은 404, 다른 게시물의 이미지 번호도 404
        img = b["images"][-1]["id"]
        self.assertEqual(call(A.get_library_pose_thumb_n, req(), a["id"], img)[0], 200)
        self.assertEqual(call(A.get_library_pose_image_n, req("u2"), a["id"], img)[0], 404)
        self.assertEqual(call(A.get_library_pose_image_n, req("adm", "admin"), 500, img)[0], 404)

    def test_7_all_or_nothing(self):
        def counts():
            with db.connect() as conn:
                return [conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("poses", "pose_images")]
        before, files = counts(), set(data_dir("library/poses").iterdir())
        bad = [("name", "x"), ("image", upload(png())), ("image", upload(b"nope", "b.png"))]
        self.assertEqual(call(A.add_library_pose, req(form=bad)), (400, "이미지 파일이 아니에요."))
        many = [("name", "x")] + [("image", upload(png((10, 10)))) for _ in range(P.MAX_IMAGES + 1)]
        self.assertEqual(call(A.add_library_pose, req(form=many))[0], 400)
        self.assertEqual(counts(), before)
        self.assertEqual(set(data_dir("library/poses").iterdir()), files)


if __name__ == "__main__":
    unittest.main(verbosity=2)
