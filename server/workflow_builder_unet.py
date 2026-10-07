"""UNet+CLIP+VAE 셋으로 된 이미지 모델(예: Anima)용 범용 워크플로우 빌더(architecture "unet").

로더 부분만 다르고 나머지(LoRA 체인·txt2img/img2img·hires_fix·usdu·SaveImage와 노드 제목 규칙)는
workflow_builder.py의 체크포인트 빌더를 그대로 쓴다:
    UNETLoader(spec["checkpoint"]) + CLIPLoader(spec["clip"], spec["clip_type"]) + VAELoader(spec["vae"])
krea.2·MiniMax-H3처럼 그래프가 다른 모델은 각자의 전용 빌더를 쓴다.
"""
from workflow_builder import WorkflowBuildError, build_workflow as _build_workflow

DEFAULT_CLIP_TYPE = "stable_diffusion"  # 첨부 Anima 워크플로우 값 — 다른 UNet 계열은 맞지 않을 수 있다
# 스펙에 값이 없을 때 쓰는 샘플링 기본값(첨부 Anima 워크플로우 값).
DEFAULTS = {"steps": 30, "cfg": 4.0, "sampler_name": "er_sde", "scheduler": "simple"}
# UNet+CLIP+VAE형 FaceDetailer 수치 기본값(NS-34 본문의 Anima 값).
FACE_DETAILER_DEFAULTS = {"steps": 30, "cfg": 4.0, "sampler_name": "er_sde", "scheduler": "simple", "denoise": 0.5}


def _unet_loader(spec: dict, add) -> tuple[list, list, list]:
    names = {}
    for field, label in (("checkpoint", "디퓨전 모델(UNet)"), ("clip", "텍스트 인코더(CLIP)"), ("vae", "VAE")):
        names[field] = str(spec.get(field) or "").strip()
        if not names[field]:
            raise WorkflowBuildError(f"{label}을(를) 골라야 해요.")
    unet_id = add("UNETLoader", {"unet_name": names["checkpoint"], "weight_dtype": "default"}, "확산 모델 로드")
    clip_id = add("CLIPLoader", {"clip_name": names["clip"],
                                 "type": str(spec.get("clip_type") or DEFAULT_CLIP_TYPE).strip(),
                                 "device": "default"}, "CLIP 로드")
    vae_id = add("VAELoader", {"vae_name": names["vae"]}, "VAE 로드")
    return [unet_id, 0], [clip_id, 0], [vae_id, 0]


def build_workflow(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise WorkflowBuildError("스펙이 JSON 객체가 아니에요.")
    if isinstance(spec.get("controlnet"), dict) and spec["controlnet"].get("enabled"):
        # OpenPose ControlNet은 체크포인트 하나로 된 SDXL 계열 전용이다(NS-41).
        raise WorkflowBuildError("OpenPose ControlNet은 체크포인트형 베이스 모델에서만 쓸 수 있어요.")
    spec = {**spec, **{k: v for k, v in DEFAULTS.items() if spec.get(k) in (None, "")}}
    return _build_workflow(spec, loader=_unet_loader, face_defaults=FACE_DETAILER_DEFAULTS)
