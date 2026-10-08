"""NS-43-1 Library > Position 서버 — DB v21·추가·이미지 추가·수정·게시물/이미지 삭제·회원별 범위. 임시 데이터 폴더만 쓴다."""
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

# v20 DB(포즈 한 건)에서 v21이 기존 데이터를 건드리지 않고 도는지 본다.
_all = db.MIGRATIONS
db.MIGRATIONS = [m for m in _all if m[0] <= 20]
db._initialized = False
db.init()
db.MIGRATIONS = _all
_old = db._open()
_old.execute("INSERT INTO poses(id, name, image_file, owner_id, created_at) VALUES (7, '옛 포즈', '', 'u1', 'x')")
_old.close()
db._initialized = False
db.init()

import app as A
import position_library as L
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
    return set(data_dir("library/positions").iterdir())


def add(uid="u1", n=1, name="정면"):
    form = [("name", name), ("danbooru_prompt", "from_front")] + [("image", upload(png((100 + i, 100)))) for i in range(n)]
    code, item = call(A.add_library_position, req(uid, form=form))
    assert code == 200, item
    return item


class PositionTests(unittest.TestCase):
    def test_1_migration(self):
        with db.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.MIGRATIONS[-1][0])
            cols = {r[1] for r in conn.execute("PRAGMA table_info(positions)")}
            self.assertEqual(conn.execute("SELECT name FROM poses WHERE id=7").fetchone()[0], "옛 포즈")
        self.assertTrue({"name", "description", "danbooru_prompt", "owner_id"} <= cols)

    def test_2_add_list_images(self):
        a = add(n=2)
        self.assertEqual((a["name"], a["danbooru_prompt"], a["owner_id"], a["image_count"]), ("정면", "from_front", "u1", 2))
        self.assertEqual(a["thumb_url"], a["images"][0]["thumb_url"])
        self.assertTrue(a["thumb_url"].startswith(f"/api/library/positions/{a['id']}/images/"))
        self.assertTrue(all(L.image_path(i, t).is_file() for i in a["images"] for t in (True, False)))
        self.assertEqual(call(A.get_library_position_thumb, req(), a["id"], a["images"][1]["id"])[0], 200)
        code, b = call(A.add_library_position_images, req(form=[("image", upload(png()))]), a["id"])
        self.assertEqual((code, b["image_count"], b["images"][-1]["position"]), (200, 3, 2))
        self.assertIn(a["id"], [i["id"] for i in call(A.list_library_positions, req())[1]["items"]])
        self.assertEqual(call(A.add_library_position, req(form={"name": " ", "image": upload(png())}))[0], 400)
        self.assertEqual(call(A.add_library_position, req(form={"name": "x"}))[0], 400)

    def test_3_update(self):
        a = add()
        code, b = call(A.update_library_position, req(body={"name": " 뒤 ", "danbooru_prompt": "from_behind"}), a["id"])
        self.assertEqual((code, b["name"], b["danbooru_prompt"], b["description"]), (200, "뒤", "from_behind", ""))
        self.assertEqual(call(A.update_library_position, req(body={"name": ""}), a["id"])[0], 400)
        self.assertEqual(call(A.update_library_position, req(body=b"{nope"), a["id"])[0], 400)
        self.assertEqual(call(A.update_library_position, req("u2", body={"name": "x"}), a["id"])[0], 404)
        code, c = call(A.update_library_position, req("adm", "admin", body={"description": "d"}), a["id"])
        self.assertEqual((code, c["description"], c["name"]), (200, "d", "뒤"))

    def test_4_delete_image(self):
        a = add(n=2)
        first, second = a["images"]
        self.assertEqual(call(A.delete_library_position_image, req("u2"), a["id"], first["id"])[0], 404)
        self.assertEqual(call(A.delete_library_position_image, req(), a["id"], 99999)[0], 404)
        code, b = call(A.delete_library_position_image, req(), a["id"], first["id"])
        self.assertEqual((code, [i["id"] for i in b["images"]]), (200, [second["id"]]))
        self.assertFalse(L.image_path(first, False).exists() or L.image_path(first, True).exists())
        # 마지막 한 장은 거절
        self.assertEqual(call(A.delete_library_position_image, req(), a["id"], second["id"])[0], 400)
        self.assertTrue(L.image_path(second, False).is_file())

    def test_5_delete_position(self):
        a = add(n=2)
        before = files()
        self.assertEqual(call(A.delete_library_position, req("u2"), a["id"])[0], 404)
        self.assertEqual(call(A.delete_library_position, req(), a["id"]), (200, {"ok": True}))
        self.assertIsNone(L.get_position(a["id"]))
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM position_images WHERE position_id=?",
                                          (a["id"],)).fetchone()[0], 0)
        gone = {L.image_path(i, t) for i in a["images"] for t in (True, False)}
        self.assertEqual(files(), before - gone)
        self.assertEqual(call(A.delete_library_position, req(), a["id"])[0], 404)

    def test_6_owner_scope(self):
        mine = add("u2", name="u2 것")
        self.assertEqual([i["name"] for i in call(A.list_library_positions, req("u2"))[1]["items"]], ["u2 것"])
        self.assertNotIn("u2 것", [i["name"] for i in call(A.list_library_positions, req("u1"))[1]["items"]])
        self.assertIn("u2 것", [i["name"] for i in call(A.list_library_positions, req("adm", "admin"))[1]["items"]])
        img = mine["images"][0]["id"]
        self.assertEqual(call(A.get_library_position_image, req("u1"), mine["id"], img)[0], 404)
        self.assertEqual(call(A.get_library_position_image, req("adm", "admin"), mine["id"], img)[0], 200)
        self.assertEqual(call(A.add_library_position_images, req("u1", form=[("image", upload(png()))]), mine["id"])[0], 404)
        self.assertEqual(call(A.delete_library_position, req("adm", "admin"), mine["id"])[0], 200)

    def test_7_to_input(self):
        """NS-51 "Openpose CN" — 고른 장·게시물 전체를 요청한 회원의 입력 이미지 풀로 복사한다."""
        import auth
        import input_assets

        def as_user(uid="u1", role="user", body=None):
            auth.current_user.set({"id": uid, "role": role})   # 미들웨어가 하는 일 — 회원별 풀을 고른다
            return req(uid, role, body=body)
        a = L.get_position(add(n=1)["id"])
        gif = io.BytesIO()
        Image.new("RGB", (100, 50)).save(gif, "GIF")
        b = L.add_position("u1", "둘", "", "two", [(png((600, 900)), ""), (gif.getvalue(), "")])
        img_a, (b1, b2) = a["images"][0], b["images"]
        # 단일
        code, out = call(A.library_position_to_input, as_user(), a["id"], img_a["id"])
        self.assertEqual((code, out), (200, {"name": f"library_position_{a['id']}_{img_a['id']}.png",
                                             "danbooru_prompt": "from_front"}))
        self.assertIn(str(Path("users") / "u1"), str(input_assets.resolve_input_image(out["name"])))
        code, gif_out = call(A.library_position_to_input, as_user(), b["id"], b2["id"])
        self.assertEqual(gif_out["name"], f"library_position_{b['id']}_{b2['id']}.png")   # GIF → PNG
        before = len(input_assets.list_input_images())
        self.assertEqual(call(A.library_position_to_input, as_user(), b["id"], b2["id"]), (200, gif_out))   # 재사용
        self.assertEqual(len(input_assets.list_input_images()), before)
        self.assertEqual(call(A.library_position_to_input, as_user("u2"), a["id"], img_a["id"])[0], 404)
        self.assertEqual(call(A.library_position_to_input, as_user(), a["id"], 99999)[0], 404)
        self.assertEqual(call(A.library_position_to_input, as_user("adm", "admin"), a["id"], img_a["id"])[0], 200)
        # 일괄 — 목록 순서, 게시물 안은 이미지 순서
        code, rows = call(A.library_positions_to_input, as_user(body={"position_ids": [b["id"], a["id"]]}))
        self.assertEqual(code, 200, rows)
        self.assertEqual([(r["position_id"], r["image_id"]) for r in rows],
                         [(b["id"], b1["id"]), (b["id"], b2["id"]), (a["id"], img_a["id"])])
        self.assertEqual((rows[0]["position_name"], rows[0]["danbooru_prompt"], rows[0]["width"], rows[0]["height"],
                          rows[0]["sdxl_width"], rows[0]["sdxl_height"]), ("둘", "two", 600, 900, 832, 1280))
        self.assertEqual(rows[1]["name"], gif_out["name"])
        for body in (b"{nope", {"position_ids": []}, {"position_ids": ["1"]}, {"position_ids": [True]}, [1]):
            self.assertEqual(call(A.library_positions_to_input, as_user(body=body))[0], 400, body)
        # 하나라도 못 보면 404이고 아무것도 복사하지 않는다
        c = add("u2", name="남의 것")
        before = len(input_assets.list_input_images())
        self.assertEqual(call(A.library_positions_to_input, as_user(body={"position_ids": [c["id"], 99999]}))[0], 404)
        self.assertEqual(call(A.library_positions_to_input, as_user(body={"position_ids": [a["id"], c["id"]]}))[0], 404)
        self.assertEqual(len(input_assets.list_input_images()), before)
        # 파일이 없는 장 — 단일은 404, 일괄은 건너뛴다
        L.image_path(b1, False).unlink()
        self.assertEqual(call(A.library_position_to_input, as_user(), b["id"], b1["id"])[0], 404)
        code, rows = call(A.library_positions_to_input, as_user(body={"position_ids": [b["id"]]}))
        self.assertEqual((code, [r["image_id"] for r in rows]), (200, [b2["id"]]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
