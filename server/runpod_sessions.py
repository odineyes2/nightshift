"""RunPod 세션(사용 내역) 로그 — Claude가 RunPod 커넥터로 pod를 켜고 끌 때마다 한 줄씩
남긴다. server/runpod_sync.py의 sync_runpod_pods()가 RunPod 계정의 pod마다
sync_pod_session()을 불러 채운다(DB 탭이 이 기록을 보여준다).

## 시작/종료 시각을 어떻게 정확하게 아는가

nightshift가 sync를 늦게 불러도 시각이 부정확해지지 않는다 — RunPod REST API
(`GET /v1/pods`)가 그 pod에 실제로 일어난 시각을 자기 쪽에 남겨 두기 때문이다
(2026-09-24 실측 확인):
    lastStartedAt    = "2026-09-23 03:38:26.551 +0000 UTC"
    lastStatusChange = "Exited by user: Wed Sep 23 2026 10:00:19 GMT+0000 (Coordinated Universal Time)"
언제 물어보든 이 두 값을 그대로 가져오면 되므로, 사람이 MCP로 sync를 부르는 지금
방식 그대로도(주기적 자동 동기화 없이도) 정확하다. 다만 **sync를 아예 한 번도 안
부른 사이에 같은 pod가 두 번 이상 껐다 켜졌다 하면** RunPod 쪽에서도 최신 전환만
남기 때문에 중간 기록은 잃는다 — 시작/정지할 때마다(또는 그 근처에) sync를 부르는
정상적인 사용 패턴에서는 문제가 안 된다.

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
    "RTX A5000": 24, "RTX 2000 Ada": 16,
}

_STATUS_CHANGE_DATE_RE = re.compile(r"(\w{3} \w{3} \d{1,2} \d{4} \d{2}:\d{2}:\d{2} GMT[+-]\d{4})")


def vram_for(gpu_type: str) -> int | None:
    return GPU_VRAM_GB.get((gpu_type or "").strip())


def _parse_runpod_dt(raw: str | None) -> str | None:
    """"2026-09-23 03:38:26.551 +0000 UTC" 형태(RunPod REST의 lastStartedAt/createdAt)를
    ISO로. 알아볼 수 없으면 None(호출부가 "지금"으로 대신한다)."""
    if not raw:
        return None
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
        "gpu_type": row["gpu_type"], "vram_gb": row["vram_gb"], "cost_per_hr": row["cost_per_hr"],
        "started_at": row["started_at"], "ended_at": row["ended_at"],
        "duration_sec": row["duration_sec"], "cost_total": row["cost_total"],
    }


def sync_pod_session(
    runpod_pod_id: str, pod_name: str, gpu_type: str, cost_per_hr: float | None, running: bool,
    last_started_at_raw: str | None, last_status_change_raw: str | None,
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
            if open_row is not None and open_row["started_at"] == started_at:
                return  # 이미 이 시작을 기록해 뒀다 — 할 일 없음
            if open_row is not None:
                # 다른 시작 시각의 열린 세션이 있다는 건 그 사이 종료를 놓쳤다는 뜻 —
                # 잃어버리지 않게 "지금"으로 닫아 둔다(정확한 종료 시각은 알 수 없음).
                _close_row(conn, open_row, now)
            conn.execute(
                "INSERT INTO runpod_sessions(runpod_pod_id, pod_name, gpu_type, vram_gb, cost_per_hr, started_at) "
                "VALUES(?,?,?,?,?,?)",
                (runpod_pod_id, pod_name, gpu_type or "", vram_gb, cost_per_hr, started_at),
            )
        else:
            if open_row is None:
                return  # 열린 세션이 없으면 닫을 것도 없다
            ended_at = _parse_status_change_dt(last_status_change_raw) or now
            _close_row(conn, open_row, ended_at)


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


def list_sessions(limit: int = 200) -> list[dict]:
    """started_at DESC로 최근 것부터. 아직 열린 세션(ended_at NULL)은 duration_sec/
    cost_total을 지금 시각 기준으로 즉석 계산해서 채워 돌려준다(추정치 — estimated=True)."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM runpod_sessions ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
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


__all__ = ["sync_pod_session", "list_sessions", "vram_for", "GPU_VRAM_GB"]
