"""krea.2 개선 프롬프트의 실행 기록·동기화·마이그레이션·상세 API를 임시 데이터에서 검사한다."""
import io
import json
import os
import secrets
import sys
import tempfile
import unittest
from pathlib import Path

import httpx
from PIL import Image
from PIL.PngImagePlugin import PngInfo

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for kind in ("DATA", "OUTPUT", "ASSETS"):
    directory = Path(TMP.name) / kind.lower()
    directory.mkdir()
    os.environ[f"NIGHTSHIFT_{kind}_DIR"] = str(directory)
PASSWORD = "T!" + secrets.token_urlsafe(20)
os.environ["NIGHTSHIFT_ADMIN_USER"] = "admin"
os.environ["NIGHTSHIFT_ADMIN_PASSWORD"] = PASSWORD
sys.path.insert(0, str(ROOT / "server"))

import db  # noqa: E402


def graph(refine=True):
    nodes = {
        "1": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": "입력 원문"},
              "_meta": {"title": "main_prompt"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": ["3", 0] if refine else ["1", 0]}},
        "4": {"class_type": "KSampler", "inputs": {"seed": 7, "positive": ["2", 0]}},
    }
    if refine:
        nodes["3"] = {"class_type": "TextGenerate", "inputs": {"prompt": ["1", 0]}}
        nodes["5"] = {"class_type": "PreviewAny", "inputs": {"source": ["3", 0]},
                      "_meta": {"title": "보강된 프롬프트 미리보기"}}
    return nodes


def png():
    info = PngInfo()
    info.add_text("prompt", json.dumps(graph(), ensure_ascii=False))
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(stream, "PNG", pnginfo=info)
    return stream.getvalue()


class EnhancedPromptTests(unittest.TestCase):
    def test_migration_sync_and_detail_scope(self):
        migrations = db.MIGRATIONS
        db.MIGRATIONS = migrations[:-1]
        try:
            db.init()
            with db.connect() as conn:
                conn.execute("INSERT INTO assets(path, kind, created_at, prompt, note, favorite) "
                             "VALUES('old.png','image',?,?,?,1)", (db.now_iso(), "원문", "수동 메모"))
                conn.execute("INSERT INTO comfy_downloads(key, downloaded_at, pod_id) VALUES(?,?,?)",
                             ("old.png", db.now_iso(), "legacy"))
        finally:
            db.MIGRATIONS = migrations
            db._initialized = False
        db.init()
        with db.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 26)
            old = conn.execute("SELECT prompt, enhanced_prompt, note, favorite FROM assets WHERE path='old.png'").fetchone()
            self.assertEqual(tuple(old), ("원문", None, "수동 메모", 1))
            self.assertEqual(conn.execute("SELECT pod_id, enhanced_prompt FROM comfy_downloads WHERE key='old.png'")
                             .fetchone()[:], ("legacy", None))

        import app as A
        import assets_index as AI
        import auth
        import comfy_outputs as CO

        users = []
        for name in ("enhanced_a", "enhanced_b"):
            user = auth.register(name, f"{name}@example.test", PASSWORD)
            with db.connect() as conn:
                conn.execute("UPDATE users SET status='active' WHERE id=?", (user["id"],))
            users.append(user)
        admin = auth.ensure_admin()
        self.assertIsNotNone(admin)
        output = Path(os.environ["NIGHTSHIFT_OUTPUT_DIR"])
        payload = png()
        histories = {}

        def entry(filename, text=None, refine=True, extra=False):
            filenames = filename if isinstance(filename, list) else [filename]
            outputs = {"8": {"images": [{"filename": name, "subfolder": "", "type": "output"}
                                         for name in filenames]}}
            if text is not None:
                outputs["5"] = {"text": [text]}
            if extra:
                outputs["9"] = {"text": ["다른 노드 문장"]}
            return {"prompt": [filename, 0, graph(refine)], "outputs": outputs}

        histories["a"] = {"run-a": entry(["a.png", "a2.png"], "개선 A", extra=True),
                           "run-existing": entry("preexisting.png", "기존 파일 개선"),
                           "run-missing": entry("missing.png", extra=True),
                           "run-off": entry("off.png", "쓰면 안 되는 문장", refine=False),
                           "run-invalid": entry("invalid.png", ["잘못된 형식"])}
        histories["b"] = {"run-b": entry("b.png", "개선 B")}

        def fake_get(url, timeout):
            host = url.split("/")[2]
            if "/history?" in url:
                return json.dumps(histories[host], ensure_ascii=False).encode()
            self.assertIn("/view?", url)
            return payload

        old_get, old_mapper = CO._http_get, CO._key_mapper
        CO._http_get = fake_get
        owner_by_pod = {"a": users[0]["id"], "b": users[1]["id"]}
        CO.set_key_mapper(lambda item, pod: f"u{owner_by_pod[pod]}/{item['key']}")
        try:
            preexisting = output / f"u{users[0]['id']}/preexisting.png"
            preexisting.parent.mkdir(parents=True)
            preexisting.write_bytes(payload)
            for pod in ("a", "b"):
                result = CO.sync_outputs(f"http://{pod}", pod_id=pod)
                self.assertFalse(result["errors"])
            AI.sync(force=True)
            a_path = f"u{users[0]['id']}/a.png"
            b_path = f"u{users[1]['id']}/b.png"
            with db.connect() as conn:
                rows = {r["path"]: r for r in conn.execute(
                    "SELECT path, prompt, enhanced_prompt, owner_id FROM assets WHERE deleted_at IS NULL")}
            self.assertEqual(rows[a_path]["enhanced_prompt"], "개선 A")
            self.assertEqual(rows[f"u{users[0]['id']}/a2.png"]["enhanced_prompt"], "개선 A")
            self.assertEqual(rows[f"u{users[0]['id']}/preexisting.png"]["enhanced_prompt"], "기존 파일 개선")
            self.assertEqual(rows[b_path]["enhanced_prompt"], "개선 B")
            self.assertEqual(rows[a_path]["prompt"], "입력 원문")
            self.assertEqual(rows[a_path]["owner_id"], users[0]["id"])
            for name in ("missing.png", "off.png", "invalid.png"):
                self.assertIsNone(rows[f"u{users[0]['id']}/{name}"]["enhanced_prompt"])

            with db.connect() as conn:
                conn.execute("UPDATE assets SET enhanced_prompt=NULL, note='보존' WHERE path=?", (a_path,))
                conn.execute("UPDATE comfy_downloads SET enhanced_prompt=NULL WHERE key=?", (a_path,))
            result = CO.sync_outputs("http://a", pod_id="a")
            self.assertEqual(result["downloaded"], [])
            with db.connect() as conn:
                row = conn.execute("SELECT enhanced_prompt, note FROM assets WHERE path=?", (a_path,)).fetchone()
            self.assertEqual(tuple(row), ("개선 A", "보존"))
            histories["a"] = {}
            CO.sync_outputs("http://a", pod_id="a")
            with db.connect() as conn:
                self.assertEqual(conn.execute("SELECT enhanced_prompt FROM assets WHERE path=?", (a_path,))
                                 .fetchone()[0], "개선 A")
            async def detail():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=A.app),
                                             base_url="http://test") as client:
                    self.assertEqual((await client.get("/api/output-assets/detail", params={"path": b_path})).status_code, 401)
                    token_a = auth.create_session(users[0]["id"])
                    token_b = auth.create_session(users[1]["id"])
                    client.cookies.set(auth.SESSION_COOKIE, token_a)
                    own = await client.get("/api/output-assets/detail", params={"path": a_path})
                    self.assertEqual(own.status_code, 200, own.text)
                    self.assertEqual(own.json()["enhanced_prompt"], "개선 A")
                    self.assertEqual((await client.get("/api/output-assets/detail", params={"path": b_path})).status_code, 404)
                    client.cookies.set(auth.SESSION_COOKIE, token_b)
                    own = await client.get("/api/output-assets/detail", params={"path": b_path})
                    self.assertEqual(own.status_code, 200, own.text)
                    self.assertEqual(own.json()["enhanced_prompt"], "개선 B")

            import asyncio
            asyncio.run(detail())
            (output / a_path).unlink()
            CO.sync_outputs("http://a", pod_id="a")
            self.assertFalse((output / a_path).exists())
        finally:
            CO._http_get = old_get
            CO.set_key_mapper(old_mapper)


if __name__ == "__main__":
    unittest.main()
