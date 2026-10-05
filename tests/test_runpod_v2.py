"""RunPod REST v2 이전(NS-33) 검사 — urlopen을 모의로 바꿔 외부 호출을 막는다. 실행: python tests/test_runpod_v2.py"""
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import tempfile

tmp = tempfile.mkdtemp()
for k in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{k}_DIR"] = os.path.join(tmp, k.lower())
os.environ.setdefault("NIGHTSHIFT_ADMIN_USER", "admin")
os.environ.setdefault("NIGHTSHIFT_ADMIN_PASSWORD", "Test-Passw0rd-xyz!")
os.environ.pop("NIGHTSHIFT_PUBLIC_URL", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import runpod_api as R  # noqa: E402

R.RUNPOD_API_KEY = "k"
assert R.RUNPOD_API_BASE == "https://api.runpod.io/v2"
calls = []   # (method, url, body)
queue = []   # 차례로 돌려줄 응답(값 또는 예외)


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(req, timeout=None):
    calls.append((req.get_method(), req.full_url, json.loads(req.data) if req.data else None))
    val = queue.pop(0)
    if isinstance(val, BaseException):
        raise val
    return Resp(b"" if val is None else json.dumps(val).encode())


def http_err(code, detail):
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(json.dumps({"title": "t", "status": code, "detail": detail}).encode()))


R.urllib.request.urlopen = fake_urlopen

# 목록 2쪽 이어 받기, 필드 대응, env 미노출, 꺼진 파드의 cost 0 → None
queue[:] = [
    {"pods": [{"id": "a", "name": "A", "status": "STARTING", "image": "img", "cost": 0.4, "startedAt": "2026-03-13T20:00:00Z",
               "gpu": {"id": "NVIDIA GeForce RTX 4090"}, "ports": ["8188/http"], "env": {"PW": "leak"},
               "mounts": {"network": [{"volumeId": "v1"}], "persistent": {"size": 20}}}],
     "pagination": {"nextCursor": "c2", "hasNextPage": True}},
    {"pods": [{"id": "b", "status": "EXITED", "cost": 0.0}], "pagination": {"nextCursor": None, "hasNextPage": False}},
]
calls.clear()
pods, err = R.list_runpod_pods_verbose()
assert err is None and [p["id"] for p in pods] == ["a", "b"], (pods, err)
assert urllib.parse.parse_qs(urllib.parse.urlparse(calls[1][1]).query)["cursor"] == ["c2"], calls
a, b = pods
assert a["status"] == "STARTING" and a["image"] == "img" and a["cost_per_hr"] == 0.4 and a["network_volume_id"] == "v1"
assert a["last_started_at"] == "2026-03-13T20:00:00Z" and a["last_status_change"] is None and a["ports"] == ["8188/http"]
assert b["cost_per_hr"] is None and b["network_volume_id"] is None
assert "leak" not in json.dumps(pods)
assert R.is_on("PROVISIONING") and R.is_on("STARTING") and R.is_on("RUNNING")
assert not R.is_on("EXITED") and not R.is_on("TERMINATED") and not R.is_on(None)

# 카드용 정규화 — gpu.id가 GPU 이름, 디스크 필드
card = R._normalize({"id": "a", "name": "A", "status": "RUNNING", "gpu": {"id": "G"}, "disk": 80, "cost": 0.3,
                     "mounts": {"persistent": {"size": 20}}, "env": {"PW": "leak"}})
assert card == {"pod_name": "A", "status": "RUNNING", "gpu_type": "G", "cost_per_hr": 0.3,
                "container_disk_gb": 80, "volume_gb": 20}, card

# 오류는 RFC 9457 detail을 보여 준다
queue[:] = [http_err(500, "boom")]
pods, err = R.list_runpod_pods_verbose()
assert pods is None and "HTTP 500" in err and "boom" in err, err

# 볼륨: network-volumes, dataCenter
queue[:] = [{"networkVolumes": [{"id": "v1", "name": "n", "size": 100, "dataCenter": "AP-JP-1", "type": "STANDARD"}]}]
calls.clear()
vols, err = R.list_network_volumes()
assert err is None and vols[0]["data_center"] == "AP-JP-1" and vols[0]["monthly_usd"] == 7.0, vols
assert calls[0][1] == "https://api.runpod.io/v2/network-volumes"
queue[:] = [None]
assert R.delete_network_volume("v1") is None and calls[-1][:2] == ("DELETE", "https://api.runpod.io/v2/network-volumes/v1")

# 켜기·끄기: /action에 {"action": ...}, 목표 상태를 돌려준다
queue[:] = [{"id": "a", "status": "STARTING", "env": {"PW": "leak"}}]
assert R.pod_action("a", "start") == ("RUNNING", None)
assert calls[-1] == ("POST", "https://api.runpod.io/v2/pods/a/action", {"action": "start"}), calls[-1]
queue[:] = [http_err(409, "already")]
st, err = R.pod_action("a", "stop")
assert st is None and "409" in err and "already" in err

# 만들기: 400이면 다음 GPU, 성공하면 그 GPU로
gpus = R.RUNPOD_TIERS["image"]["gpus"]
queue[:] = [http_err(400, "no capacity"), {"id": "new", "name": "t", "cost": 0.25, "gpu": {"id": gpus[1]}, "dataCenterId": "AP-JP-1"}]
calls.clear()
pod, err = R.create_pod("t", "image", env={"PW": "s"})
assert err is None and pod == {"id": "new", "name": "t", "cloud": "SECURE", "cost_per_hr": 0.25, "gpu": gpus[1],
                               "data_center": "AP-JP-1"}, (pod, err)
assert [c[2]["gpu"]["id"] for c in calls] == gpus[:2] and calls[0][2]["dataCenterIds"] == ["AP-JP-1"]
assert calls[0][1] == "https://api.runpod.io/v2/pods" and calls[0][2]["cloud"] == "SECURE" and calls[0][2]["env"] == {"PW": "s"}

# 402(잔액 부족)면 바로 멈춘다
queue[:] = [http_err(402, "no money")]
calls.clear()
pod, err = R.create_pod("t", "image")
assert pod is None and "402" in err and len(calls) == 1, (err, calls)

# 모두 400이면 4단계 × GPU 수만큼 시도하고 실패
queue[:] = [http_err(400, "x") for _ in range(4 * len(gpus))]
calls.clear()
pod, err = R.create_pod("t", "image")
assert pod is None and len(calls) == 4 * len(gpus) and not queue

# 코드에 v1 흔적이 없다
src = open(R.__file__, encoding="utf-8").read()
assert "rest.runpod.io" not in src and "desiredStatus" not in src

# --- 호출부(NS-33-2): STARTING은 켜짐(과금 중), EXITED면 세션을 동기화 시각으로 닫는다 ---
import asyncio  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from types import SimpleNamespace  # noqa: E402

os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import app as A  # noqa: E402

S = A.runpod_sessions
assert S._parse_runpod_dt("2026-03-13T20:00:00Z") == "2026-03-13T20:00:00+00:00"
assert S._parse_runpod_dt("2026-09-23 03:38:26.551 +0000 UTC") == "2026-09-23T03:38:26.551000+00:00"
assert S._parse_runpod_dt("garbage") is None

starting = {"id": "st1", "name": "nightshift-x", "status": "STARTING", "ports": ["8188/http"], "cost_per_hr": 0.3,
            "last_started_at": None, "last_status_change": None, "network_volume_id": None, "image": "img"}
R.list_runpod_pods_verbose = lambda: ([dict(starting)], None)
R.get_gpu_type_cached = lambda pid: "NVIDIA GeForce RTX 4090"
res = A.runpod_sync.sync_runpod_pods(1, False, lambda p: None, lambda pid: False)
assert [a["runpod_pod_id"] for a in res["added"]] == ["st1"], res   # 자동 등록
rows = S.list_sessions()
assert len(rows) == 1 and rows[0]["ended_at"] is None, rows       # 세션 열기
opened = rows[0]["started_at"]
A.runpod_sync._log_sessions([dict(starting)])                     # startedAt이 아직 없어도 같은 세션 유지
assert [r["started_at"] for r in S.list_sessions()] == [opened]

# 대시보드 "구동 중"에 STARTING 포함, EXITED 제외
R.list_network_volumes = lambda: ([], None)
R.get_client_balance = lambda: (1.0, None)
R.list_runpod_pods_verbose = lambda: ([dict(starting), dict(starting, id="ex1", status="EXITED")], None)
d = asyncio.run(A.runpod_dashboard_api(SimpleNamespace(state=SimpleNamespace(user={"id": 1, "role": "admin"}))))
assert [p["id"] for p in d["pods"]["items"]] == ["st1"], d

# 꺼지면(EXITED, v2에는 종료 시각 없음) 세션을 이번 동기화 시각으로 닫는다
before = datetime.now(timezone.utc)
A.runpod_sync._log_sessions([dict(starting, status="EXITED", cost_per_hr=None)])
row = S.list_sessions()[0]
assert row["ended_at"] and datetime.fromisoformat(row["ended_at"]) >= before - timedelta(seconds=2), row

# 부팅 감시: STARTING에서 멈춘 nightshift 파드도 시간이 지나면 지우고 다시 만든다
w = A.pod_registry.create_pod({"kind": "comfyui", "url": "https://stk-8188.proxy.runpod.net", "name": "stk",
                               "enabled": True, "note": "runpod:stk", "runpod_tier": "image"})
terminated = []
A.runpod_api.get_runpod_info = lambda url: {"status": "STARTING", "last_started_at": None}
A.runpod_api.create_pod = lambda name, tier, env, entrypoint: ({"id": "stk2"}, None)
A.runpod_api.terminate_pod = lambda rp: terminated.append(rp)
A.driver_for = lambda pod: type("D", (), {"health": staticmethod(lambda p, t=None: {"ok": False})})
A._log_runpod_sessions_quietly = lambda: None
A.ensure_runtime = lambda pod: None
acted = A._boot_watch_check(datetime.now(timezone.utc) + timedelta(hours=2))
assert acted.get(w["id"]) == "retried" and "stk" in terminated, (acted, terminated)
print("ok")
