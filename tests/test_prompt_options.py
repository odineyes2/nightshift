"""프롬프트 옵션 해석과 CSV·워크플로우 적용을 검사한다."""

import csv
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "templates"))

import prompt_options
import prompt_row
import csv_batch
import img2video_i2v_csv_batch
import img2video_flf2v_csv_batch


class PromptOptionsTests(unittest.TestCase):
    def test_seed_range_and_choices(self):
        clean, options, errors = prompt_options.parse_prompt_options("dress --seed 1..4")
        self.assertEqual((clean, options, errors), ("dress", {"seed_range": (1, 4)}, []))
        source = 'main_prompt\n"dress {red, blue} {silk, cotton} --seed 1..4"\n'
        result, _ = prompt_options.apply_csv_options(source.encode())
        rows = list(csv.DictReader(io.StringIO(result.decode())))
        self.assertEqual(len(rows), 16)
        self.assertEqual([row["seed"] for row in rows[:4]], ["1", "2", "3", "4"])
        self.assertEqual(rows[0]["main_prompt"], "dress red silk")
        self.assertEqual(rows[-1]["main_prompt"], "dress blue cotton")
        escaped, _ = prompt_options.apply_csv_options(b'main_prompt\n"dress \\{red, blue}"\n')
        self.assertEqual(next(csv.DictReader(io.StringIO(escaped.decode())))["main_prompt"], "dress {red, blue}")
        for prompt in ("cat --seed 4..1", "cat --seed 1..65", "cat --seed 1..4 --r 2"):
            with self.subTest(prompt=prompt):
                self.assertTrue(prompt_options.parse_prompt_options(prompt)[2])
        with self.assertRaisesRegex(ValueError, "64줄"):
            prompt_options.apply_csv_options(('main_prompt\n"' + "{a,b}" * 7 + '"\n').encode())

    def test_video_columns_and_workflow_injection(self):
        source = 'main_prompt,user_prompt\ncat,"walking --dur 5s --fps 24"\n'
        with self.assertRaisesRegex(ValueError, "영상 템플릿"):
            prompt_options.apply_csv_options(source.encode())
        result, _ = prompt_options.apply_csv_options(source.encode(), allow_video=True)
        row = next(csv.DictReader(io.StringIO(result.decode())))
        self.assertEqual((row["frames"], row["fps"], row["user_prompt"]), ("120", "24", "walking"))
        override, _ = prompt_options.apply_csv_options(
            b'main_prompt\ncat --dur 5s --fps 24 --frames 81\n', allow_video=True)
        self.assertEqual(next(csv.DictReader(io.StringIO(override.decode())))["frames"], "81")
        for module in (img2video_i2v_csv_batch, img2video_flf2v_csv_batch):
            with self.subTest(module=module.__name__):
                workflow = copy.deepcopy(module.load_workflow(module.VIDEO_WORKFLOW_PATH))
                prompt_row.apply_video(workflow, row, module.set_linked_value)
                length = next(node["inputs"]["length"] for node in workflow.values()
                              if node.get("class_type") in ("WanImageToVideo", "WanFirstLastFrameToVideo"))
                fps = next(node["inputs"]["fps"] for node in workflow.values() if node.get("class_type") == "CreateVideo")
                if isinstance(fps, list):
                    fps = workflow[str(fps[0])]["inputs"]["value"]
                self.assertEqual((length, fps), (120, 24))

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

    def test_second_stage_options_and_csv_columns(self):
        clean, opts, errors = prompt_options.parse_prompt_options(
            "cat --sampler dpmpp_2m --scheduler karras --denoise 0.65 --nodetail --upscale 2.5")
        self.assertEqual((clean, errors), ("cat", []))
        self.assertEqual(opts, {"sampler": "dpmpp_2m", "scheduler": "karras", "denoise": 0.65,
                                "detail": False, "upscale": 2.5})
        result, records = prompt_options.apply_csv_options(
            b"main_prompt,detail\ncat --sampler euler --scheduler normal --denoise 0 --nodetail --upscale 4,true\n")
        row = next(csv.DictReader(io.StringIO(result.decode())))
        self.assertEqual({key: row[key] for key in ("sampler", "scheduler", "denoise", "detail", "upscale")},
                         {"sampler": "euler", "scheduler": "normal", "denoise": "0.0", "detail": "false", "upscale": "4.0"})
        self.assertEqual(records[0]["detail"], False)
        for prompt in ("cat --denoise -0.1", "cat --denoise 1.1", "cat --denoise NaN",
                       "cat --upscale 0.9", "cat --upscale 4.1", "cat --detail value",
                       "cat --sampler two words", "cat --scheduler x/y"):
            with self.subTest(prompt=prompt):
                self.assertTrue(prompt_options.parse_prompt_options(prompt)[2])

    def test_second_stage_workflow_changes(self):
        workflow = {
            "1": {"class_type": "KSampler", "_meta": {"title": "KSampler"},
                  "inputs": {"sampler_name": "old", "scheduler": "old", "denoise": 1}},
            "2": {"class_type": "VAEDecode", "inputs": {}},
            "3": {"class_type": "FaceDetailer", "inputs": {"image": ["2", 0]}},
            "4": {"class_type": "ImageScaleBy", "inputs": {"image": ["3", 0], "scale_by": 1.5}},
        }
        def set_value(workflow, node, field, value):
            node["inputs"][field] = value
        prompt_row.apply(workflow, {"sampler": "euler", "scheduler": "karras", "denoise": "0.4",
                                    "detail": "false", "upscale": "2"}, set_value)
        self.assertEqual([workflow["1"]["inputs"][key] for key in ("sampler_name", "scheduler", "denoise")],
                         ["euler", "karras", 0.4])
        self.assertNotIn("3", workflow)
        self.assertEqual(workflow["4"]["inputs"], {"image": ["2", 0], "scale_by": 2.0})
        with self.assertRaisesRegex(ValueError, "Face Detailer"):
            prompt_options.validate_workflow_options(workflow, {"detail": True})
        with self.assertRaisesRegex(ValueError, "업스케일"):
            prompt_options.validate_workflow_options({"1": workflow["1"]}, {"upscale": 2})

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


class PromptOptionsHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        for key in ("DATA", "OUTPUT", "ASSETS"):
            os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(cls.temp.name) / key.lower())
        os.environ.setdefault("NIGHTSHIFT_ADMIN_USER", "admin")
        os.environ.setdefault("NIGHTSHIFT_ADMIN_PASSWORD", "Test-Passw0rd-xyz!")
        os.chdir(ROOT / "server")
        import app
        import db
        user = app.auth.register("prompt-http", "prompt-http@example.test", "Test-Passw0rd-xyz!")
        with db.connect() as conn:
            conn.execute("UPDATE users SET status='active' WHERE id=?", (user["id"],))
        cls.app = app

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_parse_route_requires_login_and_returns_parser_result(self):
        from fastapi.testclient import TestClient
        headers = {"X-Requested-With": "nightshift"}
        client = TestClient(self.app.app)
        try:
            # 스케줄러가 뜨지 않도록 lifespan 관리 컨텍스트는 열지 않는다.
            self.assertEqual(client.post("/api/prompt-options/parse", json={"text": "cat"}, headers=headers).status_code, 401)
            response = client.post("/api/auth/login", json={"username": "prompt-http", "password": "Test-Passw0rd-xyz!"}, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            response = client.post("/api/prompt-options/parse", json={"text": "1girl --ar 2:3 --res 1024 --seed 7"}, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {"clean_text": "1girl", "options": {"ar": [2, 3], "res": 1024, "seed": 7}, "errors": [], "resolution": [832, 1216]})
            response = client.post("/api/prompt-options/parse", json={"text": "cat --sed 1"}, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["errors"])
            self.assertEqual(client.post("/api/prompt-options/parse", json={"text": 7}, headers=headers).status_code, 400)
        finally:
            client.close()

    def test_jobs_route_stores_second_stage_csv_and_rejects_missing_nodes(self):
        from fastapi.testclient import TestClient
        headers = {"X-Requested-With": "nightshift"}
        workflow = {"1": {"class_type": "KSampler", "_meta": {"title": "KSampler"},
                          "inputs": {"seed": 0, "sampler_name": "euler", "scheduler": "normal", "denoise": 1}}}
        client = TestClient(self.app.app)
        try:
            response = client.post("/api/auth/login", json={"username": "prompt-http", "password": "Test-Passw0rd-xyz!"}, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            body = {"template_id": "csv_batch", "workflow": workflow,
                    "csv": "main_prompt\ncat --sampler euler --scheduler karras --denoise 0.5 --nodetail\n"}
            response = client.post("/api/jobs", json=body, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            job = response.json()
            row = next(csv.DictReader(io.StringIO((self.app.JOBS_DIR / job["csv_filename"]).read_text(encoding="utf-8"))))
            self.assertEqual([row[key] for key in ("sampler", "scheduler", "denoise", "detail")],
                             ["euler", "karras", "0.5", "false"])
            for prompt in ("cat --detail", "cat --upscale 2"):
                with self.subTest(prompt=prompt):
                    body["csv"] = f"main_prompt\n{prompt}\n"
                    self.assertEqual(client.post("/api/jobs", json=body, headers=headers).status_code, 400)
            single = {"template_id": "seed_batch", "workflow": workflow,
                      "options": {"main_prompt": "cat --sampler heun --scheduler karras --denoise 0.25 --nodetail"}}
            response = client.post("/api/jobs", json=single, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual({key: response.json()["options"][key] for key in
                              ("sampler", "scheduler", "denoise", "detail")},
                             {"sampler": "heun", "scheduler": "karras", "denoise": "0.25", "detail": "False"})
        finally:
            client.close()

    def test_jobs_route_expands_seed_and_rejects_video_options_on_image(self):
        from fastapi.testclient import TestClient
        headers = {"X-Requested-With": "nightshift"}
        workflow = {"1": {"class_type": "KSampler", "inputs": {"seed": 0}}}
        client = TestClient(self.app.app)
        try:
            self.assertEqual(client.post("/api/auth/login", json={"username": "prompt-http",
                              "password": "Test-Passw0rd-xyz!"}, headers=headers).status_code, 200)
            body = {"template_id": "csv_batch", "workflow": workflow,
                    "csv": 'main_prompt\n"cat {red, blue} --seed 1..4"\n'}
            response = client.post("/api/jobs", json=body, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            rows = list(csv.DictReader(io.StringIO((self.app.JOBS_DIR / response.json()["csv_filename"]).read_text(encoding="utf-8"))))
            self.assertEqual(len(rows), 8)
            self.assertEqual([row["seed"] for row in rows[:4]], ["1", "2", "3", "4"])
            body["csv"] = "main_prompt\ncat --frames 81\n"
            self.assertEqual(client.post("/api/jobs", json=body, headers=headers).status_code, 400)
            body = {"template_id": "img2video_i2v_csv_batch", "workflow": workflow,
                    "csv": 'main_prompt,user_prompt\ncat,"walking --dur 5s --fps 24"\n'}
            response = client.post("/api/jobs", json=body, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            row = next(csv.DictReader(io.StringIO((self.app.JOBS_DIR / response.json()["csv_filename"]).read_text(encoding="utf-8"))))
            self.assertEqual((row["frames"], row["fps"]), ("120", "24"))
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
