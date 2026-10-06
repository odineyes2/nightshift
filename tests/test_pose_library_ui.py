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
    assert (ROOT / "static" / "css" / "11-library.css").exists()
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
