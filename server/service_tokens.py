"""일반 회원용 서비스 토큰을 해시로 저장하고 발급·폐기·인증한다."""

import hashlib
import re
import secrets
import time

import auth
import db


class ServiceTokenError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


_touch_cache: dict[str, float] = {}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public(row) -> dict:
    return {key: row[key] for key in
            ("id", "user_id", "name", "last4", "created_at", "revoked_at", "last_used_at")}


def issue(user_id: int, name: str) -> dict:
    name = name.strip() if isinstance(name, str) else ""
    if not 1 <= len(name) <= 80:
        raise ServiceTokenError("토큰 이름은 1~80자로 입력해 주세요.")
    token = "nst_" + secrets.token_urlsafe(32)
    with db.connect() as conn:
        user = conn.execute("SELECT role, status FROM users WHERE id=?", (user_id,)).fetchone()
        if user is None:
            raise ServiceTokenError("없는 회원이에요.", 404)
        if user["role"] != auth.ROLE_USER or user["status"] != auth.STATUS_ACTIVE:
            raise ServiceTokenError("활성 일반 회원에게만 발급할 수 있어요.", 403)
        cur = conn.execute(
            "INSERT INTO service_tokens(user_id, name, token_hash, last4, created_at) VALUES(?,?,?,?,?)",
            (user_id, name, _hash(token), token[-4:], db.now_iso()))
        row = conn.execute("SELECT * FROM service_tokens WHERE id=?", (cur.lastrowid,)).fetchone()
    return {**_public(row), "token": token}


# 외부 서비스 연동: 서비스마다 전용 일반 회원(<서비스>-bot) 하나와 그 서비스 이름의 토큰 하나를 둔다.
SERVICE_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")


def _service_names(service) -> tuple[str, str]:
    if not isinstance(service, str) or not SERVICE_RE.match(service):
        raise ServiceTokenError("서비스 이름이 올바르지 않아요.")
    return f"{service}-bot", f"{service} 연동"


def connect(service) -> dict:
    """전용 계정을 준비하고(없으면 로그인할 수 없는 계정으로 만든다) 기존 연동 토큰을 폐기한 뒤 새로 발급한다."""
    username, token_name = _service_names(service)
    now = db.now_iso()
    with db.connect() as conn:
        user = conn.execute("SELECT id, role, status FROM users WHERE username=?", (username,)).fetchone()
        if user is None:
            # 비밀번호는 아무도 모르는 무작위 값이라 이 계정으로는 로그인할 수 없다.
            cur = conn.execute(
                "INSERT INTO users(username, email, password_hash, role, status, created_at) VALUES(?,?,?,?,?,?)",
                (username, f"{username}@service.invalid", auth.hash_password(secrets.token_urlsafe(48)),
                 auth.ROLE_USER, auth.STATUS_ACTIVE, now))
            user_id = cur.lastrowid
        elif user["role"] != auth.ROLE_USER or user["status"] != auth.STATUS_ACTIVE:
            raise ServiceTokenError(f"'{username}' 계정이 활성 일반 회원이 아니에요.", 409)
        else:
            user_id = user["id"]
        # 전용 계정이라 이전 연동 토큰뿐 아니라 손으로 발급한 토큰까지 모두 폐기하고 하나만 남긴다.
        conn.execute("UPDATE service_tokens SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now, user_id))
    return {**issue(user_id, token_name), "username": username}


def is_service_user(user_id) -> bool:
    """connect()가 만든 서비스 연동 전용 계정(<서비스>-bot, 로그인 불가 메일 주소)인가."""
    if user_id is None:
        return False
    with db.connect() as conn:
        row = conn.execute("SELECT username, email, role FROM users WHERE id=?", (user_id,)).fetchone()
    return bool(row and row["role"] == auth.ROLE_USER and row["username"].endswith("-bot")
                and (row["email"] or "") == f"{row['username']}@service.invalid")


ON_BEHALF_HEADER = "x-on-behalf-of"


def acting_user(service_user: dict, raw) -> dict | None:
    """서비스 연동 계정이 X-On-Behalf-Of: <회원 id>로 그 회원 대신 보낸 요청이면 그 회원(아니면 None).
    charchat처럼 nightshift 계정을 함께 쓰는 앱이 회원마다 결과물을 그 회원 소유로 남기려고 쓴다.
    - 연동 전용 계정(connect가 만든 *-bot)의 토큰만, 대상은 활성 회원만(다른 연동 계정은 안 됨).
    - 관리자 대상이어도 관리자 권한은 싣지 않는다(role=user) — 토큰 경로에서 남의 것을 볼 수 없게."""
    if not isinstance(raw, str) or not raw.isdigit() or len(raw) > 12:
        return None
    if not is_service_user(service_user.get("id")):
        return None
    target_id = int(raw)
    if target_id == service_user.get("id") or is_service_user(target_id):
        return None
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (target_id,)).fetchone()
    if row is None or row["status"] != auth.STATUS_ACTIVE:
        return None
    return {**auth._public(row), "role": auth.ROLE_USER}


def service_name(user_id) -> str | None:
    """연동 계정 id → 서비스 이름(charchat-bot → charchat)."""
    with db.connect() as conn:
        row = conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
    return row["username"].removesuffix("-bot") if row else None


def pod_owner_ids_for(job: dict) -> list:
    """이 작업을 맡을 수 있는 파드 주인들. 연동 앱이 회원 대신 보낸 작업(via_service)도 그 회원의 파드에서만 돈다
    (2026-11-15 사용자 결정: 각자 자기 RunPod 계정으로 — 회원 워커 자동 켜기는 07d). 연동 계정 자신의 작업만 관리자 파드."""
    return pod_owner_ids_for_job(job.get("owner_id"))


def pod_owner_ids_for_job(owner_id, via_service=None) -> list:
    """이 회원의 작업을 맡을 수 있는 파드 주인들. 연동 계정(*-bot)은 파드가 없으므로 그 작업은 관리자 파드에서 돈다.
    회원은 자기 파드만(via_service는 예전 호출 호환용으로 받기만 하고 쓰지 않는다)."""
    if not is_service_user(owner_id):
        return [owner_id]
    with db.connect() as conn:
        admins = [row["id"] for row in conn.execute(
            "SELECT id FROM users WHERE role=? AND status=? ORDER BY id", (auth.ROLE_ADMIN, auth.STATUS_ACTIVE))]
    return [owner_id, *[a for a in admins if a != owner_id]]


def disconnect(service) -> dict:
    """그 서비스 전용 계정의 토큰을 모두 폐기한다. 계정은 남긴다(작업·결과물의 소유자라서)."""
    username, _ = _service_names(service)
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE service_tokens SET revoked_at=? WHERE revoked_at IS NULL "
            "AND user_id=(SELECT id FROM users WHERE username=?)", (db.now_iso(), username))
    return {"revoked": cur.rowcount}


def list_tokens() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM service_tokens ORDER BY created_at DESC, id DESC").fetchall()
    return [_public(row) for row in rows]


def revoke(token_id: int) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM service_tokens WHERE id=?", (token_id,)).fetchone()
        if row is None:
            raise ServiceTokenError("없는 토큰이에요.", 404)
        if row["revoked_at"] is None:
            conn.execute("UPDATE service_tokens SET revoked_at=? WHERE id=?", (db.now_iso(), token_id))
        row = conn.execute("SELECT * FROM service_tokens WHERE id=?", (token_id,)).fetchone()
    return _public(row)


def user_for_token(token: str) -> dict | None:
    if not token.startswith("nst_") or len(token) > 100:
        return None
    token_hash = _hash(token)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT u.*, t.revoked_at FROM service_tokens t JOIN users u ON u.id=t.user_id "
            "WHERE t.token_hash=?", (token_hash,)).fetchone()
        if row is None or row["revoked_at"] is not None or row["status"] != auth.STATUS_ACTIVE:
            return None
        # DB가 바뀌어도 admin 권한은 토큰에 실리지 않는다.
        if row["role"] != auth.ROLE_USER:
            return None
        now = time.monotonic()
        if now - _touch_cache.get(token_hash, 0) >= 60:
            conn.execute("UPDATE service_tokens SET last_used_at=? WHERE token_hash=?", (db.now_iso(), token_hash))
            _touch_cache[token_hash] = now
    return auth._public(row)
