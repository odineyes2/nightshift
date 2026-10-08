"""새 작업의 워커 선택(formPodId) 검사 — test_job_queue_registration_ui.js를 node로 돌린다(node가 없으면 건너뜀).

python tests/test_job_queue_registration_ui.py
"""
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

node = shutil.which("node")
if not node:
    print("node 없음 — JS 실행 검사 건너뜀")
    raise SystemExit(0)
r = subprocess.run([node, "--check", str(ROOT / "static" / "js" / "09-routing-results.js")], capture_output=True, text=True)
assert r.returncode == 0, r.stderr
r = subprocess.run([node, str(ROOT / "tests" / "test_job_queue_registration_ui.js")], capture_output=True, text=True, encoding="utf-8")
assert r.returncode == 0, r.stdout + r.stderr
print("ok")
