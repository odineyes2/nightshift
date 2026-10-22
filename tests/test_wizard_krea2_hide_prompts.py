"""Krea.2 마법사 적용 시 프롬프트 칸을 숨기고 계열 변경 때 복원하는지 브라우저에서 검사한다."""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_workflow_option_panels import fixture


def main(capture=False):
    from playwright.sync_api import sync_playwright

    html, script = fixture()
    script = script.replace(
        "const wizardBaseMeta = () => ({}), wizardPostApplies",
        "const wizardBaseMeta = () => ({architecture: wizard.familyId === 'krea' ? 'krea2' : 'sdxl'}), wizardPostApplies",
    )
    script = script.replace(
        "const wizardQuality = {key:null};",
        "let wizardQuality = {key:null}; const clearWizardQuality = () => { wizardQuality.key = null; };",
    )
    script = script.replace(
        "const setWizardQualityPrompt = async () => {}, onTemplateChange = async () => {};",
        """const setWizardQualityPrompt = async () => {
  wizardQuality.key = wizard.familyId;
  document.getElementById('quality-prompt-field').style.display = '';
};
const onTemplateChange = async () => {
  const qualityField = document.getElementById('quality-prompt-field');
  optionsFields.before(qualityField);
  optionsFields.innerHTML = '';
  optionsFields.appendChild(qualityField);
  for(const name of ['negative_prompt', 'face_negative_prompt']){
    const field = document.createElement('div'); field.className = 'field';
    const label = document.createElement('label'); label.className = 'field-label';
    label.textContent = name === 'negative_prompt' ? '네거티브 프롬프트' : '얼굴 네거티브 프롬프트';
    const input = document.createElement('input'); input.dataset.name = name;
    field.appendChild(label); field.appendChild(input); optionsFields.appendChild(field);
  }
};""",
    )
    assert "wizard.familyId === 'krea'" in script
    assert "const onTemplateChange = async () => {" in script

    def respond(route):
        url = route.request.url
        if url == 'http://127.0.0.1/':
            route.fulfill(content_type='text/html', body=html)
        elif '/api/build-workflow' in url:
            route.fulfill(json={'workflow': {'1': {'class_type': 'KSampler'}}})
        elif '/api/' in url:
            route.fulfill(json={'values': {}})
        else:
            route.abort()

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            page.route('**/*', respond)
            page.goto('http://127.0.0.1/')
            page.add_script_tag(content=script)
            page.evaluate("document.getElementById('new-job-details-panel').style.display = ''")
            for family, visible in [('krea', False), ('illustrious', True), ('krea', False)]:
                result = page.evaluate("async family => { wizard.familyId = family; return await wizardApply(); }", family)
                assert result, family
                page.evaluate("document.getElementById('new-job-details-panel').style.display = ''")
                for selector in ('#quality-prompt-field', '#options-fields [data-name="negative_prompt"]'):
                    assert page.locator(selector).is_visible() == visible, (family, selector)
                if family == 'krea':
                    assert page.locator('[data-name="face_negative_prompt"]').locator('xpath=..').evaluate(
                        '(el) => el.style.display') == 'none'
                assert bool(page.evaluate('wizardQuality.key')) == visible, family
                if family == 'krea':
                    assert page.locator('#sweet-prefix-field').is_hidden()
            if capture:
                output = ROOT / 'tests/shots/wizard-krea2-prompts'
                output.mkdir(parents=True, exist_ok=True)
                for theme in ('light', 'dark'):
                    page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                    for width in (1300, 390):
                        page.set_viewport_size({'width': width, 'height': 850})
                        for family in ('krea', 'illustrious'):
                            assert page.evaluate('async family => {wizard.familyId=family; return await wizardApply();}', family)
                            page.evaluate("newJobActiveTab = 'details'; renderNewJobTabs()")
                            page.locator('#new-job-modal .modal-panel').screenshot(
                                path=str(output / f'{theme}-{width}-{family}.png'))
        finally:
            browser.close()
    print('ok: Krea.2 숨김, 다른 계열 복원, 재적용 숨김')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--capture', action='store_true')
    main(parser.parse_args().capture)
