"""무드보드 크기 설정 API의 인증·권한·입력 검증과 저장을 확인한다."""
import os
import json
import secrets
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for part in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{part}_DIR"] = str(Path(TMP.name) / part.lower())
PASSWORD = secrets.token_urlsafe(18)
os.environ["NIGHTSHIFT_ADMIN_USER"] = "admin"
os.environ["NIGHTSHIFT_ADMIN_PASSWORD"] = PASSWORD
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")

import app as A  # noqa: E402
import board_display as S  # noqa: E402
import db  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class BoardDisplaySettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        A.auth.ensure_admin()
        user = A.auth.register("member", "member@example.test", PASSWORD)
        with db.connect() as conn:
            conn.execute("UPDATE users SET status='active' WHERE id=?", (user["id"],))
        cls.admin = TestClient(A.app)
        cls.member = TestClient(A.app)
        cls.anonymous = TestClient(A.app)
        cls.headers = {"X-Requested-With": "nightshift"}
        for client, name in ((cls.admin, "admin"), (cls.member, "member")):
            response = client.post("/api/auth/login", json={"username": name, "password": PASSWORD},
                                   headers=cls.headers)
            assert response.status_code == 200, response.text

    @classmethod
    def tearDownClass(cls):
        cls.admin.close()
        cls.member.close()
        cls.anonymous.close()
        TMP.cleanup()

    def setUp(self):
        S.FILE.unlink(missing_ok=True)

    def test_auth_validation_and_persistence(self):
        path = "/api/settings/board-display"
        default = {"arrow_size": "medium", "port_size": "medium"}
        self.assertEqual(self.anonymous.get(path).status_code, 401)
        self.assertEqual(self.anonymous.put(path, json=default, headers=self.headers).status_code, 401)
        self.assertEqual(self.member.get(path).json(), default)
        self.assertEqual(self.member.put(path, json=default, headers=self.headers).status_code, 403)
        for invalid in ({}, {"arrow_size": "small"},
                        {"arrow_size": "huge", "port_size": "medium"},
                        {"arrow_size": 1, "port_size": "medium"},
                        {"arrow_size": "small", "port_size": "large", "extra": True}):
            response = self.admin.put(path, json=invalid, headers=self.headers)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(self.admin.get(path).json(), default)
        saved = {"arrow_size": "small", "port_size": "large"}
        response = self.admin.put(path, json=saved, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), saved)
        self.assertEqual(self.member.get(path).json(), saved)
        self.assertEqual(S.load(), saved)
        self.assertEqual(json.loads(S.FILE.read_text(encoding="utf-8")), saved)


if __name__ == "__main__":
    unittest.main()
