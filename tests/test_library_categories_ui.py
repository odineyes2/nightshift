"""격리된 브라우저에서 사용자 정의 Library 카테고리와 아티클 조작을 확인한다."""
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from capture_ui import isolated_environment, login, wait_screen, wait_server  # noqa: E402
from capture_ui_support import (BOOTSTRAP, child_environment, copy_sources, free_port,
                                local_request, stop_process, tracked_files)  # noqa: E402


def main():
    (ROOT / ".ui-captures").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="library-ui-", dir=ROOT / ".ui-captures") as tmp:
        directory = Path(tmp)
        copy_sources(ROOT, tracked_files(ROOT), directory / "src")
        env = child_environment(directory)
        image = directory / "sample.png"
        Image.new("RGB", (64, 64), "blue").save(image)
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        (directory / "capture_app.py").write_text(BOOTSTRAP, encoding="utf-8")
        with (directory / "server.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [sys.executable, str(directory / "capture_app.py"), str(ROOT / "scripts"),
                 str(directory / "src" / "server"), str(port)],
                cwd=directory, env=env, stdout=log, stderr=log)
            try:
                wait_server(process, base)
                with isolated_environment(env, directory / "tmp"), sync_playwright() as playwright:
                    browser = playwright.chromium.launch(env=env)
                    try:
                        context = browser.new_context(service_workers="block", viewport={"width": 1300, "height": 850})
                        context.route("**/*", lambda route: route.continue_() if local_request(route.request.url, base) else route.abort())
                        login(context, base, env)
                        page = context.new_page()
                        page.goto(base + "/#library")
                        wait_screen(page, "library", None, None)

                        page.locator("#library-category-create").click()
                        page.locator("#library-category-name").fill("Shot & Framing")
                        page.locator("#library-category-save").click()
                        tab = page.get_by_role("button", name="Shot & Framing")
                        tab.wait_for()
                        assert "active" in tab.get_attribute("class")
                        assert page.locator("#library-sequence-btn").is_hidden()

                        page.locator("#library-add-btn").click()
                        page.locator("#pose-add-name").fill("첫 아티클")
                        page.locator("#pose-add-desc").fill("설명")
                        page.locator("#pose-add-prompt").fill("portrait")
                        page.locator("#pose-add-file").set_input_files(str(image))
                        page.locator("#pose-add-save").click()
                        page.locator("#library-article-name").get_by_text("첫 아티클").wait_for()
                        assert page.locator("#library-article-grid .gallery-item").count() == 1
                        assert page.locator("#library-article-move-btn").is_hidden()

                        page.locator("#library-article-add-btn").click()
                        page.locator("#pose-add-file").set_input_files(str(image))
                        page.locator("#pose-add-save").click()
                        page.get_by_text("2장", exact=True).wait_for()
                        picks = page.locator("#library-article-grid .library-cover-check")
                        picks.nth(1).click()
                        page.locator("#library-article-cover-btn").click()
                        page.locator("#library-article-grid .gallery-item").nth(1).locator(".library-cover-badge").wait_for()

                        page.locator("#library-article-edit-btn").click()
                        page.locator("#pose-add-name").fill("수정한 아티클")
                        page.locator("#pose-add-save").click()
                        page.locator("#library-article-name").get_by_text("수정한 아티클").wait_for()

                        page.locator("#library-category-rename").click()
                        page.locator("#library-category-name").fill("Frames")
                        page.locator("#library-category-save").click()
                        page.get_by_role("button", name="Frames").wait_for()
                        assert page.locator("#library-article-name").is_hidden()
                        for theme in ("light", "dark"):
                            page.evaluate("theme => localStorage.setItem('nightshift-theme', theme)", theme)
                            page.reload()
                            wait_screen(page, "library", None, None)
                            page.get_by_role("button", name="Frames").click()
                            page.locator("#library-list .library-card").wait_for()
                            for width, height in ((1300, 850), (390, 800)):
                                page.set_viewport_size({"width": width, "height": height})
                                page.screenshot(path=str(ROOT / ".ui-captures" / f"library-category-{theme}-{width}.png"), full_page=True)
                                assert page.locator("#tab-library").evaluate("el => el.scrollWidth <= window.innerWidth")

                        page.once("dialog", lambda dialog: (assert_dialog(dialog, "아티클 1개", "이미지 2장"), dialog.accept()))
                        page.locator("#library-category-delete").click()
                        page.get_by_role("button", name="Frames").wait_for(state="detached")
                        assert page.get_by_role("button", name="Frames").count() == 0
                        assert page.locator('#library-subtabs [data-library-tab="pose"]').get_attribute("class").find("active") >= 0
                        context.close()
                    finally:
                        browser.close()
            finally:
                stop_process(process)
    print("ok: category and article browser interactions")


def assert_dialog(dialog, *parts):
    for part in parts:
        assert part in dialog.message, dialog.message


if __name__ == "__main__":
    main()
