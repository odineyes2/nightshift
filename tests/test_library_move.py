"""NS-46-2 Library Pose↔Position 게시물 이동 — 내용·이미지 순서·파일, 원본 삭제, 남의 게시물 404, 21장 이상. 임시 데이터 폴더만 쓴다."""
import asyncio
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
os.environ.setdefault("NIGHTSHIFT_ADMIN_USER", "admin")
os.environ.setdefault("NIGHTSHIFT_ADMIN_PASSWORD", "Test-Passw0rd-xyz!")
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")

import app as A
import pose_library
import position_library
from fastapi import HTTPException
from PIL import Image


def png(w: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, 100), (50, 100, 200)).save(buf, "PNG")
    return buf.getvalue()


def req(uid="u1", role="user"):
    return SimpleNamespace(state=SimpleNamespace(user={"id": uid, "role": role}))


def call(fn, *args):
    try:
        out = fn(*args)
        return 200, asyncio.run(out) if asyncio.iscoroutine(out) else out
    except HTTPException as e:
        return e.status_code, e.detail


def widths(item, path_fn) -> list:
    return [pose_library.image_size(path_fn(i, False).read_bytes())[0] for i in item["images"]]


class MoveTest(unittest.TestCase):
    def test_pose_to_position_and_back(self):
        pose = pose_library.add_pose("u1", "정면", "설명", "from_front", [(png(101), "https://a/1"), (png(102), "")])
        old_files = [pose_library.image_path(i, t) for i in pose["images"] for t in (False, True)]
        code, pos = call(A.move_library_pose_to_position, req(), pose["id"])
        self.assertEqual(code, 200, pos)
        self.assertEqual((pos["name"], pos["description"], pos["danbooru_prompt"], pos["owner_id"]),
                         ("정면", "설명", "from_front", "u1"))
        self.assertEqual(widths(pos, position_library.image_path), [101, 102])
        self.assertEqual([i["source_url"] for i in pos["images"]], ["https://a/1", ""])
        for i in pos["images"]:
            self.assertTrue(position_library.image_path(i, True).is_file())
        self.assertIsNone(pose_library.get_pose(pose["id"]))
        self.assertFalse(any(f.exists() for f in old_files))

        code, back = call(A.move_library_position_to_pose, req(), pos["id"])
        self.assertEqual(code, 200, back)
        self.assertEqual((back["name"], back["danbooru_prompt"], back["owner_id"]), ("정면", "from_front", "u1"))
        self.assertEqual(widths(back, pose_library.image_path), [101, 102])
        self.assertEqual(back["image_file"], back["images"][0]["image_file"])   # 옛 코드용 첫 장 칸
        self.assertEqual(back["source_url"], "https://a/1")
        self.assertIsNone(position_library.get_position(pos["id"]))

    def test_others_post_is_404(self):
        pose = pose_library.add_pose("u1", "남의 것", "", "", [(png(110), "")])
        self.assertEqual(call(A.move_library_pose_to_position, req("u2"), pose["id"])[0], 404)
        pos = position_library.add_position("u1", "남의 것", "", "", [(png(110), "")])
        self.assertEqual(call(A.move_library_position_to_pose, req("u2"), pos["id"])[0], 404)
        self.assertIsNotNone(pose_library.get_pose(pose["id"]))
        self.assertIsNotNone(position_library.get_position(pos["id"]))
        self.assertEqual(call(A.move_library_pose_to_position, req("admin", "admin"), pose["id"])[0], 200)

    def test_more_than_20_images(self):
        pose = pose_library.add_pose("u1", "많음", "", "", [(png(200 + i), "") for i in range(20)])
        pose = pose_library.add_images(pose["id"], [(png(220), "")])
        code, pos = call(A.move_library_pose_to_position, req(), pose["id"])
        self.assertEqual(code, 200, pos)
        self.assertEqual(widths(pos, position_library.image_path), list(range(200, 221)))

    def test_failure_keeps_source(self):
        pose = pose_library.add_pose("u1", "깨짐", "", "", [(png(120), ""), (png(121), "")])
        pose_library.image_path(pose["images"][1], False).write_bytes(b"not an image")
        before = set(position_library._dir().iterdir())
        self.assertEqual(call(A.move_library_pose_to_position, req(), pose["id"])[0], 400)
        self.assertIsNotNone(pose_library.get_pose(pose["id"]))
        self.assertEqual(set(position_library._dir().iterdir()), before)


if __name__ == "__main__":
    unittest.main()
