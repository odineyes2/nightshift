"""전체 화면 공용 핀치·이동을 실제 Chromium 터치 입력으로 검사한다."""
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
# 기존 검사의 마크업 추출만 재사용하며 검사 본문은 실행하지 않는다.
helpers = dict(__file__=str(ROOT / 'tests/test_lightbox_fullscreen.py'))
exec((ROOT / 'tests/test_lightbox_fullscreen.py').read_text(encoding='utf-8').split('with sync_playwright()')[0], helpers)
gallery = helpers['gallery']
shared = gallery.split('// ---- 라이트박스 전체 화면 (이미지·영상 공용) ----')[1].split('// 모바일에서 화살표 버튼 대신 스와이프로도 이미지를 넘길 수 있게')[0]
fixture = helpers['fixture']
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    context = browser.new_context(has_touch=True, viewport={'width':390, 'height':800})
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.set_content(fixture)
    page.add_script_tag(content="const ico = () => '';" + shared + """
      const o = document.getElementById('gallery-lightbox'), m = o.querySelector('.lightbox-img');
      o.style.display = 'flex';
      m.src = 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600"><rect width="800" height="600" fill="%2397A0AC"/><circle cx="400" cy="300" r="100" fill="%232563EB"/></svg>';
      window.next = 0; window.prev = 0;
      setupLightboxSwipe(o, m, {onNext:()=>window.next++, onPrev:()=>window.prev++});
    """)
    page.wait_for_function('m.complete && m.naturalWidth > 0')
    cdp = context.new_cdp_session(page)
    def touch(kind, points):
        cdp.send('Input.dispatchTouchEvent', {'type':kind, 'touchPoints':[{'x':x,'y':y,'id':i} for i,(x,y) in enumerate(points)]})
    def pinch(start=50, end=110):
        touch('touchStart', [(195-start,400),(195+start,400)])
        touch('touchMove', [(195-end,400),(195+end,400)])
        touch('touchEnd', [])
        page.wait_for_timeout(50)
    pinch()
    assert page.evaluate('m.style.scale') == ''
    cdp.send('Emulation.setPageScaleFactor', {'pageScaleFactor':1})
    page.evaluate('paintLightboxFullscreen(o,true)')
    pinch()
    assert 1 < page.evaluate('Number(m.style.scale)') <= 5
    touch('touchStart', [(195,400)])
    touch('touchMove', [(260,440)])
    touch('touchEnd', [])
    assert page.evaluate('window.next + window.prev') == 0
    assert page.evaluate('m.style.translate') != '0px 0px'
    page.evaluate('rotateLightboxView(o,90)')
    assert page.evaluate('m.style.scale') == ''
    pinch()
    assert page.evaluate('lightboxZoomed(o)')
    assert page.evaluate('(() => {const r=m.getBoundingClientRect(); return r.left<innerWidth && r.right>0 && r.top<innerHeight && r.bottom>0})()')
    page.evaluate('paintLightboxFullscreen(o,false)')
    assert page.evaluate('m.style.scale') == ''
    page.evaluate('o.dataset.rot=0; paintLightboxFullscreen(o,true)')
    touch('touchStart', [(280,400)])
    touch('touchMove', [(100,400)])
    touch('touchEnd', [])
    assert page.evaluate('window.next') == 1
    # 최대 배율과 1배 복귀.
    pinch(10,150)
    assert page.evaluate('Number(m.style.scale)') == 5
    pinch(120,20)
    assert not page.evaluate('lightboxZoomed(o)'), page.evaluate('[m.style.scale,m.style.translate]')
    # 실제 갤러리 렌더·닫기 진입점에서도 확대를 초기화한다.
    page.add_script_tag(content="const galleryLightbox=o; let lightboxIndex=0; const displayedGalleryImages=[];" +
                             helpers['function'](gallery, 'renderLightbox') + helpers['function'](gallery, 'closeLightbox'))
    pinch()
    page.evaluate('renderLightbox()')
    assert page.evaluate('m.style.scale') == ''
    pinch()
    page.evaluate('closeLightbox()')
    assert page.evaluate('m.style.scale') == ''
    page.evaluate("o.style.display='flex'; paintLightboxFullscreen(o,true)")
    # 영상의 실제 ignore 콜백을 그대로 연결한다.
    video = helpers['video']
    setup = video.split('const VIDEO_CONTROLS_STRIP = 64;')[1].split("document.getElementById('video-gallery-lightbox-close')")[0]
    page.add_script_tag(content="""const videoGalleryLightbox=document.getElementById('video-gallery-lightbox');
      const showNextVideo=()=>window.next++, showPrevVideo=()=>window.prev++;
      const VIDEO_CONTROLS_STRIP=64;
      o.style.display='none'; videoGalleryLightbox.style.display='flex'; paintLightboxFullscreen(videoGalleryLightbox,true);
    """ + setup)
    touch('touchStart', [(145,400),(245,780)])
    touch('touchMove', [(100,350),(290,780)])
    touch('touchEnd', [])
    assert page.evaluate("document.querySelector('video').style.scale") == ''
    page.evaluate("videoGalleryLightbox.style.display='none'; o.style.display='flex'")
    for theme in ('light','dark'):
        for width,height in ((1300,850),(390,800)):
            page.set_viewport_size({'width':width,'height':height})
            page.evaluate('theme=>{document.documentElement.dataset.theme=theme; resetLightboxZoom(o)}', theme)
            page.screenshot(path=str(ROOT / 'tests/shots' / f'lightbox_pinch_{theme}_{width}.png'))
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not errors, errors
    browser.close()
print('OK: fullscreen pinch zoom, pan, swipe, rotation, reset, video controls')

