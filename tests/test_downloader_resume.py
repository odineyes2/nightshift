"""다운로더 노드의 받은 크기 확인·이어 받기(NS-85) — 중간에 연결을 끊는 로컬 HTTP 서버로 검사한다.
실행: python tests/test_downloader_resume.py"""
import importlib.util, os, sys, tempfile, threading, types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---- ComfyUI 쪽 모듈 흉내(test_downloader_nodepacks.py와 같다) ----
web = types.SimpleNamespace(json_response=lambda body, status=200: types.SimpleNamespace(body=body, status=status))
sys.modules["aiohttp"] = types.SimpleNamespace(web=web)
sys.modules["folder_paths"] = types.SimpleNamespace(get_folder_paths=lambda name: [], models_dir="/tmp")


class Routes:
    def __getattr__(self, _method):
        return lambda path: (lambda fn: fn)


sys.modules["server"] = types.SimpleNamespace(PromptServer=types.SimpleNamespace(instance=types.SimpleNamespace(routes=Routes())))
NODE = Path(__file__).resolve().parent.parent / "templates" / "comfy_nodes" / "nightshift_downloader" / "__init__.py"
spec = importlib.util.spec_from_file_location("ns_downloader_resume", NODE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.time.sleep = lambda s: None   # 재시도 대기 없이

DATA = os.urandom(3 * 1024 * 1024 + 123)
CUT = 1024 * 1024 + 7   # 응답마다 이만큼만 보내고 끊는다
state = {"range": True, "requests": []}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        rng = self.headers.get("Range")
        state["requests"].append(rng)
        start = int(rng[6:-1]) if (rng and state["range"]) else 0
        body = DATA[start:]
        if start:
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, len(DATA) - 1, len(DATA)))
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body[:CUT])   # Content-Length보다 적게 보내고 연결을 닫는다
        self.close_connection = True


srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = "http://127.0.0.1:%d/model.safetensors" % srv.server_port
tmp = Path(tempfile.mkdtemp())


def run(name):
    job = {"id": name, "status": "queued", "downloaded": 0, "total": 0, "error": None,
           "started_at": 0, "finished_at": None, "cancel": False}
    target = str(tmp / name)
    m._run(job, URL, {}, target, False)
    return job, target


# 1) 끊길 때마다 Range로 이어 받아 끝까지 받는다.
job, target = run("a.safetensors")
assert job["status"] == "done", job
assert Path(target).read_bytes() == DATA
assert job["downloaded"] == job["total"] == len(DATA), job
assert state["requests"][0] is None and all(r and r.startswith("bytes=") for r in state["requests"][1:]), state["requests"]
assert len(state["requests"]) == 4, state["requests"]   # 1MB씩 끊기니 네 번에 다 받는다

# 2) 서버가 Range를 무시하면 계속 처음부터라 다 못 받는다 — 완료로 치지 않고 오류, .part도 지운다.
state.update(range=False, requests=[])
job, target = run("b.safetensors")
assert job["status"] == "error" and "다 받지 못했어요" in job["error"], job
assert not os.path.exists(target) and not os.path.exists(target + ".part")
assert len(state["requests"]) == m.RESUME_TRIES + 1, state["requests"]

# 3) 끊기지 않으면 한 번에 끝난다.
CUT = len(DATA)
state.update(range=True, requests=[])
job, target = run("c.safetensors")
assert job["status"] == "done" and Path(target).read_bytes() == DATA and len(state["requests"]) == 1, (job, state)

srv.shutdown()
print("ok")
