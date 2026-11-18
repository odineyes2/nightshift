"""서비스 연동 계정이 회원 대신 보내는 요청(X-On-Behalf-Of) 검사 — 작업·결과물이 그 회원 소유가 되고, 그 회원 파드에서만 돌며,
"<서비스> 삽화" 프로젝트에 모인다. 가짜 데이터만 쓴다. 실행: python tests/test_service_on_behalf.py"""
import asyncio
import base64
import io
import os
import secrets
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory()
for part in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{part}_DIR"] = str(Path(TMP.name) / part.lower())
os.environ.pop("NIGHTSHIFT_INPUT_IMAGES_DIR", None)
PASSWORD = secrets.token_urlsafe(18)
os.environ["NIGHTSHIFT_ADMIN_USER"] = "admin"
os.environ["NIGHTSHIFT_ADMIN_PASSWORD"] = PASSWORD
sys.path.insert(0, str(ROOT / "server"))
os.chdir(ROOT / "server")

import app as A  # noqa: E402
import db  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ST = A.service_tokens
A.save_state = lambda: None
A.poke_scheduler = lambda: None


class OnBehalfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()
        A.auth.ensure_admin()
        cls.admin_id = A.auth.admin_id()
        cls.member_id = A.auth.register("reader", "reader@example.test", PASSWORD)["id"]
        cls.pending_id = A.auth.register("waiting", "waiting@example.test", PASSWORD)["id"]
        with db.connect() as conn:
            conn.execute("UPDATE users SET status='active' WHERE id=?", (cls.member_id,))
        issued = ST.connect("charchat")
        cls.token, cls.bot = issued["token"], A.auth.get_user(issued["user_id"])
        cls.other_bot = A.auth.get_user(ST.connect("moai")["user_id"])
        cls.client = TestClient(A.app)

    def call(self, path, behalf=None):
        headers = {"Authorization": "Bearer " + self.token}
        if behalf is not None:
            headers["X-On-Behalf-Of"] = str(behalf)
        return self.client.get(path, headers=headers)

    def test_acting_user_rules(self):
        self.assertEqual(ST.acting_user(self.bot, str(self.member_id))["id"], self.member_id)
        admin = ST.acting_user(self.bot, str(self.admin_id))
        self.assertEqual((admin["id"], admin["role"]), (self.admin_id, "user"))   # 관리자 권한은 싣지 않는다
        for raw in (str(self.pending_id), "999999", "abc", "", "-1", str(self.other_bot["id"]), str(self.bot["id"])):
            self.assertIsNone(ST.acting_user(self.bot, raw), raw)
        member = A.auth.get_user(self.member_id)
        self.assertIsNone(ST.acting_user(member, str(self.admin_id)))   # 연동 계정이 아니면 대신 보낼 수 없다

    def test_header_through_middleware(self):
        self.assertEqual(self.call("/api/generate/auto-power").status_code, 200)
        self.assertEqual(self.call("/api/generate/auto-power", self.member_id).status_code, 200)
        self.assertEqual(self.call("/api/generate/auto-power", self.pending_id).status_code, 403)

    def test_billing_tells_own_key_and_paid_only(self):
        # charchat이 회원 대신 묻는다: 자기 RunPod 키가 있는지·유료인지 두 값만(키·한도는 주지 않는다)
        with patch.object(A.runpod_api, "key_for_owner", lambda owner: "rpa_x" if owner == self.member_id else None):
            data = self.call("/api/generate/billing", self.member_id).json()
        self.assertEqual(data, {"own_runpod": True, "paid": False})
        with patch.object(A.runpod_api, "key_for_owner", lambda owner: None):
            self.assertEqual(self.call("/api/generate/billing", self.member_id).json()["own_runpod"], False)
        self.assertEqual(self.call("/api/generate/auto-power", "x").status_code, 403)
        # 쿠키 로그인 요청에는 헤더가 아무 효과가 없다(연동 토큰일 때만 읽는다)
        member = TestClient(A.app)
        member.post("/api/auth/login", json={"username": "reader", "password": PASSWORD},
                    headers={"X-Requested-With": "nightshift"})
        me = member.get("/api/auth/me", headers={"X-On-Behalf-Of": str(self.admin_id)}).json()["user"]
        self.assertEqual(me["id"], self.member_id)

    def test_job_owned_by_member_runs_on_member_pods_and_lands_in_project(self):
        async def make():
            u = A.auth.current_user.set(ST.acting_user(self.bot, str(self.member_id)))
            v = A.auth.current_via_service.set(self.bot["id"])
            try:
                return await A.create_job(A.resolve_template("seed_batch"), b'{"1": {"class_type": "SaveImage", "inputs": {}}}',
                                          "w.json", None, None, {}, user=A.auth.current_user.get(),
                                          parse_inline_options=False)
            finally:
                A.auth.current_via_service.reset(v)
                A.auth.current_user.reset(u)
        job = asyncio.run(make())
        self.assertEqual((job["owner_id"], job["via_service"]), (self.member_id, self.bot["id"]))
        # 회원 대신 보낸 작업도 그 회원의 파드에서만 돈다(각자 자기 RunPod — 회원 워커 자동 켜기는 07d)
        self.assertEqual(ST.pod_owner_ids_for(job), [self.member_id])
        project = A.project_store.get_project(job["project_id"])
        self.assertEqual((project["name"], project["owner_id"]), ("charchat 삽화", self.member_id))
        again = asyncio.run(make())
        self.assertEqual(again["project_id"], job["project_id"])          # 같은 프로젝트를 다시 쓴다
        # 회원이 직접 만든 작업은 관리자 파드에서 돌지 않는다
        self.assertEqual(ST.pod_owner_ids_for({"owner_id": self.member_id}), [self.member_id])
        # 작업 조회는 대신 보낸 회원으로만
        self.assertEqual(self.call(f"/api/jobs/{job['id']}/outputs", self.member_id).status_code, 200)
        self.assertIn(self.call(f"/api/jobs/{job['id']}/outputs").status_code, (403, 404))   # 연동 계정 자신으로는 못 본다

    def test_output_image_ownership(self):
        with db.connect() as conn:
            conn.execute("INSERT INTO assets(path, kind, owner_id, size_bytes, created_at) VALUES(?,?,?,?,?)",
                         ("jobx/a.png", "image", self.member_id, 1, db.now_iso()))
        self.assertEqual(self.call("/api/output-images/jobx/a.png").status_code, 403)     # 연동 계정 것이 아니다
        self.assertNotEqual(self.call("/api/output-images/jobx/a.png", self.member_id).status_code, 403)
        self.assertEqual(self.call("/api/output-images/jobx/a.png", self.admin_id).status_code, 403)

    def test_saved_input_reaches_runner_for_direct_and_service_requests(self):
        # 실제 API 저장 → 실행기 → 템플릿의 경로 해석을 별도 프로세스에서 연결한다.
        # ComfyUI·RunPod·알림은 호출하지 않는다.
        A.model_registry.upsert("checkpoints", "waiIllustriousSDXL_v170.safetensors",
                                {"base_model": "Illustrious"})
        image = io.BytesIO()
        Image.new("RGB", (64, 64), (40, 60, 90)).save(image, "PNG")
        body = {"op": "upscale", "model": "checkpoints:waiIllustriousSDXL_v170.safetensors",
                "prompt": "landscape", "seed": 7, "image": base64.b64encode(image.getvalue()).decode()}
        real_popen = A.subprocess.Popen
        template_path = ROOT / "templates" / "input_image_batch.py"
        probe = (
            "import os,runpy; from pathlib import Path; "
            f"ns=runpy.run_path({str(template_path)!r},run_name='path_probe'); "
            "p=ns['input_images_dir']()/os.environ['INPUT_IMAGE']; "
            "assert p.is_file(), str(p); print('INPUT_FOUND')"
        )
        import notify
        for owner_id in (self.admin_id, self.member_id):
            for delegated in (False, True):
                with self.subTest(owner=owner_id, delegated=delegated), TestClient(A.app) as client:
                    if delegated:
                        headers = {"Authorization": "Bearer " + self.token, "X-On-Behalf-Of": str(owner_id)}
                    else:
                        client.cookies.set(A.auth.SESSION_COOKIE, A.auth.create_session(owner_id))
                        headers = {"X-Requested-With": "nightshift"}
                    response = client.post("/api/generate/refine", json=body, headers=headers)
                    self.assertEqual(response.status_code, 200, response.text)
                    job = A.jobs[response.json()["job_id"]]
                    self.assertEqual(job["owner_id"], owner_id)
                    self.assertEqual(job["via_service"], self.bot["id"] if delegated else None)
                    import input_assets
                    scoped_id = owner_id if delegated or owner_id != self.admin_id else None
                    saved = input_assets.input_dir_for(scoped_id) / job["options"]["input_image"]
                    self.assertEqual(saved.read_bytes(), image.getvalue())
                    pod = {"id": "input-path-probe", "kind": "comfyui"}
                    job.update(pod_id=pod["id"], status="queued")
                    with patch.object(A, "wait_for_pod", return_value=("run", "http://unused")), \
                            patch.object(A.pod_registry, "get_pod", return_value=pod), \
                            patch.object(A, "job_missing_on_pod", return_value=[]), \
                            patch.object(A, "driver_for", return_value=SimpleNamespace(job_env=lambda *_: {})), \
                            patch.object(A.subprocess, "Popen", side_effect=lambda _cmd, **kw: real_popen(
                                [sys.executable, "-c", probe], **kw)), \
                            patch.object(notify, "job_finished"), \
                            patch.object(A, "pull_job_outputs"), patch.object(A, "_sync_assets_quietly"):
                        A._run_one_job(pod["id"], job["id"])
                    log = (A.LOGS_DIR / (job["id"] + ".log")).read_text(errors="replace")
                    self.assertEqual((job["status"], job["returncode"]), ("done", 0), log)
                    self.assertIn("INPUT_FOUND", log)


if __name__ == "__main__":
    unittest.main()
