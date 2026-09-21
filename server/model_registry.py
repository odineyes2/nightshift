"""
모델 등록부 — 파일 종류(체크포인트/디퓨전 모델/LoRA/VAE/텍스트 인코더/...)별로 사람이 붙이는 정보.

ComfyUI의 /object_info는 "설치된 파일 이름"만 준다. 그 파일이 어떤 계열(SDXL, Flux, Wan2.2...)
인지, 어디서 받았는지, LoRA면 트리거 워드가 뭔지는 적어 둘 곳이 없어서 (종류, 파일명)을 열쇠로
DB(models 테이블)에 둔다. 설치 여부는 파드마다 다르지만 등록부는 파일명 기준이라 파드와 무관하다.

정보를 하나도 안 채운 항목은 행을 만들지 않는다(비우면 지운다) — "설치돼 있지만 등록 안 함"과
"등록했다가 다 비움"이 같은 상태가 되게 하려는 것이다.
"""

import json
from pathlib import Path

import db

# 등록부가 다루는 종류 — 키는 MODEL_LIST_SOURCES(app.py)의 키와 같다.
KINDS = [
    ("checkpoints", "체크포인트"),
    ("diffusion_models", "디퓨전 모델(UNet)"),
    ("loras", "LoRA"),
    ("vae", "VAE"),
    ("text_encoders", "텍스트 인코더(CLIP)"),
    ("clip_vision", "CLIP Vision"),
    ("controlnet", "ControlNet"),
    ("upscale_models", "업스케일러"),
]
KIND_IDS = {k for k, _ in KINDS}

# 입력칸의 자동완성용 — 이 밖의 값도 자유롭게 적을 수 있다.
ARCHITECTURES = ["SD1.5", "SDXL", "Illustrious", "Pony", "Flux", "Wan2.2", "Wan2.1", "Qwen-Image", "Z-Image", "기타"]

MAX_TEXT = 4000
MAX_LIST = 50


class RegistryError(ValueError):
    pass


def _clean_str(value, field: str, limit: int = MAX_TEXT) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RegistryError(f"{field}은(는) 문자열이어야 해요.")
    value = value.strip()
    if len(value) > limit:
        raise RegistryError(f"{field}이(가) 너무 길어요(최대 {limit}자).")
    return value


def _clean_list(value, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise RegistryError(f"{field}은(는) 문자열 목록이어야 해요.")
    seen, out = set(), []
    for v in value:
        v = v.strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    if len(out) > MAX_LIST:
        raise RegistryError(f"{field}은(는) 최대 {MAX_LIST}개까지예요.")
    return out


def _row_to_entry(row) -> dict:
    return {
        "kind": row["kind"],
        "filename": row["filename"],
        "architecture": row["architecture"],
        "notes": row["notes"],
        "tags": json.loads(row["tags_json"] or "[]"),
        "trigger": row["trigger"],
        "families": json.loads(row["families_json"] or "[]"),
        "source_url": row["source_url"],
        "updated_at": row["updated_at"],
    }


def list_entries() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM models ORDER BY kind, filename COLLATE NOCASE").fetchall()
    return [_row_to_entry(r) for r in rows]


def lora_triggers() -> dict[str, dict]:
    """예전 lora_triggers.json과 같은 모양 {파일명: {trigger, families}} — 마법사/워크플로우 탭이 쓴다."""
    out = {}
    for e in list_entries():
        if e["kind"] == "loras" and (e["trigger"] or e["families"]):
            out[e["filename"]] = {"trigger": e["trigger"], "families": e["families"]}
    return out


def upsert(kind: str, filename: str, fields: dict) -> dict | None:
    """넘겨준 필드만 바꾼다(안 넘긴 필드는 그대로). 결과가 완전히 비면 지우고 None을 돌려준다."""
    if kind not in KIND_IDS:
        raise RegistryError("알 수 없는 모델 종류예요.")
    filename = (filename or "").strip()
    if not filename or len(filename) > 500:
        raise RegistryError("파일명이 올바르지 않아요.")
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM models WHERE kind=? AND filename=?", (kind, filename)).fetchone()
        current = _row_to_entry(row) if row else {
            "architecture": "", "notes": "", "tags": [], "trigger": "", "families": [], "source_url": ""}
        if "architecture" in fields:
            current["architecture"] = _clean_str(fields["architecture"], "계열", 100)
        if "notes" in fields:
            current["notes"] = _clean_str(fields["notes"], "메모")
        if "tags" in fields:
            current["tags"] = _clean_list(fields["tags"], "태그")
        if "trigger" in fields:
            current["trigger"] = _clean_str(fields["trigger"], "트리거 워드", 1000)
        if "families" in fields:
            current["families"] = _clean_list(fields["families"], "호환 베이스 모델")
        if "source_url" in fields:
            current["source_url"] = _clean_str(fields["source_url"], "출처 주소", 1000)
        if kind != "loras":   # 트리거 워드와 호환 베이스 모델은 LoRA만의 개념이다
            current["trigger"] = ""
            current["families"] = []
        empty = not (current["architecture"] or current["notes"] or current["tags"]
                     or current["trigger"] or current["families"] or current["source_url"])
        if empty:
            conn.execute("DELETE FROM models WHERE kind=? AND filename=?", (kind, filename))
            return None
        updated = db.now_iso()
        conn.execute(
            "INSERT INTO models(kind, filename, architecture, notes, tags_json, trigger, families_json, source_url, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(kind, filename) DO UPDATE SET "
            "architecture=excluded.architecture, notes=excluded.notes, tags_json=excluded.tags_json, "
            "trigger=excluded.trigger, families_json=excluded.families_json, source_url=excluded.source_url, "
            "updated_at=excluded.updated_at",
            (kind, filename, current["architecture"], current["notes"],
             json.dumps(current["tags"], ensure_ascii=False), current["trigger"],
             json.dumps(current["families"], ensure_ascii=False), current["source_url"], updated),
        )
    current.update({"kind": kind, "filename": filename, "updated_at": updated})
    return current


def remove_family(family_id: str) -> None:
    """베이스 모델(family)을 지우면 LoRA들의 호환 목록에서도 뺀다."""
    with db.connect() as conn:
        rows = conn.execute("SELECT kind, filename, families_json FROM models WHERE families_json != '[]'").fetchall()
        for r in rows:
            families = json.loads(r["families_json"])
            if family_id in families:
                families = [f for f in families if f != family_id]
                conn.execute("UPDATE models SET families_json=?, updated_at=? WHERE kind=? AND filename=?",
                             (json.dumps(families, ensure_ascii=False), db.now_iso(), r["kind"], r["filename"]))


def import_legacy_lora_triggers(state_file: Path) -> int:
    """lora_triggers.json({파일명: 문자열 또는 {trigger, families}})을 한 번만 등록부로 옮긴다.
    원본은 .migrated로 보존한다(다른 예전 파일 이관과 같은 방식)."""
    with db.connect() as conn:
        if db.get_meta(conn, "legacy_lora_triggers_imported"):
            return 0
    count = 0
    if state_file.exists():
        try:
            with open(state_file, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            raw = {}
        for name, value in (raw.items() if isinstance(raw, dict) else []):
            if isinstance(value, str):
                value = {"trigger": value}
            if not isinstance(value, dict):
                continue
            try:
                if upsert("loras", name, {"trigger": value.get("trigger") or "",
                                          "families": value.get("families") or []}):
                    count += 1
            except RegistryError:
                continue
    with db.connect() as conn:
        db.set_meta(conn, "legacy_lora_triggers_imported", db.now_iso())
    if state_file.exists():
        target = state_file.with_name(state_file.name + ".migrated")
        try:
            if target.exists():
                target.unlink()
            state_file.rename(target)
        except OSError:
            pass
    return count
