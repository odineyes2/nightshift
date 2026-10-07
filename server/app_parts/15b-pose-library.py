# Library > Pose(NS-39) — 포즈 레퍼런스 게시물 목록·추가·이미지. 회원마다 자기 것만, admin은 전부 본다.
# app.py가 app_parts/*.py를 번호 순서대로 한 네임스페이스에서 실행한다 — 앞 파일의 이름을 import 없이 쓴다.


@app.get("/api/library/poses")
def list_library_poses(request: Request):
    return {"items": pose_library.list_poses(auth.owner_scope(me(request)))}


async def _library_form_images(form) -> list:
    """multipart의 image 파일 여러 개와 image_url 여러 개(한 칸에 줄마다 하나도 됨)를 [(바이트, 원본 주소)]로."""
    images = [(await f.read(pose_library.MAX_BYTES + 1), "")
              for f in form.getlist("image") if isinstance(f, UploadFile) and f.filename]
    urls = [u.strip() for v in form.getlist("image_url") for u in str(v).splitlines() if u.strip()]
    if len(images) + len(urls) > pose_library.MAX_IMAGES:   # 주소를 받기 전에 막는다
        raise pose_library.PoseError(f"이미지는 한 번에 {pose_library.MAX_IMAGES}장까지 넣을 수 있어요.")
    for url in urls:
        images.append((await asyncio.to_thread(pose_library.fetch_image, url), url))
    return images


def _library_pose(request: Request, pose_id: int) -> dict:
    pose = pose_library.get_pose(pose_id)
    if not pose or not auth.can_access(me(request), pose["owner_id"]):
        raise HTTPException(404, "게시물을 찾을 수 없어요.")
    return pose


@app.post("/api/library/poses")
async def add_library_pose(request: Request):
    """multipart: name, description, danbooru_prompt, image(파일 여러 개)·image_url(여러 개) — 합쳐 1~20장."""
    user = me(request)
    form = await request.form()
    try:
        images = await _library_form_images(form)
        return await asyncio.to_thread(
            pose_library.add_pose, user["id"], str(form.get("name") or ""), str(form.get("description") or ""),
            str(form.get("danbooru_prompt") or ""), images)
    except pose_library.PoseError as e:
        raise HTTPException(400, str(e))


@app.post("/api/library/poses/{pose_id}/images")
async def add_library_pose_images(request: Request, pose_id: int):
    """기존 게시물에 이미지를 덧붙인다 — 주인 또는 admin만(아니면 404)."""
    _library_pose(request, pose_id)
    form = await request.form()
    try:
        images = await _library_form_images(form)
        return await asyncio.to_thread(pose_library.add_images, pose_id, images)
    except pose_library.PoseError as e:
        raise HTTPException(400, str(e))


def _library_pose_file(request: Request, pose_id: int, image_id, thumb: bool):
    image = pose_library.get_pose_image(_library_pose(request, pose_id), image_id)
    path = pose_library.image_path(image, thumb) if image else None
    if not path or not path.is_file():
        raise HTTPException(404, "이미지 파일이 없어요.")
    return FileResponse(path)


@app.get("/api/library/poses/{pose_id}/image")
def get_library_pose_image(request: Request, pose_id: int):
    return _library_pose_file(request, pose_id, None, False)


@app.get("/api/library/poses/{pose_id}/thumb")
def get_library_pose_thumb(request: Request, pose_id: int):
    return _library_pose_file(request, pose_id, None, True)


@app.get("/api/library/poses/{pose_id}/images/{image_id}/image")
def get_library_pose_image_n(request: Request, pose_id: int, image_id: int):
    return _library_pose_file(request, pose_id, image_id, False)


@app.get("/api/library/poses/{pose_id}/images/{image_id}/thumb")
def get_library_pose_thumb_n(request: Request, pose_id: int, image_id: int):
    return _library_pose_file(request, pose_id, image_id, True)


@app.post("/api/library/poses/{pose_id}/images/{image_id}/to-input")
async def library_pose_to_input(request: Request, pose_id: int, image_id: int):
    """"이 포즈로 생성"(NS-41) — 고른 장을 요청한 회원의 입력 이미지 풀로 복사하고 {name, danbooru_prompt}를
    돌려준다. 주인 또는 admin만(아니면 404). 같은 이름·같은 내용이 이미 있으면 사본을 새로 만들지 않는다."""
    pose = _library_pose(request, pose_id)
    image = pose_library.get_pose_image(pose, image_id)
    if not image or not pose_library.image_path(image, False).is_file():
        raise HTTPException(404, "이미지 파일이 없어요.")
    name, _ = await _pose_to_input(pose, image)
    return {"name": name, "danbooru_prompt": pose.get("danbooru_prompt") or ""}


async def _pose_to_input(pose: dict, image: dict) -> tuple[str, bytes]:
    """포즈 한 장을 입력 이미지 풀로 복사하고 (풀의 이름, 바이트)를 돌려준다. 같은 내용이면 새로 쓰지 않는다."""
    try:
        name, content = await asyncio.to_thread(pose_library.input_copy, pose, image)
    except pose_library.PoseError as e:
        raise HTTPException(400, str(e))
    try:
        same = await asyncio.to_thread(resolve_input_image(name).read_bytes) == content
    except InputAssetError:
        same = False
    if not same:
        name = await asyncio.to_thread(save_input_image, name, content)
    return name, content


@app.post("/api/library/poses/to-input")
async def library_poses_to_input(request: Request):
    """순차 생성(NS-42) — 본문 {"pose_ids": [...]}의 게시물마다 모든 이미지를 입력 이미지 풀로 복사하고
    목록 순서대로 [{pose_id, pose_name, image_id, name, danbooru_prompt, width, height, sdxl_width, sdxl_height}]를
    돌려준다. 하나라도 볼 수 없는 게시물이면 404."""
    try:
        body = json.loads(await request.body())
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    ids = body.get("pose_ids") if isinstance(body, dict) else None
    if not isinstance(ids, list) or not ids or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        raise HTTPException(400, "pose_ids는 게시물 번호 배열이어야 해요.")
    poses = [_library_pose(request, i) for i in ids]  # 복사 전에 전부 검사한다
    out = []
    for pose in poses:
        for image in pose["images"]:
            if not pose_library.image_path(image, False).is_file():
                continue
            name, content = await _pose_to_input(pose, image)
            try:
                w, h = await asyncio.to_thread(pose_library.image_size, content)
            except pose_library.PoseError as e:
                raise HTTPException(400, str(e))
            sw, sh = pose_library.sdxl_size(w, h)
            out.append({"pose_id": pose["id"], "pose_name": pose.get("name") or "", "image_id": image["id"],
                        "name": name, "danbooru_prompt": pose.get("danbooru_prompt") or "",
                        "width": w, "height": h, "sdxl_width": sw, "sdxl_height": sh})
    return out
