"""
프로젝트 CRUD와 통계. 프로젝트는 파드와 무관하게 작업(job)과 결과물(asset)을 묶는다.
project_id가 NULL인 것은 "미분류"다 — 프로젝트를 지우면 그 안의 작업/결과물은 지워지지
않고 미분류로 돌아온다(FK ON DELETE SET NULL). 메모리 jobs dict 쪽 정리는 app.py가 한다.
"""

import json

import db

# est_cost = 각 작업의 실행 시간(시작~종료) x 그 작업이 돈 파드의 시간당 비용(작업을 만들 때 찍어둔
# 스냅샷)의 합 — 파드가 실제로 켜져 있던 시간이 아니라 "이 작업이 차지한 시간"의 추정치다. 비용을 모르는
# 작업(예전 작업, RunPod 정보를 못 얻은 경우)은 합계에서 빠지고 uncosted_jobs로 센다. 삭제한 작업도
# (돈은 이미 썼으므로) 포함한다.
_STATS_SQL = """
SELECT p.*,
  (SELECT COUNT(*) FROM jobs j WHERE j.project_id = p.id AND j.deleted_at IS NULL) AS job_count,
  (SELECT COUNT(*) FROM jobs j WHERE j.project_id = p.id AND j.deleted_at IS NULL
        AND j.status IN ('queued','running')) AS active_jobs,
  (SELECT COUNT(*) FROM assets a WHERE a.project_id = p.id AND a.deleted_at IS NULL) AS asset_count,
  (SELECT COUNT(*) FROM assets a WHERE a.project_id = p.id AND a.deleted_at IS NULL
        AND a.favorite = 1) AS favorite_count,
  COALESCE(
    (SELECT a.path FROM assets a WHERE a.id = p.cover_asset_id AND a.deleted_at IS NULL AND a.project_id = p.id),
    (SELECT a.path FROM assets a WHERE a.project_id = p.id AND a.kind = 'image'
        AND a.deleted_at IS NULL ORDER BY a.created_at DESC LIMIT 1)
  ) AS cover_path,
  (SELECT COALESCE(SUM((julianday(j.finished_at) - julianday(j.started_at)) * 24.0 * j.pod_cost_per_hr), 0)
     FROM jobs j WHERE j.project_id = p.id AND j.started_at IS NOT NULL AND j.finished_at IS NOT NULL
        AND j.pod_cost_per_hr IS NOT NULL) AS est_cost,
  (SELECT COALESCE(SUM((julianday(j.finished_at) - julianday(j.started_at)) * 86400.0), 0)
     FROM jobs j WHERE j.project_id = p.id AND j.started_at IS NOT NULL AND j.finished_at IS NOT NULL) AS run_seconds,
  (SELECT COUNT(*) FROM jobs j WHERE j.project_id = p.id AND j.started_at IS NOT NULL
        AND j.finished_at IS NOT NULL AND j.pod_cost_per_hr IS NULL) AS uncosted_jobs,
  (SELECT MAX(t) FROM (
      SELECT MAX(j.queued_at) AS t FROM jobs j WHERE j.project_id = p.id
      UNION ALL
      SELECT MAX(a.created_at) FROM assets a WHERE a.project_id = p.id AND a.deleted_at IS NULL
  )) AS last_activity
FROM projects p
"""


def _row(r) -> dict:
    d = dict(r)
    d["defaults"] = json.loads(d.pop("defaults_json") or "{}")
    d["archived"] = d.get("archived_at") is not None
    return d


def list_projects(include_archived: bool = False, owner_id: int | None = None) -> list[dict]:
    """owner_id를 주면 그 회원의 프로젝트만(admin은 None으로 전부)."""
    where, params = [], []
    if not include_archived:
        where.append("p.archived_at IS NULL")
    if owner_id is not None:
        where.append("p.owner_id = ?"); params.append(owner_id)
    with db.connect() as conn:
        rows = conn.execute(
            _STATS_SQL + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY last_activity DESC, p.id DESC", params
        ).fetchall()
    return [_row(r) for r in rows]


def unassigned_summary(owner_id: int | None = None) -> dict:
    """"미분류"(프로젝트 없는 것) 요약. owner_id를 주면 그 회원 것만, 없으면(admin) 전부."""
    jo = " AND owner_id = ?" if owner_id is not None else ""
    ao = " AND owner_id = ?" if owner_id is not None else ""
    args = (owner_id,) if owner_id is not None else ()
    with db.connect() as conn:
        job_count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE project_id IS NULL AND deleted_at IS NULL" + jo, args).fetchone()[0]
        active = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE project_id IS NULL AND deleted_at IS NULL "
            "AND status IN ('queued','running')" + jo, args).fetchone()[0]
        asset_count = conn.execute(
            "SELECT COUNT(*) FROM assets WHERE project_id IS NULL AND deleted_at IS NULL" + ao, args).fetchone()[0]
        cover = conn.execute(
            "SELECT path FROM assets WHERE project_id IS NULL AND kind='image' AND deleted_at IS NULL" + ao +
            " ORDER BY created_at DESC LIMIT 1", args).fetchone()
        favorites = conn.execute(
            "SELECT COUNT(*) FROM assets WHERE project_id IS NULL AND deleted_at IS NULL AND favorite = 1" + ao,
            args).fetchone()[0]
        cost = conn.execute(
            "SELECT COALESCE(SUM((julianday(finished_at) - julianday(started_at)) * 24.0 * pod_cost_per_hr), 0) "
            "FROM jobs WHERE project_id IS NULL AND started_at IS NOT NULL AND finished_at IS NOT NULL "
            "AND pod_cost_per_hr IS NOT NULL" + jo, args).fetchone()[0]
        uncosted = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE project_id IS NULL AND started_at IS NOT NULL "
            "AND finished_at IS NOT NULL AND pod_cost_per_hr IS NULL" + jo, args).fetchone()[0]
    return {"job_count": job_count, "active_jobs": active, "asset_count": asset_count,
            "favorite_count": favorites, "est_cost": cost, "uncosted_jobs": uncosted,
            "cover_path": cover["path"] if cover else None}


def get_project(project_id: int) -> dict | None:
    with db.connect() as conn:
        r = conn.execute(_STATS_SQL + " WHERE p.id = ?", (project_id,)).fetchone()
    return _row(r) if r else None


def cover_candidate_ok(asset_id, project_id: int) -> bool:
    """대표 이미지로 쓸 수 있는 결과물인가 — 그 프로젝트에 들어 있는 (지워지지 않은) 이미지여야 한다.
    다른 프로젝트·다른 회원의 결과물 번호를 넣어 경로를 엿보지 못하게 서버가 확인한다."""
    if not isinstance(asset_id, int) or isinstance(asset_id, bool):
        return False
    with db.connect() as conn:
        return conn.execute(
            "SELECT 1 FROM assets WHERE id=? AND project_id=? AND kind='image' AND deleted_at IS NULL",
            (asset_id, project_id)).fetchone() is not None


def project_exists(project_id, owner_id: int | None = None) -> bool:
    """프로젝트가 있는가. owner_id를 주면 그 회원의 프로젝트일 때만 True."""
    if not isinstance(project_id, int) or isinstance(project_id, bool):
        return False
    with db.connect() as conn:
        row = conn.execute("SELECT owner_id FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        return False
    return owner_id is None or row["owner_id"] == owner_id


def project_owner(project_id: int):
    """프로젝트의 주인 id (없는 프로젝트면 예외 대신 (False, None))."""
    with db.connect() as conn:
        row = conn.execute("SELECT owner_id FROM projects WHERE id=?", (project_id,)).fetchone()
    return (row is not None, row["owner_id"] if row else None)


def create_project(name: str, description: str = "", defaults: dict | None = None, owner_id: int | None = None) -> dict:
    now = db.now_iso()
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO projects(name, description, defaults_json, created_at, updated_at, owner_id) VALUES(?,?,?,?,?,?)",
            (name, description, json.dumps(defaults or {}, ensure_ascii=False), now, now, owner_id),
        )
        project_id = cur.lastrowid
    return get_project(project_id)


def update_project(project_id: int, fields: dict) -> dict | None:
    sets, params = [], []
    if "name" in fields:
        sets.append("name=?"); params.append(fields["name"])
    if "description" in fields:
        sets.append("description=?"); params.append(fields["description"])
    if "defaults" in fields:
        sets.append("defaults_json=?"); params.append(json.dumps(fields["defaults"], ensure_ascii=False))
    if "cover_asset_id" in fields:
        sets.append("cover_asset_id=?"); params.append(fields["cover_asset_id"])
    if "archived" in fields:
        sets.append("archived_at=?"); params.append(db.now_iso() if fields["archived"] else None)
    if not sets:
        return get_project(project_id)
    sets.append("updated_at=?"); params.append(db.now_iso())
    with db.connect() as conn:
        cur = conn.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id=?", (*params, project_id))
        if cur.rowcount == 0:
            return None
    return get_project(project_id)


def delete_project(project_id: int) -> bool:
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
        return cur.rowcount > 0


def set_job_assets_project(job_id: str, project_id: int | None) -> int:
    """job의 프로젝트가 바뀌면 그 job이 만든 결과물도 같이 옮긴다."""
    with db.connect() as conn:
        cur = conn.execute("UPDATE assets SET project_id=? WHERE job_id=?", (project_id, job_id))
        return cur.rowcount
