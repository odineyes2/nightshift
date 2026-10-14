"""NS-72-2: 마법사 2단계 텍스트 인코더 선택 — helper 동작과 spec.clip 전달을 정적·node로 검사."""
import re
import shutil
import subprocess
from pathlib import Path

WIZ = Path(__file__).resolve().parent.parent / "static" / "js" / "06-wizard.js"


def main():
    src = WIZ.read_text(encoding="utf-8")
    assert "wizard-text-encoder-input" in src and "wizard.textEncoder" in src
    assert "spec.clip = textEncoder" in src
    h = re.search(r"function wizardTextEncoder\(family\)\{.*?\n\}", src, re.S)
    assert h, "wizardTextEncoder"

    node = shutil.which("node")
    if not node:
        print("node 없음 — 실행 검사 건너뜀")
        return
    js = ("let wizard={textEncoder:null};" + h.group(0) + """
const ck={kind:'checkpoints',text_encoders:['a','b']}, one={kind:'diffusion_models',text_encoders:['a']};
const two={kind:'diffusion_models',text_encoders:['a','b']};
const r=[wizardTextEncoder(ck),wizardTextEncoder(one),wizardTextEncoder(two)];
wizard.textEncoder='b'; r.push(wizardTextEncoder(two));
wizard.textEncoder='zzz'; r.push(wizardTextEncoder(two));
console.log(JSON.stringify(r));""")
    out = subprocess.run([node, "-e", js], capture_output=True, text=True, check=True).stdout.strip()
    assert out == '[null,null,"a","b","a"]', out
    subprocess.run([node, "--check", str(WIZ)], check=True)
    print("ok")


if __name__ == "__main__":
    main()
