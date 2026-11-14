# 서비스 연동 영상 생성 POST /api/generate/video — 이미지 한 장을 첫·끝 프레임으로 같은 그림을 넣어 이음매 없는 짧은 루프 영상을 만든다.
# 모델: wan22(WAN2.2 flf2v 고정 그래프, 성인용 LoRA를 뺀 사본) · minimax_h3(MiniMax-H3 fl2v 빌더). 작업은 영상 등급 워커에서만 돈다(runpod_tier="video").
# 설정 > 자동 켜기의 "영상 파드 사용"(auto_power_settings video_jobs, 기본 꺼짐)이 꺼져 있으면 받지 않는다.
VIDEO_MODELS = {"wan22": "wan22_flf2v_batch", "minimax_h3": "minimax_h3_i2v_batch"}
VIDEO_MAX_IMAGE_BYTES = 8 * 1024 * 1024
VIDEO_IMAGE_FORMATS = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}
WAN22_FPS = 16
# 루프 영상에 남기는 LoRA — 4-step 가속(lightx2v)만. 고정 그래프에 함께 들어 있는 나머지(성인용 등)는 빼고 잇는다.
WAN22_KEEP_LORA = "lightx2v"
# 고정 그래프의 확산 모델(civitai 병합본, 등록부에 받을 주소 없음) 대신 공식 WAN2.2 I2V(Comfy-Org, 등록부에 주소 있음)를 쓴다.
WAN22_UNETS = {"high": "wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors", "low": "wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors"}


def _strip_wan_loras(workflow: dict) -> dict:
    """LoraLoaderModelOnly 중 가속 LoRA가 아닌 것을 지우고, 그 노드를 가리키던 입력을 그 노드의 입력 모델로 바로 잇는다."""
    workflow = json.loads(json.dumps(workflow))
    while True:
        drop = next((nid for nid, node in workflow.items()
                     if isinstance(node, dict) and node.get("class_type") == "LoraLoaderModelOnly"
                     and WAN22_KEEP_LORA not in str(node.get("inputs", {}).get("lora_name", ""))), None)
        if drop is None:
            return workflow
        source = workflow.pop(drop)["inputs"].get("model")
        for node in workflow.values():
            for key, value in (node.get("inputs") or {}).items():
                if isinstance(value, list) and len(value) == 2 and str(value[0]) == str(drop):
                    node["inputs"][key] = source


def _loop_video_size(width: int, height: int, max_edge: int) -> tuple[int, int]:
    """긴 변을 max_edge에 맞춘(확대는 안 함) 16의 배수 크기."""
    scale = min(1.0, max_edge / max(width, height))
    return (max(16, round(width * scale / 16) * 16), max(16, round(height * scale / 16) * 16))


def _video_number(body: dict, key: str, default: float, low: float, high: float, label: str) -> float:
    value = body.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise HTTPException(400, f"{label}는 {low:g}~{high:g} 사이여야 해요.")
    return float(value)


def _video_image(body: dict) -> tuple[bytes, str, int, int]:
    raw = body.get("image")
    if not isinstance(raw, str) or not raw or len(raw) > VIDEO_MAX_IMAGE_BYTES * 4 // 3 + 8:
        raise HTTPException(400, "이미지(base64, 8MB 이하)를 보내 주세요.")
    try:
        data = base64.b64decode(raw, validate=True)
        with Image.open(io.BytesIO(data)) as img:
            fmt, (width, height) = img.format, img.size
            img.verify()
    except Exception:
        raise HTTPException(400, "이미지를 읽을 수 없어요.") from None
    if fmt not in VIDEO_IMAGE_FORMATS or len(data) > VIDEO_MAX_IMAGE_BYTES or not (64 <= min(width, height) and max(width, height) <= 4096):
        raise HTTPException(400, "png·jpg·webp 이미지(한 변 64~4096px, 8MB 이하)만 받아요.")
    return data, VIDEO_IMAGE_FORMATS[fmt], width, height


@app.post("/api/generate/video")
async def generate_video(request: Request):
    user = me(request)
    if not auto_power_settings.load("video")["video_jobs"]:
        raise HTTPException(409, "nightshift에서 영상 파드 사용이 꺼져 있어요(설정 > 자동 켜기).")
    body = await read_json_object(request, allow_empty=False)
    model = body.get("model")
    if model not in VIDEO_MODELS:
        raise HTTPException(400, f"영상 모델은 {', '.join(VIDEO_MODELS)} 중 하나여야 해요.")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 2000:
        raise HTTPException(400, "프롬프트는 1~2000자로 입력해 주세요.")
    prompt = prompt.strip()
    seconds = _video_number(body, "seconds", 3, 2, 5, "영상 길이(초)")
    max_edge = int(_video_number(body, "max_edge", 720, 384, 1024, "긴 변(px)"))
    data, ext, width, height = _video_image(body)
    try:
        image_name = await asyncio.to_thread(save_input_image, f"loop_{uuid.uuid4().hex[:12]}.{ext}", data)
    except InputAssetError as exc:
        raise HTTPException(400, str(exc)) from exc
    video_w, video_h = _loop_video_size(width, height, max_edge)
    if model == "wan22":
        workflow = json.loads((VIDEO_WORKFLOWS_DIR / "wan22_flf2v.json").read_text(encoding="utf-8"))
        workflow = _strip_wan_loras(workflow)
        for node in workflow.values():
            if node.get("class_type") == "UNETLoader":
                name = str(node["inputs"].get("unet_name", "")).lower()
                node["inputs"]["unet_name"] = WAN22_UNETS["low" if "low" in name else "high"]
        for node in workflow.values():
            if node.get("class_type") == "WanFirstLastFrameToVideo":
                node["inputs"]["length"] = 4 * round(seconds * WAN22_FPS / 4) + 1   # WAN 길이는 4n+1 프레임
        options = {"video_count": "1", "start_image": image_name, "end_image": image_name, "user_prompt": prompt,
                   "text_enhance": "off", "video_width": str(video_w), "video_height": str(video_h),
                   "lora_name": "", "lora_strength": ""}
        filename = "loop_wan22_flf2v.json"
    else:
        try:
            workflow = workflow_builder_minimax_h3.build_i2v_workflow(
                {"positive": prompt, "use_first_frame": True, "use_last_frame": True, "duration": seconds,
                 "seed": secrets.randbelow(2**31), "loras": []})
        except WorkflowBuildError as exc:
            raise HTTPException(400, str(exc)) from exc
        for node in workflow.values():
            if node.get("class_type") == "ImageScaleToTotalPixels":
                node["inputs"]["megapixels"] = round(video_w * video_h / 1_000_000, 3)
        options = {"video_count": "1", "first_frame_image": image_name, "last_frame_image": image_name,
                   "user_prompt": prompt, "video_duration": f"{seconds:g}"}
        filename = "loop_minimax_h3.json"
    job = await create_job(resolve_template(VIDEO_MODELS[model]), json.dumps(workflow, ensure_ascii=False).encode(),
                           filename, None, None, options, user=user, parse_inline_options=False, runpod_tier="video")
    return {"job_id": job["id"], "status": job["status"]}
