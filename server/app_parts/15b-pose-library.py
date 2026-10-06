# Library > Pose(NS-39) — 포즈 레퍼런스 게시물 목록·추가·이미지. 회원마다 자기 것만, admin은 전부 본다.
# app.py가 app_parts/*.py를 번호 순서대로 한 네임스페이스에서 실행한다 — 앞 파일의 이름을 import 없이 쓴다.


@app.get("/api/library/poses")
def list_library_poses(request: Request):
    return {"items": pose_library.list_poses(auth.owner_scope(me(request)))}


@app.post("/api/library/poses")
async def add_library_pose(request: Request):
    """multipart: name, description, danbooru_prompt, image(파일) 또는 image_url."""
    user = me(request)
    form = await request.form()
    file = form.get("image")
    image_url = str(form.get("image_url") or "").strip()
    try:
        if isinstance(file, UploadFile) and file.filename:
            content, image_url = await file.read(pose_library.MAX_BYTES + 1), ""
        elif image_url:
            content = await asyncio.to_thread(pose_library.fetch_image, image_url)
        else:
            raise pose_library.PoseError("이미지를 올리거나 이미지 주소를 적어주세요.")
        return await asyncio.to_thread(
            pose_library.add_pose, user["id"], str(form.get("name") or ""), str(form.get("description") or ""),
            str(form.get("danbooru_prompt") or ""), content, image_url)
    except pose_library.PoseError as e:
        raise HTTPException(400, str(e))


def _library_pose_file(request: Request, pose_id: int, thumb: bool):
    pose = pose_library.get_pose(pose_id)
    if not pose or not auth.can_access(me(request), pose["owner_id"]):
        raise HTTPException(404, "게시물을 찾을 수 없어요.")
    path = pose_library.image_path(pose, thumb)
    if not path.is_file():
        raise HTTPException(404, "이미지 파일이 없어요.")
    return FileResponse(path)


@app.get("/api/library/poses/{pose_id}/image")
def get_library_pose_image(request: Request, pose_id: int):
    return _library_pose_file(request, pose_id, False)


@app.get("/api/library/poses/{pose_id}/thumb")
def get_library_pose_thumb(request: Request, pose_id: int):
    return _library_pose_file(request, pose_id, True)
