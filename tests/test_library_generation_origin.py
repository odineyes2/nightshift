"""NS-56-2 생성원 검증·저장·결과 매핑. 임시 DB와 모의 큐만 사용한다."""
import asyncio
import copy
import json
import unittest
from unittest import mock

from test_pose_sequence import A, P, SB, png
import position_library as Q
import lighting_library as L
from fastapi import HTTPException


class GenerationOriginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.account = A.auth.register("origin-user", "origin@example.test", "Test-Passw0rd-xyz!")

    def setUp(self):
        self.pose = P.add_pose("u1", "포즈", "", "sitting", [(png(), ""), (png(), "")])
        self.position = Q.add_position("u1", "위치", "", "standing", [(png(), "")])
        self.user = {"id": "u1", "role": "user"}
        self.workflow = {"1": {"class_type": "SaveImage", "inputs": {}},
                         "2": {"class_type": "SaveImage", "inputs": {}},
                         "3": {"class_type": "EmptyLatentImage", "inputs": {"batch_size": 3}}}

    def origin(self, article=None, kind="pose", mode="prompt"):
        article = article or self.pose
        item = {"source_kind": kind, "article_id": article["id"],
                "source_image_id": article["images"][0]["id"], "generation_mode": mode,
                "danbooru_prompt": article["danbooru_prompt"]}
        if mode == "openpose":
            lib = P if kind == "pose" else Q
            name, data = lib.input_copy(article, article["images"][0])
            item["name"] = A.save_input_image(name, data)
        return item

    def create(self, origins=None, sequence=None, extra=None):
        options = {"seed_count": 2, "seed_mode": "sequential", **(extra or {})}
        if origins is not None:
            options["library_generation_context"] = json.dumps(origins)
        if sequence is not None:
            options["pose_sequence"] = json.dumps(sequence)
        with mock.patch.object(A, "poke_scheduler"), mock.patch.object(A, "save_state"):
            return asyncio.run(A.create_job(A.resolve_template("seed_batch"),
                json.dumps(self.workflow).encode(), "wf.json", None, None, options,
                user=self.user, start_paused=True))

    def test_access_and_image_membership(self):
        item = self.origin()
        with self.assertRaises(HTTPException) as e:
            A.parse_library_generation_context([item], {"id": "u2", "role": "user"})
        self.assertEqual(e.exception.status_code, 404)
        self.assertEqual(A.parse_library_generation_context([item], {"id": "admin", "role": "admin"})[0]["owner_id"], "u1")
        other = P.add_pose("u1", "다른 포즈", "", "", [(png(), "")])
        for change in ({"source_image_id": other["images"][0]["id"]}, {"article_id": True},
                       {"source_kind": "unknown"}, {"generation_mode": "unknown"}):
            with self.assertRaises(HTTPException):
                A.parse_library_generation_context([{**item, **change}], self.user)

    def test_position_cn_copy_validation(self):
        item = self.origin(self.position, "position", "openpose")
        context = A.parse_library_generation_context([item], self.user)
        self.assertEqual((context[0]["source_kind"], context[0]["generation_mode"]), ("position", "openpose"))
        with self.assertRaises(HTTPException):
            A.parse_library_generation_context([{**item, "name": "unrelated.png"}], self.user)
        A.resolve_input_image(item["name"]).write_bytes(png((32, 32)))
        with self.assertRaises(HTTPException):
            A.parse_library_generation_context([item], self.user)

    def test_lighting_prompt_only_and_access(self):
        lighting = L.add_lighting("u1", "조명", "", "soft lighting", [(png(), "")])
        item = self.origin(lighting, "lighting")
        job = self.create([item])
        origin = job["library_generation_context"][0]
        self.assertEqual((origin["source_kind"], origin["generation_mode"]), ("lighting", "prompt"))
        self.assertEqual(job["library_output_origins"]["executions"]["1"], origin)
        for changed, user, status in (
            ({**item, "generation_mode": "openpose"}, self.user, 400),
            (item, {"id": "u2", "role": "user"}, 404),
        ):
            with self.assertRaises(HTTPException) as e:
                A.parse_library_generation_context([changed], user)
            self.assertEqual(e.exception.status_code, status)
        self.assertEqual(A.parse_library_generation_context([item], {"id": "admin", "role": "admin"})[0]["owner_id"], "u1")

    def test_single_cn_and_same_article_sequence(self):
        item = self.origin(self.position, "position", "openpose")
        job = self.create([item], extra={"pose_image": item["name"]})
        self.assertEqual(job["library_generation_context"][0]["source_kind"], "position")
        first = self.origin()
        second = {**first, "source_image_id": self.pose["images"][1]["id"]}
        sequence = [{**o, "image": "", "tags": "sitting"} for o in (first, second)]
        job = self.create([first, second], sequence)
        self.assertEqual(job["library_output_origins"]["executions"]["3"]["source_image_id"], second["source_image_id"])

    def test_single_save_restore_and_legacy(self):
        job = self.create([self.origin()])
        job["owner_id"] = self.account["id"]
        A.db.save_jobs({job["id"]: job})
        restored = A.db.load_jobs()[job["id"]]
        self.assertEqual(restored["library_generation_context"], job["library_generation_context"])
        self.assertEqual(restored["library_output_origins"], job["library_output_origins"])
        self.assertEqual(job["library_generation_context"][0]["source_kind"], "pose")
        self.assertNotIn("library_generation_context", job["options"])
        self.assertIsNone(self.create()["library_output_origins"])
        self.assertIsNone(A.library_origin_for_output({"id": "old"}, "seed_batch_1_seed0_00001_.png", "old"))

    def test_sequence_and_all_batch_filenames(self):
        origins = [self.origin(), self.origin(self.position, "position")]
        sequence = [{**o, "image": "", "tags": o["danbooru_prompt"]} for o in origins]
        job = self.create(origins, sequence)
        parsed = A.parse_pose_sequence(sequence)
        self.assertEqual([p["source_kind"] for p in parsed], ["pose", "position"])
        for index in range(1, 5):
            wf = copy.deepcopy(self.workflow)
            with mock.patch.dict(SB.os.environ, {"JOB_ID": job["id"]}), \
                 mock.patch.object(SB, "queue_prompt", return_value="mock-id") as queue, \
                 mock.patch.object(SB, "wait_for_completion"), \
                 mock.patch.object(SB, "apply_pose_image"):
                SB.run_once(wf, "mock", index - 1, index, "prompt", "", "", "")
            submitted = queue.call_args.args[1]
            prefix = submitted["1"]["inputs"]["filename_prefix"]
            folder, name = prefix.split("/")
            for counter in range(1, 4):
                origin = A.library_origin_for_output(job, f"{name}_{counter:05d}_.png", folder, "1")
                self.assertEqual(origin["source_kind"], "pose" if index <= 2 else "position")
            self.assertIsNone(A.library_origin_for_output(job, f"{name}_00001_.png", folder, "2"))
        for filename in ("seed_batch_5_seed4_00001_.png", "other.png", "seed_batch_1_seed0_00001_.jpg",
                         "seed_batch_1_seed8_00001_.png", "seed_batch_1_seed0_00000_.png"):
            self.assertIsNone(A.library_origin_for_output(job, filename, job["id"]))
        self.assertIsNone(A.library_origin_for_output(job, "seed_batch_1_seed0_00001_.png", "another-job"))

    def test_context_must_match_execution_input(self):
        item = self.origin()
        with self.assertRaises(HTTPException):
            self.create([item, item])
        with self.assertRaises(HTTPException):
            self.create([item], [{**item, "image": "", "tags": "", "source_image_id": self.pose["images"][1]["id"]}])
        with self.assertRaises(HTTPException):
            self.create([item], extra={"pose_image": "unrelated.png"})
        self.workflow = {"1": {"class_type": "SaveImageWebsocket", "inputs": {}}}
        self.assertEqual(self.create([item])["library_output_origins"]["executions"], {})


if __name__ == "__main__":
    unittest.main()
