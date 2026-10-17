"""사용자 정의 Library의 v26→v27 이행과 실제 HTTP 인증·CRUD·이미지 수명을 검사한다."""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
os.environ["NIGHTSHIFT_ADMIN_USER"] = "admin"
os.environ["NIGHTSHIFT_ADMIN_PASSWORD"] = "Test-Passw0rd-xyz!"
sys.path.insert(0, str(ROOT / "server"))

import db  # noqa: E402

MIGRATIONS = db.MIGRATIONS
db.MIGRATIONS = [step for step in MIGRATIONS if step[0] <= 26]
db.init()
with db.connect() as conn:
    conn.execute("INSERT INTO assets(path,kind,created_at,prompt,enhanced_prompt) VALUES('old.png','image',?,?,?)",
                 (db.now_iso(), "원문", "개선"))
db.MIGRATIONS = MIGRATIONS
db._initialized = False
db.init()

import app as A  # noqa: E402
import library_categories as L  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.routing import Mount  # noqa: E402

HEADERS = {"X-Requested-With": "nightshift"}


def png(color="red"):
    stream = io.BytesIO()
    Image.new("RGB", (16, 12), color).save(stream, "PNG")
    return stream.getvalue()


class CategoryHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.anonymous = TestClient(A.app)
        cls.clients = []

    @classmethod
    def tearDownClass(cls):
        cls.anonymous.close()
        for client in cls.clients:
            client.close()

    def client(self, name):
        user = A.auth.register(name, f"{name}@example.test", "Test-Passw0rd-xyz!")
        with db.connect() as conn:
            conn.execute("UPDATE users SET status='active' WHERE id=?", (user["id"],))
        client = TestClient(A.app)
        self.clients.append(client)
        response = client.post("/api/auth/login", json={"username": name, "password": "Test-Passw0rd-xyz!"}, headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def test_migration_and_route_order(self):
        with db.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 27)
            self.assertEqual(tuple(conn.execute("SELECT prompt,enhanced_prompt FROM assets WHERE path='old.png'").fetchone()),
                             ("원문", "개선"))
            self.assertFalse(conn.execute("PRAGMA foreign_key_check").fetchall())
        root = next(i for i, route in enumerate(A.app.routes) if isinstance(route, Mount) and route.path == "")
        routes = [i for i, route in enumerate(A.app.routes) if route.path == "/api/library/categories"]
        self.assertEqual(len(routes), 2)
        self.assertTrue(all(i < root for i in routes))

    def test_failed_image_save_cleans_files_and_rows(self):
        category = L.create_category("rollback-owner", "실패 복구")
        original = L._save_images

        def fail_after_save(conn, article_id, checked, created):
            original(conn, article_id, checked, created)
            raise OSError("검사 중 저장 실패")

        with patch.object(L, "_save_images", fail_after_save):
            with self.assertRaises(OSError):
                L.create_article(category["id"], "rollback-owner", "아티클", "", "", [(png(), "")])
        self.assertEqual(L.list_articles(category["id"]), [])
        self.assertEqual(list((Path(TMP.name) / "data" / "library" / "articles").iterdir()), [])
        L.delete_category(category)

    def test_authenticated_lifecycle(self):
        base = "/api/library/categories"
        self.assertEqual(self.anonymous.get(base).status_code, 401)
        owner, other, admin = self.client("category_owner"), self.client("category_other"), self.client("category_admin")
        with db.connect() as conn:
            user_id = conn.execute("SELECT id FROM users WHERE username='category_admin'").fetchone()[0]
            conn.execute("UPDATE users SET role='admin' WHERE id=?", (user_id,))
        # 로그인 세션의 사용자 정보가 DB에서 다시 읽히는지 확인한다.
        self.assertEqual(owner.post(base, json={"name": "Shot"}).status_code, 403)
        response = owner.post(base, json={"name": " Shot "}, headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        category = response.json()
        cid = category["id"]
        path = f"{base}/{cid}"
        self.assertEqual(category["name"], "Shot")
        self.assertEqual(owner.post(base, json={"name": "shot"}, headers=HEADERS).status_code, 400)
        self.assertEqual(owner.post(base, json={"name": "  "}, headers=HEADERS).status_code, 400)
        self.assertEqual(other.get(base).json(), {"items": []})
        self.assertEqual(other.patch(path, json={"name": "남의 것"}, headers=HEADERS).status_code, 404)
        self.assertEqual(other.delete(path, headers=HEADERS).status_code, 404)
        self.assertEqual(admin.get(base).status_code, 200)
        self.assertIn(cid, [c["id"] for c in admin.get(base).json()["items"]])
        self.assertEqual(owner.patch(path, json={"name": "Framing"}, headers=HEADERS).json()["id"], cid)

        articles = f"{path}/articles"
        self.assertEqual(owner.post(articles, data={"name": "빈 항목"}, headers=HEADERS).status_code, 400)
        upload = {"image": ("one.png", png(), "image/png")}
        response = owner.post(articles, data={"name": "첫 아티클", "danbooru_prompt": "wide shot"},
                              files=upload, headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        article = response.json()
        aid = article["id"]
        image_id = article["images"][0]["id"]
        article_path = f"{articles}/{aid}"
        image_path = f"{article_path}/images/{image_id}"
        self.assertEqual(article["cover_image_id"], image_id)
        self.assertEqual(other.get(f"{image_path}/image").status_code, 404)
        self.assertEqual(owner.get(f"{image_path}/image").status_code, 200)
        self.assertEqual(owner.get(f"{image_path}/thumb").status_code, 200)
        self.assertEqual(admin.get(f"{image_path}/image").status_code, 200)
        self.assertEqual(other.patch(article_path, json={"name": "침입"}, headers=HEADERS).status_code, 404)
        self.assertEqual(owner.patch(article_path, json={"cover_image_id": 999999}, headers=HEADERS).status_code, 400)
        self.assertEqual(owner.post(f"{article_path}/images", files={"image": ("bad.png", b"bad", "image/png")},
                                    headers=HEADERS).status_code, 400)
        second = owner.post(f"{article_path}/images", files={"image": ("two.png", png("blue"), "image/png")},
                            headers=HEADERS)
        self.assertEqual(second.status_code, 200, second.text)
        second_id = second.json()["images"][1]["id"]
        updated = owner.patch(article_path, json={"name": "수정", "description": "설명",
                                                  "danbooru_prompt": "close shot", "cover_image_id": second_id}, headers=HEADERS)
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual((updated.json()["name"], updated.json()["description"], updated.json()["cover_image_id"]),
                         ("수정", "설명", second_id))
        self.assertEqual(owner.delete(f"{article_path}/images/{second_id}", headers=HEADERS).json()["cover_image_id"], image_id)
        self.assertEqual(owner.delete(image_path, headers=HEADERS).status_code, 400)
        files = list((Path(TMP.name) / "data" / "library" / "articles").iterdir())
        self.assertEqual(len(files), 2)
        output = Path(TMP.name) / "output" / "gallery.png"
        output.parent.mkdir(exist_ok=True)
        output.write_bytes(png("green"))
        with db.connect() as conn:
            owner_id = conn.execute("SELECT id FROM users WHERE username='category_owner'").fetchone()[0]
            conn.execute("INSERT INTO assets(path,kind,created_at,owner_id) VALUES('gallery.png','image',?,?)",
                         (db.now_iso(), owner_id))
        self.assertEqual(other.post(articles, data={"name": "침입", "output_name": "gallery.png"},
                                    headers=HEADERS).status_code, 404)
        self.assertEqual(owner.post(articles, data={"name": "잘못된 경로", "output_name": "../gallery.png"},
                                    headers=HEADERS).status_code, 400)
        gallery = owner.post(articles, data={"name": "갤러리 아티클", "output_name": "gallery.png"}, headers=HEADERS)
        self.assertEqual(gallery.status_code, 200, gallery.text)
        self.assertEqual(gallery.json()["image_count"], 1)
        self.assertEqual(other.get(articles).status_code, 404)
        deleted = owner.delete(path, headers=HEADERS)
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json(), {"ok": True, "article_count": 2, "image_count": 2})
        self.assertEqual(list((Path(TMP.name) / "data" / "library" / "articles").iterdir()), [])
        self.assertTrue(output.is_file())
        self.assertEqual(owner.get(articles).status_code, 404)


if __name__ == "__main__":
    unittest.main()
