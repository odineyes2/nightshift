"""불러온 워크플로우의 모델·텍스트 인코더·Hires 배율·LoRA만 재조립 없이 제자리에서 고친다(NS-122).

샘플러·Face Detailer 수치처럼 재조립하면 기본값으로 돌아가는 값을 남기려고 JSON을 직접 고친다.
어느 노드를 고칠지 확실하지 않은 구조(로더가 여럿, 일자가 아닌 LoRA 체인)는 WorkflowBuildError로 거절한다.
"""
import copy
import math
import re

from workflow_builder import WorkflowBuildError, _float

MODEL_LOADERS = {"CheckpointLoaderSimple": "ckpt_name", "UNETLoader": "unet_name"}
LORA_CLASSES = ("LoraLoader", "LoraLoaderModelOnly")
VALUE_KEYS = ("checkpoint", "clip", "hires_scale", "loras", "model_only")
# 모델 파일명은 하위 폴더(a/b.safetensors)·공백이 있을 수 있다(ControlNet 파일명과 같은 규칙).
FILENAME_PATTERN = r"[^\\:*?\"<>|\x00-\x1f]{1,200}"
NOT_STRAIGHT = "LoRA 체인이 일자가 아니라 고칠 수 없어요"


def _find(workflow: dict, classes) -> list:
    return [(node_id, node) for node_id, node in workflow.items()
            if isinstance(node, dict) and node.get("class_type") in classes]


def _single(workflow: dict, classes, label: str) -> tuple:
    found = _find(workflow, classes)
    if not found:
        raise WorkflowBuildError(f"워크플로우에 {label} 노드가 없어요.")
    if len(found) > 1:
        raise WorkflowBuildError(f"{label} 노드가 {len(found)}개라 어느 것을 고칠지 알 수 없어요.")
    return found[0]


def _filename(value, label: str) -> str:
    name = value.strip() if isinstance(value, str) else ""
    if not re.fullmatch(FILENAME_PATTERN, name) or ".." in name:
        raise WorkflowBuildError(f"{label} 값 '{value}'이(가) 올바르지 않아요.")
    return name


def _number(value, name: str, **limits) -> float:
    if isinstance(value, bool):
        raise WorkflowBuildError(f"{name} 값 '{value}'은(는) 숫자가 아니에요.")
    result = _float(value, name, **limits)
    if not math.isfinite(result):
        raise WorkflowBuildError(f"{name} 값 '{value}'은(는) 숫자가 아니에요.")
    return result


def _is_link(value) -> bool:
    return isinstance(value, list) and len(value) == 2 and type(value[1]) is int


def _links(workflow: dict):
    """워크플로우의 모든 링크를 (노드 id, 입력 dict, 입력 이름)으로 돌려준다."""
    for node_id, node in workflow.items():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if isinstance(inputs, dict):
            for key, value in inputs.items():
                if _is_link(value):
                    yield node_id, inputs, key


def _lora_chain(workflow: dict) -> tuple[list, list, list]:
    """LoRA 노드를 model 입력으로 이어 (앞→끝 순서의 id, 뿌리 model 링크, 뿌리 clip 링크)를 돌려준다.
    뿌리 clip은 LoraLoaderModelOnly 체인이면 None이고, 일자 체인이 아니면 거절한다."""
    loras = dict(_find(workflow, LORA_CLASSES))
    if not loras:
        return [], None, None
    if len({node["class_type"] for node in loras.values()}) > 1:
        raise WorkflowBuildError(f"{NOT_STRAIGHT}(LoRA 노드 종류가 섞여 있어요).")
    heads, next_of = [], {}
    for node_id, node in loras.items():
        link = (node.get("inputs") or {}).get("model")
        if not _is_link(link):
            raise WorkflowBuildError(f"{NOT_STRAIGHT}(LoRA 노드 {node_id}의 model 입력이 연결돼 있지 않아요).")
        upstream = str(link[0])
        if upstream not in loras:
            heads.append(node_id)
        elif upstream in next_of or link[1] != 0:
            raise WorkflowBuildError(f"{NOT_STRAIGHT}(LoRA 노드 {upstream}에서 갈라져요).")
        else:
            next_of[upstream] = node_id
    if len(heads) != 1:
        raise WorkflowBuildError(f"{NOT_STRAIGHT}(체인 시작이 {len(heads)}개예요).")
    chain = heads
    while chain[-1] in next_of:
        chain.append(next_of[chain[-1]])
    if len(chain) != len(loras):
        raise WorkflowBuildError(f"{NOT_STRAIGHT}(체인 밖 LoRA가 있어요).")

    model_root = workflow[chain[0]]["inputs"]["model"]
    clip_root = None
    if workflow[chain[0]]["class_type"] == "LoraLoader":
        clip_root = workflow[chain[0]]["inputs"].get("clip")
        if not _is_link(clip_root) or str(clip_root[0]) in loras:
            raise WorkflowBuildError(f"{NOT_STRAIGHT}(LoRA 노드 {chain[0]}의 clip 입력이 올바르지 않아요).")
        for previous, node_id in zip(chain, chain[1:]):
            clip = workflow[node_id]["inputs"].get("clip")
            if not _is_link(clip) or (str(clip[0]), clip[1]) != (previous, 1):
                raise WorkflowBuildError(f"{NOT_STRAIGHT}(LoRA 노드 {node_id}의 clip 입력이 체인과 달라요).")
    for node_id, inputs, key in _links(workflow):
        source = str(inputs[key][0])
        if source in loras and source != chain[-1] and node_id not in loras:
            raise WorkflowBuildError(f"{NOT_STRAIGHT}(노드 {node_id}이(가) 체인 중간의 LoRA를 써요).")
    return chain, model_root, clip_root


def _clean_loras(raw) -> list:
    if not isinstance(raw, list):
        raise WorkflowBuildError("loras는 목록이어야 해요.")
    out = []
    for index, lora in enumerate(raw, start=1):
        if not isinstance(lora, dict):
            raise WorkflowBuildError("각 LoRA 항목은 객체여야 해요.")
        if not str(lora.get("name") or "").strip():
            continue  # 비워둔 줄은 빌더처럼 건너뛴다
        out.append((_filename(lora["name"], f"LoRA {index} 이름"),
                    _number(lora.get("strength"), f"LoRA {index} 강도", default=1.0)))
    return out


def _replace_loras(workflow: dict, loras: list, model_only: bool):
    chain, model_root, clip_root = _lora_chain(workflow)
    if not chain and not loras:
        return
    if chain:
        class_type = workflow[chain[0]]["class_type"]
        old_model, old_clip = [chain[-1], 0], [chain[-1], 1]
    else:
        class_type = "LoraLoaderModelOnly" if model_only else "LoraLoader"
        loader_id, loader = _single(workflow, MODEL_LOADERS, "모델 로더")
        model_root = [loader_id, 0]
        if class_type == "LoraLoader":
            clip_root = ([loader_id, 1] if loader["class_type"] == "CheckpointLoaderSimple"
                         else [_single(workflow, ("CLIPLoader",), "CLIP 로더")[0], 0])
        old_model, old_clip = model_root, clip_root

    # 같은 이름이 남으면 strength_clip을 잇는다. 이름이 링크인 노드는 이을 값이 없다.
    kept_clip = {}
    for node_id in chain:
        inputs = workflow[node_id]["inputs"]
        if isinstance(inputs.get("lora_name"), str) and "strength_clip" in inputs:
            kept_clip.setdefault(inputs["lora_name"], []).append(inputs["strength_clip"])
    first_id = max((int(node_id) for node_id in workflow if str(node_id).isdecimal()), default=0) + 1
    for node_id in chain:
        del workflow[node_id]

    new_ids = [str(first_id + offset) for offset in range(len(loras))]
    new_model = [new_ids[-1], 0] if new_ids else model_root
    new_clip = [new_ids[-1], 1] if new_ids else clip_root
    moves = {(str(old_model[0]), old_model[1]): new_model}
    if class_type == "LoraLoader":
        moves[(str(old_clip[0]), old_clip[1])] = new_clip
    for _, inputs, key in _links(workflow):
        target = moves.get((str(inputs[key][0]), inputs[key][1]))
        if target:
            inputs[key] = list(target)

    model_src, clip_src = model_root, clip_root
    for index, (node_id, (name, strength)) in enumerate(zip(new_ids, loras), start=1):
        inputs = {"lora_name": name, "strength_model": strength, "model": list(model_src)}
        if class_type == "LoraLoader":
            kept = kept_clip.get(name)
            inputs.update({"strength_clip": kept.pop(0) if kept else strength, "clip": list(clip_src)})
            clip_src = [node_id, 1]
        workflow[node_id] = {"inputs": inputs, "class_type": class_type, "_meta": {"title": f"LoRA {index}"}}
        model_src = [node_id, 0]


def apply_model_values(workflow: dict, values: dict) -> dict:
    """values에 있는 항목만 고친 새 워크플로우를 돌려준다. 고칠 수 없으면 WorkflowBuildError."""
    if not isinstance(values, dict):
        raise WorkflowBuildError("values는 객체여야 해요.")
    unknown = [key for key in values if key not in VALUE_KEYS]
    if unknown:
        raise WorkflowBuildError(f"알 수 없는 항목이에요: {unknown[0]}")
    model_only = values.get("model_only", False)
    if not isinstance(model_only, bool):
        raise WorkflowBuildError("model_only는 true/false여야 해요.")

    result = copy.deepcopy(workflow)
    if "checkpoint" in values:
        _, node = _single(result, MODEL_LOADERS, "모델 로더")
        node.setdefault("inputs", {})[MODEL_LOADERS[node["class_type"]]] = _filename(values["checkpoint"], "모델")
    if "clip" in values:
        _, node = _single(result, ("CLIPLoader",), "CLIP 로더")
        node.setdefault("inputs", {})["clip_name"] = _filename(values["clip"], "텍스트 인코더")
    if "hires_scale" in values:
        _, node = _single(result, ("LatentUpscaleBy",), "Hires 업스케일")
        scale = _number(values["hires_scale"], "hires 배율", minimum=1.0, maximum=4.0)
        node.setdefault("inputs", {})["scale_by"] = scale
    if "loras" in values:
        _replace_loras(result, _clean_loras(values["loras"]), model_only)
    return result
