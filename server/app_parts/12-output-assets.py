# app.py가 app_parts/*.py를 번호 순서대로 한 네임스페이스에서 실행한다 — 앞 파일의 이름을 import 없이 쓴다.
@app.put("/api/jobs/{job_id}/project")
async def set_job_project(job_id: str, request: Request):
    # 작업을 다른 프로젝트로(또는 미분류로) 옮긴다 — 그 작업이 만든 결과물도 같이 옮겨간다.
    user = me(request)
    job = job_or_404(user, job_id)
    body = await read_json_object(request, allow_empty=False)
    if "project_id" not in body:
        raise HTTPException(400, "project_id가 필요해요(미분류로 옮기려면 null).")
    project_id = parse_project_id(body["project_id"])
    # 작업은 그 작업의 주인의 프로젝트로만 옮길 수 있다.
    if project_id is not None and not project_store.project_exists(project_id, owner_id=job.get("owner_id")):
        raise HTTPException(400, "없는 프로젝트예요.")
    with lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "없는 작업이에요.")
        job["project_id"] = project_id
        snapshot = dict(job)
    save_state()
    project_store.set_job_assets_project(job_id, project_id)
    return snapshot


def _split_tags(value: str | None) -> list[str]:
    return [t for t in (value or "").split(",") if t.strip()]


@app.get("/api/output-assets")
def list_assets_api(request: Request, project_id: str | None = None, kind: str | None = None,
                    job_id: str | None = None, favorite: bool | None = None,
                    q: str | None = None, tag: str | None = None, min_rating: int | None = None,
                    limit: int = 200, offset: int = 0):
    # 결과물 색인 조회 — project_id는 숫자 또는 "unassigned"(미분류). 프롬프트 등 PNG 메타데이터와
    # 태그·평점·메모까지 담아 온다. q는 공백으로 나눈 단어가 모두 (프롬프트·메모·태그·파일명·
    # 체크포인트·시드) 중 어딘가에 들어 있는 것만 남긴다.
    if kind not in (None, "image", "video"):
        raise HTTPException(400, "kind는 image 또는 video여야 해요.")
    project: str | int | None = None
    if project_id is not None:
        project = "unassigned" if project_id == "unassigned" else parse_project_id(project_id)
    scope = auth.owner_scope(me(request))
    _sync_assets_quietly()
    return {"assets": asset_meta.list_assets(q, _split_tags(tag), favorite, min_rating, kind, project, job_id,
                                             max(1, min(limit, 1000)), max(0, offset), owner_id=scope)}


def _asset_paths_from(body: dict) -> list[str]:
    paths = body.get("paths")
    if paths is None and isinstance(body.get("path"), str):
        paths = [body["path"]]
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p for p in paths):
        raise HTTPException(400, "paths(결과물 경로 목록)가 필요해요.")
    return paths


@app.get("/api/output-assets/detail")
def asset_detail_api(path: str, request: Request):
    # 라이트박스의 정보 패널용 — 프롬프트/시드/체크포인트/파라미터와 메모·평점·태그 전부.
    scope = auth.owner_scope(me(request))
    _sync_assets_quietly()
    try:
        return asset_meta.get_detail(path, owner_id=scope)
    except asset_meta.AssetNotFound:
        raise HTTPException(404, "결과물을 찾을 수 없어요.")


@app.post("/api/output-assets/update")
async def update_assets_api(request: Request):
    scope = auth.owner_scope(me(request))
    # 즐겨찾기/평점/메모를 바꾼다(paths 여러 개면 전부 같은 값으로). 메모는 한 장씩만.
    body = await read_json_object(request, allow_empty=False)
    paths = _asset_paths_from(body)
    if "note" in body and len(paths) > 1:
        raise HTTPException(400, "메모는 결과물 한 장씩만 바꿀 수 있어요.")
    if "note" in body and not isinstance(body["note"], str):
        raise HTTPException(400, "note는 문자열이어야 해요.")
    if "favorite" in body and not isinstance(body["favorite"], bool):
        raise HTTPException(400, "favorite은 true/false여야 해요.")
    if "nsfw" in body and not isinstance(body["nsfw"], bool):
        raise HTTPException(400, "nsfw는 true/false여야 해요.")
    kwargs = {}
    if "favorite" in body:
        kwargs["favorite"] = body["favorite"]
    if "nsfw" in body:
        kwargs["nsfw"] = body["nsfw"]
    if "rating" in body:
        r = body["rating"]
        if r is not None and (not isinstance(r, int) or isinstance(r, bool)):
            raise HTTPException(400, "rating은 0~5 숫자여야 해요.")
        kwargs["rating"] = r
    if "note" in body:
        kwargs["note"] = body["note"]
    try:
        changed = await asyncio.to_thread(asset_meta.update_assets, paths, owner_id=scope, **kwargs)
    except asset_meta.AssetNotFound:
        raise HTTPException(404, "결과물을 찾을 수 없어요.")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"updated": changed}


@app.post("/api/output-assets/move")
async def move_assets_api(request: Request):
    scope = auth.owner_scope(me(request))
    # 결과물(이미지/영상)을 다른 프로젝트로 옮긴다 — project_id가 null이면 미분류. 파일은 그대로다.
    body = await read_json_object(request, allow_empty=False)
    paths = _asset_paths_from(body)
    if "project_id" not in body:
        raise HTTPException(400, "project_id가 필요해요(미분류로 옮기려면 null).")
    project_id = parse_project_id(body["project_id"])
    if project_id is not None and not project_store.project_exists(project_id, owner_id=scope):
        raise HTTPException(400, "없는 프로젝트예요.")
    try:
        moved = await asyncio.to_thread(asset_meta.move_assets, paths, project_id, scope)
    except asset_meta.AssetNotFound:
        raise HTTPException(404, "결과물을 찾을 수 없어요.")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"moved": moved, "project_id": project_id}


@app.post("/api/output-assets/tags")
async def change_asset_tags_api(request: Request):
    scope = auth.owner_scope(me(request))
    # 태그를 붙이고(add)/떼고(remove) 바뀐 결과물의 태그 목록을 돌려준다.
    body = await read_json_object(request, allow_empty=False)
    paths = _asset_paths_from(body)
    for key in ("add", "remove"):
        if key in body and not (isinstance(body[key], list) and all(isinstance(t, str) for t in body[key])):
            raise HTTPException(400, f"{key}는 문자열 목록이어야 해요.")
    try:
        tags = await asyncio.to_thread(asset_meta.change_tags, paths, body.get("add"), body.get("remove"), scope)
    except asset_meta.AssetNotFound:
        raise HTTPException(404, "결과물을 찾을 수 없어요.")
    return {"tags": tags}


@app.get("/api/tags")
def list_tags_api(request: Request):
    # 태그 자동완성/필터 후보 — 결과물에 붙은 개수 순.
    return {"tags": asset_meta.list_tags(owner_id=auth.owner_scope(me(request)))}


