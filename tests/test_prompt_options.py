"""프롬프트 옵션 해석과 CSV·워크플로우 적용을 검사한다."""

import csv
import io
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "templates"))

import prompt_options
import prompt_row
import csv_batch


class PromptOptionsTests(unittest.TestCase):
    def test_help_specs_match_parser_names_and_aliases(self):
        self.assertEqual({item["name"] for item in prompt_options.OPTION_SPECS}, prompt_options._KNOWN)
        self.assertEqual({alias: item["name"] for item in prompt_options.OPTION_SPECS
                          for alias in item["aliases"]}, prompt_options._ALIASES)

    def test_parse_and_resolution(self):
        clean, opts, errors = prompt_options.parse_prompt_options(
            r"portrait \--ar literal --ar 2:3 --res 1024 --seed 1 --no bad hands, text "
            "--repeat 3 --lora face.safetensors:0.7 --lora style.safetensors:1 "
            "--model base.safetensors --steps 24 --cfg 6.5")
        self.assertEqual(errors, [])
        self.assertEqual(clean, "portrait --ar literal")
        self.assertEqual(prompt_options.resolution(opts), (832, 1216))
        self.assertEqual(opts["loras"], [("face.safetensors", 0.7), ("style.safetensors", 1.0)])
        self.assertEqual(prompt_options.resolution({"ar": (2, 3), "res": (768, 1024)}), (768, 1024))

    def test_invalid_options(self):
        for prompt in ("cat --sed 1", "cat --ar 0:2", "cat --repeat 0", "cat --cfg NaN", "cat --seed abc"):
            with self.subTest(prompt=prompt):
                self.assertTrue(prompt_options.parse_prompt_options(prompt)[2])

    def test_aliases_and_csv_repeat(self):
        clean, opts, errors = prompt_options.parse_prompt_options(
            "cat --aspect 1:1 --ckpt model.safetensors --r 3")
        self.assertEqual((clean, errors), ("cat", []))
        self.assertEqual((opts["ar"], opts["model"], opts["repeat"]), ((1, 1), "model.safetensors", 3))
        result, records = prompt_options.apply_csv_options(
            b"main_prompt,seed\ncat --seed 7 --repeat 3,2\n", expand_repeat=True)
        rows = list(csv.DictReader(io.StringIO(result.decode())))
        self.assertEqual([row["seed"] for row in rows], ["7", "8", "9"])
        self.assertEqual(records[0]["seeds"], 3)

    def test_csv_precedence_and_negative_append(self):
        original = "main_prompt,negative_prompt,seed,width,height\ncat --seed 3 --res 1024 --ar 2:3 --no blur,noise,9,512,512\n"
        # 쉼표가 있는 옵션 값은 CSV에서 인용한다.
        original = 'main_prompt,negative_prompt,seed,width,height\n"cat --seed 3 --res 1024 --ar 2:3 --no blur, noise",existing,9,512,512\n'
        result, records = prompt_options.apply_csv_options(original.encode())
        row = next(csv.DictReader(io.StringIO(result.decode())))
        self.assertEqual(row["main_prompt"], "cat")
        self.assertEqual(row["negative_prompt"], "existing, blur, noise")
        self.assertEqual((row["seed"], row["width"], row["height"]), ("3", "832", "1216"))
        self.assertEqual(records[0]["seed"], 3)
        self.assertEqual(prompt_options.apply_csv_options(b"main_prompt\ncat\n"), (b"main_prompt\ncat\n", []))
        escaped, _ = prompt_options.apply_csv_options(b"main_prompt\ncat \\--seed 1\n")
        self.assertEqual(next(csv.DictReader(io.StringIO(escaped.decode())))["main_prompt"], "cat --seed 1")

    def test_workflow_injection(self):
        workflow = {
            "1": {"class_type": "KSampler", "inputs": {"steps": 10, "cfg": 2}},
            "2": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "old"}},
            "3": {"class_type": "LoraLoader", "inputs": {"lora_name": "old", "strength_model": 1}},
        }
        def set_value(workflow, node, field, value):
            node["inputs"][field] = value
        prompt_row.apply(workflow, {"steps": "25", "cfg": "7.5", "checkpoint": "new.safetensors",
                                    "loras": "face.safetensors:0.8"}, set_value)
        self.assertEqual(workflow["1"]["inputs"], {"steps": 25, "cfg": 7.5})
        self.assertEqual(workflow["2"]["inputs"]["ckpt_name"], "new.safetensors")
        self.assertEqual(workflow["3"]["inputs"]["strength_model"], 0.8)
        with self.assertRaises(ValueError):
            prompt_row.apply(workflow, {"loras": "a:1;b:1"}, set_value)

    def test_csv_runner_submits_overridden_workflow(self):
        workflow = {
            "1": {"class_type": "KSampler", "_meta": {"title": "KSampler"}, "inputs": {"seed": 0, "steps": 10, "cfg": 2}},
            "2": {"class_type": "EmptyLatentImage", "_meta": {"title": "latent"}, "inputs": {"width": 512, "height": 512}},
            "3": {"class_type": "CheckpointLoaderSimple", "_meta": {"title": "model"}, "inputs": {"ckpt_name": "old"}},
            "4": {"class_type": "LoraLoader", "_meta": {"title": "lora"}, "inputs": {"lora_name": "old", "strength_model": 1}},
            "5": {"class_type": "CLIPTextEncode", "_meta": {"title": "main_prompt"}, "inputs": {"text": "old"}},
        }
        submitted = []
        old_queue, old_wait = csv_batch.queue_prompt, csv_batch.wait_for_completion
        try:
            csv_batch.queue_prompt = lambda url, graph: submitted.append(graph) or "fake-id"
            csv_batch.wait_for_completion = lambda url, prompt_id: None
            csv_batch.run_once(workflow, "http://fake-comfy", {
                "main_prompt": "cat", "seed": "5", "width": "832", "height": "1216",
                "steps": "25", "cfg": "7.5", "checkpoint": "new.safetensors",
                "loras": "face.safetensors:0.8",
            }, "example", 5, 1)
        finally:
            csv_batch.queue_prompt, csv_batch.wait_for_completion = old_queue, old_wait
        graph = submitted[0]
        self.assertEqual(graph["1"]["inputs"], {"seed": 5, "steps": 25, "cfg": 7.5})
        self.assertEqual((graph["2"]["inputs"]["width"], graph["2"]["inputs"]["height"]), (832, 1216))
        self.assertEqual(graph["3"]["inputs"]["ckpt_name"], "new.safetensors")
        self.assertEqual(graph["4"]["inputs"]["lora_name"], "face.safetensors")


if __name__ == "__main__":
    unittest.main()
