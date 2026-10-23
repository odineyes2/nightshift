"""옵션 안내 창의 세 진입점·닫기·복사·입력 복귀를 브라우저에서 검사한다."""

from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static/index.html").read_text(encoding="utf-8")
MODAL = HTML[HTML.index('<div class="modal-overlay" id="prompt-options-help-modal"'):
             HTML.index('<div class="modal-overlay" id="cover-picker-modal"')]
SCRIPT = (ROOT / "static/js/17e-prompt-options-help.js").read_text(encoding="utf-8")
JOBS_SCRIPT = (ROOT / "static/js/01-core-jobs.js").read_text(encoding="utf-8")
DETAIL_SCRIPT = JOBS_SCRIPT[JOBS_SCRIPT.index("function renderJobDetailModalBody()"):JOBS_SCRIPT.index("// 이 작업이 만든 생성물")]
SPECS = [{"name": "ar", "aliases": ["aspect"], "format": "가로:세로",
          "example": "--ar 2:3", "description": "비율을 지정해요."}]


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.route("**/api/prompt-options/spec", lambda route: route.fulfill(json={"options": SPECS}))
            def parse_response(route):
                value = route.request.post_data_json["text"]
                if "--sed" in value:
                    route.fulfill(json={"clean_text": "cat", "options": {}, "errors": ["--sed: 모르는 옵션이에요."], "resolution": None})
                elif "--seed" in value:
                    route.fulfill(json={"clean_text": "cat", "options": {"seed": 7, "repeat": 4}, "errors": [], "resolution": [832, 1216]})
                else:
                    route.fulfill(json={"clean_text": value, "options": {}, "errors": [], "resolution": None})
            page.route("**/api/prompt-options/parse", parse_response)
            markup = (f"<div class='prompt-enhance-col'><div class='prompt-enhance-actions' id='new-actions'></div><textarea class='enhance-raw'></textarea></div>"
                      f"<div class='board-gen-field'><div id='board-actions'></div><textarea>board</textarea></div>"
                      f"<table><thead><tr><th id='csv-actions'></th></tr></thead><tbody id='csv-editor-tbody'><tr><td><input data-ci='0' value='csv'></td></tr></tbody></table>"
                      f"<div id='job-detail-title'></div><div id='job-detail-fields'></div><div id='job-detail-actions'></div>{MODAL}")
            page.route("http://127.0.0.1/", lambda route: route.fulfill(body=markup, content_type="text/html"))
            page.goto("http://127.0.0.1/")
            page.add_script_tag(content="const escapeHtml = s => String(s).replaceAll('&', '&amp;').replaceAll('<', '&lt;');"
                                        "const copyTextToClipboard = async s => { window.copied = s; };" + SCRIPT)
            page.evaluate("""() => {
              document.querySelector('#new-actions').append(createPromptOptionsHelpButton());
              document.querySelector('#board-actions').innerHTML = promptOptionsHelpButtonHtml();
              document.querySelector('#csv-actions').innerHTML = promptOptionsHelpButtonHtml(0);
            }""")
            for area, close in ((".prompt-enhance-col", "Escape"), (".board-gen-field", "backdrop"), ("#csv-actions", "button")):
                target = page.locator(area + " textarea") if area != "#csv-actions" else page.locator("#csv-editor-tbody input")
                target.fill("kept text")
                target.focus()
                if area != "#csv-actions":
                    target.evaluate("el => el.setSelectionRange(2, 5)")
                page.locator(area + " .prompt-options-help-button").click()
                page.locator("#prompt-options-help-modal").wait_for(state="visible")
                page.locator(".prompt-help-table tbody tr").first.wait_for(state="visible")
                assert page.locator(".prompt-help-table tbody tr").count() == 1
                page.locator(".prompt-help-copy").first.click()
                page.wait_for_function("window.copied !== undefined")
                assert target.input_value() == "kept text"
                if close == "Escape":
                    page.keyboard.press("Escape")
                elif close == "backdrop":
                    page.locator("#prompt-options-help-modal").evaluate("el => el.click()")
                else:
                    page.locator("#prompt-options-help-close").click()
                assert page.locator("#prompt-options-help-modal").is_hidden()
                assert target.evaluate("el => document.activeElement === el")
                if area != "#csv-actions":
                    assert target.evaluate("el => [el.selectionStart, el.selectionEnd]") == [2, 5]
            for selector in ('.prompt-enhance-col textarea', '.board-gen-field textarea', '#csv-editor-tbody input'):
                target = page.locator(selector)
                if selector.endswith('input'):
                    target.evaluate("el => el.dataset.col = 'main_prompt'")
                target.fill('cat --seed 7')
                chips = target.locator('xpath=following-sibling::*[1]')
                chips.locator('.prompt-option-chip').first.wait_for()
                assert 'seed 7' in chips.inner_text()
                assert '832×1216' in chips.inner_text()
                target.fill('cat --sed 1')
                chips.locator('.prompt-option-chip.error').wait_for()
                target.fill('cat')
                assert chips.is_hidden()
            page.add_script_tag(content=DETAIL_SCRIPT)
            page.evaluate("""() => {
              window.openJobDetailId = 'test';
              window.lastJobs = [{id:'test', status:'done', template_id:'seed_batch', options:{main_prompt:'cat'},
                prompt_overrides:[{line:1, seed:7, ar:[2,3], res:1024}, {line:2, seed:8}]}];
              window.estimateJob = () => ({durationMs:0, finishIso:''});
              window.podsCache = []; window.preflightLines = () => [];
              window.fmtTime = () => '-'; window.fmtDuration = () => '-'; window.STATUS_LABEL = {done:'완료'};
              window.renderJobDetailModalBody();
            }""")
            detail = page.locator('#job-detail-fields').inner_text()
            assert '프롬프트 값 사용 중' in detail
            assert 'seed 7 · 832×1216 · 외 1줄' in detail
            print("ok")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
