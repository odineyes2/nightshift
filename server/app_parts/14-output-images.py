# app.py가 app_parts/*.py를 번호 순서대로 한 네임스페이스에서 실행한다 — 앞 파일의 이름을 import 없이 쓴다.
@app.post("/api/send-email")
async def send_email(request: Request):
    # SMTP 계정 정보는 이 요청 처리에만 쓰고 어디에도 저장하지 않는다 — jobs_state.json은
    # git으로 버전 관리되므로 비밀번호를 job 데이터에 남기면 커밋 이력에 그대로 남는다.
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")

    smtp_user = (data.get("smtp_user") or "").strip()
    smtp_password = data.get("smtp_password") or ""
    to_email = (data.get("to_email") or "").strip()

    if not smtp_user or not smtp_password or not to_email:
        raise HTTPException(400, "보내는 메일 계정/비밀번호/받는 메일 계정을 모두 입력하세요.")

    max_mb = data.get("max_mb", 20)
    try:
        max_mb = int(max_mb)
        if max_mb <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(400, "메일당 최대 용량(MB)은 1 이상의 정수여야 해요.")

    try:
        result = await asyncio.to_thread(send_output_images, smtp_user, smtp_password, to_email, max_mb,
                                         None, asset_meta.owned_paths(me(request)["id"]))   # 내 이미지만
    except EmailSendError as e:
        raise HTTPException(400, str(e))
    return result


def _own_paths(user: dict) -> set[str] | None:
    """이 회원이 손댈 수 있는 결과물 경로 집합 — 관리자는 None(전부)."""
    return None if auth.is_admin(user) else asset_meta.owned_paths(user["id"])


def _meta_resolver(owner_id: int | None = None):
    """갤러리 항목에 붙일 결과물 메타(프로젝트·즐겨찾기·평점·태그)를 돌려주는 함수를 만든다
    (name, job_id) -> dict. 결과물 색인(assets)이 진실이고, 아직 색인에 없는 새 파일은 그 job의
    프로젝트만 물려받은 기본값으로 본다."""
    try:
        by_path = asset_meta.meta_by_path(owner_id)
    except Exception:
        by_path = {}

    def resolve(name: str, job_id: str | None) -> dict:
        found = by_path.get(name)
        if found:
            return found
        project_id = None
        if job_id:
            with lock:
                job = jobs.get(job_id)
            if job:
                project_id = job.get("project_id")
        return {"asset_id": None, "favorite": False, "rating": None, "nsfw": False, "project_id": project_id, "tags": []}
    return resolve


def list_output_images_meta(user: dict) -> list[dict]:
    scope = auth.owner_scope(user)   # 일반 회원에게는 자기 결과물만 보인다
    names = auth.usernames() if scope is None else {}
    # 갤러리 탭을 채우는 용도. zip/이메일 발송과 달리 폴더가 비어 있거나 아직 없는 것도
    # 정상 상태로 취급한다(뭔가 있어야 의미 있는 동작이 아니라, 그냥 목록을 보여줄 뿐이므로).
    try:
        files = list_output_images(OUTPUT_DIR, only_paths=_own_paths(user))
    except OutputFolderError:
        return []
    base = Path(OUTPUT_DIR)
    # nightshift 큐를 거치지 않고 ComfyUI에서 직접 돌린 이미지는 job_id가 없어서
    # 파드 갤러리(프론트의 podIdForImage)가 어느 파드 것인지 알 길이 없었다 —
    # "⬇ 결과 가져오기"가 남긴 동기화 기록(comfy_output_sync.json)에 이제 파드
    # 정보가 있으니, job_id가 없을 때 쓸 수 있게 같이 넘긴다.
    pod_ids = synced_pod_ids()
    meta_of = _meta_resolver(scope)
    items = []
    for f in files:
        stat = f.stat()
        rel = f.relative_to(base)
        name = rel.as_posix()
        # 템플릿 스크립트가 JOB_ID 하위 폴더에 나눠 저장하므로(seed_batch.py 등),
        # 상대 경로가 여러 단계면 첫 번째 폴더 이름을 job_id로 노출한다 — 갤러리의
        # "작업별 보기"가 이 값으로 묶는다. 하위 폴더 없이 바로 밑에 있는(예전 방식
        # 또는 JOB_ID 없이 실행된) 이미지는 job_id가 없다.
        job_id = rel.parts[0] if len(rel.parts) > 1 else None
        # 갤러리 "자세히 보기"에 해상도를 보여주려고 읽는다 — PIL은 헤더만 읽고
        # 픽셀 데이터는 지연 로드하므로(.load()를 안 부르면) 전체 디코드보다 훨씬
        # 가볍다. 손상된 파일이어도 목록 자체는 계속 보여야 하니 실패하면 조용히
        # None으로 둔다.
        width = height = None
        try:
            with Image.open(f) as img:
                width, height = img.size
        except Exception:
            pass
        items.append({
            "name": name,
            "job_id": job_id,
            "synced_pod_id": pod_ids.get(name),
            **meta_of(name, job_id),
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "width": width,
            "height": height,
        })
    if scope is None:   # 관리자에게는 누구 것인지 알려 준다
        for it in items:
            it["owner_name"] = names.get(it.get("owner_id"))
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items


@app.get("/api/output-images")
def list_output_images_api(request: Request, q: str | None = None, tag: str | None = None,
                           favorite: bool | None = None, min_rating: int | None = None,
                           model: str | None = None, hide_nsfw: bool = False):
    # 갤러리가 4초마다 부르는 곳 — 색인(assets)이 디스크와 어긋나지 않게 짧은 간격
    # 안에서는 건너뛰는 sync를 같이 돌린다(회전/삭제/직접 넣은 파일이 여기서 따라잡힌다).
    # q(프롬프트·메모·태그·파일명·체크포인트·시드)/tag(쉼표로 여러 개, 모두 붙은 것)/favorite/
    # min_rating을 주면 그 조건에 맞는 것만 남긴다. hide_nsfw는 헤더의 NSFW 토글이 "숨김"일 때 붙는다.
    _sync_assets_quietly()
    user = me(request)
    items = list_output_images_meta(user)
    allowed = asset_meta.search_paths(q, _split_tags(tag), favorite, min_rating, kind="image",
                                      owner_id=auth.owner_scope(user), model=model or None, hide_nsfw=hide_nsfw)
    if allowed is not None:
        items = [i for i in items if i["name"] in allowed]
    return {"images": items}


async def read_json_object(request: Request, allow_empty: bool = True) -> dict:
    body = (await request.body()).strip()
    if not body:
        if allow_empty:
            return {}
        raise HTTPException(400, "JSON 본문이 필요해요.")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    if not isinstance(data, dict):
        raise HTTPException(400, "JSON 객체여야 해요.")
    return data


@app.post("/api/comfy-outputs/sync")
async def sync_comfy_outputs(request: Request):
    user = me(request)
    # 원격 ComfyUI가 만든 결과 이미지를 로컬 출력 폴더로 끌어온다(comfy_outputs.py).
    # 작업이 끝날 때마다 자동으로도 돌지만(pull_outputs 설정), pod를 껐다 켠 뒤 밀린
    # 것을 한꺼번에 받거나 설정을 뒤늦게 켠 경우를 위해 수동으로도 돌릴 수 있다.
    data = await read_json_object(request)
    job_id = (data.get("job_id") or "").strip() or None
    force = bool(data.get("force"))
    pod_id = (data.get("pod_id") or "").strip() or None
    if job_id:
        job_or_404(user, job_id)

    # pod_id를 주면 그 파드에서 가져온다(파드 갤러리의 "⬇ 결과 가져오기") — 안 주면
    # 이 회원의 기본 파드에서 가져온다(전역 갤러리가 여기 해당한다).
    if pod_id:
        pod = pod_or_404(user, pod_id)
    else:
        pod = default_pod_for_user(user)
        if pod is None:
            raise HTTPException(400, "워커가 아직 없어요. 워커 화면에서 먼저 추가해 주세요.")
        pod_id = pod["id"]
    health = await asyncio.to_thread(driver_for(pod).health, pod)
    url, connected = health["url"], health["ok"]
    if not url or not connected:
        raise HTTPException(503, "ComfyUI에 연결할 수 없어 결과 이미지를 가져올 수 없어요.")
    try:
        result = await asyncio.to_thread(sync_outputs, url, only_subfolder=job_id, force=force, pod_id=pod_id)
    except OutputSyncError as e:
        raise HTTPException(UPSTREAM_ERROR, str(e))
    return {"url": url, **result}


@app.post("/api/comfy-outputs/forget")
async def forget_comfy_outputs(request: Request):
    user = me(request)
    # "한 번 받아온 이미지"라는 기록을 지운다 — 지운 이미지가 다음 동기화에서 되살아나지
    # 않게 하는 것이 이 기록의 목적이므로, 정말 다시 받고 싶을 때만 쓰는 탈출구다.
    # (한 번만 다시 받으면 되는 경우라면 sync의 force=true가 더 간단하다.)
    data = await read_json_object(request)
    job_id = (data.get("job_id") or "").strip() or None
    # 기록은 서버 전체가 함께 쓰는 것이라, 작업 하나(내 것)만 지우거나 관리자가 전체를 지울 때만 허용한다.
    if job_id:
        job_or_404(user, job_id)
    else:
        admin_only(request)
    removed = await asyncio.to_thread(forget_downloaded, job_id)
    return {"forgotten": removed, **sync_state_summary()}


def _require_output_owner(rel_path: str, not_found: str) -> None:
    """결과 파일 하나에 접근해도 되는지 — 관리자는 전부, 일반 회원은 자기 것만(남의 것은 없는 것처럼 404)."""
    user = auth.current_user.get()
    if user is None:
        raise HTTPException(401, "로그인이 필요해요.")
    if auth.is_admin(user):
        return
    if assets_index.owner_of(rel_path) != user["id"]:
        raise HTTPException(404, not_found)


def resolve_output_image(filename: str) -> Path:
    # filename은 "job_id/파일명.png"처럼 한 단계 하위 폴더를 포함할 수 있다(작업별
    # 폴더 구조, output_images.py 참고). ".."로 상위 폴더를 벗어나려는 시도나 절대
    # 경로는 막고, 정규화한 최종 경로가 여전히 OUTPUT_DIR 내부인지 다시 한번 확인한다
    # (심볼릭 링크 등으로 우회하는 경우까지 방어).
    if not filename or filename.startswith("/") or ".." in Path(filename).parts:
        raise HTTPException(400, "올바르지 않은 파일명이에요.")
    base = Path(OUTPUT_DIR).resolve()
    path = (base / filename).resolve()
    if not path.is_relative_to(base):
        raise HTTPException(400, "올바르지 않은 파일명이에요.")
    if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise HTTPException(404, "이미지를 찾을 수 없어요.")
    _require_output_owner(path.relative_to(base).as_posix(), "이미지를 찾을 수 없어요.")
    return path


# :path 컨버터는 "/"를 포함해 뒤에 오는 걸 전부 욕심껏(greedy) 먹어버려서, 아래
# thumbnail 라우트를 원본 이미지 라우트보다 먼저 등록해야 한다 — 순서가 바뀌면
# "job_id/파일.png/thumbnail" 요청도 원본 이미지 라우트(filename:path)가 먼저
# 통째로 집어삼켜서 404가 난다(FastAPI/Starlette는 등록 순서대로 첫 매치를 씀).
# 축소본 크기 단계 — 요청한 크기는 가까운 위 단계로 올린다. 단계를 묶어야 캐시 파일 수가
# 크기 조합만큼 늘어나지 않는다. 1280 이상은 라이트박스 같은 화면용 사본이다.
THUMB_SIZES = (80, 120, 200, 400, 800, 1280, 2048)


def thumb_size_step(size: int) -> int:
    return next((s for s in THUMB_SIZES if s >= size), THUMB_SIZES[-1])


def _thumb_cache_prefix(path: Path) -> str:
    # 원본 경로마다 고정된 앞부분 — 지울 때 이 앞부분으로 그 이미지의 캐시를 모두 찾는다.
    return hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:16]


def drop_thumb_cache(path: Path) -> None:
    """원본 이미지 하나의 축소본 캐시를 모두 지운다(이미지를 지울 때)."""
    for f in THUMB_CACHE_DIR.glob(_thumb_cache_prefix(path) + "-*"):
        f.unlink(missing_ok=True)


def prune_thumb_cache(max_bytes: int | None = None) -> None:
    """캐시 폴더가 상한을 넘으면 오래 안 쓴(mtime이 오래된) 것부터 지운다 — 서버 시작 때 1회."""
    limit = THUMB_CACHE_MAX_BYTES if max_bytes is None else max_bytes
    files = sorted((f.stat().st_mtime, f.stat().st_size, f) for f in THUMB_CACHE_DIR.glob("*.jpg"))
    total = sum(s for _, s, _ in files)
    for _, s, f in files:
        if total <= limit:
            break
        f.unlink(missing_ok=True)
        total -= s


@app.get("/api/output-images/{filename:path}/thumbnail")
def get_output_image_thumbnail(filename: str, request: Request, size: int = 320, fit: str = "inside", v: str = ""):
    # 갤러리 격자·라이트박스용 축소본 — 원본을 그대로 내려받으면 느리고 대역폭을 낭비한다.
    # 만든 축소본은 THUMB_CACHE_DIR에 저장해 두고 같은 요청에는 디코딩 없이 파일을 준다.
    # 캐시 키는 ETag와 같은 값(경로 해시+mtime+size+fit)이라, 원본이 회전되거나 덮어써지면
    # mtime이 바뀌어 새 키로 다시 만들어진다(옛 키 파일은 그때 지운다).
    # 주소에 v=(버전)가 있으면 주소 자체가 바뀌므로 브라우저가 1년 동안 다시 묻지 않게 한다.
    # fit=cover면 긴 변이 아니라 정사각형으로 가운데를 잘라 size×size로 만든다 — 갤러리 칸이
    # 정사각형(object-fit:cover)이라 어차피 그렇게 잘려 보이는데, 긴 변 기준으로 줄이면 세로로
    # 긴 이미지의 짧은 변이 칸보다 작아져 CSS가 늘려 그리는 바람에 흐릿해졌다.
    if fit not in ("inside", "cover"):
        raise HTTPException(400, "fit은 inside 또는 cover여야 해요.")
    path = resolve_output_image(filename)
    size = thumb_size_step(size)
    stat = path.stat()
    prefix = _thumb_cache_prefix(path)
    key = f"{stat.st_mtime_ns:x}-{stat.st_size:x}-{size}-{fit}"
    etag = f'"{key}"'
    cache_control = "private, max-age=31536000, immutable" if v else "private, max-age=86400"
    headers = {"ETag": etag, "Cache-Control": cache_control}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    cached = THUMB_CACHE_DIR / f"{prefix}-{key}.jpg"
    if cached.is_file():
        os.utime(cached)  # 정리(prune_thumb_cache)가 최근에 쓴 것을 남기도록
        return FileResponse(cached, media_type="image/jpeg", headers=headers)
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            if fit == "cover":
                img = ImageOps.fit(img, (size, size), method=Image.LANCZOS, centering=(0.5, 0.5))
            else:
                img.thumbnail((size, size))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85 if size >= 1280 else 82)
    except Exception as e:
        raise HTTPException(500, f"썸네일을 만들지 못했어요: {e}")
    # 원본이 바뀌기 전의 옛 키 파일을 지우고 새로 쓴다. 임시 이름으로 쓴 뒤 바꿔서
    # 동시에 온 요청이 반쯤 쓴 파일을 받지 않게 한다.
    for old in THUMB_CACHE_DIR.glob(f"{prefix}-*"):
        if not old.name.startswith(f"{prefix}-{stat.st_mtime_ns:x}-{stat.st_size:x}-"):
            old.unlink(missing_ok=True)
    tmp = cached.with_suffix(f".{uuid.uuid4().hex}.tmp")
    tmp.write_bytes(buf.getvalue())
    os.replace(tmp, cached)
    return Response(content=buf.getvalue(), media_type="image/jpeg", headers=headers)


@app.get("/api/output-images/{filename:path}")
def get_output_image(filename: str):
    # 갤러리 라이트박스에서 원본 크기로 보여주는 용도.
    return FileResponse(resolve_output_image(filename))


@app.delete("/api/output-images/{filename:path}")
def delete_output_image(filename: str):
    # 갤러리에서 이미지 하나만 지우는 용도 — 되돌릴 수 없는 삭제라서, 확인 절차는
    # 프론트엔드(버튼 클릭 시 confirm 창)가 맡는다.
    path = resolve_output_image(filename)
    path.unlink()
    drop_thumb_cache(path)
    return {"ok": True}


@app.post("/api/output-images/{filename:path}/rotate")
async def rotate_output_image(filename: str):
    # 갤러리 라이트박스에서 지금 보고 있는 이미지 한 장을 시계 방향 90도로 돌린다 —
    # rotate-landscape/download-selected-rotated와 달리 원본을 그 자리에서 실제로
    # 덮어쓴다(사용자가 직접 누른 명시적인 동작이므로).
    path = resolve_output_image(filename)
    await asyncio.to_thread(rotate_image_file, path)
    return {"ok": True}


def parse_image_names_body(data: dict) -> list[str]:
    names = data.get("names")
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise HTTPException(400, "이미지 파일명 목록(names)이 필요해요.")
    return names


def build_zip_from_paths(paths: list[Path], rotate_landscape: bool = False) -> Path:
    # 작업별 하위 폴더 구조 없이 파일명만으로 평평하게 담는다 — 압축을 풀었을 때
    # 폴더 구조 없이 한 자리에 전부 모여있길 원해서다. 서로 다른 작업 폴더에서
    # 온 파일이 우연히 같은 이름이면 zip 안에서 이름이 겹치므로, 그런 경우에만
    # "이름 (1).ext"처럼 번호를 붙여 구분한다.
    #
    # rotate_landscape=True면 가로형(너비>높이) 이미지만 시계 방향 90도로 돌려서
    # zip에 담는다 — 원본 파일은 절대 건드리지 않는다(메모리에서만 돌려서 그
    # 결과 바이트만 zip에 씀). "회전 후 다운로드" 버튼이 쓰는 옵션으로, 예전에는
    # 원본 파일 자체를 영구히 돌려버렸는데(디스크에 덮어씀) 그 회전이 이 다운로드
    # 한 번을 위한 것일 뿐이라 원본은 그대로 두고 다운로드본만 돌리도록 바꿨다.
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    tmp_path = Path(tmp.name)
    used_names: set[str] = set()
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in paths:
            arcname = f.name
            if arcname in used_names:
                stem, suffix = f.stem, f.suffix
                n = 1
                while f"{stem} ({n}){suffix}" in used_names:
                    n += 1
                arcname = f"{stem} ({n}){suffix}"
            used_names.add(arcname)

            rotated_bytes = None
            if rotate_landscape:
                with Image.open(f) as img:
                    if img.width > img.height:
                        rotated = img.transpose(Image.Transpose.ROTATE_270)
                        buf = io.BytesIO()
                        rotated.save(buf, format=img.format or "PNG")
                        rotated_bytes = buf.getvalue()
            if rotated_bytes is not None:
                zf.writestr(arcname, rotated_bytes)
            else:
                zf.write(f, arcname=arcname)
    return tmp_path


def build_output_zip(only_paths: set[str] | None = None) -> Path:
    files = find_image_files(OUTPUT_DIR, only_paths)
    return build_zip_from_paths(files)


@app.get("/api/download-images")
async def download_images(request: Request):
    only = asset_meta.owned_paths(me(request)["id"])
    # 압축은 시간이 걸릴 수 있으니 이벤트 루프를 막지 않게 스레드에서 처리하고,
    # 임시로 만든 zip 파일은 응답이 끝난 뒤 백그라운드에서 지운다.
    try:
        zip_path = await asyncio.to_thread(build_output_zip, only)
    except EmailSendError as e:
        raise HTTPException(404, str(e))

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"nightshift_output_{timestamp}.zip",
        background=BackgroundTask(lambda: zip_path.unlink(missing_ok=True)),
    )


@app.post("/api/output-images/download-selected")
async def download_selected_images(request: Request):
    # 갤러리에서 여러 장을 선택해 한 번에 받는 용도 — 잘못된 이름이나 그 사이 지워진
    # 파일은 조용히 건너뛰고, 하나라도 남아있으면 그것만으로 zip을 만든다.
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    names = parse_image_names_body(data)

    paths = []
    for name in names:
        try:
            paths.append(resolve_output_image(name))
        except HTTPException:
            continue
    if not paths:
        raise HTTPException(404, "선택한 이미지를 찾을 수 없어요.")

    zip_path = await asyncio.to_thread(build_zip_from_paths, paths)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"nightshift_selected_{timestamp}.zip",
        background=BackgroundTask(lambda: zip_path.unlink(missing_ok=True)),
    )


@app.delete("/api/output-images")
async def delete_images(request: Request):
    only = asset_meta.owned_paths(me(request)["id"])
    # 되돌릴 수 없는 삭제라서, 확인 절차는 프론트엔드(버튼 클릭 시 confirm 창)가 맡는다.
    try:
        files = await asyncio.to_thread(list_output_images, OUTPUT_DIR, only)
        deleted = await asyncio.to_thread(delete_output_images, OUTPUT_DIR, only)
    except OutputFolderError as e:
        raise HTTPException(404, str(e))
    for f in files:
        drop_thumb_cache(f)
    return {"deleted": deleted}


@app.post("/api/output-images/delete-selected")
async def delete_selected_images(request: Request):
    # 갤러리에서 여러 장을 선택해 한 번에 지우는 용도 — 되돌릴 수 없는 삭제라서, 확인
    # 절차는 프론트엔드(버튼 클릭 시 confirm 창)가 맡는다. 잘못된 이름이나 그 사이 이미
    # 지워진 파일은 조용히 건너뛴다.
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    names = parse_image_names_body(data)

    deleted = 0
    for name in names:
        try:
            path = resolve_output_image(name)
        except HTTPException:
            continue
        path.unlink()
        drop_thumb_cache(path)
        deleted += 1
    return {"deleted": deleted}


@app.post("/api/output-images/rotate-selected")
async def rotate_selected_images(request: Request):
    # 갤러리에서 여러 장을 선택해 한 번에 돌리는 용도 — download-selected-rotated와
    # 달리 원본을 그 자리에서 실제로 덮어쓴다. 잘못된 이름이나 그 사이 지워진 파일은
    # 조용히 건너뛴다.
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    names = parse_image_names_body(data)

    rotated = 0
    errors = []
    for name in names:
        try:
            path = resolve_output_image(name)
        except HTTPException:
            continue
        try:
            await asyncio.to_thread(rotate_image_file, path)
        except Exception as e:
            errors.append(f"{name}: {e}")
            continue
        rotated += 1
    return {"rotated": rotated, "errors": errors}


@app.post("/api/output-images/rotate-landscape")
async def rotate_images(request: Request):
    only = asset_meta.owned_paths(me(request)["id"])
    try:
        result = await asyncio.to_thread(rotate_landscape_images, OUTPUT_DIR, only)
    except OutputFolderError as e:
        raise HTTPException(404, str(e))
    return result


@app.post("/api/output-images/download-selected-rotated")
async def download_selected_images_rotated(request: Request):
    # "회전 후 다운로드" 버튼용 — 가로형만 시계 방향 90도로 돌려서 zip에 담아
    # 내려준다. download-selected와 달리 원본 파일은 전혀 건드리지 않는다(예전엔
    # /api/output-images/rotate-selected로 원본을 영구히 덮어쓴 뒤 다운로드했는데,
    # 그 회전이 이 한 번의 다운로드만을 위한 것일 뿐 갤러리에 계속 남아있을
    # 이유가 없어서, 다운로드되는 바이트만 돌리고 저장된 원본은 그대로 두게
    # 바꿨다 — build_zip_from_paths의 rotate_landscape 참고).
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    names = parse_image_names_body(data)

    paths = []
    for name in names:
        try:
            paths.append(resolve_output_image(name))
        except HTTPException:
            continue
    if not paths:
        raise HTTPException(404, "선택한 이미지를 찾을 수 없어요.")

    zip_path = await asyncio.to_thread(build_zip_from_paths, paths, rotate_landscape=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"nightshift_selected_rotated_{timestamp}.zip",
        background=BackgroundTask(lambda: zip_path.unlink(missing_ok=True)),
    )


def list_output_videos_meta(user: dict) -> list[dict]:
    scope = auth.owner_scope(user)
    names = auth.usernames() if scope is None else {}
    # 영상 갤러리 탭을 채우는 용도. 이미지와 같은 출력 폴더를 보되 동영상 확장자만
    # 걸러낸다 — width/height는 ffprobe 없이는 못 읽으므로(의도적으로 새 시스템
    # 의존성을 추가하지 않기로 함) 내지 않는다. 자세히 보기가 없는 이유도 같다.
    try:
        files = list_output_videos(OUTPUT_DIR, only_paths=_own_paths(user))
    except OutputFolderError:
        return []
    base = Path(OUTPUT_DIR)
    pod_ids = synced_pod_ids()  # list_output_images_meta 참고 — job_id 없는 영상용.
    meta_of = _meta_resolver(scope)
    items = []
    for f in files:
        stat = f.stat()
        rel = f.relative_to(base)
        name = rel.as_posix()
        job_id = rel.parts[0] if len(rel.parts) > 1 else None
        items.append({
            "name": name,
            "job_id": job_id,
            "synced_pod_id": pod_ids.get(name),
            **meta_of(name, job_id),
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })
    if scope is None:   # 관리자에게는 누구 것인지 알려 준다
        for it in items:
            it["owner_name"] = names.get(it.get("owner_id"))
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items


