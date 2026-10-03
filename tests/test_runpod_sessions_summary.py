"""RunPod 세션 기간 거르기·합계 검사(NS-20) — 임시 DB에 가짜 세션 260개를 넣고 손 계산과 비교한다.
실행: python tests/test_runpod_sessions_summary.py"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["NIGHTSHIFT_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "test.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import db  # noqa: E402
import runpod_sessions as rs  # noqa: E402

db.init()
UTC = timezone.utc
KST = timezone(timedelta(hours=9))
base = datetime(2026, 9, 1, tzinfo=UTC)
rows = []   # (started_at 문자열, 시작 datetime, 시간(초), 비용)
with db.connect() as conn:
    # 3시간마다 260개(9/1~10/3): 1시간, $0.5/시
    for i in range(260):
        s = base + timedelta(hours=3 * i)
        e = s + timedelta(hours=1)
        conn.execute("INSERT INTO runpod_sessions(runpod_pod_id, pod_name, gpu_type, cost_per_hr, started_at, ended_at, "
                     "duration_sec, cost_total) VALUES(?,?,?,?,?,?,?,?)",
                     (f"p{i}", "n", "L4", 0.5, s.isoformat(), e.isoformat(), 3600, 0.5))
        rows.append((s, 3600, 0.5))
    # 자정 경계: KST 9/30 23:30 시작(UTC 9/30 14:30) ~ 다음날 — 시작 기준으로 9월에 든다. 표기는 +09:00
    edge = datetime(2026, 9, 30, 23, 30, tzinfo=KST)
    conn.execute("INSERT INTO runpod_sessions(runpod_pod_id, pod_name, gpu_type, cost_per_hr, started_at, ended_at, "
                 "duration_sec, cost_total) VALUES(?,?,?,?,?,?,?,?)",
                 ("edge", "n", "L4", 1.0, edge.isoformat(), (edge + timedelta(hours=3)).isoformat(), 10800, 3.0))
    rows.append((edge, 10800, 3.0))
    # 진행 중 1개(비용 단가 없음)
    conn.execute("INSERT INTO runpod_sessions(runpod_pod_id, pod_name, gpu_type, cost_per_hr, started_at) VALUES(?,?,?,?,?)",
                 ("open", "n", "L4", None, (datetime.now(UTC) - timedelta(minutes=10)).isoformat()))


def expect(start, end):
    picked = [r for r in rows if (start is None or r[0] >= start) and (end is None or r[0] < end)]
    return len(picked), sum(r[1] for r in picked), sum(r[2] for r in picked)


# KST 2026년 9월
sep_start, sep_end = datetime(2026, 9, 1, tzinfo=KST), datetime(2026, 10, 1, tzinfo=KST)
for start, end in [(sep_start, sep_end),
                   (datetime(2026, 9, 30, tzinfo=KST), datetime(2026, 10, 1, tzinfo=KST)),   # 하루
                   (datetime(2026, 9, 21, tzinfo=KST), datetime(2026, 9, 28, tzinfo=KST)),   # 월요일 시작 주
                   (datetime(2027, 1, 1, tzinfo=KST), datetime(2028, 1, 1, tzinfo=KST))]:    # 빈 기간
    n, dur, cost = expect(start, end)
    sm = rs.summarize_sessions(start, end)
    assert (sm["count"], sm["duration_sec"]) == (n, dur), (start, sm, n, dur)
    assert abs(sm["cost_total"] - cost) < 1e-9, (start, sm, cost)
    assert sm["estimated_count"] == 0
    assert len(rs.list_sessions(start=start, end=end)) == min(n, 200)

# 9월은 200개를 넘는다 — 목록은 200개로 잘려도 합계는 전체
assert expect(sep_start, sep_end)[0] > 200
# 경계 세션은 KST 하루 9/30에 든다(UTC로 거르면 빠졌을 것)
assert any(s["runpod_pod_id"] == "edge" for s in rs.list_sessions(start=datetime(2026, 9, 30, tzinfo=KST),
                                                                   end=datetime(2026, 10, 1, tzinfo=KST)))

# 전체(기간 없음): 진행 중 세션이 추정으로 들어간다
sm = rs.summarize_sessions()
assert sm["count"] == len(rows) + 1 and sm["estimated_count"] == 1
assert sm["duration_sec"] >= sum(r[1] for r in rows) + 590
assert abs(sm["cost_total"] - sum(r[2] for r in rows)) < 1e-9   # 단가 없는 세션은 비용 합에서 빠진다
assert len(rs.list_sessions()) == 200

# 경계 문자열: Z·시간대 없음(UTC로 봄)·틀린 값
assert rs.parse_bound("2026-09-01T00:00:00Z") == datetime(2026, 9, 1, tzinfo=UTC)
assert rs.parse_bound("2026-09-01T00:00:00") == datetime(2026, 9, 1, tzinfo=UTC)
assert rs.parse_bound("") is None
try:
    rs.parse_bound("어제")
    raise AssertionError("틀린 값이 통과했다")
except ValueError:
    pass

# GPU 고정표는 공백만 제거해 정확히 연결한다(NS-27-2).
assert rs.vram_for("RTX PRO 4500") == 32
assert rs.vram_for(" RTX 4000 Ada \t") == 20
for gpu in ("", None, "Unknown GPU", "RTX 4000 Ada SFF", "rtx 4000 ada"):
    assert rs.vram_for(gpu) is None, gpu

# 과거·진행 중 기록의 NULL만 응답에서 보완하고 저장된 값(0 포함)은 유지한다.
cases = [
    ("RTX PRO 4500", None, 32),
    (" RTX 4000 Ada \t", None, 20),
    ("RTX 4000 Ada", 19, 19),
    ("RTX PRO 4500", 0, 0),
    ("Unknown GPU", None, None),
    ("RTX 4000 Ada SFF", None, None),
    ("", None, None),
]
with db.connect() as conn:
    for i, (gpu, stored, expected) in enumerate(cases):
        for opened in (False, True):
            conn.execute(
                "INSERT INTO runpod_sessions(runpod_pod_id, pod_name, gpu_type, vram_gb, started_at, ended_at) "
                "VALUES(?,?,?,?,?,?)",
                (f"vram-{i}-{opened}", "n", gpu, stored, base.isoformat(),
                 None if opened else (base + timedelta(hours=1)).isoformat()),
            )
    before = [tuple(r) for r in conn.execute("SELECT * FROM runpod_sessions ORDER BY id")]

entries = {s["runpod_pod_id"]: s for s in rs.list_sessions(limit=None)}
for i, (gpu, stored, expected) in enumerate(cases):
    for opened in (False, True):
        entry = entries[f"vram-{i}-{opened}"]
        assert entry["vram_gb"] == expected, entry
        assert entry["gpu_type"] == gpu
        assert entry["estimated"] == opened
rs.summarize_sessions()
with db.connect() as conn:
    after = [tuple(r) for r in conn.execute("SELECT * FROM runpod_sessions ORDER BY id")]
assert after == before, "조회가 저장된 세션을 변경했다"

# 신규 세션은 기존 동기화 흐름에서 두 모델의 용량을 저장한다.
for i, gpu in enumerate(("RTX PRO 4500", " RTX 4000 Ada ")):
    rs.sync_pod_session(f"new-vram-{i}", "n", gpu, 0.5, True,
                        "2026-09-01 00:00:00.000 +0000 UTC", None)
    with db.connect() as conn:
        row = conn.execute("SELECT vram_gb FROM runpod_sessions WHERE runpod_pod_id=?",
                           (f"new-vram-{i}",)).fetchone()
    assert row["vram_gb"] == (32, 20)[i]

print("ok: 기간·합계 및 VRAM 회귀 검사")
