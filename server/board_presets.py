"""
보드 생성 카드의 프리셋(board_presets 표, db.py SCHEMA_V13 참고) — 회원별로 "이 템플릿 + 이
워크플로우 + 고정 옵션 값 + 카드에 꺼내 둘 옵션 이름들"을 저장한다. 검증(템플릿이 있는지,
옵션 이름이 템플릿 것인지 등)은 app.py가 하고, 여기서는 owner_id로 좁힌 읽기/쓰기만 한다.
워크플로우 JSON은 목록에서는 빼고(크다) 하나를 꺼낼 때만 돌려준다.
"""

import json

import db

_LIST_COLS = "id, owner_id, name, template_id, options_json, exposed_json, lora_trigger, created_at, updated_at"


def _public(row, with_workflow: bool) -> dict:
    d = dict(row)
    d["options"] = json.loads(d.pop("options_json") or "{}")
    d["exposed"] = json.loads(d.pop("exposed_json") or "[]")
    if with_workflow:
        wf, vwf = d.pop("workflow_json", None), d.pop("video_workflow_json", None)
        d["workflow"] = json.loads(wf) if wf else None
        d["video_workflow"] = json.loads(vwf) if vwf else None
    return d


def list_presets(owner_id: int) -> list:
    with db.connect() as conn:
        rows = conn.execute(f"SELECT {_LIST_COLS} FROM board_presets WHERE owner_id=? ORDER BY name COLLATE NOCASE, id",
                            (owner_id,)).fetchall()
    return [_public(r, False) for r in rows]


def get_preset(owner_id: int, preset_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM board_presets WHERE id=? AND owner_id=?", (preset_id, owner_id)).fetchone()
    return _public(row, True) if row else None


def create_preset(owner_id: int, fields: dict) -> dict:
    now = db.now_iso()
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO board_presets(owner_id, name, template_id, workflow_json, video_workflow_json, options_json, "
            "exposed_json, lora_trigger, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (owner_id, fields["name"], fields["template_id"],
             json.dumps(fields["workflow"]) if fields.get("workflow") is not None else None,
             json.dumps(fields["video_workflow"]) if fields.get("video_workflow") is not None else None,
             json.dumps(fields.get("options") or {}), json.dumps(fields.get("exposed") or []),
             fields.get("lora_trigger") or "", now, now))
        preset_id = cur.lastrowid
    return get_preset(owner_id, preset_id)


def update_preset(owner_id: int, preset_id: int, fields: dict) -> dict | None:
    """넘겨준 것만 바꾼다(이름만 바꾸기 · 같은 이름으로 다시 저장해 내용 전체 덮어쓰기 둘 다 여기로)."""
    sets, params = [], []
    for key in ("name", "template_id"):
        if key in fields:
            sets.append(f"{key}=?")
            params.append(fields[key])
    if "lora_trigger" in fields:
        sets.append("lora_trigger=?")
        params.append(fields["lora_trigger"] or "")
    for key, col in (("workflow", "workflow_json"), ("video_workflow", "video_workflow_json")):
        if key in fields:
            sets.append(f"{col}=?")
            params.append(json.dumps(fields[key]) if fields[key] is not None else None)
    for key, col in (("options", "options_json"), ("exposed", "exposed_json")):
        if key in fields:
            sets.append(f"{col}=?")
            params.append(json.dumps(fields[key]))
    if sets:
        sets.append("updated_at=?")
        params.append(db.now_iso())
        with db.connect() as conn:
            cur = conn.execute(f"UPDATE board_presets SET {', '.join(sets)} WHERE id=? AND owner_id=?",
                               (*params, preset_id, owner_id))
            if cur.rowcount == 0:
                return None
    return get_preset(owner_id, preset_id)


def delete_preset(owner_id: int, preset_id: int) -> bool:
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM board_presets WHERE id=? AND owner_id=?", (preset_id, owner_id))
        return cur.rowcount > 0
