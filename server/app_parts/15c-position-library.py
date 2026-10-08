# Library > Position(NS-43) — Pose와 같은 게시물 목록·추가·이미지에 수정·삭제를 더한다. 회원마다 자기 것만, admin은 전부.
# app.py가 app_parts/*.py를 번호 순서대로 한 네임스페이스에서 실행한다 — 15b의 _library_form_images를 그대로 쓴다.


@app.get("/api/library/positions")
def list_library_positions(request: Request):
    return {"items": position_library.list_positions(auth.owner_scope(me(request)))}


def _library_position(request: Request, position_id: int) -> dict:
    item = position_library.get_position(position_id)
    if not item or not auth.can_access(me(request), item["owner_id"]):
        raise HTTPException(404, "게시물을 찾을 수 없어요.")
    return item


@app.post("/api/library/positions")
async def add_library_position(request: Request):
    """multipart: name, description, danbooru_prompt, image(파일 여러 개)·image_url(여러 개) — 합쳐 1~20장."""
    user = me(request)
    form = await request.form()
    try:
        images = await _library_form_images(form)
        return await asyncio.to_thread(
            position_library.add_position, user["id"], str(form.get("name") or ""),
            str(form.get("description") or ""), str(form.get("danbooru_prompt") or ""), images)
    except pose_library.PoseError as e:
        raise HTTPException(400, str(e))


@app.post("/api/library/positions/{position_id}/images")
async def add_library_position_images(request: Request, position_id: int):
    """기존 게시물에 이미지를 덧붙인다 — 주인 또는 admin만(아니면 404)."""
    _library_position(request, position_id)
    form = await request.form()
    try:
        images = await _library_form_images(form)
        return await asyncio.to_thread(position_library.add_images, position_id, images)
    except pose_library.PoseError as e:
        raise HTTPException(400, str(e))


@app.patch("/api/library/positions/{position_id}")
async def update_library_position(request: Request, position_id: int):
    """본문 JSON {name?, description?, danbooru_prompt?} — 온 칸만 바꾼다."""
    _library_position(request, position_id)
    try:
        body = json.loads(await request.body())
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    if not isinstance(body, dict):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    try:
        return position_library.update_position(position_id, body)
    except pose_library.PoseError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/library/positions/{position_id}")
def delete_library_position(request: Request, position_id: int):
    """게시물과 모든 이미지 파일을 지운다."""
    position_library.delete_position(_library_position(request, position_id))
    return {"ok": True}


@app.post("/api/library/positions/{position_id}/move-to-pose")
async def move_library_position_to_pose(request: Request, position_id: int):
    """게시물을 Pose로 옮긴다(원본은 지움) — 응답은 새 Pose 게시물(id가 바뀐다)."""
    position = _library_position(request, position_id)
    try:
        return await asyncio.to_thread(library_move.move_position_to_pose, position)
    except (pose_library.PoseError, OSError) as e:
        raise HTTPException(400, f"옮기지 못했어요: {e}")


@app.delete("/api/library/positions/{position_id}/images/{image_id}")
def delete_library_position_image(request: Request, position_id: int, image_id: int):
    """이미지 한 장을 지운다 — 마지막 한 장이면 400."""
    try:
        return position_library.delete_image(_library_position(request, position_id), image_id)
    except LookupError:
        raise HTTPException(404, "이미지를 찾을 수 없어요.")
    except pose_library.PoseError as e:
        raise HTTPException(400, str(e))


def _library_position_file(request: Request, position_id: int, image_id: int, thumb: bool):
    image = position_library.get_image(_library_position(request, position_id), image_id)
    path = position_library.image_path(image, thumb) if image else None
    if not path or not path.is_file():
        raise HTTPException(404, "이미지 파일이 없어요.")
    return FileResponse(path)


@app.get("/api/library/positions/{position_id}/images/{image_id}/image")
def get_library_position_image(request: Request, position_id: int, image_id: int):
    return _library_position_file(request, position_id, image_id, False)


@app.get("/api/library/positions/{position_id}/images/{image_id}/thumb")
def get_library_position_thumb(request: Request, position_id: int, image_id: int):
    return _library_position_file(request, position_id, image_id, True)


async def _position_to_input(position: dict, image: dict) -> tuple[str, bytes]:
    """Position 한 장을 입력 이미지 풀로 복사하고 (풀의 이름, 바이트)를 돌려준다 — 15b의 _pose_to_input과 같다."""
    try:
        name, content = await asyncio.to_thread(position_library.input_copy, position, image)
    except pose_library.PoseError as e:
        raise HTTPException(400, str(e))
    try:
        same = await asyncio.to_thread(resolve_input_image(name).read_bytes) == content
    except InputAssetError:
        same = False
    if not same:
        name = await asyncio.to_thread(save_input_image, name, content)
    return name, content


@app.post("/api/library/positions/{position_id}/images/{image_id}/to-input")
async def library_position_to_input(request: Request, position_id: int, image_id: int):
    """"Openpose CN"으로 생성(NS-51) — 고른 장을 요청한 회원의 입력 이미지 풀로 복사하고 {name, danbooru_prompt}."""
    position = _library_position(request, position_id)
    image = position_library.get_image(position, image_id)
    if not image or not position_library.image_path(image, False).is_file():
        raise HTTPException(404, "이미지 파일이 없어요.")
    name, _ = await _position_to_input(position, image)
    return {"name": name, "danbooru_prompt": position.get("danbooru_prompt") or ""}


@app.post("/api/library/positions/to-input")
async def library_positions_to_input(request: Request):
    """순차 생성(NS-51) — 본문 {"position_ids": [...]}의 게시물마다 모든 이미지를 입력 이미지 풀로 복사하고
    목록 순서대로 [{position_id, position_name, image_id, name, danbooru_prompt, width, height, sdxl_width, sdxl_height}].
    하나라도 볼 수 없는 게시물이면 404."""
    try:
        body = json.loads(await request.body())
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    ids = body.get("position_ids") if isinstance(body, dict) else None
    if not isinstance(ids, list) or not ids or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        raise HTTPException(400, "position_ids는 게시물 번호 배열이어야 해요.")
    positions = [_library_position(request, i) for i in ids]  # 복사 전에 전부 검사한다
    out = []
    for position in positions:
        for image in position["images"]:
            if not position_library.image_path(image, False).is_file():
                continue
            name, content = await _position_to_input(position, image)
            try:
                w, h = await asyncio.to_thread(pose_library.image_size, content)
            except pose_library.PoseError as e:
                raise HTTPException(400, str(e))
            sw, sh = pose_library.sdxl_size(w, h)
            out.append({"position_id": position["id"], "position_name": position.get("name") or "",
                        "image_id": image["id"], "name": name,
                        "danbooru_prompt": position.get("danbooru_prompt") or "",
                        "width": w, "height": h, "sdxl_width": sw, "sdxl_height": sh})
    return out
