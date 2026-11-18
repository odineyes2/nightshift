"""외부 서비스가 프리셋 없이 고르는 생성 모델 목록과, 그 모델로 t2i 워크플로우를 조립하는 일을 한다.

모델 등록부(model_registry)에 올라 있는 체크포인트와, 범용 UNet·krea.2 디퓨전 모델만 고를 수 있다.
조립은 "워크플로우 빌더" 탭과 같은 빌더(workflow_builder*.py)를 쓰고, 칸이 비면 등록부의 스윗 포인트를 쓴다.
"""

import lora_access
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


MAX_LORAS = 3


def clean_loras(entry, raw, user_id=None):
    """요청의 loras([{name, strength}])를 검사해 [(파일 이름, 강도)]로 — 등록부에 있는 LoRA만, 생성 모델과 계열이 맞아야 한다.
    계열을 적지 않은 LoRA는 그대로 받는다. 인물 LoRA(캐릭터 스튜디오)를 연동 서비스가 고를 때 쓴다."""
    if raw in (None, []):
        return []
    if not isinstance(raw, list) or len(raw) > MAX_LORAS:
        raise ModelError(f"loras는 {MAX_LORAS}개까지의 목록이어야 해요.")
    family = family_id(entry)
    out = []
    for item in raw:
        name = item.get("name") if isinstance(item, dict) else None
        lora = model_registry.get_entry("loras", name) if isinstance(name, str) and name else None
        if lora is None:
            raise ModelError("등록부에 없는 LoRA예요.")
        if not lora_access.can_use(name, user_id):   # 다른 회원이 학습한 비공개 인물 LoRA
            raise ModelError(f"쓸 수 없는 LoRA예요: {name}")
        bases = {model_registry.base_id(b) for b in lora.get("base_models") or []}
        if bases and family not in bases:
            raise ModelError(f"이 생성 모델에는 쓸 수 없는 LoRA예요: {name}")
        strength = item.get("strength", 1.0)
        if isinstance(strength, bool) or not isinstance(strength, (int, float)) or not 0 < strength <= 1.5:
            raise ModelError("LoRA 강도는 0보다 크고 1.5 이하여야 해요.")
        if name in [n for n, _ in out]:
            raise ModelError("같은 LoRA를 두 번 넣을 수 없어요.")
        out.append((name, float(strength)))
    return out


def _lora_spec(loras):
    return [{"name": name, "strength_model": strength, "strength_clip": strength} for name, strength in loras]


MAX_REGIONS = 3


def clean_regions(raw):
    """요청의 regions([문자열])을 검사한다 — 그림을 가로로 똑같이 나눈 칸마다(왼쪽부터) 그 칸에 그릴 인물의 묘사.
    두 인물 그림에서 누가 왼쪽인지 정해 두면 얼굴별 보정(refine faces)이 그 순서로 인물을 맞출 수 있다."""
    if raw in (None, []):
        return []
    if not isinstance(raw, list) or not 2 <= len(raw) <= MAX_REGIONS:
        raise ModelError(f"regions는 2~{MAX_REGIONS}개의 목록이어야 해요.")
    if any(not isinstance(text, str) or not text.strip() or len(text) > 1000 for text in raw):
        raise ModelError("regions의 각 칸은 1~1000자 묘사여야 해요.")
    return [text.strip() for text in raw]


def _apply_regions(workflow, regions, width, height):
    """메인 긍정 조건에 칸별 조건(ConditioningSetArea)을 더해 모든 KSampler에 넣는다. 노드 제목에는 실행 템플릿이
    부분일치로 집는 낱말(main_prompt·negative_prompt·KSampler·Save·latent)을 넣지 않는다."""
    samplers = [node for node in workflow.values() if isinstance(node, dict) and node.get("class_type") == "KSampler"]
    if not samplers:
        raise ModelError("이 생성 모델은 칸 나누기를 할 수 없어요.")
    positive = samplers[0]["inputs"]["positive"]
    clip = workflow[str(positive[0])]["inputs"]["clip"]
    next_id = max(int(key) for key in workflow) + 1
    combined, step = positive, width // len(regions) // 8 * 8
    for index, text in enumerate(regions):
        encode, area, merge = str(next_id), str(next_id + 1), str(next_id + 2)
        next_id += 3
        workflow[encode] = {"inputs": {"text": text, "clip": clip}, "class_type": "CLIPTextEncode", "_meta": {"title": f"칸 {index + 1} 묘사"}}
        workflow[area] = {"inputs": {"conditioning": [encode, 0], "width": step, "height": height, "x": index * step, "y": 0,
                                     "strength": 1.0}, "class_type": "ConditioningSetArea", "_meta": {"title": f"칸 {index + 1} 자리"}}
        workflow[merge] = {"inputs": {"conditioning_1": combined, "conditioning_2": [area, 0]}, "class_type": "ConditioningCombine",
                           "_meta": {"title": f"칸 {index + 1} 합치기"}}
        combined = [merge, 0]
    for sampler in samplers:
        sampler["inputs"]["positive"] = list(combined)


def build(entry, prompt, negative_prompt, aspect, seed, loras=(), regions=()):
    """(workflow, width, height, steps, positive) — 프롬프트·시드·크기를 넣은 t2i 워크플로우.
    positive는 스윗 포인트 접두어까지 붙은 실제 프롬프트다(실행 템플릿이 main_prompt로 다시 넣는다).
    loras는 clean_loras가 돌려준 [(파일 이름, 강도)], regions는 clean_regions가 돌려준 칸별 묘사(왼쪽부터)."""
    if aspect not in ASPECTS:
        raise ModelError("허용되지 않은 비율이에요.")
    width, height = ASPECTS[aspect]
    architecture, parts = _architecture(entry)
    sweet = entry.get("sweet") or {}
    prefix = str(sweet.get("positive_prefix") or "").strip()
    spec = {"base": "txt2img", "checkpoint": entry["filename"],
            "positive": f"{prefix}, {prompt}" if prefix else prompt,
            "negative": negative_prompt or sweet.get("negative") or workflow_builder.DEFAULT_NEGATIVE,
            "seed": seed, "width": width, "height": height, "batch_size": 1, "loras": _lora_spec(loras),
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
    if regions:
        if architecture not in ("sdxl", "unet"):
            raise ModelError("이 생성 모델은 칸 나누기를 할 수 없어요.")
        _apply_regions(workflow, regions, width, height)
    sampler = next((node for node in workflow.values()
                    if isinstance(node, dict) and node.get("class_type") == "KSampler"), None)
    steps = sampler["inputs"].get("steps") if sampler else spec.get("steps")
    return workflow, width, height, steps, spec["positive"]


# ── 기존 그림 한 장 보정(POST /api/generate/refine) ─────────────────────
# faces: 얼굴마다 그 인물의 프롬프트·LoRA로 다시 그린다(왼쪽부터). 두 인물 그림에서 LoRA를 함께 걸면 눈색 같은
#       세부가 서로 섞이는 것을 고친다. 큰 얼굴부터 얼굴 수만큼 고르고(뒤쪽 군중 제외), 그림을 가로로 얼굴 수만큼 나눈
#       칸(생성의 regions와 같은 칸)에 왼쪽 끝이 들어가는 얼굴만 그 인물로 다시 그린다. 한 사람이 뒤돌아 얼굴이 하나뿐이면
#       그 얼굴은 자기 칸의 인물로만 다시 그린다 — 순서로 짝지으면 남의 얼굴로 바뀌었다(2026-11-17 실제 생성).
# face: FaceDetailer 단독 그래프(workflow_builder base="face_detailer"). krea.2는 FaceDetailer 빌더가 없어
#       등록부의 Anima(UNet형) 모델로 대신 한다 — 얼굴 조각만 다시 그리므로 그림체 차이가 작다.
# upscale: 2배 고정. 확대 모델(4배)로 키운 뒤 절반으로 줄이고, SDXL·UNet형은 같은 모델로 Ultimate SD Upscale을
#       낮은 denoise로 한 번 더 돌려 디테일을 채운다. krea.2는 확대 모델만 쓴다(다시 그리지 않는다).
REFINE_OPS = ("face", "upscale", "faces")
MAX_FACES = 3
FACES_DENOISE = 0.5
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
            "upscaled_image": result, "model": model, "positive": [pos, 0], "negative": [neg, 0], "vae": vae,
            "seed": seed, "steps": int(sampling["steps"]), "cfg": float(sampling["cfg"]),
            "sampler_name": str(sampling["sampler_name"]), "scheduler": str(sampling["scheduler"]),
            "denoise": REFINE_USDU_DENOISE, "mode_type": "Linear", "tile_width": 1024, "tile_height": 1024,
            "mask_blur": 8, "tile_padding": 32, "seam_fix_mode": "None", "seam_fix_denoise": 1.0, "seam_fix_width": 64,
            "seam_fix_mask_blur": 8, "seam_fix_padding": 16, "force_uniform_tiles": True, "tiled_decode": False,
            "batch_size": 1,
        }, "Ultimate SD Upscale")
        result = [usdu, 0]
    add("SaveImage", {"filename_prefix": "refine_upscale", "images": result}, "Save Image")
    return workflow, used


def build_faces(entry, faces, negative_prompt, seed, width=1344):
    """얼굴별 보정 워크플로우 — faces는 왼쪽부터 [(프롬프트, [(LoRA 파일, 강도)])]. 입력 그림은 제목 "input_image"인 LoadImage."""
    arch, parts = _architecture(entry)
    if arch not in ("sdxl", "unet"):
        raise ModelError("이 생성 모델은 얼굴별 보정을 할 수 없어요.")
    if not faces or len(faces) > MAX_FACES:
        raise ModelError(f"얼굴은 1~{MAX_FACES}개까지 넣을 수 있어요.")
    sweet = entry.get("sweet") or {}
    prefix = str(sweet.get("positive_prefix") or "").strip()
    negative = negative_prompt or sweet.get("negative") or workflow_builder.DEFAULT_NEGATIVE
    sampling = _sampling(entry, arch)
    workflow, counter = {}, [0]

    def add(class_type, inputs, title):
        counter[0] += 1
        workflow[str(counter[0])] = {"inputs": inputs, "class_type": class_type, "_meta": {"title": title}}
        return str(counter[0])

    image = [add("LoadImage", {"image": ""}, "input_image"), 0]
    try:
        if arch == "unet":
            model, clip, vae = workflow_builder_unet._unet_loader({"checkpoint": entry["filename"], **parts}, add)
        else:
            model, clip, vae = workflow_builder._checkpoint_loader({"checkpoint": entry["filename"]}, add)
    except workflow_builder.WorkflowBuildError as exc:
        raise ModelError(str(exc)) from exc
    neg = add("CLIPTextEncode", {"text": negative, "clip": clip}, "얼굴 부정")
    detector = add("UltralyticsDetectorProvider", {"model_name": workflow_builder.FACE_DETECTOR_MODEL}, "얼굴 탐지 모델")
    found = add("BboxDetectorSEGS", {"bbox_detector": [detector, 0], "image": image, "threshold": 0.5, "dilation": 10,
                                     "crop_factor": 3.0, "drop_size": 10, "labels": "all"}, "얼굴 찾기")
    rest = [add("ImpactSEGSOrderedFilter", {"segs": [found, 0], "target": "area(=w*h)", "order": True,
                                            "take_start": 0, "take_count": len(faces)}, "큰 얼굴 고르기"), 0]
    step, shift = width / len(faces), width / 16   # 얼굴 왼쪽 끝은 얼굴 가운데보다 왼쪽이라 경계를 조금 당긴다
    for index, (prompt, loras) in enumerate(faces, start=1):
        if not prompt:
            raise ModelError("얼굴마다 프롬프트가 필요해요.")
        low = 0 if index == 1 else int(step * (index - 1) - shift)
        high = width * 4 if index == len(faces) else int(step * index - shift) - 1
        pick = add("ImpactSEGSRangeFilter", {"segs": rest, "target": "x1", "mode": True, "min_value": low, "max_value": high},
                   f"얼굴 {index} 고르기")
        face_model = model
        for name, strength in loras:
            face_model = [add("LoraLoaderModelOnly", {"lora_name": name, "strength_model": strength, "model": face_model},
                              f"얼굴 {index} LoRA"), 0]
        pos = add("CLIPTextEncode", {"text": f"{prefix}, {prompt}" if prefix else prompt, "clip": clip}, f"얼굴 {index} 긍정")
        image = [add("DetailerForEach", {
            "image": image, "segs": [pick, 0], "model": face_model, "clip": clip, "vae": vae, "guide_size": 512,
            "guide_size_for": True, "max_size": 1024, "seed": seed + index, "steps": 20, "cfg": float(sampling["cfg"]),
            "sampler_name": "euler", "scheduler": "simple", "positive": [pos, 0], "negative": [neg, 0],
            "denoise": FACES_DENOISE, "feather": 5, "noise_mask": True, "force_inpaint": True, "wildcard": "", "cycle": 1,
            "inpaint_model": False, "noise_mask_feather": 20}, f"얼굴 {index} 다시 그리기"), 0]
    add("SaveImage", {"filename_prefix": "refine_faces", "images": image}, "Save Image")
    return workflow
