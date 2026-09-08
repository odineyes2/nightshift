"""워크플로우 JSON을 코드로 조립한다 — 업로드 없이 "워크플로우 빌더" 탭과 "새 작업
추가" 마법사에서 쓴다.

워크플로우 JSON은 결국 "노드 id → {class_type, inputs}" 맵일 뿐이라, 사용자가
ComfyUI에서 직접 만들어 export한 파일만 쓸 이유가 없다. 여기서는 "베이스(첫
샘플링을 어디서 시작하는지) + 후처리(그 결과에 이어 붙이는 추가 패스)"로 조합해서
표준 파이프라인을 조립한다. 마법사의 "워크플로우 유형" 단계가 이 조합 방식과
정확히 대응한다(베이스는 하나만, 후처리는 0개 이상 동시에 고를 수 있음 — 예:
txt2img+hires-fix, txt2img+usdu, txt2img+hires-fix+usdu, img2img+usdu 등).
ControlNet/IPAdapter는 체크포인트마다 배선이 달라 이 빌더가 조립하지 않고
family별로 관리자가 통째로 올려둔 프리셋 워크플로우를 그대로 쓴다(app.py의
workflow_presets 참고) — 그래서 베이스/후처리와 동시에 쓸 수 없다.

spec["base"](기본 "txt2img"):
    txt2img — EmptyLatentImage(spec["width"]/["height"]/["batch_size"])에서 시작
    img2img — LoadImage(제목 "input_image", 실행 시점에 템플릿이 실제 파일로
        덮어씀) → VAEEncode에서 시작(denoise<1, spec["denoise"]). EmptyLatentImage가
        없어 크기는 입력 이미지를 따른다.

공통 베이스 패스:
    CheckpointLoaderSimple → LoraLoader 체인(0개 이상) → CLIPTextEncode 긍정/부정
      → (베이스별 latent 소스) → KSampler

spec["hires_fix"] = {"enabled": true, "scale_by", "denoise", "steps"} (선택,
베이스 뒤에 이어 붙임):
    KSampler의 latent 결과 → LatentUpscaleBy → KSampler 2차 패스(다른 seed/denoise/
    스텝) — 그 결과가 이후 단계(VAEDecode 또는 usdu)의 입력이 된다.

spec["usdu"] = {"enabled": true, "upscale_by", "upscale_method", "denoise"} (선택,
hires_fix보다 뒤, VAEDecode 다음에 이어 붙임 — 커스텀 노드
"UltimateSDUpscaleNoUpscale"(ssitu/ComfyUI_UltimateSDUpscale)가 설치돼 있어야 함):
    (베이스/hires_fix가 만든) IMAGE → ImageScaleBy(spec["usdu"]["upscale_by"]배)
      → UltimateSDUpscaleNoUpscale(타일 단위로 다시 샘플링, VAE 디코드까지 노드
        내부에서 처리) → 이 결과가 최종 SaveImage 입력이 된다.
    업스케일 모델(ESRGAN 등)을 쓰는 별도 "upscale_models" 체인은 일부러 넣지
    않는다 — 어떤 업스케일 모델이 설치돼 있을지 보장할 수 없어서, 항상 만들 수
    있는 알고리즘 기반 리사이즈(ImageScaleBy)만 쓴다.
    usdu만 켜고 베이스를 img2img로 고르면(hires_fix 없이) "외부 이미지를 그대로
    업스케일/디테일 보강"에 가깝게 쓸 수 있다(예전에 있던 "usdu 단독 모드"를
    "img2img(denoise를 낮게) + usdu"로 표현하는 셈 — 별도 모드를 두지 않고 같은
    조합 규칙 안에서 다뤄지도록 통합했다).

만들어진 결과물은 "업로드한 워크플로우"와 완전히 같은 자격으로 다뤄진다 —
작업 관리 탭의 워크플로우 슬롯에 그대로 채워 넣어 기존 큐/실행 경로를 그대로
타고, templates/*.py의 주입(시드/프롬프트/해상도/체크포인트/LoRA/입력 이미지)도
그대로 걸린다. 그래서 노드 제목(_meta.title)을 그 주입 로직이 찾는 규칙에
맞춰 붙인다:

    "main_prompt"   apply_main_prompt가 제목으로만 찾는다(종류 무관)
    "KSampler"      SEED_NODE_TITLE 기본값 — 베이스 패스의 KSampler에만 붙는다
                    (hires_fix의 2차 KSampler는 "KSampler (hires-fix)"라 실행 시점
                    시드 주입 대상에서 제외됨 — 기존 hires-fix 동작과 동일)
    "Empty Latent Image"  LATENT_NODE_TITLE 기본값 "latent"에 걸린다(base="txt2img"만
                    해당 — img2img는 이 노드가 없어서 해상도 주입은 아무 노드도
                    못 찾았다는 경고만 남기고 건너뛴다. 크기는 입력 이미지가 결정
                    하므로 정상 동작이다)
    "input_image"   apply_input_image류(templates/input_image_*.py)가 제목으로
                    찾는다(base="img2img" 전용)
    "Save Image"    SAVE_NODE_TITLE 기본값 "Save"

hires-fix의 업스케일 노드 제목에는 일부러 "latent"를 넣지 않는다 — 넣으면
해상도 주입(apply_resolution)이 EmptyLatentImage 대신 그 노드를 집을 수 있다.
같은 이유로 EmptyLatentImage를 업스케일 노드보다 먼저 넣는다(딕셔너리 순서).
"""

DEFAULT_NEGATIVE = "worst quality, low quality, bad anatomy, bad hands, text, watermark, signature"

# 노드 id는 그냥 1부터 붙인 문자열이다(ComfyUI API 형식은 id가 문자열이기만 하면 됨).
# 링크는 ["노드id", 출력번호] 꼴이고, 출력번호는 그 노드의 출력 순서다:
#   CheckpointLoaderSimple → 0:MODEL, 1:CLIP, 2:VAE
#   LoraLoader             → 0:MODEL, 1:CLIP
#   CLIPTextEncode         → 0:CONDITIONING
#   EmptyLatentImage/KSampler/LatentUpscaleBy → 0:LATENT
#   VAEDecode              → 0:IMAGE
CHECKPOINT_MODEL, CHECKPOINT_CLIP, CHECKPOINT_VAE = 0, 1, 2


class WorkflowBuildError(Exception):
    """스펙이 워크플로우로 만들 수 없는 값일 때."""


def _int(value, name, default=None, minimum=None):
    if value is None or value == "":
        if default is None:
            raise WorkflowBuildError(f"{name} 값이 필요해요.")
        value = default
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        raise WorkflowBuildError(f"{name} 값 '{value}'은(는) 숫자가 아니에요.")
    if minimum is not None and result < minimum:
        raise WorkflowBuildError(f"{name} 값은 {minimum} 이상이어야 해요.")
    return result


def _float(value, name, default=None, minimum=None, maximum=None):
    if value is None or value == "":
        if default is None:
            raise WorkflowBuildError(f"{name} 값이 필요해요.")
        value = default
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise WorkflowBuildError(f"{name} 값 '{value}'은(는) 숫자가 아니에요.")
    if minimum is not None and result < minimum:
        raise WorkflowBuildError(f"{name} 값은 {minimum} 이상이어야 해요.")
    if maximum is not None and result > maximum:
        raise WorkflowBuildError(f"{name} 값은 {maximum} 이하여야 해요.")
    return result


def build_workflow(spec: dict) -> dict:
    """스펙(dict)으로 ComfyUI API 형식 워크플로우(dict)를 만든다."""
    if not isinstance(spec, dict):
        raise WorkflowBuildError("스펙이 JSON 객체가 아니에요.")

    base = str(spec.get("base") or "txt2img").strip()
    if base not in ("txt2img", "img2img"):
        raise WorkflowBuildError(f"base 값 '{base}'은(는) txt2img/img2img 중 하나여야 해요.")

    checkpoint = str(spec.get("checkpoint") or "").strip()
    if not checkpoint:
        raise WorkflowBuildError("체크포인트를 골라야 해요.")

    positive = str(spec.get("positive") or "").strip()
    if not positive:
        raise WorkflowBuildError("긍정 프롬프트를 입력해야 해요.")
    negative = str(spec.get("negative") if spec.get("negative") is not None else DEFAULT_NEGATIVE)

    seed = _int(spec.get("seed"), "시드", default=0, minimum=0)
    steps = _int(spec.get("steps"), "스텝", default=28, minimum=1)
    cfg = _float(spec.get("cfg"), "CFG", default=6.0, minimum=0)
    sampler_name = str(spec.get("sampler_name") or "dpmpp_2m").strip()
    scheduler = str(spec.get("scheduler") or "karras").strip()
    vae = str(spec.get("vae") or "").strip()
    filename_prefix = str(spec.get("filename_prefix") or "nightshift")

    workflow: dict = {}
    next_id = [1]

    def add(class_type: str, inputs: dict, title: str) -> str:
        node_id = str(next_id[0])
        next_id[0] += 1
        workflow[node_id] = {"inputs": inputs, "class_type": class_type, "_meta": {"title": title}}
        return node_id

    ckpt_id = add("CheckpointLoaderSimple", {"ckpt_name": checkpoint}, "체크포인트 로드")

    # LoRA 체인 — 각 로더의 model/clip 출력을 다음 로더로 이어 붙인다.
    model_src = [ckpt_id, CHECKPOINT_MODEL]
    clip_src = [ckpt_id, CHECKPOINT_CLIP]
    loras = spec.get("loras") or []
    if not isinstance(loras, list):
        raise WorkflowBuildError("loras는 목록이어야 해요.")
    for index, lora in enumerate(loras, start=1):
        if not isinstance(lora, dict):
            raise WorkflowBuildError("각 LoRA 항목은 객체여야 해요.")
        name = str(lora.get("name") or "").strip()
        if not name:
            continue  # 비워둔 줄은 그냥 건너뛴다(화면에서 빈 줄을 남겨둘 수 있음)
        lora_id = add(
            "LoraLoader",
            {
                "lora_name": name,
                "strength_model": _float(lora.get("strength_model"), f"LoRA {index} 모델 강도", default=1.0),
                "strength_clip": _float(lora.get("strength_clip"), f"LoRA {index} 클립 강도", default=1.0),
                "model": model_src,
                "clip": clip_src,
            },
            f"LoRA {index}",
        )
        model_src = [lora_id, 0]
        clip_src = [lora_id, 1]

    # 프롬프트 — 긍정 노드 제목은 반드시 "main_prompt"를 포함해야 한다(위 모듈 설명 참고).
    positive_id = add("CLIPTextEncode", {"text": positive, "clip": clip_src}, "main_prompt")
    negative_id = add("CLIPTextEncode", {"text": negative, "clip": clip_src}, "negative_prompt")

    # VAE는 인코드/디코드/USDU 내부 어디서든 필요해서 분기 전에 미리 만든다.
    if vae:
        vae_src = [add("VAELoader", {"vae_name": vae}, "VAE 로드"), 0]
    else:
        vae_src = [ckpt_id, CHECKPOINT_VAE]

    if base == "img2img":
        # 실제 파일명은 templates/input_image_*.py가 실행 시점에 덮어쓴다(위
        # 모듈 설명의 "input_image" 참고) — 여기서는 자리표시자만 넣어둔다.
        image_id = add("LoadImage", {"image": ""}, "input_image")
        encode_id = add("VAEEncode", {"pixels": [image_id, 0], "vae": vae_src}, "VAE Encode")
        latent_src = [encode_id, 0]
        denoise = _float(spec.get("denoise"), "denoise", default=0.6, minimum=0.0, maximum=1.0)
    else:
        # 해상도 주입이 이 노드를 집도록, 이후에 추가될 업스케일 노드보다 먼저 넣는다.
        width = _int(spec.get("width"), "너비", default=1024, minimum=8)
        height = _int(spec.get("height"), "높이", default=1024, minimum=8)
        batch_size = _int(spec.get("batch_size"), "배치 크기", default=1, minimum=1)
        latent_id = add(
            "EmptyLatentImage",
            {"width": width, "height": height, "batch_size": batch_size},
            "Empty Latent Image",
        )
        latent_src = [latent_id, 0]
        denoise = 1.0

    sampler_inputs = {
        "seed": seed,
        "steps": steps,
        "cfg": cfg,
        "sampler_name": sampler_name,
        "scheduler": scheduler,
        "denoise": denoise,
        "model": model_src,
        "positive": [positive_id, 0],
        "negative": [negative_id, 0],
        "latent_image": latent_src,
    }
    sampler_id = add("KSampler", dict(sampler_inputs), "KSampler")
    result_latent = [sampler_id, 0]

    hires_fix = spec.get("hires_fix") or {}
    if isinstance(hires_fix, dict) and hires_fix.get("enabled"):
        scale_by = _float(hires_fix.get("scale_by"), "hires 배율", default=1.5, minimum=1.0, maximum=4.0)
        hires_denoise = _float(hires_fix.get("denoise"), "hires denoise", default=0.5, minimum=0.0, maximum=1.0)
        hires_steps = _int(hires_fix.get("steps"), "hires 스텝", default=steps, minimum=1)
        upscale_id = add(
            "LatentUpscaleBy",
            {"upscale_method": str(hires_fix.get("upscale_method") or "nearest-exact"),
             "scale_by": scale_by, "samples": result_latent},
            "Hires Upscale",  # 제목에 "latent"를 넣지 않는다(해상도 주입이 여길 집으면 안 됨)
        )
        hires_inputs = dict(sampler_inputs)
        hires_inputs.update({
            "seed": seed + 1,
            "steps": hires_steps,
            "denoise": hires_denoise,
            "latent_image": [upscale_id, 0],
        })
        hires_id = add("KSampler", hires_inputs, "KSampler (hires-fix)")
        result_latent = [hires_id, 0]

    decode_id = add("VAEDecode", {"samples": result_latent, "vae": vae_src}, "VAE 디코드")
    result_image = [decode_id, 0]

    usdu = spec.get("usdu") or {}
    if isinstance(usdu, dict) and usdu.get("enabled"):
        upscale_by = _float(usdu.get("upscale_by"), "업스케일 배율", default=2.0, minimum=1.0, maximum=8.0)
        scaled_id = add(
            "ImageScaleBy",
            {
                "image": result_image,
                "upscale_method": str(usdu.get("upscale_method") or "lanczos"),
                "scale_by": upscale_by,
            },
            "Upscale Image By",
        )
        usdu_denoise = _float(usdu.get("denoise"), "USDU denoise", default=0.35, minimum=0.0, maximum=1.0)
        usdu_id = add(
            "UltimateSDUpscaleNoUpscale",
            {
                "image": [scaled_id, 0],
                "model": model_src,
                "positive": [positive_id, 0],
                "negative": [negative_id, 0],
                "vae": vae_src,
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": sampler_name,
                "scheduler": scheduler,
                "denoise": usdu_denoise,
                "mode_type": "Linear",
                "tile_width": 512,
                "tile_height": 512,
                "mask_blur": 8,
                "tile_padding": 32,
                "seam_fix_mode": "None",
                "seam_fix_denoise": 1.0,
                "seam_fix_width": 64,
                "seam_fix_mask_blur": 8,
                "seam_fix_padding": 16,
                "force_uniform_tiles": True,
                "tiled_decode": False,
            },
            "Ultimate SD Upscale",
        )
        # UltimateSDUpscaleNoUpscale은 내부에서 VAE 디코드까지 마쳐 IMAGE를
        # 바로 내놓으므로, 이 결과를 그대로 최종 이미지로 쓴다.
        result_image = [usdu_id, 0]

    add("SaveImage", {"filename_prefix": filename_prefix, "images": result_image}, "Save Image")
    return workflow
