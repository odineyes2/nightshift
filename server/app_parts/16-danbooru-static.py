# app.py가 app_parts/*.py를 번호 순서대로 한 네임스페이스에서 실행한다 — 앞 파일의 이름을 import 없이 쓴다.


@app.get("/api/danbooru/tag-edits")
def get_danbooru_tag_edits():
    return danbooru_tag_edits


@app.put("/api/danbooru/tag-edits")
async def put_danbooru_tag_edits(request: Request):
    admin_only(request)
    # 프론트엔드가 편집 상태 전체({categoryKey: {added, removed}})를 매번 통째로
    # 보내서 그대로 덮어쓴다 — 카테고리가 많지 않고 편집도 잦지 않아 부분 patch를
    # 둘 이유가 없다.
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    if not isinstance(data, dict):
        raise HTTPException(400, "카테고리별 편집 내역(객체)이어야 해요.")
    for key, value in data.items():
        if not isinstance(value, dict) or not all(
            isinstance(value.get(field, []), list) for field in ("added", "removed")
        ):
            raise HTTPException(400, f"'{key}' 항목 형식이 올바르지 않아요.")

    danbooru_tag_edits.clear()
    danbooru_tag_edits.update(data)
    save_danbooru_tag_edits()
    return danbooru_tag_edits


@app.get("/api/danbooru/history")
def get_danbooru_history():
    return {"history": danbooru_history}


@app.post("/api/danbooru/history")
async def add_danbooru_history(request: Request):
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")

    selection = data.get("selection")
    texts = data.get("texts")
    if not isinstance(selection, dict) or not isinstance(texts, dict):
        raise HTTPException(400, "selection/texts는 객체여야 해요.")
    name = data.get("name") or ""
    if not isinstance(name, str):
        raise HTTPException(400, "name은 문자열이어야 해요.")

    entry = {
        "id": str(uuid.uuid4())[:8],
        "name": name,
        "timestamp": int(time.time() * 1000),
        "selection": selection,
        "texts": texts,
    }
    with lock:
        danbooru_history.insert(0, entry)
        del danbooru_history[DANBOORU_HISTORY_LIMIT:]
    save_danbooru_history()
    return entry


@app.delete("/api/danbooru/history/{entry_id}")
def delete_danbooru_history(entry_id: str):
    with lock:
        entry = next((h for h in danbooru_history if h["id"] == entry_id), None)
        if entry is None:
            raise HTTPException(404, "없는 기록이에요.")
        danbooru_history.remove(entry)
    save_danbooru_history()
    return {"ok": True}


# 화면 JS/CSS는 static/js·static/css에 기능별로 나눠 두고(수정할 때 필요한 파일만 읽게),
# 브라우저에는 번호 순서대로 이어 붙인 한 덩어리로 보낸다. 한 스크립트로 실행되므로
# 파일 사이의 함수 끌어올림·실행 순서가 분할 전과 똑같다. 빌드 과정은 없다.
def _bundle(request: Request, folder: str, ext: str, media_type: str):
    files = sorted((REPO_ROOT / "static" / folder).glob(f"*.{ext}"))
    body = "".join(f.read_text("utf-8") for f in files).encode("utf-8")
    etag = '"' + hashlib.sha1(body).hexdigest()[:16] + '"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(body, media_type=media_type, headers=headers)


@app.get("/js/bundle.js", include_in_schema=False)
def js_bundle(request: Request):
    return _bundle(request, "js", "js", "application/javascript; charset=utf-8")


@app.get("/css/bundle.css", include_in_schema=False)
def css_bundle(request: Request):
    return _bundle(request, "css", "css", "text/css; charset=utf-8")


app.mount("/", StaticFiles(directory=str(REPO_ROOT / "static"), html=True), name="static")


