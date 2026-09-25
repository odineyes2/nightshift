"""
프로젝트 보드(무한 캔버스) — 이미지/영상/텍스트 카드(board_nodes)와 그 사이 연결선
(board_edges). 둘 다 project_id로 스코프되고, 프로젝트가 지워지면 같이 지워진다
(ON DELETE CASCADE, db.py의 SCHEMA_V10 참고 — jobs/assets와 달리 "미분류"로 남을
의미가 없어서다). 소유권 확인(이 프로젝트가 요청한 회원 것인지)은 app.py가
project_or_404()로 이미 하므로, 여기서는 project_id로 좁히는 조회/수정만 한다.
"""

import db


def _row(r) -> dict:
    return dict(r)


def list_board(project_id: int) -> dict:
    """이 프로젝트의 노드+연결선 전체(GET .../board)."""
    with db.connect() as conn:
        nodes = conn.execute(
            "SELECT * FROM board_nodes WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
        edges = conn.execute(
            "SELECT * FROM board_edges WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
    return {"nodes": [_row(r) for r in nodes], "edges": [_row(r) for r in edges]}


def create_node(project_id: int, kind: str, asset_path: str | None, text: str,
                x: float, y: float, width: float | None, height: float | None) -> dict:
    now = db.now_iso()
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO board_nodes(project_id, kind, asset_path, text, x, y, width, height, z_index, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, kind, asset_path, text, x, y,
             width if width is not None else 220, height if height is not None else 220,
             0, now, now),
        )
        node_id = cur.lastrowid
        row = conn.execute("SELECT * FROM board_nodes WHERE id=?", (node_id,)).fetchone()
    return _row(row)


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
    if not sets:
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM board_nodes WHERE id=?", (node_id,)).fetchone()
        return _row(row) if row else None
    sets.append("updated_at=?")
    params.append(db.now_iso())
    with db.connect() as conn:
        cur = conn.execute(f"UPDATE board_nodes SET {', '.join(sets)} WHERE id=?", (*params, node_id))
        if cur.rowcount == 0:
            return None
        row = conn.execute("SELECT * FROM board_nodes WHERE id=?", (node_id,)).fetchone()
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
