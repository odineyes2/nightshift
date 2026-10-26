"""NS-115-1 서비스 토큰의 저장·관리 API·Bearer 접근 범위를 실제 앱 요청으로 검사한다."""

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")

import app as A
import db


class ServiceTokenHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        with db.connect() as conn:
            for name, role in (("token-admin", "admin"), ("token-owner", "user"),
                               ("token-other", "user")):
                conn.execute(
                    "INSERT INTO users(username, email, password_hash, role, status, created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (name, f"{name}@example.test", "unused", role, "active", db.now_iso()))
            cls.ids = {row["username"]: row["id"] for row in
                       conn.execute("SELECT id, username FROM users")}

    def setUp(self):
        self.client = TestClient(A.app)
        self.addCleanup(self.client.close)
        with db.connect() as conn:
            conn.execute("DELETE FROM service_tokens")
            conn.execute("UPDATE users SET status='active' WHERE username='token-owner'")
        admin_session = A.auth.create_session(self.ids["token-admin"])
        self.admin = TestClient(A.app, cookies={A.auth.SESSION_COOKIE: admin_session})
        self.addCleanup(self.admin.close)
        self.write = {"X-Requested-With": "nightshift"}

    def issue(self):
        response = self.admin.post("/api/admin/service-tokens", headers=self.write,
                                   json={"user_id": self.ids["token-owner"], "name": "Moai"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_issue_hash_only_list_last4_and_revoke(self):
        self.assertEqual(self.client.get("/api/admin/service-tokens").status_code, 401)
        issued = self.issue()
        token = issued["token"]
        self.assertTrue(token.startswith("nst_"))
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM service_tokens WHERE id=?", (issued["id"],)).fetchone()
        self.assertEqual(row["token_hash"], hashlib.sha256(token.encode()).hexdigest())
        self.assertNotIn(token, str(dict(row)))
        self.assertEqual(row["last4"], token[-4:])
        listed = self.admin.get("/api/admin/service-tokens").json()["tokens"]
        self.assertEqual(listed[0]["last4"], token[-4:])
        self.assertNotIn("token", listed[0])
        self.assertNotIn("token_hash", listed[0])
        header = {"Authorization": f"Bearer {token}"}
        self.assertEqual(self.client.get("/api/generate/capacity", headers=header).status_code, 404)
        revoked = self.admin.delete(f"/api/admin/service-tokens/{issued['id']}", headers=self.write)
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertIsNotNone(revoked.json()["revoked_at"])
        self.assertEqual(self.client.get("/api/generate/capacity", headers=header).status_code, 401)

    def test_authentication_and_scope(self):
        self.assertEqual(self.client.get("/api/output-images/example.png").status_code, 401)
        issued = self.issue()
        header = {"Authorization": f"Bearer {issued['token']}"}
        self.assertEqual(self.client.get("/api/admin/users", headers=header).status_code, 403)
        self.assertEqual(self.client.get("/api/auth/me", headers=header).status_code, 403)
        self.assertEqual(self.client.get("/api/projects", headers=header).status_code, 403)
        # 생성 API는 다음 Task에서 등록된다. 지금은 인증·허용 목록을 통과한 뒤 405다.
        self.assertEqual(self.client.post("/api/generate", headers=header, json={}).status_code, 405)
        self.assertEqual(self.admin.get("/api/admin/users", headers=header).status_code, 403)
        with db.connect() as conn:
            conn.execute("INSERT INTO assets(path, kind, created_at, owner_id) VALUES(?,?,?,?)",
                         ("another-owner.png", "image", db.now_iso(), self.ids["token-other"]))
        self.assertEqual(self.client.get("/api/output-images/another-owner.png", headers=header).status_code, 403)
        self.assertEqual(self.client.get("/api/generate/capacity", headers={"Authorization":
                             "Bearer nst_invalid"}).status_code, 401)
        with db.connect() as conn:
            conn.execute("UPDATE users SET status='disabled' WHERE id=?", (self.ids["token-owner"],))
        self.assertEqual(self.client.get("/api/generate/capacity", headers=header).status_code, 401)

    def test_admin_and_pending_user_cannot_receive_token(self):
        response = self.admin.post("/api/admin/service-tokens", headers=self.write,
                                   json={"user_id": self.ids["token-admin"], "name": "admin"})
        self.assertEqual(response.status_code, 403)
        with db.connect() as conn:
            conn.execute("UPDATE users SET status='pending' WHERE id=?", (self.ids["token-other"],))
        response = self.admin.post("/api/admin/service-tokens", headers=self.write,
                                   json={"user_id": self.ids["token-other"], "name": "pending"})
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
