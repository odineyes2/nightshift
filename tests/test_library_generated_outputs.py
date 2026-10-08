"""NS-56-3 자동 등록: 임시 DB·PNG만 사용하며 갤러리 보존과 재시도를 검사한다."""
import io
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock
from concurrent.futures import ThreadPoolExecutor

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for key in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{key}_DIR"] = str(Path(TMP.name) / key.lower())
sys.path.insert(0, str(ROOT / "server"))

from PIL import Image
import auth
import db
import assets_index as A
import library_generated_outputs as G
import pose_library as P
import position_library as Q
import lighting_library as L


def png():
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), "red").save(buf, "PNG")
    return buf.getvalue()


class GeneratedOutputsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.user = auth.register("generated-user", "generated@example.test", "Test-Passw0rd-xyz!")["id"]
        cls.other = auth.register("generated-other", "other@example.test", "Test-Passw0rd-xyz!")["id"]
        with db.connect() as conn:
            conn.execute("UPDATE users SET status='active'")

    def setUp(self):
        self.pose = P.add_pose(self.user, "포즈", "", "", [(png(), "")])
        self.position = Q.add_position(self.user, "위치", "", "", [(png(), "")])
        self.jid = f"job-{uuid.uuid4().hex}"
        self.origins = [self.origin(self.pose, "pose"), self.origin(self.position, "position")]
        self.job = {"id": self.jid, "owner_id": self.user, "status": "running",
                    "options": {"seed_mode": "sequential", "seed_count": 2},
                    "library_generation_context": self.origins,
                    "library_output_origins": {"save_node_id": "1", "executions": {
                        str(i): self.origins[(i - 1) // 2] for i in range(1, 5)}}}
        self.save_job()
        G._warned.clear()

    def origin(self, article, kind):
        return {"source_kind": kind, "article_id": article["id"], "source_image_id": article["images"][0]["id"],
                "owner_id": str(self.user), "generation_mode": "prompt", "selection_index": 0}

    def save_job(self):
        jobs = db.load_jobs()
        jobs[self.jid] = self.job
        db.save_jobs(jobs)

    def output(self, index=1, batch=1, data=None, name=None):
        path = Path(A.OUTPUT_DIR) / self.jid / (name or f"seed_batch_{index}_seed{index-1}_{batch:05d}_.png")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png() if data is None else data)
        return path

    def sync(self):
        return A.sync(force=True)

    def count(self, kind="pose"):
        return len((P.get_pose(self.pose["id"]) if kind == "pose" else Q.get_position(self.position["id"]))["images"])

    def record(self, path):
        with db.connect() as conn:
            return dict(conn.execute("SELECT * FROM library_generated_outputs WHERE path=?",
                                     (path.relative_to(Path(A.OUTPUT_DIR)).as_posix(),)).fetchone())

    def test_partial_multi_seed_batch_and_gallery(self):
        self.output(1, 1)
        self.sync()
        self.assertEqual((self.count(), self.count("position")), (2, 1))
        for index in range(1, 5):
            for batch in range(1, 4):
                self.output(index, batch)
        self.output(name="unexpected.png")
        self.sync()
        self.assertEqual((self.count(), self.count("position")), (7, 7))
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM assets WHERE job_id=? AND deleted_at IS NULL",
                                          (self.jid,)).fetchone()[0], 13)

    def test_lighting_registration_once_and_deleted_source(self):
        lighting = L.add_lighting(self.user, "조명", "", "soft lighting", [(png(), "")])
        origin = self.origin(lighting, "lighting")
        self.job["library_generation_context"] = [origin]
        self.job["library_output_origins"]["executions"] = {"1": origin, "2": origin}
        self.save_job()
        path = self.output()
        self.sync()
        self.sync()
        self.assertEqual(len(L.get_lighting(lighting["id"])["images"]), 2)
        record = self.record(path)
        self.assertEqual((record["source_kind"], record["article_id"], record["status"]),
                         ("lighting", lighting["id"], "registered"))
        self.assertEqual(record["image_id"], L.get_lighting(lighting["id"])["images"][-1]["id"])
        L.delete_lighting(L.get_lighting(lighting["id"]))
        deleted_path = self.output(2)
        self.sync()
        self.sync()
        self.assertEqual((self.record(deleted_path)["status"], self.record(deleted_path)["reason"]),
                         ("skipped", "source_deleted"))

    def test_lighting_openpose_origin_rejected(self):
        origin = {**self.origins[0], "source_kind": "lighting", "generation_mode": "openpose"}
        self.job["library_generation_context"] = [origin]
        self.job["library_output_origins"]["executions"] = {"1": origin}
        self.assertEqual(G.match_path(self.job, f"{self.jid}/seed_batch_1_seed0_00001_.png"),
                         (None, "origin_mismatch"))

    def test_repeat_restart_redownload_and_user_delete(self):
        path = self.output()
        self.sync()
        image = P.get_pose(self.pose["id"])["images"][-1]
        self.sync()
        db._initialized = False
        db.load_jobs()
        self.sync()
        path.unlink()
        self.sync()
        self.output()
        self.sync()
        self.assertEqual(self.count(), 2)
        P.delete_image(P.get_pose(self.pose["id"]), image["id"])
        self.sync()
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.record(path)["status"], "registered")

    def test_corrupt_image_retry_keeps_gallery(self):
        path = self.output(data=b"broken png")
        self.sync()
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.record(path)["status"], "retry")
        with db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT deleted_at FROM assets WHERE path=?",
                              (path.relative_to(Path(A.OUTPUT_DIR)).as_posix(),)).fetchone()[0])
        self.output()
        self.sync()
        self.assertEqual(self.count(), 2)

    def test_copy_and_thumbnail_failure_retry_cleanup(self):
        path = self.output()
        before = set(P._dir().iterdir())
        original = Path.write_bytes
        def fail(file, data):
            if file.parent == P._dir():
                raise OSError("fixture copy failure")
            return original(file, data)
        with mock.patch.object(Path, "write_bytes", fail):
            self.sync()
        self.assertEqual(self.record(path)["status"], "retry")
        self.assertEqual(set(P._dir().iterdir()), before)
        with mock.patch.object(Image.Image, "save", side_effect=OSError("fixture thumbnail failure")):
            self.sync()
        self.assertEqual(self.count(), 1)
        self.assertEqual(set(P._dir().iterdir()), before)
        self.sync()
        self.assertEqual(self.count(), 2)

    def test_deleted_source_and_permission_mismatch(self):
        path = self.output()
        P.delete_pose(P.get_pose(self.pose["id"]))
        self.sync()
        self.assertEqual(self.record(path)["reason"], "source_deleted")
        pospath = self.output(3)
        with db.connect() as conn:
            conn.execute("UPDATE positions SET owner_id=? WHERE id=?", (self.other, self.position["id"]))
        self.sync()
        self.assertEqual(self.count("position"), 1)
        self.assertEqual(self.record(pospath)["reason"], "permission_mismatch")

    def test_job_owner_asset_owner_source_image_and_admin(self):
        path = self.output()
        self.job["owner_id"] = self.other
        self.save_job()
        self.sync()
        self.assertEqual(self.record(path)["reason"], "permission_mismatch")
        with db.connect() as conn:
            conn.execute("UPDATE users SET role='admin' WHERE id=?", (self.other,))
        self.sync()
        self.assertEqual(self.count(), 2)
        self.output(2)
        self.sync()
        self.assertEqual(self.count(), 3)
        # 미등록 결과의 소유자를 바꾸어 갤러리 권한도 대조한다.
        path3 = self.output(3)
        with mock.patch.object(G, "sync"):
            self.sync()
        with db.connect() as conn:
            conn.execute("UPDATE assets SET owner_id=? WHERE path=?", (self.user, path3.relative_to(Path(A.OUTPUT_DIR)).as_posix()))
        self.sync()
        self.assertEqual(self.record(path3)["reason"], "permission_mismatch")

    def test_legacy_wrong_name_mapping_and_source_image_deleted(self):
        path = self.output()
        self.job.pop("library_generation_context")
        self.save_job()
        self.sync()
        self.assertEqual(self.count(), 1)
        self.job["library_generation_context"] = self.origins
        self.save_job()
        for name in ("seed_batch_5_seed4_00001_.png", "seed_batch_1_seed8_00001_.png", "seed_batch_1_seed0_00000_.png"):
            self.assertIsNone(G.origin_for_path(self.job, f"{self.jid}/{name}"))
        P.add_images(self.pose["id"], [(png(), "")])
        P.delete_image(P.get_pose(self.pose["id"]), self.origins[0]["source_image_id"])
        self.sync()
        self.assertEqual(self.record(path)["reason"], "source_image_deleted")

    def test_both_generation_modes_and_position_user_delete(self):
        self.origins[0]["generation_mode"] = "openpose"
        self.origins[1]["generation_mode"] = "openpose"
        self.save_job()
        self.output(1)
        path = self.output(3)
        self.sync()
        self.assertEqual((self.count(), self.count("position")), (2, 2))
        Q.delete_image(Q.get_position(self.position["id"]), self.record(path)["image_id"])
        self.sync()
        self.assertEqual(self.count("position"), 1)

    def test_relation_failure_and_concurrent_duplicate(self):
        path = self.output()
        before = set(P._dir().iterdir())
        record = G._record
        def fail(conn, path, origin, status, *args, **kwargs):
            if status == "registered":
                raise RuntimeError("fixture relation failure")
            return record(conn, path, origin, status, *args, **kwargs)
        with mock.patch.object(G, "_record", fail):
            self.sync()
        self.assertEqual(self.count(), 1)
        self.assertEqual(set(P._dir().iterdir()), before)
        self.assertEqual(self.record(path)["status"], "retry")
        relative = path.relative_to(Path(A.OUTPUT_DIR)).as_posix()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: G._register(Path(A.OUTPUT_DIR), relative), range(2)))
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.record(path)["status"], "registered")

    # NS-56-5: 누락 사유를 작업 ID·사유 코드로 구분하고 일반 작업에는 경고하지 않는다.
    def logs(self):
        """이 작업의 경고만 모은다(다른 검사 작업의 경고는 제외)."""
        with self.assertLogs(G.log, "WARNING") as cm:
            G.log.warning("marker")
            self.sync()
        return [m.split(":", 2)[2] for m in cm.output if self.jid in m]

    def reason(self, code, extra="registration"):
        return f"라이브러리 자동 {'등록 누락' if extra == 'registration' else extra}: job={self.jid} reason={code}"

    def test_normal_job_no_warning_and_registered_quiet(self):
        self.job.pop("library_generation_context")
        self.job.pop("library_output_origins")
        self.save_job()
        self.output()
        self.output(name="unexpected.png")
        self.assertEqual(self.logs(), [])
        self.job["library_generation_context"] = self.origins
        self.job["library_output_origins"] = {"save_node_id": "1", "executions": {"1": self.origins[0]}}
        self.save_job()
        self.output(name="other.png")
        self.assertEqual(self.logs(), [self.reason("output_name_mismatch")])
        self.assertEqual(self.count(), 2)

    def test_missing_origin_mapping_and_name_reasons_logged_once(self):
        self.job.pop("library_generation_context")
        self.save_job()
        self.output()
        self.assertEqual(self.logs(), [self.reason("missing_origin")])
        self.assertEqual(self.logs(), [])  # 반복 색인에서 같은 진단을 되풀이하지 않는다.
        self.job["library_generation_context"] = self.origins
        self.job["library_output_origins"] = {}
        self.save_job()
        self.assertEqual(self.logs(), [self.reason("no_save_node_mapping")])
        self.job["library_output_origins"] = {"save_node_id": "1", "executions": {"1": self.origins[0]}}
        self.save_job()
        self.output(3)
        self.output(name="seed_batch_1_seed7_00001_.png")
        self.assertEqual(sorted(self.logs()), [self.reason("execution_unmapped"), self.reason("output_name_mismatch")])
        self.assertEqual(self.count(), 2)
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM assets WHERE job_id=? AND deleted_at IS NULL",
                                          (self.jid,)).fetchone()[0], 3)

    def test_source_deleted_permission_and_copy_failure_reasons(self):
        self.output()
        pospath = self.output(3)
        P.delete_pose(P.get_pose(self.pose["id"]))
        with db.connect() as conn:
            conn.execute("UPDATE positions SET owner_id=? WHERE id=?", (self.other, self.position["id"]))
        self.assertEqual(sorted(self.logs()), [self.reason("permission_mismatch"), self.reason("source_deleted")])
        self.assertEqual(self.record(pospath)["status"], "skipped")
        with db.connect() as conn:
            conn.execute("UPDATE positions SET owner_id=? WHERE id=?", (self.user, self.position["id"]))
        original = Path.write_bytes
        def fail(file, data):
            if file.parent == Q._dir():
                raise OSError("fixture copy failure")
            return original(file, data)
        with mock.patch.object(Path, "write_bytes", fail):
            self.assertEqual(self.logs(), [self.reason("copy_failed:OSError", "등록 실패")])
        self.assertEqual(self.record(pospath)["status"], "retry")
        self.assertEqual(self.logs(), [])
        self.assertEqual(self.count("position"), 2)


if __name__ == "__main__":
    unittest.main()
