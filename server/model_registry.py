"""
모델 등록부 — 파일 종류(체크포인트/디퓨전 모델/LoRA/VAE/텍스트 인코더/...)별로 사람이 붙이는 정보.

ComfyUI의 /object_info는 "설치된 파일 이름"만 준다. 그 파일이 어떤 베이스 모델용인지, 소개 페이지와
받을 주소는 어디인지, LoRA면 트리거 키워드가 뭔지는 적어 둘 곳이 없어서 (종류, 파일명)을 열쇠로
DB(models 테이블)에 둔다. 파드와 무관한 기준 데이터다 — 어느 파드에 그 파일이 있는지는 상관없고,
어느 파드에서든 이 정보를 보고 모델을 받거나 트리거 키워드를 쓴다. 파일이 실제로 놓이는 곳만 파드다.

필드: base_model(베이스 모델 — 새 작업 마법사가 이 값으로 체크포인트를 묶고 LoRA를 걸러낸다), page_url(소개 페이지),
download_url(파일 받을 주소), trigger_keyword(LoRA), tags, notes.

정보를 하나도 안 채운 항목은 행을 만들지 않는다(비우면 지운다).
"""

import json
import re
import urllib.parse
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
BASE_MODELS = ["SD 1.5", "SDXL", "Illustrious", "Pony", "NoobAI", "Flux", "Wan 2.2", "Wan 2.1", "Qwen-Image", "Z-Image", "krea.2", "MiniMax-H3", "기타"]

MAX_TEXT = 4000
MAX_LIST = 50
FIELDS = ("base_model", "notes", "tags", "trigger_keyword", "page_url", "download_url")


def base_id(base_model: str) -> str:
    """베이스 모델 값의 식별자 — 워크플로우 프리셋 파일 이름(<id>__<유형>.json)과 마법사가 쓴다."""
    return re.sub(r"[^a-z0-9_.-]+", "-", (base_model or "").strip().lower()).strip("-.")


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


def _clean_url(value, field: str) -> str:
    """화면이 링크로 그리는 값이라 http(s)만 허용한다(javascript: 같은 것이 href로 들어가면 안 된다)."""
    value = _clean_str(value, field, 1000)
    if not value:
        return ""
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise RegistryError(f"{field}은(는) http:// 또는 https://로 시작하는 주소여야 해요.")
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
        "base_model": row["base_model"],
        "notes": row["notes"],
        "tags": json.loads(row["tags_json"] or "[]"),
        "trigger_keyword": row["trigger_keyword"],
        "page_url": row["page_url"],
        "download_url": row["download_url"],
        "updated_at": row["updated_at"],
    }


def list_entries() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM models ORDER BY kind, filename COLLATE NOCASE").fetchall()
    return [_row_to_entry(r) for r in rows]


def get_entry(kind: str, filename: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM models WHERE kind=? AND filename=?", (kind, filename)).fetchone()
    return _row_to_entry(row) if row else None


def lora_triggers() -> dict[str, dict]:
    """{파일명: {trigger, base_id}} — 마법사/워크플로우 탭이 쓴다. base_id가 비어 있으면 어떤 베이스 모델에나 보인다."""
    out = {}
    for e in list_entries():
        if e["kind"] == "loras" and (e["trigger_keyword"] or e["base_model"]):
            out[e["filename"]] = {"trigger": e["trigger_keyword"], "base_id": base_id(e["base_model"])}
    return out


def checkpoint_groups(installed: set[str] | None = None) -> dict[str, dict]:
    """마법사 1단계용 — base_model이 있는 체크포인트/디퓨전 모델(UNet)을 그 값으로 묶는다:
    {id: {label, kind, checkpoints}}. kind는 "checkpoints"(CheckpointLoaderSimple 조립) 또는
    "diffusion_models"(UNETLoader 기반 — krea.2/MiniMax-H3처럼 전용 빌더가 조립하는 family).
    installed를 주면 그 안에 있는(=실제로 쓸 수 있는) 파일만 남긴다."""
    groups: dict[str, dict] = {}
    for e in list_entries():
        if e["kind"] not in ("checkpoints", "diffusion_models") or not e["base_model"]:
            continue
        if installed is not None and e["filename"] not in installed:
            continue
        gid = base_id(e["base_model"])
        if not gid:
            continue
        g = groups.setdefault(gid, {"label": e["base_model"], "kind": e["kind"], "checkpoints": []})
        g["checkpoints"].append(e["filename"])
    return dict(sorted(groups.items(), key=lambda kv: kv[1]["label"].lower()))


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
            "base_model": "", "notes": "", "tags": [], "trigger_keyword": "",
            "page_url": "", "download_url": ""}
        if "base_model" in fields:
            current["base_model"] = _clean_str(fields["base_model"], "베이스 모델", 100)
        if "notes" in fields:
            current["notes"] = _clean_str(fields["notes"], "메모")
        if "tags" in fields:
            current["tags"] = _clean_list(fields["tags"], "태그")
        if "trigger_keyword" in fields:
            current["trigger_keyword"] = _clean_str(fields["trigger_keyword"], "트리거 키워드", 1000)
        if "page_url" in fields:
            current["page_url"] = _clean_url(fields["page_url"], "페이지 주소")
        if "download_url" in fields:
            current["download_url"] = _clean_url(fields["download_url"], "다운로드 주소")
        if kind != "loras":   # 트리거 키워드는 LoRA만의 개념이다
            current["trigger_keyword"] = ""
        empty = not (current["base_model"] or current["notes"] or current["tags"] or current["trigger_keyword"]
                     or current["page_url"] or current["download_url"])
        if empty:
            conn.execute("DELETE FROM models WHERE kind=? AND filename=?", (kind, filename))
            return None
        updated = db.now_iso()
        conn.execute(
            "INSERT INTO models(kind, filename, base_model, notes, tags_json, trigger_keyword, "
            "page_url, download_url, updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(kind, filename) DO UPDATE SET "
            "base_model=excluded.base_model, notes=excluded.notes, tags_json=excluded.tags_json, "
            "trigger_keyword=excluded.trigger_keyword, "
            "page_url=excluded.page_url, download_url=excluded.download_url, updated_at=excluded.updated_at",
            (kind, filename, current["base_model"], current["notes"],
             json.dumps(current["tags"], ensure_ascii=False), current["trigger_keyword"],
             current["page_url"], current["download_url"], updated),
        )
    current.update({"kind": kind, "filename": filename, "updated_at": updated})
    return current


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
                if upsert("loras", name, {"trigger_keyword": value.get("trigger") or ""}):
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
