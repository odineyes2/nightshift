"""NS-61-1 아티클 대표 이미지 — DB v25·PATCH cover_image_id·대표 삭제·순차 to-input 대표 1장·이동 뒤 대표 유지.
임시 데이터 폴더에서 TestClient로 로그인·라우팅을 거친다."""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
os.environ.setdefault("NIGHTSHIFT_ADMIN_USER", "admin")
os.environ.setdefault("NIGHTSHIFT_ADMIN_PASSWORD", "Test-Passw0rd-xyz!")
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")

import db

# v24 DB(이미지 2장짜리 포즈 한 건)를 v25로 올려도 기존 행은 NULL(첫 장 대표)로 읽혀야 한다.
_all = db.MIGRATIONS
db.MIGRATIONS = [m for m in _all if m[0] <= 24]
db._initialized = False
db.init()
db.MIGRATIONS = _all
_old = db._open()
_old.execute("INSERT INTO poses(id, name, image_file, owner_id, created_at) VALUES (7, '옛 포즈', '', 'u1', 'x')")
_old.execute("INSERT INTO pose_images(id, pose_id, image_file, thumb_file, position, created_at)"
             " VALUES (70, 7, 'a.png', 'a.webp', 0, 'x'), (71, 7, 'b.png', 'b.webp', 1, 'x')")
_old.close()
db._initialized = False
db.init()

import app as A
import pose_library
from fastapi.testclient import TestClient
from PIL import Image

HEADERS = {"X-Requested-With": "nightshift"}
PASSWORD = "Test-Passw0rd-xyz!"


def png(w):
    buf = io.BytesIO()
    Image.new("RGB", (w, 100), "red").save(buf, "PNG")
    return buf.getvalue()


class CoverTests(unittest.TestCase):
    def client(self, name):
        user = A.auth.register(name, f"{name}@example.test", PASSWORD)
        with db.connect() as conn:
            conn.execute("UPDATE users SET status='active' WHERE id=?", (user["id"],))
        c = TestClient(A.app)
        self.addCleanup(c.close)
        r = c.post("/api/auth/login", json={"username": name, "password": PASSWORD}, headers=HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        return c

    def add(self, c, kind, name, n=3):
        files = [("image", (f"{i}.png", png(100 + i), "image/png")) for i in range(n)]
        r = c.post(f"/api/library/{kind}", data={"name": name}, files=files, headers=HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def listed(self, c, kind, article_id):
        r = c.get(f"/api/library/{kind}", headers=HEADERS)
        return next(i for i in r.json()["items"] if i["id"] == article_id)

    def test_0_migration_v25(self):
        with db.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 25)
            for table in ("poses", "positions", "lightings"):
                self.assertIn("cover_image_id", [r[1] for r in conn.execute(f"PRAGMA table_info({table})")])
        old = pose_library.get_pose(7)
        self.assertEqual(old["cover_image_id"], 70)   # NULL → 첫 장
        self.assertEqual(old["thumb_url"], "/api/library/poses/7/images/70/thumb")

    def test_1_patch_cover_all_kinds(self):
        owner, other = self.client("cover-owner"), self.client("cover-other")
        for kind in ("poses", "positions", "lightings"):
            with self.subTest(kind=kind):
                a, b = self.add(owner, kind, "가"), self.add(owner, kind, "나", 1)
                ids = [i["id"] for i in a["images"]]
                self.assertEqual(a["cover_image_id"], ids[0])
                r = owner.patch(f"/api/library/{kind}/{a['id']}", json={"cover_image_id": ids[2]}, headers=HEADERS)
                self.assertEqual(r.status_code, 200, r.text)
                item = self.listed(owner, kind, a["id"])
                self.assertEqual(item["cover_image_id"], ids[2])
                self.assertEqual(item["thumb_url"], a["images"][2]["thumb_url"])
                self.assertEqual(item["image_url"], a["images"][2]["image_url"])
                # 남의 게시물 이미지·문자열·bool은 400, 남의 게시물은 404
                for bad in (b["images"][0]["id"], str(ids[1]), True, None):
                    r = owner.patch(f"/api/library/{kind}/{a['id']}", json={"cover_image_id": bad}, headers=HEADERS)
                    self.assertEqual(r.status_code, 400, (bad, r.text))
                r = other.patch(f"/api/library/{kind}/{a['id']}", json={"cover_image_id": ids[1]}, headers=HEADERS)
                self.assertEqual(r.status_code, 404)
                # 덧붙여도(생성 결과 업로드와 같은 경로) 대표는 그대로
                r = owner.post(f"/api/library/{kind}/{a['id']}/images",
                               files=[("image", ("x.png", png(150), "image/png"))], headers=HEADERS)
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.json()["cover_image_id"], ids[2])
                # 대표를 지우면 첫 장으로 돌아간다
                r = owner.delete(f"/api/library/{kind}/{a['id']}/images/{ids[2]}", headers=HEADERS)
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(self.listed(owner, kind, a["id"])["cover_image_id"], ids[0])
                with db.connect() as conn:
                    self.assertIsNone(conn.execute(f"SELECT cover_image_id FROM {kind} WHERE id=?",
                                                   (a["id"],)).fetchone()[0])

    def test_2_to_input_one_per_article(self):
        c = self.client("cover-seq")
        for kind, key in (("poses", "pose_ids"), ("positions", "position_ids")):
            with self.subTest(kind=kind):
                a, b = self.add(c, kind, "가"), self.add(c, kind, "나")
                cover = a["images"][1]["id"]
                self.assertEqual(c.patch(f"/api/library/{kind}/{a['id']}", json={"cover_image_id": cover},
                                         headers=HEADERS).status_code, 200)
                r = c.post(f"/api/library/{kind}/to-input", json={key: [a["id"], b["id"]]}, headers=HEADERS)
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual([i["image_id"] for i in r.json()], [cover, b["images"][0]["id"]])
        if kind == "positions":   # 대표 파일이 없으면 파일 있는 다음 장
            import position_library
            position_library.image_path(position_library.get_position(a["id"])["images"][1], False).unlink()
            r = c.post("/api/library/positions/to-input", json={"position_ids": [a["id"]]}, headers=HEADERS)
            self.assertEqual([i["image_id"] for i in r.json()], [a["images"][0]["id"]])

    def test_3_move_keeps_cover(self):
        c = self.client("cover-move")
        a = self.add(c, "poses", "옮길 것")
        self.assertEqual(c.patch(f"/api/library/poses/{a['id']}", json={"cover_image_id": a["images"][1]["id"]},
                                 headers=HEADERS).status_code, 200)
        r = c.post(f"/api/library/poses/{a['id']}/move-to-position", headers=HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        moved = r.json()
        self.assertEqual(moved["cover_image_id"], moved["images"][1]["id"])
        r = c.post(f"/api/library/positions/{moved['id']}/move-to-pose", headers=HEADERS)
        self.assertEqual(r.status_code, 200, r.text)
        back = r.json()
        self.assertEqual(back["cover_image_id"], back["images"][1]["id"])
        # 옛 /image 주소도 대표 장을 준다
        r = c.get(f"/api/library/poses/{back['id']}/image", headers=HEADERS)
        self.assertEqual(Image.open(io.BytesIO(r.content)).size, (101, 100))


if __name__ == "__main__":
    unittest.main()
