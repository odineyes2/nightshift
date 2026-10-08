"""NS-56-4 실제 /api/upload multipart 경계에서 생성원이 보존되어 원본 아티클에 자동 등록되는지 검사한다.
임시 데이터 폴더만 쓰고 스케줄러·ComfyUI·네트워크는 모의 처리한다(lifespan을 띄우지 않는다)."""
import json
import unittest
import uuid
from pathlib import Path
from unittest import mock

from test_pose_sequence import A, P, png
import assets_index
import db
import position_library as Q
from fastapi.testclient import TestClient

H = {"X-Requested-With": "nightshift"}
PASSWORD = "Test-Passw0rd-xyz!"
PROMPT_WF = {"1": {"class_type": "SaveImage", "inputs": {}}}
OPENPOSE_WF = {**PROMPT_WF, "2": {"class_type": "DWPreprocessor", "inputs": {}}}


def client(name):
    user = A.auth.register(name, f"{name}@example.test", PASSWORD)
    with db.connect() as conn:
        conn.execute("UPDATE users SET status='active' WHERE id=?", (user["id"],))
    c = TestClient(A.app)
    assert c.post("/api/auth/login", json={"username": name, "password": PASSWORD}, headers=H).status_code == 200
    return c, user


class LibraryGenerationUploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c, cls.user = client(f"upload-{uuid.uuid4().hex[:8]}")
        cls.other, _ = client(f"other-{uuid.uuid4().hex[:8]}")
        cls.uid = cls.user["id"]

    def setUp(self):
        patches = [mock.patch.object(A, "poke_scheduler"), mock.patch.object(A, "dispatch_job")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # 입력 이미지는 회원별 폴더라 CN 사본을 이 회원 폴더에 둔다.
        token = A.auth.current_user.set(self.user)
        self.addCleanup(A.auth.current_user.reset, token)

    def article(self, kind):
        if kind == "pose":
            return P.add_pose(self.uid, "포즈", "", "sitting", [(png((64, 64)), "")])
        return Q.add_position(self.uid, "위치", "", "standing", [(png((64, 64)), "")])

    def count(self, kind, article):
        lib = P.get_pose if kind == "pose" else Q.get_position
        return len(lib(article["id"])["images"])

    def origin(self, kind, mode, article):
        item = {"source_kind": kind, "article_id": article["id"], "source_image_id": article["images"][0]["id"],
                "generation_mode": mode, "danbooru_prompt": article["danbooru_prompt"], "name": ""}
        if mode == "openpose":
            lib = P if kind == "pose" else Q
            name, data = lib.input_copy(article, article["images"][0])
            item["name"] = A.save_input_image(name, data)
        return item

    def upload(self, fields, mode="prompt", c=None):
        wf = OPENPOSE_WF if mode == "openpose" else PROMPT_WF
        data = {"template_id": "seed_batch", "seed_count": "1", "seed_mode": "sequential",
                "start_paused": "1", **fields}
        return (c or self.c).post("/api/upload", data=data, headers=H,
                                  files={"workflow": ("wf.json", json.dumps(wf), "application/json")})

    def single_fields(self, item):
        return {"library_generation_context": json.dumps([item]), "pose_image": item["name"]}

    def sequence_fields(self, items):
        return {"pose_sequence": json.dumps([{**{k: o[k] for k in ("source_kind", "article_id", "source_image_id",
                                                                    "generation_mode")},
                                              "image": o["name"], "tags": o["danbooru_prompt"]} for o in items])}

    def register_outputs(self, job_id, executions):
        # 저장/복원 뒤 seed_batch 실행기의 이름 규칙대로 모의 PNG를 두고 색인한다.
        db._initialized = False
        restored = db.load_jobs()[job_id]
        out = Path(assets_index.OUTPUT_DIR) / job_id
        out.mkdir(parents=True, exist_ok=True)
        for index in executions:
            (out / f"seed_batch_{index}_seed{index - 1}_00001_.png").write_bytes(png((32, 24)))
        assets_index.sync(force=True)
        return restored

    def test_single_and_sequence_register_to_source_article(self):
        for kind in ("pose", "position"):
            for mode in ("prompt", "openpose"):
                with self.subTest(kind=kind, mode=mode, flow="single"):
                    article = self.article(kind)
                    r = self.upload(self.single_fields(self.origin(kind, mode, article)), mode)
                    self.assertEqual(r.status_code, 200, r.text)
                    job = r.json()
                    self.assertEqual(job["library_generation_context"][0]["source_kind"], kind)
                    restored = self.register_outputs(job["id"], [1])
                    self.assertEqual(restored["library_output_origins"], job["library_output_origins"])
                    self.assertEqual(self.count(kind, article), 2)
                with self.subTest(kind=kind, mode=mode, flow="sequence"):
                    first, second = self.article(kind), self.article(kind)
                    r = self.upload(self.sequence_fields([self.origin(kind, mode, a) for a in (first, second)]), mode)
                    self.assertEqual(r.status_code, 200, r.text)
                    self.register_outputs(r.json()["id"], [1, 2])
                    self.assertEqual((self.count(kind, first), self.count(kind, second)), (2, 2))

    def test_rejects_wrong_id_ownership_and_cn_copy(self):
        article = self.article("pose")
        item = self.origin("pose", "prompt", article)
        for bad in ({**item, "article_id": 999999}, {**item, "source_image_id": 999999}):
            self.assertIn(self.upload(self.single_fields(bad)).status_code, (400, 404))
        self.assertEqual(self.upload(self.single_fields(item), c=self.other).status_code, 404)
        cn = self.origin("position", "openpose", self.article("position"))
        r = self.upload({**self.single_fields(cn), "pose_image": "unrelated.png"}, "openpose")
        self.assertEqual(r.status_code, 400)
        A.resolve_input_image(cn["name"]).write_bytes(png((16, 16)))
        self.assertEqual(self.upload(self.single_fields(cn), "openpose").status_code, 400)

    def test_plain_job_unchanged(self):
        r = self.upload({})
        self.assertEqual(r.status_code, 200, r.text)
        job = r.json()
        self.assertIsNone(job.get("library_generation_context"))
        self.assertIsNone(job.get("library_output_origins"))
        self.assertNotIn("library_generation_context", job["options"])


if __name__ == "__main__":
    unittest.main()
