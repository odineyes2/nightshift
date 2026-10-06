# app.py가 app_parts/*.py를 번호 순서대로 한 네임스페이스에서 실행한다 — 앞 파일의 이름을 import 없이 쓴다.
@app.get("/api/base-model-families")
async def get_base_model_families(request: Request, pod_id: str | None = None):
    """새 작업 마법사 1단계 — 모델 등록부에서 base_model이 적힌 체크포인트를 그 값으로 묶어 준다.
    {id: {label, checkpoints}}: id는 base_model 값의 식별자(워크플로우 프리셋 파일 이름과 LoRA 걸러내기에 쓴다).
    쓸 수 있는 것만 보이게, 지금 들어가 있는 파드(없으면 내 연결된 파드 전체)에 설치된 체크포인트로 좁힌다. 파드에
    연결하지 못하면 좁힐 기준이 없으니 등록된 것을 전부 보여 준다."""
    user = me(request)
    if pod_id == "auto":   # 파드를 정하지 않고 구상하는 중 — 등록해 둔 체크포인트를 전부 보여 준다(배정은 스케줄러가 한다)
        return model_registry.checkpoint_groups(None)
    pods = [object_info_pod(pod_id)] if pod_id else [
        p for p in visible_pods_for(user) if p.get("kind") == pod_registry.DEFAULT_KIND and p.get("enabled")]
    installed: set[str] | None = None

    def installed_of(pod):
        try:
            _, info = fetch_comfy_object_info(False, pod)
        except Exception:
            return None
        if info is None:
            return None
        # 체크포인트(CheckpointLoaderSimple)와 디퓨전 모델(UNETLoader, krea.2/MiniMax-H3처럼
        # 체크포인트가 없는 family) 둘 다의 설치 목록을 합쳐야 두 kind의 family가 모두 걸러진다.
        return (set(combo_choices(info, *MODEL_LIST_SOURCES["checkpoints"]))
                | set(combo_choices(info, *MODEL_LIST_SOURCES["diffusion_models"])))

    results = await asyncio.gather(*[asyncio.to_thread(installed_of, p) for p in pods if not p.get("_none")])
    live = [r for r in results if r is not None]
    if live:
        installed = set().union(*live)
    return model_registry.checkpoint_groups(installed)


# "새 작업 추가" 마법사 2단계(워크플로우 유형)의 정적 카탈로그 — 세 그룹으로
# 나뉜다(workflow_builder.py의 조합 규칙과 정확히 대응):
#
#   base   — 첫 샘플링을 어디서 시작할지, 반드시 하나만 고른다(서로 배타적).
#            POST /api/build-workflow(workflow_builder.py)가 spec["base"]로 받는다.
#   post   — base 뒤에 이어 붙이는 후처리, 0개 이상 동시에 고를 수 있다(체이닝
#            가능 — 예: hires_fix+usdu를 같이 켜면 hires-fix 다음에 usdu가 실행됨).
#            spec["hires_fix"]/spec["usdu"]로 받는다.
#   preset — ControlNet/IPAdapter처럼 체크포인트마다 배선이 달라 이 서버가 자동
#            조립하지 못하는 유형. family별로 미리 올려둔 워크플로우(GET
#            /api/workflow-presets/{family}/{type})를 그대로 쓰므로, base/post와
#            동시에 쓸 수 없다(마법사가 이 배타 관계를 강제한다).
#
# template_ids는 이 유형(들)을 "시드 반복"/"CSV 순회" 중 어느 실행 방식으로 돌릴지에
# 따라 실제로 큐에 올릴 템플릿 id를 알려준다 — base 쪽만 갖고 있다(post는 base가
# 고른 템플릿을 그대로 쓴다: img2img가 필요로 하는 입력 이미지 주입은 base가
# img2img일 때만 필요하고, hires_fix/usdu는 워크플로우 안에서 완결되므로 별도
# 템플릿이 필요 없다 — 예전엔 usdu가 항상 외부 이미지를 요구하는 별도 유형이었지만,
# 이제는 base=txt2img+usdu처럼 입력 이미지 없이도 조합할 수 있다). requires_node가
# 있으면 그 클래스가 GET /api/comfy-object-info의 node_types에 없을 때 "설치 필요"
# 안내만 하고 선택 자체는 막지 않는다(화면에서 처리).
WORKFLOW_TYPES = {
    "base": [
        {
            "id": "txt2img", "label": "Text to Image",
            "template_ids": {"seed": "seed_batch", "csv": "csv_batch"},
        },
        {
            "id": "img2img", "label": "Image to Image", "requires_input_image": True,
            "template_ids": {"seed": "input_image_batch", "csv": "input_image_csv_batch"},
        },
        {
            # 기존 이미지 1장의 얼굴만 보정한다(workflow_builder.py의 base="face_detailer").
            # family_id가 없어 txt2img/img2img처럼 체크포인트형·UNet+CLIP+VAE형 family에서만 보인다.
            "id": "face_detailer", "label": "Face Detailer", "requires_input_image": True,
            "requires_node": "FaceDetailer",
            "template_ids": {"seed": "input_image_batch", "csv": "input_image_csv_batch"},
        },
        {
            # family_id가 있으면 그 family(base_id 기준)를 골랐을 때만 마법사 2단계에 보인다
            # (기존 txt2img/img2img는 family_id가 없어 전 family에 보임 — 그대로 유지).
            # architecture는 POST /api/build-workflow가 어느 빌더 모듈로 갈지 고르는 키다.
            "id": "krea2_t2i", "label": "Text to Image (krea.2)",
            "family_id": "krea.2", "architecture": "krea2",
            "template_ids": {"seed": "seed_batch", "csv": "csv_batch"},
        },
        {
            # checkpoint_match: MiniMax-H3에는 UNet 파일이 실제로 2개 있고(fl2va/ref2va),
            # 어느 워크플로우 유형을 쓸 수 있는지가 그 파일에 달려 있다(model_registry에는
            # 둘 다 base_model="MiniMax-H3"로 한 family에 묶여 있으므로 family_id만으로는
            # 못 가른다) — 마법사 1단계에서 고른 체크포인트 파일명에 이 부분 문자열이
            # 있어야만 2단계에 보인다(대소문자 무시, 프론트 renderWizardTypeModal 참고).
            "id": "minimax_h3_t2v", "label": "Text to Video (MiniMax-H3)",
            "family_id": "minimax-h3", "architecture": "minimax_h3_i2v", "checkpoint_match": "fl2va",
            "template_ids": {"seed": "minimax_h3_i2v_batch", "csv": "minimax_h3_i2v_csv_batch"},
        },
        {
            "id": "minimax_h3_i2v", "label": "First/Last Frame to Video (MiniMax-H3)",
            "family_id": "minimax-h3", "architecture": "minimax_h3_i2v", "checkpoint_match": "fl2va",
            "requires_input_image": True,
            "template_ids": {"seed": "minimax_h3_i2v_batch", "csv": "minimax_h3_i2v_csv_batch"},
        },
        {
            "id": "minimax_h3_r2v", "label": "Reference to Video (MiniMax-H3)",
            "family_id": "minimax-h3", "architecture": "minimax_h3_r2v", "checkpoint_match": "ref2va",
            "template_ids": {"seed": "minimax_h3_r2v_batch", "csv": "minimax_h3_r2v_csv_batch"},
        },
    ],
    "post": [
        # applies_to_base가 있으면 그 base를 골랐을 때만 보인다 — hires_fix/usdu는 SDXL식
        # (EmptyLatentImage+KSampler) 그래프 전용이라 krea2_t2i/minimax_h3_*에는 안 맞는다.
        {"id": "hires_fix", "label": "Hires Fix", "applies_to_base": ["txt2img", "img2img"]},
        # face_detailer는 디코드 뒤·usdu 앞에 들어간다(spec["face_detailer"]).
        {"id": "face_detailer", "label": "Face Detailer", "requires_node": "FaceDetailer",
         "applies_to_base": ["txt2img", "img2img"]},
        {"id": "usdu", "label": "Ultimate SD Upscale", "requires_node": "UltimateSDUpscaleNoUpscale",
         "applies_to_base": ["txt2img", "img2img"]},
    ],
    # pre — 프롬프트를 생성 전에 다듬는 전처리, 0개 이상. requires_model이 그 파드의 model_kind
    # 목록에 없으면 /api/enhance-prompt가 409(model_missing)로 돌려준다(모델 탭에서 받는다).
    "pre": [
        {"id": "prompt_enhance", "label": "Prompt Enhance",
         "requires_model": "Huihui-qwen3vl_4b_fp8_scaled.safetensors", "model_kind": "text_encoders"},
    ],
    "preset": [
        {
            "id": "openpose_cn", "label": "OpenPose ControlNet", "ref_kind": "pose",
            "template_ids": {"seed": "pose_batch", "csv": "pose_csv_batch"},
        },
        {
            "id": "depth_cn", "label": "Depth ControlNet", "ref_kind": "depth",
            "template_ids": {"seed": "depth_batch", "csv": "depth_csv_batch"},
        },
        {
            "id": "lineart_cn", "label": "Lineart ControlNet", "ref_kind": "lineart",
            "template_ids": {"seed": "lineart_batch", "csv": "lineart_csv_batch"},
        },
        {
            "id": "ipadapter", "label": "IPAdapter",
            "template_ids": {"seed": "ipadapter_batch", "csv": "ipadapter_csv_batch"},
        },
    ],
}


@app.get("/api/workflow-types")
def get_workflow_types():
    return WORKFLOW_TYPES


def workflow_type_ids() -> set[str]:
    """베이스 모델별 허용 목록에 넣을 수 있는 유형 id — base·post·preset(전처리 pre는 계열과 무관해 늘 보인다)."""
    return {t["id"] for g in ("base", "post", "preset") for t in WORKFLOW_TYPES[g]}


def preset_filename(family_id: str, type_id: str) -> str:
    for value, label in ((family_id, "family_id"), (type_id, "type_id")):
        if not value or not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise HTTPException(400, f"{label} 값이 올바르지 않아요 (영문/숫자/-/_/.만 가능).")
    return f"{family_id}__{type_id}.json"


@app.get("/api/workflow-presets")
def list_workflow_presets():
    # 마법사가 "이 family + 이 워크플로우 유형" 조합에 프리셋이 이미 있는지 한 번에
    # 확인할 수 있게, 저장된 모든 조합을 나열해서 돌려준다.
    presets = []
    for path in sorted(WORKFLOW_PRESETS_DIR.glob("*__*.json")):
        family_id, _, rest = path.stem.partition("__")
        if family_id and rest:
            presets.append({"family_id": family_id, "type_id": rest})
    return {"presets": presets}


@app.get("/api/workflow-presets/{family_id}/{type_id}")
def get_workflow_preset(family_id: str, type_id: str):
    path = WORKFLOW_PRESETS_DIR / preset_filename(family_id, type_id)
    if not path.exists():
        raise HTTPException(404, "해당 조합의 프리셋 워크플로우가 없어요.")
    return Response(content=path.read_text(encoding="utf-8"), media_type="application/json")


@app.get("/api/video-workflows/{name}")
def get_video_workflow(name: str):
    # 영상 생성 템플릿(wan22_i2v_batch 등)을 고르면 프론트엔드가 이걸 받아
    # 워크플로우 업로드 칸에 자동으로 채운다 — 사용자가 직접 파일을 고를 필요가
    # 없다. name은 고정된 화이트리스트라 경로 조작 걱정이 없다.
    if name not in VIDEO_WORKFLOW_NAMES:
        raise HTTPException(404, "해당 영상 워크플로우가 없어요.")
    path = VIDEO_WORKFLOWS_DIR / f"{name}.json"
    if not path.exists():
        raise HTTPException(404, "워크플로우 파일이 서버에 없어요.")
    return Response(content=path.read_text(encoding="utf-8"), media_type="application/json")


@app.put("/api/workflow-presets/{family_id}/{type_id}")
async def put_workflow_preset(family_id: str, type_id: str, request: Request):
    admin_only(request)
    # "🎛 LoRA" 탭(워크플로우 프리셋 관리 부분)이 워크플로우 JSON을 통째로 올려서
    # family+유형 조합 하나에 저장한다 — 업로드한 파일을 그대로 검증 없이 저장한다
    # (실제로 돌아가는지는 POST /api/validate-workflow를 별도로 안내하면 됨).
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    workflow = data.get("workflow") if isinstance(data, dict) and "workflow" in data else data
    if not isinstance(workflow, dict):
        raise HTTPException(400, "워크플로우 JSON(노드 id → 노드) 형식이 아니에요.")

    path = WORKFLOW_PRESETS_DIR / preset_filename(family_id, type_id)
    path.write_text(json.dumps(workflow, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "family_id": family_id, "type_id": type_id}


@app.delete("/api/workflow-presets/{family_id}/{type_id}")
def delete_workflow_preset(family_id: str, type_id: str, request: Request):
    admin_only(request)
    path = WORKFLOW_PRESETS_DIR / preset_filename(family_id, type_id)
    if not path.exists():
        raise HTTPException(404, "해당 조합의 프리셋 워크플로우가 없어요.")
    path.unlink()
    return {"ok": True}


# Face Detailer 수치 — 세부설정의 "수정" 탭(작업별)과 "셋팅" 탭(베이스 모델별 기본값)이 쓴다.
# 이름 → (형, 최솟값, 최댓값). sampler_name·scheduler는 문자열이다.
FACE_DETAILER_FIELDS = {
    "steps": (int, 1, None), "cfg": (float, 0.0, None), "denoise": (float, 0.0, 1.0),
    "guide_size": (int, 64, None), "max_size": (int, 64, None), "feather": (int, 0, None),
    "bbox_threshold": (float, 0.0, 1.0), "bbox_dilation": (int, None, None), "bbox_crop_factor": (float, 1.0, None),
    "drop_size": (int, 1, None), "cycle": (int, 1, None), "noise_mask_feather": (int, 0, None),
    "sampler_name": (str, None, None), "scheduler": (str, None, None),
}
FACE_DETAILER_DEFAULTS_FILE = data_path("face_detailer_defaults.json")


def clean_face_detailer_values(raw) -> dict:
    """사용자가 보낸 Face Detailer 수치를 검사해 알려진 칸만 형을 맞춰 돌려준다(빈 값은 뺀다)."""
    if not isinstance(raw, dict):
        raise HTTPException(400, "Face Detailer 값은 객체여야 해요.")
    out = {}
    for key, value in raw.items():
        if key not in FACE_DETAILER_FIELDS:
            raise HTTPException(400, f"알 수 없는 Face Detailer 항목이에요: {key}")
        kind, lo, hi = FACE_DETAILER_FIELDS[key]
        if value is None or value == "":
            continue
        if kind is str:
            value = str(value).strip()
            if not re.fullmatch(r"[A-Za-z0-9_.+-]{1,64}", value):
                raise HTTPException(400, f"{key} 값 '{value}'이(가) 올바르지 않아요.")
        else:
            try:
                value = kind(float(value)) if kind is int else float(value)
            except (TypeError, ValueError):
                raise HTTPException(400, f"{key} 값 '{value}'은(는) 숫자가 아니에요.")
            if (lo is not None and value < lo) or (hi is not None and value > hi):
                raise HTTPException(400, f"{key} 값은 {lo if lo is not None else ''}~{hi if hi is not None else ''} 범위여야 해요.")
        out[key] = value
    return out


def load_face_detailer_defaults() -> dict:
    try:
        data = json.loads(FACE_DETAILER_DEFAULTS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def face_detailer_defaults_for(family_id: str, architecture: str = "sdxl") -> dict:
    """빌더 기본값(로더 형태별) 위에 이 베이스 모델에 저장된 값을 덮은 전체 수치."""
    import workflow_builder
    builtin = workflow_builder_unet.FACE_DETAILER_DEFAULTS if architecture == "unet" else workflow_builder.FACE_DETAILER_DEFAULTS
    saved = load_face_detailer_defaults().get(family_id) or {}
    return {**workflow_builder.FACE_DETAILER_COMMON, **builtin, **saved}


@app.get("/api/face-detailer-defaults/{family_id}")
def get_face_detailer_defaults(family_id: str, architecture: str = "sdxl"):
    preset_filename(family_id, "x")  # family_id 형식 검사
    return {"family_id": family_id, "values": face_detailer_defaults_for(family_id, architecture),
            "saved": load_face_detailer_defaults().get(family_id) or {}}


@app.put("/api/face-detailer-defaults/{family_id}")
async def put_face_detailer_defaults(family_id: str, request: Request):
    # 베이스 모델마다 적정값이 달라 관리자가 모델별로 저장한다(프리셋 저장과 같은 권한).
    admin_only(request)
    preset_filename(family_id, "x")
    try:
        body = await request.json()
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    values = clean_face_detailer_values(body.get("values") if isinstance(body, dict) else None)
    data = load_face_detailer_defaults()
    if values:
        data[family_id] = values
    else:
        data.pop(family_id, None)  # 빈 값을 보내면 빌더 기본값으로 되돌린다
    FACE_DETAILER_DEFAULTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "family_id": family_id, "saved": values}


def apply_face_detailer_values(workflow: dict, values: dict) -> int:
    """워크플로우의 모든 FaceDetailer 노드 입력을 values로 고치고 고친 노드 수를 돌려준다."""
    count = 0
    for node in workflow.values():
        if isinstance(node, dict) and node.get("class_type") == "FaceDetailer":
            node.setdefault("inputs", {}).update(values)
            count += 1
    return count


@app.post("/api/face-detailer/apply")
async def apply_face_detailer_api(request: Request):
    # "수정" 탭 — 붙어 있는 워크플로우의 FaceDetailer 수치를 이 작업만 바꾼다(저장된 기본값은 그대로).
    try:
        body = await request.json()
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    workflow = body.get("workflow") if isinstance(body, dict) else None
    if not isinstance(workflow, dict):
        raise HTTPException(400, "워크플로우 JSON(노드 id → 노드) 형식이 아니에요.")
    values = clean_face_detailer_values(body.get("values"))
    if not apply_face_detailer_values(workflow, values):
        raise HTTPException(400, "워크플로우에 FaceDetailer 노드가 없어요.")
    return {"workflow": workflow}


@app.post("/api/build-workflow")
async def build_workflow_api(request: Request, pod_id: str | None = None):
    # "워크플로우 빌더" 탭 — 업로드 없이 스펙(체크포인트/LoRA/프롬프트/샘플러/해상도)
    # 만으로 워크플로우 JSON을 만들어 돌려준다. 만들어진 JSON은 화면에서 작업 관리
    # 탭의 워크플로우 슬롯에 그대로 채워지고, 그다음은 업로드한 파일과 완전히 같은
    # 경로(POST /api/upload)를 탄다 — 여기서 큐에 직접 넣지 않는 이유다.
    body = await request.body()
    try:
        spec = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    if not isinstance(spec, dict):
        raise HTTPException(400, "스펙이 JSON 객체가 아니에요.")

    try:
        _, object_info = await asyncio.to_thread(
            fetch_comfy_object_info, False, object_info_pod(pod_id))
    except Exception:
        object_info = None

    # ComfyUI가 떠 있으면 고른 값들이 실제로 설치/지원되는지 먼저 본다. 꺼져 있으면
    # 확인할 기준이 없으니 검증을 건너뛴다(다른 경로들과 같은 규칙).
    if object_info is not None:
        def require_installed(value: str, kind: str, label: str):
            if not value:
                return
            source = MODEL_LIST_SOURCES.get(kind)
            if source is None:
                return
            # 목록이 통째로 비었으면(새로 만든 파드) 막지 않는다 — 작업을 받아 두고 스케줄러가 "없는 모델"로 기다리며
            # 자동 설치하게 한다(job_missing_on_pod는 빈 목록도 "없음"으로 본다 — 역할이 다르다).
            installed = combo_choices(object_info, *source)
            if installed and value not in installed:
                raise HTTPException(400, f"{label} '{value}'은(는) 지금 연결된 ComfyUI에 설치돼 있지 않아요.")

        architecture = str(spec.get("architecture") or "sdxl").strip()
        # krea.2/MiniMax-H3는 체크포인트가 아니라 UNETLoader(디퓨전 모델) 파일을 쓴다.
        checkpoint_kind = "checkpoints" if architecture == "sdxl" else "diffusion_models"
        # MiniMax-H3는 spec["checkpoint"](마법사 1단계에서 고른 파일 — family 표시/LoRA
        # 필터링용일 뿐)가 아니라 workflow_builder_minimax_h3.py가 정한 고정 UNet 파일을
        # 실제로 쓴다 — 그 파일이 설치돼 있는지를 확인해야 "설치돼 있다고 나왔는데 실행하면
        # 없다"는 불일치가 안 생긴다.
        minimax_required = {
            "minimax_h3_i2v": workflow_builder_minimax_h3.I2V_UNET_NAME,
            "minimax_h3_r2v": workflow_builder_minimax_h3.R2V_UNET_NAME,
        }.get(architecture)
        if minimax_required is not None:
            # 고른 UNet(파인튜닝 모델 포함)이 있으면 빌더가 그 파일을 쓰므로 그걸 확인한다.
            require_installed(workflow_builder_minimax_h3.unet_for(spec, minimax_required), checkpoint_kind, "체크포인트")
        else:
            require_installed(str(spec.get("checkpoint") or "").strip(), checkpoint_kind, "체크포인트")
        require_installed(str(spec.get("vae") or "").strip(), "vae", "VAE")
        if architecture == "unet":
            require_installed(str(spec.get("clip") or "").strip(), "text_encoders", "텍스트 인코더")
        for lora in (spec.get("loras") or []):
            if isinstance(lora, dict):
                require_installed(str(lora.get("name") or "").strip(), "loras", "LoRA")
        for field, label in (("sampler_name", "샘플러"), ("scheduler", "스케줄러")):
            value = str(spec.get(field) or "").strip()
            choices = combo_choices(object_info, "KSampler", field)
            if value and choices and value not in choices:
                raise HTTPException(400, f"{label} '{value}'은(는) 이 ComfyUI가 지원하지 않아요.")

    architecture = str(spec.get("architecture") or "sdxl").strip()
    try:
        if architecture == "sdxl":
            workflow = build_workflow(spec)
        elif architecture == "unet":
            workflow = workflow_builder_unet.build_workflow(spec)
        elif architecture == "krea2":
            workflow = workflow_builder_krea2.build_workflow(spec)
        elif architecture == "minimax_h3_i2v":
            workflow = workflow_builder_minimax_h3.build_i2v_workflow(spec)
        elif architecture == "minimax_h3_r2v":
            workflow = workflow_builder_minimax_h3.build_r2v_workflow(spec)
        else:
            raise HTTPException(400, f"알 수 없는 architecture 값이에요: {architecture}")
    except WorkflowBuildError as e:
        raise HTTPException(400, str(e))
    return {"workflow": workflow}


@app.post("/api/validate-workflow")
async def validate_workflow(request: Request, pod_id: str | None = None):
    # 업로드하려는 워크플로우가 이 서버에서 돌아갈 수 있는지 미리 확인한다 —
    # 다른 ComfyUI 설치본에서 만든 워크플로우는 여기 없는 커스텀 노드를 쓰거나
    # 없는 체크포인트/LoRA 파일을 가리키기 쉬운데, 지금은 그걸 배치가 한참
    # 돌다가 실패해야 알 수 있다. 어디까지나 안내용이라 큐 등록 자체는 막지 않는다.
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    workflow = data.get("workflow") if isinstance(data, dict) and "workflow" in data else data
    if not isinstance(workflow, dict):
        raise HTTPException(400, "워크플로우 JSON(노드 id → 노드) 형식이 아니에요.")

    try:
        comfy_url, object_info = await asyncio.to_thread(
            fetch_comfy_object_info, False, object_info_pod(pod_id))
    except Exception as e:
        raise HTTPException(502, f"ComfyUI 노드 목록을 가져오지 못했어요: {e}")
    if object_info is None:
        # 연결이 안 됐으면 "문제 없음"이 아니라 "확인 못 함"이다 — 화면에서 구분해서 안내한다.
        return {
            "connected": False, "url": comfy_url, "ok": True, "auto": pod_id == "auto",
            "missing_nodes": [], "missing_values": [], "checked_nodes": 0,
        }

    missing_nodes, missing_values, checked = workflow_missing(object_info, workflow)
    if missing_values:
        user = me(request)
        pod_id_used = object_info_pod(pod_id).get("id")
        await asyncio.to_thread(annotate_available_on, missing_values, user, pod_id_used)
    return {
        "connected": True,
        "url": comfy_url,
        "ok": not missing_nodes and not missing_values,
        "missing_nodes": missing_nodes,
        "missing_values": missing_values,
        "checked_nodes": checked,
    }


@app.post("/api/enhance-prompt")
async def enhance_prompt(request: Request):
    # "새 작업 추가"의 메인 프롬프트 옆 "Prompt Enhance" 버튼이 호출한다. 큐에 올리는
    # 배치 작업과 달리 응답을 바로 화면에 보여줘야 하므로 워커 큐를 거치지 않고
    # 이 요청을 처리하는 동안 ComfyUI에 동기적으로(스레드로 감싸서) 물어본다.
    data = await request.json()
    prompt = str(data.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "개선할 프롬프트를 입력하세요.")
    mode = data.get("mode") if data.get("mode") in ("natural", "danbooru") else "natural"
    if await asyncio.to_thread(lambda: enhancer_model_missing(object_info_pod(data.get("pod_id")))):
        return JSONResponse(status_code=409, content={
            "error": "model_missing", "kind": "text_encoders", "model": ENHANCER_MODEL,
            "detail": f"이 파드에 텍스트 인코더 {ENHANCER_MODEL}가 없어요. 모델 탭에서 받아 주세요."})
    enhanced =await asyncio.to_thread(_enhance_prompt_sync, prompt, mode)
    return {"enhanced": enhanced}


def parse_ref_kind(raw: str | None, *, allow_none: bool = False) -> str:
    """요청의 kind 파라미터(pose/depth/lineart)를 검증한다. allow_none이면
    "none"도 허용한다(보조 참조를 안 쓰는 경우를 나타내는 값)."""
    choices = REF_KINDS + (("none",) if allow_none else ())
    if raw not in choices:
        raise HTTPException(400, f"kind는 {choices} 중 하나여야 해요.")
    return raw


@app.get("/api/assets")
def list_assets(kind: str = "pose"):
    # 업로드 폼의 "인물 수"/"세트" 캐스케이딩 드롭다운을 채우는 용도. kind(pose/
    # depth/lineart)별로 완전히 분리된 트리를 돌려준다 — char_no별 세트 목록을
    # 트리로 한 번에 돌려줘서, 프론트엔드가 "인물 수"를 바꿀 때마다 서버에 다시
    # 요청하지 않고도 "세트" 드롭다운을 그 자리에서 다시 채울 수 있게 한다. 매
    # 호출마다 폴더를 다시 스캔해서 방금 새로 올려둔 세트도 반영한다.
    kind = parse_ref_kind(kind)
    return {"char_nos": list_assets_tree(kind)}


@app.post("/api/assets/import-from-output")
async def import_output_images_to_ref_set(request: Request):
    # 갤러리에서 마음에 든 결과 이미지를 참조 세트(포즈/depth/lineart)로 보내는
    # 용도 — 지금까지는 이 세트들을 채우려면 서버 파일시스템에 직접 올려야
    # 했는데, 이 엔드포인트가 그 유일한 업로드 경로다. char_no/세트는 존재하지
    # 않으면(새 인물 수·새 세트) save_ref_image가 그대로 폴더를 만들어서, 이
    # 하나로 기존 세트 추가와 새 세트 생성을 둘 다 처리한다. 원본은 지우지
    # 않고 사본만 만든다(복사).
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")

    names = parse_image_names_body(data)
    kind = parse_ref_kind(data.get("kind", "pose"))
    try:
        char_no = validate_new_char_no(data.get("char_no"))
        ref_set = validate_new_folder_name(data.get("set_name"), "참조 세트")
    except RefAssetError as e:
        raise HTTPException(400, str(e))

    added = 0
    skipped = []
    for name in names:
        try:
            path = resolve_output_image(name)
        except HTTPException as e:
            skipped.append({"name": name, "reason": e.detail})
            continue

        # job_id별 하위 폴더(작업 정보 없으면 그대로)에서 온 이름이라, 참조 세트의
        # 평평한 구조에 맞게 "<job_id>_<원본파일명>"으로 합친다 — 어느 작업에서
        # 나온 참조인지 파일명만 보고 알 수 있게 하기 위함(output_images.py 참고).
        parts = name.split("/")
        dest_filename = f"{parts[0]}_{parts[-1]}" if len(parts) > 1 else parts[0]

        try:
            content = await asyncio.to_thread(path.read_bytes)
            await asyncio.to_thread(save_ref_image, kind, char_no, ref_set, dest_filename, content)
        except OSError as e:
            skipped.append({"name": name, "reason": f"저장 실패: {e}"})
            continue
        added += 1

    return {"added": added, "skipped": skipped}


def resolve_option_kind(option: dict, options_so_far: dict) -> str:
    """char_no/asset_folder 옵션이 참조할 종류(pose/depth/lineart/none)를 정한다.
    "kind"를 정적으로 선언했으면 그대로(주 참조), "kind_from"을 선언했으면
    (manifest에서 이 옵션보다 앞에 선언된) 다른 옵션이 고른 종류를 그대로
    따라간다(보조 참조 — 예: secondary_kind가 "depth"면 secondary_char_no/
    secondary_set도 depth 트리를 본다). 둘 다 없으면 예전 pose 전용 동작과
    같도록 "pose"를 기본값으로 쓴다."""
    if option.get("kind"):
        return option["kind"]
    kind_from = option.get("kind_from")
    if kind_from:
        return options_so_far.get(kind_from, "none")
    return "pose"


def coerce_option(option: dict, raw: str | None, options_so_far: dict, pod: dict | None = None):
    if raw is None or raw == "":
        raw = option.get("default")
    # 비면 그 작업이 애초에 성공할 수 없는 옵션(예: 셸 명령의 "명령")은 큐에 넣기 전에
    # 막는다 — 돌려봐야 실패할 작업이 대기 목록에 쌓이면 안 되므로.
    if option.get("required") and (raw is None or str(raw).strip() == ""):
        raise HTTPException(400, f"'{option['label']}'을(를) 입력하세요.")
    opt_type = option.get("type")

    if opt_type == "number":
        try:
            num = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(400, f"'{option['label']}' 값이 올바른 숫자가 아니에요.")
        return int(num) if num.is_integer() else num

    if opt_type == "number_optional":
        # width/height처럼 비워두면 워크플로우에 이미 들어있는 값을 그대로 두는
        # 게 정상 동작인 숫자 옵션. "number"와 달리 빈 값을 기본값으로 치환하지
        # 않고(위에서 raw = default로 대체됐더라도 default 자체가 빈 문자열이면
        # 그대로 빈 채로) 그대로 통과시킨다.
        if raw is None or str(raw).strip() == "":
            return ""
        try:
            num = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(400, f"'{option['label']}' 값이 올바른 숫자가 아니에요.")
        return int(num) if num.is_integer() else num

    if opt_type == "select":
        choices = option.get("choices") or []
        if raw not in choices:
            raise HTTPException(400, f"'{option['label']}' 값은 {choices} 중 하나여야 해요.")
        return raw

    if opt_type == "comfy_model":
        # ComfyUI에 실제로 설치된 목록(MODEL_LIST_SOURCES의 종류)에서 고르는 드롭다운.
        # 비워두면 "워크플로우에 이미 들어있는 값을 그대로 쓴다"는 뜻이라 그냥 통과.
        value = "" if raw is None else str(raw).strip()
        if not value:
            return ""
        source = MODEL_LIST_SOURCES.get(option.get("model_kind"))
        if source is None:
            raise HTTPException(400, f"'{option['label']}' 옵션의 model_kind 설정이 올바르지 않아요.")
        try:
            # 그 작업이 실제로 갈 파드의 설치 목록으로 검증한다 — 다른 파드에만
            # 있는 체크포인트를 통과시키면 실행 직전에야 실패한다.
            _, object_info = fetch_comfy_object_info(False, pod)
        except Exception:
            object_info = None
        # ComfyUI가 꺼져 있으면 검증할 기준 자체가 없다. 이 앱은 ComfyUI가 안 떠 있는
        # 동안에도 큐에 미리 쌓아두는 걸 정상 동작으로 보므로(워커가 실행 직전에 다시
        # 확인함), 확인할 수 없을 때는 막지 않고 그대로 통과시킨다.
        if object_info is None:
            return value
        # 빈 목록(새 파드)이면 막지 않는다 — 위 require_installed와 같은 이유(받아 두고 자동 설치로 기다린다).
        installed = combo_choices(object_info, *source)
        if installed and value not in installed:
            raise HTTPException(
                400,
                f"'{option['label']}' 값 '{value}'은(는) 지금 연결된 ComfyUI에 설치돼 있지 않아요.",
            )
        return value

    if opt_type == "char_no":
        # "인물 수" 드롭다운 — 실제 존재하는 <kind> 종류 하위 숫자 폴더 중 하나여야
        # 함. kind는 옵션이 정적으로 선언(주 참조, 예: "kind": "pose")하거나,
        # "kind_from"으로 다른 옵션(보조 참조 종류 선택)의 값을 그대로 따라간다 —
        # 그 옵션 값이 "none"(보조 참조 안 씀)이면 이 옵션도 의미가 없으니 빈
        # 값을 그대로 통과시킨다(검증하지 않음).
        kind = resolve_option_kind(option, options_so_far)
        if kind == "none":
            return ""
        value = "" if raw is None else str(raw)
        if value not in list_char_nos(kind):
            raise HTTPException(400, f"'{option['label']}' 값이 올바르지 않아요.")
        return value

    if opt_type == "input_image":
        # img2img/USDU 워크플로우 유형이 쓰는, 세트 구분 없는 평평한 입력 이미지
        # 목록(input_assets.py)에서 파일 하나를 고르는 드롭다운.
        value = "" if raw is None else str(raw).strip()
        if not value:
            raise HTTPException(400, f"'{option['label']}' 값을 선택해야 해요.")
        try:
            resolve_input_image(value)
        except InputAssetError as e:
            raise HTTPException(400, str(e))
        return value

    # 아래 세 개는 "안 써도 되는" 참조 슬롯(MiniMax-H3 r2v의 ref_image_1~9/ref_video_1~3/
    # ref_audio_1~3)이 쓴다 — 비어 있으면 그냥 통과(number_optional과 같은 "_optional
    # 접미사는 비어도 됨" 규칙), 값이 있으면 그 종류의 자산 풀에 실제로 있는지만 검증한다.
    if opt_type == "input_image_optional":
        value = "" if raw is None else str(raw).strip()
        if not value:
            return ""
        try:
            resolve_input_image(value)
        except InputAssetError as e:
            raise HTTPException(400, str(e))
        return value

    if opt_type == "input_video":
        value = "" if raw is None else str(raw).strip()
        if not value:
            return ""
        try:
            resolve_input_video(value)
        except InputAssetError as e:
            raise HTTPException(400, str(e))
        return value

    if opt_type == "input_audio":
        value = "" if raw is None else str(raw).strip()
        if not value:
            return ""
        try:
            resolve_input_audio(value)
        except InputAssetError as e:
            raise HTTPException(400, str(e))
        return value

    if opt_type == "asset_folder":
        # 잡을 큐에 올리는 시점(업로드 시)에 참조 세트 폴더가 실제로 있고 이미지가
        # 있는지 미리 확인해서, 큐 시작 이후에야 실패하는 일이 없게 한다. char_no는
        # option["char_no_option"](기본 "char_no")이 가리키는 다른 옵션의 이미
        # 처리된 값을 스코프로 쓰고, 없으면 DEFAULT_CHAR_NO를 쓴다. kind는 위
        # char_no와 같은 규칙("kind" 정적 선언 또는 "kind_from") — "none"이면
        # 보조 참조를 안 쓰는 경우이니 검증 없이 빈 값을 통과시킨다.
        kind = resolve_option_kind(option, options_so_far)
        if kind == "none":
            return ""
        value = "" if raw is None else str(raw)
        char_no_key = option.get("char_no_option", "char_no")
        char_no = options_so_far.get(char_no_key, DEFAULT_CHAR_NO)
        try:
            validate_ref_set(kind, value, char_no)
        except RefAssetError as e:
            raise HTTPException(400, str(e))
        return value

    return "" if raw is None else str(raw)


def validate_ref_csv_rows(csv_bytes: bytes, kind: str, ref_column: str, char_no_column: str = "char_no"):
    # *_csv_batch 공용 업로드 시점 검증: CSV의 모든 행을 미리 훑어 ref_column(및
    # char_no_column) 값이 실제로 해석 가능한지(resolve_ref) 확인한다. 스크립트가
    # 실행되다가 특정 행에서야 실패하는 일이 없도록, 한 행이라도 문제가 있으면
    # 업로드 자체를 거부한다. char_no_column은 ref_column을 지정한 행에서만
    # 의미가 있으므로(참조 폴더의 탐색 루트일 뿐), ref_column이 비어 있는 행은
    # 건드리지 않는다 — 주 참조든 보조 참조든 같은 규칙이라 이 함수 하나를
    # 컬럼 이름만 바꿔서 재사용한다.
    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "CSV가 유효한 UTF-8 텍스트가 아니에요.")

    rows = list(csv.DictReader(io.StringIO(text)))
    errors = []
    for line_no, row in enumerate(rows, start=2):  # 헤더가 1번 줄
        ref_value = (row.get(ref_column) or "").strip()
        if not ref_value:
            continue
        try:
            char_no = parse_char_no(row.get(char_no_column))
        except ValueError:
            errors.append(f"{line_no}번째 줄({char_no_column}='{row.get(char_no_column)}'): 정수가 아니에요.")
            continue
        try:
            resolve_ref(kind, ref_value, char_no)
        except RefReferenceError as e:
            errors.append(f"{line_no}번째 줄({ref_column}='{ref_value}', {char_no_column}='{char_no}'): {e}")

    if errors:
        raise HTTPException(400, f"CSV의 {ref_column}/{char_no_column} 컬럼을 확인하세요.\n" + "\n".join(errors))


def csv_has_ref_value(csv_bytes: bytes, column: str) -> bool:
    # *_csv_batch는 CSV 행마다 참조 컬럼이 비어 있으면 그 행은 의도적으로
    # ControlNet 없이 생성한다(README 참고) — 모든 행의 값이 비어 있으면 워크플로우에
    # LoadImage 노드가 없어도 문제가 없으므로, 그런 경우까지 아래
    # validate_workflow_has_ref_node()가 막아버리지 않도록 미리 구분해둔다.
    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False  # 디코딩 자체는 validate_ref_csv_rows가 이미 걸러줌
    rows = csv.DictReader(io.StringIO(text))
    return any((row.get(column) or "").strip() for row in rows)


