# app.py가 app_parts/*.py를 번호 순서대로 한 네임스페이스에서 실행한다 — 앞 파일의 이름을 import 없이 쓴다.
# ---- 모델 내려받기 (model_download.py) ----------------------------------------------

def _download_pod(user: dict, pod_id: str) -> dict:
    pod = pod_or_404(user, pod_id)
    if pod.get("kind") != pod_registry.DEFAULT_KIND:
        raise HTTPException(400, "ComfyUI 워커만 모델을 받을 수 있어요.")
    return pod


def _download_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except model_download.DownloadError as e:
        raise HTTPException(e.status, str(e))


_downloads_seen_done: set = set()


@app.post("/api/models/resolve")
async def resolve_model_source(request: Request):
    me(request)
    data = await read_json_object(request, allow_empty=False)
    token = str(data.get("token") or "").strip() or None
    return await asyncio.to_thread(_download_call, model_download.resolve, str(data.get("url") or ""), token)


@app.get("/api/models/downloader/status")
async def downloader_status(request: Request, pod_id: str):
    pod = _download_pod(me(request), pod_id)
    return await asyncio.to_thread(model_download.node_status, pod)


@app.get("/api/models/downloader/install-script")
def downloader_install_script(request: Request, pod_id: str):
    pod = _download_pod(me(request), pod_id)
    return Response(model_download.install_script(pod["id"]), media_type="text/plain; charset=utf-8",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/models/download")
async def start_model_download(request: Request):
    user = me(request)
    data = await read_json_object(request, allow_empty=False)
    pod = _download_pod(user, str(data.get("pod_id") or ""))
    url = str(data.get("url") or "").strip()
    kind = str(data.get("kind") or "")
    filename = str(data.get("filename") or "").strip()
    if kind not in model_registry.KIND_IDS:
        raise HTTPException(400, "모델 종류를 골라주세요.")
    if not url or not filename:
        raise HTTPException(400, "주소와 파일명이 필요해요.")
    token = str(data.get("token") or "").strip() or None
    body = {"url": url, "folder": kind, "filename": filename, "overwrite": bool(data.get("overwrite")),
            "headers": model_download.auth_header_for(url, token)}
    result = await asyncio.to_thread(_download_call, model_download.call_node, pod, "POST", "/nightshift/dl/start", body)
    # 등록부는 관리자만 고칠 수 있다 — 관리자가 받을 때만 Civitai/HF에서 알아낸 정보를 함께 적어 둔다.
    meta = data.get("meta")
    if auth.is_admin(user) and isinstance(meta, dict):
        fields = {k: meta[k] for k in model_registry.FIELDS if k in meta}
        try:
            model_registry.upsert(kind, filename, fields)
        except model_registry.RegistryError:
            pass
    return result


@app.get("/api/models/downloads")
async def list_model_downloads(request: Request, pod_id: str):
    pod = _download_pod(me(request), pod_id)
    result = await asyncio.to_thread(_download_call, model_download.call_node, pod, "GET", "/nightshift/dl/status")
    for item in result.get("downloads", []):
        key = (pod["id"], item.get("id"))
        if item.get("status") == "done" and key not in _downloads_seen_done:
            _downloads_seen_done.add(key)
            model_download.ComfyUIDriver.invalidate_capabilities(pod["id"])   # 새 파일이 설치 목록에 바로 보이게
    return result


@app.post("/api/models/downloads/cancel")
async def cancel_model_download(request: Request):
    user = me(request)
    data = await read_json_object(request, allow_empty=False)
    pod = _download_pod(user, str(data.get("pod_id") or ""))
    return await asyncio.to_thread(_download_call, model_download.call_node, pod, "POST",
                                   "/nightshift/dl/cancel", {"id": str(data.get("id") or "")})


@app.get("/api/models")
def get_model_registry():
    """모델 등록부 — 파일 종류 목록과 지금까지 정보를 적어 둔 항목들. 어느 파드에 뭐가 설치돼
    있는지는 /api/comfy-object-info가 알려 주고, 화면이 둘을 파일명으로 합친다."""
    return {
        "kinds": [{"id": k, "label": label} for k, label in model_registry.KINDS],
        "base_models": model_registry.BASE_MODELS,
        "items": model_registry.list_entries(),
    }


@app.put("/api/models")
async def put_model_registry_entry(request: Request):
    admin_only(request)
    try:
        data = json.loads((await request.body()).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    if not isinstance(data, dict):
        raise HTTPException(400, "{kind, filename, ...} 형태의 객체여야 해요.")
    fields = {k: data[k] for k in model_registry.FIELDS if k in data}
    try:
        entry = model_registry.upsert(str(data.get("kind") or ""), str(data.get("filename") or ""), fields)
    except model_registry.RegistryError as e:
        raise HTTPException(400, str(e))
    return {"entry": entry}


@app.get("/api/models/download-command")
def model_download_command(request: Request, kind: str, filename: str):
    # RunPod 자체 터미널에 붙여넣을 curl 명령 한 줄 — model_download.py의 파드 내
    # 다운로더 노드 인프라와는 별개다(그건 프론트에서 아직 안 쓴다). 명령에 civitai
    # 토큰이 그대로 드러나므로 등록 정보 수정과 같은 기준으로 관리자만 쓸 수 있다.
    admin_only(request)
    entry = model_registry.get_entry(kind, filename)
    if not entry or not entry.get("download_url"):
        raise HTTPException(400, "이 모델에는 다운로드 주소가 없어요.")
    url = entry["download_url"]
    # kind가 곧 ComfyUI의 실제 모델 폴더 이름이라 그대로 받을 폴더가 된다. filename에
    # "/"가 있으면 그 앞부분은 kind 폴더 밑의 하위 폴더(ComfyUI 로더들이 combo 값에
    # 쓰는 "하위폴더/이름" 표기와 같은 규칙), 없으면 kind 폴더 바로 밑이다.
    sub, base = posixpath.split(filename)
    target = f"/workspace/shared_models/{kind}" + (f"/{sub}" if sub else "") + f"/{base}"
    header = ""
    warning = None
    if "civitai" in (urllib.parse.urlparse(url).hostname or ""):
        token = os.environ.get("CIVITAI_TOKEN", "").strip()
        if token:
            header = f" -H {shlex.quote('Authorization: Bearer ' + token)}"
        else:
            warning = "CIVITAI_TOKEN이 .env에 없어요 — 토큰 없이 받아지는 파일만 될 거예요."
    command = f"curl -L --create-dirs{header} -o {shlex.quote(target)} {shlex.quote(url)}"
    return {"command": command, "target": target, "warning": warning}


@app.get("/api/input-images")
def get_input_images():
    # img2img/USDU 워크플로우 유형과 영상 생성(WAN2.2) 템플릿의 "입력 이미지"
    # 선택 드롭다운을 채우는 데 쓴다. ref_assets의 pose/depth/lineart와 달리
    # 세트/char_no 구분이 없는 평평한 목록(input_assets.py 모듈 설명 참고).
    return {"images": list_input_images()}


@app.post("/api/input-images")
async def upload_input_image(request: Request):
    # "이미지 선택 — 업로드" 경로(영상 생성 작업 화면, img2img "입력 이미지" 필드
    # 둘 다 이 풀을 공유한다). 파일 하나만 받는다 — 여러 장을 한 번에 올릴 일이
    # 없어서(그때그때 하나씩 골라 쓰는 용도) 단순하게 뒀다.
    form = await request.form()
    file = form.get("image")
    if not isinstance(file, UploadFile) or not file.filename:
        raise HTTPException(400, "이미지 파일을 첨부하세요.")
    content = await file.read()
    try:
        name = await asyncio.to_thread(save_input_image, file.filename, content)
    except InputAssetError as e:
        raise HTTPException(400, str(e))
    return {"name": name}


@app.delete("/api/input-images/{name}")
def delete_input_image_api(name: str):
    try:
        delete_input_image(name)
    except InputAssetError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@app.get("/api/input-images/{name}/raw")
def get_input_image_raw(name: str):
    # 참조 슬롯 카드 갤러리의 <img>가 직접 가리키는 주소 — 원본을 그대로 내려준다.
    # 입력 이미지는 보통 크지 않아 별도 축소본 생성 없이 원본으로 충분하다.
    try:
        path = resolve_input_image(name)
    except InputAssetError as e:
        raise HTTPException(404, str(e))
    return FileResponse(path)


# MiniMax-H3 r2v의 비디오/오디오 참조 선택 드롭다운을 채운다 — 이미지 풀과 같은 디렉토리를
# 공유하고 확장자로만 구분한다(input_assets.py 모듈 설명 참고). 결과 갤러리에서 가져오기는
# 참조용 원본이 보통 사용자 컴퓨터에 있어 두지 않았다(이미지의 import-from-output과 다름).
@app.get("/api/input-videos")
def get_input_videos():
    return {"videos": list_input_videos()}


@app.post("/api/input-videos")
async def upload_input_video(request: Request):
    form = await request.form()
    file = form.get("video")
    if not isinstance(file, UploadFile) or not file.filename:
        raise HTTPException(400, "비디오 파일을 첨부하세요.")
    content = await file.read()
    try:
        name = await asyncio.to_thread(save_input_video, file.filename, content)
    except InputAssetError as e:
        raise HTTPException(400, str(e))
    return {"name": name}


@app.delete("/api/input-videos/{name}")
def delete_input_video_api(name: str):
    try:
        delete_input_video(name)
    except InputAssetError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@app.get("/api/input-videos/{name}/raw")
def get_input_video_raw(name: str):
    # 참조 슬롯 카드 갤러리의 <video>가 직접 가리키는 주소 — 서버에서 썸네일을 만들지
    # 않고 원본을 그대로 내려주면, 브라우저가 preload="metadata"로 첫 프레임을 알아서
    # 그린다(ffmpeg 등 서버 쪽 의존성을 늘리지 않으려는 선택).
    try:
        path = resolve_input_video(name)
    except InputAssetError as e:
        raise HTTPException(404, str(e))
    return FileResponse(path)


@app.get("/api/input-audios")
def get_input_audios():
    return {"audios": list_input_audios()}


@app.post("/api/input-audios")
async def upload_input_audio(request: Request):
    form = await request.form()
    file = form.get("audio")
    if not isinstance(file, UploadFile) or not file.filename:
        raise HTTPException(400, "오디오 파일을 첨부하세요.")
    content = await file.read()
    try:
        name = await asyncio.to_thread(save_input_audio, file.filename, content)
    except InputAssetError as e:
        raise HTTPException(400, str(e))
    return {"name": name}


@app.delete("/api/input-audios/{name}")
def delete_input_audio_api(name: str):
    try:
        delete_input_audio(name)
    except InputAssetError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@app.post("/api/input-images/import-from-output")
async def import_output_images_to_input_pool(request: Request):
    # "이미지 선택 — 갤러리에서 선택" 경로 — 결과 이미지 갤러리에서 고른 이미지를
    # 입력 이미지 풀로 사본을 만든다. /api/assets/import-from-output(참조 세트로
    # 보내기)과 같은 패턴: 원본은 지우지 않고 복사만 하며, 그 사이 지워진 이미지는
    # 조용히 건너뛴다.
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    names = parse_image_names_body(data)

    added = []
    skipped = []
    for name in names:
        try:
            added.append(await _import_output_image_to_input_pool(name))
        except HTTPException as e:
            skipped.append({"name": name, "reason": e.detail})
        except (OSError, InputAssetError) as e:
            skipped.append({"name": name, "reason": f"저장 실패: {e}"})
    return {"added": added, "skipped": skipped}


async def _import_output_image_to_input_pool(name: str) -> str:
    """결과 이미지 하나를 입력 이미지 풀로 복사하고 풀 안의 이름을 돌려준다(갤러리의 "갤러리에서
    선택"·"영상 만들기"와 보드 생성 카드의 입구가 같이 쓴다). 없는 이미지면 HTTPException.
    주인 확인은 부르는 쪽 몫이다."""
    path = resolve_output_image(name)
    # job_id별 하위 폴더에서 온 이름이라, 평평한 입력 이미지 풀에 맞게
    # "<job_id>_<원본파일명>"으로 합친다(참조 세트 가져오기와 같은 규칙).
    parts = name.split("/")
    dest_filename = f"{parts[0]}_{parts[-1]}" if len(parts) > 1 else parts[0]
    content = await asyncio.to_thread(path.read_bytes)
    # 같은 이미지로 여러 번 만들 때마다 _2, _3 사본이 쌓이지 않게, 이미 같은 이름·같은
    # 내용의 파일이 풀에 있으면 그걸 그대로 쓴다.
    try:
        existing = resolve_input_image(dest_filename)
        same = await asyncio.to_thread(existing.read_bytes) == content
    except InputAssetError:
        same = False
    return dest_filename if same else await asyncio.to_thread(save_input_image, dest_filename, content)


