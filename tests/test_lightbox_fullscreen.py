"""실제 Fullscreen API와 거절·미지원·종료 중 늦은 진입을 검사한다. OS 표시는 실기기 확인이 필요하다."""
import re
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
gallery = (ROOT / 'static/js/15-gallery.js').read_text(encoding='utf-8')
video = (ROOT / 'static/js/16-video.js').read_text(encoding='utf-8')
shared = gallery.split('// ---- 라이트박스 전체 화면 (이미지·영상 공용) ----')[1].split('// 화면 기준 손가락 이동')[0]
markup = (ROOT / 'static/index.html').read_text(encoding='utf-8')

def overlay(ref):
    start = markup.index(f'<div class="lightbox-overlay" id="{ref}"')
    depth = 0
    for match in re.finditer(r'<div\b[^>]*>|</div>', markup[start:]):
        depth += -1 if match[0] == '</div>' else 1
        if depth == 0:
            return markup[start:start + match.end()]

def function(source, name):
    start = source.index(f'function {name}(')
    end = source.index('\n}', start) + 2
    return source[start:end]

css = '\n'.join(p.read_text(encoding='utf-8') for p in sorted((ROOT / 'static/css').glob('*.css')))
symbols = '<svg style="display:none">' + ''.join(re.findall(r'<symbol\b.*?</symbol>', markup, re.S)) + '</svg>'
fixture = '<!doctype html><html><head><style>' + css + '</style></head><body>' + symbols + overlay('gallery-lightbox') + overlay('video-gallery-lightbox') + '</body></html>'
shots = ROOT / 'tests/shots'; shots.mkdir(exist_ok=True)

with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    page = browser.new_page()
    errors, notices = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('dialog', lambda dialog: (notices.append(dialog.message), dialog.accept()))
    def load():
        page.route('http://ns.test/', lambda route: route.fulfill(content_type='text/html', body=fixture))
        page.goto('http://ns.test/')
        page.add_script_tag(content="""
            const galleryLightbox = document.getElementById('gallery-lightbox');
            const videoGalleryLightbox = document.getElementById('video-gallery-lightbox');
            let lightboxIndex = 0;
            const assetMoveModalOpen = () => false;
            const ico = name => `<svg class="ico"><use href="#i-${name}"/></svg>`;
        """ + shared + function(gallery, 'closeLightbox') + """
            for(const overlay of document.querySelectorAll('.lightbox-overlay')){
                overlay.querySelector('.lightbox-fs-btn').onclick = () => toggleLightboxFullscreen(overlay);
                overlay.querySelector('.lightbox-close').onclick = () => {
                    setLightboxFullscreen(overlay, false); overlay.style.display = 'none';
                };
            }
            galleryLightbox.style.display = 'flex';
            document.getElementById('gallery-lightbox-close').onclick = closeLightbox;
            document.getElementById('gallery-lightbox-img').src = 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600"><rect width="800" height="600" fill="%2397A0AC"/></svg>';
        """ + "document.addEventListener('keydown'," + gallery.rsplit("document.addEventListener('keydown',", 1)[1].split('\n});', 1)[0] + '\n});')

    # 실제 브라우저 진입·해제, 외부 종료, 이미지·영상 공용 경로.
    load()
    page.evaluate("() => { window.requestOptions = []; const request = Element.prototype.requestFullscreen; Element.prototype.requestFullscreen = function(options){ window.requestOptions.push(options); return request.call(this, options); }; }")
    for ref in ('gallery-lightbox', 'video-gallery-lightbox'):
        page.evaluate("ref => { for(const overlay of document.querySelectorAll('.lightbox-overlay')) overlay.style.display = overlay.id === ref ? 'flex' : 'none'; }", ref)
        page.click(f'#{ref} .lightbox-fs-btn')
        page.wait_for_function('ref => document.fullscreenElement?.id === ref', arg=ref)
        assert page.evaluate('window.requestOptions.at(-1).navigationUI') == 'hide'
        page.evaluate('document.exitFullscreen()')
        page.wait_for_function('ref => !document.getElementById(ref).classList.contains("lightbox-fullscreen")', arg=ref)
        assert page.locator(f'#{ref} .lightbox-fs-btn').get_attribute('aria-label') == '전체 화면'
        page.click(f'#{ref} .lightbox-fs-btn')
        page.wait_for_function('ref => document.fullscreenElement?.id === ref', arg=ref)
        page.click(f'#{ref} .lightbox-fs-btn')
        page.wait_for_function('!document.fullscreenElement')

    page.evaluate("videoGalleryLightbox.style.display = 'none'; galleryLightbox.style.display = 'flex'")
    page.keyboard.press('f')
    page.wait_for_function('document.fullscreenElement === galleryLightbox')
    page.click('#gallery-lightbox-close')
    page.wait_for_function('!document.fullscreenElement && galleryLightbox.style.display === "none"')

    # 거절·미지원 환경은 창 채우기를 유지하고 안내한다.
    for implementation in ('() => Promise.reject(new Error("denied"))', 'undefined'):
        load()
        page.evaluate(f'galleryLightbox.requestFullscreen = {implementation}; galleryLightbox.webkitRequestFullscreen = undefined')
        page.click('#gallery-lightbox-fs-btn')
        page.wait_for_function('isLightboxFullscreen(galleryLightbox)')
        assert notices
        page.click('#gallery-lightbox-fs-btn')
        assert not page.evaluate('isLightboxFullscreen(galleryLightbox)')

    # 중복 클릭·요청 도중 닫기·늦은 성공은 다시 전체 화면에 갇히게 하지 않는다.
    load()
    page.evaluate("""() => { window.requests = 0; window.active = null; window.exits = 0;
        Object.defineProperty(document, 'fullscreenElement', {configurable:true, get:() => window.active});
        galleryLightbox.requestFullscreen = () => { window.requests++; return new Promise(resolve => window.finish = () => {
            window.active = galleryLightbox; document.dispatchEvent(new Event('fullscreenchange')); resolve(); }); };
        document.exitFullscreen = async () => { window.exits++; window.active = null; document.dispatchEvent(new Event('fullscreenchange')); };
    }""")
    page.click('#gallery-lightbox-fs-btn')
    page.evaluate('() => { setLightboxFullscreen(galleryLightbox, true); }')
    assert page.evaluate('window.requests') == 1
    page.click('#gallery-lightbox-close')
    page.evaluate('window.finish()')
    page.wait_for_function('window.exits === 1 && !document.fullscreenElement')
    assert not page.evaluate('isLightboxFullscreen(galleryLightbox)')

    # 다른 요소 및 영상 controls의 전체 화면을 앱 종료 함수가 해제하지 않는다.
    page.evaluate("window.active = document.querySelector('video'); window.exits = 0; setLightboxFullscreen(galleryLightbox, false)")
    assert page.evaluate('window.exits') == 0

    # 낮·밤, PC·모바일의 기존 레이아웃과 보기 회전.
    load()
    for theme in ('light', 'dark'):
        for width, height, tag in ((1300, 850, 'desktop'), (390, 800, 'mobile')):
            page.set_viewport_size({'width': width, 'height': height})
            page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
            page.click('#gallery-lightbox-fs-btn')
            page.wait_for_function('document.fullscreenElement === galleryLightbox')
            page.click('#gallery-lightbox .lightbox-rot-cw')
            page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            page.wait_for_function("(() => { const image = document.getElementById('gallery-lightbox-img'); const r = image.getBoundingClientRect(); return image.complete && image.naturalWidth > 0 && r.width > 0 && r.height > 0 && r.right > 0 && r.left < innerWidth; })()")
            page.screenshot(path=str(shots / f'lightbox_fullscreen_{theme}_{tag}.png'))
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.click('#gallery-lightbox-fs-btn')
            page.wait_for_function('!document.fullscreenElement')
    assert not errors, errors
    browser.close()
print('OK: lightbox fullscreen (OS status bar requires device verification)')
