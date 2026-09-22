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
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

import auth
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
        return meta_from_graph(json.loads(raw))
    except Exception:
        return {}


_HEAD_TAIL_BYTES = 4 * 1024 * 1024
# mp4 ilst의 data 아톰 머리(타입 1=UTF-8, 로케일 0) — SaveVideo가 프롬프트 JSON을 이 바로 뒤에 넣는다.
_ILST_DATA = b"data" + bytes([0, 0, 0, 1, 0, 0, 0, 0])


def extract_video_meta(path: Path) -> dict:
    """VHS 등이 mp4 안에 "PROMPT" 표식과 함께 넣어 주는 프롬프트 그래프(JSON)를 찾아 읽는다.
    영상 하나가 수십~수백 MB일 수 있어 앞·뒤 4MB만 훑는다(메타데이터는 보통 그 안에 있다)."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            chunks = [f.read(_HEAD_TAIL_BYTES)]
            if size > 2 * _HEAD_TAIL_BYTES:
                f.seek(size - _HEAD_TAIL_BYTES)
                chunks.append(f.read(_HEAD_TAIL_BYTES))
        decoder = json.JSONDecoder()
        for data in chunks:
            # 두 가지 배치: VHS가 넣는 "PROMPT" 표식 뒤, 그리고 mp4 ilst의 data 아톰(ComfyUI SaveVideo) 바로 뒤.
            starts = []
            for marker, window in ((b"PROMPT", 64), (_ILST_DATA, 0)):
                pos = 0
                while (i := data.find(marker, pos)) >= 0:
                    if window:
                        j = data.find(b'{"', i, i + window)
                    else:
                        j = i + len(marker) if data[i + len(marker):i + len(marker) + 1] == b"{" else -1
                    if j >= 0:
                        starts.append(j)
                    pos = i + len(marker)
            for j in starts:
                try:
                    graph, _ = decoder.raw_decode(data[j:].decode("utf-8", errors="ignore"))
                except ValueError:
                    continue
                meta = meta_from_graph(graph)
                if meta:
                    return meta
    except Exception:
        pass
    return {}


# 로더 노드의 입력 이름 -> params_json에 넣을 목록 이름. 체크포인트는 checkpoint 칸에 따로 둔다.
_MODEL_INPUTS = (("lora_name", "loras"), ("unet_name", "unets"), ("vae_name", "vaes"), ("clip_name", "text_encoders"))


def meta_from_graph(graph) -> dict:
    try:
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
        used: dict[str, list] = {}
        for n in nodes:
            inputs = n["inputs"]
            if isinstance(inputs.get("ckpt_name"), str) and "checkpoint" not in meta:
                meta["checkpoint"] = inputs["ckpt_name"]
            for field, key in _MODEL_INPUTS:
                value = inputs.get(field)
                if isinstance(value, str) and value and value not in used.setdefault(key, []):
                    used[key].append(value)
        params.update({k: v for k, v in used.items() if v})
        if params:
            meta["params_json"] = json.dumps(params, ensure_ascii=False)
        return meta
    except Exception:
        return {}


def _iso(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


def owner_of(path: str):
    """결과물(출력 폴더 기준 상대 경로)의 주인 id. 색인에 없으면 한 번 색인을 맞춰 본 뒤에도 없을 때 None."""
    query = "SELECT owner_id FROM assets WHERE path=? AND deleted_at IS NULL"
    with db.connect() as conn:
        row = conn.execute(query, (path,)).fetchone()
    if row is None:
        sync()   # 방금 생긴 파일일 수 있다(짧은 간격 안이면 건너뛴다)
        with db.connect() as conn:
            row = conn.execute(query, (path,)).fetchone()
    return row["owner_id"] if row else None


_USER_DIR_RE = re.compile(r"^u(\d+)/")


def _owner_for(path: str, job_owner, origin_pod_id, user_ids: set[int]):
    """새로 만난 결과물의 주인. 순서: 그 작업을 만든 회원 → 경로의 u<회원id>/ 폴더(ComfyUI에서 직접 돌려 받아온 것을 파드
    주인별로 나눠 담은 곳) → 받아온 파드의 주인 → 그래도 모르면 관리자. 주인 없음(NULL)으로 두지 않는다 — 그러면 그 결과물은
    어느 프로젝트로도 옮길 수 없고 일반 회원에게도 안 보인다. 파일이 먼저 색인되고 "어느 파드에서 받았나" 기록이 나중에
    저장되는 경우가 있어서, 경로 접두사를 먼저 본다."""
    if job_owner is not None and job_owner in user_ids:
        return job_owner
    m = _USER_DIR_RE.match(path)
    if m and int(m.group(1)) in user_ids:
        return int(m.group(1))
    pod_owner = _pod_owner(origin_pod_id)
    if pod_owner is not None and pod_owner in user_ids:
        return pod_owner
    return auth.admin_id()


def _pod_owner(pod_id):
    if not pod_id:
        return None
    try:
        import pod_registry
        pod = pod_registry.get_pod(pod_id)
        return pod.get("owner_id") if pod else None
    except Exception:
        return None


def _origin_pods() -> dict:
    try:
        from comfy_outputs import synced_pod_ids
        return synced_pod_ids()
    except Exception:
        return {}


_BACKFILL_KEY = "asset_model_meta_v1"


def _backfill_model_meta(found: dict) -> None:
    """예전에 색인한 결과물에는 UNet/VAE/텍스트 인코더 이름이 없고(영상은 아예 메타를 안 읽었다), 그래서
    모델별 사용 통계가 비어 보인다. DB마다 한 번만 파일을 다시 읽어 그 칸들을 채운다.
    이미 값이 있는 seed/prompt/checkpoint는 그대로 두고 비어 있는 것만 채운다."""
    with db.connect() as conn:
        if db.get_meta(conn, _BACKFILL_KEY):
            return
        rows = conn.execute("SELECT id, path, kind, seed, prompt, negative_prompt, checkpoint FROM assets "
                            "WHERE deleted_at IS NULL").fetchall()
        for r in rows:
            item = found.get(r["path"])
            if item is None:
                continue
            f, kind = item
            meta: dict = {}
            try:
                if kind == "video":
                    meta = extract_video_meta(f)
                else:
                    with Image.open(f) as img:
                        meta = extract_comfy_meta(img)
            except Exception:
                continue
            if not meta:
                continue
            conn.execute(
                "UPDATE assets SET params_json=COALESCE(?, params_json), checkpoint=COALESCE(checkpoint, ?), "
                "seed=COALESCE(seed, ?), prompt=COALESCE(prompt, ?), negative_prompt=COALESCE(negative_prompt, ?) "
                "WHERE id=?",
                (meta.get("params_json"), meta.get("checkpoint"), meta.get("seed"),
                 meta.get("prompt"), meta.get("negative_prompt"), r["id"]))
        db.set_meta(conn, _BACKFILL_KEY, db.now_iso())


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
            job_rows = {r["id"]: (r["project_id"], r["owner_id"])
                        for r in conn.execute("SELECT id, project_id, owner_id FROM jobs")}
            job_projects = {jid: v[0] for jid, v in job_rows.items()}
            project_mature = {r["id"]: r["is_mature"] for r in conn.execute("SELECT id, is_mature FROM projects")}
            user_ids = {r["id"] for r in conn.execute("SELECT id FROM users")}
            # 주인이 없는 채로 남은 결과물(예전 버전이 남긴 것)을 채운다.
            for r in conn.execute("SELECT id, path, job_id, origin_pod_id FROM assets WHERE owner_id IS NULL").fetchall():
                owner = _owner_for(r["path"], job_rows.get(r["job_id"], (None, None))[1] if r["job_id"] else None,
                                   r["origin_pod_id"] or origins.get(r["path"]), user_ids)
                if owner is not None:
                    conn.execute("UPDATE assets SET owner_id=? WHERE id=?", (owner, r["id"]))
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
                if row is None and kind == "video":
                    meta = extract_video_meta(f)
                if row is None:
                    parts = path.split("/")
                    job_id = parts[0] if len(parts) > 1 and parts[0] in job_projects else None
                    # 주인: 그 작업을 만든 회원, 작업이 없으면(ComfyUI에서 직접 만든 것) 받아온 파드의 주인.
                    owner_id = _owner_for(path, job_rows[job_id][1] if job_id else None, origins.get(path), user_ids)
                    project_id = job_projects.get(job_id) if job_id else None
                    nsfw = 1 if project_mature.get(project_id) else 0
                    conn.execute(
                        """INSERT INTO assets(path, kind, project_id, job_id, size_bytes, mtime_ns, width, height,
                               created_at, seed, prompt, negative_prompt, checkpoint, params_json,
                               origin_pod_id, owner_id, nsfw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (path, kind, project_id, job_id,
                         st.st_size, st.st_mtime_ns, width, height, _iso(st.st_mtime),
                         meta.get("seed"), meta.get("prompt"), meta.get("negative_prompt"),
                         meta.get("checkpoint"), meta.get("params_json"), origins.get(path), owner_id, nsfw),
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
        _backfill_model_meta(found)
        _last_sync = time.monotonic()
        return {"added": added, "updated": updated, "removed": removed, "total": len(found)}
    finally:
        _sync_lock.release()
