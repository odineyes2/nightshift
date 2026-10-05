"""RunPod 세션(사용 내역) 로그 — Claude가 RunPod 커넥터로 pod를 켜고 끌 때마다 한 줄씩
남긴다. server/runpod_sync.py의 sync_runpod_pods()가 RunPod 계정의 pod마다
sync_pod_session()을 불러 채운다(DB 탭이 이 기록을 보여준다).

## 시작/종료 시각을 어떻게 아는가

시작 시각은 RunPod REST v2(`GET /v2/pods`)의 `startedAt`(RFC 3339)을 그대로 쓴다.
켜짐 판정은 `runpod_api.is_on`(PROVISIONING/STARTING/RUNNING = 과금 중)이다. 아직
startedAt이 없는 켜지는 중 파드는 처음 본 동기화 시각으로 세션을 연다.
v2에는 종료 시각(v1의 `lastStatusChange`)이 없어서, **종료 시각은 꺼짐을 처음 본
동기화 시각**이다(오차는 최대 RUNPOD_SESSION_LOG_SEC). 동기화 사이에 켜졌다 꺼진
세션은 남지 않을 수 있다. v1 형태의 값이 오면 예전처럼 그 시각을 쓴다.

## VRAM

RunPod API 어디에도 VRAM 필드가 없어서 GPU 모델명 → VRAM 고정표로 채운다
(https://www.runpod.io/gpu-models, 2026-09-24 확인). 표에 없는 모델은 vram_gb를
비워 둔다(추측하지 않음) — RunPod가 새 GPU를 추가하면 이 표를 갱신해야 한다.
"""

import re
from datetime import datetime, timezone

import db

GPU_VRAM_GB = {
    "B300": 288, "H200": 141, "B200": 180, "RTX PRO 6000": 96, "H100 NVL": 94,
    "H100 PCIe": 80, "H100 SXM": 80, "A100 PCIe": 80, "A100 SXM": 80,
    "L40S": 48, "RTX 6000 Ada": 48, "A40": 48, "L40": 48, "RTX A6000": 48,
    "RTX PRO 4500": 32, "RTX 5090": 32, "L4": 24, "RTX 3090": 24, "RTX 4090": 24,
    "RTX A5000": 24, "RTX 4000 Ada": 20, "RTX A4500": 20,
    "RTX A4000": 16, "RTX 5080": 16, "RTX 2000 Ada": 16,
}

# RunPod에서 사용하는 전체 모델명도 명시적으로 연결한다. 부분 문자열로 추정하지 않는다.
GPU_MODEL_ALIASES = {
    "NVIDIA RTX A4000": "RTX A4000",
    "NVIDIA RTX A4500": "RTX A4500",
    "NVIDIA GeForce RTX 5080": "RTX 5080",
    "NVIDIA RTX 4000 Ada Generation": "RTX 4000 Ada",
    "NVIDIA RTX PRO 4500 Blackwell": "RTX PRO 4500",
}

_STATUS_CHANGE_DATE_RE = re.compile(r"(\w{3} \w{3} \d{1,2} \d{4} \d{2}:\d{2}:\d{2} GMT[+-]\d{4})")


def vram_for(gpu_type: str) -> int | None:
    model = (gpu_type or "").strip()
    return GPU_VRAM_GB.get(GPU_MODEL_ALIASES.get(model, model))


def _parse_runpod_dt(raw: str | None) -> str | None:
    """RunPod 시각을 ISO로 — v2 startedAt의 RFC 3339("2026-03-13T20:00:00Z")와 v1 형태
    ("2026-09-23 03:38:26.551 +0000 UTC")를 모두 받는다. 알아볼 수 없으면 None(호출부가 "지금"으로 대신한다)."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        pass
    cleaned = raw.strip().removesuffix(" UTC").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f %z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            return datetime.strptime(cleaned, fmt).astimezone(timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def _parse_status_change_dt(raw: str | None) -> str | None:
    """"Exited by user: Wed Sep 23 2026 10:00:19 GMT+0000 (Coordinated Universal Time)"
    형태에서 날짜 부분만 정규식으로 뽑아 파싱한다. 문구 자체가 이 형태가 아니거나(다른
    전환 사유 등) 파싱에 실패하면 None."""
    if not raw:
        return None
    m = _STATUS_CHANGE_DATE_RE.search(raw)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%a %b %d %Y %H:%M:%S GMT%z").astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def _row_to_entry(row) -> dict:
    return {
        "id": row["id"], "runpod_pod_id": row["runpod_pod_id"], "pod_name": row["pod_name"],
        "gpu_type": row["gpu_type"],
        "vram_gb": row["vram_gb"] if row["vram_gb"] is not None else vram_for(row["gpu_type"]),
        "cost_per_hr": row["cost_per_hr"],
        "started_at": row["started_at"], "ended_at": row["ended_at"],
        "duration_sec": row["duration_sec"], "cost_total": row["cost_total"],
        "worker_name": row["worker_name"],
    }


def sync_pod_session(
    runpod_pod_id: str, pod_name: str, gpu_type: str, cost_per_hr: float | None, running: bool,
    last_started_at_raw: str | None, last_status_change_raw: str | None, worker_name: str | None = None,
) -> None:
    """이 pod의 지금 상태 하나를 반영해 세션을 열거나 닫는다 — runpod_sync.py가 RunPod
    계정의 pod마다 sync 한 번당 한 번씩 부른다."""
    now = db.now_iso()
    vram_gb = vram_for(gpu_type)
    with db.connect() as conn:
        open_row = conn.execute(
            "SELECT * FROM runpod_sessions WHERE runpod_pod_id=? AND ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (runpod_pod_id,),
        ).fetchone()

        if running:
            started_at = _parse_runpod_dt(last_started_at_raw) or now
            # startedAt이 아직 없으면(STARTING 등) 이미 연 세션을 그대로 둔다
            if open_row is not None and (open_row["started_at"] == started_at or not last_started_at_raw):
                if worker_name and not open_row["worker_name"]:   # 켜진 뒤에 워커가 연결됐으면 채운다
                    conn.execute("UPDATE runpod_sessions SET worker_name=? WHERE id=?", (worker_name, open_row["id"]))
                return  # 이미 이 시작을 기록해 뒀다 — 할 일 없음
            if open_row is not None:
                # 다른 시작 시각의 열린 세션이 있다는 건 그 사이 종료를 놓쳤다는 뜻 —
                # 잃어버리지 않게 "지금"으로 닫아 둔다(정확한 종료 시각은 알 수 없음).
                _close_row(conn, open_row, now)
            conn.execute(
                "INSERT INTO runpod_sessions(runpod_pod_id, pod_name, gpu_type, vram_gb, cost_per_hr, started_at, worker_name) "
                "VALUES(?,?,?,?,?,?,?)",
                (runpod_pod_id, pod_name, gpu_type or "", vram_gb, cost_per_hr, started_at, worker_name),
            )
        else:
            ended_at = _parse_status_change_dt(last_status_change_raw)
            if open_row is not None:
                _close_row(conn, open_row, ended_at or now)
                return
            # 켜질 때 sync를 못 불러 세션을 연 적이 없어도(새 워커 만들기로 켠 파드 등, NS-2), RunPod가 마지막
            # 시작·종료 시각을 둘 다 알려 주면 끝난 세션을 통째로 남긴다. 같은 시작은 두 번 넣지 않는다.
            started_at = _parse_runpod_dt(last_started_at_raw)
            if not started_at or not ended_at or datetime.fromisoformat(ended_at) <= datetime.fromisoformat(started_at):
                return
            if conn.execute("SELECT 1 FROM runpod_sessions WHERE runpod_pod_id=? AND started_at=?",
                            (runpod_pod_id, started_at)).fetchone():
                return
            cur = conn.execute(
                "INSERT INTO runpod_sessions(runpod_pod_id, pod_name, gpu_type, vram_gb, cost_per_hr, started_at, worker_name) "
                "VALUES(?,?,?,?,?,?,?)",
                (runpod_pod_id, pod_name, gpu_type or "", vram_gb, cost_per_hr, started_at, worker_name),
            )
            _close_row(conn, conn.execute("SELECT * FROM runpod_sessions WHERE id=?", (cur.lastrowid,)).fetchone(), ended_at)


def close_missing(present_pod_ids, ended_at: str | None = None) -> int:
    """RunPod 계정에서 사라진(지운) pod의 열린 세션을 닫는다 — 지운 pod는 목록에 안 나와서 위의
    sync_pod_session으로는 영영 안 닫힌다. 정확한 종료 시각은 모르므로 "지금"(또는 준 시각). 닫은 수."""
    ended_at = ended_at or db.now_iso()
    present = set(present_pod_ids)
    with db.connect() as conn:
        rows = [r for r in conn.execute("SELECT * FROM runpod_sessions WHERE ended_at IS NULL").fetchall()
                if r["runpod_pod_id"] not in present]
        for r in rows:
            _close_row(conn, r, ended_at)
    return len(rows)


def _close_row(conn, row, ended_at: str) -> None:
    started = datetime.fromisoformat(row["started_at"])
    ended = datetime.fromisoformat(ended_at)
    duration_sec = max(0, round((ended - started).total_seconds()))
    cost_per_hr = row["cost_per_hr"]
    cost_total = (duration_sec / 3600.0) * cost_per_hr if cost_per_hr is not None else None
    conn.execute(
        "UPDATE runpod_sessions SET ended_at=?, duration_sec=?, cost_total=? WHERE id=?",
        (ended_at, duration_sec, cost_total, row["id"]),
    )


def parse_bound(raw: str | None) -> datetime | None:
    """기간 경계(ISO 문자열)를 aware datetime으로. 시간대가 없으면 UTC로 본다. 틀린 값은 ValueError."""
    if not raw:
        return None
    dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _in_range(started_at: str, start: datetime | None, end: datetime | None) -> bool:
    """세션은 시작 시각 기준으로 통째로 한 기간에 든다(start 이상, end 미만, NS-20). started_at은
    표기가 제각각일 수 있어 문자열 비교 대신 파싱해서 비교한다."""
    if start is None and end is None:
        return True
    started = parse_bound(started_at)
    return (start is None or started >= start) and (end is None or started < end)


def list_sessions(limit: int | None = 200, start: datetime | None = None, end: datetime | None = None) -> list[dict]:
    """started_at DESC로 최근 것부터, [start, end) 안에 시작한 것만(limit=None이면 전부). 아직 열린
    세션(ended_at NULL)은 duration_sec/cost_total을 지금 시각 기준으로 즉석 계산해서 채워 돌려준다
    (추정치 — estimated=True)."""
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM runpod_sessions ORDER BY started_at DESC").fetchall()
    rows = [r for r in rows if _in_range(r["started_at"], start, end)]
    if limit is not None:
        rows = rows[:limit]
    now = datetime.now(timezone.utc)
    out = []
    for row in rows:
        entry = _row_to_entry(row)
        entry["estimated"] = False
        if entry["ended_at"] is None:
            started = datetime.fromisoformat(entry["started_at"])
            duration_sec = max(0, round((now - started).total_seconds()))
            entry["duration_sec"] = duration_sec
            entry["cost_total"] = (duration_sec / 3600.0) * entry["cost_per_hr"] if entry["cost_per_hr"] is not None else None
            entry["estimated"] = True
        out.append(entry)
    return out


def summarize_sessions(start: datetime | None = None, end: datetime | None = None) -> dict:
    """[start, end) 안에 시작한 세션 전체(LIMIT 없음)의 수·시간·비용 합계. 진행 중 세션은 지금 시각
    기준 추정치로 넣고 그 수를 estimated_count로 알린다. 비용을 모르는(cost_per_hr 없음) 세션은 비용 합에서 빠진다."""
    sessions = list_sessions(limit=None, start=start, end=end)
    return {
        "count": len(sessions),
        "duration_sec": sum(s["duration_sec"] or 0 for s in sessions),
        "cost_total": sum(s["cost_total"] or 0 for s in sessions),
        "estimated_count": sum(1 for s in sessions if s["estimated"]),
    }


__all__ = ["sync_pod_session", "close_missing", "list_sessions", "summarize_sessions", "parse_bound",
           "vram_for", "GPU_VRAM_GB"]
