"""
SQLite 저장소 — 프로젝트/작업(job)/결과물(asset)/태그.

## 역할

- jobs_state.json(작업 기록)을 대신한다. app.py는 여전히 메모리의 `jobs` dict를 쓰고,
  `save_state()`/`load_state()`만 이 모듈로 위임한다(메모리 구조와 나머지 코드는 그대로).
- 결과 파일(이미지/영상) 자체는 NIGHTSHIFT_OUTPUT_DIR에 그대로 두고, 여기에는 색인만
  둔다(assets). 파일이 진실이고 DB는 그걸 가리키는 색인이라, 손으로 넣은 파일도
  assets_index.sync()가 찾아 등록한다.

## 스키마 결정

- jobs는 job dict 전체를 data_json에 그대로 담고(어떤 필드도 잃지 않는다), 조회에 쓰는
  것만 컬럼으로 뽑아 둔다(project_id, status, 파드 스냅샷 등). 읽을 때는 data_json만 본다.
- 프로젝트는 파드와 무관하게 가로지른다. 파드는 일시적이라 FK가 아니라 job에 찍어두는
  스냅샷(pod_name/pod_kind/pod_gpu/pod_cost_per_hr)이다.
- project_id가 NULL이면 "미분류"다(프로젝트 행이 아니다).

## 동시성

쓰는 프로세스는 app.py 하나다(MCP 서버는 REST를 거친다). WAL + busy_timeout을 걸고,
호출마다 짧게 연결을 열었다 닫는다(스레드에서 불러도 안전).

환경변수:
    NIGHTSHIFT_DB_PATH  DB 파일 (기본 <data>/nightshift.db)
"""

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from data_paths import data_path

DB_PATH = Path(os.environ.get("NIGHTSHIFT_DB_PATH") or data_path("nightshift.db"))

_init_lock = threading.Lock()
_initialized = False

SCHEMA_V1 = """
CREATE TABLE projects (
  id             INTEGER PRIMARY KEY,
  name           TEXT NOT NULL,
  description    TEXT NOT NULL DEFAULT '',
  defaults_json  TEXT NOT NULL DEFAULT '{}',
  cover_asset_id INTEGER,
  archived_at    TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);

CREATE TABLE jobs (
  id             TEXT PRIMARY KEY,
  project_id     INTEGER REFERENCES projects(id) ON DELETE SET NULL,
  template_id    TEXT,
  template_label TEXT,
  status         TEXT,
  queued_at      TEXT,
  started_at     TEXT,
  finished_at    TEXT,
  deleted_at     TEXT,
  pod_id         TEXT,
  pod_name       TEXT,
  pod_kind       TEXT,
  pod_gpu        TEXT,
  pod_cost_per_hr REAL,
  data_json      TEXT NOT NULL
);
CREATE INDEX jobs_project ON jobs(project_id, queued_at DESC);

CREATE TABLE assets (
  id             INTEGER PRIMARY KEY,
  path           TEXT NOT NULL UNIQUE,
  kind           TEXT NOT NULL CHECK (kind IN ('image','video')),
  project_id     INTEGER REFERENCES projects(id) ON DELETE SET NULL,
  job_id         TEXT REFERENCES jobs(id) ON DELETE SET NULL,
  size_bytes     INTEGER,
  mtime_ns       INTEGER,
  width          INTEGER,
  height         INTEGER,
  duration_ms    INTEGER,
  created_at     TEXT NOT NULL,
  sha256         TEXT,
  seed           INTEGER,
  prompt         TEXT,
  negative_prompt TEXT,
  checkpoint     TEXT,
  params_json    TEXT,
  favorite       INTEGER NOT NULL DEFAULT 0,
  rating         INTEGER,
  note           TEXT NOT NULL DEFAULT '',
  origin_pod_id  TEXT,
  origin_key     TEXT,
  deleted_at     TEXT
);
CREATE INDEX assets_project ON assets(project_id, created_at DESC);
CREATE INDEX assets_job ON assets(job_id);

CREATE TABLE tags (
  id   INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE asset_tags (
  asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  tag_id   INTEGER NOT NULL REFERENCES tags(id)   ON DELETE CASCADE,
  PRIMARY KEY (asset_id, tag_id)
);
CREATE INDEX asset_tags_tag ON asset_tags(tag_id);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# v2: 원격 ComfyUI에서 "이미 받아온 파일" 기록을 comfy_output_sync.json에서 옮겨 온다. 이 기록은
# 한 번 받은 파일을 사용자가 지워도 다시 받지 않게 하는 장치이자(comfy_outputs.py), 갤러리가
# job_id 없는 파일의 파드를 알아보는 단서다. assets.origin_key는 같은 목적으로 미리 만들어 둔
# 칸이었지만 결과물 행이 아직 없는 파일(받기만 하고 색인 전에 지운 것)도 기록해야 해서 이
# 테이블이 맡고, 쓰이지 않는 그 칸은 지운다.
SCHEMA_V2 = """
CREATE TABLE comfy_downloads (
  key           TEXT PRIMARY KEY,
  downloaded_at TEXT NOT NULL,
  pod_id        TEXT
);
ALTER TABLE assets DROP COLUMN origin_key
"""

# v3: 회원. users/sessions를 만들고, 프로젝트·작업·결과물에 소유자(owner_id)를 단다.
# owner_id가 NULL이면 "주인 없음"이다 — 관리자만 볼 수 있고, 시작할 때 관리자에게 넘겨진다(auth.ensure_admin).
# 회원을 지워도 그 사람의 데이터는 지우지 않고 owner_id를 NULL로 되돌린다(ON DELETE SET NULL).
SCHEMA_V3 = """
CREATE TABLE users (
  id            INTEGER PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
  email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin','user')),
  status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','active','disabled')),
  created_at    TEXT NOT NULL,
  approved_at   TEXT,
  last_login_at TEXT
);
CREATE TABLE sessions (
  token_hash   TEXT PRIMARY KEY,
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at   TEXT NOT NULL,
  expires_at   TEXT NOT NULL,
  last_seen_at TEXT,
  ip           TEXT,
  user_agent   TEXT
);
CREATE INDEX sessions_user ON sessions(user_id);
ALTER TABLE projects ADD COLUMN owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE jobs ADD COLUMN owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE assets ADD COLUMN owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
CREATE INDEX projects_owner ON projects(owner_id);
CREATE INDEX jobs_owner ON jobs(owner_id);
CREATE INDEX assets_owner ON assets(owner_id, created_at DESC)
"""

# v4: 모델 등록부. ComfyUI가 알려 주는 "설치된 파일 목록"에는 이름밖에 없어서, 사람이 붙이는
# 정보(계열/메모/태그/트리거 워드/호환 베이스 모델/출처)를 (종류, 파일명)별로 따로 둔다. 파일이
# 어느 파드에 있는지와 무관하게 파일명이 열쇠라, 같은 모델을 가진 여러 파드가 한 항목을 공유한다.
SCHEMA_V4 = """
CREATE TABLE models (
  kind         TEXT NOT NULL,
  filename     TEXT NOT NULL,
  architecture TEXT NOT NULL DEFAULT '',
  notes        TEXT NOT NULL DEFAULT '',
  tags_json    TEXT NOT NULL DEFAULT '[]',
  trigger      TEXT NOT NULL DEFAULT '',
  families_json TEXT NOT NULL DEFAULT '[]',
  source_url   TEXT NOT NULL DEFAULT '',
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (kind, filename)
)
"""

# v5: 등록부 필드를 "파드와 무관한 기준 데이터"에 맞게 이름을 바로잡고 받는 주소를 더한다.
# architecture -> base_model(어느 베이스 모델용/계열), trigger -> trigger_keyword,
# source_url -> page_url(모델 소개 페이지), download_url(파일을 받을 주소 — Civitai/Hugging Face 등).
SCHEMA_V5 = """
ALTER TABLE models RENAME COLUMN architecture TO base_model;
ALTER TABLE models RENAME COLUMN trigger TO trigger_keyword;
ALTER TABLE models RENAME COLUMN source_url TO page_url;
ALTER TABLE models ADD COLUMN download_url TEXT NOT NULL DEFAULT ''
"""

# v6: LoRA "호환 베이스 모델 그룹"(families)을 없앤다. 마법사의 베이스 모델은 이제 등록부의 base_model 값으로 묶고,
# LoRA도 자기 base_model이 같은 것만 보여 준다 — 같은 정보를 두 곳(그룹 파일과 등록부)에 적을 이유가 없다.
SCHEMA_V6 = """
ALTER TABLE models DROP COLUMN families_json
"""

# v7: NSFW(성인) 콘텐츠 표시/숨기기. 프로젝트에 "성인 콘텐츠 포함" 체크박스를 달고, 그 프로젝트에
# 속한 결과물은 만들어지거나 그 프로젝트로 옮겨질 때 assets.nsfw를 따라 찍는다(결과물 자체에도
# 컬럼을 두는 이유는 프로젝트 없는 결과물도 값을 가질 수 있게, 그리고 갤러리 필터가 매번 프로젝트
# 테이블과 조인하지 않고 바로 걸러내게 하려는 것) — server/projects.py의 update_project,
# server/asset_meta.py의 move_assets, server/assets_index.py의 sync가 이 값을 맞춘다.
SCHEMA_V7 = """
ALTER TABLE projects ADD COLUMN is_mature INTEGER NOT NULL DEFAULT 0;
ALTER TABLE assets ADD COLUMN nsfw INTEGER NOT NULL DEFAULT 0;
CREATE INDEX assets_nsfw ON assets(nsfw)
"""

# v8: 회원별 개인 API 키. Civitai/RunPod 다운로드 명령처럼 회원마다 다른 값을 써야 하는 곳에서
# 예전에는 서버 하나의 .env(CIVITAI_TOKEN 등)를 모두가 같이 썼는데, 회원마다 자기 키를 계정
# 관리 모달에서 직접 넣어 두게 한다. 이 값을 실제로 읽어 쓰는 코드는 아직 없다(저장만 해 둔다) —
# auth._public()이 의도적으로 안 돌려주는 값이라, 로그인 응답이나 회원 목록에는 안 보인다.
SCHEMA_V8 = """
ALTER TABLE users ADD COLUMN civitai_token TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN runpod_api_key TEXT NOT NULL DEFAULT ''
"""

# v9: RunPod 세션(사용 내역) 로그 — Claude가 RunPod 커넥터로 pod를 켜고 끌 때마다
# server/runpod_sessions.py가 이 표에 한 줄씩 남긴다(DB 탭에서 보여줌). 시작/종료
# 시각은 RunPod API 자체가 주는 실제 발생 시각(lastStartedAt/lastStatusChange)을
# 파싱한 것이라, nightshift가 sync를 늦게 불러도 정확하다 — server/runpod_sessions.py
# 모듈 docstring 참고. ended_at이 NULL이면 아직 진행 중인 세션(그 경우
# duration_sec/cost_total도 NULL — 화면이 그때그때 계산해서 보여준다).
SCHEMA_V9 = """
CREATE TABLE runpod_sessions (
  id             INTEGER PRIMARY KEY,
  runpod_pod_id  TEXT NOT NULL,
  pod_name       TEXT NOT NULL DEFAULT '',
  gpu_type       TEXT NOT NULL DEFAULT '',
  vram_gb        INTEGER,
  cost_per_hr    REAL,
  started_at     TEXT NOT NULL,
  ended_at       TEXT,
  duration_sec   INTEGER,
  cost_total     REAL
);
CREATE INDEX runpod_sessions_pod ON runpod_sessions(runpod_pod_id, started_at DESC)
"""

# 새 버전은 여기 끝에 (버전, SQL) 한 줄을 추가한다 — PRAGMA user_version이 현재 버전이다.
MIGRATIONS = [(1, SCHEMA_V1), (2, SCHEMA_V2), (3, SCHEMA_V3), (4, SCHEMA_V4), (5, SCHEMA_V5), (6, SCHEMA_V6),
              (7, SCHEMA_V7), (8, SCHEMA_V8), (9, SCHEMA_V9)]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _open() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init() -> None:
    """DB 파일과 스키마를 준비한다(여러 번 불러도 안전)."""
    global _initialized
    with _init_lock:
        if _initialized:
            return
        conn = _open()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            for target, sql in MIGRATIONS:
                if version >= target:
                    continue
                conn.execute("BEGIN")
                try:
                    for statement in _split_statements(sql):
                        conn.execute(statement)
                    conn.execute(f"PRAGMA user_version={target}")
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                version = target
        finally:
            conn.close()
        _initialized = True


def _split_statements(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]


@contextmanager
def connect():
    """짧게 쓰고 닫는 연결. `with connect() as conn:` 안의 변경은 하나의 트랜잭션이다."""
    init()
    conn = _open()
    try:
        conn.execute("BEGIN")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
    finally:
        conn.close()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


# ---- jobs (app.py의 메모리 jobs dict <-> DB) -----------------------------------------

_saved_json: dict[str, str] = {}   # job_id -> 마지막으로 저장한 data_json (바뀐 것만 쓰려고)


def _job_columns(job: dict, valid_project_ids: set[int]) -> tuple:
    project_id = job.get("project_id")
    if project_id not in valid_project_ids:
        project_id = None   # 지워진 프로젝트를 가리키면 미분류로 본다
    return (
        project_id,
        job.get("template_id"), job.get("template_label"), job.get("status"),
        job.get("queued_at"), job.get("started_at"), job.get("finished_at"),
        job.get("deleted_at") or ("deleted" if job.get("deleted") else None),
        job.get("pod_id"), job.get("pod_name"), job.get("pod_kind"),
        job.get("pod_gpu"), job.get("pod_cost_per_hr"),
        job.get("owner_id"),
    )


def save_jobs(jobs: dict[str, dict]) -> None:
    """메모리 jobs 전체를 DB에 맞춘다 — 바뀐 job만 upsert, 사라진 job은 삭제.
    (예전 save_state()가 jobs 전체를 JSON으로 덮어쓰던 것과 같은 의미다.)"""
    snapshot = {jid: json.dumps(job, ensure_ascii=False, default=str) for jid, job in jobs.items()}
    with connect() as conn:
        valid = {r["id"] for r in conn.execute("SELECT id FROM projects")}
        for jid, data in snapshot.items():
            if _saved_json.get(jid) == data:
                continue
            cols = _job_columns(jobs[jid], valid)
            conn.execute(
                """INSERT INTO jobs(id, project_id, template_id, template_label, status,
                       queued_at, started_at, finished_at, deleted_at,
                       pod_id, pod_name, pod_kind, pod_gpu, pod_cost_per_hr, owner_id, data_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       project_id=excluded.project_id, template_id=excluded.template_id,
                       template_label=excluded.template_label, status=excluded.status,
                       queued_at=excluded.queued_at, started_at=excluded.started_at,
                       finished_at=excluded.finished_at, deleted_at=excluded.deleted_at,
                       pod_id=excluded.pod_id, pod_name=excluded.pod_name, pod_kind=excluded.pod_kind,
                       pod_gpu=excluded.pod_gpu, pod_cost_per_hr=excluded.pod_cost_per_hr,
                       owner_id=excluded.owner_id, data_json=excluded.data_json""",
                (jid, *cols, data),
            )
        existing = {r["id"] for r in conn.execute("SELECT id FROM jobs")}
        for jid in existing - set(snapshot):
            conn.execute("DELETE FROM jobs WHERE id=?", (jid,))
    _saved_json.clear()
    _saved_json.update(snapshot)


def load_jobs() -> dict[str, dict]:
    with connect() as conn:
        rows = conn.execute("SELECT id, project_id, owner_id, data_json FROM jobs").fetchall()
    result = {}
    for r in rows:
        job = json.loads(r["data_json"])
        # 컬럼이 진실이다 — 프로젝트가 지워져 FK가 NULL이 된 job은 dict에서도 미분류로 본다.
        job["project_id"] = r["project_id"]
        job["owner_id"] = r["owner_id"]
        result[r["id"]] = job
    _saved_json.clear()
    _saved_json.update({jid: json.dumps(job, ensure_ascii=False, default=str) for jid, job in result.items()})
    return result


def import_legacy_jobs(state_file: Path) -> int:
    """jobs_state.json이 있고 아직 안 가져왔으면 한 번만 DB로 옮기고, 원본은
    .migrated로 이름을 바꿔 둔다(되돌릴 수 있게 지우지 않는다). 가져온 개수를 돌려준다."""
    with connect() as conn:
        if get_meta(conn, "legacy_jobs_imported"):
            return 0
        count = 0
        if state_file.exists():
            with open(state_file, encoding="utf-8") as f:
                legacy = json.load(f)
            valid: set[int] = set()
            for jid, job in legacy.items():
                cols = _job_columns(job, valid)
                conn.execute(
                    """INSERT OR IGNORE INTO jobs(id, project_id, template_id, template_label, status,
                           queued_at, started_at, finished_at, deleted_at,
                           pod_id, pod_name, pod_kind, pod_gpu, pod_cost_per_hr, owner_id, data_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (jid, *cols, json.dumps(job, ensure_ascii=False, default=str)),
                )
                count += 1
        set_meta(conn, "legacy_jobs_imported", now_iso())
    if state_file.exists():
        target = state_file.with_name(state_file.name + ".migrated")
        try:
            if target.exists():
                target.unlink()
            state_file.rename(target)
        except OSError:
            pass
    return count


# ---- 원격 ComfyUI 동기화 기록 (comfy_outputs.py) -------------------------------------------------

def load_comfy_sync() -> dict:
    """{"downloaded": {"<subfolder>/<파일명>": {"at", "pod_id"}}, "last_sync"} — 예전
    comfy_output_sync.json과 같은 모양이라 comfy_outputs.py의 동기화 로직이 그대로 쓴다."""
    with connect() as conn:
        rows = conn.execute("SELECT key, downloaded_at, pod_id FROM comfy_downloads").fetchall()
        last_sync = get_meta(conn, "comfy_last_sync")
    return {
        "downloaded": {r["key"]: {"at": r["downloaded_at"], "pod_id": r["pod_id"]} for r in rows},
        "last_sync": last_sync,
    }


def save_comfy_sync(state: dict) -> None:
    """state 전체에 DB를 맞춘다 — 바뀐 항목만 쓰고, 없어진 항목은 지운다(forget). 파드가
    새로 적힌 항목은 결과물 색인(assets)의 origin_pod_id도 같이 맞춘다."""
    downloaded = state.get("downloaded") or {}
    with connect() as conn:
        existing = {r["key"]: (r["downloaded_at"], r["pod_id"])
                    for r in conn.execute("SELECT key, downloaded_at, pod_id FROM comfy_downloads")}
        for key, value in downloaded.items():
            at = (value.get("at") if isinstance(value, dict) else value) or now_iso()
            pod_id = value.get("pod_id") if isinstance(value, dict) else None
            if existing.get(key) == (at, pod_id):
                continue
            conn.execute(
                "INSERT INTO comfy_downloads(key, downloaded_at, pod_id) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET downloaded_at=excluded.downloaded_at, pod_id=excluded.pod_id",
                (key, at, pod_id),
            )
            if pod_id:
                conn.execute(
                    "UPDATE assets SET origin_pod_id=? WHERE path=? AND (origin_pod_id IS NULL OR origin_pod_id != ?)",
                    (pod_id, key, pod_id),
                )
        for key in set(existing) - set(downloaded):
            conn.execute("DELETE FROM comfy_downloads WHERE key=?", (key,))
        if state.get("last_sync"):
            set_meta(conn, "comfy_last_sync", state["last_sync"])


def import_legacy_comfy_sync(state_file: Path) -> int:
    """comfy_output_sync.json이 있고 아직 안 가져왔으면 한 번만 DB로 옮기고 원본은
    .migrated로 보존한다(import_legacy_jobs와 같은 방식). 가져온 개수를 돌려준다."""
    with connect() as conn:
        if get_meta(conn, "legacy_comfy_sync_imported"):
            return 0
        count = 0
        if state_file.exists():
            try:
                with open(state_file, encoding="utf-8") as f:
                    legacy = json.load(f)
            except (OSError, json.JSONDecodeError):
                legacy = {}   # 깨진 기록은 예전에도 "빈 상태로 시작"이었다
            downloaded = legacy.get("downloaded") if isinstance(legacy, dict) else None
            for key, value in (downloaded or {}).items():
                at = (value.get("at") if isinstance(value, dict) else value) or now_iso()
                pod_id = value.get("pod_id") if isinstance(value, dict) else None
                conn.execute("INSERT OR IGNORE INTO comfy_downloads(key, downloaded_at, pod_id) VALUES(?,?,?)",
                             (key, str(at), pod_id))
                count += 1
            if isinstance(legacy, dict) and legacy.get("last_sync"):
                set_meta(conn, "comfy_last_sync", str(legacy["last_sync"]))
        set_meta(conn, "legacy_comfy_sync_imported", now_iso())
    if state_file.exists():
        target = state_file.with_name(state_file.name + ".migrated")
        try:
            if target.exists():
                target.unlink()
            state_file.rename(target)
        except OSError:
            pass
    return count
