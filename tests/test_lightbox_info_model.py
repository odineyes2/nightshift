"""라이트박스 정보 패널의 모델 표시와 화면별 화살표 방향을 검사한다."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parent.parent
source = (ROOT / 'static/js/10-gallery-meta.js').read_text(encoding='utf-8')
panel = source[source.index('function createAssetMetaPanel('):source.index('\nconst imageMetaPanel =')]
css = '\n'.join(path.read_text(encoding='utf-8') for path in sorted((ROOT / 'static/css').glob('*.css')))
markup = (ROOT / 'static/index.html').read_text(encoding='utf-8')
overlay = markup[markup.index('<div class="lightbox-overlay" id="gallery-lightbox"'):
                 markup.index('<!-- Library 라이트박스')]
symbols = '<svg style="display:none"><symbol id="i-chevron-down" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></symbol></svg>'
fixture = f'<!doctype html><html><head><meta charset="utf-8"><style>{css}</style></head><body>{symbols}{overlay}</body></html>'

with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={'width': 1300, 'height': 850})
    details = {
        'checkpoint.png': {'checkpoint': 'checkpoint.safetensors'},
        'unet.png': {'params_json': json.dumps({'unets': ['anima.safetensors']})},
        'unknown.png': {'checkpoint': 'unknown.safetensors'},
    }
    entries = [
        {'kind': 'checkpoints', 'filename': 'checkpoint.safetensors', 'base_models': ['SDXL']},
        {'kind': 'diffusion_models', 'filename': 'anima.safetensors', 'base_models': ['Anima']},
    ]
    def respond(route):
        url = route.request.url
        if '/api/output-assets/detail?path=' in url:
            route.fulfill(json=details[url.split('path=', 1)[1]])
        elif url.endswith('/api/models'):
            route.fulfill(json={'items': entries})
        elif url == 'http://ns.test/':
            route.fulfill(content_type='text/html', body=fixture)
        else:
            route.abort()
    page.route('**/*', respond)
    page.goto('http://ns.test/')
    page.add_script_tag(content="""
        const ico = name => `<svg class="ico"><use href="#i-${name}"/></svg>`;
        const escapeHtml = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
        const AM_OPEN_KEY = 'test-meta-open';
        let assetMetaOpen = false;
        let modelRegistry = {items: {}};
        const modelEntry = (kind, name) => modelRegistry.items[`${kind}:${name}`] || {};
        const basesOf = entry => entry.base_models || (entry.base_model ? [entry.base_model] : []);
        async function fetchModelRegistry(){
            if(window.deferRegistry) await new Promise(resolve => window.completeRegistry = resolve);
            const data = await (await fetch('/api/models')).json();
            modelRegistry.items = Object.fromEntries(data.items.map(e => [`${e.kind}:${e.filename}`, e]));
        }
    """ + panel + """
        const imageMetaPanel = createAssetMetaPanel(document.getElementById('gallery-lightbox-meta'), 'image');
        document.getElementById('gallery-lightbox').style.display = 'flex';
    """)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    for filename, expected in [('checkpoint.png', ('SDXL', 'checkpoint.safetensors')),
                               ('unet.png', ('Anima', 'anima.safetensors')),
                               ('unknown.png', (None, 'unknown.safetensors'))]:
        page.evaluate('name => imageMetaPanel.show({name})', filename)
        rows = page.locator('#gallery-lightbox-meta .am-kv')
        values = {row.locator('dt').inner_text(): row.locator('dd').inner_text() for row in rows.all()}
        assert values.get('베이스 모델') == expected[0], (filename, values, page.locator('#gallery-lightbox-meta .am-info').inner_html(), errors)
        assert values.get('모델') == expected[1], (filename, values)
        assert '체크포인트' not in values

    page.evaluate('() => { modelRegistry.items = {}; window.deferRegistry = true; }')
    page.evaluate('() => imageMetaPanel.show({name: "unet.png"})')
    assert [row.locator('dt').inner_text() for row in page.locator('#gallery-lightbox-meta .am-kv').all()] == ['모델']
    page.evaluate('() => { window.deferRegistry = false; window.completeRegistry(); }')
    page.locator('#gallery-lightbox-meta .am-kv').filter(has_text='베이스 모델').wait_for(state='attached')
    assert 'Anima' in page.locator('#gallery-lightbox-meta .am-info').inner_text()

    overlay = page.locator('#gallery-lightbox')
    toggle = overlay.locator('.am-toggle')
    icon = toggle.locator('.ico')
    page.add_style_tag(content='.am-toggle .ico{transition:none!important}')
    for width, closed, opened in [(1300, 'matrix(0, -1, 1, 0, 0, 0)', 'matrix(0, 1, -1, 0, 0, 0)'),
                                   (390, 'none', 'matrix(-1, 0, 0, -1, 0, 0)')]:
        page.set_viewport_size({'width': width, 'height': 850})
        assert icon.evaluate('el => getComputedStyle(el).transform') == closed
        toggle.evaluate('el => el.click()')
        assert icon.evaluate('el => getComputedStyle(el).transform') == opened
        toggle.evaluate('el => el.click()')
    page.evaluate('() => imageMetaPanel.show({name: "unet.png"})')
    toggle.evaluate('el => el.click()')
    page.locator('#gallery-lightbox-img').evaluate("el => el.src = `data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='600' height='600'%3E%3Crect width='600' height='600' fill='%23708094'/%3E%3C/svg%3E`")
    shots = ROOT / 'tests/shots'
    shots.mkdir(exist_ok=True)
    for theme in ('light', 'dark'):
        page.evaluate('theme => document.documentElement.dataset.theme = theme', theme)
        for width in (1300, 390):
            page.set_viewport_size({'width': width, 'height': 850})
            page.screenshot(path=str(shots / f'lightbox_info_model_{theme}_{width}.png'))
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not errors, errors
    browser.close()
print('OK: lightbox model info and arrow directions')
