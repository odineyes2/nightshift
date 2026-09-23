"""
회원·세션·권한 — 아이디/이메일/비밀번호 가입, 관리자 승인, 세션 쿠키 로그인.

## 모델

- 회원은 role(`admin`/`user`)과 status(`pending`/`active`/`disabled`)를 가진다. 가입하면 항상
  `user` + `pending`이고, 관리자가 승인(`active`)해야 로그인해서 쓸 수 있다. admin 계정은 가입 화면으로는
  만들어지지 않고, 서버가 시작할 때 환경변수(NIGHTSHIFT_ADMIN_USER/PASSWORD)로 만든다(ensure_admin).
- 로그인하면 무작위 세션 토큰을 HttpOnly 쿠키로 준다. DB에는 토큰의 sha256만 저장한다(DB가 새도 토큰은 못 쓴다).
- 비밀번호는 scrypt(솔트 포함)로 해시해서 저장한다.
- 로그인 시도는 (IP, 아이디) 단위로 제한한다 — 인터넷에 공개된 서버라 무차별 대입을 막아야 한다.

## 소유자

프로젝트·작업·결과물·파드에는 owner_id가 붙고, 일반 회원은 자기 것만, admin은 전부 본다. owner_id가 NULL이면
"주인 없음"으로 admin만 본다(ensure_admin이 시작할 때 admin에게 넘겨 준다).

환경변수:
    NIGHTSHIFT_ADMIN_USER      admin 아이디 (기본 admin)
    NIGHTSHIFT_ADMIN_PASSWORD  admin 비밀번호 (admin이 아직 없을 때 이 값으로 만든다 — 없으면 만들지 않는다)
    NIGHTSHIFT_ADMIN_EMAIL     admin 이메일 (기본 <아이디>@localhost)
"""

import contextvars
import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

import db

log = logging.getLogger("uvicorn.error")

SESSION_COOKIE = "ns_session"
SESSION_DAYS = 30

ROLE_ADMIN, ROLE_USER = "admin", "user"
STATUS_PENDING, STATUS_ACTIVE, STATUS_DISABLED = "pending", "active", "disabled"

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,31}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s.]{2,}$")
RESERVED_NAMES = {"admin", "administrator", "root", "system", "nightshift", "support"}
MIN_PASSWORD, MAX_PASSWORD = 8, 128

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1

# 요청 하나 동안 "지금 누구인가" — 미들웨어가 채우고, 서버 곳곳이 읽는다.
current_user: contextvars.ContextVar[dict | None] = contextvars.ContextVar("current_user", default=None)


class AuthError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---- 비밀번호 ------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                            dklen=32, maxmem=64 * 1024 * 1024)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p),
                                dklen=len(digest_hex) // 2, maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


_DUMMY_HASH = hash_password(secrets.token_urlsafe(12))   # 없는 아이디도 같은 시간이 걸리게(계정 존재 여부 노출 방지)


def validate_password(password, username: str = "") -> str:
    if not isinstance(password, str) or not (MIN_PASSWORD <= len(password) <= MAX_PASSWORD):
        raise AuthError(f"비밀번호는 {MIN_PASSWORD}자 이상 {MAX_PASSWORD}자 이하여야 해요.")
    if username and password.lower() == username.lower():
        raise AuthError("비밀번호가 아이디와 같으면 안 돼요.")
    return password


# ---- 사용자 조회 -----------------------------------------------------------------------

def _public(row) -> dict:
    return {k: row[k] for k in ("id", "username", "email", "role", "status", "created_at", "approved_at", "last_login_at")}


def is_admin(user: dict | None) -> bool:
    return bool(user) and user.get("role") == ROLE_ADMIN


def owner_scope(user: dict | None):
    """목록을 거를 때 쓰는 값 — admin은 None(거르지 않음), 일반 회원은 자기 id."""
    if user is None:
        raise AuthError("로그인이 필요해요.", 401)
    return None if is_admin(user) else user["id"]


def can_access(user: dict | None, owner_id) -> bool:
    """이 소유자의 것을 이 회원이 볼 수 있나 — admin은 전부, 일반 회원은 자기 것만(주인 없음은 admin만)."""
    if user is None:
        return False
    return is_admin(user) or (owner_id is not None and owner_id == user["id"])


def admin_id() -> int | None:
    """첫 번째 admin 계정의 id — 주인을 정할 수 없는 결과물의 마지막 보루."""
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
    return row["id"] if row else None


def get_user(user_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return _public(row) if row else None


def usernames() -> dict[int, str]:
    with db.connect() as conn:
        return {r["id"]: r["username"] for r in conn.execute("SELECT id, username FROM users")}


def list_users() -> list[dict]:
    """관리 대시보드용 — 회원마다 프로젝트/작업/결과물 개수와 결과물 용량을 붙인다."""
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY (role='admin') DESC, "
                            "CASE status WHEN 'pending' THEN 0 WHEN 'active' THEN 1 ELSE 2 END, created_at DESC").fetchall()
        projects = {r["owner_id"]: r["n"] for r in conn.execute(
            "SELECT owner_id, COUNT(*) AS n FROM projects WHERE owner_id IS NOT NULL GROUP BY owner_id")}
        jobs = {r["owner_id"]: r["n"] for r in conn.execute(
            "SELECT owner_id, COUNT(*) AS n FROM jobs WHERE owner_id IS NOT NULL AND deleted_at IS NULL GROUP BY owner_id")}
        assets = {r["owner_id"]: (r["n"], r["bytes"] or 0) for r in conn.execute(
            "SELECT owner_id, COUNT(*) AS n, SUM(size_bytes) AS bytes FROM assets "
            "WHERE owner_id IS NOT NULL AND deleted_at IS NULL GROUP BY owner_id")}
    out = []
    for r in rows:
        u = _public(r)
        n_assets, size = assets.get(r["id"], (0, 0))
        u.update(project_count=projects.get(r["id"], 0), job_count=jobs.get(r["id"], 0),
                 asset_count=n_assets, asset_bytes=size)
        out.append(u)
    return out


# ---- 가입 / 로그인 ---------------------------------------------------------------------

def register(username, email, password) -> dict:
    username = str(username or "").strip()
    email = str(email or "").strip()
    if not USERNAME_RE.match(username):
        raise AuthError("아이디는 영문·숫자로 시작하는 3~32자(영문, 숫자, _ . - 사용 가능)여야 해요.")
    if username.lower() in RESERVED_NAMES:
        raise AuthError("사용할 수 없는 아이디예요.")
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise AuthError("이메일 형식이 올바르지 않아요.")
    validate_password(password, username)
    password_hash = hash_password(password)
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise AuthError("이미 사용 중인 아이디예요.", 409)
        if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            raise AuthError("이미 가입된 이메일이에요.", 409)
        cur = conn.execute(
            "INSERT INTO users(username, email, password_hash, role, status, created_at) VALUES(?,?,?,?,?,?)",
            (username, email, password_hash, ROLE_USER, STATUS_PENDING, db.now_iso()))
        row = conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
    return _public(row)


_throttle_lock = threading.Lock()
_fails: dict[str, list[float]] = {}
FAIL_WINDOW_SEC, FAIL_LIMIT_USER, FAIL_LIMIT_IP = 600, 5, 30


def _recent(key: str, now: float) -> list[float]:
    recent = [t for t in _fails.get(key, []) if now - t < FAIL_WINDOW_SEC]
    if recent:
        _fails[key] = recent
    else:
        _fails.pop(key, None)
    return recent


def _check_throttle(ip: str, username: str) -> None:
    now = time.time()
    with _throttle_lock:
        by_user = _recent(f"u|{ip}|{username.lower()}", now)
        by_ip = _recent(f"i|{ip}", now)
    if len(by_user) >= FAIL_LIMIT_USER or len(by_ip) >= FAIL_LIMIT_IP:
        oldest = min(by_user or by_ip)
        wait = int(FAIL_WINDOW_SEC - (now - oldest)) + 1
        raise AuthError(f"로그인 시도가 너무 많아요. {max(1, wait // 60)}분 뒤에 다시 시도하세요.", 429)


def _record_fail(ip: str, username: str) -> None:
    now = time.time()
    with _throttle_lock:
        for key in (f"u|{ip}|{username.lower()}", f"i|{ip}"):
            _fails.setdefault(key, []).append(now)


def _clear_fails(ip: str, username: str) -> None:
    with _throttle_lock:
        _fails.pop(f"u|{ip}|{username.lower()}", None)


def authenticate(login, password, ip: str = "") -> dict:
    """아이디(또는 이메일)와 비밀번호를 확인해 회원을 돌려준다. 실패는 AuthError."""
    login = str(login or "").strip()
    if not login or not isinstance(password, str) or not password:
        raise AuthError("아이디와 비밀번호를 입력하세요.")
    _check_throttle(ip, login)
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=? OR email=?", (login, login)).fetchone()
    stored = row["password_hash"] if row else _DUMMY_HASH
    ok = verify_password(password, stored) and row is not None
    if not ok:
        _record_fail(ip, login)
        raise AuthError("아이디 또는 비밀번호가 올바르지 않아요.", 401)
    if row["status"] == STATUS_PENDING:
        raise AuthError("관리자 승인을 기다리는 중이에요. 승인되면 로그인할 수 있어요.", 403)
    if row["status"] == STATUS_DISABLED:
        raise AuthError("정지된 계정이에요. 관리자에게 문의하세요.", 403)
    _clear_fails(ip, login)
    with db.connect() as conn:
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (db.now_iso(), row["id"]))
    return _public(row)


# ---- 세션 ------------------------------------------------------------------------------

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id: int, ip: str = "", user_agent: str = "") -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(),))
        conn.execute(
            "INSERT INTO sessions(token_hash, user_id, created_at, expires_at, last_seen_at, ip, user_agent) "
            "VALUES(?,?,?,?,?,?,?)",
            (_hash_token(token), user_id, now.isoformat(), (now + timedelta(days=SESSION_DAYS)).isoformat(),
             now.isoformat(), ip[:64], user_agent[:200]))
    return token


_touch_cache: dict[str, float] = {}


def user_for_token(token: str | None) -> dict | None:
    """세션 토큰의 주인(승인된 회원)을 돌려준다. 없거나 만료됐거나 정지된 회원이면 None."""
    if not token:
        return None
    token_hash = _hash_token(token)
    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT u.*, s.expires_at AS s_expires FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash=?", (token_hash,)).fetchone()
        if row is None:
            return None
        if row["s_expires"] < now.isoformat() or row["status"] != STATUS_ACTIVE:
            return None
        if time.time() - _touch_cache.get(token_hash, 0) > 60:   # 조회마다 쓰지 않는다
            conn.execute("UPDATE sessions SET last_seen_at=? WHERE token_hash=?", (now.isoformat(), token_hash))
            _touch_cache[token_hash] = time.time()
    return _public(row)


def delete_session(token: str | None) -> None:
    if not token:
        return
    with db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))


def delete_user_sessions(user_id: int) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


# ---- 관리 ------------------------------------------------------------------------------

def set_status(user_id: int, status: str, acting_admin_id: int) -> dict:
    if status not in (STATUS_ACTIVE, STATUS_DISABLED):
        raise AuthError("status는 active 또는 disabled여야 해요.")
    target = get_user(user_id)
    if target is None:
        raise AuthError("없는 회원이에요.", 404)
    if target["role"] == ROLE_ADMIN:
        raise AuthError("관리자 계정은 바꿀 수 없어요.", 400)
    with db.connect() as conn:
        if status == STATUS_ACTIVE:
            conn.execute("UPDATE users SET status='active', approved_at=COALESCE(approved_at, ?) WHERE id=?",
                         (db.now_iso(), user_id))
        else:
            conn.execute("UPDATE users SET status='disabled' WHERE id=?", (user_id,))
    if status == STATUS_DISABLED:
        delete_user_sessions(user_id)
    return get_user(user_id)


def reset_password(user_id: int, new_password) -> None:
    target = get_user(user_id)
    if target is None:
        raise AuthError("없는 회원이에요.", 404)
    validate_password(new_password, target["username"])
    with db.connect() as conn:
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(new_password), user_id))
    delete_user_sessions(user_id)


def change_password(user_id: int, old_password, new_password, keep_token: str | None = None) -> None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None or not isinstance(old_password, str) or not verify_password(old_password, row["password_hash"]):
        raise AuthError("현재 비밀번호가 올바르지 않아요.", 400)
    validate_password(new_password, row["username"])
    with db.connect() as conn:
        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(new_password), user_id))
        # 다른 기기의 세션은 끊고 지금 세션만 남긴다.
        if keep_token:
            conn.execute("DELETE FROM sessions WHERE user_id=? AND token_hash != ?", (user_id, _hash_token(keep_token)))
        else:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


SECRET_FIELDS = ("civitai_token", "runpod_api_key")
MAX_SECRET_LEN = 500


def get_secrets(user_id: int) -> dict:
    """회원 본인의 API 키(civitai_token/runpod_api_key) — _public()이 일반 조회에는 안 섞는 값이라
    계정 관리 모달이 본인 것을 볼 때만 따로 부른다."""
    with db.connect() as conn:
        row = conn.execute("SELECT civitai_token, runpod_api_key FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        raise AuthError("없는 회원이에요.", 404)
    return {k: row[k] for k in SECRET_FIELDS}


def set_secrets(user_id: int, fields: dict) -> dict:
    updates = {}
    for key in SECRET_FIELDS:
        if key not in fields:
            continue
        value = str(fields[key] or "").strip()
        if len(value) > MAX_SECRET_LEN:
            raise AuthError(f"{key}이(가) 너무 길어요(최대 {MAX_SECRET_LEN}자).")
        updates[key] = value
    if updates:
        with db.connect() as conn:
            set_clause = ", ".join(f"{k}=?" for k in updates)
            conn.execute(f"UPDATE users SET {set_clause} WHERE id=?", (*updates.values(), user_id))
    return get_secrets(user_id)


def delete_user(user_id: int) -> None:
    """회원을 지운다. 그 사람의 프로젝트·작업·결과물은 지우지 않고 주인 없음(=관리자 것)이 된다."""
    target = get_user(user_id)
    if target is None:
        raise AuthError("없는 회원이에요.", 404)
    if target["role"] == ROLE_ADMIN:
        raise AuthError("관리자 계정은 지울 수 없어요.", 400)
    with db.connect() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))


def ensure_admin() -> dict | None:
    """admin이 없으면 환경변수로 만들고, 주인 없는 데이터(예전 단일 사용자 시절 것)를 admin에게 넘긴다."""
    username = (os.environ.get("NIGHTSHIFT_ADMIN_USER") or "admin").strip()
    password = os.environ.get("NIGHTSHIFT_ADMIN_PASSWORD") or ""
    email = (os.environ.get("NIGHTSHIFT_ADMIN_EMAIL") or f"{username}@localhost").strip()
    with db.connect() as conn:
        admin = conn.execute("SELECT * FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
        if admin is None:
            if not password:
                log.error("admin 계정이 없고 NIGHTSHIFT_ADMIN_PASSWORD도 설정돼 있지 않아요 — 아무도 로그인할 수 없습니다. "
                          ".env에 NIGHTSHIFT_ADMIN_USER / NIGHTSHIFT_ADMIN_PASSWORD를 설정하고 다시 시작하세요.")
                return None
            if len(password) < MIN_PASSWORD:
                log.error("NIGHTSHIFT_ADMIN_PASSWORD는 %d자 이상이어야 해요.", MIN_PASSWORD)
                return None
            if conn.execute("SELECT 1 FROM users WHERE username=? OR email=?", (username, email)).fetchone():
                log.error("admin으로 쓸 아이디/이메일이 이미 다른 회원에게 쓰이고 있어요: %s", username)
                return None
            now = db.now_iso()
            cur = conn.execute(
                "INSERT INTO users(username, email, password_hash, role, status, created_at, approved_at) "
                "VALUES(?,?,?,?,?,?,?)", (username, email, hash_password(password), ROLE_ADMIN, STATUS_ACTIVE, now, now))
            admin = conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
            log.warning("admin 계정 '%s'을(를) 만들었어요.", username)
        for table in ("projects", "jobs", "assets"):
            conn.execute(f"UPDATE {table} SET owner_id=? WHERE owner_id IS NULL", (admin["id"],))
    return _public(admin)


_reg_lock = threading.Lock()
_reg_times: dict[str, list[float]] = {}
REGISTER_LIMIT, REGISTER_WINDOW_SEC = 5, 3600


def check_register_throttle(ip: str) -> None:
    """같은 IP에서 가입 신청을 너무 자주 못 하게 한다(스팸 방지) — 시간당 5건."""
    now = time.time()
    with _reg_lock:
        recent = [t for t in _reg_times.get(ip, []) if now - t < REGISTER_WINDOW_SEC]
        if len(recent) >= REGISTER_LIMIT:
            raise AuthError("가입 신청이 너무 잦아요. 잠시 후 다시 시도하세요.", 429)
        recent.append(now)
        _reg_times[ip] = recent
