"""NS-41-3: 마법사·세부설정의 OpenPose 화면 검사.

- 고친 JS 문법(node --check)
- postFitsFamily: family_kind "checkpoints" 유형은 UNet형(diffusion_models) family에서 숨는다
- workflowHasOpenPose: DWPreprocessor가 있을 때만 참 → pose_image 칸·OpenPose 패널이 보인다
- poseSizeFor: 포즈 비율에 맞춘 64 배수 크기(면적 약 1024², 512~2048)
- pose_image 칸은 업로드·갤러리 선택이 있는 buildInputImageControl로 그린다
node가 없으면 JS 실행 검사는 건너뛴다.
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "js"
FILES = [JS / f for f in ("02-job-move-newjob-form.js", "03-newjob-refs-inputs.js", "04-newjob-tools.js",
                          "06-wizard.js", "17b-settings.js")]
CSS = ROOT / "static" / "css" / "05-jobs.css"
HTML = ROOT / "static" / "index.html"


def func(src: str, name: str) -> str:
    m = re.search(r"function " + name + r"\([\s\S]*?\n}\n", src)
    assert m, f"{name}가 없다"
    return m.group(0)


def main():
    wizard = (JS / "06-wizard.js").read_text(encoding="utf-8")
    tools = (JS / "04-newjob-tools.js").read_text(encoding="utf-8")
    refs = (JS / "03-newjob-refs-inputs.js").read_text(encoding="utf-8")
    assert "postFitsFamily(t, currentFamily)" in wizard
    assert "setOpenPoseFieldsVisible(workflowHasOpenPose(parsed))" in wizard
    assert "spec.controlnet = await controlNetSpec(" in wizard
    assert "opt.name === 'pose_image'" in refs
    assert "'/api/controlnet/apply'" in tools and "/api/controlnet-defaults/" in tools
    html = HTML.read_text(encoding="utf-8")
    assert 'id="op-panel"' in html and 'id="op-edit-grid"' in html and 'id="op-setting-grid"' in html
    css = CSS.read_text(encoding="utf-8")
    assert '#options-fields:not(.has-openpose) .field:has([data-name="pose_image"])' in css

    node = shutil.which("node")
    if not node:
        print("node 없음 — JS 실행 검사 건너뜀")
        print("ok")
        return
    for f in FILES:
        subprocess.run([node, "--check", str(f)], check=True)
    script = "\n".join([func(wizard, "postFitsFamily"), func(wizard, "workflowHasOpenPose"),
                        func(tools, "poseSizeFor")]) + "\n" + """
const assert = require('assert');
const op = { id: 'openpose', family_kind: 'checkpoints' };
assert.strictEqual(postFitsFamily(op, { kind: 'checkpoints' }), true);
assert.strictEqual(postFitsFamily(op, { kind: 'diffusion_models' }), false);
assert.strictEqual(postFitsFamily({ id: 'hires_fix' }, { kind: 'diffusion_models' }), true);
assert.strictEqual(workflowHasOpenPose({ 1: { class_type: 'DWPreprocessor' } }), true);
assert.strictEqual(workflowHasOpenPose({ 1: { class_type: 'KSampler' } }), false);
assert.strictEqual(workflowHasOpenPose(null), false);
assert.deepStrictEqual(poseSizeFor(1000, 1000), { width: 1024, height: 1024 });
assert.deepStrictEqual(poseSizeFor(768, 1024), { width: 896, height: 1152 });
assert.deepStrictEqual(poseSizeFor(1920, 1080), { width: 1344, height: 768 });
const s = poseSizeFor(100, 2000);
assert.ok(s.width === 512 && s.height === 2048, JSON.stringify(s));
"""
    subprocess.run([node, "-e", script], check=True)
    print("ok")


if __name__ == "__main__":
    main()
    sys.exit(0)
