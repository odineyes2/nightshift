"""MiniMax-H3 워크플로우(이미지→영상, 참조→영상)를 스펙에서 조립한다 — krea.2와 같은 이유로
SDXL 빌더(workflow_builder.py)에 못 끼우는 구조라 전용 모듈로 둔다(app.py의 build_workflow_api가
spec["architecture"]로 이쪽에 분기).

i2v/r2v는 UNet 파일이 다르다(각각 fl2va/ref2va — MiniMax가 모드별로 다른 파일을 요구함) — 사용자가
고르는 게 아니라 이 모듈이 상수로 고정한다(마치 templates/wan22_video_batch.py가 "고정 그래프"를
쓰듯). model_registry에는 family 표시/LoRA 호환 필터링용으로 diffusion_models kind에 하나만
등록해 두면 된다 — 실제로 어느 파일을 쓰는지는 이 모듈이 결정한다.

영상 길이는 "video_duration" 제목의 PrimitiveFloat로 둔다(리터럴이 아니라 별도 노드인 이유는
templates/minimax_h3_*_batch.py가 런타임에 이 값을 배치별로 바꿔치기하기 위함 — width/height처럼
"그래프 구조"가 아니라 "값"이라서 빌드 타임이 아니라 런타임에 조정한다). 프레임 길이 환산 공식은
원본 ComfyUI 워크플로우 값을 그대로 쓴다(MiniMax-H3의 latent 윈도우 정렬 요구사항 — 임의로 바꾸면
안 됨).

참조 이미지/비디오/오디오(ref_images/ref_videos/ref_audios)는 개수가 그래프 구조 자체를
바꾸므로(LoadImage/영상로더/LoadAudio 노드 수) 런타임이 아니라 빌드 타임
(spec["ref_image_count"]/["ref_video_count"]/["ref_audio_count"])에 정한다. 셋 다 0이면
참조할 게 없다는 뜻이라 에러다.

비디오 참조는 두 가지 방식이 있다(spec["video_ref_mode"], 기본 "vhs"):
    "vhs"     VHS_LoadVideo(커뮤니티 노드, VideoHelperSuite) 하나로 프레임(출력 0)과
              내장 오디오(출력 2)를 동시에 뽑는다 — 사실상 표준이지만 파드에 그 커스텀
              노드가 없을 수 있다.
    "native"  LoadVideo(네이티브) → GetVideoComponents(네이티브, 출력 0=이미지/1=오디오)
              조합 — 커스텀 노드 없이 되지만 노드가 하나 더 필요하다.
그 비디오에 내장된 오디오는 ref_video_audios.ref_video_audio_N으로 따로 들어간다(순수
오디오 파일 참조인 ref_audios.ref_audio_N과는 별개 슬롯).
"""

from workflow_builder import WorkflowBuildError, _float, _int

I2V_UNET_NAME = "minimax_h3_fl2va_pruned_int8_convrot.safetensors"
R2V_UNET_NAME = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
UNET_WEIGHT_DTYPE = "default"
CLIP_NAME = "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
VIDEO_VAE_NAME = "minimax_h3_video_vae_fp16.safetensors"
AUDIO_VAE_NAME = "minimax_h3_audio_vae_fp32.safetensors"
SAMPLER_NAME = "res_multistep"
SCHEDULER = "simple"
STEPS = 20
VIDEO_REF_MODES = ("vhs", "native")

# 초(duration) -> 프레임 길이 환산. MiniMax-H3는 24fps 기준으로 최소 5프레임, "5 mod 17" 정렬을
# 요구한다(원본 워크플로우 그대로 — 임의로 바꾸지 말 것).
DURATION_TO_LENGTH_EXPR = "max(5, round(a * 24)) + (5 - (max(5, round(a * 24)) % 17)) % 17"

MAX_REF_IMAGES = 9
MAX_REF_VIDEOS = 3
MAX_REF_AUDIOS = 3


def _new_adder(workflow: dict):
    next_id = [1]

    def add(class_type: str, inputs: dict, title: str) -> str:
        node_id = str(next_id[0])
        next_id[0] += 1
        workflow[node_id] = {"inputs": inputs, "class_type": class_type, "_meta": {"title": title}}
        return node_id

    return add


def _lora_chain(add, model_src, loras):
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
    return model_src


def _video_ref_nodes(add, mode: str, index: int):
    """비디오 참조 하나(제목 ref_video_{index})를 만들고 (이미지 출력, 오디오 출력)을 돌려준다."""
    title = f"ref_video_{index}"
    if mode == "vhs":
        node_id = add(
            "VHS_LoadVideo",
            {
                "video": "", "force_rate": 0, "custom_width": 0, "custom_height": 0,
                "frame_load_cap": 0, "skip_first_frames": 0, "select_every_nth": 1,
                "format": "AnimateDiff",
            },
            title,
        )
        return [node_id, 0], [node_id, 2]
    load_id = add("LoadVideo", {"file": "", "video-preview": ""}, title)
    comp_id = add("GetVideoComponents", {"video": [load_id, 0]}, f"{title}_components")
    return [comp_id, 0], [comp_id, 1]


def _audio_ref_node(add, index: int):
    """오디오 참조 하나(제목 ref_audio_{index})를 만들고 오디오 출력을 돌려준다 — 네이티브
    LoadAudio 하나뿐, VHS 같은 대체 방식이 필요 없다."""
    node_id = add("LoadAudio", {"audio": ""}, f"ref_audio_{index}")
    return [node_id, 0]


def _sampling_to_save(add, model_src, cond_node_id, video_vae_src, audio_vae_src, seed, filename_prefix):
    """조건화 노드(MiniMaxH3*ToVideo, 출력 0=CONDITIONING/1=LATENT) 이후의 공통 샘플링→저장 체인."""
    noise_id = add("RandomNoise", {"noise_seed": seed}, "무작위 노이즈")
    sampler_sel_id = add("KSamplerSelect", {"sampler_name": SAMPLER_NAME}, "KSampler (선택)")
    scheduler_id = add(
        "BasicScheduler",
        {"scheduler": SCHEDULER, "steps": STEPS, "denoise": 1, "model": model_src},
        "기본 스케줄러",
    )
    guider_id = add("BasicGuider", {"model": model_src, "conditioning": [cond_node_id, 0]}, "기본 가이드")
    sample_id = add(
        "SamplerCustomAdvanced",
        {
            "noise": [noise_id, 0], "guider": [guider_id, 0], "sampler": [sampler_sel_id, 0],
            "sigmas": [scheduler_id, 0], "latent_image": [cond_node_id, 1],
        },
        "고급 사용자 정의 샘플러",
    )
    video_decode_id = add("VAEDecode", {"samples": [sample_id, 0], "vae": video_vae_src}, "VAE 디코드")
    audio_decode_id = add("VAEDecodeAudio", {"samples": [sample_id, 0], "vae": audio_vae_src}, "오디오 VAE 디코드")
    create_id = add(
        "CreateVideo",
        {"fps": 24, "bit_depth": 8, "images": [video_decode_id, 0], "audio": [audio_decode_id, 0]},
        "비디오 생성",
    )
    add(
        "SaveVideo",
        {"filename_prefix": filename_prefix, "format": "auto", "codec": "auto", "video": [create_id, 0]},
        "비디오 저장",
    )


def unet_for(spec: dict, default: str) -> str:
    """마법사 1단계에서 고른 UNet 파일(spec["checkpoint"]). 안 골랐으면 공식 파일(default).
    파인튜닝 모델(예: dasiwa…)도 같은 그래프에 파일만 바꿔 끼우면 되므로 고른 파일을 그대로 쓴다."""
    return str(spec.get("checkpoint") or "").strip() or default


def build_i2v_workflow(spec: dict) -> dict:
    """Image to Video(정확히는 First/Last Frame to Video, "fl2v") 겸 Text to Video("t2v") —
    둘 다 MiniMaxH3ImageToVideo 노드 하나로 조립되는 같은 워크플로우이고, UNet도 같은
    파일(마법사에서 고른 UNet, 안 고르면 I2V_UNET_NAME — 실제로는 "…fl2va…" 파일, r2v의 "…ref2va…"와 다름)을 쓴다.
    차이는 이미지를 하나도 안 쓰느냐(t2v)뿐이다:
      - 이미지를 하나라도 쓰면(fl2v) width/height를 그 이미지의 실제 크기에서 계산한다
        (ImageScaleToTotalPixels → GetImageSize).
      - 이미지가 하나도 없으면(t2v) 이미지에서 크기를 셀 수 없으므로, width/height를
        각각 PrimitiveInt 노드로 만들어 MiniMaxH3ImageToVideo에 직접 연결한다(리터럴이
        아니라 별도 노드로 두는 이유는 video_duration과 같다 — 나중에 배치별로 값을
        바꿔 끼우기 쉽게 하기 위함)."""
    if not isinstance(spec, dict):
        raise WorkflowBuildError("스펙이 JSON 객체가 아니에요.")
    positive = str(spec.get("positive") or "").strip()
    if not positive:
        raise WorkflowBuildError("프롬프트를 입력해야 해요.")

    use_first_frame = bool(spec.get("use_first_frame", True))
    use_last_frame = bool(spec.get("use_last_frame", False))
    is_t2v = not use_first_frame and not use_last_frame

    seed = _int(spec.get("seed"), "시드", default=0, minimum=0)
    duration = _float(spec.get("duration"), "영상 길이(초)", default=5.0, minimum=1.0, maximum=15.0)
    filename_prefix = str(spec.get("filename_prefix") or "video/MiniMax_H3")

    workflow: dict = {}
    add = _new_adder(workflow)

    unet_id = add("UNETLoader", {"unet_name": unet_for(spec, I2V_UNET_NAME), "weight_dtype": UNET_WEIGHT_DTYPE}, "확산 모델 로드")
    clip_id = add("CLIPLoader", {"clip_name": CLIP_NAME, "type": "minimax", "device": "default"}, "CLIP 로드")
    video_vae_id = add("VAELoader", {"vae_name": VIDEO_VAE_NAME}, "VAE 로드")
    audio_vae_id = add("VAELoader", {"vae_name": AUDIO_VAE_NAME}, "VAE 로드")
    clip_src = [clip_id, 0]

    model_src = _lora_chain(add, [unet_id, 0], spec.get("loras") or [])

    main_prompt_id = add("PrimitiveStringMultiline", {"value": positive}, "main_prompt")
    duration_id = add("PrimitiveFloat", {"value": duration}, "video_duration")
    length_id = add(
        "ComfyMathExpression", {"expression": DURATION_TO_LENGTH_EXPR, "values.a": [duration_id, 0]}, "수학식"
    )

    cond_inputs = {
        "prompt": [main_prompt_id, 0], "length": [length_id, 1], "clip": clip_src, "vae": [video_vae_id, 0],
    }
    first_frame_id = last_frame_id = None
    if is_t2v:
        width = _int(spec.get("width"), "너비", default=704, minimum=64)
        height = _int(spec.get("height"), "높이", default=1280, minimum=64)
        width_id = add("PrimitiveInt", {"value": width}, "width")
        height_id = add("PrimitiveInt", {"value": height}, "height")
        cond_inputs["width"] = [width_id, 0]
        cond_inputs["height"] = [height_id, 0]
    else:
        # first_frame/last_frame은 MiniMaxH3ImageToVideo에서 둘 다 선택 입력이다(둘 중
        # 하나만 써도 되고, 둘 다 쓰면 두 이미지 사이를 보간하는 영상이 되며, 같은
        # 이미지를 양쪽에 넣으면 루프 영상이 된다). 크기 체인은 둘 다 만들 필요 없이
        # first_frame이 있으면 그걸, 없으면 last_frame을 기준으로 하나만 만든다.
        first_frame_id = add("LoadImage", {"image": ""}, "first_frame_image") if use_first_frame else None
        last_frame_id = add("LoadImage", {"image": ""}, "last_frame_image") if use_last_frame else None
        size_source_id = first_frame_id if first_frame_id is not None else last_frame_id
        scaled_id = add(
            "ImageScaleToTotalPixels",
            {"upscale_method": "nearest-exact", "megapixels": 1, "resolution_steps": 32, "image": [size_source_id, 0]},
            "총 픽셀 수에 맞춰 이미지 크기 조정",
        )
        size_id = add("GetImageSize", {"image": [scaled_id, 0]}, "이미지 크기 가져오기")
        cond_inputs["width"] = [size_id, 0]
        cond_inputs["height"] = [size_id, 1]
        if first_frame_id is not None:
            cond_inputs["first_frame"] = [first_frame_id, 0]
        if last_frame_id is not None:
            cond_inputs["last_frame"] = [last_frame_id, 0]

    cond_id = add("MiniMaxH3ImageToVideo", cond_inputs, "MiniMax H3 Image to Video")
    _sampling_to_save(add, model_src, cond_id, [video_vae_id, 0], [audio_vae_id, 0], seed, filename_prefix)
    return workflow


def build_r2v_workflow(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise WorkflowBuildError("스펙이 JSON 객체가 아니에요.")
    positive = str(spec.get("positive") or "").strip()
    if not positive:
        raise WorkflowBuildError("프롬프트를 입력해야 해요.")

    ref_image_count = _int(spec.get("ref_image_count"), "참조 이미지 개수", default=2, minimum=0)
    if ref_image_count > MAX_REF_IMAGES:
        raise WorkflowBuildError(f"참조 이미지는 최대 {MAX_REF_IMAGES}장까지예요.")
    ref_video_count = _int(spec.get("ref_video_count"), "참조 비디오 개수", default=0, minimum=0)
    if ref_video_count > MAX_REF_VIDEOS:
        raise WorkflowBuildError(f"참조 비디오는 최대 {MAX_REF_VIDEOS}개까지예요.")
    ref_audio_count = _int(spec.get("ref_audio_count"), "참조 오디오 개수", default=0, minimum=0)
    if ref_audio_count > MAX_REF_AUDIOS:
        raise WorkflowBuildError(f"참조 오디오는 최대 {MAX_REF_AUDIOS}개까지예요.")
    if ref_image_count + ref_video_count + ref_audio_count == 0:
        raise WorkflowBuildError("참조 이미지/비디오/오디오 중 최소 하나는 있어야 해요.")
    video_ref_mode = str(spec.get("video_ref_mode") or "vhs").strip()
    if video_ref_mode not in VIDEO_REF_MODES:
        raise WorkflowBuildError(f"video_ref_mode 값은 {'/'.join(VIDEO_REF_MODES)} 중 하나여야 해요.")

    seed = _int(spec.get("seed"), "시드", default=0, minimum=0)
    duration = _float(spec.get("duration"), "영상 길이(초)", default=15.0, minimum=1.0, maximum=15.0)
    filename_prefix = str(spec.get("filename_prefix") or "video/MiniMax_H3")

    workflow: dict = {}
    add = _new_adder(workflow)

    unet_id = add("UNETLoader", {"unet_name": unet_for(spec, R2V_UNET_NAME), "weight_dtype": UNET_WEIGHT_DTYPE}, "확산 모델 로드")
    clip_id = add("CLIPLoader", {"clip_name": CLIP_NAME, "type": "minimax", "device": "default"}, "CLIP 로드")
    video_vae_id = add("VAELoader", {"vae_name": VIDEO_VAE_NAME}, "VAE 로드")
    audio_vae_id = add("VAELoader", {"vae_name": AUDIO_VAE_NAME}, "VAE 로드")
    clip_src = [clip_id, 0]

    model_src = _lora_chain(add, [unet_id, 0], spec.get("loras") or [])

    main_prompt_id = add("PrimitiveStringMultiline", {"value": positive}, "main_prompt")
    duration_id = add("PrimitiveFloat", {"value": duration}, "video_duration")
    length_id = add(
        "ComfyMathExpression", {"expression": DURATION_TO_LENGTH_EXPR, "values.a": [duration_id, 0]}, "수학식"
    )

    ref_inputs = {}
    for i in range(1, ref_image_count + 1):
        image_id = add("LoadImage", {"image": ""}, f"ref_image_{i}")
        ref_inputs[f"ref_images.ref_image_{i - 1}"] = [image_id, 0]
    for i in range(1, ref_video_count + 1):
        video_out, video_audio_out = _video_ref_nodes(add, video_ref_mode, i)
        ref_inputs[f"ref_videos.ref_video_{i - 1}"] = video_out
        ref_inputs[f"ref_video_audios.ref_video_audio_{i - 1}"] = video_audio_out
    for i in range(1, ref_audio_count + 1):
        ref_inputs[f"ref_audios.ref_audio_{i - 1}"] = _audio_ref_node(add, i)

    cond_id = add(
        "MiniMaxH3ReferenceToVideo",
        {
            "prompt": [main_prompt_id, 0], "width": 0, "height": 0, "length": [length_id, 1],
            "ref_image_size": "match", "clip": clip_src, "vae": [video_vae_id, 0],
            "audio_vae": [audio_vae_id, 0], **ref_inputs,
        },
        "MiniMax H3 Reference to Video",
    )
    _sampling_to_save(add, model_src, cond_id, [video_vae_id, 0], [audio_vae_id, 0], seed, filename_prefix)
    return workflow
