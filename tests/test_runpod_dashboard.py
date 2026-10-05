"""내 Runpod 대시보드 조회 API(NS-31) 검사 — 외부 호출은 urlopen을 모의로 바꿔 막는다. 실행: python tests/test_runpod_dashboard.py"""
import asyncio
import io
import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path
from types import SimpleNamespace

tmp = tempfile.mkdtemp()
for k in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{k}_DIR"] = os.path.join(tmp, k.lower())
os.environ.setdefault("NIGHTSHIFT_ADMIN_USER", "admin")
os.environ.setdefault("NIGHTSHIFT_ADMIN_PASSWORD", "Test-Passw0rd-xyz!")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
os.chdir(Path(__file__).resolve().parent.parent / "server")

import app as A  # noqa: E402
from fastapi import HTTPException  # noqa: E402

R = A.runpod_api
KEY = "secret-key-xyz"
R.RUNPOD_API_KEY = KEY

PODS = [{"id": "p1", "name": "run", "desiredStatus": "RUNNING", "imageName": "img", "costPerHr": 0.3,
         "networkVolumeId": "v1", "env": {"JUPYTER_PASSWORD": "leak"}},
        {"id": "p2", "name": "off", "desiredStatus": "EXITED", "networkVolumeId": "v2"}]
VOLS = [{"id": "v1", "name": "used", "size": 100, "dataCenterId": "AP-JP-1"},
        {"id": "v2", "name": "stopped-pod", "size": 10}, {"id": "v3", "name": "lonely", "size": 50}]
BAL = {"data": {"myself": {"clientBalance": 12.5}}}

calls = []   # (method, url)
replies = {}   # url 조각 → 응답 값 또는 예외


class Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(req, timeout=None):
    calls.append((req.get_method(), req.full_url))
    assert req.get_header("Authorization") == f"Bearer {KEY}"
    for frag, val in replies.items():
        if frag in req.full_url:
            if isinstance(val, BaseException):
                raise val
            return Resp(val if isinstance(val, bytes) else json.dumps(val).encode())
    raise AssertionError(f"모르는 주소: {req.full_url}")


R.urllib.request.urlopen = fake_urlopen


def set_replies(pods=PODS, vols=VOLS, bal=BAL):
    replies.clear()
    replies.update({"/pods": pods, "/networkvolumes": vols, "graphql": bal})


def req(role):
    return SimpleNamespace(state=SimpleNamespace(user={"id": "u", "role": role} if role else None))


def dash(role="admin"):
    try:
        return 200, asyncio.run(A.runpod_dashboard_api(req(role)))
    except HTTPException as e:
        return e.status_code, e.detail


# 전체 성공 — 구동 중인 파드만, 볼륨은 연결 없는 것까지 전부, 잔액 USD, 전체 성공 시각
set_replies()
code, d = dash()
assert code == 200, d
assert [p["id"] for p in d["pods"]["items"]] == ["p1"], d["pods"]
assert d["pods"]["items"][0]["cost_per_hr"] == 0.3
assert [v["id"] for v in d["volumes"]["items"]] == ["v1", "v2", "v3"]
assert {v["id"]: v["pods"] for v in d["volumes"]["items"]} == {"v1": ["run"], "v2": ["off"], "v3": []}
assert d["balance"]["usd"] == 12.5 and d["balance"]["error"] is None
assert d["fetched_at"] and all(d[k]["fetched_at"] == d["fetched_at"] for k in ("pods", "volumes", "balance"))
assert d["api_key"] is True
dumped = json.dumps(d)
assert "leak" not in dumped and KEY not in dumped and "env" not in dumped

# 조회는 GET(목록)과 GraphQL 조회 POST 하나뿐 — 만들기·지우기·켜기·끄기를 부르지 않는다
assert sorted(m for m, _ in calls) == ["GET", "GET", "POST"], calls
assert all(u.endswith(("/pods", "/networkvolumes")) or u == R.RUNPOD_GRAPHQL_URL for _, u in calls), calls

# 0달러·빈 목록은 실패가 아니다
set_replies(pods=[], vols=[], bal={"data": {"myself": {"clientBalance": 0}}})
code, d = dash()
assert d["balance"]["usd"] == 0.0 and d["balance"]["error"] is None
assert d["pods"]["items"] == [] and d["volumes"]["items"] == [] and d["fetched_at"]

# 부분 실패 — 잔액만 시간 초과: 나머지는 보이고, 잔액은 0이 아니라 None+오류, 전체 시각은 없음
set_replies(bal=TimeoutError())
code, d = dash()
assert code == 200 and d["pods"]["items"] and d["volumes"]["items"]
assert d["balance"]["usd"] is None and "TimeoutError" in d["balance"]["error"] and d["balance"]["fetched_at"] is None
assert d["fetched_at"] is None and d["pods"]["fetched_at"]

# 부분 실패 — 파드 목록 HTTP 오류: 파드는 None(빈 목록 아님), 볼륨의 연결 파드도 None(모름)
set_replies(pods=urllib.error.HTTPError("u", 500, "x", {}, io.BytesIO(b"")))
code, d = dash()
assert d["pods"]["items"] is None and "HTTP 500" in d["pods"]["error"]
assert all(v["pods"] is None for v in d["volumes"]["items"]) and d["balance"]["usd"] == 12.5

# 잘못된 응답 — JSON 아님·형식 다름·GraphQL errors·숫자 아닌 잔액
for kw in ({"pods": b"<html>"}, {"vols": {"oops": 1}}, {"bal": {"errors": [{"message": KEY}]}},
           {"bal": {"data": {"myself": {"clientBalance": "12"}}}}, {"bal": {"data": {"myself": {"clientBalance": True}}}},
           {"bal": {"data": None}}):
    set_replies(**kw)
    code, d = dash()
    part = {"pods": "pods", "vols": "volumes", "bal": "balance"}[next(iter(kw))]
    assert code == 200 and d[part]["error"] and d["fetched_at"] is None, (kw, d)
    assert d[part].get("items", d[part].get("usd")) is None, (kw, d)
    assert KEY not in json.dumps(d), kw

# 키 없음 — 외부 호출 없이 셋 다 오류
R.RUNPOD_API_KEY = ""
calls.clear()
code, d = dash()
assert code == 200 and not d["api_key"] and calls == []
assert all("RUNPOD_API_KEY" in d[k]["error"] for k in ("pods", "volumes", "balance")) and d["fetched_at"] is None
R.RUNPOD_API_KEY = KEY

# 관리자 외 접근 차단 — 외부 호출도 하지 않는다
calls.clear()
assert dash("user")[0] == 403 and dash(None)[0] == 401 and calls == []

# 실제 미들웨어를 거친 비로그인 요청도 막힌다
from fastapi.testclient import TestClient  # noqa: E402
r = TestClient(A.app).get("/api/runpod/dashboard")
assert r.status_code in (401, 403), r.status_code
assert calls == []
print("ok")
