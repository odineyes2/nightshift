"""NS-46-4: 마법사 LoRA 목록이 켜진 파드에 없는 등록부 LoRA도 보여 주는지 검사.

- renderWizardLoraModal이 modelChoices('loras') 대신 설치 ∪ 등록부 합집합 helper(wizardLoraChoices)를 쓴다
- helper를 node로 실행: 파일 이름(마지막 경로 조각)이 같으면 설치된 쪽 이름 하나만 남고, 파드가 없으면 등록부만
- 06-wizard.js 문법(node --check, node가 없으면 건너뜀)
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIZ = ROOT / "static" / "js" / "06-wizard.js"


def main():
    src = WIZ.read_text(encoding="utf-8")
    m = re.search(r"function renderWizardLoraModal\(\)\{.*?\n\}", src, re.S)
    assert m, "renderWizardLoraModal"
    assert "wizardLoraChoices()" in m.group(0)
    assert "modelChoices('loras')" not in m.group(0)
    h = re.search(r"function wizardLoraChoices\(\)\{.*?\n\}", src, re.S)
    assert h, "wizardLoraChoices"
    helper = h.group(0)
    assert "info.catalog" in helper and "split(/[\\\\/]/).pop()" in helper

    node = shutil.which("node")
    if not node:
        print("node 없음 — 실행 검사 건너뜀")
        return
    cases = [
        ({"connected": True, "models": {"loras": ["sub/a.safetensors", "b.safetensors"]},
          "catalog": {"loras": ["a.safetensors", "c.safetensors", "b.safetensors"]}},
         ["sub/a.safetensors", "b.safetensors", "c.safetensors"]),
        ({"connected": False, "models": {"loras": ["x.safetensors"]}, "catalog": {"loras": ["c.safetensors"]}},
         ["c.safetensors"]),
        (None, []),
    ]
    for info, want in cases:
        script = f"let comfyObjectInfoCache = {json.dumps(info)};\n{helper}\nconsole.log(JSON.stringify(wizardLoraChoices()));"
        out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout
        assert json.loads(out) == want, (info, out)
    subprocess.run([node, "--check", str(WIZ)], check=True)


if __name__ == "__main__":
    main()
    print("ok")
