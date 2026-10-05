"""NS-34-6: 마법사·세부설정의 Face Detailer 화면 검사.

- 06-wizard.js·05-models.js 문법(node --check)
- workflowHasFaceDetailer: 워크플로우에 FaceDetailer가 있을 때만 참 → 얼굴 프롬프트 칸이 보인다
- 05-jobs.css가 has-face-detailer 클래스가 없을 때 얼굴 프롬프트 칸을 숨긴다
node가 없으면 JS 실행 검사는 건너뛴다.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIZARD = ROOT / "static" / "js" / "06-wizard.js"
MODELS = ROOT / "static" / "js" / "05-models.js"
CSS = ROOT / "static" / "css" / "05-jobs.css"


def main():
    src = WIZARD.read_text(encoding="utf-8")
    m = re.search(r"function workflowHasFaceDetailer[\s\S]*?\n}\n", src)
    assert m, "workflowHasFaceDetailer가 없다"
    assert "setFaceDetailerFieldsVisible(workflowHasFaceDetailer(parsed))" in src
    assert "spec.face_detailer.enabled = true" in src
    assert "face_detailer: false" in MODELS.read_text(encoding="utf-8")

    css = CSS.read_text(encoding="utf-8")
    assert '#options-fields:not(.has-face-detailer) .field:has([data-name="face_prompt"]' in css

    node = shutil.which("node")
    if not node:
        print("node 없음 — JS 실행 검사 건너뜀")
        print("ok")
        return
    for f in (WIZARD, MODELS):
        subprocess.run([node, "--check", str(f)], check=True)
    cases = [
        ({"1": {"class_type": "FaceDetailer"}, "2": {"class_type": "SaveImage"}}, True),
        ({"1": {"class_type": "KSampler"}, "2": {"class_type": "SaveImage"}}, False),
        (None, False),
        ({}, False),
    ]
    script = m.group(0) + "\nconst cases = " + json.dumps(cases) + ";\n" + \
        "for(const [wf, want] of cases){ if(workflowHasFaceDetailer(wf) !== want){ console.error(JSON.stringify(wf)); process.exit(1); } }\n"
    subprocess.run([node, "-e", script], check=True)
    print("ok")


if __name__ == "__main__":
    main()
    sys.exit(0)
