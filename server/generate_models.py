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
