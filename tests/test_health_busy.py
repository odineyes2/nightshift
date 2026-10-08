"""dev 오케스트레이터용 /api/health·/api/jobs/busy — 로그인 없이 열리고, busy는 running 작업만 본다. 임시 데이터 폴더만 쓴다."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
os.environ.setdefault("NIGHTSHIFT_ADMIN_USER", "admin")
os.environ.setdefault("NIGHTSHIFT_ADMIN_PASSWORD", "Test-Passw0rd-xyz!")
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")

import app as A
from fastapi.testclient import TestClient

c = TestClient(A.app)   # lifespan 없이 — 스케줄러를 띄우지 않는다
assert c.get("/api/health").status_code == 200
assert c.get("/api/jobs").status_code == 401   # 다른 API는 여전히 로그인 필요
assert c.get("/api/jobs/busy").json() == {"busy": False}
A.jobs["q"] = {"status": "queued"}
assert c.get("/api/jobs/busy").json() == {"busy": False}   # queued는 재시작 뒤에도 남는다
A.jobs["d"] = {"status": "running", "deleted": True}
assert c.get("/api/jobs/busy").json() == {"busy": False}
A.jobs["r"] = {"status": "running"}
assert c.get("/api/jobs/busy").json() == {"busy": True}
c.close()
print("ok")
