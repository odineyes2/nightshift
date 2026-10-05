"""NS-34-4 Face Detailer 빌더 — 후처리와 base 유형, 체크포인트형·UNet+CLIP+VAE형 두 로더."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import workflow_builder as W
import workflow_builder_unet as U

CKPT = {"checkpoint": "sdxl.safetensors", "positive": "(prompt)", "negative": "(neg)",
        "loras": [{"name": "a.safetensors"}]}
UNET = {**CKPT, "checkpoint": "anima.safetensors", "clip": "qwen.safetensors", "vae": "qwen_vae.safetensors"}
BUILDERS = (("checkpoint", W.build_workflow, CKPT, W.FACE_DETAILER_DEFAULTS),
            ("unet", U.build_workflow, UNET, U.FACE_DETAILER_DEFAULTS))
# 실행 템플릿이 제목 부분일치로 찾는 말 — Face Detailer 쪽 노드 제목에 들어가면 안 된다.
INJECT_WORDS = ("main_prompt", "negative_prompt", "KSampler", "Save", "latent")


def by_class(wf, cls):
    return [(i, n) for i, n in wf.items() if n["class_type"] == cls]


def one(wf, cls):
    found = by_class(wf, cls)
    assert len(found) == 1, (cls, found)
    return found[0]


class FaceDetailerBuilderTests(unittest.TestCase):
    def check_fd(self, wf, defaults, image_src):
        fd_id, fd = one(wf, "FaceDetailer")
        inp = fd["inputs"]
        self.assertEqual(inp["image"], image_src)
        det_id, det = one(wf, "UltralyticsDetectorProvider")
        self.assertEqual(det["inputs"]["model_name"], "bbox/face_yolov8m.pt")
        self.assertEqual(inp["bbox_detector"], [det_id, 0])
        lora_id, _ = one(wf, "LoraLoader")  # LoRA가 걸린 model/clip을 쓴다
        self.assertEqual(inp["model"], [lora_id, 0])
        self.assertEqual(inp["clip"], [lora_id, 1])
        for key in ("steps", "cfg", "sampler_name", "scheduler", "denoise"):
            self.assertEqual(inp[key], defaults[key], key)
        for key, value in W.FACE_DETAILER_COMMON.items():
            self.assertEqual(inp[key], value, key)
        self.assertTrue(inp["noise_mask"] and inp["force_inpaint"])
        for ref, title in ((inp["positive"], "Face Detailer 긍정"), (inp["negative"], "Face Detailer 부정")):
            self.assertEqual(wf[ref[0]]["_meta"]["title"], title)
            self.assertEqual(wf[ref[0]]["inputs"]["clip"], [lora_id, 1])
        for node_id in (fd_id, det_id, inp["positive"][0], inp["negative"][0]):
            title = wf[node_id]["_meta"]["title"]
            for word in INJECT_WORDS:
                self.assertNotIn(word.lower(), title.lower(), title)
        _, save = one(wf, "SaveImage")
        return fd_id, save

    def test_post_order_hires_fd_usdu(self):
        for name, build, spec, defaults in BUILDERS:
            with self.subTest(name):
                wf = build({**spec, "hires_fix": {"enabled": True}, "usdu": {"enabled": True},
                            "face_detailer": {"enabled": True}})
                decode_id, decode = one(wf, "VAEDecode")
                hires_id = [i for i, n in by_class(wf, "KSampler") if "hires" in n["_meta"]["title"]][0]
                self.assertEqual(decode["inputs"]["samples"], [hires_id, 0])
                fd_id, save = self.check_fd(wf, defaults, [decode_id, 0])
                scale_id, scale = one(wf, "ImageScaleBy")
                self.assertEqual(scale["inputs"]["image"], [fd_id, 0])
                usdu_id, _ = one(wf, "UltimateSDUpscaleNoUpscale")
                self.assertEqual(save["inputs"]["images"], [usdu_id, 0])

    def test_post_without_usdu_saves_fd(self):
        for name, build, spec, defaults in BUILDERS:
            with self.subTest(name):
                wf = build({**spec, "face_detailer": {"enabled": True, "steps": 12}})
                fd_id, save = self.check_fd(wf, {**defaults, "steps": 12}, [one(wf, "VAEDecode")[0], 0])
                self.assertEqual(save["inputs"]["images"], [fd_id, 0])

    def test_face_prompts_fallback_to_main(self):
        wf = W.build_workflow({**CKPT, "face_detailer": {"enabled": True}})
        texts = {n["_meta"]["title"]: n["inputs"]["text"] for _, n in by_class(wf, "CLIPTextEncode")}
        self.assertEqual(texts["Face Detailer 긍정"], "(prompt)")
        self.assertEqual(texts["Face Detailer 부정"], "(neg)")
        wf = W.build_workflow({**CKPT, "face_detailer": {"enabled": True, "positive": "face", "negative": "bad"}})
        texts = {n["_meta"]["title"]: n["inputs"]["text"] for _, n in by_class(wf, "CLIPTextEncode")}
        self.assertEqual((texts["Face Detailer 긍정"], texts["Face Detailer 부정"]), ("face", "bad"))

    def test_disabled_adds_nothing(self):
        wf = W.build_workflow(CKPT)
        self.assertFalse(by_class(wf, "FaceDetailer") or by_class(wf, "UltralyticsDetectorProvider"))

    def test_face_detailer_base(self):
        for name, build, spec, defaults in BUILDERS:
            with self.subTest(name):
                wf = build({**spec, "base": "face_detailer", "positive": "",
                            "face_detailer": {"positive": "face"}, "hires_fix": {"enabled": True},
                            "usdu": {"enabled": True}})
                image_id, image = one(wf, "LoadImage")
                self.assertEqual(image["_meta"]["title"], "input_image")
                fd_id, save = self.check_fd(wf, defaults, [image_id, 0])
                self.assertEqual(save["inputs"]["images"], [fd_id, 0])
                for cls in ("KSampler", "VAEDecode", "EmptyLatentImage", "UltimateSDUpscaleNoUpscale"):
                    self.assertFalse(by_class(wf, cls), cls)
                titles = [n["_meta"]["title"] for n in wf.values()]
                self.assertNotIn("main_prompt", titles)
        with self.assertRaises(W.WorkflowBuildError):
            W.build_workflow({**CKPT, "base": "face_detailer", "positive": ""})


if __name__ == "__main__":
    unittest.main()
