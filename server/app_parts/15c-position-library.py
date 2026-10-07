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
