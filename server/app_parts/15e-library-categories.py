# 사용자 정의 Library 카테고리·아티클의 인증 API를 정적 파일 마운트 앞에 등록한다.


def _custom_category(request: Request, category_id: int) -> dict:
    category = library_categories.get_category(category_id)
    if not category or not auth.can_access(me(request), category["owner_id"]):
        raise HTTPException(404, "카테고리를 찾을 수 없어요.")
    return category


def _custom_article(request: Request, category_id: int, article_id: int) -> dict:
    _custom_category(request, category_id)
    article = library_categories.get_article(article_id)
    if not article or article["category_id"] != category_id:
        raise HTTPException(404, "아티클을 찾을 수 없어요.")
    return article


async def _custom_json(request: Request) -> dict:
    try:
        body = json.loads(await request.body())
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    if not isinstance(body, dict):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    return body


@app.get("/api/library/categories")
def list_custom_categories(request: Request):
    return {"items": library_categories.list_categories(auth.owner_scope(me(request)))}


@app.post("/api/library/categories")
async def create_custom_category(request: Request):
    user = me(request)
    body = await _custom_json(request)
    try:
        return library_categories.create_category(user["id"], body.get("name"))
    except pose_library.PoseError as exc:
        raise HTTPException(400, str(exc))


@app.patch("/api/library/categories/{category_id}")
async def rename_custom_category(request: Request, category_id: int):
    _custom_category(request, category_id)
    body = await _custom_json(request)
    try:
        return library_categories.rename_category(category_id, body.get("name"))
    except pose_library.PoseError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/library/categories/{category_id}")
def delete_custom_category(request: Request, category_id: int):
    return library_categories.delete_category(_custom_category(request, category_id))


@app.get("/api/library/categories/{category_id}/articles")
def list_custom_articles(request: Request, category_id: int):
    _custom_category(request, category_id)
    return {"items": library_categories.list_articles(category_id)}


@app.post("/api/library/categories/{category_id}/articles")
async def create_custom_article(request: Request, category_id: int):
    category = _custom_category(request, category_id)
    form = await request.form()
    try:
        images = await _library_form_images(form)
        return await asyncio.to_thread(
            library_categories.create_article, category_id, category["owner_id"],
            form.get("name"), form.get("description"), form.get("danbooru_prompt"), images)
    except pose_library.PoseError as exc:
        raise HTTPException(400, str(exc))


@app.patch("/api/library/categories/{category_id}/articles/{article_id}")
async def update_custom_article(request: Request, category_id: int, article_id: int):
    _custom_article(request, category_id, article_id)
    body = await _custom_json(request)
    try:
        return library_categories.update_article(article_id, body)
    except pose_library.PoseError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/library/categories/{category_id}/articles/{article_id}")
def delete_custom_article(request: Request, category_id: int, article_id: int):
    library_categories.delete_article(_custom_article(request, category_id, article_id))
    return {"ok": True}


@app.post("/api/library/categories/{category_id}/articles/{article_id}/images")
async def add_custom_article_images(request: Request, category_id: int, article_id: int):
    _custom_article(request, category_id, article_id)
    form = await request.form()
    try:
        images = await _library_form_images(form)
        return await asyncio.to_thread(library_categories.add_images, article_id, images)
    except pose_library.PoseError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/library/categories/{category_id}/articles/{article_id}/images/{image_id}")
def delete_custom_article_image(request: Request, category_id: int, article_id: int, image_id: int):
    article = _custom_article(request, category_id, article_id)
    try:
        return library_categories.delete_image(article, image_id)
    except LookupError:
        raise HTTPException(404, "이미지를 찾을 수 없어요.")
    except pose_library.PoseError as exc:
        raise HTTPException(400, str(exc))


def _custom_image_file(request: Request, category_id: int, article_id: int, image_id: int, thumb: bool):
    article = _custom_article(request, category_id, article_id)
    image = next((entry for entry in article["images"] if entry["id"] == image_id), None)
    path = library_categories.image_path(image, thumb) if image else None
    if not path or not path.is_file():
        raise HTTPException(404, "이미지 파일이 없어요.")
    return FileResponse(path)


@app.get("/api/library/categories/{category_id}/articles/{article_id}/images/{image_id}/image")
def get_custom_article_image(request: Request, category_id: int, article_id: int, image_id: int):
    return _custom_image_file(request, category_id, article_id, image_id, False)


@app.get("/api/library/categories/{category_id}/articles/{article_id}/images/{image_id}/thumb")
def get_custom_article_thumb(request: Request, category_id: int, article_id: int, image_id: int):
    return _custom_image_file(request, category_id, article_id, image_id, True)
