"""
모델 등록부 — 파일 종류(체크포인트/디퓨전 모델/LoRA/VAE/텍스트 인코더/...)별로 사람이 붙이는 정보.

ComfyUI의 /object_info는 "설치된 파일 이름"만 준다. 그 파일이 어떤 베이스 모델용인지, 소개 페이지와
받을 주소는 어디인지, LoRA면 트리거 키워드가 뭔지는 적어 둘 곳이 없어서 (종류, 파일명)을 열쇠로
DB(models 테이블)에 둔다. 파드와 무관한 기준 데이터다 — 어느 파드에 그 파일이 있는지는 상관없고,
어느 파드에서든 이 정보를 보고 모델을 받거나 트리거 키워드를 쓴다. 파일이 실제로 놓이는 곳만 파드다.

필드: base_models(소속 베이스 모델 목록, 연결 표 model_base_models — 새 작업 마법사가 이 값으로 체크포인트를 묶고
LoRA를 걸러낸다. base_model은 그 첫 값으로 호환용), page_url(소개 페이지),
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
    ("ultralytics", "얼굴 탐지 모델(ultralytics)"),
    ("custom_nodes", "노드팩"),
]
KIND_IDS = {k for k, _ in KINDS}
# 노드팩은 파일이 아니라 저장소다 — 파일명 칸은 custom_nodes 아래 폴더 이름, 받을 주소 칸은 github 저장소 주소.
NODEPACK_KIND = "custom_nodes"
GITHUB_REPO_RE = re.compile(r"^https://github\.com/[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$")
NODEPACK_DIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

MAX_TEXT = 4000
MAX_LIST = 50
FIELDS = ("base_model", "base_models", "notes", "tags", "trigger_keyword", "page_url", "download_url")


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


def _links(conn, kind: str | None = None, filename: str | None = None) -> dict[tuple, list[str]]:
    """{(kind, filename): [베이스 모델, ...]} — 연결 표(model_base_models, db.py v18)를 순서대로 읽는다."""
    sql, args = "SELECT kind, filename, base_model FROM model_base_models", ()
    if kind is not None:
        sql, args = sql + " WHERE kind=? AND filename=?", (kind, filename)
    out: dict[tuple, list[str]] = {}
    for r in conn.execute(sql + " ORDER BY position, rowid", args).fetchall():
        out.setdefault((r["kind"], r["filename"]), []).append(r["base_model"])
    return out


def _row_to_entry(row, links: dict) -> dict:
    bases = links.get((row["kind"], row["filename"]), [])
    return {
        "kind": row["kind"],
        "filename": row["filename"],
        "base_model": bases[0] if bases else "",   # 호환용 — 첫 번째 베이스 모델
        "base_models": bases,
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
        links = _links(conn)
    return [_row_to_entry(r, links) for r in rows]


def get_entry(kind: str, filename: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM models WHERE kind=? AND filename=?", (kind, filename)).fetchone()
        return _row_to_entry(row, _links(conn, kind, filename)) if row else None


# 베이스 모델 목록(base_models 표, db.py v17) — 화면의 베이스 모델 선택지. 이름은 대소문자 무시로 하나뿐이다.
def list_base_models() -> list[dict]:
    """[{name, count}] — count는 그 이름(대소문자 무시)에 속한 등록부 항목 수."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT b.name, (SELECT COUNT(*) FROM model_base_models l WHERE l.base_model = b.name) AS count "
            "FROM base_models b ORDER BY b.position, b.name COLLATE NOCASE").fetchall()
    return [{"name": r["name"], "count": r["count"]} for r in rows]


def _clean_base_name(value) -> str:
    name = _clean_str(value, "베이스 모델 이름", 100)
    if not name:
        raise RegistryError("베이스 모델 이름을 적어 주세요.")
    return name


def add_base_model(name) -> str:
    name = _clean_base_name(name)
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM base_models WHERE name=?", (name,)).fetchone():
            raise RegistryError(f"'{name}'은(는) 이미 있어요.")
        conn.execute("INSERT INTO base_models(name, position, created_at) "
                     "VALUES(?, (SELECT COALESCE(MAX(position), 0) + 1 FROM base_models), ?)", (name, db.now_iso()))
    return name


def rename_base_model(old, new) -> str:
    """목록 이름과 그 이름에 속한 연결(과 models.base_model 사본)을 한 트랜잭션에서 함께 바꾼다."""
    old, new = _clean_base_name(old), _clean_base_name(new)
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM base_models WHERE name=?", (old,)).fetchone():
            raise RegistryError(f"'{old}'은(는) 목록에 없어요.")
        if old.lower() != new.lower() and conn.execute("SELECT 1 FROM base_models WHERE name=?", (new,)).fetchone():
            raise RegistryError(f"'{new}'은(는) 이미 있어요.")
        conn.execute("UPDATE base_models SET name=? WHERE name=?", (new, old))
        # 목록 밖 값으로 이미 new에도 묶인 항목은 겹치지 않게 old 연결만 지운다
        conn.execute("UPDATE OR IGNORE model_base_models SET base_model=? WHERE base_model=?", (new, old))
        conn.execute("DELETE FROM model_base_models WHERE base_model=? AND base_model<>?", (old, new))
        conn.execute("UPDATE models SET base_model=?, updated_at=? WHERE base_model=? COLLATE NOCASE",
                     (new, db.now_iso(), old))
    return new


def delete_base_model(name) -> None:
    """쓰는 등록부 항목이 있으면 지우지 않는다."""
    name = _clean_base_name(name)
    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM base_models WHERE name=?", (name,)).fetchone():
            raise RegistryError(f"'{name}'은(는) 목록에 없어요.")
        used = conn.execute("SELECT COUNT(*) FROM model_base_models WHERE base_model=?", (name,)).fetchone()[0]
        if used:
            raise RegistryError(f"모델 {used}개가 '{name}'을(를) 쓰고 있어요. 그 모델들의 베이스 모델을 먼저 바꿔 주세요.")
        conn.execute("DELETE FROM base_models WHERE name=?", (name,))


def reorder_base_models(names) -> list[str]:
    """names 순서대로 position을 1부터 다시 매긴다. 목록의 모든 이름이 한 번씩 있어야 한다."""
    if not isinstance(names, list):
        raise RegistryError("names는 이름 목록이어야 해요.")
    names = [_clean_base_name(n) for n in names]
    with db.connect() as conn:
        current = [r["name"] for r in conn.execute("SELECT name FROM base_models").fetchall()]
        have = {n.lower(): n for n in current}
        given = [n.lower() for n in names]
        if len(set(given)) != len(given):
            raise RegistryError("같은 이름이 두 번 있어요.")
        unknown = [n for n in names if n.lower() not in have]
        if unknown:
            raise RegistryError(f"목록에 없는 이름이에요: {', '.join(unknown)}")
        missing = [n for n in current if n.lower() not in set(given)]
        if missing:
            raise RegistryError(f"빠진 이름이 있어요: {', '.join(missing)}")
        conn.executemany("UPDATE base_models SET position=? WHERE name=?",
                         [(i, have[n.lower()]) for i, n in enumerate(names, 1)])
    return [have[n.lower()] for n in names]


def lora_triggers() -> dict[str, dict]:
    """{파일명: {trigger, base_id, base_ids}} — 마법사/워크플로우 탭이 쓴다. base_ids가 비어 있으면 어떤 베이스
    모델에나 보인다. base_id(첫 값)는 예전 화면 호환용이다."""
    out = {}
    for e in list_entries():
        if e["kind"] == "loras" and (e["trigger_keyword"] or e["base_models"]):
            ids = [base_id(b) for b in e["base_models"]]
            out[e["filename"]] = {"trigger": e["trigger_keyword"], "base_id": ids[0] if ids else "", "base_ids": ids}
    return out


# 전용 빌더를 쓰거나(krea.2·MiniMax-H3) 영상 모델(Wan)이라 범용 UNet 이미지 빌더 대상이 아닌 family.
UNET_IMAGE_EXCLUDED = ("krea.2", "minimax-h3")
UNET_IMAGE_EXCLUDED_PREFIXES = ("wan",)
# 등록부에 같은 베이스 모델로 지정된 부품이 없을 때 쓰는 family별 기본 파일(출처: huggingface.co/circlestone-labs/Anima).
# 공유 VAE처럼 다른 베이스 모델로 등록된 파일도 쓸 수 있게 한다. 등록부 지정이 언제나 우선이다.
KNOWN_UNET_IMAGE_PARTS = {"anima": {"clip": "qwen_3_06b_base.safetensors", "vae": "qwen_image_vae.safetensors"}}


def unet_image_part_sources(gid: str) -> dict | None:
    """unet_image_parts와 같은 결정에 출처를 붙인다: {clip|vae: {filename, source: registry|default}}."""
    if not gid or gid in UNET_IMAGE_EXCLUDED or gid.startswith(UNET_IMAGE_EXCLUDED_PREFIXES):
        return None
    out = {k: {"filename": f, "source": "default"} for k, f in KNOWN_UNET_IMAGE_PARTS.get(gid, {}).items()}
    found: set[str] = set()
    for e in list_entries():
        if e["kind"] in ("text_encoders", "vae") and gid in {base_id(b) for b in e["base_models"]}:
            part = "clip" if e["kind"] == "text_encoders" else "vae"
            if part not in found:
                found.add(part)
                out[part] = {"filename": e["filename"], "source": "registry"}
    return out


def unet_image_parts(gid: str) -> dict | None:
    """diffusion_models family가 범용 UNet+CLIP+VAE 이미지 빌더(workflow_builder_unet.py)에 쓸 부품
    {clip, vae}를 돌려준다. 같은 베이스 모델로 등록된 텍스트 인코더·VAE 파일을 먼저 쓰고, 없는 부품은
    KNOWN_UNET_IMAGE_PARTS로 채운다. 그래도 모자라면 찾은 것만 담긴다. 제외 family는 None이다."""
    parts = unet_image_part_sources(gid)
    return None if parts is None else {k: v["filename"] for k, v in parts.items()}


def base_model_detail(name) -> dict:
    """베이스 모델 세부 설정 모달용 — {name, members: {종류: [파일명]}, assembly}.
    assembly는 소속 UNet(diffusion_models)이 있을 때만 채운다: {unet: [...], parts: {clip, vae}(출처 포함),
    missing: [...], dedicated: 전용 빌더/영상 family라 범용 조립 대상이 아님}. 체크포인트는 부품이 파일 안에 있어 None."""
    name = _clean_base_name(name)
    with db.connect() as conn:
        row = conn.execute("SELECT name FROM base_models WHERE name=?", (name,)).fetchone()
    name = row["name"] if row else name   # 목록 밖 값(옛 등록부)도 소속 모델은 보여 준다
    members: dict[str, list[str]] = {}
    for e in list_entries():
        if name.lower() in (b.lower() for b in e["base_models"]):
            members.setdefault(e["kind"], []).append(e["filename"])
    assembly = None
    if members.get("diffusion_models"):
        parts = unet_image_part_sources(base_id(name))
        assembly = {"unet": members["diffusion_models"], "parts": parts or {},
                    "missing": [] if parts is None else [k for k in ("clip", "vae") if k not in parts],
                    "dedicated": parts is None}
    return {"name": name, "listed": bool(row), "members": members, "assembly": assembly}


def set_base_membership(name, kind: str, filename: str, member: bool) -> dict | None:
    """등록부 항목 하나를 베이스 모델에 넣거나 뺀다(연결만 바꾼다). 빼서 다른 정보도 없으면 upsert 규칙대로
    행이 지워지고 None이다."""
    name = _clean_base_name(name)
    entry = get_entry(kind, filename) or {"base_models": []}
    bases = [b for b in entry["base_models"] if b.lower() != name.lower()]
    if member:
        with db.connect() as conn:
            row = conn.execute("SELECT name FROM base_models WHERE name=?", (name,)).fetchone()
        if not row:
            raise RegistryError(f"'{name}'은(는) 목록에 없어요.")
        bases.append(row["name"])
    elif len(bases) == len(entry["base_models"]):
        raise RegistryError(f"'{filename}'은(는) '{name}'에 속해 있지 않아요.")
    return upsert(kind, filename, {"base_models": bases})


def checkpoint_groups(installed: set[str] | None = None) -> dict[str, dict]:
    """마법사 1단계용 — 체크포인트/디퓨전 모델(UNet)을 소속된 베이스 모델마다 묶는다(여럿이면 모두에 들어간다):
    {id: {label, kind, checkpoints}}. kind는 "checkpoints"(CheckpointLoaderSimple 조립) 또는
    "diffusion_models"(UNETLoader 기반 — krea.2/MiniMax-H3처럼 전용 빌더가 조립하는 family).
    installed를 주면 그 안에 있는(=실제로 쓸 수 있는) 파일만 남긴다."""
    groups: dict[str, dict] = {}
    for e in list_entries():
        if e["kind"] not in ("checkpoints", "diffusion_models"):
            continue
        if installed is not None and e["filename"] not in installed:
            continue
        for base in e["base_models"]:
            gid = base_id(base)
            if not gid:
                continue
            # prompt_style: 전처리(Prompt Enhance)가 모드를 자동으로 고르는 기준. UNet 기반 family(krea.2/MiniMax-H3)는
            # 자연어, 체크포인트 기반(SDXL 계열)은 Danbooru 태그로 본다.
            g = groups.setdefault(gid, {"label": base, "kind": e["kind"], "checkpoints": [],
                                        "prompt_style": "danbooru" if e["kind"] == "checkpoints" else "natural"})
            g["checkpoints"].append(e["filename"])
    for gid, g in groups.items():
        parts = unet_image_parts(gid) if g["kind"] == "diffusion_models" else None
        if parts is None:
            continue
        missing = [k for k in ("clip", "vae") if k not in parts]
        if missing:  # 마법사가 범용 베이스를 숨기지 않고 무엇이 빠졌는지 안내한다
            g["unet_image_missing"] = missing
        else:
            g["unet_image"] = parts
    # 베이스 모델 목록(Settings 탭)의 position 순서. 목록 밖 값은 뒤에 이름순으로 둔다.
    order = {base_id(b["name"]): i for i, b in enumerate(list_base_models())}
    return dict(sorted(groups.items(), key=lambda kv: (order.get(kv[0], len(order)), kv[1]["label"].lower())))


def upsert(kind: str, filename: str, fields: dict) -> dict | None:
    """넘겨준 필드만 바꾼다(안 넘긴 필드는 그대로). 결과가 완전히 비면 지우고 None을 돌려준다."""
    if kind not in KIND_IDS:
        raise RegistryError("알 수 없는 모델 종류예요.")
    filename = (filename or "").strip()
    if not filename or len(filename) > 500:
        raise RegistryError("파일명이 올바르지 않아요.")
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM models WHERE kind=? AND filename=?", (kind, filename)).fetchone()
        current = _row_to_entry(row, _links(conn, kind, filename)) if row else {
            "base_model": "", "base_models": [], "notes": "", "tags": [], "trigger_keyword": "",
            "page_url": "", "download_url": ""}
        # base_models(목록)가 우선이고, 예전처럼 base_model 문자열만 오면 [값]으로 본다.
        if "base_models" in fields or "base_model" in fields:
            raw = fields["base_models"] if "base_models" in fields else [fields["base_model"] or ""]
            if raw is None:
                raw = []
            if not isinstance(raw, list):
                raise RegistryError("베이스 모델은 목록이어야 해요.")
            bases, seen = [], set()
            for v in raw:
                v = _clean_str(v, "베이스 모델", 100)
                if v and v.lower() not in seen:   # 연결 표 키가 대소문자 무시라 하나로 합친다
                    seen.add(v.lower())
                    bases.append(v)
            if len(bases) > MAX_LIST:
                raise RegistryError(f"베이스 모델은 최대 {MAX_LIST}개까지예요.")
            current["base_models"] = bases
            current["base_model"] = bases[0] if bases else ""
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
        if kind == NODEPACK_KIND:
            if not NODEPACK_DIR_RE.match(filename):
                raise RegistryError("노드팩 폴더 이름은 영문·숫자·._-만 쓸 수 있어요.")
            # 파드 시작 스크립트(셸)에 들어가는 값이라 github 저장소 주소 형식만 받는다.
            if current["download_url"] and not GITHUB_REPO_RE.match(current["download_url"]):
                raise RegistryError("노드팩 주소는 https://github.com/소유자/저장소 형식이어야 해요.")
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
        conn.execute("DELETE FROM model_base_models WHERE kind=? AND filename=?", (kind, filename))
        conn.executemany("INSERT INTO model_base_models(kind, filename, base_model, position) VALUES(?,?,?,?)",
                         [(kind, filename, b, i) for i, b in enumerate(current["base_models"])])
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
