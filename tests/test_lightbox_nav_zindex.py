"""라이트박스 이동 버튼이 이미지 영역 위에 표시되도록 쌓임 순서를 확인한다."""
import re
from pathlib import Path


css = (Path(__file__).resolve().parent.parent / 'static/css/08-danbooru.css').read_text(encoding='utf-8')
nav_rule = re.search(r'(?m)^\s*\.lightbox-nav\s*\{([^}]*)\}', css)
assert nav_rule, '.lightbox-nav 규칙이 없다'
assert re.search(r'(?:^|;)\s*z-index\s*:\s*3\s*(?:;|$)', nav_rule.group(1)), '이동 버튼이 이미지 위에 있지 않다'

next_with_info = re.search(
    r'@media\s*\(min-width:\s*900px\)\s*\{\s*'
    r'\.lightbox-overlay:not\(\.lightbox-fullscreen\):has\(\.am-body:not\(\[hidden\]\)\)\s+'
    r'\.lightbox-next\s*\{([^}]*)\}', css,
)
assert next_with_info, '펼친 정보창의 다음 버튼 위치 규칙이 없다'
assert re.search(
    r'right\s*:\s*calc\(\s*40px\s*\+\s*320px\s*\+\s*20px\s*\+\s*8px\s*\)',
    next_with_info.group(1),
), '다음 버튼이 이미지 칸 안쪽 끝에 있지 않다'

print('OK: lightbox navigation z-index')
