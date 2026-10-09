"""CSV 템플릿의 trigger_prompt 합치기 검사(NS-63-1) — trigger_prompt 노드가 없는 마법사 워크플로우면
행의 trigger_prompt가 main_prompt 앞에 붙고, 전용 노드가 있으면 지금처럼 그 노드에만 들어간다.

실행: python tests/test_csv_trigger_merge.py
"""
import copy
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
import workflow_builder  # noqa: E402

TEMPLATES = ("csv_batch", "pose_csv_batch", "depth_csv_batch", "lineart_csv_batch", "input_image_csv_batch",
             "ipadapter_csv_batch", "img2video_i2v_csv_batch", "img2video_flf2v_csv_batch")


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "templates" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def text(wf, title):
    return next(n for n in wf.values() if n["_meta"]["title"] == title)["inputs"]["text"]


def apply(mod, wf, row):
    wf = copy.deepcopy(wf)
    merged = mod.merge_trigger_into_main(wf, row)
    mod.apply_prompts(wf, merged)
    return wf, merged


def main():
    built = workflow_builder.build_workflow({"checkpoint": "a.safetensors", "positive": "built", "negative": "neg"})
    with_trigger = copy.deepcopy(built)
    with_trigger["900"] = {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["1", 1]},
                           "_meta": {"title": "trigger_prompt"}}

    for name in TEMPLATES:
        mod = load(name)
        src = (ROOT / "templates" / f"{name}.py").read_text(encoding="utf-8")
        assert "row = merge_trigger_into_main(workflow, row)\n    row = merge_quality_into_main(workflow, row)\n    apply_prompts(workflow, row)" in src, name

        row = {"trigger_prompt": "ohwx style", "main_prompt": "a cat"}
        wf, merged = apply(mod, built, row)
        assert text(wf, "main_prompt") == "ohwx style, a cat", name
        assert merged["main_prompt"] == "ohwx style, a cat" and merged["trigger_prompt"] == "", name
        assert row == {"trigger_prompt": "ohwx style", "main_prompt": "a cat"}, name  # 원본 행은 그대로

        # 이미 들어 있으면(대소문자 무시) 다시 붙이지 않는다.
        wf, _ = apply(mod, built, {"trigger_prompt": "OHWX style", "main_prompt": "ohwx style, a cat"})
        assert text(wf, "main_prompt") == "ohwx style, a cat", name

        # main이 비면 노드에 있던 텍스트 앞에 붙는다.
        wf, _ = apply(mod, built, {"trigger_prompt": "ohwx style", "main_prompt": ""})
        assert text(wf, "main_prompt") == "ohwx style, built", name

        # prompt 열만 쓰는 옛 CSV도 합친다.
        _, merged = apply(mod, built, {"trigger_prompt": "ohwx", "prompt": "a dog"})
        assert merged["prompt"] == "ohwx, a dog" and not merged.get("main_prompt"), name

        # trigger_prompt 전용 노드가 있으면 지금처럼 그 노드에만 들어간다.
        wf, merged = apply(mod, with_trigger, row)
        assert merged is row, name
        assert text(wf, "trigger_prompt") == "ohwx style" and text(wf, "main_prompt") == "a cat", name

        # 트리거가 비면 손대지 않는다.
        assert mod.merge_trigger_into_main(built, {"main_prompt": "x"}) == {"main_prompt": "x"}, name
    print("ok")


if __name__ == "__main__":
    main()
