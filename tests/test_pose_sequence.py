"""NS-42-1 순차 생성 서버·템플릿 — 일괄 to-input API·SDXL 크기·pose_sequence 검사·progress label·seed_batch 포즈 순회. 임시 데이터 폴더만 쓴다."""
import asyncio
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
os.environ.setdefault("NIGHTSHIFT_ADMIN_USER", "admin")
os.environ.setdefault("NIGHTSHIFT_ADMIN_PASSWORD", "Test-Passw0rd-xyz!")
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")

import app as A
import pose_library as P
from fastapi import HTTPException
from PIL import Image

spec = importlib.util.spec_from_file_location("seed_batch", ROOT / "templates" / "seed_batch.py")
SB = importlib.util.module_from_spec(spec)
spec.loader.exec_module(SB)


def png(size=(800, 600)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 100, 50)).save(buf, "PNG")
    return buf.getvalue()


def req(body, uid="u1", role="user", internal=False):
    async def read_body():
        return json.dumps(body).encode()
    return SimpleNamespace(state=SimpleNamespace(user={"id": uid, "role": role}, internal=internal), body=read_body)


def call(fn, *args):
    try:
        out = fn(*args)
        return 200, asyncio.run(out) if asyncio.iscoroutine(out) else out
    except HTTPException as e:
        return e.status_code, e.detail


class PoseSequenceTests(unittest.TestCase):
    def test_sdxl_size(self):
        self.assertEqual(P.sdxl_size(800, 600), (1152, 896))
        self.assertEqual(P.sdxl_size(1000, 1000), (1024, 1024))
        self.assertEqual(P.sdxl_size(100, 2000), (512, 2048))

    def test_bulk_to_input(self):
        a = P.add_pose("u1", "앉기", "", "sitting", [(png((800, 600)), ""), (png((600, 800)), "")])
        b = P.add_pose("u1", "서기", "", "standing", [(png((1000, 1000)), "")])
        other = P.add_pose("u2", "남의 것", "", "", [(png(), "")])
        code, out = call(A.library_poses_to_input, req({"pose_ids": [b["id"], a["id"]]}))
        self.assertEqual(code, 200, out)
        self.assertEqual([(o["pose_id"], o["image_id"]) for o in out],
                         [(b["id"], b["images"][0]["id"])] + [(a["id"], i["id"]) for i in a["images"]])
        self.assertEqual(out[0]["name"], f"library_pose_{b['id']}_{b['images'][0]['id']}.png")
        self.assertEqual((out[1]["width"], out[1]["sdxl_width"], out[1]["sdxl_height"]), (800, 1152, 896))
        self.assertEqual((out[1]["danbooru_prompt"], out[1]["pose_name"]), ("sitting", "앉기"))
        # 같은 내용이면 사본을 새로 만들지 않는다
        code, again = call(A.library_poses_to_input, req({"pose_ids": [b["id"]]}))
        self.assertEqual(again[0]["name"], out[0]["name"])
        # 남의 게시물이 하나라도 있으면 404, admin은 전부
        self.assertEqual(call(A.library_poses_to_input, req({"pose_ids": [a["id"], other["id"]]}))[0], 404)
        self.assertEqual(call(A.library_poses_to_input, req({"pose_ids": [other["id"]]}, "admin", "admin"))[0], 200)
        self.assertEqual(call(A.library_poses_to_input, req({"pose_ids": []}))[0], 400)

    def test_parse_pose_sequence(self):
        name = A.save_input_image("seq_pose.png", png())
        items = A.parse_pose_sequence(json.dumps([{"image": name, "pose_id": 3, "tags": "sitting", "width": 896, "height": 1152}]))
        self.assertEqual(items[0], {"image": name, "pose_id": 3, "pose_name": "", "tags": "sitting", "width": 896, "height": 1152})
        self.assertIsNone(A.parse_pose_sequence(""))
        for bad in ("[]", "{", json.dumps([{"image": "missing.png"}]), json.dumps([{"image": name, "width": "x"}])):
            with self.assertRaises(HTTPException) as e:
                A.parse_pose_sequence(bad)
            self.assertEqual(e.exception.status_code, 400)
        # 템플릿 옵션 목록에는 있지만(폼이 보내도록) 옵션으로 저장하지 않는다
        seed = next(t for t in A.load_templates_list() if t["id"] == "seed_batch")
        self.assertIn("pose_sequence", [o["name"] for o in seed["options"]])

    def test_progress_label(self):
        A.jobs["plabel"] = {"id": "plabel", "progress": None}
        self.assertEqual(call(A.update_job_progress, "plabel", req({"total": 6, "done": 2, "label": "포즈 2/3"}, internal=True))[0], 200)
        self.assertEqual(A.jobs["plabel"]["progress"], {"total": 6, "done": 2, "label": "포즈 2/3"})
        self.assertEqual(call(A.update_job_progress, "plabel", req({"total": 6, "done": 2, "label": 3}, internal=True))[0], 400)
        self.assertEqual(call(A.update_job_progress, "plabel", req({"total": 6, "done": 2, "label": "x" * 41}, internal=True))[0], 400)
        call(A.update_job_progress, "plabel", req({"total": 6, "done": 3}, internal=True))
        self.assertEqual(A.jobs["plabel"]["progress"], {"total": 6, "done": 3})

    def _run_template(self, env, poses=None):
        calls, progress = [], []
        env = {"WORKFLOW_PATH": "w.json", "SEED_MODE": "sequential", "MAIN_PROMPT": "1girl", "WIDTH": "", "HEIGHT": "", **env}
        if poses is not None:
            path = Path(TMP.name) / "poses.json"
            path.write_text(json.dumps(poses), encoding="utf-8")
            env["POSE_SEQUENCE_PATH"] = str(path)
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(SB, "load_workflow", return_value={}), \
             mock.patch.object(SB, "get_default_batch_size", return_value=1), \
             mock.patch.object(SB, "run_once", side_effect=lambda *a, **k: calls.append((a, k))), \
             mock.patch.object(SB, "report_progress", side_effect=lambda *a: progress.append(a[2:])):
            if poses is None:
                os.environ.pop("POSE_SEQUENCE_PATH", None)
            SB.main()
        return calls, progress

    def test_template_pose_loop(self):
        poses = [{"image": f"p{i}.png", "tags": f"tag{i}" if i != 2 else "", "width": 896 + i * 64, "height": 1152}
                 for i in (1, 2, 3)]
        poses[2].pop("width")
        calls, progress = self._run_template({"SEED_COUNT": "2", "WIDTH": "1024", "HEIGHT": "1024"}, poses)
        self.assertEqual(len(calls), 6)
        self.assertEqual([k["pose_image"] for _, k in calls], ["p1.png"] * 2 + ["p2.png"] * 2 + ["p3.png"] * 2)
        self.assertEqual([a[4] for a, _ in calls][::2], ["1girl, tag1", "1girl", "1girl, tag3"])
        self.assertTrue(all(k["face_prompt"] == "1girl" for _, k in calls))
        self.assertEqual([(a[6], a[7]) for a, _ in calls][::2], [("960", "1152"), ("1024", "1152"), ("1024", "1152")])
        self.assertEqual([a[2] for a, _ in calls], [0, 1, 2, 3, 4, 5])
        labels = [p[2] for p in progress]
        self.assertEqual(labels, ["포즈 1/3"] * 2 + ["포즈 2/3"] * 2 + ["포즈 3/3"] * 3)
        self.assertEqual(progress[-1][:2], (6, 6))

    def test_template_without_poses(self):
        calls, progress = self._run_template({"SEED_COUNT": "3"})
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(k == {} for _, k in calls))
        self.assertEqual(progress[0], (3, 0))

    def test_scheduler_passes_pose_path(self):
        src = (ROOT / "server" / "app_parts" / "04-scheduler-app.py").read_text(encoding="utf-8")
        self.assertIn('extra_env["POSE_SEQUENCE_PATH"]', src)
        sub = (ROOT / "server" / "app_parts" / "10-job-submit.py").read_text(encoding="utf-8")
        self.assertIn('"pose_sequence_filename": pose_sequence_name', sub)


if __name__ == "__main__":
    unittest.main()
