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

spec["face_detailer"] = {"enabled": true, "positive", "negative", "steps", "cfg", "sampler_name",
"scheduler", "denoise", ...} (선택, 디코드(=hires 뒤) 다음·usdu 앞에 이어 붙임 — Impact-Pack의
FaceDetailer와 Impact-Subpack의 UltralyticsDetectorProvider(bbox/face_yolov8m.pt)가 있어야 함):
    IMAGE → FaceDetailer(LoRA가 걸린 model/clip, 얼굴 전용 긍정·부정 CLIPTextEncode) → (usdu) → SaveImage.
    중간 이미지는 저장하지 않는다(SaveImage는 끝에 하나뿐). 수치 기본값은 로더 형태마다 다르다
    (FACE_DETAILER_DEFAULTS, UNet형은 workflow_builder_unet.FACE_DETAILER_DEFAULTS). 얼굴 프롬프트가
    비면 메인 프롬프트를 쓴다 — 실행 시점 주입(templates/*)이 행마다 다시 넣는다.
spec["controlnet"] = {"enabled": true, "type": "openpose", "control_net_name", "strength", "start_percent",
"end_percent", "dw": {DWPreprocessor 입력}} (선택, txt2img/img2img 전용) — LoadImage("pose_image") →
    DWPreprocessor → ControlNetApplyAdvanced(← ControlNetLoader, 체크포인트 VAE)가 main_prompt·negative_prompt
    조건을 받아 베이스·hires KSampler에 넘긴다. Face Detailer·USDU는 원래 조건을 쓴다.

spec["base"] = "face_detailer"는 기존 이미지 1장의 얼굴만 보정한다:
    LoadImage(제목 "input_image") → FaceDetailer → SaveImage. 메인 KSampler·main_prompt가 없고
    hires_fix/usdu도 붙지 않는다(latent가 없다).
FaceDetailer 쪽 노드 제목("Face Detailer"·"Face Detailer 긍정"/"Face Detailer 부정"·"얼굴 탐지 모델")에는
"main_prompt"·"negative_prompt"·"KSampler"·"Save"·"latent"를 넣지 않는다 — 실행 템플릿의 부분일치
주입이 잘못 집는다.

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

FACE_DETECTOR_MODEL = "bbox/face_yolov8m.pt"
# 체크포인트형 FaceDetailer 수치 기본값(NS-34 본문 값). 스펙의 face_detailer에 값이 있으면 그 값을 쓴다.
FACE_DETAILER_DEFAULTS = {"steps": 20, "cfg": 8.0, "sampler_name": "euler", "scheduler": "simple", "denoise": 0.5}
# 로더 형태와 상관없는 공통 기본값(본문 값).
FACE_DETAILER_COMMON = {
    "guide_size": 512, "max_size": 1024, "feather": 5, "bbox_threshold": 0.5, "bbox_dilation": 10,
    "bbox_crop_factor": 3.0, "drop_size": 10, "cycle": 1, "noise_mask_feather": 20,
}

# OpenPose ControlNet 기본값(NS-41 첨부 워크플로우 값). 스펙의 controlnet·controlnet["dw"]에 값이 있으면 그 값을 쓴다.
CONTROLNET_DEFAULTS = {"strength": 0.8, "start_percent": 0.0, "end_percent": 0.8}
DWPOSE_DEFAULTS = {
    "detect_hand": "enable", "detect_body": "enable", "detect_face": "enable", "resolution": 1024,
    "bbox_detector": "yolox_l.onnx", "pose_estimator": "dw-ll_ucoco_384_bs5.torchscript.pt",
    "scale_stick_for_xinsr_cn": "disable",
}


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


def _checkpoint_loader(spec: dict, add) -> tuple[list, list, list]:
    """CheckpointLoaderSimple(+선택 VAELoader)을 넣고 (model, clip, vae) 링크를 돌려준다."""
    checkpoint = str(spec.get("checkpoint") or "").strip()
    if not checkpoint:
        raise WorkflowBuildError("체크포인트를 골라야 해요.")
    ckpt_id = add("CheckpointLoaderSimple", {"ckpt_name": checkpoint}, "체크포인트 로드")
    vae = str(spec.get("vae") or "").strip()
    vae_src = [add("VAELoader", {"vae_name": vae}, "VAE 로드"), 0] if vae else [ckpt_id, CHECKPOINT_VAE]
    return [ckpt_id, CHECKPOINT_MODEL], [ckpt_id, CHECKPOINT_CLIP], vae_src


def _face_detailer(add, fd: dict, defaults: dict, image, model, clip, vae, seed: int,
                   positive: str, negative: str) -> list:
    """FaceDetailer + 얼굴 탐지 모델 + 얼굴 프롬프트 노드를 넣고 보정된 IMAGE 링크를 돌려준다."""
    face_positive = str(fd.get("positive") or "").strip() or positive
    if not face_positive:
        raise WorkflowBuildError("얼굴 긍정 프롬프트를 입력해야 해요.")
    face_negative = str(fd.get("negative") or "").strip() or negative
    pos_id = add("CLIPTextEncode", {"text": face_positive, "clip": clip}, "Face Detailer 긍정")
    neg_id = add("CLIPTextEncode", {"text": face_negative, "clip": clip}, "Face Detailer 부정")
    detector_id = add("UltralyticsDetectorProvider", {"model_name": FACE_DETECTOR_MODEL}, "얼굴 탐지 모델")
    v = {**FACE_DETAILER_COMMON, **defaults}
    fd_id = add(
        "FaceDetailer",
        {
            "image": image, "model": model, "clip": clip, "vae": vae,
            "guide_size": _int(fd.get("guide_size"), "guide_size", default=v["guide_size"], minimum=64),
            "guide_size_for": True,
            "max_size": _int(fd.get("max_size"), "max_size", default=v["max_size"], minimum=64),
            "seed": seed,
            "steps": _int(fd.get("steps"), "얼굴 스텝", default=v["steps"], minimum=1),
            "cfg": _float(fd.get("cfg"), "얼굴 CFG", default=v["cfg"], minimum=0),
            "sampler_name": str(fd.get("sampler_name") or v["sampler_name"]).strip(),
            "scheduler": str(fd.get("scheduler") or v["scheduler"]).strip(),
            "positive": [pos_id, 0], "negative": [neg_id, 0],
            "denoise": _float(fd.get("denoise"), "얼굴 denoise", default=v["denoise"], minimum=0.0, maximum=1.0),
            "feather": _int(fd.get("feather"), "feather", default=v["feather"], minimum=0),
            "noise_mask": True, "force_inpaint": True,
            "bbox_threshold": _float(fd.get("bbox_threshold"), "bbox_threshold", default=v["bbox_threshold"],
                                     minimum=0.0, maximum=1.0),
            "bbox_dilation": _int(fd.get("bbox_dilation"), "bbox_dilation", default=v["bbox_dilation"]),
            "bbox_crop_factor": _float(fd.get("bbox_crop_factor"), "bbox_crop_factor",
                                       default=v["bbox_crop_factor"], minimum=1.0),
            # sam_*·wildcard·inpaint_model·tiled_*는 첨부 워크플로우 값으로 고정한다(SAM은 안 쓴다).
            "sam_detection_hint": "center-1", "sam_dilation": 0, "sam_threshold": 0.93,
            "sam_bbox_expansion": 0, "sam_mask_hint_threshold": 0.7, "sam_mask_hint_use_negative": "False",
            "drop_size": _int(fd.get("drop_size"), "drop_size", default=v["drop_size"], minimum=1),
            "bbox_detector": [detector_id, 0],
            "wildcard": "",
            "cycle": _int(fd.get("cycle"), "cycle", default=v["cycle"], minimum=1),
            "inpaint_model": False,
            "noise_mask_feather": _int(fd.get("noise_mask_feather"), "noise_mask_feather",
                                       default=v["noise_mask_feather"], minimum=0),
            "tiled_encode": False, "tiled_decode": False,
        },
        "Face Detailer",
    )
    return [fd_id, 0]


def _enable(value, name: str, default: str) -> str:
    value = default if value is None or value == "" else value
    if isinstance(value, bool):
        value = "enable" if value else "disable"
    if value not in ("enable", "disable"):
        raise WorkflowBuildError(f"{name} 값은 enable/disable 중 하나여야 해요.")
    return value


def _openpose_controlnet(add, cn: dict, positive, negative, vae) -> tuple[list, list]:
    """포즈 이미지 → DWPreprocessor → ControlNetApplyAdvanced(← ControlNetLoader)를 넣고 CN이 걸린
    (positive, negative) 조건 링크를 돌려준다. 포즈 이미지 파일명은 실행 시점에 템플릿이 제목 "pose_image"로 찾아 넣는다."""
    name = str(cn.get("control_net_name") or "").strip()
    if not name:
        raise WorkflowBuildError("ControlNet 모델을 골라야 해요.")
    dw = cn.get("dw") or {}
    if not isinstance(dw, dict):
        raise WorkflowBuildError("controlnet.dw는 객체여야 해요.")
    loader_id = add("ControlNetLoader", {"control_net_name": name}, "Load ControlNet Model")
    # 제목에 "input_image"를 넣지 않는다 — img2img 입력 주입이 이 노드를 집으면 안 된다.
    image_id = add("LoadImage", {"image": ""}, "pose_image")
    dw_inputs = {key: _enable(dw.get(key), key, DWPOSE_DEFAULTS[key])
                 for key in ("detect_hand", "detect_body", "detect_face", "scale_stick_for_xinsr_cn")}
    dw_inputs.update({
        "resolution": _int(dw.get("resolution"), "DWPose resolution", default=DWPOSE_DEFAULTS["resolution"], minimum=64),
        "bbox_detector": str(dw.get("bbox_detector") or DWPOSE_DEFAULTS["bbox_detector"]).strip(),
        "pose_estimator": str(dw.get("pose_estimator") or DWPOSE_DEFAULTS["pose_estimator"]).strip(),
        "image": [image_id, 0],
    })
    dw_id = add("DWPreprocessor", dw_inputs, "DWPose Estimator")
    start = _float(cn.get("start_percent"), "start_percent", default=CONTROLNET_DEFAULTS["start_percent"],
                   minimum=0.0, maximum=1.0)
    end = _float(cn.get("end_percent"), "end_percent", default=CONTROLNET_DEFAULTS["end_percent"],
                 minimum=0.0, maximum=1.0)
    if start > end:
        raise WorkflowBuildError("start_percent는 end_percent보다 클 수 없어요.")
    apply_id = add(
        "ControlNetApplyAdvanced",
        {"strength": _float(cn.get("strength"), "ControlNet strength", default=CONTROLNET_DEFAULTS["strength"],
                            minimum=0.0, maximum=2.0),
         "start_percent": start, "end_percent": end,
         "positive": positive, "negative": negative, "control_net": [loader_id, 0],
         "image": [dw_id, 0], "vae": vae},
        "Apply ControlNet",
    )
    return [apply_id, 0], [apply_id, 1]


def build_workflow(spec: dict, loader=_checkpoint_loader, face_defaults: dict = FACE_DETAILER_DEFAULTS) -> dict:
    """스펙(dict)으로 ComfyUI API 형식 워크플로우(dict)를 만든다. loader는 모델 로더 노드를
    넣고 (model, clip, vae) 링크를 돌려준다 — 그 뒤 조립은 로더 형태와 상관없이 같다
    (UNet+CLIP+VAE형은 workflow_builder_unet.py가 자기 로더를 넘긴다)."""
    if not isinstance(spec, dict):
        raise WorkflowBuildError("스펙이 JSON 객체가 아니에요.")

    base = str(spec.get("base") or "txt2img").strip()
    if base not in ("txt2img", "img2img", "face_detailer"):
        raise WorkflowBuildError(f"base 값 '{base}'은(는) txt2img/img2img/face_detailer 중 하나여야 해요.")
    face_detailer = spec.get("face_detailer") or {}
    if not isinstance(face_detailer, dict):
        raise WorkflowBuildError("face_detailer는 객체여야 해요.")
    controlnet = spec.get("controlnet") or {}
    if not isinstance(controlnet, dict):
        raise WorkflowBuildError("controlnet은 객체여야 해요.")
    if controlnet.get("enabled"):
        if str(controlnet.get("type") or "openpose") != "openpose":
            raise WorkflowBuildError("controlnet.type은 openpose만 지원해요.")
        if base == "face_detailer":
            raise WorkflowBuildError("OpenPose ControlNet은 txt2img·img2img와만 함께 쓸 수 있어요.")

    positive =str(spec.get("positive") or "").strip()
    if not positive and base != "face_detailer":  # face_detailer는 얼굴 프롬프트만 있어도 된다
        raise WorkflowBuildError("긍정 프롬프트를 입력해야 해요.")
    negative = str(spec.get("negative") if spec.get("negative") is not None else DEFAULT_NEGATIVE)

    seed = _int(spec.get("seed"), "시드", default=0, minimum=0)
    steps = _int(spec.get("steps"), "스텝", default=28, minimum=1)
    cfg = _float(spec.get("cfg"), "CFG", default=6.0, minimum=0)
    sampler_name = str(spec.get("sampler_name") or "dpmpp_2m").strip()
    scheduler = str(spec.get("scheduler") or "karras").strip()
    filename_prefix = str(spec.get("filename_prefix") or "nightshift")

    workflow: dict = {}
    next_id = [1]

    def add(class_type: str, inputs: dict, title: str) -> str:
        node_id = str(next_id[0])
        next_id[0] += 1
        workflow[node_id] = {"inputs": inputs, "class_type": class_type, "_meta": {"title": title}}
        return node_id

    # VAE는 인코드/디코드/USDU 내부 어디서든 필요해서 로더와 함께 미리 만든다.
    model_src, clip_src, vae_src = loader(spec, add)

    # LoRA 체인 — 각 로더의 model/clip 출력을 다음 로더로 이어 붙인다.
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

    if base == "face_detailer":
        # 기존 이미지 1장 → 얼굴 보정 → 저장. 파일명은 img2img처럼 실행 시점에 템플릿이 넣는다.
        image_id = add("LoadImage", {"image": ""}, "input_image")
        result_image = _face_detailer(add, face_detailer, face_defaults, [image_id, 0], model_src, clip_src,
                                      vae_src, seed, positive, negative)
        add("SaveImage", {"filename_prefix": filename_prefix, "images": result_image}, "Save Image")
        return workflow

    # 프롬프트 — 긍정 노드 제목은 반드시 "main_prompt"를 포함해야 한다(위 모듈 설명 참고).
    positive_id = add("CLIPTextEncode", {"text": positive, "clip": clip_src}, "main_prompt")
    negative_id = add("CLIPTextEncode", {"text": negative, "clip": clip_src}, "negative_prompt")

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

    # ControlNet은 베이스·hires KSampler에만 건다. Face Detailer·USDU는 원래 조건을 쓴다(조각·타일엔 전체 포즈가 안 맞는다).
    sampler_positive, sampler_negative = [positive_id, 0], [negative_id, 0]
    if controlnet.get("enabled"):
        sampler_positive, sampler_negative = _openpose_controlnet(add, controlnet, sampler_positive,
                                                                  sampler_negative, vae_src)

    sampler_inputs = {
        "seed": seed,
        "steps": steps,
        "cfg": cfg,
        "sampler_name": sampler_name,
        "scheduler": scheduler,
        "denoise": denoise,
        "model": model_src,
        "positive": sampler_positive,
        "negative": sampler_negative,
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

    if face_detailer.get("enabled"):
        result_image = _face_detailer(add, face_detailer, face_defaults, result_image, model_src, clip_src,
                                      vae_src, seed, positive, negative)

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
