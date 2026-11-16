"""외부 서비스가 프리셋 없이 고르는 생성 모델 목록과, 그 모델로 t2i 워크플로우를 조립하는 일을 한다.

모델 등록부(model_registry)에 올라 있는 체크포인트와, 범용 UNet·krea.2 디퓨전 모델만 고를 수 있다.
조립은 "워크플로우 빌더" 탭과 같은 빌더(workflow_builder*.py)를 쓰고, 칸이 비면 등록부의 스윗 포인트를 쓴다.
"""

import model_registry
import workflow_builder
import workflow_builder_krea2
import workflow_builder_unet

# 비율별 크기 — SDXL 계열 버킷(약 1메가픽셀)을 그대로 쓴다.
ASPECTS = {"1:1": (1024, 1024), "4:3": (1152, 896), "3:4": (896, 1152),
           "16:9": (1344, 768), "9:16": (768, 1344)}
MAX_COUNT = 4
KINDS = ("checkpoints", "diffusion_models")


class ModelError(ValueError):
    pass


def _architecture(entry):
    """(architecture, 부품) — 고를 수 없는 모델이면 (None, None)."""
    if entry["kind"] == "checkpoints":
        return "sdxl", {}
    gids = [model_registry.base_id(b) for b in entry["base_models"]]
    if "krea.2" in gids:
        return "krea2", {}
    for gid in gids:
        parts = model_registry.unet_image_parts(gid)
        if parts and parts.get("clip") and parts.get("vae"):
            return "unet", parts
    return None, None


def model_id(entry):
    return f"{entry['kind']}:{entry['filename']}"


def list_models():
    """[{id, label, base_model, architecture}] — 등록부 순서(종류, 파일 이름)대로."""
    out = []
    for entry in model_registry.list_entries():
        if entry["kind"] not in KINDS:
            continue
        architecture, _ = _architecture(entry)
        if architecture is None:
            continue
        label = entry["filename"].rsplit("/", 1)[-1].rsplit(".", 1)[0]
        out.append({"id": model_id(entry), "label": label, "base_model": entry["base_model"],
                    "architecture": architecture})
    return out


def find(value):
    """모델 id로 등록부 항목을 찾는다. 고를 수 없는 모델이면 None."""
    if not isinstance(value, str) or ":" not in value:
        return None
    kind, _, filename = value.partition(":")
    if kind not in KINDS or not filename:
        return None
    entry = model_registry.get_entry(kind, filename)
    if entry is None or _architecture(entry)[0] is None:
        return None
    return entry


def build(entry, prompt, negative_prompt, aspect, seed):
    """(workflow, width, height, steps, positive) — 프롬프트·시드·크기를 넣은 t2i 워크플로우.
    positive는 스윗 포인트 접두어까지 붙은 실제 프롬프트다(실행 템플릿이 main_prompt로 다시 넣는다)."""
    if aspect not in ASPECTS:
        raise ModelError("허용되지 않은 비율이에요.")
    width, height = ASPECTS[aspect]
    architecture, parts = _architecture(entry)
    sweet = entry.get("sweet") or {}
    prefix = str(sweet.get("positive_prefix") or "").strip()
    spec = {"base": "txt2img", "checkpoint": entry["filename"],
            "positive": f"{prefix}, {prompt}" if prefix else prompt,
            "negative": negative_prompt or sweet.get("negative") or workflow_builder.DEFAULT_NEGATIVE,
            "seed": seed, "width": width, "height": height, "batch_size": 1,
            **{key: sweet[key] for key in ("steps", "cfg", "sampler_name", "scheduler") if sweet.get(key) not in (None, "")}}
    try:
        if architecture == "sdxl":
            workflow = workflow_builder.build_workflow(spec)
        elif architecture == "unet":
            workflow = workflow_builder_unet.build_workflow({**spec, **parts})
        else:
            workflow = workflow_builder_krea2.build_workflow({**spec, "refine_prompt": False})
    except workflow_builder.WorkflowBuildError as exc:
        raise ModelError(str(exc)) from exc
    sampler = next((node for node in workflow.values()
                    if isinstance(node, dict) and node.get("class_type") == "KSampler"), None)
    steps = sampler["inputs"].get("steps") if sampler else spec.get("steps")
    return workflow, width, height, steps, spec["positive"]


# ── 기존 그림 한 장 보정(POST /api/generate/refine) ─────────────────────
# face: FaceDetailer 단독 그래프(workflow_builder base="face_detailer"). krea.2는 FaceDetailer 빌더가 없어
#       등록부의 Anima(UNet형) 모델로 대신 한다 — 얼굴 조각만 다시 그리므로 그림체 차이가 작다.
# upscale: 2배 고정. 확대 모델(4배)로 키운 뒤 절반으로 줄이고, SDXL·UNet형은 같은 모델로 Ultimate SD Upscale을
#       낮은 denoise로 한 번 더 돌려 디테일을 채운다. krea.2는 확대 모델만 쓴다(다시 그리지 않는다).
REFINE_OPS = ("face", "upscale")
REFINE_UPSCALE_MODEL = "4x_foolhardy_Remacri.safetensors"
REFINE_UPSCALE_BY = 2.0
REFINE_USDU_DENOISE = 0.25


def architecture(entry):
    return _architecture(entry)[0]


def family_id(entry):
    return model_registry.base_id((entry.get("base_models") or [entry.get("base_model") or ""])[0])


def face_fallback():
    """krea.2 그림의 얼굴 보정에 쓸 Anima 모델 — 'base'가 들어간 파일을 먼저. 없으면 None."""
    candidates = [model_registry.get_entry(*row["id"].split(":", 1)) for row in list_models()
                  if row["architecture"] == "unet" and model_registry.base_id(row["base_model"]) == "anima"]
    candidates = [entry for entry in candidates if entry]
    candidates.sort(key=lambda entry: "base" not in entry["filename"].lower())
    return candidates[0] if candidates else None


def refine_entry(entry, op):
    """보정에 실제로 쓸 등록부 항목(krea.2의 얼굴 보정만 다른 모델로 바뀐다)."""
    if op == "face" and architecture(entry) not in ("sdxl", "unet"):
        fallback = face_fallback()
        if fallback is None:
            raise ModelError("얼굴 보정에 쓸 Anima 모델이 등록부에 없어요.")
        return fallback
    return entry


def _sampling(entry, arch):
    sweet = entry.get("sweet") or {}
    base = dict(workflow_builder_unet.DEFAULTS) if arch == "unet" else {
        "steps": 28, "cfg": 6.0, "sampler_name": "dpmpp_2m", "scheduler": "karras"}
    base.update({key: sweet[key] for key in ("steps", "cfg", "sampler_name", "scheduler") if sweet.get(key) not in (None, "")})
    return base


def build_refine(entry, op, prompt, negative_prompt, seed, face_defaults=None):
    """(workflow, 쓴 항목) — 입력 그림은 제목 "input_image"인 LoadImage에 실행 템플릿(input_image_batch)이 넣는다."""
    if op not in REFINE_OPS:
        raise ModelError(f"보정 종류는 {', '.join(REFINE_OPS)} 중 하나여야 해요.")
    used = refine_entry(entry, op)
    arch, parts = _architecture(used)
    sweet = used.get("sweet") or {}
    prefix = str(sweet.get("positive_prefix") or "").strip()
    positive = (f"{prefix}, {prompt}" if prefix and prompt else prompt or prefix).strip()
    negative = negative_prompt or sweet.get("negative") or workflow_builder.DEFAULT_NEGATIVE
    if op == "face":
        if not positive:
            raise ModelError("얼굴 보정에는 프롬프트가 필요해요.")
        spec = {"base": "face_detailer", "checkpoint": used["filename"], "positive": positive, "negative": negative,
                "seed": seed, "filename_prefix": "refine_face", "face_detailer": {"enabled": True}}
        try:
            if arch == "unet":
                workflow = workflow_builder.build_workflow({**spec, **parts}, loader=workflow_builder_unet._unet_loader,
                                                           face_defaults=face_defaults or workflow_builder_unet.FACE_DETAILER_DEFAULTS)
            else:
                workflow = workflow_builder.build_workflow(spec, face_defaults=face_defaults or workflow_builder.FACE_DETAILER_DEFAULTS)
        except workflow_builder.WorkflowBuildError as exc:
            raise ModelError(str(exc)) from exc
        return workflow, used

    workflow, counter = {}, [0]

    def add(class_type, inputs, title):
        counter[0] += 1
        workflow[str(counter[0])] = {"inputs": inputs, "class_type": class_type, "_meta": {"title": title}}
        return str(counter[0])

    image = add("LoadImage", {"image": ""}, "input_image")
    upscaler = add("UpscaleModelLoader", {"model_name": REFINE_UPSCALE_MODEL}, "확대 모델 로드")
    big = add("ImageUpscaleWithModel", {"upscale_model": [upscaler, 0], "image": [image, 0]}, "확대 모델로 키우기")
    # 확대 모델은 4배 — 원본의 2배가 되게 절반으로 줄인다.
    result = [add("ImageScaleBy", {"image": [big, 0], "upscale_method": "lanczos", "scale_by": REFINE_UPSCALE_BY / 4},
                  "2배로 맞추기"), 0]
    if arch in ("sdxl", "unet"):
        if not positive:
            raise ModelError("업스케일에는 프롬프트가 필요해요.")
        try:
            if arch == "unet":
                model, clip, vae = workflow_builder_unet._unet_loader({"checkpoint": used["filename"], **parts}, add)
            else:
                model, clip, vae = workflow_builder._checkpoint_loader({"checkpoint": used["filename"]}, add)
        except workflow_builder.WorkflowBuildError as exc:
            raise ModelError(str(exc)) from exc
        sampling = _sampling(used, arch)
        pos = add("CLIPTextEncode", {"text": positive, "clip": clip}, "업스케일 긍정")
        neg = add("CLIPTextEncode", {"text": negative, "clip": clip}, "업스케일 부정")
        usdu = add("UltimateSDUpscaleNoUpscale", {
            "image": result, "model": model, "positive": [pos, 0], "negative": [neg, 0], "vae": vae,
            "seed": seed, "steps": int(sampling["steps"]), "cfg": float(sampling["cfg"]),
            "sampler_name": str(sampling["sampler_name"]), "scheduler": str(sampling["scheduler"]),
            "denoise": REFINE_USDU_DENOISE, "mode_type": "Linear", "tile_width": 1024, "tile_height": 1024,
            "mask_blur": 8, "tile_padding": 32, "seam_fix_mode": "None", "seam_fix_denoise": 1.0, "seam_fix_width": 64,
            "seam_fix_mask_blur": 8, "seam_fix_padding": 16, "force_uniform_tiles": True, "tiled_decode": False,
        }, "Ultimate SD Upscale")
        result = [usdu, 0]
    add("SaveImage", {"filename_prefix": "refine_upscale", "images": result}, "Save Image")
    return workflow, used
