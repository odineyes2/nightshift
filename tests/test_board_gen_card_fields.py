"""생성 카드 초안의 필드·프롬프트 입구·높이를 실제 보드 API로 검사한다."""
import os
import secrets
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
os.environ.setdefault("NIGHTSHIFT_ADMIN_USER", "admin")
os.environ.setdefault("NIGHTSHIFT_ADMIN_PASSWORD", secrets.token_urlsafe(24))
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")

import app as A
import db


class BoardGenCardFieldsHTTPTests(unittest.TestCase):
    def test_exposed_fields_and_slots(self):
        password = secrets.token_urlsafe(18)
        user = A.auth.register("board-fields", "board-fields@example.test", password)
        with db.connect() as conn:
            conn.execute("UPDATE users SET status='active' WHERE id=?", (user["id"],))
        with TestClient(A.app) as client:
            headers = {"X-Requested-With": "nightshift"}
            login = client.post("/api/auth/login", json={"username": "board-fields", "password": password}, headers=headers)
            self.assertEqual(login.status_code, 200, login.text)
            project = client.post("/api/projects", json={"name": "카드 필드"}, headers=headers)
            self.assertEqual(project.status_code, 200, project.text)
            base = f"/api/projects/{project.json()['id']}/board/nodes"
            exposed = ["seed_count", "seed_mode", "main_prompt", "width", "height"]
            draft = {"template_id": "seed_batch", "workflow": {}, "slots": [], "exposed": exposed,
                     "options": {"seed_count": "3", "seed_mode": "sequential", "main_prompt": "테스트",
                                 "width": "1536", "height": "1024"}}
            response = client.post(base, json={"kind": "gen", "draft": draft}, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            card = response.json()
            self.assertEqual([f["name"] for f in card["data"]["fields"]], exposed)
            self.assertEqual([s["name"] for s in card["data"]["slots"]], ["main_prompt"])
            self.assertEqual(card["data"]["values"]["width"], "1536")
            self.assertEqual(card["height"], 52 + 4 * 60 + 96 + 120 + 14 + 46)
            saved = client.patch(base + f"/{card['id']}", json={"values": {"width": "2048", "height": "1152"}}, headers=headers)
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(saved.json()["data"]["values"]["height"], "1152")

            hidden = client.post(base, json={"kind": "gen", "draft": {**draft, "exposed": []}}, headers=headers)
            self.assertEqual(hidden.status_code, 200, hidden.text)
            self.assertEqual(hidden.json()["data"]["fields"], [])
            self.assertEqual(hidden.json()["data"]["slots"], [])


if __name__ == "__main__":
    unittest.main()
