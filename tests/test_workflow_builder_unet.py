"""NS-34-1 UNet+CLIP+VAE 범용 이미지 빌더와 제외 family 판정. 임시 데이터 폴더만 쓴다."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
sys.path.insert(0, str(ROOT / "server"))

import db
import model_registry
import workflow_builder
import workflow_builder_unet as U

db.init()

SPEC = {"checkpoint": "anima.safetensors", "clip": "qwen_3_06b.safetensors", "vae": "qwen_image_vae.safetensors",
        "positive": "(prompt)", "loras": [{"name": "a.safetensors"}, {"name": "b.safetensors"}]}


def by_class(wf, cls):
    return [(i, n) for i, n in wf.items() if n["class_type"] == cls]


class UnetBuilderTests(unittest.TestCase):
    def test_loaders_and_lora_chain(self):
        wf = U.build_workflow(SPEC)
        (unet, _), = by_class(wf, "UNETLoader")
        (clip, clip_node), = by_class(wf, "CLIPLoader")
        (vae, _), = by_class(wf, "VAELoader")
        self.assertEqual(clip_node["inputs"]["type"], "stable_diffusion")
        self.assertFalse(by_class(wf, "CheckpointLoaderSimple"))
        (l1, n1), (l2, n2) = by_class(wf, "LoraLoader")
        self.assertEqual((n1["inputs"]["model"], n1["inputs"]["clip"]), ([unet, 0], [clip, 0]))
        self.assertEqual((n2["inputs"]["model"], n2["inputs"]["clip"]), ([l1, 0], [l1, 1]))
        for _, n in by_class(wf, "CLIPTextEncode"):
            self.assertEqual(n["inputs"]["clip"], [l2, 1])
        (_, ks), = by_class(wf, "KSampler")
        self.assertEqual(ks["inputs"]["model"], [l2, 0])
        self.assertEqual((ks["inputs"]["steps"], ks["inputs"]["cfg"], ks["inputs"]["sampler_name"]), (30, 4.0, "er_sde"))
        (_, dec), = by_class(wf, "VAEDecode")
        self.assertEqual(dec["inputs"]["vae"], [vae, 0])
        self.assertEqual(len(by_class(wf, "EmptyLatentImage")), 1)

    def test_img2img_hires_usdu(self):
        wf = U.build_workflow({**SPEC, "base": "img2img", "hires_fix": {"enabled": True}, "usdu": {"enabled": True}})
        (vae, _), = by_class(wf, "VAELoader")
        (_, enc), = by_class(wf, "VAEEncode")
        self.assertEqual(enc["inputs"]["vae"], [vae, 0])
        self.assertEqual(wf[enc["inputs"]["pixels"][0]]["_meta"]["title"], "input_image")
        self.assertEqual(len(by_class(wf, "KSampler")), 2)
        (dec, _), = by_class(wf, "VAEDecode")
        (scale, sc), = by_class(wf, "ImageScaleBy")
        self.assertEqual(sc["inputs"]["image"], [dec, 0])
        (usdu, un), = by_class(wf, "UltimateSDUpscaleNoUpscale")
        self.assertEqual((un["inputs"]["image"], un["inputs"]["vae"]), ([scale, 0], [vae, 0]))
        (_, save), = by_class(wf, "SaveImage")
        self.assertEqual(save["inputs"]["images"], [usdu, 0])

    def test_missing_parts_rejected(self):
        for field in ("checkpoint", "clip", "vae"):
            with self.assertRaises(workflow_builder.WorkflowBuildError):
                U.build_workflow({**SPEC, field: ""})

    def test_checkpoint_builder_unchanged(self):
        wf = workflow_builder.build_workflow({"checkpoint": "x.safetensors", "positive": "p"})
        (ck, _), = by_class(wf, "CheckpointLoaderSimple")
        (_, dec), = by_class(wf, "VAEDecode")
        self.assertEqual(dec["inputs"]["vae"], [ck, 2])
        self.assertFalse(by_class(wf, "UNETLoader"))


class FamilyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for kind, name, base in (
            ("diffusion_models", "anima.safetensors", "Anima"),
            ("text_encoders", "qwen_3_06b.safetensors", "Anima"),
            ("vae", "qwen_image_vae.safetensors", "Anima"),
            ("diffusion_models", "lonely.safetensors", "Lonely"),  # 텍스트 인코더·VAE 없음
            ("diffusion_models", "krea2.safetensors", "krea.2"),
            ("diffusion_models", "h3_fl2va.safetensors", "MiniMax-H3"),
            ("diffusion_models", "wan22.safetensors", "Wan 2.2"),
            ("diffusion_models", "wan21.safetensors", "Wan 2.1"),
        ):
            model_registry.upsert(kind, name, {"base_model": base})
        for base in ("krea.2", "MiniMax-H3", "Wan 2.2", "Wan 2.1"):  # 제외 family도 부품이 다 있어야 의미 있는 검사다
            model_registry.upsert("text_encoders", f"te_{base}.safetensors", {"base_model": base})
            model_registry.upsert("vae", f"vae_{base}.safetensors", {"base_model": base})

    def test_unet_image_only_for_eligible(self):
        groups = model_registry.checkpoint_groups()
        self.assertEqual(groups["anima"]["unet_image"],
                         {"clip": "qwen_3_06b.safetensors", "vae": "qwen_image_vae.safetensors"})
        for gid in ("lonely", "krea.2", "minimax-h3", "wan-2.2", "wan-2.1"):
            self.assertIn(gid, groups)
            self.assertNotIn("unet_image", groups[gid], gid)


if __name__ == "__main__":
    unittest.main()
