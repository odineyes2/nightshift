"""CSV 편집 모달 수식 검사 — test_csv_formula.js를 node로 돌린다(node가 없으면 건너뜀).

python tests/test_csv_formula.py
"""
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

node = shutil.which("node")
if not node:
    print("node 없음 — JS 실행 검사 건너뜀")
    raise SystemExit(0)
r = subprocess.run([node, "--check", str(ROOT / "static" / "js" / "04a-csv-formula.js")], capture_output=True, text=True)
assert r.returncode == 0, r.stderr
r = subprocess.run([node, str(ROOT / "tests" / "test_csv_formula.js")], capture_output=True, text=True, encoding="utf-8")
assert r.returncode == 0, r.stdout + r.stderr
print("ok")
