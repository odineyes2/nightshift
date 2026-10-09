"""CSV 템플릿 8종의 quality_prompt 합치기 검사(NS-65-1).

실행: python tests/test_csv_quality_merge.py
"""
import contextlib
import copy
import io

from test_csv_trigger_merge import ROOT, TEMPLATES, load, text, workflow_builder


def apply(mod, built, row):
    wf = copy.deepcopy(built)
    merged = mod.merge_trigger_into_main(wf, row)
    merged = mod.merge_quality_into_main(wf, merged)
    mod.apply_prompts(wf, merged)
    return wf, merged


def main():
    built = workflow_builder.build_workflow(
        {"checkpoint": "a.safetensors", "positive": "built", "negative": "neg"})
    for name in TEMPLATES:
        mod = load(name)
        src = (ROOT / "templates" / f"{name}.py").read_text(encoding="utf-8")
        assert ("row = merge_trigger_into_main(workflow, row)\n"
                "    row = merge_quality_into_main(workflow, row)\n"
                "    apply_prompts(workflow, row)") in src, name
        row = {"main_prompt": "a cat", "quality_prompt": "masterpiece"}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            wf, merged = apply(mod, built, row)
            apply(mod, built, row)
        assert text(wf, "main_prompt") == "a cat, masterpiece", name
        assert merged["quality_prompt"] == "" and merged is not row, name
        assert row == {"main_prompt": "a cat", "quality_prompt": "masterpiece"}, name
        assert stderr.getvalue().count("quality_prompt") == 1, (name, stderr.getvalue())

        for main_text, quality, expected in (
            ("a cat, masterpiece", "MASTERPIECE", "a cat, masterpiece"),
            ("a cat, , \t\n", " masterpiece ", "a cat, masterpiece"),
            ("", "masterpiece", "built, masterpiece"),
        ):
            wf, _ = apply(mod, built, {"main_prompt": main_text, "quality_prompt": quality})
            assert text(wf, "main_prompt") == expected, name

        empty = copy.deepcopy(built)
        next(n for n in empty.values() if n["_meta"]["title"] == "main_prompt")["inputs"]["text"] = ""
        wf, _ = apply(mod, empty, {"quality_prompt": "masterpiece"})
        assert text(wf, "main_prompt") == "masterpiece", name
        wf, merged = apply(mod, built, {"prompt": "a dog", "main_prompt": "",
                                      "quality_prompt": "masterpiece"})
        assert merged["prompt"] == "a dog, masterpiece" and not merged["main_prompt"], name
        assert text(wf, "main_prompt") == "a dog, masterpiece", name
        wf, _ = apply(mod, built, dict(row, trigger_prompt="trigger"))
        assert text(wf, "main_prompt") == "trigger, a cat, masterpiece", name

        for class_type, field in (("CLIPTextEncode", "text"), ("PrimitiveString", "value")):
            dedicated = copy.deepcopy(built)
            dedicated["900"] = {"class_type": class_type, "inputs": {field: ""},
                                "_meta": {"title": "quality_prompt"}}
            assert mod.merge_quality_into_main(dedicated, row) is row, name
            wf, _ = apply(mod, dedicated, row)
            assert wf["900"]["inputs"][field] == "masterpiece", name
            assert text(wf, "main_prompt") == "a cat", name
        for row in ({"main_prompt": "x"}, {"main_prompt": "x", "quality_prompt": " \t"}):
            assert mod.merge_quality_into_main(built, row) is row, name
    print("ok")


if __name__ == "__main__":
    main()
