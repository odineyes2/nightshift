"""실제 LoRA 렌더·이벤트와 스크롤, 선택, 강도 및 네 가지 화면을 브라우저로 검사한다."""
from pathlib import Path
import re
import tempfile
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / 'static/index.html').read_text(encoding='utf-8')
JS = (ROOT / 'static/js/06-wizard.js').read_text(encoding='utf-8')
SCRIPT = JS[JS.index('async function openWizardLoraModal()'):JS.index("document.getElementById('wizard-step-lora').addEventListener")]
CORE = (ROOT / 'static/js/01-core-jobs.js').read_text(encoding='utf-8')
ICO = CORE[CORE.index('function ico('):CORE.index('// ---- 긴 목록')]
MODAL = HTML[HTML.index('<div class="modal-overlay" id="wizard-lora-modal"'):HTML.index('<div class="modal-overlay" id="wizard-batchmode-modal"')]
SPRITE = re.search(r'<svg\b[^>]*>[\s\S]*?<symbol\b[\s\S]*?</svg>', re.sub(r'<!--[\s\S]*?-->', '', HTML)).group()
CSS = '\n'.join(p.read_text(encoding='utf-8') for p in sorted((ROOT / 'static/css').glob('*.css')))


def main():
    shots = Path(tempfile.mkdtemp(prefix='ns-120-lora-'))
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for theme in ('light', 'dark'):
                for width in (1300, 390):
                    page = browser.new_page(viewport={'width': width, 'height': 900})
                    page.route('**/*', lambda route: route.abort())
                    page.set_content('<!doctype html><html><head><meta charset="utf-8"></head><body>' + SPRITE + MODAL + '</body></html>')
                    page.add_style_tag(content=CSS)
                    page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                    page.add_script_tag(content='''
                    var wizard = {familyId:'test', loras:[]};
                    var preset = false;
                    var wizardIsPreset = () => preset;
                    var wizardTypeChosen = () => true;
                    var wizardUpdateStepButtons = () => {};
                    var openWizardModal = id => document.getElementById(id).style.display = 'flex';
                    var fetchComfyObjectInfo = async () => {};
                    var fetchModelInventory = async () => {};
                    var getLoraEntry = name => ({baseIds: name === 'incompatible' ? ['other'] : []});
                    var podsWithModel = (kind, name) => name.includes('000') ? ['테스트 워커'] : [];
                    var escapeHtml = s => String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
                    var names = Array.from({length:40}, (_,i) => 'models/' + String(i).padStart(3,'0') + '_긴이름_' + 'LongName'.repeat(8) + '.safetensors');
                    var comfyObjectInfoCache = {connected:true, models:{loras:[...names, 'incompatible']}};
                    ''' + ICO + SCRIPT)
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.evaluate('openWizardLoraModal()')
                    rows = page.locator('#wizard-lora-modal-body .wizard-pick-row')
                    assert rows.count() == 40
                    assert page.locator('.wizard-lora-check svg').count() == 0
                    listing = page.locator('#wizard-lora-modal-body .wizard-pick-list')
                    for fraction in (0.5, 1):
                        listing.evaluate('(el, f) => el.scrollTop = (el.scrollHeight-el.clientHeight)*f', fraction)
                        target = rows.evaluate_all('els => els.findIndex(el => {const r=el.getBoundingClientRect(), p=el.parentElement.getBoundingClientRect(); return r.top>=p.top && r.bottom<=p.bottom})')
                        assert target >= 0
                        row = rows.nth(target)
                        before = listing.evaluate('el => el.scrollTop')
                        name_x = row.locator('.wizard-lora-name > span').last.bounding_box()['x']
                        row.locator('.wizard-lora-name').click()
                        assert abs(listing.evaluate('el => el.scrollTop') - before) <= 1
                        assert row.locator('.wizard-lora-check use').get_attribute('href') == '#i-check'
                        assert row.locator('.wizard-lora-name > span').last.bounding_box()['x'] == name_x
                        strength = row.locator('input')
                        strength.click()
                        strength.fill('0.65')
                        assert page.evaluate('wizard.loras.length') == 1
                        assert page.evaluate('wizard.loras[0].strength') == 0.65
                        before = listing.evaluate('el => el.scrollTop')
                        row.locator('.wizard-lora-check use').dispatch_event('click')
                        expected = listing.evaluate('(el, old) => Math.min(old, el.scrollHeight-el.clientHeight)', before)
                        assert abs(listing.evaluate('el => el.scrollTop') - expected) <= 1
                        assert page.evaluate('wizard.loras.length') == 0
                    # Multiple selections retain edited strength after another render.
                    page.evaluate('''() => {wizard.loras=[]; renderWizardLoraModal();}''')
                    rows.nth(0).locator('.wizard-lora-name').click()
                    rows.nth(0).locator('input').fill('0.75')
                    rows.nth(1).locator('.wizard-lora-name').click()
                    assert page.evaluate('wizard.loras.length') == 2
                    assert rows.nth(0).locator('input').input_value() == '0.75'
                    page.evaluate('preset=true')
                    rows.nth(2).locator('.wizard-lora-name').click()
                    assert page.evaluate('wizard.loras.length') == 1
                    assert rows.nth(2).locator('input').input_value() == '1'
                    assert page.locator('.wizard-lora-check svg').count() == 1
                    # Opening afresh replaces the old list with loading content first.
                    page.evaluate('openWizardLoraModal()')
                    assert listing.evaluate('el => el.scrollTop') == 0
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    assert listing.evaluate('el => el.scrollWidth <= el.clientWidth')
                    for row in rows.all():
                        assert row.evaluate('el => el.scrollWidth <= el.clientWidth')
                    selected = rows.nth(2)
                    assert selected.locator('.wizard-pick-row-label').bounding_box()['x'] + selected.locator('.wizard-pick-row-label').bounding_box()['width'] <= selected.locator('input').bounding_box()['x']
                    # Bring the selected row into the captured viewport.
                    selected.scroll_into_view_if_needed()
                    page.screenshot(path=str(shots / f'{theme}-{width}.png'))
                    assert not errors, errors
                    page.close()
            print(f'PASS: LoRA selection, scroll, preset, strength, layout; screenshots: {shots}')
        finally:
            browser.close()


if __name__ == '__main__':
    main()

