"""유료 회원의 기간·하루 한도를 판정하고, 관리자 파드로 보낼 작업을 기록(예약)한다.

## 규칙 (2026-11-15 사용자 결정)

- 관리자가 회원 관리에서 유료 기간(그날까지, 서버 현지 날짜)과 하루 한도(이미지 장수·영상 개수)를 정한다. 결제는 아직 없다.
- 대상은 연동 앱(charchat)이 회원 대신 보낸 작업(job.via_service)뿐이다 — nightshift를 직접 쓰는 작업은 늘 회원 자기 파드.
- 기간 안이고 오늘 한도가 남았으면 작업에 billing="admin"을 붙이고 paid_usage에 적는다. 그 작업은 관리자 파드에서만 돈다
  (service_tokens.pod_owner_ids_for). 관리자 RunPod 키는 서버 안에서만 쓰이고, 회원에게는 관리자 워커의 이름·주소·GPU를 보이지 않는다.
- 한도를 넘으면 표시하지 않는다 → 회원 키가 있으면 회원 워커 자동 켜기(경로 1), 없으면 대기. 날이 바뀌거나 유료가 되면
  대기 중인 작업을 다시 본다(promote_waiting).
- 이미 관리자 파드로 정해진 작업은 기다리는 동안 기간이 끝나도 그대로 처리한다.
"""

import re
from datetime import date, datetime

import auth
import db

DAILY_LIMIT_MAX = 10000


class PlanError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def today() -> str:
    return datetime.now().astimezone().date().isoformat()


def _row(user_id):
    with db.read() as conn:
        return conn.execute("SELECT id, role, status, paid_until, paid_daily_images, paid_daily_videos FROM users WHERE id=?",
                            (user_id,)).fetchone()


def is_paid(row, on: str | None = None) -> bool:
    return bool(row and row["status"] == auth.STATUS_ACTIVE and row["role"] != auth.ROLE_ADMIN
                and row["paid_until"] and (on or today()) <= row["paid_until"])


def usage_on(user_id, day: str | None = None) -> dict:
    with db.read() as conn:
        rows = conn.execute("SELECT kind, SUM(units) AS n FROM paid_usage WHERE user_id=? AND day=? GROUP BY kind",
                            (user_id, day or today())).fetchall()
    used = {"image": 0, "video": 0}
    used.update({r["kind"]: r["n"] or 0 for r in rows})
    return used


def plan(user_id) -> dict:
    """화면용 — 유료 여부·기간·한도·오늘 쓴 양."""
    row = _row(user_id)
    used = usage_on(user_id)
    return {"paid": is_paid(row), "paid_until": row["paid_until"] if row else None,
            "daily_images": row["paid_daily_images"] if row else 0, "daily_videos": row["paid_daily_videos"] if row else 0,
            "images_today": used["image"], "videos_today": used["video"]}


def job_kind_units(job: dict) -> tuple[str, int]:
    if job.get("runpod_tier") == "video":
        return "video", 1
    try:
        units = int((job.get("options") or {}).get("seed_count") or 1)
    except (TypeError, ValueError):
        units = 1
    return "image", max(1, min(units, 100))


def reserve(user_id, job_id: str, kind: str, units: int) -> bool:
    """유료 기간 안이고 오늘 한도가 남았으면 그 작업을 관리자 파드로 기록하고 True. 한도 0은 무제한."""
    row = _row(user_id)
    if not is_paid(row):
        return False
    limit = row["paid_daily_images"] if kind == "image" else row["paid_daily_videos"]
    day = today()
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM paid_usage WHERE job_id=?", (job_id,)).fetchone():
            return True
        used = conn.execute("SELECT COALESCE(SUM(units), 0) FROM paid_usage WHERE user_id=? AND day=? AND kind=?",
                            (user_id, day, kind)).fetchone()[0]
        if limit and used + units > limit:
            return False
        conn.execute("INSERT INTO paid_usage(user_id, job_id, kind, units, day, created_at) VALUES(?,?,?,?,?,?)",
                     (user_id, job_id, kind, units, day, db.now_iso()))
    return True


def release(job_id: str) -> None:
    """관리자 파드로 돌기 전에 지운 작업의 기록을 되돌린다(한도를 돌려준다)."""
    with db.connect() as conn:
        conn.execute("DELETE FROM paid_usage WHERE job_id=?", (job_id,))


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def set_plan(user_id: int, body: dict) -> dict:
    """관리자 설정: paid_until('YYYY-MM-DD' 또는 null=해제), daily_images·daily_videos(0=무제한)."""
    row = _row(user_id)
    if row is None:
        raise PlanError("없는 회원이에요.", 404)
    if row["role"] == auth.ROLE_ADMIN:
        raise PlanError("관리자는 유료 설정이 필요 없어요.")
    updates = {}
    if "paid_until" in body:
        value = body["paid_until"]
        if value in (None, ""):
            updates["paid_until"] = None
        else:
            if not isinstance(value, str) or not DATE_RE.match(value):
                raise PlanError("유료 기간은 YYYY-MM-DD 날짜로 넣어 주세요.")
            try:
                date.fromisoformat(value)
            except ValueError:
                raise PlanError("없는 날짜예요.") from None
            updates["paid_until"] = value
    for key, column in (("daily_images", "paid_daily_images"), ("daily_videos", "paid_daily_videos")):
        if key in body:
            n = body[key]
            if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= DAILY_LIMIT_MAX:
                raise PlanError(f"하루 한도는 0~{DAILY_LIMIT_MAX} 사이 정수로 넣어 주세요(0은 무제한).")
            updates[column] = n
    if updates:
        with db.connect() as conn:
            conn.execute(f"UPDATE users SET {', '.join(f'{k}=?' for k in updates)} WHERE id=?", (*updates.values(), user_id))
    return plan(user_id)


def month_usage() -> dict[int, dict]:
    """관리자 회원 목록용 — 이번 달 회원별 관리자 파드 사용량(이미지 장수·영상 개수)."""
    month = today()[:7]
    with db.read() as conn:
        rows = conn.execute("SELECT user_id, kind, SUM(units) AS n FROM paid_usage WHERE substr(day, 1, 7)=? GROUP BY user_id, kind",
                            (month,)).fetchall()
    out: dict[int, dict] = {}
    for r in rows:
        out.setdefault(r["user_id"], {"image": 0, "video": 0})[r["kind"]] = r["n"] or 0
    return out
