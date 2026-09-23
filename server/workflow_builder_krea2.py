"""krea.2 워크플로우를 스펙에서 조립한다 — workflow_builder.py의 SDXL식(CheckpointLoaderSimple)
빌더와 나란히 쓰는, krea.2 전용 빌더다. krea.2는 UNETLoader/CLIPLoader가 분리돼 있고(체크포인트
개념이 없음), 선택적으로 LLM(TextGenerate)로 프롬프트를 보강하는 구조라 SDXL 빌더에 끼워 넣을 수
없다(app.py의 build_workflow_api가 spec["architecture"]로 이쪽으로 분기한다).

spec["checkpoint"]는 UNETLoader의 unet_name이다(체크포인트가 아니라 디퓨전 모델 파일명 — 마법사가
model_registry의 diffusion_models kind에서 고른 값을 그대로 여기 넣는다).

spec["refine_prompt"](기본 true)가 true면 사용자가 준 프롬프트를 시스템 프롬프트와 합쳐
TextGenerate(krea.2의 CLIP 자체가 LLM)로 보강한 뒤 인코딩하고, false면 원문을 그대로 인코딩한다.
원본 ComfyUI 워크플로우는 이 on/off를 ComfySwitchNode로 실행 시점에 고르지만, 여기서는 매번
새로 조립하므로 안 쓰는 쪽 노드를 아예 만들지 않는다.

"main_prompt" 제목은 원문 프롬프트를 받는 Primitive 노드에 붙인다(리파인 전 단계) — CLIPTextEncode가
아니다. 그래야 templates/seed_batch.py의 런타임 프롬프트 override(MAIN_PROMPT)가 리파인 체인을
다시 타면서 반영된다(CLIPTextEncode에 직접 덮어쓰면 TextGenerate가 계산한 결과를 버리게 됨).

negative는 krea.2 자체가 안 쓴다(cfg=1, ConditioningZeroOut으로 대체) — spec에 없어도 된다.
"""

from workflow_builder import WorkflowBuildError, _float, _int

UNET_WEIGHT_DTYPE = "default"
CLIP_NAME = "qwen3vl_4b_fp8_scaled.safetensors"
VAE_NAME = "qwen_image_vae.safetensors"

# krea.2 예시 워크플로우의 리파인용 시스템 프롬프트를 그대로 쓴다(프롬프트 엔지니어링 지침이라
# 바꾸면 리파인 품질이 달라진다 — 손대려면 의도적으로 바꿀 때만).
SYSTEM_PROMPT = (
    "You are an expert prompt engineer for text-to-image models. Your task is to expand the "
    "user's prompt into a highly effective image-generation prompt.\n\n"
    "Think step by step about the request before writing the answer:\n"
    "- What is the subject and mood?\n"
    "- What visual styles, mediums, and lighting options would fit? Consider two or three "
    "alternatives and pick the one that best serves the caption.\n"
    "- What composition, framing, and grounded details will help the text-to-image model?\n\n"
    "Then output a single expanded prompt paragraph.\n\n"
    "Follow these rules strictly:\n"
    "1. **Faithfulness First:** Preserve all original subjects, actions, colors, and spatial "
    "relationships. Do not add new objects, props, characters, or animals unless the user "
    "clearly implies them.\n"
    "2. **Practical T2I Structure:** Write a prompt that a text-to-image model can parse "
    "cleanly. Group subjects with their own attributes and actions. Use grounded phrasing for "
    "poses, interactions, and spatial layout.\n"
    "3. **Style Planning Stays Internal:** Use your internal reasoning to choose style, medium, "
    "framing, and lighting. Do not emit planning tags or wrappers in the visible answer body.\n"
    "4. **Text Rendering:** If the user requests visible text, quotes, labels, or typography, "
    "specify the exact text clearly and wrap requested words in quotes.\n"
    "5. **Avoid Over-Specification:** Do not invent highly specific clothing, colors, "
    "materials, or scene details unless the input supports them.\n"
    "6. **Structure:** Write one cohesive paragraph after the thinking block. No bullets, "
    "JSON, or markdown.\n"
    "7. **Respect Existing Detail:** If the user's prompt is already detailed, lightly polish "
    "and finalize rather than heavily expanding — preserve their phrasing and direction.\n"
    "8. **Respect the Human Form:** Treat depictions of people with dignity. Assume clothing "
    "covers genitals and intimate anatomy.\n"
    "9. **Preserve User Medium:** When the user explicitly requests a medium (e.g. \"photo "
    "of\", \"photograph of\", \"illustration of\", \"painting of\", \"sketch of\", \"3D render "
    "of\"), honor it. Do not pivot to a different medium to avoid difficulty — match the "
    "user's stated intent.\n\n"
    "User's Input:\n\n"
)


def build_workflow(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise WorkflowBuildError("스펙이 JSON 객체가 아니에요.")

    checkpoint = str(spec.get("checkpoint") or "").strip()
    if not checkpoint:
        raise WorkflowBuildError("디퓨전 모델(UNet) 파일을 골라야 해요.")
    positive = str(spec.get("positive") or "").strip()
    if not positive:
        raise WorkflowBuildError("긍정 프롬프트를 입력해야 해요.")

    refine_prompt = spec.get("refine_prompt", True)
    seed = _int(spec.get("seed"), "시드", default=0, minimum=0)
    steps = _int(spec.get("steps"), "스텝", default=8, minimum=1)
    cfg = _float(spec.get("cfg"), "CFG", default=1.0, minimum=0)
    sampler_name = str(spec.get("sampler_name") or "euler").strip()
    scheduler = str(spec.get("scheduler") or "simple").strip()
    width = _int(spec.get("width"), "너비", default=1024, minimum=8)
    height = _int(spec.get("height"), "높이", default=1024, minimum=8)
    batch_size = _int(spec.get("batch_size"), "배치 크기", default=1, minimum=1)
    filename_prefix = str(spec.get("filename_prefix") or "nightshift")

    workflow: dict = {}
    next_id = [1]

    def add(class_type: str, inputs: dict, title: str) -> str:
        node_id = str(next_id[0])
        next_id[0] += 1
        workflow[node_id] = {"inputs": inputs, "class_type": class_type, "_meta": {"title": title}}
        return node_id

    unet_id = add("UNETLoader", {"unet_name": checkpoint, "weight_dtype": UNET_WEIGHT_DTYPE}, "확산 모델 로드")
    clip_id = add("CLIPLoader", {"clip_name": CLIP_NAME, "type": "krea2", "device": "default"}, "CLIP 로드")
    vae_id = add("VAELoader", {"vae_name": VAE_NAME}, "VAE 로드")
    clip_src = [clip_id, 0]
    vae_src = [vae_id, 0]

    model_src = [unet_id, 0]
    loras = spec.get("loras") or []
    if not isinstance(loras, list):
        raise WorkflowBuildError("loras는 목록이어야 해요.")
    for index, lora in enumerate(loras, start=1):
        if not isinstance(lora, dict):
            raise WorkflowBuildError("각 LoRA 항목은 객체여야 해요.")
        name = str(lora.get("name") or "").strip()
        if not name:
            continue
        lora_id = add(
            "LoraLoaderModelOnly",
            {
                "lora_name": name,
                "strength_model": _float(lora.get("strength_model"), f"LoRA {index} 강도", default=1.0),
                "model": model_src,
            },
            f"LoRA {index}",
        )
        model_src = [lora_id, 0]

    # 원문 프롬프트 — 런타임 override(MAIN_PROMPT) 대상. 리파인 체인보다 앞에 둔다.
    main_prompt_id = add("PrimitiveStringMultiline", {"value": positive}, "main_prompt")
    prompt_src = [main_prompt_id, 0]

    if refine_prompt:
        system_prompt_id = add("PrimitiveStringMultiline", {"value": SYSTEM_PROMPT}, "System Prompt")
        concat_id = add(
            "StringConcatenate",
            {"string_a": [system_prompt_id, 0], "string_b": prompt_src, "delimiter": ""},
            "연결",
        )
        refine_id = add(
            "TextGenerate",
            {
                "prompt": [concat_id, 0],
                "max_length": 512,
                "sampling_mode": "on",
                "sampling_mode.temperature": 0.7,
                "sampling_mode.top_k": 64,
                "sampling_mode.top_p": 0.95,
                "sampling_mode.min_p": 0.05,
                "sampling_mode.repetition_penalty": 1.05,
                "sampling_mode.seed": 0,
                "sampling_mode.presence_penalty": 0,
                "thinking": True,
                "use_default_template": True,
                "clip": clip_src,
            },
            "텍스트 생성",
        )
        prompt_src = [refine_id, 0]

    positive_id = add("CLIPTextEncode", {"text": prompt_src, "clip": clip_src}, "CLIP 텍스트 인코딩 (프롬프트)")
    negative_id = add("ConditioningZeroOut", {"conditioning": [positive_id, 0]}, "조건 (0으로 출력)")

    latent_id = add("EmptyLatentImage", {"width": width, "height": height, "batch_size": batch_size}, "Empty Latent Image")

    sampler_id = add(
        "KSampler",
        {
            "seed": seed, "steps": steps, "cfg": cfg, "sampler_name": sampler_name, "scheduler": scheduler,
            "denoise": 1.0, "model": model_src, "positive": [positive_id, 0], "negative": [negative_id, 0],
            "latent_image": [latent_id, 0],
        },
        "KSampler",
    )
    decode_id = add("VAEDecode", {"samples": [sampler_id, 0], "vae": vae_src}, "VAE 디코드")
    add("SaveImage", {"filename_prefix": filename_prefix, "images": [decode_id, 0]}, "Save Image")

    return workflow
