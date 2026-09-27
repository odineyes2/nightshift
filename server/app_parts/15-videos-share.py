# app.py가 app_parts/*.py를 번호 순서대로 한 네임스페이스에서 실행한다 — 앞 파일의 이름을 import 없이 쓴다.
@app.get("/api/output-videos")
def list_output_videos_api(request: Request, q: str | None = None, tag: str | None = None,
                           favorite: bool | None = None, min_rating: int | None = None,
                           model: str | None = None, hide_nsfw: bool = False):
    _sync_assets_quietly()
    user = me(request)
    items = list_output_videos_meta(user)
    allowed = asset_meta.search_paths(q, _split_tags(tag), favorite, min_rating, kind="video",
                                      owner_id=auth.owner_scope(user), model=model or None, hide_nsfw=hide_nsfw)
    if allowed is not None:
        items = [i for i in items if i["name"] in allowed]
    return {"videos": items}


def resolve_output_video(filename: str) -> Path:
    # resolve_output_image와 같은 방식의 경로 검증(상위 폴더 탈출·심볼릭 링크 우회 방지).
    if not filename or filename.startswith("/") or ".." in Path(filename).parts:
        raise HTTPException(400, "올바르지 않은 파일명이에요.")
    base = Path(OUTPUT_DIR).resolve()
    path = (base / filename).resolve()
    if not path.is_relative_to(base):
        raise HTTPException(400, "올바르지 않은 파일명이에요.")
    if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise HTTPException(404, "동영상을 찾을 수 없어요.")
    _require_output_owner(path.relative_to(base).as_posix(), "동영상을 찾을 수 없어요.")
    return path


@app.get("/api/output-videos/{filename:path}")
def get_output_video(filename: str):
    # 영상 갤러리 라이트박스의 <video> 태그가 재생하는 용도. FileResponse는
    # HTTP Range 요청을 그대로 지원해서(Starlette 내장) 앞으로 감기·되감기가 된다.
    return FileResponse(resolve_output_video(filename))


@app.delete("/api/output-videos/{filename:path}")
def delete_output_video(filename: str):
    resolve_output_video(filename).unlink()
    return {"ok": True}


# ---- 영상 편집(자르기 / 이어 붙이기) — video_edit.py ---------------------------------------
# 갤러리에서 고른 영상으로 새 영상을 만든다(원본은 그대로). 작업은 백그라운드로 돌고 진행률을 폴링으로 본다.

def register_generated_video(rel: str, source_rels: list[str], scope, note: str) -> None:
    """서버가 새로 만든 영상(편집 결과, 외부 편집기가 올린 결과)을 색인에 넣고, 원본이 다 같은 프로젝트면 그 프로젝트로
    넣고, 메모를 남긴다. 어떤 실패도 부른 쪽을 실패시키지 않는다(파일은 이미 만들어졌다)."""
    try:
        assets_index.sync(force=True)
        if source_rels:
            with db.connect() as conn:
                rows = conn.execute(
                    f"SELECT DISTINCT project_id FROM assets WHERE path IN ({','.join('?' * len(source_rels))})", source_rels).fetchall()
            projects = {r["project_id"] for r in rows}
            if len(projects) == 1 and next(iter(projects)) is not None:
                asset_meta.move_assets([rel], next(iter(projects)), scope)
        asset_meta.update_assets([rel], note=note, owner_id=scope)
    except Exception:
        logging.getLogger("uvicorn.error").exception("생성된 영상 등록 실패: %s", rel)


def _slug(text: str, fallback: str) -> str:
    slug = re.sub(r"[^\w.-]+", "-", text.strip(), flags=re.UNICODE).strip("-._")[:40]
    return slug or fallback


@app.post("/api/video-edits")
async def create_video_edit(request: Request):
    user = me(request)
    data = await read_json_object(request, allow_empty=False)
    op = data.get("op")
    if op not in ("concat", "trim"):
        raise HTTPException(400, "op는 concat 또는 trim이어야 해요.")
    names = data.get("inputs")
    if not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names):
        raise HTTPException(400, "inputs(영상 이름 목록)가 필요해요.")
    if len(names) > video_edit.MAX_INPUTS:
        raise HTTPException(400, f"한 번에 {video_edit.MAX_INPUTS}개까지 이어 붙일 수 있어요.")
    sources = [resolve_output_video(n) for n in names]   # 남의 영상/없는 영상은 여기서 404
    start_s = end_s = None
    if op == "trim":
        try:
            start_s = float(data.get("start") or 0)
            end_s = float(data["end"]) if data.get("end") not in (None, "") else None
        except (TypeError, ValueError):
            raise HTTPException(400, "start/end는 초 단위 숫자여야 해요.")
        if start_s < 0 or (end_s is not None and end_s <= start_s):
            raise HTTPException(400, "끝 시각은 시작 시각보다 뒤여야 해요.")
    base = Path(OUTPUT_DIR).resolve()
    prefix = "" if auth.is_admin(user) else f"u{user['id']}/"
    label = _slug(str(data.get("name") or ""), "concat" if op == "concat" else "trim")
    rel = f"{prefix}edits/{datetime.now().strftime('%Y%m%d-%H%M%S')}_{label}_{uuid.uuid4().hex[:4]}.mp4"
    out_path = base / rel
    source_rels = [p.relative_to(base).as_posix() for p in sources]
    scope = auth.owner_scope(user)

    label_names = ", ".join(Path(r).name for r in source_rels[:5]) + (" …" if len(source_rels) > 5 else "")
    note = (f"{'이어 붙임' if op == 'concat' else '자름'}: {label_names}"
            + (f" ({start_s:g}s~{end_s:g}s)" if op == "trim" and end_s is not None else ""))

    def post(_out: Path) -> None:
        register_generated_video(rel, source_rels, scope, note)

    try:
        job_id = video_edit.start(op, sources, out_path, user["id"], start_s=start_s, end_s=end_s, out_rel=rel, post=post)
    except video_edit.EditError as e:
        raise HTTPException(400, str(e))
    return {"id": job_id}


def _edit_job_or_404(user: dict, job_id: str) -> dict:
    job = video_edit.get(job_id)
    if job is None or not (auth.is_admin(user) or job["owner_id"] == user["id"]):
        raise HTTPException(404, "없는 편집 작업이에요.")
    return job


@app.get("/api/video-edits/{job_id}")
def get_video_edit(job_id: str, request: Request):
    return video_edit.public(_edit_job_or_404(me(request), job_id))


@app.post("/api/video-edits/{job_id}/cancel")
def cancel_video_edit(job_id: str, request: Request):
    _edit_job_or_404(me(request), job_id)
    video_edit.cancel(job_id)
    return {"ok": True}


# ---- 외부 편집기(OpenCut)와 자원 공유 — share_sessions.py ----------------------------------------
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
SHARE_UPLOAD_EXT = {".mp4", ".webm", ".mov", ".m4v"}
SHARE_CONTENT_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
                       ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
                       ".gif": "image/gif", ".bmp": "image/bmp"}


@app.get("/api/share/config")
def share_config(request: Request):
    me(request)
    return {"opencut_url": OPENCUT_URL or None}


@app.post("/api/share/sessions")
async def create_share_session(request: Request):
    """갤러리에서 고른 이미지/영상을 편집기가 가져갈 수 있는 세션을 만든다 → {token, url}(편집기를 여는 주소)."""
    user = me(request)
    if not OPENCUT_URL:
        raise HTTPException(503, "외부 편집기가 설정돼 있지 않아요(NIGHTSHIFT_OPENCUT_URL).")
    data = await read_json_object(request, allow_empty=False)
    names = data.get("names")
    if not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names):
        raise HTTPException(400, "names(파일 이름 목록)가 필요해요.")
    if len(names) > share_sessions.MAX_FILES:
        raise HTTPException(400, f"한 번에 {share_sessions.MAX_FILES}개까지 보낼 수 있어요.")
    base = Path(OUTPUT_DIR).resolve()
    files = []
    for name in names:
        ext = Path(name).suffix.lower()
        path = resolve_output_video(name) if ext in VIDEO_EXTENSIONS else resolve_output_image(name)   # 남의 것/없는 것은 여기서 404
        files.append({"rel": path.relative_to(base).as_posix(), "kind": "video" if ext in VIDEO_EXTENSIONS else "image",
                      "name": path.name})
    rels = [f["rel"] for f in files]
    with db.connect() as conn:
        rows = conn.execute(f"SELECT DISTINCT project_id FROM assets WHERE path IN ({','.join('?' * len(rels))})", rels).fetchall()
    projects = {r["project_id"] for r in rows}
    project_id = next(iter(projects)) if len(projects) == 1 else None
    token = share_sessions.create(user["id"], files, project_id)
    return {"token": token, "url": f"{OPENCUT_URL}/nightshift?ns={token}", "expires_in": share_sessions.SESSION_TTL_SEC}


def _share_or_404(token: str) -> dict:
    session = share_sessions.get(token)
    if session is None:
        raise HTTPException(404, "만료됐거나 없는 공유 세션이에요.")
    return session


@app.get("/api/shared/sessions/{token}")
def get_share_manifest(token: str, request: Request):
    """편집기가 읽는 목록 — 파일마다 이 세션 안에서만 통하는 주소가 붙는다."""
    session = _share_or_404(token)
    base = Path(OUTPUT_DIR).resolve()
    origin = f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"
    files = []
    for i, f in enumerate(session["files"]):
        path = base / f["rel"]
        if not path.is_file():
            continue
        files.append({"index": i, "name": f["name"], "kind": f["kind"], "size": path.stat().st_size,
                      "url": f"{origin}/api/shared/sessions/{token}/files/{i}"})
    return {"files": files, "upload_url": f"{origin}/api/shared/sessions/{token}/upload",
            "expires_at": session["expires"], "uploads_left": share_sessions.MAX_UPLOADS - session["uploads"]}


@app.get("/api/shared/sessions/{token}/files/{index}")
def get_shared_file(token: str, index: int):
    session = _share_or_404(token)
    if not 0 <= index < len(session["files"]):
        raise HTTPException(404, "없는 파일이에요.")
    base = Path(OUTPUT_DIR).resolve()
    path = (base / session["files"][index]["rel"]).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise HTTPException(404, "파일을 찾을 수 없어요.")
    return FileResponse(path, media_type=SHARE_CONTENT_TYPES.get(path.suffix.lower()))


@app.post("/api/shared/sessions/{token}/upload")
async def upload_shared_result(token: str, request: Request, name: str = "edit.mp4"):
    """편집기가 내보낸 영상을 받아 세션 주인의 편집 폴더에 새 영상으로 저장한다(요청 본문이 곧 파일)."""
    session = _share_or_404(token)
    ext = Path(name).suffix.lower()
    if ext not in SHARE_UPLOAD_EXT:
        raise HTTPException(400, f"영상 파일({', '.join(sorted(SHARE_UPLOAD_EXT))})만 올릴 수 있어요.")
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > SHARE_UPLOAD_MAX_BYTES:
        raise HTTPException(413, "파일이 너무 커요.")
    if not share_sessions.count_upload(token):
        raise HTTPException(429, "이 세션으로는 더 올릴 수 없어요.")
    owner = auth.get_user(session["owner_id"])
    if owner is None or owner.get("status") != "active":
        raise HTTPException(403, "이 세션의 회원을 쓸 수 없어요.")
    base = Path(OUTPUT_DIR).resolve()
    prefix = "" if auth.is_admin(owner) else f"u{owner['id']}/"
    label = _slug(Path(name).stem, "opencut")
    rel = f"{prefix}edits/{datetime.now().strftime('%Y%m%d-%H%M%S')}_opencut_{label}_{uuid.uuid4().hex[:4]}{ext}"
    out_path = base / rel
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.stem + ".partial" + out_path.suffix)
    size = 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > SHARE_UPLOAD_MAX_BYTES:
                    raise HTTPException(413, "파일이 너무 커요.")
                f.write(chunk)
        if size == 0:
            raise HTTPException(400, "빈 파일이에요.")
        os.replace(tmp, out_path)
    finally:
        if tmp.exists():
            tmp.unlink()
    sources = [f["rel"] for f in session["files"]]
    note = "OpenCut에서 편집: " + ", ".join(Path(r).name for r in sources[:5]) + (" …" if len(sources) > 5 else "")
    scope = None if auth.is_admin(owner) else owner["id"]
    await asyncio.to_thread(register_generated_video, rel, sources, scope, note)
    return {"ok": True, "output": rel, "size": size}


def build_output_video_zip(only_paths: set[str] | None = None) -> Path:
    files = find_video_files(OUTPUT_DIR, only_paths)
    return build_zip_from_paths(files)


@app.get("/api/download-videos")
async def download_videos(request: Request):
    only = asset_meta.owned_paths(me(request)["id"])
    try:
        zip_path = await asyncio.to_thread(build_output_video_zip, only)
    except OutputFolderError as e:
        raise HTTPException(404, str(e))

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"nightshift_videos_{timestamp}.zip",
        background=BackgroundTask(lambda: zip_path.unlink(missing_ok=True)),
    )


@app.post("/api/output-videos/download-selected")
async def download_selected_videos(request: Request):
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    names = parse_image_names_body(data)

    paths = []
    for name in names:
        try:
            paths.append(resolve_output_video(name))
        except HTTPException:
            continue
    if not paths:
        raise HTTPException(404, "선택한 동영상을 찾을 수 없어요.")

    zip_path = await asyncio.to_thread(build_zip_from_paths, paths)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"nightshift_videos_selected_{timestamp}.zip",
        background=BackgroundTask(lambda: zip_path.unlink(missing_ok=True)),
    )


@app.delete("/api/output-videos")
async def delete_videos(request: Request):
    only = asset_meta.owned_paths(me(request)["id"])
    try:
        deleted = await asyncio.to_thread(delete_output_videos, OUTPUT_DIR, only)
    except OutputFolderError as e:
        raise HTTPException(404, str(e))
    return {"deleted": deleted}


@app.post("/api/output-videos/delete-selected")
async def delete_selected_videos(request: Request):
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    names = parse_image_names_body(data)

    deleted = 0
    for name in names:
        try:
            path = resolve_output_video(name)
        except HTTPException:
            continue
        path.unlink()
        deleted += 1
    return {"deleted": deleted}
