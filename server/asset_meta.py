"""
결과물(asset)에 사람이 붙이는 정보 — 즐겨찾기·평점·메모·태그 — 와 그걸로 하는 검색.

색인(assets_index.py)이 디스크의 파일을 assets 행으로 맞추고, 여기서는 그 행의
favorite/rating/note 와 tags 를 읽고 쓴다. 경로(OUTPUT_DIR 기준 상대경로)로 식별한다 —
갤러리 화면이 다루는 이름이 그 경로이기 때문이다.

검색은 FTS 대신 LIKE 다: 프롬프트가 "cowgirl, 1girl" 같은 쉼표 나열이고 한국어 메모는
조사가 붙어 단어 경계 토큰화로는 부분 일치가 안 맞는데, 결과물이 수천 장 규모라 LIKE 스캔이
충분히 빠르다. 규모가 커지면 FTS5(trigram)로 바꿀 수 있게 검색을 search_paths() 한 곳에 모아 뒀다.
"""

import re

import assets_index
import db

MAX_TAG_LEN = 40
MAX_NOTE_LEN = 4000


class AssetNotFound(Exception):
    pass


def normalize_tag(tag) -> str:
    return re.sub(r"\s+", " ", str(tag or "").strip())[:MAX_TAG_LEN]


def normalize_tags(tags) -> list[str]:
    seen, out = set(), []
    for tag in tags or []:
        t = normalize_tag(tag)
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _tags_for(conn, asset_ids: list[int]) -> dict[int, list[str]]:
    if not asset_ids:
        return {}
    out: dict[int, list[str]] = {i: [] for i in asset_ids}
    marks = ",".join("?" * len(asset_ids))
    for r in conn.execute(
        f"SELECT at.asset_id, t.name FROM asset_tags at JOIN tags t ON t.id = at.tag_id "
        f"WHERE at.asset_id IN ({marks}) ORDER BY t.name COLLATE NOCASE", asset_ids):
        out[r["asset_id"]].append(r["name"])
    return out


def meta_by_path() -> dict[str, dict]:
    """{경로: {asset_id, favorite, rating, tags}} — 갤러리 목록이 항목마다 붙일 때 쓴다."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, path, favorite, rating, project_id FROM assets WHERE deleted_at IS NULL").fetchall()
        tags = {}
        for r in conn.execute(
            "SELECT at.asset_id, t.name FROM asset_tags at JOIN tags t ON t.id = at.tag_id "
            "ORDER BY t.name COLLATE NOCASE"):
            tags.setdefault(r["asset_id"], []).append(r["name"])
    return {
        r["path"]: {"asset_id": r["id"], "favorite": bool(r["favorite"]), "rating": r["rating"],
                    "project_id": r["project_id"], "tags": tags.get(r["id"], [])}
        for r in rows
    }


def _build_where(q=None, tags=None, favorite=None, min_rating=None, kind=None,
                 project=None, job_id=None) -> tuple[list[str], list]:
    where, params = ["a.deleted_at IS NULL"], []
    if project == "unassigned":
        where.append("a.project_id IS NULL")
    elif project is not None:
        where.append("a.project_id = ?"); params.append(int(project))
    if job_id:
        where.append("a.job_id = ?"); params.append(job_id)
    if kind:
        where.append("a.kind = ?"); params.append(kind)
    if favorite is not None:
        where.append("a.favorite = ?"); params.append(1 if favorite else 0)
    if min_rating:
        where.append("a.rating >= ?"); params.append(int(min_rating))
    for tag in normalize_tags(tags):
        where.append("EXISTS (SELECT 1 FROM asset_tags at JOIN tags t ON t.id = at.tag_id "
                     "WHERE at.asset_id = a.id AND t.name = ? COLLATE NOCASE)")
        params.append(tag)
    for term in [t for t in (q or "").split() if t]:
        like = _like(term)
        where.append(
            "(a.prompt LIKE ? ESCAPE '\\' OR a.note LIKE ? ESCAPE '\\' OR a.path LIKE ? ESCAPE '\\' "
            "OR a.checkpoint LIKE ? ESCAPE '\\' "
            "OR EXISTS (SELECT 1 FROM asset_tags at JOIN tags t ON t.id = at.tag_id "
            "          WHERE at.asset_id = a.id AND t.name LIKE ? ESCAPE '\\') "
            "OR CAST(a.seed AS TEXT) = ?)")
        params.extend([like, like, like, like, like, term])
    return where, params


def search_paths(q: str | None = None, tags: list[str] | None = None, favorite: bool | None = None,
                 min_rating: int | None = None, kind: str | None = None) -> set[str] | None:
    """조건에 맞는 경로 집합. 조건이 하나도 없으면 None(거르지 않음) — 갤러리 목록이 쓴다."""
    if not (q or "").split() and not normalize_tags(tags) and favorite is None and not min_rating and kind is None:
        return None
    where, params = _build_where(q, tags, favorite, min_rating, kind)
    with db.connect() as conn:
        rows = conn.execute("SELECT a.path FROM assets a WHERE " + " AND ".join(where), params).fetchall()
    return {r["path"] for r in rows}


def list_assets(q=None, tags=None, favorite=None, min_rating=None, kind=None, project=None,
                job_id=None, limit: int = 200, offset: int = 0) -> list[dict]:
    """결과물 색인 조회(태그 포함). project: 프로젝트 id 또는 "unassigned"."""
    where, params = _build_where(q, tags, favorite, min_rating, kind, project, job_id)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT a.* FROM assets a WHERE " + " AND ".join(where)
            + " ORDER BY a.created_at DESC, a.id DESC LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        tag_map = _tags_for(conn, [r["id"] for r in rows])
    out = []
    for r in rows:
        d = dict(r)
        d["favorite"] = bool(d["favorite"])
        d["tags"] = tag_map.get(r["id"], [])
        out.append(d)
    return out


def _asset_ids(conn, paths: list[str]) -> dict[str, int]:
    """경로 -> id. 없는 경로가 하나라도 있으면 AssetNotFound(호출부가 미리 _ensure_indexed 한다)."""
    ids: dict[str, int] = {}
    for p in paths:
        r = conn.execute("SELECT id FROM assets WHERE path=? AND deleted_at IS NULL", (p,)).fetchone()
        if r is None:
            raise AssetNotFound(p)
        ids[p] = r["id"]
    return ids


def _ensure_indexed(paths: list[str]) -> None:
    with db.connect() as conn:
        known = {r["path"] for r in conn.execute(
            f"SELECT path FROM assets WHERE deleted_at IS NULL AND path IN ({','.join('?' * len(paths))})", paths)}
    if any(p not in known for p in paths):
        assets_index.sync(force=True)


def get_detail(path: str) -> dict:
    _ensure_indexed([path])
    with db.connect() as conn:
        r = conn.execute("SELECT * FROM assets WHERE path=? AND deleted_at IS NULL", (path,)).fetchone()
        if r is None:
            raise AssetNotFound(path)
        detail = dict(r)
        detail["tags"] = _tags_for(conn, [r["id"]])[r["id"]]
        job = conn.execute("SELECT template_label, pod_name FROM jobs WHERE id=?", (r["job_id"],)).fetchone() \
            if r["job_id"] else None
    detail["favorite"] = bool(detail["favorite"])
    detail["job_label"] = job["template_label"] if job else None
    detail["pod_name"] = job["pod_name"] if job else None
    return detail


def update_assets(paths: list[str], favorite=None, rating="unset", note=None) -> int:
    """즐겨찾기/평점/메모를 바꾼다. rating은 0~5(0/None이면 평점 없음), 안 넘기면 그대로."""
    paths = list(dict.fromkeys(paths))
    if not paths:
        return 0
    _ensure_indexed(paths)
    sets, params = [], []
    if favorite is not None:
        sets.append("favorite=?"); params.append(1 if favorite else 0)
    if rating != "unset":
        value = None if rating in (None, 0) else int(rating)
        if value is not None and not 1 <= value <= 5:
            raise ValueError("평점은 1~5(또는 0=없음)여야 해요.")
        sets.append("rating=?"); params.append(value)
    if note is not None:
        sets.append("note=?"); params.append(str(note)[:MAX_NOTE_LEN])
    if not sets:
        return 0
    with db.connect() as conn:
        ids = _asset_ids(conn, paths)
        marks = ",".join("?" * len(ids))
        cur = conn.execute(f"UPDATE assets SET {', '.join(sets)} WHERE id IN ({marks})", (*params, *ids.values()))
        return cur.rowcount


def change_tags(paths: list[str], add=None, remove=None) -> dict[str, list[str]]:
    """여러 결과물에 태그를 붙이고/뗀다. 바뀐 결과물의 태그 목록을 {경로: [태그]}로 돌려준다."""
    paths = list(dict.fromkeys(paths))
    add, remove = normalize_tags(add), normalize_tags(remove)
    if not paths:
        return {}
    _ensure_indexed(paths)
    with db.connect() as conn:
        ids = _asset_ids(conn, paths)
        for name in add:
            conn.execute("INSERT OR IGNORE INTO tags(name) VALUES(?)", (name,))
            tag_id = conn.execute("SELECT id FROM tags WHERE name=? COLLATE NOCASE", (name,)).fetchone()["id"]
            for asset_id in ids.values():
                conn.execute("INSERT OR IGNORE INTO asset_tags(asset_id, tag_id) VALUES(?,?)", (asset_id, tag_id))
        for name in remove:
            row = conn.execute("SELECT id FROM tags WHERE name=? COLLATE NOCASE", (name,)).fetchone()
            if row:
                for asset_id in ids.values():
                    conn.execute("DELETE FROM asset_tags WHERE asset_id=? AND tag_id=?", (asset_id, row["id"]))
        # 아무 결과물에도 안 붙은 태그는 치운다(자동완성 목록이 지저분해지지 않게).
        conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM asset_tags)")
        tags = _tags_for(conn, list(ids.values()))
    return {p: tags[i] for p, i in ids.items()}


def list_tags(limit: int = 500) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT t.name, COUNT(*) AS count FROM tags t "
            "JOIN asset_tags at ON at.tag_id = t.id JOIN assets a ON a.id = at.asset_id AND a.deleted_at IS NULL "
            "GROUP BY t.id ORDER BY count DESC, t.name COLLATE NOCASE LIMIT ?", (limit,)).fetchall()
    return [{"name": r["name"], "count": r["count"]} for r in rows]
