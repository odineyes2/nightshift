"""NS-41-1 OpenPose ControlNet 빌더 — 배선·기본값·후처리와 함께 쓸 때의 순서·제외 모델·NODE_PACKS.
python tests/test_openpose_builder.py"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import workflow_builder as W  # noqa: E402
import workflow_builder_unet as U  # noqa: E402

CN = {"enabled": True, "type": "openpose", "control_net_name": "openpose_pre.safetensors"}
SPEC = {"checkpoint": "wai.safetensors", "positive": "1girl", "negative": "bad", "controlnet": CN}


def by_class(wf, cls):
    return [i for i, n in wf.items() if n["class_type"] == cls]


def one(wf, cls):
    found = by_class(wf, cls)
    assert len(found) == 1, (cls, found)
    return found[0]


def raises(fn, *args):
    try:
        fn(*args)
    except W.WorkflowBuildError:
        return
    raise AssertionError("WorkflowBuildError가 나야 해요")


def title_id(wf, title):
    return next(i for i, n in wf.items() if n["_meta"]["title"] == title)


for base in ("txt2img", "img2img"):
    for hires in (False, True):
        for fd in (False, True):
            for usdu in (False, True):
                spec = {**SPEC, "base": base, "hires_fix": {"enabled": hires},
                        "face_detailer": {"enabled": fd}, "usdu": {"enabled": usdu}}
                wf = W.build_workflow(spec)
                pos, neg = title_id(wf, "main_prompt"), title_id(wf, "negative_prompt")
                ckpt = one(wf, "CheckpointLoaderSimple")
                loader, dw, apply = one(wf, "ControlNetLoader"), one(wf, "DWPreprocessor"), one(wf, "ControlNetApplyAdvanced")
                pose = title_id(wf, "pose_image")
                assert wf[pose]["class_type"] == "LoadImage"
                assert wf[dw]["inputs"]["image"] == [pose, 0]
                a = wf[apply]["inputs"]
                assert a["positive"] == [pos, 0] and a["negative"] == [neg, 0]
                assert a["control_net"] == [loader, 0] and a["image"] == [dw, 0] and a["vae"] == [ckpt, 2]
                assert (a["strength"], a["start_percent"], a["end_percent"]) == (0.8, 0.0, 0.8)
                assert {k: wf[dw]["inputs"][k] for k in W.DWPOSE_DEFAULTS} == W.DWPOSE_DEFAULTS
                # 베이스·hires KSampler는 CN 조건, FaceDetailer·USDU는 원래 조건(CN을 거치지 않는다)
                samplers = by_class(wf, "KSampler")
                assert len(samplers) == (2 if hires else 1)
                for s in samplers:
                    assert wf[s]["inputs"]["positive"] == [apply, 0] and wf[s]["inputs"]["negative"] == [apply, 1]
                for cls in ("FaceDetailer", "UltimateSDUpscaleNoUpscale"):
                    for n in by_class(wf, cls):
                        assert apply not in {v[0] for v in wf[n]["inputs"].values() if isinstance(v, list)}
                # img2img 입력 주입(제목에 input_image)과 포즈 이미지가 섞이지 않는다
                assert "input_image" not in wf[pose]["_meta"]["title"]
                # 순서: KSampler → hires → Decode → FD → USDU → Save
                order = [int(i) for i in samplers] + [int(one(wf, "VAEDecode"))]
                order += [int(i) for i in by_class(wf, "FaceDetailer")]
                order += [int(i) for i in by_class(wf, "UltimateSDUpscaleNoUpscale")]
                order.append(int(one(wf, "SaveImage")))
                assert order == sorted(order), order

# 스펙 값이 기본값을 덮는다
wf = W.build_workflow({**SPEC, "controlnet": {**CN, "strength": 0.5, "end_percent": 1,
                                               "dw": {"scale_stick_for_xinsr_cn": "enable", "resolution": 768}}})
assert wf[one(wf, "ControlNetApplyAdvanced")]["inputs"]["strength"] == 0.5
assert wf[one(wf, "DWPreprocessor")]["inputs"]["scale_stick_for_xinsr_cn"] == "enable"
assert wf[one(wf, "DWPreprocessor")]["inputs"]["resolution"] == 768

# CN이 꺼져 있으면 노드가 없다(기존 그래프 그대로)
assert not by_class(W.build_workflow({**SPEC, "controlnet": {}}), "ControlNetLoader")

# 거절: 모델 없음, start>end, face_detailer base, UNet 빌더
raises(W.build_workflow, {**SPEC, "controlnet": {**CN, "control_net_name": ""}})
raises(W.build_workflow, {**SPEC, "controlnet": {**CN, "start_percent": 0.9, "end_percent": 0.5}})
raises(W.build_workflow, {**SPEC, "base": "face_detailer"})
raises(U.build_workflow, {**SPEC, "clip": "c.safetensors", "vae": "v.safetensors"})

# 노드팩 매핑 — 없으면 "노드팩 없음 (comfyui_controlnet_aux)"으로 자동 설치 대상이 된다
src = (ROOT / "server/app_parts/02-pod-runtime-comfy.py").read_text(encoding="utf-8")
assert '"DWPreprocessor": "comfyui_controlnet_aux"' in src

print("ok")
