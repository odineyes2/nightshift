"""무드보드 크기 설정 API의 인증·권한·입력 검증과 저장을 확인한다."""
import os
import json
import secrets
import subprocess
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
                        {"arrow_size": "medium", "port_size": "none"},
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
        without_arrow = {"arrow_size": "none", "port_size": "medium"}
        response = self.admin.put(path, json=without_arrow, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), without_arrow)
        self.assertEqual(self.member.get(path).json(), without_arrow)
        self.assertEqual(S.load(), without_arrow)

    def test_none_preview_and_board_arrow(self):
        from playwright.sync_api import expect, sync_playwright
        sys.path.insert(0, str(ROOT / "scripts"))
        from capture_ui import isolated_environment, login, wait_server
        from capture_ui_support import (BOOTSTRAP, child_environment, copy_sources, free_port,
                                        local_request, stop_process, tracked_files)

        (ROOT / ".ui-captures").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / ".ui-captures") as tmp:
            directory = Path(tmp)
            copy_sources(ROOT, tracked_files(ROOT), directory / "src")
            env = child_environment(directory)
            port = free_port()
            base = f"http://127.0.0.1:{port}"
            (directory / "capture_app.py").write_text(BOOTSTRAP, encoding="utf-8")
            with (directory / "server.log").open("w", encoding="utf-8") as log:
                process = subprocess.Popen([sys.executable, str(directory / "capture_app.py"),
                                            str(ROOT / "scripts"), str(directory / "src" / "server"), str(port)],
                                           cwd=directory, env=env, stdout=log, stderr=log)
                try:
                    wait_server(process, base)
                    with isolated_environment(env, directory / "tmp"), sync_playwright() as playwright:
                        browser = playwright.chromium.launch(env=env)
                        try:
                            context = browser.new_context(service_workers="block")
                            context.route("**/*", lambda route: route.continue_()
                                          if local_request(route.request.url, base) else route.abort())
                            login(context, base, env)
                            headers = {"x-requested-with": "nightshift", "content-type": "application/json"}
                            project = context.request.post(base + "/api/projects", headers=headers,
                                                           data=json.dumps({"name": "Arrow none UI"})).json()
                            page = context.new_page()
                            page.goto(f"{base}/#project/{project['id']}/prboard")
                            page.wait_for_function("() => currentTab === 'prboard' && currentBoardId !== null")
                            memo = page.evaluate("() => createBoardNode('text', null, '메모', null, false)")
                            gen = page.evaluate("""() => createBoardNode('gen', null, '', null, true,
                                { template_id: 'seed_batch', workflow: {}, options: { main_prompt: '기본' }, exposed: ['main_prompt'] })""")
                            page.wait_for_function("() => boardNodes.length === 2")
                            page.evaluate("([a, b]) => createBoardEdge(a, b, 'main_prompt', false)",
                                          [memo["id"], gen["id"]])
                            page.wait_for_function("() => boardEdges.length === 1")
                            arrow = page.locator("#board-edges-top .board-edge-arrow")
                            expect(arrow).to_have_count(1)
                            self.assertEqual(arrow.get_attribute("marker-end"), "url(#board-arrow)")

                            page.goto(base + "/#settings")
                            page.locator('[data-settings-tab="moodboard"]').click()
                            page.locator('[name="board-arrow-size"][value="none"]').check()
                            self.assertIsNone(page.locator("#board-display-preview > svg > path").get_attribute("marker-end"))
                            shots = ROOT / ".ui-captures" / "board-display-none"
                            shots.mkdir(exist_ok=True)
                            for theme in ("light", "dark"):
                                page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
                                for width, height in ((1300, 850), (390, 800)):
                                    page.set_viewport_size({"width": width, "height": height})
                                    page.screenshot(path=str(shots / f"{theme}-{width}.png"))
                            page.set_viewport_size({"width": 1300, "height": 850})
                            with page.expect_response(lambda response: response.url.endswith("/api/settings/board-display")
                                                      and response.request.method == "PUT" and response.status == 200):
                                page.locator("#board-display-save").click()
                            self.assertEqual(page.locator("#board-display-state").inner_text(), "저장했어요.")
                            page.goto(f"{base}/#project/{project['id']}/prboard")
                            page.wait_for_function("() => currentTab === 'prboard' && currentBoardId !== null && boardEdges.length === 1")
                            expect(arrow).to_have_count(1)
                            self.assertIsNone(arrow.get_attribute("marker-end"))
                            self.assertEqual(page.locator("#board-edges-top").count(), 1)
                            page.locator(".board-edge-hit").click(force=True)
                            expect(page.locator("#board-edge-del")).to_have_count(1)
                            page.locator("#board-edge-del").click()
                            expect(page.locator(".board-edge-hit")).to_have_count(0)

                            page.goto(base + "/#settings")
                            page.locator('[data-settings-tab="moodboard"]').click()
                            page.locator('[name="board-arrow-size"][value="medium"]').check()
                            self.assertEqual(page.locator("#board-display-preview > svg > path").get_attribute("marker-end"),
                                             "url(#board-preview-arrow)")
                            with page.expect_response(lambda response: response.url.endswith("/api/settings/board-display")
                                                      and response.request.method == "PUT" and response.status == 200):
                                page.locator("#board-display-save").click()
                            page.goto(f"{base}/#project/{project['id']}/prboard")
                            page.wait_for_function("() => currentTab === 'prboard' && currentBoardId !== null && boardNodes.length === 2")
                            page.evaluate("([a, b]) => createBoardEdge(a, b, 'main_prompt', false)",
                                          [memo["id"], gen["id"]])
                            page.wait_for_function("() => boardEdges.length === 1")
                            expect(arrow).to_have_count(1)
                            self.assertEqual(arrow.get_attribute("marker-end"), "url(#board-arrow)")
                            context.close()
                        finally:
                            browser.close()
                finally:
                    stop_process(process)


if __name__ == "__main__":
    unittest.main()
