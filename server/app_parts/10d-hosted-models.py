# 홈서버에 보관하는 LoRA·업스케일러 — 관리자가 올리고(PUT), 워커가 파일별 열쇠로 받아 간다(GET, 로그인 없음).
# 올리면 모델 등록부에도 적는다. 받을 주소는 등록부에 적지 않는다 —
# 자동 설치가 보관 파일을 먼저 찾아 hosted_models.url로 만든다(13-jobs-api._start_model_downloads).


@app.put("/api/hosted-models/{kind}/{filename}")
async def put_hosted_model(kind: str, filename: str, request: Request):
    admin_only(request)
    try:
        hosted_models.path(kind, filename)
        data = await request.body()
        await asyncio.to_thread(hosted_models.save, kind, filename, data)
    except hosted_models.HostedModelError as exc:
        raise HTTPException(400, str(exc)) from exc
    fields = {"size_bytes": len(data)}
    base_model = request.query_params.get("base_model", "").strip()
    trigger = request.query_params.get("trigger", "").strip()
    if base_model:
        fields["base_model"] = base_model
    if trigger:
        fields["trigger_keyword"] = trigger
    try:
        entry = model_registry.upsert(kind, filename, fields)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "kind": kind, "filename": filename, "size_bytes": len(data), "entry": entry}


@app.get("/api/hosted-models/{kind}/{token}/{filename}")
def get_hosted_model(kind: str, token: str, filename: str):
    try:
        target = hosted_models.path(kind, filename)
        valid = hmac.compare_digest(token, hosted_models.token(kind, filename))
    except hosted_models.HostedModelError:
        valid, target = False, None
    if not valid or not target.is_file():
        raise HTTPException(404, "없는 주소예요.")
    return FileResponse(target, media_type="application/octet-stream", headers={"Cache-Control": "no-store"})
