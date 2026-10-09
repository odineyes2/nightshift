"""마법사 전체 초기화 JS 검사 — node가 없으면 건너뛴다."""
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
node = shutil.which("node")
if not node:
    print("node 없음 — JS 실행 검사 건너뜀")
    raise SystemExit(0)
for name in ("03-newjob-refs-inputs.js", "05-models.js", "06-wizard.js"):
    result = subprocess.run([node, "--check", str(ROOT / "static/js" / name)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
result = subprocess.run([node, str(ROOT / "tests/test_newjob_wizard_reset.js")], capture_output=True, text=True, encoding="utf-8")
assert result.returncode == 0, result.stdout + result.stderr
print("ok")
