"""NS-122-1 워크플로우 모델·LoRA 제자리 패치 — 빌더 출력에 패치한 결과가 새로 조립한 결과와 배선이 같은지, 거절·API 응답을 본다."""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
os.environ.setdefault("NIGHTSHIFT_ADMIN_USER", "admin")
os.environ.setdefault("NIGHTSHIFT_ADMIN_PASSWORD", "Test-Passw0rd-xyz!")
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")

import db

db.init()

import app as A
import workflow_builder
import workflow_builder_krea2
import workflow_builder_unet
from fastapi.testclient import TestClient
from workflow_builder import WorkflowBuildError
from workflow_models import apply_model_values

FULL = {"hires_fix": {"enabled": True, "scale_by": 1.5}, "face_detailer": {"enabled": True},
        "usdu": {"enabled": True}}
BUILDERS = {
    "sdxl": (workflow_builder.build_workflow, {"checkpoint": "base.safetensors", "positive": "a cat", **FULL}, False),
    "unet": (workflow_builder_unet.build_workflow,
             {"checkpoint": "anima.safetensors", "clip": "te.safetensors", "vae": "vae.safetensors",
              "positive": "a cat", **FULL}, False),
    "krea2": (workflow_builder_krea2.build_workflow, {"checkpoint": "krea.safetensors", "positive": "a cat"}, True),
}
ONE = [("one.safetensors", 0.6)]  # 이름이 남으면 strength_clip을 이으니 새 조립과 비교하려면 강도가 같아야 한다
TWO = [("one.safetensors", 0.6), ("two.safetensors", 0.7)]


def build(kind: str, loras: list) -> dict:
    builder, spec, _ = BUILDERS[kind]
    return builder({**spec, "loras": [{"name": name, "strength_model": s, "strength_clip": s}
                                      for name, s in loras]})


def patch(kind: str, workflow: dict, loras: list) -> dict:
    return apply_model_values(workflow, {"loras": [{"name": name, "strength": s} for name, s in loras],
                                         "model_only": BUILDERS[kind][2]})


def wiring(workflow: dict) -> list:
    """노드 id와 무관하게 비교하도록, 아무도 쓰지 않는 끝 노드마다 입력 링크를 펼친 문자열을 돌려준다."""
    used = set()
    for node in workflow.values():
        for value in node["inputs"].values():
            if isinstance(value, list):
                assert str(value[0]) in workflow, f"끊어진 링크 {value}"
                used.add(str(value[0]))

    def expand(node_id):
        node = workflow[node_id]
        inputs = {key: [expand(str(value[0])), value[1]] if isinstance(value, list) else value
                  for key, value in node["inputs"].items()}
        return {"class": node["class_type"], "title": node["_meta"]["title"], "inputs": inputs}

    return sorted(json.dumps(expand(node_id), ensure_ascii=False, sort_keys=True)
                  for node_id in workflow if node_id not in used)


def nodes_of(workflow: dict, *classes) -> dict:
    return {node_id: node for node_id, node in workflow.items() if node["class_type"] in classes}


class LoraChainTests(unittest.TestCase):
    def test_patch_matches_fresh_build(self):
        for kind in BUILDERS:
            for before, after in (([], TWO), (TWO, []), (ONE, TWO), (TWO, TWO[1:]), ([], [])):
                with self.subTest(kind=kind, before=len(before), after=len(after)):
                    patched = patch(kind, build(kind, before), after)
                    self.assertEqual(wiring(patched), wiring(build(kind, after)))

    def test_finds_downstream_consumers(self):
        # FaceDetailer·USDU·두 KSampler가 모두 새 끝을 가리키고 CLIP 소비자도 옮겨졌는지 직접 본다.
        patched = patch("sdxl", build("sdxl", ONE), TWO)
        tail = max(nodes_of(patched, "LoraLoader"), key=int)
        for node in nodes_of(patched, "KSampler", "FaceDetailer", "UltimateSDUpscaleNoUpscale").values():
            self.assertEqual(node["inputs"]["model"], [tail, 0])
        for node in nodes_of(patched, "CLIPTextEncode", "FaceDetailer").values():
            self.assertEqual(node["inputs"]["clip"], [tail, 1])

    def test_strength_clip_kept_for_same_name(self):
        base = workflow_builder.build_workflow({
            **BUILDERS["sdxl"][1],
            "loras": [{"name": "one.safetensors", "strength_model": 0.8, "strength_clip": 0.4},
                      {"name": "gone.safetensors", "strength_model": 1.0, "strength_clip": 0.2}]})
        patched = apply_model_values(base, {"loras": [{"name": "new.safetensors", "strength": 0.7},
                                                      {"name": "one.safetensors", "strength": 0.5}]})
        got = {node["inputs"]["lora_name"]: (node["inputs"]["strength_model"], node["inputs"]["strength_clip"])
               for node in nodes_of(patched, "LoraLoader").values()}
        self.assertEqual(got, {"new.safetensors": (0.7, 0.7), "one.safetensors": (0.5, 0.4)})

    def test_new_ids_titles_and_class(self):
        base = build("krea2", ONE)
        highest = max(int(node_id) for node_id in base)
        # 체인이 있으면 model_only 값과 상관없이 기존 노드 클래스를 따른다.
        patched = apply_model_values(base, {"loras": [{"name": n, "strength": s} for n, s in TWO],
                                            "model_only": False})
        self.assertEqual(nodes_of(patched, "LoraLoader"), {})
        loras = nodes_of(patched, "LoraLoaderModelOnly")
        self.assertEqual(sorted(loras), [str(highest + 1), str(highest + 2)])
        self.assertEqual([loras[i]["_meta"]["title"] for i in sorted(loras)], ["LoRA 1", "LoRA 2"])
        self.assertNotIn("clip", loras[str(highest + 2)]["inputs"])

    def test_blank_rows_skipped_and_input_untouched(self):
        base = build("sdxl", ONE)
        before = copy.deepcopy(base)
        patched = apply_model_values(base, {"loras": [{"name": " "}, {"name": "two.safetensors", "strength": 0}]})
        self.assertEqual(base, before)
        lora = next(iter(nodes_of(patched, "LoraLoader").values()))
        self.assertEqual((lora["inputs"]["strength_model"], lora["_meta"]["title"]), (0.0, "LoRA 1"))

    def assert_rejected(self, workflow, message):
        with self.assertRaises(WorkflowBuildError) as caught:
            apply_model_values(workflow, {"loras": [{"name": "x.safetensors", "strength": 1}]})
        self.assertIn(message, str(caught.exception))

    def test_non_straight_chains_rejected(self):
        base = build("sdxl", TWO)
        first, second = sorted(nodes_of(base, "LoraLoader"), key=int)
        sampler = next(iter(nodes_of(base, "KSampler").values()))
        encoder = next(iter(nodes_of(base, "CLIPTextEncode").values()))
        loader = base[first]["inputs"]["model"][0]

        def changed(edit):
            workflow = copy.deepcopy(base)
            edit(workflow)
            return workflow

        # 중간 LoRA에서 다른 노드로 갈라짐(model·clip)
        for field, node, link in (("model", sampler, [first, 0]), ("clip", encoder, [first, 1])):
            node_id = next(i for i, n in base.items() if n is node)
            self.assert_rejected(changed(lambda w: w[node_id]["inputs"].update({field: link})), "일자가 아니")
        # 같은 뿌리에서 나란히 갈라진 LoRA
        self.assert_rejected(changed(lambda w: w[second]["inputs"].update({"model": [loader, 0]})), "시작이 2개")
        # LoRA 하나에서 LoRA 둘로 갈라짐
        third = {**copy.deepcopy(base[second]), "_meta": {"title": "LoRA 3"}}
        self.assert_rejected(changed(lambda w: w.update({"900": third})), "갈라져요")
        # 체인과 이어지지 않은 고리 LoRA(체인 밖)
        ring = {"inputs": {"lora_name": "r", "strength_model": 1, "strength_clip": 1, "model": ["901", 0],
                           "clip": ["901", 1]}, "class_type": "LoraLoader", "_meta": {"title": "r"}}
        self.assert_rejected(changed(lambda w: w.update({"901": ring})), "체인 밖")
        # clip이 체인과 다르게 연결됨
        self.assert_rejected(changed(lambda w: w[second]["inputs"].update({"clip": [loader, 1]})), "clip 입력")
        # 종류가 섞인 체인
        self.assert_rejected(changed(lambda w: w[second].update({"class_type": "LoraLoaderModelOnly"})), "섞여")

    def test_adding_needs_single_loader(self):
        base = build("krea2", [])
        loader = next(iter(nodes_of(base, "UNETLoader").values()))
        base["900"] = copy.deepcopy(loader)
        with self.assertRaises(WorkflowBuildError):
            patch("krea2", base, TWO)


class ValueTests(unittest.TestCase):
    def test_checkpoint_clip_hires(self):
        sdxl = apply_model_values(build("sdxl", ONE), {"checkpoint": "sub/new.safetensors", "hires_scale": 2})
        self.assertEqual(next(iter(nodes_of(sdxl, "CheckpointLoaderSimple").values()))["inputs"]["ckpt_name"],
                         "sub/new.safetensors")
        self.assertEqual(next(iter(nodes_of(sdxl, "LatentUpscaleBy").values()))["inputs"]["scale_by"], 2.0)
        unet = apply_model_values(build("unet", []), {"checkpoint": "new.safetensors", "clip": "te2.safetensors"})
        self.assertEqual(next(iter(nodes_of(unet, "UNETLoader").values()))["inputs"]["unet_name"], "new.safetensors")
        self.assertEqual(next(iter(nodes_of(unet, "CLIPLoader").values()))["inputs"]["clip_name"], "te2.safetensors")

    def test_absent_keys_untouched(self):
        base = build("sdxl", ONE)
        self.assertEqual(apply_model_values(base, {}), base)

    def test_rejected_values(self):
        two_unets = build("krea2", [])
        two_unets["900"] = copy.deepcopy(next(iter(nodes_of(two_unets, "UNETLoader").values())))
        cases = [
            (build("krea2", []), {"hires_scale": 1.5}),                  # Hires 노드 없음
            (build("sdxl", []), {"clip": "te.safetensors"}),             # CLIPLoader 없음
            ({}, {"checkpoint": "x.safetensors"}),                       # 로더 없음
            (two_unets, {"checkpoint": "x.safetensors"}),                # 로더 둘
            (build("sdxl", []), {"checkpoint": "../x.safetensors"}),
            (build("sdxl", []), {"checkpoint": ""}),
            (build("sdxl", []), {"hires_scale": 9}),
            (build("sdxl", []), {"hires_scale": float("nan")}),
            (build("sdxl", []), {"loras": "x"}),
            (build("sdxl", []), {"loras": [{"name": "x", "strength": "abc"}]}),
            (build("sdxl", []), {"loras": [{"name": "x", "strength": float("inf")}]}),
            (build("sdxl", []), {"model_only": "yes"}),
            (build("sdxl", []), {"vae": "x"}),
            (build("sdxl", []), None),
        ]
        for workflow, values in cases:
            with self.subTest(values=values), self.assertRaises(WorkflowBuildError):
                apply_model_values(workflow, values)


class ApiTests(unittest.TestCase):
    def test_logged_in_member(self):
        headers = {"X-Requested-With": "nightshift"}
        pw = os.environ["NIGHTSHIFT_ADMIN_PASSWORD"]
        user = A.auth.register("workflow-models-member", "workflow-models@example.test", pw)
        with db.connect() as conn:
            conn.execute("UPDATE users SET status='active' WHERE id=?", (user["id"],))
        # lifespan 없이 실제 앱을 써서 스케줄러·외부 워커를 시작하지 않는다.
        member, anonymous = TestClient(A.app), TestClient(A.app)
        self.addCleanup(member.close)
        self.addCleanup(anonymous.close)
        response = member.post("/api/auth/login", json={"username": "workflow-models-member", "password": pw},
                               headers=headers)
        self.assertEqual(response.status_code, 200, response.text)

        url = "/api/workflow/apply-models"
        workflow = build("sdxl", ONE)
        body = {"workflow": workflow, "values": {"loras": [{"name": n, "strength": s} for n, s in TWO]}}
        self.assertEqual(anonymous.post(url, json=body, headers=headers).status_code, 401)
        self.assertEqual(member.post(url, json=body).status_code, 403)
        response = member.post(url, json=body, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(list(response.json()), ["workflow"])
        self.assertEqual(wiring(response.json()["workflow"]), wiring(build("sdxl", TWO)))

        branched = copy.deepcopy(workflow)
        lora_id = next(iter(nodes_of(branched, "LoraLoader")))
        branched["900"] = {**copy.deepcopy(branched[lora_id]), "_meta": {"title": "LoRA 2"}}
        response = member.post(url, json={"workflow": branched, "values": body["values"]}, headers=headers)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("일자가 아니", response.json()["detail"])

        for bad in ({"workflow": [], "values": {}}, {"workflow": workflow}, []):
            self.assertEqual(member.post(url, json=bad, headers=headers).status_code, 400)
        self.assertEqual(member.post(url, content=b"{", headers=headers).status_code, 400)


if __name__ == "__main__":
    unittest.main()
