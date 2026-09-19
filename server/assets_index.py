"""
결과물(이미지/영상) 색인 — 디스크의 파일을 DB assets 테이블에 맞춘다.

파일이 진실이고 DB는 색인이다. sync()는 출력 폴더를 훑어서
  - 새 파일은 등록한다(job 하위 폴더면 그 job의 프로젝트를 물려받는다),
  - 크기/수정 시각이 바뀐 파일(회전 등)은 갱신하고,
  - 사라진 파일은 deleted_at을 찍는다(다시 나타나면 되살린다).
갤러리 목록 API가 4초마다 불러도 부담이 없게 짧은 간격 안의 재호출은 건너뛴다.

ComfyUI가 PNG에 넣어주는 프롬프트 그래프에서 시드/프롬프트/체크포인트를 best-effort로
뽑는다(형식이 달라 못 읽으면 그 칸만 비운다 — 등록 자체는 항상 성공한다).
"""

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

import db
from output_images import OUTPUT_DIR, OutputFolderError, list_output_images
from output_videos import list_output_videos

MIN_SYNC_INTERVAL = 5.0
_sync_lock = threading.Lock()
_last_sync = 0.0

_SAMPLER_TYPES = ("KSampler", "KSamplerAdvanced", "SamplerCustom", "SamplerCustomAdvanced")


def _collect_texts(graph: dict, ref, out: list, seen: set, depth: int = 0) -> None:
    """conditioning 연결을 거슬러 올라가며 프롬프트 문자열을 모은다. 프롬프트를
    ConditioningConcat/Combine으로 여러 조각을 이어 붙이는 워크플로우가 많아서, 하나만
    찾지 않고 연결된 텍스트 인코더를 전부 순서대로 모은다."""
    if depth > 12 or not isinstance(ref, list) or not ref:
        return
    nid = str(ref[0])
    if nid in seen:
        return
    seen.add(nid)
    node = graph.get(nid)
    if not isinstance(node, dict):
        return
    inputs = node.get("inputs") or {}
    text = inputs.get("text")
    if isinstance(text, str) and text.strip():
        out.append(text.strip())
    elif isinstance(text, list):
        _collect_texts(graph, text, out, seen, depth + 1)
    else:
        for key in ("value", "string"):
            if isinstance(inputs.get(key), str) and inputs[key].strip():
                out.append(inputs[key].strip())
                break
    for key, val in inputs.items():
        if key != "text" and isinstance(val, list) and ("conditioning" in key or key in ("positive", "negative")):
            _collect_texts(graph, val, out, seen, depth + 1)


def _prompt_for(graph: dict, ref) -> str | None:
    out: list = []
    _collect_texts(graph, ref, out, set())
    return ", ".join(out) if out else None


def _int_or_none(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def extract_comfy_meta(img: Image.Image) -> dict:
    try:
        raw = (getattr(img, "text", None) or img.info or {}).get("prompt")
        if not raw:
            return {}
        graph = json.loads(raw)
        if not isinstance(graph, dict):
            return {}
        nodes = [n for n in graph.values() if isinstance(n, dict) and isinstance(n.get("inputs"), dict)]
        sampler = next((n for n in nodes if n.get("class_type") in _SAMPLER_TYPES), None)
        meta: dict = {}
        params: dict = {}
        if sampler:
            inputs = sampler["inputs"]
            for key in ("steps", "cfg", "sampler_name", "scheduler", "denoise"):
                if isinstance(inputs.get(key), (int, float, str)) and not isinstance(inputs.get(key), bool):
                    params[key] = inputs[key]
            positive, negative = inputs.get("positive"), inputs.get("negative")
            guider = graph.get(str(inputs["guider"][0])) if isinstance(inputs.get("guider"), list) else None
            if isinstance(guider, dict):
                gin = guider.get("inputs") or {}
                positive = positive or gin.get("positive") or gin.get("conditioning")
                negative = negative or gin.get("negative")
            meta["prompt"] = _prompt_for(graph, positive)
            meta["negative_prompt"] = _prompt_for(graph, negative)
        # 시드: 샘플러에 직접 있거나(KSampler) 노이즈 노드에 있다(SamplerCustom 계열).
        seed_holders = ([sampler] if sampler else []) + nodes
        for n in seed_holders:
            seed = _int_or_none(n["inputs"].get("seed"))
            if seed is None:
                seed = _int_or_none(n["inputs"].get("noise_seed"))
            if seed is not None:
                meta["seed"] = seed
                break
        loras = []
        for n in nodes:
            inputs = n["inputs"]
            if isinstance(inputs.get("ckpt_name"), str) and "checkpoint" not in meta:
                meta["checkpoint"] = inputs["ckpt_name"]
            if isinstance(inputs.get("lora_name"), str):
                loras.append(inputs["lora_name"])
        if loras:
            params["loras"] = loras
        if params:
            meta["params_json"] = json.dumps(params, ensure_ascii=False)
        return meta
    except Exception:
        return {}


def _iso(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


def _origin_pods() -> dict:
    try:
        from comfy_outputs import synced_pod_ids
        return synced_pod_ids()
    except Exception:
        return {}


def sync(force: bool = False) -> dict | None:
    """출력 폴더와 assets를 맞춘다. 결과 요약을 돌려주고, 건너뛰었으면 None."""
    global _last_sync
    if not force and time.monotonic() - _last_sync < MIN_SYNC_INTERVAL:
        return None
    if not _sync_lock.acquire(blocking=False):
        return None   # 다른 스레드가 이미 하는 중
    try:
        try:
            images = list_output_images(OUTPUT_DIR)
            videos = list_output_videos(OUTPUT_DIR)
        except OutputFolderError:
            return None   # 폴더가 없으면(볼륨이 빠졌을 수 있다) 아무것도 지우지 않는다
        base = Path(OUTPUT_DIR)
        found: dict[str, tuple[Path, str]] = {}
        for f in images:
            found[f.relative_to(base).as_posix()] = (f, "image")
        for f in videos:
            found[f.relative_to(base).as_posix()] = (f, "video")
        origins = _origin_pods()
        added = updated = removed = 0
        with db.connect() as conn:
            rows = {r["path"]: r for r in conn.execute(
                "SELECT id, path, size_bytes, mtime_ns, deleted_at FROM assets")}
            live_rows = sum(1 for r in rows.values() if r["deleted_at"] is None)
            job_projects = {r["id"]: r["project_id"] for r in conn.execute("SELECT id, project_id FROM jobs")}
            for path, (f, kind) in found.items():
                try:
                    st = f.stat()
                except OSError:
                    continue
                row = rows.get(path)
                width = height = None
                meta: dict = {}
                need_probe = row is None or row["size_bytes"] != st.st_size or row["mtime_ns"] != st.st_mtime_ns
                if need_probe and kind == "image":
                    try:
                        with Image.open(f) as img:
                            width, height = img.size
                            if row is None:
                                meta = extract_comfy_meta(img)
                    except Exception:
                        pass
                if row is None:
                    parts = path.split("/")
                    job_id = parts[0] if len(parts) > 1 and parts[0] in job_projects else None
                    conn.execute(
                        """INSERT INTO assets(path, kind, project_id, job_id, size_bytes, mtime_ns, width, height,
                               created_at, seed, prompt, negative_prompt, checkpoint, params_json,
                               origin_pod_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (path, kind, job_projects.get(job_id) if job_id else None, job_id,
                         st.st_size, st.st_mtime_ns, width, height, _iso(st.st_mtime),
                         meta.get("seed"), meta.get("prompt"), meta.get("negative_prompt"),
                         meta.get("checkpoint"), meta.get("params_json"), origins.get(path)),
                    )
                    added += 1
                elif need_probe or row["deleted_at"] is not None:
                    conn.execute(
                        "UPDATE assets SET size_bytes=?, mtime_ns=?, width=COALESCE(?, width), "
                        "height=COALESCE(?, height), sha256=NULL, deleted_at=NULL WHERE id=?",
                        (st.st_size, st.st_mtime_ns, width, height, row["id"]),
                    )
                    updated += 1
            # 스캔이 통째로 비어 있는데 DB에는 있으면(마운트가 잠깐 비었을 수 있다) 지웠다고 보지 않는다.
            if found or live_rows == 0:
                now = db.now_iso()
                for path, row in rows.items():
                    if path not in found and row["deleted_at"] is None:
                        conn.execute("UPDATE assets SET deleted_at=? WHERE id=?", (now, row["id"]))
                        removed += 1
        _last_sync = time.monotonic()
        return {"added": added, "updated": updated, "removed": removed, "total": len(found)}
    finally:
        _sync_lock.release()


def list_assets(project: str | int | None = None, kind: str | None = None, job_id: str | None = None,
                favorite: bool | None = None, limit: int = 200, offset: int = 0) -> list[dict]:
    """project: 프로젝트 id, 또는 "unassigned"(미분류). None이면 전체."""
    where, params = ["a.deleted_at IS NULL"], []
    if project == "unassigned":
        where.append("a.project_id IS NULL")
    elif project is not None:
        where.append("a.project_id = ?")
        params.append(int(project))
    if kind:
        where.append("a.kind = ?")
        params.append(kind)
    if job_id:
        where.append("a.job_id = ?")
        params.append(job_id)
    if favorite is not None:
        where.append("a.favorite = ?")
        params.append(1 if favorite else 0)
    sql = ("SELECT a.* FROM assets a WHERE " + " AND ".join(where)
           + " ORDER BY a.created_at DESC, a.id DESC LIMIT ? OFFSET ?")
    with db.connect() as conn:
        rows = conn.execute(sql, (*params, limit, offset)).fetchall()
    return [dict(r) for r in rows]


def project_ids_by_path() -> dict[str, int | None]:
    """{상대경로: project_id} — 갤러리 목록이 항목마다 프로젝트를 붙일 때 쓴다."""
    with db.connect() as conn:
        rows = conn.execute("SELECT path, project_id FROM assets WHERE deleted_at IS NULL").fetchall()
    return {r["path"]: r["project_id"] for r in rows}
