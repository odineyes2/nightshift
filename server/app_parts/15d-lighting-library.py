# Library > Lighting(NS-60-1) — Pose와 같은 게시물 목록·추가·이미지에 수정·삭제를 더한다. 회원마다 자기 것만, admin은 전부.
# app.py가 app_parts/*.py를 번호 순서대로 한 네임스페이스에서 실행한다 — 15b의 _library_form_images를 그대로 쓴다.


@app.get("/api/library/lightings")
def list_library_lightings(request: Request):
    return {"items": lighting_library.list_lightings(auth.owner_scope(me(request)))}


def _library_lighting(request: Request, lighting_id: int) -> dict:
    item = lighting_library.get_lighting(lighting_id)
    if not item or not auth.can_access(me(request), item["owner_id"]):
        raise HTTPException(404, "게시물을 찾을 수 없어요.")
    return item


@app.post("/api/library/lightings")
async def add_library_lighting(request: Request):
    """multipart: name, description, danbooru_prompt, image(파일 여러 개)·image_url(여러 개) — 합쳐 1~20장."""
    user = me(request)
    form = await request.form()
    try:
        images = await _library_form_images(form)
        return await asyncio.to_thread(
            lighting_library.add_lighting, user["id"], str(form.get("name") or ""),
            str(form.get("description") or ""), str(form.get("danbooru_prompt") or ""), images)
    except pose_library.PoseError as e:
        raise HTTPException(400, str(e))


@app.post("/api/library/lightings/{lighting_id}/images")
async def add_library_lighting_images(request: Request, lighting_id: int):
    """기존 게시물에 이미지를 덧붙인다 — 주인 또는 admin만(아니면 404)."""
    _library_lighting(request, lighting_id)
    form = await request.form()
    try:
        images = await _library_form_images(form)
        return await asyncio.to_thread(lighting_library.add_images, lighting_id, images)
    except pose_library.PoseError as e:
        raise HTTPException(400, str(e))


@app.patch("/api/library/lightings/{lighting_id}")
async def update_library_lighting(request: Request, lighting_id: int):
    """본문 JSON {name?, description?, danbooru_prompt?} — 온 칸만 바꾼다."""
    _library_lighting(request, lighting_id)
    try:
        body = json.loads(await request.body())
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    if not isinstance(body, dict):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    try:
        return lighting_library.update_lighting(lighting_id, body)
    except pose_library.PoseError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/library/lightings/{lighting_id}")
def delete_library_lighting(request: Request, lighting_id: int):
    """게시물과 모든 이미지 파일을 지운다."""
    lighting_library.delete_lighting(_library_lighting(request, lighting_id))
    return {"ok": True}


@app.delete("/api/library/lightings/{lighting_id}/images/{image_id}")
def delete_library_lighting_image(request: Request, lighting_id: int, image_id: int):
    """이미지 한 장을 지운다 — 마지막 한 장이면 400."""
    try:
        return lighting_library.delete_image(_library_lighting(request, lighting_id), image_id)
    except LookupError:
        raise HTTPException(404, "이미지를 찾을 수 없어요.")
    except pose_library.PoseError as e:
        raise HTTPException(400, str(e))


def _library_lighting_file(request: Request, lighting_id: int, image_id: int, thumb: bool):
    image = lighting_library.get_image(_library_lighting(request, lighting_id), image_id)
    path = lighting_library.image_path(image, thumb) if image else None
    if not path or not path.is_file():
        raise HTTPException(404, "이미지 파일이 없어요.")
    return FileResponse(path)


@app.get("/api/library/lightings/{lighting_id}/images/{image_id}/image")
def get_library_lighting_image(request: Request, lighting_id: int, image_id: int):
    return _library_lighting_file(request, lighting_id, image_id, False)


@app.get("/api/library/lightings/{lighting_id}/images/{image_id}/thumb")
def get_library_lighting_thumb(request: Request, lighting_id: int, image_id: int):
    return _library_lighting_file(request, lighting_id, image_id, True)

