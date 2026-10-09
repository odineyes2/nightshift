"""아티클 대표 이미지 화면(NS-61) — 마크업·JS 문자열과 node --check로 검사한다."""
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS_PATH = ROOT / "static" / "js" / "17c-library.js"
JS = JS_PATH.read_text(encoding="utf-8")
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "css" / "11-library.css").read_text(encoding="utf-8")


def main():
    # 대표 선택 버튼 — 아티클 헤더와 라이트박스
    for bid in ("library-article-cover-btn", "library-lightbox-cover-btn"):
        assert f'id="{bid}"' in HTML, bid
        assert f"getElementById('{bid}').addEventListener" in JS, bid
    # 저장은 기존 PATCH에 cover_image_id
    assert "JSON.stringify({ cover_image_id: imageId })" in JS
    assert "method: 'PATCH'" in JS
    # 참조 생성(목록 카드)·순차 생성은 대표를 쓴다
    assert "poseGenerateButton('library-card-pose-btn', p.cover_image_id)" in JS
    assert "p.images[0].id" not in JS
    assert "flatMap(p => p.images" not in JS
    assert "im.id === p.cover_image_id" in JS
    assert "모든 이미지를" not in JS and "대표 이미지를" in JS
    # 격자 체크·대표 배지
    assert "library-cover-check" in JS and "library-cover-badge" in JS
    assert ".library-cover-badge" in CSS and ".library-cover-check" in CSS
    # 마법사 참조 목록은 높이를 제한하고 스크롤한다 — 베이스 모델 목록을 가리지 않는다(NS-61-3)
    rule = CSS.split(".library-selection-preview {", 1)[1].split("}", 1)[0]
    assert "max-height:" in rule and "overflow-y:auto" in rule and "flex-shrink:0" in rule, rule
    assert "#wizard-family-modal-body > .wizard-pick-list { flex-shrink:0; }" in CSS
    node = shutil.which("node")
    if node:
        r = subprocess.run([node, "--check", str(JS_PATH)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    else:
        print("node 없음 — node --check 건너뜀")
    print("ok")


if __name__ == "__main__":
    main()
