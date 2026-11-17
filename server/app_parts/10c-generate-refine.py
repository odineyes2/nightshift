# 서비스 연동 보정 POST /api/generate/refine — 이미 만든 그림 한 장의 얼굴 보정(face)·2배 업스케일(upscale).
# 그래프 조립은 generate_models.build_refine(krea.2 얼굴 보정은 Anima로 대신). 작업은 input_image_batch 템플릿으로
# 이미지 등급 워커에서 돈다. 입력 그림 검사는 영상 API와 같다(_video_image: png·jpg·webp, 한 변 64~4096px, 8MB 이하).


@app.post("/api/generate/refine")
async def generate_refine(request: Request):
    user = me(request)
    body = await read_json_object(request, allow_empty=False)
    op = body.get("op")
    if op not in generate_models.REFINE_OPS:
        raise HTTPException(400, f"보정 종류는 {', '.join(generate_models.REFINE_OPS)} 중 하나여야 해요.")
    entry = generate_models.find(body.get("model"))
    if entry is None:
        raise HTTPException(404, "생성 모델을 찾을 수 없어요.")
    prompt = body.get("prompt", "")
    negative = body.get("negative_prompt", "")
    if not isinstance(prompt, str) or len(prompt) > 4000:
        raise HTTPException(400, "프롬프트는 4000자 이하여야 해요.")
    if not isinstance(negative, str) or len(negative) > 4000:
        raise HTTPException(400, "네거티브 프롬프트는 4000자 이하여야 해요.")
    try:
        seed = generate_presets._integer(body.get("seed", secrets.randbelow(2**32)), "시드", 0, 2**32 - 1)
    except generate_presets.PresetError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    data, ext, width, height = _video_image(body)
    if op == "upscale" and max(width, height) * generate_models.REFINE_UPSCALE_BY > 4096:
        raise HTTPException(400, "이미 충분히 커서 더 키울 수 없어요(긴 변 4096px까지).")
    try:
        if op == "faces":
            # 얼굴마다 그 인물의 프롬프트·LoRA로 다시 그린다 — faces는 그림의 왼쪽 얼굴부터 [{prompt, loras}].
            raw = body.get("faces")
            if not isinstance(raw, list) or not 1 <= len(raw) <= generate_models.MAX_FACES:
                raise generate_models.ModelError(f"faces는 1~{generate_models.MAX_FACES}개의 목록이어야 해요.")
            faces = []
            for item in raw:
                text = item.get("prompt") if isinstance(item, dict) else None
                if not isinstance(text, str) or not text.strip() or len(text) > 4000:
                    raise generate_models.ModelError("얼굴마다 프롬프트(1~4000자)가 필요해요.")
                faces.append((text.strip(), generate_models.clean_loras(entry, item.get("loras"))))
            workflow, used = generate_models.build_faces(entry, faces, negative, seed, width), entry
        else:
            used = generate_models.refine_entry(entry, op)
            face_defaults = None
            if op == "face":
                arch = generate_models.architecture(used)
                face_defaults = face_detailer_defaults_for(generate_models.family_id(used), "unet" if arch == "unet" else "sdxl")
            workflow, used = generate_models.build_refine(entry, op, prompt.strip(), negative, seed, face_defaults)
    except generate_models.ModelError as exc:
        raise HTTPException(409 if "등록부에 없어요" in str(exc) and op != "faces" else 400, str(exc)) from exc
    try:
        image_name = await asyncio.to_thread(save_input_image, f"refine_{uuid.uuid4().hex[:12]}.{ext}", data)
    except InputAssetError as exc:
        raise HTTPException(400, str(exc)) from exc
    options = {"image_count": "1", "input_image": image_name, "main_prompt": ""}
    job = await create_job(resolve_template("input_image_batch"), json.dumps(workflow, ensure_ascii=False).encode(),
                           f"refine_{op}.json", None, None, options, user=user, parse_inline_options=False)
    return {"job_id": job["id"], "status": job["status"], "model": generate_models.model_id(used),
            "width": int(width * (generate_models.REFINE_UPSCALE_BY if op == "upscale" else 1)),
            "height": int(height * (generate_models.REFINE_UPSCALE_BY if op == "upscale" else 1))}
