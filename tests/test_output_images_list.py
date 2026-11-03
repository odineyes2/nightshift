"""NS-116-1 갤러리 목록 해상도 — 색인 값을 쓰고 파일이 바뀌었을 때만 연다. 임시 데이터 폴더만 쓴다."""
import io
import os
import secrets
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
os.environ.setdefault("NIGHTSHIFT_ADMIN_USER", "admin")
os.environ.setdefault("NIGHTSHIFT_ADMIN_PASSWORD", "Test-Passw0rd-xyz!")
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")

import app as A
import db
from fastapi.testclient import TestClient
from PIL import Image


def png(size):
    buf = io.BytesIO()
    Image.new("RGB", size, "blue").save(buf, "PNG")
    return buf.getvalue()


# 실행마다 새로 만드는 임시 로그인 값(Aa1!로 비밀번호 규칙을 맞춘다).
LOGIN_KEY = "Aa1!" + secrets.token_urlsafe(16)


class OutputImagesListHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        user = A.auth.register("list-admin", "list-admin@example.test", LOGIN_KEY)
        with db.connect() as conn:
            conn.execute("UPDATE users SET status='active', role='admin' WHERE id=?", (user["id"],))

    def setUp(self):
        headers = {"X-Requested-With": "nightshift"}
        # lifespan 없이 실제 앱을 써서 스케줄러·외부 워커를 시작하지 않는다.
        self.client = TestClient(A.app)
        self.addCleanup(self.client.close)
        body = dict(zip(("username", "password"), ("list-admin", LOGIN_KEY)))
        response = self.client.post("/api/auth/login", json=body, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.base = Path(A.OUTPUT_DIR)
        self.sizes = {"job-a/one.png": (40, 30), "job-a/two.png": (16, 64), "job-b/three.png": (25, 25)}
        for name, size in self.sizes.items():
            path = self.base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(png(size))
        A.assets_index.sync(force=True)

    def listing(self):
        A.assets_index._last_sync = time.monotonic()   # 요청 안의 sync는 건너뛴다.
        response = self.client.get("/api/output-images")
        self.assertEqual(response.status_code, 200, response.text)
        return {item["name"]: (item["width"], item["height"]) for item in response.json()["images"]}

    def test_dims_from_index_without_opening_files(self):
        with db.connect() as conn:
            indexed = {r["path"]: (r["width"], r["height"]) for r in conn.execute(
                "SELECT path, width, height FROM assets WHERE deleted_at IS NULL")}
        self.assertEqual({name: indexed[name] for name in self.sizes}, self.sizes)
        with mock.patch.object(A.Image, "open", wraps=A.Image.open) as opened:
            items = self.listing()
        self.assertEqual({name: items[name] for name in self.sizes}, self.sizes)
        self.assertEqual(opened.call_count, 0)

    def test_changed_file_is_opened_for_new_dims(self):
        path = self.base / "job-a/one.png"
        old = path.stat()
        path.write_bytes(png((30, 40)))
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 5_000_000_000))
        with mock.patch.object(A.Image, "open", wraps=A.Image.open) as opened:
            items = self.listing()
        self.assertEqual(items["job-a/one.png"], (30, 40))
        self.assertEqual(items["job-a/two.png"], (16, 64))
        self.assertEqual(opened.call_count, 1)


if __name__ == "__main__":
    unittest.main()
