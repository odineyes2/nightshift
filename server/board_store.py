"""
프로젝트 보드(무한 캔버스) — 이미지/영상/텍스트 카드(board_nodes)와 그 사이 연결선
(board_edges). 둘 다 project_id로 스코프되고, 프로젝트가 지워지면 같이 지워진다
(ON DELETE CASCADE, db.py의 SCHEMA_V10 참고 — jobs/assets와 달리 "미분류"로 남을
의미가 없어서다). 소유권 확인(이 프로젝트가 요청한 회원 것인지)은 app.py가
project_or_404()로 이미 하므로, 여기서는 project_id로 좁히는 조회/수정만 한다.
"""

import json

import db


def _row(r) -> dict:
    d = dict(r)
    # 카드 종류별 부가 정보(생성 카드의 프리셋 사본 등)는 JSON으로 풀어 data로 준다.
    if "data_json" in d:
        raw = d.pop("data_json")
        d["data"] = json.loads(raw) if raw else None
    return d


# 카드를 돌려줄 때 그 결과물의 nsfw 표시를 붙인다 — 프론트가 갤러리와 같은 NSFW
# 보기/블러/안 보기 설정을 카드에도 적용하려면 필요하다. asset_path는 assets.path와
# 같은 값이라 그대로 잇는다(텍스트 카드나 파일이 지워진 카드는 0).
_NODE_SELECT = ("SELECT n.*, COALESCE(a.nsfw, 0) AS nsfw FROM board_nodes n "
                "LEFT JOIN assets a ON a.path = n.asset_path")


def _select_node(conn, node_id: int):
    return conn.execute(f"{_NODE_SELECT} WHERE n.id=?", (node_id,)).fetchone()


def list_board(project_id: int) -> dict:
    """이 프로젝트의 노드+연결선 전체(GET .../board)."""
    with db.connect() as conn:
        nodes = conn.execute(
            f"{_NODE_SELECT} WHERE n.project_id=? ORDER BY n.id", (project_id,)).fetchall()
        edges = conn.execute(
            "SELECT * FROM board_edges WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
    return {"nodes": [_row(r) for r in nodes], "edges": [_row(r) for r in edges]}


def create_node(project_id: int, kind: str, asset_path: str | None, text: str,
                x: float, y: float, width: float | None, height: float | None, job_id: str | None = None,
                data: dict | None = None) -> dict:
    now = db.now_iso()
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO board_nodes(project_id, kind, asset_path, job_id, data_json, text, x, y, width, height, z_index, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, kind, asset_path, job_id, json.dumps(data) if data is not None else None, text, x, y,
             width if width is not None else 220, height if height is not None else 220,
             0, now, now),
        )
        row = _select_node(conn, cur.lastrowid)
    return _row(row)


def get_node(project_id: int, node_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute(f"{_NODE_SELECT} WHERE n.id=? AND n.project_id=?", (node_id, project_id)).fetchone()
    return _row(row) if row else None


def node_project_id(node_id: int) -> int | None:
    """이 노드가 어느 프로젝트 것인지 — app.py가 요청한 project_id와 같은지 확인하는 용도."""
    with db.connect() as conn:
        row = conn.execute("SELECT project_id FROM board_nodes WHERE id=?", (node_id,)).fetchone()
    return row["project_id"] if row else None


def update_node(node_id: int, fields: dict) -> dict | None:
    """드래그가 끝났을 때(위치) · 텍스트 카드를 고쳐 썼을 때 호출 — 넘겨준 필드만 바꾼다."""
    sets, params = [], []
    for key in ("text", "x", "y", "width", "height"):
        if key in fields:
            sets.append(f"{key}=?")
            params.append(fields[key])
    if "data" in fields:   # 부가 정보는 통째로 바꾼다(합치기는 app.py가 해서 넘긴다)
        sets.append("data_json=?")
        params.append(json.dumps(fields["data"]) if fields["data"] is not None else None)
    if not sets:
        with db.connect() as conn:
            row = _select_node(conn, node_id)
        return _row(row) if row else None
    sets.append("updated_at=?")
    params.append(db.now_iso())
    with db.connect() as conn:
        cur = conn.execute(f"UPDATE board_nodes SET {', '.join(sets)} WHERE id=?", (*params, node_id))
        if cur.rowcount == 0:
            return None
        row = _select_node(conn, node_id)
    return _row(row)


def delete_node(node_id: int) -> bool:
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM board_nodes WHERE id=?", (node_id,))
        return cur.rowcount > 0


def node_project_ids(node_ids) -> dict:
    """{node_id: project_id} — 연결선 양 끝이 둘 다 같은 프로젝트 카드인지 한 번에 확인하는 용도."""
    ids = list(node_ids)
    if not ids:
        return {}
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT id, project_id FROM board_nodes WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    return {r["id"]: r["project_id"] for r in rows}


def create_slot_edge(project_id: int, from_node_id: int, to_node_id: int, to_slot: str) -> tuple:
    """이미지 카드를 생성 카드의 입구(to_slot)에 잇는다. 입구 하나에는 선 하나만 — 그 입구로 이미
    들어오던 선과, 같은 두 카드 사이의 다른 선은 지우고 새로 넣는다. (새 선, 지운 선들)을 돌려준다
    (화면이 지운 선을 치우고, 되돌리기 때 되살린다)."""
    with db.connect() as conn:
        removed = [_row(r) for r in conn.execute(
            "SELECT * FROM board_edges WHERE project_id=? AND ((to_node_id=? AND to_slot=?) OR "
            "(from_node_id=? AND to_node_id=?) OR (from_node_id=? AND to_node_id=?))",
            (project_id, to_node_id, to_slot, from_node_id, to_node_id, to_node_id, from_node_id)).fetchall()]
        if removed:
            conn.executemany("DELETE FROM board_edges WHERE id=?", [(e["id"],) for e in removed])
        cur = conn.execute(
            "INSERT INTO board_edges(project_id, from_node_id, to_node_id, to_slot, created_at) VALUES(?,?,?,?,?)",
            (project_id, from_node_id, to_node_id, to_slot, db.now_iso()))
        row = conn.execute("SELECT * FROM board_edges WHERE id=?", (cur.lastrowid,)).fetchone()
    return _row(row), removed


def slot_edges(project_id: int, node_id: int) -> list:
    """생성 카드의 입구로 들어오는 선들 — [(출발 카드 id, 입구 이름), ...]."""
    with db.connect() as conn:
        rows = conn.execute("SELECT from_node_id, to_slot FROM board_edges WHERE project_id=? AND to_node_id=? "
                            "AND to_slot IS NOT NULL", (project_id, node_id)).fetchall()
    return [(r["from_node_id"], r["to_slot"]) for r in rows]


def gen_runs(project_id: int) -> list:
    """이 보드의 생성 카드마다 실행 기록 — [(카드 id, runs), ...]. 실행한 적 없는 카드는 뺀다."""
    with db.connect() as conn:
        rows = conn.execute("SELECT id, data_json FROM board_nodes WHERE project_id=? AND kind='gen'", (project_id,)).fetchall()
    out = []
    for r in rows:
        runs = (json.loads(r["data_json"]) if r["data_json"] else {}).get("runs") or []
        if runs:
            out.append((r["id"], runs))
    return out


def job_assets(job_id: str, limit: int = 60) -> list:
    """작업의 결과물 전부(오래된 것부터) — [{path, kind}, ...]. 생성 카드의 결과 펼치기용. 지운 것은 뺀다."""
    with db.connect() as conn:
        rows = conn.execute("SELECT path, kind FROM assets WHERE job_id=? AND deleted_at IS NULL "
                            "ORDER BY created_at, id LIMIT ?", (job_id, limit)).fetchall()
    return [{"path": r["path"], "kind": r["kind"]} for r in rows]


def create_edge(project_id: int, from_node_id: int, to_node_id: int) -> dict:
    """두 카드를 잇는다. 같은 두 카드 사이에 (방향 무관) 이미 선이 있으면 새로 만들지
    않고 그 선을 그대로 돌려준다 — 같은 곳에 선이 겹쳐 그려지면 하나를 지워도 계속
    이어져 보여서 헷갈린다."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM board_edges WHERE project_id=? AND "
            "((from_node_id=? AND to_node_id=?) OR (from_node_id=? AND to_node_id=?))",
            (project_id, from_node_id, to_node_id, to_node_id, from_node_id)).fetchone()
        if row:
            return _row(row)
        cur = conn.execute(
            "INSERT INTO board_edges(project_id, from_node_id, to_node_id, created_at) VALUES(?,?,?,?)",
            (project_id, from_node_id, to_node_id, db.now_iso()))
        row = conn.execute("SELECT * FROM board_edges WHERE id=?", (cur.lastrowid,)).fetchone()
    return _row(row)


def delete_edge(project_id: int, edge_id: int) -> bool:
    """project_id로 같이 좁혀서 지운다 — 다른 프로젝트의 선 id를 넣어도 안 지워진다."""
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM board_edges WHERE id=? AND project_id=?", (edge_id, project_id))
        return cur.rowcount > 0


class BoardNotFound(Exception):
    """넘겨받은 카드/선 id 중 이 프로젝트 것이 아닌 게 섞여 있다 — app.py가 404로 바꾼다."""


def _check_nodes_in_project(conn, project_id: int, ids) -> None:
    ids = set(ids)
    if not ids:
        return
    rows = conn.execute(
        f"SELECT id FROM board_nodes WHERE project_id=? AND id IN ({','.join('?' * len(ids))})",
        (project_id, *ids)).fetchall()
    if len(rows) != len(ids):
        raise BoardNotFound()


def move_nodes(project_id: int, moves: list) -> None:
    """여러 카드를 한 번에 옮긴다(여러 장 선택해 끌기 · 되돌리기). moves = [(id, x, y), ...].
    하나라도 이 프로젝트 카드가 아니면 아무것도 안 바꾼다(트랜잭션째 되돌림)."""
    now = db.now_iso()
    with db.connect() as conn:
        _check_nodes_in_project(conn, project_id, (m[0] for m in moves))
        conn.executemany(
            "UPDATE board_nodes SET x=?, y=?, updated_at=? WHERE id=? AND project_id=?",
            [(x, y, now, node_id, project_id) for node_id, x, y in moves])


def delete_nodes(project_id: int, ids: list) -> None:
    """여러 카드를 한 번에 지운다. 붙은 선은 CASCADE로 같이 지워진다."""
    with db.connect() as conn:
        _check_nodes_in_project(conn, project_id, ids)
        conn.executemany("DELETE FROM board_nodes WHERE id=? AND project_id=?", [(i, project_id) for i in ids])


def restore(project_id: int, nodes: list, edges: list) -> dict:
    """지운 카드·선을 되살린다(되돌리기). 원래 id가 비어 있으면 그 id 그대로 넣어서,
    화면이 들고 있는 되돌리기 기록의 id가 계속 맞게 한다. 그 사이 다른 카드가 그 id를
    가져갔으면 새 id를 받고, 바뀐 것을 id_map으로 알려 준다(화면이 기록을 고쳐 씀).
    nodes의 항목은 app.py가 검증을 마친 dict(kind/asset_path/text/x/y/width/height/z_index/
    created_at/id). 선의 양 끝은 되살린 카드이거나 이미 이 프로젝트에 있는 카드여야 한다."""
    now = db.now_iso()
    id_map = {}
    node_ids, edge_ids = [], []
    with db.connect() as conn:
        for n in nodes:
            old = n.get("id")
            free = isinstance(old, int) and conn.execute(
                "SELECT 1 FROM board_nodes WHERE id=?", (old,)).fetchone() is None
            data = n.get("data")
            cols = (project_id, n["kind"], n.get("asset_path"), n.get("job_id"),
                    json.dumps(data) if data is not None else None, n.get("text") or "", n["x"], n["y"],
                    n["width"], n["height"], n.get("z_index") or 0, n.get("created_at") or now, now)
            if free:
                conn.execute(
                    "INSERT INTO board_nodes(id, project_id, kind, asset_path, job_id, data_json, text, x, y, width, height, "
                    "z_index, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (old, *cols))
                new = old
            else:
                new = conn.execute(
                    "INSERT INTO board_nodes(project_id, kind, asset_path, job_id, data_json, text, x, y, width, height, "
                    "z_index, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", cols).lastrowid
            if old is not None:
                id_map[old] = new
            node_ids.append(new)
        for e in edges:
            a = id_map.get(e["from_node_id"], e["from_node_id"])
            b = id_map.get(e["to_node_id"], e["to_node_id"])
            if a == b:
                raise BoardNotFound()
            _check_nodes_in_project(conn, project_id, (a, b))
            dup = conn.execute(
                "SELECT id FROM board_edges WHERE project_id=? AND "
                "((from_node_id=? AND to_node_id=?) OR (from_node_id=? AND to_node_id=?))",
                (project_id, a, b, b, a)).fetchone()
            if dup:
                edge_ids.append(dup["id"])
                continue
            old = e.get("id")
            free = isinstance(old, int) and conn.execute(
                "SELECT 1 FROM board_edges WHERE id=?", (old,)).fetchone() is None
            slot = e.get("to_slot")
            if free:
                conn.execute("INSERT INTO board_edges(id, project_id, from_node_id, to_node_id, to_slot, created_at) "
                             "VALUES(?,?,?,?,?,?)", (old, project_id, a, b, slot, e.get("created_at") or now))
                edge_ids.append(old)
            else:
                edge_ids.append(conn.execute(
                    "INSERT INTO board_edges(project_id, from_node_id, to_node_id, to_slot, created_at) VALUES(?,?,?,?,?)",
                    (project_id, a, b, slot, e.get("created_at") or now)).lastrowid)
        out_nodes = [_row(_select_node(conn, i)) for i in node_ids]
        out_edges = [_row(conn.execute("SELECT * FROM board_edges WHERE id=?", (i,)).fetchone()) for i in edge_ids]
    return {"nodes": out_nodes, "edges": out_edges, "id_map": {str(k): v for k, v in id_map.items()}}


def job_nodes(project_id: int) -> list:
    """이 프로젝트 보드의 작업 카드들 — [(node_id, job_id 또는 None), ...]."""
    with db.connect() as conn:
        rows = conn.execute("SELECT id, job_id FROM board_nodes WHERE project_id=? AND kind='job' ORDER BY id",
                            (project_id,)).fetchall()
    return [(r["id"], r["job_id"]) for r in rows]


def job_results(job_ids, per_job: int = 4) -> dict:
    """작업마다 가장 최근 결과물 몇 개 — {job_id: [{path, kind, nsfw}, ...]}. 작업 카드의 미리보기용.
    지운(휴지통) 결과물은 뺀다."""
    ids = [j for j in set(job_ids) if j]
    if not ids:
        return {}
    out = {j: [] for j in ids}
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT job_id, path, kind, nsfw FROM assets WHERE deleted_at IS NULL AND job_id IN "
            f"({','.join('?' * len(ids))}) ORDER BY created_at DESC, id DESC", ids).fetchall()
    for r in rows:
        lst = out[r["job_id"]]
        if len(lst) < per_job:
            lst.append({"path": r["path"], "kind": r["kind"], "nsfw": r["nsfw"]})
    return out
