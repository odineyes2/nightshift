"""생성 카드 초안의 필드·프롬프트 입구·높이를 실제 보드 API로 검사한다."""
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
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
            refreshed = client.get(f"/api/projects/{project.json()['id']}/board", headers=headers)
            self.assertEqual(refreshed.status_code, 200, refreshed.text)
            self.assertEqual(next(n for n in refreshed.json()["nodes"] if n["id"] == card["id"])["data"]["values"]["width"], "2048")

            hidden = client.post(base, json={"kind": "gen", "draft": {**draft, "exposed": []}}, headers=headers)
            self.assertEqual(hidden.status_code, 200, hidden.text)
            self.assertEqual(hidden.json()["data"]["fields"], [])
            self.assertEqual(hidden.json()["data"]["slots"], [])

    def test_resolution_select_saves_only_dimensions(self):
        from playwright.sync_api import expect, sync_playwright

        with tempfile.TemporaryDirectory() as temp, socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            env = os.environ.copy()
            for name in ("DATA", "OUTPUT", "ASSETS"):
                env[f"NIGHTSHIFT_{name}_DIR"] = str(Path(temp) / name.lower())
            env["NIGHTSHIFT_ADMIN_USER"] = "admin"
            env["NIGHTSHIFT_ADMIN_PASSWORD"] = secrets.token_urlsafe(24)
            server = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
                cwd=ROOT / "server", env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        page = browser.new_page()
                        base = f"http://127.0.0.1:{port}"
                        for _ in range(100):
                            try:
                                if page.request.get(base + "/api/health").ok:
                                    break
                            except Exception:
                                time.sleep(.1)
                        else:
                            self.fail("격리 서버가 시작되지 않았어요")
                        headers = {"X-Requested-With": "nightshift"}
                        login = page.request.post(base + "/api/auth/login", data={
                            "username": "admin", "password": env["NIGHTSHIFT_ADMIN_PASSWORD"]}, headers=headers)
                        self.assertTrue(login.ok, login.text())
                        project = page.request.post(base + "/api/projects", data={"name": "Resolution UI"}, headers=headers)
                        self.assertTrue(project.ok, project.text())
                        project_id = project.json()["id"]
                        page.goto(f"{base}/#project/{project_id}/prboard")
                        page.wait_for_function("() => currentTab === 'prboard' && currentBoardId !== null")
                        draft = {"template_id": "seed_batch", "workflow": {}, "slots": [],
                                 "exposed": ["seed_count", "seed_mode", "main_prompt", "width", "height"],
                                 "options": {"seed_count": "3", "seed_mode": "sequential", "main_prompt": "test",
                                             "width": "1536", "height": "1024"}}
                        node = page.evaluate("(draft) => createBoardNode('gen', null, '', null, true, draft)", draft)
                        self.assertIsNotNone(node)
                        card = page.locator(f'.board-node-gen[data-node-id="{node["id"]}"]')
                        requests = []
                        page.on("response", lambda response: requests.append((response.status, response.request.post_data_json))
                                if response.request.method == "PATCH" and f"/board/nodes/{node['id']}" in response.url else None)
                        card.locator('[data-resolution="ratio"]').select_option("1:1")
                        page.wait_for_function("() => boardRequests.size === 0 && boardGenPending.size === 0")
                        self.assertTrue(requests)
                        self.assertTrue(all(status == 200 for status, _ in requests), requests)
                        self.assertTrue(all("undefined" not in body.get("values", {}) for _, body in requests), requests)
                        self.assertEqual(card.locator('[data-gen-field="width"]').input_value(), "1024")
                        self.assertEqual(card.locator('[data-gen-field="height"]').input_value(), "1024")
                        card.locator('[data-resolution="tier"]').select_option("2K")
                        page.wait_for_function("() => boardRequests.size === 0 && boardGenPending.size === 0")
                        self.assertTrue(all(status == 200 for status, _ in requests), requests)
                        self.assertTrue(all("undefined" not in body.get("values", {}) for _, body in requests), requests)
                        self.assertEqual(card.locator('[data-gen-field="width"]').input_value(), "2048")
                        self.assertEqual(card.locator('[data-gen-field="height"]').input_value(), "2048")
                        count = len(requests)
                        card.locator('[data-resolution="tier"]').select_option("2K")
                        self.assertEqual(len(requests), count)
                        self.assertEqual(page.locator('#flash-notice').count(), 0)
                        page.locator('#board-undo-btn').click()
                        page.wait_for_function("() => boardRequests.size === 0")
                        self.assertEqual(card.locator('[data-gen-field="height"]').input_value(), "1024")
                        page.locator('#board-undo-btn').click()
                        page.wait_for_function("() => boardRequests.size === 0")
                        self.assertEqual(card.locator('[data-gen-field="width"]').input_value(), "1024")
                        page.reload()
                        card = page.locator(f'.board-node-gen[data-node-id="{node["id"]}"]')
                        expect(card).to_be_visible()
                        self.assertEqual(card.locator('[data-gen-field="width"]').input_value(), "1024")
                        self.assertEqual(card.locator('[data-gen-field="height"]').input_value(), "1024")
                        card.locator('[data-gen-detail]').click()
                        expect(page.locator('#new-job-modal')).to_be_visible()
                        self.assertEqual(page.locator('#new-job-modal .option-input[data-name="width"]').input_value(), "1024")
                        self.assertEqual(page.locator('#new-job-modal .option-input[data-name="height"]').input_value(), "1024")
                        page.locator('#new-job-modal-close').click()
                        self.assertEqual(page.evaluate("() => currentTab"), "prboard")
                    finally:
                        browser.close()
            finally:
                server.terminate()
                server.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
