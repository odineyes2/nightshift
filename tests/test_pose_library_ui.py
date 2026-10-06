"""NS-39-2: Library 탭·Pose 서브탭·Grid/Details 토글·게시물 추가 모달 화면 검사.

- index.html에 탭 버튼·#tab-library·토글·추가 모달 칸이 있다
- 09-routing-results.js의 TAB_MAINS에 library가 있고 진입 시 openLibraryTab()을 부른다
- 17c-library.js·09-routing-results.js 문법(node --check, node가 없으면 건너뜀)
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
ROUTING = ROOT / "static" / "js" / "09-routing-results.js"
LIBRARY = ROOT / "static" / "js" / "17c-library.js"


def main():
    for needle in ['data-tab="library"', 'id="tab-library"', 'symbol id="i-library"', 'data-library-tab="pose"',
                   'id="library-display-toggle"', 'data-display="grid"', 'id="library-add-btn"',
                   'id="pose-add-modal"', 'id="pose-add-name"', 'id="pose-add-file"', 'id="pose-add-url"',
                   'id="pose-add-desc"', 'id="pose-add-prompt"', 'id="pose-add-error"']:
        assert needle in HTML, needle
    routing = ROUTING.read_text(encoding="utf-8")
    assert "library: document.getElementById('tab-library')" in routing
    assert "if(tab === 'library') openLibraryTab();" in routing
    lib = LIBRARY.read_text(encoding="utf-8")
    for needle in ["function openLibraryTab", "'/api/library/poses'", "fd.append('image_url'", "localStorage"]:
        assert needle in lib, needle
    # NS-40: 아티클 갤러리·Library 라이트박스(갤러리와 같은 클래스)·여러 장 입력·모바일 아이콘만 추가 버튼
    for needle in ['id="library-article"', 'id="library-article-grid"', 'class="gallery-grid" id="library-article-grid"',
                   'id="library-lightbox"', 'id="library-article-add-btn"', 'id="pose-add-file" accept="image/*" multiple',
                   '<textarea class="option-input" id="pose-add-url"', '<span class="btn-label">게시물 추가하기</span>']:
        assert needle in HTML, needle
    box = HTML[HTML.index('id="library-lightbox"'):HTML.index('id="video-gallery-lightbox"')]
    for cls in ["lightbox-close", "lightbox-fs-btn", "lightbox-rot-btn", "lightbox-nav", "lightbox-img-wrap",
                "lightbox-spinner", "lightbox-info", "lightbox-actions"]:
        assert cls in box, cls
    assert HTML.index('id="library-lightbox"') < HTML.index('src="/js/bundle.js"')   # 공용 .lightbox-rot-btn 연결을 받는다
    for needle in ["setupLightboxSwipe(libraryLightbox", "toggleLightboxFullscreen(libraryLightbox)",
                   "/images`", "openLibraryArticle"]:
        assert needle in lib, needle
    css = (ROOT / "static" / "css" / "11-library.css").read_text(encoding="utf-8")
    assert "@media (max-width:640px)" in css and "#library-add-btn .btn-label" in css
    node = shutil.which("node")
    if node:
        for f in (ROUTING, LIBRARY):
            r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
    else:
        print("node 없음 — 문법 검사 건너뜀")
    print("ok")


if __name__ == "__main__":
    main()
    sys.exit(0)
