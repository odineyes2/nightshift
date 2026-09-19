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

# 새 버전은 여기 끝에 (버전, SQL) 한 줄을 추가한다 — PRAGMA user_version이 현재 버전이다.
MIGRATIONS = [(1, SCHEMA_V1)]


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
                       pod_id, pod_name, pod_kind, pod_gpu, pod_cost_per_hr, data_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       project_id=excluded.project_id, template_id=excluded.template_id,
                       template_label=excluded.template_label, status=excluded.status,
                       queued_at=excluded.queued_at, started_at=excluded.started_at,
                       finished_at=excluded.finished_at, deleted_at=excluded.deleted_at,
                       pod_id=excluded.pod_id, pod_name=excluded.pod_name, pod_kind=excluded.pod_kind,
                       pod_gpu=excluded.pod_gpu, pod_cost_per_hr=excluded.pod_cost_per_hr,
                       data_json=excluded.data_json""",
                (jid, *cols, data),
            )
        existing = {r["id"] for r in conn.execute("SELECT id FROM jobs")}
        for jid in existing - set(snapshot):
            conn.execute("DELETE FROM jobs WHERE id=?", (jid,))
    _saved_json.clear()
    _saved_json.update(snapshot)


def load_jobs() -> dict[str, dict]:
    with connect() as conn:
        rows = conn.execute("SELECT id, project_id, data_json FROM jobs").fetchall()
    result = {}
    for r in rows:
        job = json.loads(r["data_json"])
        # 컬럼이 진실이다 — 프로젝트가 지워져 FK가 NULL이 된 job은 dict에서도 미분류로 본다.
        job["project_id"] = r["project_id"]
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
                           pod_id, pod_name, pod_kind, pod_gpu, pod_cost_per_hr, data_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
