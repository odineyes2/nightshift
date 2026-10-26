"""작업별 이미지 목록과 원본·축소본의 회원별 접근 권한을 실제 HTTP 요청으로 검사한다."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")

import app as A
import db


class JobOutputsHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        with db.connect() as conn:
            for name, role in (("outputs-admin", "admin"), ("outputs-owner", "user"),
                               ("outputs-other", "user")):
                conn.execute(
                    "INSERT INTO users(username, email, password_hash, role, status, created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (name, f"{name}@example.test", "unused", role, "active", db.now_iso()))
            cls.ids = {row["username"]: row["id"] for row in
                       conn.execute("SELECT id, username FROM users")}

    def setUp(self):
        self.client = TestClient(A.app)
        self.addCleanup(self.client.close)
        self.sessions = {}
        for name in ("outputs-admin", "outputs-owner", "outputs-other"):
            session = A.auth.create_session(self.ids[name])
            self.sessions[name] = TestClient(A.app, cookies={A.auth.SESSION_COOKIE: session})
            self.addCleanup(self.sessions[name].close)
        with A.lock:
            A.jobs.clear()
            A.jobs.update({
                "owned-job": {"id": "owned-job", "status": "done", "owner_id": self.ids["outputs-owner"]},
                "other-job": {"id": "other-job", "status": "queued", "owner_id": self.ids["outputs-other"]},
                "empty-job": {"id": "empty-job", "status": "queued", "owner_id": self.ids["outputs-owner"]},
            })
        with db.connect() as conn:
            conn.execute("DELETE FROM assets")
            conn.execute("DELETE FROM jobs")
            conn.execute("DELETE FROM service_tokens")
            for job_id, name, status in (("owned-job", "outputs-owner", "done"),
                                         ("other-job", "outputs-other", "queued"),
                                         ("empty-job", "outputs-owner", "queued")):
                conn.execute("INSERT INTO jobs(id, status, owner_id, data_json) VALUES(?,?,?,?)",
                             (job_id, status, self.ids[name], "{}"))
        self.image_name = "owned-job/결과 사진.png"
        for job_id, filename in (("owned-job", "결과 사진.png"), ("other-job", "other.png")):
            path = Path(A.OUTPUT_DIR) / job_id / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (8, 6), (50, 80, 100)).save(path)
        A.assets_index.sync(force=True)

    def issue_token(self):
        response = self.sessions["outputs-admin"].post(
            "/api/admin/service-tokens", headers={"X-Requested-With": "nightshift"},
            json={"user_id": self.ids["outputs-owner"], "name": "outputs"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_job_outputs_scope_status_and_download(self):
        url = "/api/jobs/owned-job/outputs"
        self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.sessions["outputs-other"].get(url).status_code, 403)
        self.assertEqual(self.sessions["outputs-owner"].get("/api/jobs/missing/outputs").status_code, 404)
        response = self.sessions["outputs-owner"].get(url)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["status"], "done")
        self.assertEqual(len(data["images"]), 1)
        image = data["images"][0]
        self.assertEqual(image["name"], self.image_name)
        self.assertGreater(image["size"], 0)
        self.assertIn("%", image["url"])
        self.assertEqual(self.sessions["outputs-owner"].get(image["url"]).status_code, 200)
        self.assertEqual(self.sessions["outputs-other"].get(image["url"]).status_code, 403)
        self.assertEqual(self.sessions["outputs-other"].get(image["url"] + "/thumbnail").status_code, 403)
        self.assertEqual(self.sessions["outputs-admin"].get(url).status_code, 200)
        self.assertEqual(self.sessions["outputs-admin"].get(image["url"]).status_code, 200)
        self.assertEqual(self.sessions["outputs-owner"].get("/api/jobs/empty-job/outputs").json(),
                         {"status": "queued", "images": []})
        other = self.sessions["outputs-owner"].get("/api/jobs/other-job/outputs")
        self.assertEqual(other.status_code, 403)

    def test_token_download_and_revocation(self):
        issued = self.issue_token()
        header = {"Authorization": f"Bearer {issued['token']}"}
        response = self.client.get("/api/jobs/owned-job/outputs", headers=header)
        self.assertEqual(response.status_code, 200, response.text)
        image_url = response.json()["images"][0]["url"]
        self.assertEqual(self.client.get(image_url, headers=header).status_code, 200)
        self.assertEqual(self.client.get(image_url + "/thumbnail", headers=header).status_code, 200)
        self.assertEqual(self.client.get("/api/jobs/other-job/outputs", headers=header).status_code, 403)
        self.assertEqual(self.client.get("/api/output-images/other-job/other.png", headers=header).status_code, 403)
        revoked = self.sessions["outputs-admin"].delete(
            f"/api/admin/service-tokens/{issued['id']}", headers={"X-Requested-With": "nightshift"})
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual(self.client.get(image_url, headers=header).status_code, 401)


if __name__ == "__main__":
    unittest.main()
