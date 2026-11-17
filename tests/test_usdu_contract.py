"""USDU NoUpscale의 실제 워커 입력 명세(2026-11-17)와 네 가지 생성 경로의 호환성 검사."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
sys.path.insert(0, str(ROOT / "server"))

import db
import generate_models
import model_registry
import workflow_builder
import workflow_builder_unet

# 현재 워커 /object_info/UltimateSDUpscaleNoUpscale의 required 필드.
REQUIRED = set("upscaled_image model positive negative vae seed steps cfg sampler_name scheduler denoise "
               "mode_type tile_width tile_height mask_blur tile_padding seam_fix_mode seam_fix_denoise "
               "seam_fix_width seam_fix_mask_blur seam_fix_padding force_uniform_tiles tiled_decode batch_size".split())


class UsduContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        model_registry.upsert("checkpoints", "waiIllustriousSDXL_v170.safetensors", {"base_model": "Illustrious"})
        model_registry.upsert("diffusion_models", "anima-base-v1.0.safetensors", {"base_model": "Anima"})

    def check_contract(self, workflow):
        nodes = [n for n in workflow.values() if n["class_type"] == "UltimateSDUpscaleNoUpscale"]
        self.assertEqual(len(nodes), 1)
        inputs = nodes[0]["inputs"]
        self.assertEqual(set(inputs), REQUIRED)
        self.assertEqual(inputs["batch_size"], 1)
        source, slot = inputs["upscaled_image"]
        self.assertEqual((workflow[source]["class_type"], slot), ("ImageScaleBy", 0))

    def test_builder_checkpoint_and_unet(self):
        common = {"positive": "landscape", "usdu": {"enabled": True}}
        self.check_contract(workflow_builder.build_workflow({**common, "checkpoint": "waiIllustriousSDXL_v170.safetensors"}))
        self.check_contract(workflow_builder_unet.build_workflow({**common, "checkpoint": "anima-base-v1.0.safetensors",
                            "clip": "qwen_3_06b_base.safetensors", "vae": "qwen_image_vae.safetensors"}))

    def test_refine_checkpoint_and_unet(self):
        parts = {"clip": "qwen_3_06b_base.safetensors", "vae": "qwen_image_vae.safetensors"}
        with patch.object(model_registry, "unet_image_parts", return_value=parts):
            for model in ("checkpoints:waiIllustriousSDXL_v170.safetensors", "diffusion_models:anima-base-v1.0.safetensors"):
                with self.subTest(model=model):
                    entry = generate_models.find(model)
                    workflow, _ = generate_models.build_refine(entry, "upscale", "landscape", "", 7)
                    self.check_contract(workflow)


if __name__ == "__main__":
    unittest.main()
