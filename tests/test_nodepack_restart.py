"""켜진 파드에 노드팩 설치 → ComfyUI만 재시작 — 서버 API를 앱 안에서(TestClient) 가짜 ComfyUI(+다운로더 v3)로 검사한다.
임시 데이터 폴더를 쓰고 RunPod는 부르지 않는다. 실행: python tests/test_nodepack_restart.py
1) 워커 모델 탭 버튼(POST /api/pods/{id}/nodepacks/install): 실행 중 작업이 있으면 409, 아니면 설치 → 재시작 → 작업 배정
2) 자동 설치가 켜진 워커: 버튼 없이 스케줄러가 알아서 설치·재시작하고 작업을 배정한다."""
import json, os, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
for k, sub in (("NIGHTSHIFT_DATA_DIR", "data"), ("NIGHTSHIFT_OUTPUT_DIR", "out"), ("NIGHTSHIFT_ASSETS_DIR", "assets")):
    os.environ[k] = str(tmp / sub)
os.environ.update(NIGHTSHIFT_ADMIN_USER="admin", NIGHTSHIFT_ADMIN_PASSWORD="Test-Passw0rd-xyz!", RUNPOD_API_KEY="")
SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER)); os.chdir(SERVER)

PACKS = {"FaceDetailer": "ComfyUI-Impact-Pack", "UltralyticsDetectorProvider": "ComfyUI-Impact-Subpack"}


def fake_comfy():
    """가짜 ComfyUI — 설치한 노드팩은 재시작한 뒤에야 object_info에 노드가 나타난다(진짜처럼)."""
    st = {"installed": set(), "loaded": set(), "down_until": 0.0, "restarts": 0, "np": {"status": "idle", "packs": []}}

    class H(BaseHTTPRequestHandler):
        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

        def do_GET(self):
            if time.time() < st["down_until"]:
                return self._send(503, {})
            if self.path.startswith("/system_stats"):
                return self._send(200, {"system": {}})
            if self.path.startswith("/object_info"):
                info = {"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["a.safetensors"], {}]}}}}
                for node, pack in PACKS.items():
                    if pack in st["loaded"]:
                        info[node] = {"input": {"required": {}}}
                if PACKS["UltralyticsDetectorProvider"] in st["loaded"]:
                    info["UltralyticsDetectorProvider"] = {"input": {"required": {"model_name": [["bbox/face_yolov8m.pt"], {}]}}}
                return self._send(200, info)
            if self.path.startswith("/nightshift/dl/ping"):
                return self._send(200, {"version": 3, "folders": {}})
            if self.path.startswith("/nightshift/nodepacks/status"):
                np = st["np"]
                if np["status"] == "running" and time.time() - np["started"] > 1:
                    for p in np["packs"]:
                        p["status"] = "done"
                        st["installed"].add(p["name"])
                    np["status"] = "done"
                return self._send(200, {"status": np["status"], "packs": np["packs"]})
            self._send(404, {})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.path == "/nightshift/nodepacks/install":
                st["np"] = {"status": "running", "started": time.time(),
                            "packs": [{"name": p["name"], "status": "installing"} for p in body["packs"]]}
                return self._send(200, {"ok": True})
            if self.path == "/nightshift/restart":
                st["restarts"] += 1
                st["down_until"] = time.time() + 2
                st["loaded"] = set(st["installed"])
                return self._send(200, {"ok": True})
            self._send(404, {})

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return st, f"http://127.0.0.1:{srv.server_port}"


import runpod_api  # noqa: E402
runpod_api.RUNPOD_API_BASE = "http://127.0.0.1:9"   # 혹시 불려도 진짜 RunPod에는 닿지 않게
runpod_api.RUNPOD_API_KEY = ""
from fastapi.testclient import TestClient  # noqa: E402
import app as A  # noqa: E402
H = {"X-Requested-With": "nightshift"}
WF = {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "a.safetensors"}},
      "2": {"class_type": "FaceDetailer", "inputs": {"image": ["1", 0]}},
      "3": {"class_type": "UltralyticsDetectorProvider", "inputs": {"model_name": "bbox/face_yolov8m.pt"}}}


def wait(cond, sec=40, what=""):
    end = time.time() + sec
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.5)
    raise AssertionError(f"시간 초과: {what}")


with TestClient(A.app) as c:
    assert c.post("/api/auth/login", json={"username": "admin", "password": "Test-Passw0rd-xyz!"}, headers=H).status_code == 200
    for pack in PACKS.values():
        r = c.put("/api/models", json={"kind": "custom_nodes", "filename": pack,
                                        "download_url": f"https://github.com/ltdrdata/{pack}"}, headers=H)
        assert r.status_code == 200, r.text
    for p in c.get("/api/pods", headers=H).json()["pods"]:   # 기본 파드는 쓰지 않는다
        c.put(f"/api/pods/{p['id']}", json={"enabled": False}, headers=H)
    job = lambda jid: next(j for j in c.get("/api/jobs", headers=H).json()["jobs"] if j["id"] == jid)

    def new_job():
        j = c.post("/api/jobs", data={"template_id": "seed_batch", "workflow": json.dumps(WF)}, headers=H)
        if j.status_code != 200:   # 폼 대신 JSON을 받는 경우
            j = c.post("/api/jobs", json={"template_id": "seed_batch", "workflow": WF}, headers=H)
        assert j.status_code == 200, j.text
        jid = j.json()["id"]
        if job(jid)["status"] != "queued":   # 큐가 꺼져 있으면 pending으로 만들어진다
            r = c.post(f"/api/jobs/{jid}/start", headers=H)
            assert r.status_code == 200, r.text
        return jid

    # ---- 1) 버튼 -------------------------------------------------------------------------------
    st1, url1 = fake_comfy()
    w1 = c.post("/api/pods", json={"name": "np-1", "url": url1, "enabled": True}, headers=H).json()
    j1 = new_job()
    wait(lambda: "노드팩 없음" in (job(j1).get("waiting_reason") or ""), what="노드팩 없음 대기 사유")
    needed = c.get(f"/api/pods/{w1['id']}/models", headers=H).json()["needed"]
    assert {n["name"] for n in needed if n["installable"]} == {f"노드팩 없음 ({p})" for p in PACKS.values()}, needed

    rt = A.pod_runtimes[w1["id"]]
    rt.running["fake"] = None                    # 실행 중 작업이 있으면 재시작하지 않는다
    r = c.post(f"/api/pods/{w1['id']}/nodepacks/install", headers=H)
    assert r.status_code == 409 and "실행 중" in r.json()["detail"], r.text
    del rt.running["fake"]

    r = c.post(f"/api/pods/{w1['id']}/nodepacks/install", headers=H)
    assert r.status_code == 200 and sorted(r.json()["packs"]) == sorted(PACKS.values()), r.text
    assert c.post(f"/api/pods/{w1['id']}/nodepacks/install", headers=H).status_code == 409   # 겹치기 금지
    wait(lambda: "노드팩 설치 중" in (job(j1).get("waiting_reason") or "") or job(j1).get("pod_id"), what="설치 중 대기 사유")
    run = wait(lambda: (lambda n: n if n and n["status"] in ("done", "error") else None)(
        c.get(f"/api/pods/{w1['id']}/models", headers=H).json().get("nodepack")), what="설치 끝")
    assert run["status"] == "done" and st1["restarts"] == 1 and not run["auto"], run
    assert rt.maintenance is None
    wait(lambda: job(j1).get("pod_id") == w1["id"], what="작업 배정")
    c.post(f"/api/jobs/{j1}/stop", headers=H); c.delete(f"/api/jobs/{j1}", headers=H)

    # ---- 2) 자동 설치 ---------------------------------------------------------------------------
    c.put(f"/api/pods/{w1['id']}", json={"enabled": False}, headers=H)
    st2, url2 = fake_comfy()
    w2 = c.post("/api/pods", json={"name": "np-2", "url": url2, "enabled": True}, headers=H).json()
    c.put(f"/api/pods/{w2['id']}", json={"auto_install_models": True}, headers=H)
    j2 = new_job()
    wait(lambda: job(j2).get("pod_id") == w2["id"], sec=60, what="자동 설치 뒤 작업 배정")
    run2 = c.get(f"/api/pods/{w2['id']}/models", headers=H).json()["nodepack"]
    assert run2["auto"] and run2["status"] == "done" and st2["restarts"] == 1, run2
    c.post(f"/api/jobs/{j2}/stop", headers=H); c.delete(f"/api/jobs/{j2}", headers=H)
print("ok")
