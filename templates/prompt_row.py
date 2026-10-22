"""CSV와 시드 배치의 행별 샘플러·모델 옵션을 워크플로우에 반영한다."""

import os


def apply(workflow, row, set_value):
    """프롬프트 값이 있을 때만 기존 워크플로우 입력을 덮어쓴다."""
    if not row:
        return
    samplers = [node for node in workflow.values() if isinstance(node, dict)
                and node.get("class_type") in ("KSampler", "KSamplerAdvanced")]
    if samplers:
        title = os.environ.get("SEED_NODE_TITLE", "KSampler").lower()
        sampler = next((node for node in samplers
                        if title in str(node.get("_meta", {}).get("title", "")).lower()), samplers[0])
        for field, convert in (("steps", int), ("cfg", float)):
            if row.get(field) not in (None, ""):
                set_value(workflow, sampler, field, convert(row[field]))
    if row.get("width") and row.get("height"):
        latents = [node for node in workflow.values() if isinstance(node, dict)
                   and node.get("class_type") == "EmptyLatentImage"]
        if latents:
            set_value(workflow, latents[0], "width", int(row["width"]))
            set_value(workflow, latents[0], "height", int(row["height"]))
    if row.get("checkpoint"):
        loaders = [node for node in workflow.values() if isinstance(node, dict)
                   and node.get("class_type") in ("CheckpointLoaderSimple", "CheckpointLoader", "unCLIPCheckpointLoader")]
        if not loaders:
            raise ValueError("체크포인트 로더가 없어요.")
        set_value(workflow, loaders[0], "ckpt_name", row["checkpoint"])
    if row.get("negative_prompt"):
        negatives = [node for node in workflow.values() if isinstance(node, dict)
                     and "negative_prompt" in str(node.get("_meta", {}).get("title", "")).lower()]
        if negatives:
            field = "text" if "text" in negatives[0].get("inputs", {}) else "value"
            set_value(workflow, negatives[0], field, row["negative_prompt"])
    if row.get("loras"):
        loaders = [node for node in workflow.values() if isinstance(node, dict)
                   and node.get("class_type") in ("LoraLoader", "LoraLoaderModelOnly")]
        entries = [entry.rsplit(":", 1) for entry in row["loras"].split(";")]
        if len(entries) > len(loaders):
            raise ValueError("LoRA 로더 수보다 --lora 옵션이 많아요.")
        for loader, (name, strength) in zip(loaders, entries):
            set_value(workflow, loader, "lora_name", name)
            set_value(workflow, loader, "strength_model", float(strength))
