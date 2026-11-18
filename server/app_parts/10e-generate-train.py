# 서비스 연동 인물 LoRA 학습 POST /api/generate/train — 캐릭터 스튜디오의 배우 LoRA.
# 학습 그림(base64)을 입력 이미지 폴더에 저장하고 lora_train 템플릿 작업을 만든다. 워크플로우는 바탕 체크포인트를
# 불러오는 노드 하나뿐이다 — 스케줄러가 그 모델이 있는(없으면 자동 설치하는) 이미지 워커에 배정하게 하려고.
# 끝나면 결과 LoRA는 홈서버 보관 폴더(hosted_models/loras)에 있고, 러너가 등록부에 적는다(register_trained_lora).

TRAIN_MIN_IMAGES, TRAIN_MAX_IMAGES = 8, 80
TRAIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
TRAIN_TRIGGER_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")


@app.post("/api/generate/train")
async def generate_train(request: Request):
    user = me(request)
    body = await read_json_object(request, allow_empty=False)
    name, trigger = body.get("name"), body.get("trigger")
    if not isinstance(name, str) or not TRAIN_NAME_RE.match(name):
        raise HTTPException(400, "LoRA 이름은 영문·숫자·밑줄·하이픈 64자까지예요.")
    if not isinstance(trigger, str) or not TRAIN_TRIGGER_RE.match(trigger):
        raise HTTPException(400, "트리거는 영문·숫자·밑줄 40자까지예요.")
    filename = name + ".safetensors"
    if model_registry.get_entry("loras", filename) is not None or hosted_models.exists("loras", filename):
        raise HTTPException(409, "같은 이름의 LoRA가 이미 있어요.")
    entry = generate_models.find(body.get("model"))
    if entry is None or generate_models.architecture(entry) != "sdxl":
        raise HTTPException(400, "학습은 체크포인트형(SDXL 계열) 생성 모델에서만 할 수 있어요.")
    images = body.get("images")
    if not isinstance(images, list) or not TRAIN_MIN_IMAGES <= len(images) <= TRAIN_MAX_IMAGES:
        raise HTTPException(400, f"학습 그림은 {TRAIN_MIN_IMAGES}~{TRAIN_MAX_IMAGES}장이어야 해요.")
    params = {}
    for key, low, high in (("repeats", 1, 40), ("epochs", 1, 40), ("dim", 4, 128), ("alpha", 1, 128)):
        if key in body:
            try:
                params[key] = generate_presets._integer(body[key], key, low, high)
            except generate_presets.PresetError as exc:
                raise HTTPException(exc.status, str(exc)) from exc
    if "one_face" in body:
        params["one_face"] = body["one_face"] is not False
    # 공개(관리자만 정할 수 있음, 연동 앱의 운영진 전속 배우) / 비공개(기본, 학습을 맡긴 회원만 씀).
    visibility = body.get("visibility", "private")
    if visibility not in lora_access.VISIBILITIES:
        raise HTTPException(400, "visibility는 public 또는 private이어야 해요.")
    if visibility == "public" and not auth.is_admin(auth.get_user(user["id"])):
        raise HTTPException(403, "공개 LoRA는 관리자만 만들 수 있어요.")
    params["visibility"] = visibility
    saved = []
    for index, item in enumerate(images, start=1):
        caption = item.get("caption", "") if isinstance(item, dict) else ""
        if not isinstance(caption, str) or len(caption) > 400 or "\n" in caption:
            raise HTTPException(400, "캡션은 한 줄 400자까지예요.")
        data, ext, width, height = _video_image({"image": item.get("image") if isinstance(item, dict) else None})
        try:
            stored = await asyncio.to_thread(save_input_image, f"train_{name}_{index:03d}_{uuid.uuid4().hex[:6]}.{ext}", data)
        except InputAssetError as exc:
            raise HTTPException(400, str(exc)) from exc
        saved.append({"file": stored, "caption": caption.strip()})
    manifest = {"name": name, "trigger": trigger, "checkpoint": entry["filename"], "base_models": entry.get("base_models") or [],
                "images": saved, **params}
    manifest_path = JOBS_DIR / f"train_{name}_{uuid.uuid4().hex[:8]}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    # 얼굴 탐지 모델도 넣어 둔다 — 새 파드엔 이 파일이 없어 얼굴 수 거르기가 조용히 건너뛰어졌다(2026-11-18 실제 파드).
    workflow = {"1": {"inputs": {"ckpt_name": entry["filename"]}, "class_type": "CheckpointLoaderSimple", "_meta": {"title": "학습 바탕 모델"}},
                "2": {"inputs": {"model_name": generate_models.workflow_builder.FACE_DETECTOR_MODEL}, "class_type": "UltralyticsDetectorProvider",
                      "_meta": {"title": "학습 그림 얼굴 거르기"}}}
    job = await create_job(resolve_template("lora_train"), json.dumps(workflow).encode(), f"train_{name}.json", None, None,
                           {"train_manifest": str(manifest_path.resolve())}, user=user, parse_inline_options=False)
    return {"job_id": job["id"], "status": job["status"], "lora": filename}


def register_trained_lora(job: dict) -> None:
    """lora_train 작업이 성공하면(러너가 부른다) 보관 폴더에 생긴 LoRA를 등록부에 적는다."""
    try:
        manifest = json.loads(Path(job["options"]["train_manifest"]).read_text(encoding="utf-8"))
        filename = manifest["name"] + ".safetensors"
        if not hosted_models.exists("loras", filename):
            return
        lora_access.set_access(filename, job.get("owner_id"), manifest.get("visibility", "private"))
        model_registry.upsert("loras", filename, {
            "base_models": manifest.get("base_models") or [], "trigger_keyword": manifest["trigger"],
            "size_bytes": hosted_models.path("loras", filename).stat().st_size,
            "notes": f"캐릭터 스튜디오 학습(작업 {job['id']}, 그림 {len(manifest['images'])}장)"})
    except Exception as exc:
        print(f"[lora_train] 학습한 LoRA를 등록부에 적지 못했어요: {exc}", flush=True)
