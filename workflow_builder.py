"""워크플로우 JSON을 코드로 조립한다 — 업로드 없이 "워크플로우 빌더" 탭에서 쓴다.

워크플로우 JSON은 결국 "노드 id → {class_type, inputs}" 맵일 뿐이라, 사용자가
ComfyUI에서 직접 만들어 export한 파일만 쓸 이유가 없다. 여기서는 대부분의 t2i
파이프라인이 공유하는 표준 모양을 스펙(체크포인트/LoRA/프롬프트/샘플러/해상도)
으로 받아 그대로 조립한다.

    CheckpointLoaderSimple
      → LoraLoader 체인 (0개 이상)
      → CLIPTextEncode 긍정/부정
      → EmptyLatentImage → KSampler
      → (선택) LatentUpscaleBy → KSampler 2차 패스(hires-fix)
      → VAEDecode → SaveImage

만들어진 결과물은 "업로드한 워크플로우"와 완전히 같은 자격으로 다뤄진다 —
작업 관리 탭의 워크플로우 슬롯에 그대로 채워 넣어 기존 큐/실행 경로를 그대로
타고, templates/*.py의 주입(시드/프롬프트/해상도/체크포인트/LoRA)도 그대로 걸린다.
그래서 노드 제목(_meta.title)을 그 주입 로직이 찾는 규칙에 맞춰 붙인다:

    "main_prompt"  apply_main_prompt가 제목으로만 찾는다(종류 무관)
    "KSampler"     SEED_NODE_TITLE 기본값
    "Empty Latent Image"  LATENT_NODE_TITLE 기본값 "latent"에 걸린다
    "Save Image"   SAVE_NODE_TITLE 기본값 "Save"

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

    checkpoint = str(spec.get("checkpoint") or "").strip()
    if not checkpoint:
        raise WorkflowBuildError("체크포인트를 골라야 해요.")

    positive = str(spec.get("positive") or "").strip()
    if not positive:
        raise WorkflowBuildError("긍정 프롬프트를 입력해야 해요.")
    negative = str(spec.get("negative") if spec.get("negative") is not None else DEFAULT_NEGATIVE)

    width = _int(spec.get("width"), "너비", default=1024, minimum=8)
    height = _int(spec.get("height"), "높이", default=1024, minimum=8)
    batch_size = _int(spec.get("batch_size"), "배치 크기", default=1, minimum=1)
    seed = _int(spec.get("seed"), "시드", default=0, minimum=0)
    steps = _int(spec.get("steps"), "스텝", default=28, minimum=1)
    cfg = _float(spec.get("cfg"), "CFG", default=6.0, minimum=0)
    sampler_name = str(spec.get("sampler_name") or "dpmpp_2m").strip()
    scheduler = str(spec.get("scheduler") or "karras").strip()
    vae = str(spec.get("vae") or "").strip()

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

    # 해상도 주입이 이 노드를 집도록, 업스케일 노드보다 먼저 넣는다.
    latent_id = add(
        "EmptyLatentImage",
        {"width": width, "height": height, "batch_size": batch_size},
        "Empty Latent Image",
    )

    sampler_inputs = {
        "seed": seed,
        "steps": steps,
        "cfg": cfg,
        "sampler_name": sampler_name,
        "scheduler": scheduler,
        "denoise": 1.0,
        "model": model_src,
        "positive": [positive_id, 0],
        "negative": [negative_id, 0],
        "latent_image": [latent_id, 0],
    }
    sampler_id = add("KSampler", dict(sampler_inputs), "KSampler")
    result_latent = [sampler_id, 0]

    hires = spec.get("hires") or {}
    if isinstance(hires, dict) and hires.get("enabled"):
        scale_by = _float(hires.get("scale_by"), "hires 배율", default=1.5, minimum=1.0, maximum=4.0)
        hires_denoise = _float(hires.get("denoise"), "hires denoise", default=0.5, minimum=0.0, maximum=1.0)
        hires_steps = _int(hires.get("steps"), "hires 스텝", default=steps, minimum=1)
        upscale_id = add(
            "LatentUpscaleBy",
            {"upscale_method": str(hires.get("upscale_method") or "nearest-exact"),
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

    if vae:
        vae_src = [add("VAELoader", {"vae_name": vae}, "VAE 로드"), 0]
    else:
        vae_src = [ckpt_id, CHECKPOINT_VAE]

    decode_id = add("VAEDecode", {"samples": result_latent, "vae": vae_src}, "VAE 디코드")
    add(
        "SaveImage",
        {"filename_prefix": str(spec.get("filename_prefix") or "nightshift"), "images": [decode_id, 0]},
        "Save Image",
    )
    return workflow
