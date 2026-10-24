"""생성 카드 숫자칸의 휠 조절과 보드 줌·저장을 브라우저에서 검사한다."""
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BoardGenWheelUITests(unittest.TestCase):
    def test_focused_number_wheel_saves_and_unfocused_wheel_zooms(self):
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
                        project = page.request.post(base + "/api/projects", data={"name": "Wheel UI"}, headers=headers)
                        self.assertTrue(project.ok, project.text())
                        project_id = project.json()["id"]
                        page.goto(f"{base}/#project/{project_id}/prboard")
                        page.wait_for_function("() => currentTab === 'prboard' && currentBoardId !== null")
                        draft = {"template_id": "seed_batch", "workflow": {}, "slots": [],
                                 "exposed": ["seed_count", "width", "height"],
                                 "options": {"seed_count": "3", "width": "1536", "height": "1024"}}
                        node = page.evaluate("(draft) => createBoardNode('gen', null, '', null, true, draft)", draft)
                        self.assertIsNotNone(node)
                        card = page.locator(f'.board-node-gen[data-node-id="{node["id"]}"]')
                        expect(card).to_be_visible()
                        scale = page.evaluate("() => boardState.scale")

                        def wheel_field(name, delta, expected):
                            input_box = card.locator(f'[data-gen-field="{name}"]')
                            input_box.focus()
                            box = input_box.bounding_box()
                            self.assertIsNotNone(box)
                            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                            with page.expect_response(lambda response: response.request.method == "PATCH"
                                                      and f"/board/nodes/{node['id']}" in response.url
                                                      and response.request.post_data_json.get("values", {}).get(name) == expected) as saved:
                                page.mouse.wheel(0, delta)
                            self.assertEqual(saved.value.status, 200)
                            expect(input_box).to_have_value(expected)
                            self.assertEqual(page.evaluate("() => boardState.scale"), scale)

                        wheel_field("seed_count", -100, "4")
                        wheel_field("seed_count", 100, "3")
                        wheel_field("width", -100, "1540")
                        wheel_field("width", 100, "1536")
                        wheel_field("height", 100, "1020")
                        wheel_field("height", -100, "1024")
                        card.locator('[data-gen-field="width"]').focus()
                        card.locator('[data-gen-field="width"]').evaluate("el => el.blur()")
                        box = card.locator('[data-gen-field="width"]').bounding_box()
                        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                        page.mouse.wheel(0, -100)
                        page.wait_for_function("(before) => boardState.scale > before", arg=scale)
                        expect(card.locator('[data-gen-field="width"]')).to_have_value("1536")
                        page.reload()
                        card = page.locator(f'.board-node-gen[data-node-id="{node["id"]}"]')
                        expect(card).to_be_visible()
                        for name, value in (("seed_count", "3"), ("width", "1536"), ("height", "1024")):
                            expect(card.locator(f'[data-gen-field="{name}"]')).to_have_value(value)
                    finally:
                        browser.close()
            finally:
                server.terminate()
                server.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
