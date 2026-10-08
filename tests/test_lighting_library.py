"""NS-60-1 Library > Lighting 서버 — DB v24·추가·이미지 추가·수정·게시물/이미지 삭제·회원별 범위. 임시 데이터 폴더만 쓴다."""
import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
os.environ.setdefault("NIGHTSHIFT_ADMIN_USER", "admin")
os.environ.setdefault("NIGHTSHIFT_ADMIN_PASSWORD", "Test-Passw0rd-xyz!")
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")

import db

# v23 DB(포즈 한 건)에서 v24가 기존 데이터를 건드리지 않고 도는지 본다.
_all = db.MIGRATIONS
db.MIGRATIONS = [m for m in _all if m[0] <= 23]
db._initialized = False
db.init()
db.MIGRATIONS = _all
_old = db._open()
_old.execute("INSERT INTO poses(id, name, image_file, owner_id, created_at) VALUES (7, '옛 포즈', '', 'u1', 'x')")
_old.close()
db._initialized = False
db.init()

import app as A
import lighting_library as L
from fastapi import HTTPException
from PIL import Image
from data_paths import data_dir
from starlette.datastructures import FormData, UploadFile


def png(size=(800, 600)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (50, 100, 200)).save(buf, "PNG")
    return buf.getvalue()


def req(uid="u1", role="user", form=None, body=None):
    async def read_form():
        return FormData(list(form.items()) if isinstance(form, dict) else (form or []))

    async def read_body():
        return body if isinstance(body, bytes) else json.dumps(body).encode()
    return SimpleNamespace(state=SimpleNamespace(user={"id": uid, "role": role}), form=read_form, body=read_body)


def upload(data: bytes, name="p.png"):
    return UploadFile(file=io.BytesIO(data), filename=name)


def call(fn, *args):
    try:
        out = fn(*args)
        return 200, asyncio.run(out) if asyncio.iscoroutine(out) else out
    except HTTPException as e:
        return e.status_code, e.detail


def files():
    return set(data_dir("library/lightings").iterdir())


def add(uid="u1", n=1, name="정면"):
    form = [("name", name), ("danbooru_prompt", "from_front")] + [("image", upload(png((100 + i, 100)))) for i in range(n)]
    code, item = call(A.add_library_lighting, req(uid, form=form))
    assert code == 200, item
    return item


class LightingTests(unittest.TestCase):
    def test_1_migration(self):
        with db.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.MIGRATIONS[-1][0])
            cols = {r[1] for r in conn.execute("PRAGMA table_info(lightings)")}
            self.assertEqual(conn.execute("SELECT name FROM poses WHERE id=7").fetchone()[0], "옛 포즈")
        self.assertTrue({"name", "description", "danbooru_prompt", "owner_id"} <= cols)

    def test_2_add_list_images(self):
        a = add(n=2)
        self.assertEqual((a["name"], a["danbooru_prompt"], a["owner_id"], a["image_count"]), ("정면", "from_front", "u1", 2))
        self.assertEqual(a["thumb_url"], a["images"][0]["thumb_url"])
        self.assertTrue(a["thumb_url"].startswith(f"/api/library/lightings/{a['id']}/images/"))
        self.assertTrue(all(L.image_path(i, t).is_file() for i in a["images"] for t in (True, False)))
        self.assertEqual(call(A.get_library_lighting_thumb, req(), a["id"], a["images"][1]["id"])[0], 200)
        code, b = call(A.add_library_lighting_images, req(form=[("image", upload(png()))]), a["id"])
        self.assertEqual((code, b["image_count"], b["images"][-1]["position"]), (200, 3, 2))
        self.assertIn(a["id"], [i["id"] for i in call(A.list_library_lightings, req())[1]["items"]])
        self.assertEqual(call(A.add_library_lighting, req(form={"name": " ", "image": upload(png())}))[0], 400)
        self.assertEqual(call(A.add_library_lighting, req(form={"name": "x"}))[0], 400)

    def test_3_update(self):
        a = add()
        code, b = call(A.update_library_lighting, req(body={"name": " 뒤 ", "danbooru_prompt": "from_behind"}), a["id"])
        self.assertEqual((code, b["name"], b["danbooru_prompt"], b["description"]), (200, "뒤", "from_behind", ""))
        self.assertEqual(call(A.update_library_lighting, req(body={"name": ""}), a["id"])[0], 400)
        self.assertEqual(call(A.update_library_lighting, req(body=b"{nope"), a["id"])[0], 400)
        self.assertEqual(call(A.update_library_lighting, req("u2", body={"name": "x"}), a["id"])[0], 404)
        code, c = call(A.update_library_lighting, req("adm", "admin", body={"description": "d"}), a["id"])
        self.assertEqual((code, c["description"], c["name"]), (200, "d", "뒤"))

    def test_4_delete_image(self):
        a = add(n=2)
        first, second = a["images"]
        self.assertEqual(call(A.delete_library_lighting_image, req("u2"), a["id"], first["id"])[0], 404)
        self.assertEqual(call(A.delete_library_lighting_image, req(), a["id"], 99999)[0], 404)
        code, b = call(A.delete_library_lighting_image, req(), a["id"], first["id"])
        self.assertEqual((code, [i["id"] for i in b["images"]]), (200, [second["id"]]))
        self.assertFalse(L.image_path(first, False).exists() or L.image_path(first, True).exists())
        # 마지막 한 장은 거절
        self.assertEqual(call(A.delete_library_lighting_image, req(), a["id"], second["id"])[0], 400)
        self.assertTrue(L.image_path(second, False).is_file())

    def test_5_delete_position(self):
        a = add(n=2)
        before = files()
        self.assertEqual(call(A.delete_library_lighting, req("u2"), a["id"])[0], 404)
        self.assertEqual(call(A.delete_library_lighting, req(), a["id"]), (200, {"ok": True}))
        self.assertIsNone(L.get_lighting(a["id"]))
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM lighting_images WHERE lighting_id=?",
                                          (a["id"],)).fetchone()[0], 0)
        gone = {L.image_path(i, t) for i in a["images"] for t in (True, False)}
        self.assertEqual(files(), before - gone)
        self.assertEqual(call(A.delete_library_lighting, req(), a["id"])[0], 404)

    def test_7_no_workflow_routes(self):
        routes = [r.path for r in A.app.routes if r.path.startswith("/api/library/lightings")]
        self.assertEqual(len(routes), 8)
        self.assertFalse(any("to-input" in r or "move-to-" in r for r in routes))
        self.assertFalse(hasattr(L, "input_copy"))

    def test_6_owner_scope(self):
        mine = add("u2", name="u2 것")
        self.assertEqual([i["name"] for i in call(A.list_library_lightings, req("u2"))[1]["items"]], ["u2 것"])
        self.assertNotIn("u2 것", [i["name"] for i in call(A.list_library_lightings, req("u1"))[1]["items"]])
        self.assertIn("u2 것", [i["name"] for i in call(A.list_library_lightings, req("adm", "admin"))[1]["items"]])
        img = mine["images"][0]["id"]
        self.assertEqual(call(A.get_library_lighting_image, req("u1"), mine["id"], img)[0], 404)
        self.assertEqual(call(A.get_library_lighting_image, req("adm", "admin"), mine["id"], img)[0], 200)
        self.assertEqual(call(A.add_library_lighting_images, req("u1", form=[("image", upload(png()))]), mine["id"])[0], 404)
        self.assertEqual(call(A.delete_library_lighting, req("adm", "admin"), mine["id"])[0], 200)



if __name__ == "__main__":
    unittest.main(verbosity=2)
