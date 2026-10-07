"""NS-45-1 갤러리 결과 이미지(output_name)로 Library Pose·Position 추가·덧붙이기 — 권한·경로·장 수 검사. 임시 데이터 폴더만 쓴다."""
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
import assets_index
from fastapi import HTTPException
from PIL import Image
from data_paths import data_dir
from starlette.datastructures import FormData, UploadFile

OUT = Path(os.environ["NIGHTSHIFT_OUTPUT_DIR"])
with A.db.connect() as _conn:   # 결과물 색인의 주인(외래 키) — 회원 1·2
    U1, U2 = (_conn.execute("INSERT INTO users(username, email, password_hash, role, status, created_at) "
                            "VALUES(?,?,'x','user','active','x')", (n, f"{n}@x")).lastrowid for n in ("u1", "u2"))


def png(size=(100, 80)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (50, 100, 200)).save(buf, "PNG")
    return buf.getvalue()


def output(name: str, owner: int) -> str:
    """결과 이미지 하나를 만들고 색인에 주인과 함께 넣는다."""
    f = OUT / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(png())
    assets_index.register_upload(name, owner, None)
    return name


def req(uid, role="user", form=()):
    user = {"id": uid, "role": role}
    A.auth.current_user.set(user)   # 미들웨어가 하는 일 — resolve_output_image가 주인을 본다

    async def read_form():
        return FormData(list(form))
    return SimpleNamespace(state=SimpleNamespace(user=user), form=read_form)


def call(fn, *args):
    try:
        out = fn(*args)
        return 200, asyncio.run(out) if asyncio.iscoroutine(out) else out
    except HTTPException as e:
        return e.status_code, e.detail


def files(kind):
    d = data_dir(f"library/{kind}")
    return set(d.iterdir()) if d.exists() else set()


class FromGallery(unittest.TestCase):
    def test_new_pose_from_two_outputs(self):
        names = [output("j1/a.png", U1), output("j1/b.png", U1)]
        form = [("name", "갤러리 포즈")] + [("output_name", n) for n in names]
        code, item = call(A.add_library_pose, req(U1, form=form))
        self.assertEqual(code, 200, item)
        self.assertEqual(len(item["images"]), 2)
        for n in names:   # 원본은 그대로 남는다
            self.assertTrue((OUT / n).is_file())

    def test_append_to_position(self):
        form = [("name", "정면"), ("output_name", output("j2/a.png", U1))]
        code, item = call(A.add_library_position, req(U1, form=form))
        self.assertEqual(code, 200, item)
        form = [("output_name", output("j2/b.png", U1))]
        # admin으로 덧붙인다 — owner_id 칸이 TEXT라 일반 회원의 int id는 can_access에서 어긋난다(별도 이슈)
        code, item = call(A.add_library_position_images, req(U2, A.auth.ROLE_ADMIN, form=form), item["id"])
        self.assertEqual(code, 200, item)
        self.assertEqual(len(item["images"]), 2)

    def test_others_output_404_admin_ok(self):
        name = output("j3/other.png", U2)
        before = files("poses")
        code, _ = call(A.add_library_pose, req(U1, form=[("name", "x"), ("output_name", name)]))
        self.assertEqual(code, 404)
        self.assertEqual(files("poses"), before)
        code, item = call(A.add_library_pose, req(99, A.auth.ROLE_ADMIN, form=[("name", "x"), ("output_name", name)]))
        self.assertEqual(code, 200, item)

    def test_bad_names(self):
        code, _ = call(A.add_library_pose, req(U1, form=[("name", "x"), ("output_name", "../x.png")]))
        self.assertEqual(code, 400)
        code, _ = call(A.add_library_pose, req(U1, form=[("name", "x"), ("output_name", "j4/none.png")]))
        self.assertEqual(code, 404)
        (OUT / "j4").mkdir(parents=True, exist_ok=True)
        (OUT / "j4/v.mp4").write_bytes(b"x")
        code, _ = call(A.add_library_pose, req(U1, form=[("name", "x"), ("output_name", "j4/v.mp4")]))
        self.assertEqual(code, 404)

    def test_limit_counts_all_inputs(self):
        names = [output(f"j5/{i}.png", U1) for i in range(20)]
        form = [("name", "x"), ("image", UploadFile(file=io.BytesIO(png()), filename="p.png"))]
        form += [("output_name", n) for n in names]
        code, _ = call(A.add_library_pose, req(U1, form=form))
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
