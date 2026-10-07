"""다운로더 노드 v3의 노드팩 설치·재시작 라우트(templates/comfy_nodes/nightshift_downloader) — ComfyUI 없이 aiohttp·folder_paths·
server를 흉내 내 불러 검사한다. 실행: python tests/test_downloader_nodepacks.py
git clone은 로컬 저장소로, pip는 빈 requirements.txt로 진짜로 돌린다(인터넷 안 씀)."""
import asyncio, importlib.util, json, os, subprocess, sys, tempfile, types
from pathlib import Path

# ---- ComfyUI 쪽 모듈 흉내 ----
web = types.SimpleNamespace(json_response=lambda body, status=200: types.SimpleNamespace(body=body, status=status))
sys.modules["aiohttp"] = types.SimpleNamespace(web=web)
sys.modules["folder_paths"] = types.SimpleNamespace(get_folder_paths=lambda name: [], models_dir="/tmp")


class Routes:
    def __getattr__(self, _method):
        return lambda path: (lambda fn: fn)


sys.modules["server"] = types.SimpleNamespace(PromptServer=types.SimpleNamespace(instance=types.SimpleNamespace(routes=Routes())))
NODE = Path(__file__).resolve().parent.parent / "templates" / "comfy_nodes" / "nightshift_downloader" / "__init__.py"
spec = importlib.util.spec_from_file_location("ns_downloader", NODE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
assert m.VERSION == 3

tmp = Path(tempfile.mkdtemp())
m.HERE = str(tmp / "custom_nodes" / "nightshift_downloader")   # 노드팩은 HERE의 부모(custom_nodes)에 깔린다
os.makedirs(m.HERE)
m._read_token = lambda: "tok"


class Req:
    def __init__(self, body=None, token="tok"):
        self.headers = {"X-Nightshift-Token": token}
        self._body = body

    async def json(self):
        return self._body


run = asyncio.run

# 토큰 없으면 거절
assert run(m.np_install(Req({"packs": []}, token="bad"))).status == 401
assert run(m.comfy_restart(Req(token="bad"))).status == 401
# 형식 검사 — github 저장소 주소·폴더 이름만
for bad in ({"name": "../x", "url": "https://github.com/a/b"}, {"name": "A", "url": "https://evil.com/a/b"},
            {"name": "A", "url": "https://github.com/a/b; rm -rf /"}):
    assert run(m.np_install(Req({"packs": [bad]}))).status == 400, bad
assert run(m.np_install(Req({"packs": []}))).status == 400

# 진짜 설치 — 로컬 git 저장소를 clone하고 빈 requirements로 pip를 돌려 표시 파일을 남긴다
repo = tmp / "repo"
repo.mkdir()
(repo / "requirements.txt").write_text("\n")
(repo / "__init__.py").write_text("NODE_CLASS_MAPPINGS = {}\n")
git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
for cmd in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "x"]):
    subprocess.run(git + cmd, cwd=repo, check=True)
item = {"name": "PackA", "url": str(repo), "status": "queued", "error": None}
m._install_packs([item])
dest = tmp / "custom_nodes" / "PackA"
assert item["status"] == "done", item
assert (dest / "__init__.py").exists() and (dest / ".nightshift_pip_ok").exists() and not Path(str(dest) + ".nstmp").exists()
assert m._np["status"] == "done"
# 다시 해도 clone·pip를 건너뛴다(이미 있음)
item2 = {"name": "PackA", "url": "/no/such/repo", "status": "queued", "error": None}
m._install_packs([item2])
assert item2["status"] == "done", item2
# clone 실패는 그 노드팩만 error로
item3 = {"name": "PackB", "url": str(tmp / "missing"), "status": "queued", "error": None}
m._install_packs([item3])
assert item3["status"] == "error" and "git clone" in item3["error"] and not (tmp / "custom_nodes" / "PackB").exists(), item3

# 설치 중에는 겹쳐 시작·재시작 못 한다(설치 스레드는 돌리지 않는다)
started = []
m.threading = types.SimpleNamespace(Thread=lambda target, args=(), daemon=None: types.SimpleNamespace(start=lambda: started.append(target)))
ok = run(m.np_install(Req({"packs": [{"name": "ComfyUI-Impact-Pack", "url": "https://github.com/ltdrdata/ComfyUI-Impact-Pack"}]})))
assert ok.status == 200 and started == [m._install_packs] and m._np["status"] == "running"
assert run(m.np_install(Req({"packs": [{"name": "X", "url": "https://github.com/a/b"}]}))).status == 409
assert run(m.comfy_restart(Req())).status == 409
st = run(m.np_status(Req()))
assert st.status == 200 and st.body["status"] == "running" and st.body["packs"][0]["name"] == "ComfyUI-Impact-Pack"
# 끝난 뒤에는 재시작을 받는다(실제 execv는 스레드라 돌지 않는다)
m._np["status"] = "done"
assert run(m.comfy_restart(Req())).status == 200 and started[-1] == m._reexec
print("ok")
