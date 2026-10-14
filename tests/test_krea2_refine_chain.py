"""NS-75-2 krea.2 리파인 요청·템플릿 주입·색인 회귀 — python tests/test_krea2_refine_chain.py"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

tmp = tempfile.mkdtemp()
os.environ.update(NIGHTSHIFT_DATA_DIR=tmp, NIGHTSHIFT_OUTPUT_DIR=tmp + "/out", NIGHTSHIFT_ASSETS_DIR=tmp + "/a",
                  NIGHTSHIFT_ADMIN_USER="admin", NIGHTSHIFT_ADMIN_PASSWORD="Test-Passw0rd-xyz!")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "templates"))

import httpx  # noqa: E402
import app as A  # noqa: E402
import assets_index  # noqa: E402
import auth  # noqa: E402
import seed_batch  # noqa: E402

admin = auth.ensure_admin() or auth.authenticate("admin", os.environ["NIGHTSHIFT_ADMIN_PASSWORD"])
TOKEN = auth.create_session(admin["id"])
A.object_info_pod = lambda pod_id: {"id": "mock"}
A.fetch_comfy_object_info = lambda force=False, pod=None: ("http://mock", {
    "KSampler": {"input": {"required": {"sampler_name": [["euler"]], "scheduler": [["simple"]]}}}})


async def build(refine_prompt):
    spec = {"architecture": "krea2", "checkpoint": "krea2.safetensors", "positive": "원래 프롬프트",
            "clip": "chosen_encoder.safetensors", "seed": 77, "refine_prompt": refine_prompt}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=A.app), base_url="http://test",
                                 headers={"x-requested-with": "nightshift"},
                                 cookies={auth.SESSION_COOKIE: TOKEN}) as client:
        response = await client.post("/api/build-workflow", json=spec)
    assert response.status_code == 200, response.text
    return response.json()["workflow"]


def one_node(workflow, class_type):
    matches = [(node_id, node) for node_id, node in workflow.items() if node["class_type"] == class_type]
    assert len(matches) == 1, (class_type, matches)
    return matches[0]


def check_chain(workflow, refined):
    main_nodes = [(node_id, node) for node_id, node in workflow.items()
                  if "main_prompt" in node.get("_meta", {}).get("title", "")]
    assert len(main_nodes) == 1, main_nodes
    main_id, main = main_nodes[0]
    assert main["class_type"] == "PrimitiveStringMultiline"
    assert main["inputs"]["value"] == "원래 프롬프트"

    clip_id, clip = one_node(workflow, "CLIPLoader")
    assert clip["inputs"]["clip_name"] == "chosen_encoder.safetensors"
    _, sampler = one_node(workflow, "KSampler")
    encode_id = sampler["inputs"]["positive"][0]
    encode = workflow[encode_id]
    assert encode["class_type"] == "CLIPTextEncode"
    assert encode["inputs"]["clip"] == [clip_id, 0]
    if refined:
        refine_id = encode["inputs"]["text"][0]
        refine = workflow[refine_id]
        assert refine["class_type"] == "TextGenerate"
        assert refine["inputs"]["clip"] == [clip_id, 0]
        concat_id = refine["inputs"]["prompt"][0]
        concat = workflow[concat_id]
        assert concat["class_type"] == "StringConcatenate"
        assert concat["inputs"]["string_b"] == [main_id, 0]
        system_id = concat["inputs"]["string_a"][0]
        assert workflow[system_id]["class_type"] == "PrimitiveStringMultiline"
        assert "system" in workflow[system_id]["_meta"]["title"].lower()
        _, preview = one_node(workflow, "PreviewAny")
        assert preview["inputs"]["source"] == [refine_id, 0]
    else:
        assert encode["inputs"]["text"] == [main_id, 0]
        assert not any(node["class_type"] in ("TextGenerate", "PreviewAny", "StringConcatenate")
                       for node in workflow.values())


async def main():
    on = await build(True)
    off = await build(False)
    check_chain(on, True)
    check_chain(off, False)

    seed_batch.apply_main_prompt(on, "주입된 프롬프트")
    main_id, main = seed_batch.find_node(on, title_substring="main_prompt")
    assert main["inputs"]["value"] == "주입된 프롬프트"
    assert sum("main_prompt" in node.get("_meta", {}).get("title", "") for node in on.values()) == 1
    _, sampler = one_node(on, "KSampler")
    encode = on[sampler["inputs"]["positive"][0]]
    refine = on[encode["inputs"]["text"][0]]
    concat = on[refine["inputs"]["prompt"][0]]
    assert refine["class_type"] == "TextGenerate"
    assert concat["inputs"]["string_b"] == [main_id, 0]

    on_meta = assets_index.meta_from_graph(on)
    off_meta = assets_index.meta_from_graph(off)
    assert on_meta["prompt"] == "주입된 프롬프트", on_meta
    assert json.loads(on_meta["params_json"])["prompt_enhance"] == {"kind": "workflow"}
    assert "prompt_enhance" not in json.loads(off_meta["params_json"])


asyncio.run(main())
print("ok")
