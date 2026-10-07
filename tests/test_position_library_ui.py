"""NS-43-3: Library Position 서브탭 화면·수정/삭제·txt2img 시드/순차 마법사 연결 검사.

- index.html에 Position 서브탭·수정/삭제 버튼·라이트박스 "이 장 삭제"가 있다
- 17c-library.js가 서브탭에 따라 API(/api/library/positions)·PATCH·DELETE를 쓰고 Position은 txt2img 마법사로 간다
- positionSequenceValue가 image 없이 tags만 담는다(node로 실행)
- 고친 JS 문법(node --check, node가 없으면 건너뜀)
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "js"
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def read(name):
    return (JS / name).read_text(encoding="utf-8")


def main():
    for needle in ['data-library-tab="position"', 'id="library-article-edit-btn"', 'id="library-article-delete-btn"',
                   'id="library-lightbox-delete-btn"', 'id="pose-add-image-field"']:
        assert needle in HTML, needle
    lib = read("17c-library.js")
    for needle in ["'/api/library/positions'", "method: 'PATCH'", "method: 'DELETE'", "confirm(",
                   "startPositionWizard(p.danbooru_prompt)", "startPositionSequenceWizard(items)",
                   "library-image-delete-btn", "function setLibraryTab"]:
        assert needle in lib, needle
    wiz = read("06-wizard.js")
    for needle in ["async function startPositionWizard", "function startPositionSequenceWizard",
                   "wizard.post.openpose = false", "positionSequenceValue", "${noun}마다 생성 장수"]:
        assert needle in wiz, needle
    body = wiz[wiz.index("async function startPositionWizard"):]
    assert "wizard.base = 'txt2img'" in body and "wizard.batchMode = 'seed'" in body
    css = (ROOT / "static" / "css" / "11-library.css").read_text(encoding="utf-8")
    assert ".library-image-delete-btn" in css and "#pose-add-image-field[hidden]" in css

    node = shutil.which("node")
    if not node:
        print("node 없음 — JS 검사 건너뜀")
        print("ok")
        return
    for name in ("17c-library.js", "06-wizard.js", "04-newjob-tools.js"):
        r = subprocess.run([node, "--check", str(JS / name)], capture_output=True, text=True)
        assert r.returncode == 0, (name, r.stderr)
    tools = read("04-newjob-tools.js")
    fn = tools[tools.index("function positionSequenceValue"):]
    fn = fn[:fn.index("\n}\n") + 3]
    items = [{"position_id": 3, "position_name": "a", "danbooru_prompt": " sitting "},
             {"position_id": 4, "position_name": "b", "danbooru_prompt": ""}]
    script = fn + f"console.log(positionSequenceValue({json.dumps(items)}, true));" \
                  f"console.log(positionSequenceValue({json.dumps(items)}, false));"
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    on, off = [json.loads(line) for line in r.stdout.strip().splitlines()]
    assert on == [{"pose_id": 3, "pose_name": "a", "tags": "sitting"}, {"pose_id": 4, "pose_name": "b", "tags": ""}], on
    assert all("image" not in it and "width" not in it for it in on)
    assert [it["tags"] for it in off] == ["", ""]
    print("ok")


if __name__ == "__main__":
    main()
    sys.exit(0)
