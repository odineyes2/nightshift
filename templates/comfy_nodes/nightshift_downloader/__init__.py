"""
Nightshift model downloader — ComfyUI 커스텀 노드(HTTP 라우트 + 노드 하나: 사람 조각 남녀로 나누기, v8).

ComfyUI에는 모델 파일을 내려받는 API가 없어서, nightshift가 파드에 모델을 받으려면 파드 안에서
다운로드를 대신 해 줄 프로그램이 필요하다. 이 폴더를 ComfyUI/custom_nodes/ 에 두고 ComfyUI를 다시
시작하면 아래 라우트가 생긴다(nightshift의 "Model download" 패널이 설치 스크립트를 만들어 준다).

  GET  /nightshift/dl/ping     설치 여부·버전·저장 가능한 폴더 종류
  GET  /nightshift/models/sizes 종류별 모델 파일 크기(바이트)
  POST /nightshift/dl/start    {url, folder, filename, headers?, overwrite?} 다운로드 시작
  GET  /nightshift/dl/status   진행 목록
  POST /nightshift/dl/cancel   {id}
  POST /nightshift/nodepacks/install {packs: [{name, url}]} 노드팩 설치 시작(git clone + pip, v3)
  GET  /nightshift/nodepacks/status  설치 진행
  POST /nightshift/restart     ComfyUI만 다시 시작(같은 프로세스 자리에서 다시 실행 — 파드·파일은 그대로)
  POST /nightshift/train/start {name, trigger, checkpoint, images: [{file, caption}], ...} 인물 LoRA 학습 시작(v7)
  GET  /nightshift/train/status 학습 진행(단계·스텝·로그 끝부분·결과 파일)

파드 주소는 인터넷에 열려 있으므로 모든 라우트는 X-Nightshift-Token 헤더가 같은 폴더의 token 파일과
같아야만 동작한다(토큰은 nightshift가 설치 스크립트에 넣어 준다). 받는 곳은 ComfyUI가 그 종류에
쓰는 폴더 안으로만 제한하고, 모델 파일 확장자만 허용한다.
"""

import asyncio
import hmac
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from aiohttp import web

import folder_paths
from server import PromptServer

VERSION = 8   # 7: 인물 LoRA 학습(kohya sd-scripts) · 8: 사람 조각 남녀로 나누기 노드(NightshiftSEGSGenderSplit)
HERE = os.path.dirname(os.path.abspath(__file__))

# nightshift의 종류 이름 -> ComfyUI folder_paths 이름 후보(버전마다 이름이 달랐다).
FOLDER_ALIASES = {
    "checkpoints": ["checkpoints"],
    "diffusion_models": ["diffusion_models", "unet"],
    "loras": ["loras"],
    "vae": ["vae"],
    "text_encoders": ["text_encoders", "clip"],
    "clip_vision": ["clip_vision"],
    "controlnet": ["controlnet"],
    "upscale_models": ["upscale_models"],
    "ultralytics": ["ultralytics"],
}
# Impact-Subpack이 아직 안 깔린 ComfyUI에는 folder_paths에 없는 폴더 — models/ 아래 기본 위치로 받는다.
FALLBACK_DIRS = {"ultralytics": "ultralytics"}
ALLOWED_EXT = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf")
SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ ()+\[\]-]*$")
ALLOWED_HEADERS = {"authorization"}
MAX_JOBS = 50
CHUNK = 1024 * 1024
RESUME_TRIES = 8   # 끊겼을 때 이어 받기 재시도 횟수

_jobs = {}
_lock = threading.Lock()
_slots = threading.Semaphore(2)


def _read_token():
    try:
        with open(os.path.join(HERE, "token"), "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _authorized(request):
    token = _read_token()
    given = request.headers.get("X-Nightshift-Token", "")
    return bool(token) and hmac.compare_digest(token.encode(), given.encode())


def _folder_base(kind):
    for name in FOLDER_ALIASES.get(kind, []):
        try:
            paths = folder_paths.get_folder_paths(name)
        except Exception:
            continue
        if paths:
            return paths[0]
    if kind in FALLBACK_DIRS:
        return os.path.join(folder_paths.models_dir, FALLBACK_DIRS[kind])
    return None


def _model_sizes():
    """종류별 모델 폴더의 허용된 파일만 상대경로와 바이트 크기로 모은다."""
    sizes = {}
    for kind in FOLDER_ALIASES:
        base = _folder_base(kind)
        files = {}
        if base and os.path.isdir(base):
            for root, dirs, names in os.walk(base):
                dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(root, name))]
                for name in names:
                    path = os.path.join(root, name)
                    if not name.lower().endswith(ALLOWED_EXT) or os.path.islink(path):
                        continue
                    try:
                        files[os.path.relpath(path, base).replace(os.sep, "/")] = os.path.getsize(path)
                    except OSError:
                        continue
        sizes[kind] = files
    return sizes


def _safe_target(kind, filename):
    """(전체 경로, 오류). 종류 폴더 밖으로 나가거나 이상한 이름이면 오류."""
    base = _folder_base(kind)
    if base is None:
        return None, "이 ComfyUI에는 '%s' 폴더가 없어요." % kind
    if not isinstance(filename, str) or not filename.strip() or len(filename) > 300:
        return None, "파일명이 올바르지 않아요."
    parts = filename.strip().replace("\\", "/").split("/")
    if any(p in ("", ".", "..") or not SEGMENT_RE.match(p) for p in parts):
        return None, "파일명에 쓸 수 없는 글자가 있어요."
    if not parts[-1].lower().endswith(ALLOWED_EXT):
        return None, "모델 파일 확장자(%s)만 받을 수 있어요." % ", ".join(ALLOWED_EXT)
    target = os.path.realpath(os.path.join(base, *parts))
    if os.path.commonpath([os.path.realpath(base), target]) != os.path.realpath(base):
        return None, "종류 폴더 밖에는 받을 수 없어요."
    return target, None


class _RedirectHandler(urllib.request.HTTPRedirectHandler):
    """다른 호스트로 리다이렉트되면 Authorization을 뗀다(Civitai는 서명된 저장소 주소로 넘기는데
    거기에 토큰을 실어 보내면 거절당하고, 남의 서버에 토큰을 흘리는 것이기도 하다)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlparse(newurl).netloc != urllib.parse.urlparse(req.full_url).netloc:
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


def _public_job(job):
    return {k: job[k] for k in ("id", "host", "kind", "filename", "status", "downloaded", "total",
                                "error", "started_at", "finished_at")}


def _fetch(job, url, headers, part, target):
    """url을 part에 끝까지 받는다. http.client는 연결이 중간에 끊겨도 예외 없이 빈 값을 돌려주므로
    받은 크기를 전체 크기와 맞춰 보고, 모자라면 Range로 이어 받는다(NS-85 — 25GB 중 18GB만 받고 완료로 처리됐다).
    리다이렉트를 매번 다시 따라가므로 Civitai의 서명 주소가 만료돼도 새 주소로 이어 받는다."""
    opener = urllib.request.build_opener(_RedirectHandler)
    base_headers = dict({"User-Agent": "nightshift-downloader/%d" % VERSION}, **headers)
    total = 0
    failures = 0
    while True:
        have = os.path.getsize(part) if os.path.exists(part) else 0
        req_headers = dict(base_headers)
        if have:
            req_headers["Range"] = "bytes=%d-" % have
        try:
            with opener.open(urllib.request.Request(url, headers=req_headers), timeout=60) as resp:
                length = int(resp.headers.get("Content-Length") or 0)
                if have and resp.status == 206:
                    m = re.match(r"bytes \d+-\d+/(\d+)", resp.headers.get("Content-Range") or "")
                    total = int(m.group(1)) if m else have + length
                    mode = "ab"
                else:   # 처음이거나 서버가 Range를 무시했다 — 처음부터
                    total, have, mode = length, 0, "wb"
                job["total"] = total
                job["downloaded"] = have
                if not job.get("disk_checked"):
                    free = shutil.disk_usage(os.path.dirname(target)).free
                    if total and total - have > free:
                        raise RuntimeError("디스크 여유 공간이 부족해요(필요 %d MB, 남음 %d MB)."
                                           % ((total - have) // 1048576, free // 1048576))
                    job["disk_checked"] = True
                with open(part, mode) as out:
                    while True:
                        if job["cancel"]:
                            raise InterruptedError()
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
                        job["downloaded"] += len(chunk)
        except urllib.error.HTTPError as e:
            if e.code == 416 and total and os.path.exists(part) and os.path.getsize(part) == total:
                return   # 이미 다 받았다
            if e.code not in (408, 429) and e.code < 500:
                raise   # 토큰·권한·주소 문제 — 다시 해도 같다
            failures += 1
        except (OSError, http.client.HTTPException):   # 연결 끊김·시간 초과(URLError도 OSError)
            failures += 1
        else:
            got = os.path.getsize(part)
            if not total or got >= total:
                if total and got != total:
                    raise RuntimeError("받은 크기가 전체보다 커요(%d / %d 바이트)." % (got, total))
                return
            failures += 1   # 연결이 중간에 끊겼다
        if failures > RESUME_TRIES:
            got = os.path.getsize(part) if os.path.exists(part) else 0
            raise RuntimeError("연결이 자꾸 끊겨 다 받지 못했어요(%d / %d MB, %d번 이어 받기 시도)."
                               % (got // 1048576, total // 1048576, RESUME_TRIES))
        time.sleep(min(30, 2 ** failures))


def _run(job, url, headers, target, overwrite):
    part = target + ".part"
    try:
        with _slots:
            if job["cancel"]:
                job["status"] = "cancelled"
                return
            job["status"] = "downloading"
            os.makedirs(os.path.dirname(target), exist_ok=True)
            _fetch(job, url, headers, part, target)
            if os.path.exists(target) and not overwrite:
                raise RuntimeError("같은 이름의 파일이 이미 있어요.")
            os.replace(part, target)
            job["status"] = "done"
    except InterruptedError:
        job["status"] = "cancelled"
    except urllib.error.HTTPError as e:
        hint = " (토큰이 필요하거나 권한이 없을 수 있어요)" if e.code in (401, 403) else ""
        job["status"] = "error"
        job["error"] = "HTTP %d%s" % (e.code, hint)
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)[:300]
    finally:
        job["finished_at"] = time.time()
        if job["status"] != "done":
            try:
                os.remove(part)
            except OSError:
                pass


routes = PromptServer.instance.routes


@routes.get("/nightshift/dl/ping")
async def dl_ping(request):
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    folders = {k: _folder_base(k) is not None for k in FOLDER_ALIASES}
    return web.json_response({"ok": True, "version": VERSION, "folders": folders})


@routes.get("/nightshift/models/sizes")
async def model_sizes(request):
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    return web.json_response({"sizes": await asyncio.to_thread(_model_sizes)})


@routes.post("/nightshift/dl/start")
async def dl_start(request):
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "JSON이 아니에요."}, status=400)
    url = body.get("url")
    parsed = urllib.parse.urlparse(url if isinstance(url, str) else "")
    if parsed.scheme != "https" or not parsed.hostname:
        return web.json_response({"error": "https 주소만 받을 수 있어요."}, status=400)
    kind = body.get("folder")
    target, err = _safe_target(kind, body.get("filename"))
    if err:
        return web.json_response({"error": err}, status=400)
    overwrite = bool(body.get("overwrite"))
    if os.path.exists(target) and not overwrite:
        return web.json_response({"error": "같은 이름의 파일이 이미 있어요."}, status=409)
    headers = {}
    for k, v in (body.get("headers") or {}).items():
        if isinstance(k, str) and k.lower() in ALLOWED_HEADERS and isinstance(v, str) and len(v) < 2000:
            headers["Authorization"] = v
    with _lock:
        active = [j for j in _jobs.values() if j["status"] in ("queued", "downloading")]
        if any(j["target"] == target for j in active):
            return web.json_response({"error": "이미 받는 중이에요."}, status=409)
        job = {"id": uuid.uuid4().hex[:10], "host": parsed.hostname, "kind": kind,
               "filename": body["filename"].strip(), "status": "queued", "downloaded": 0, "total": 0,
               "error": None, "started_at": time.time(), "finished_at": None, "cancel": False, "target": target}
        _jobs[job["id"]] = job
        for old in sorted(_jobs.values(), key=lambda j: j["started_at"])[:-MAX_JOBS]:
            if old["status"] not in ("queued", "downloading"):
                _jobs.pop(old["id"], None)
    threading.Thread(target=_run, args=(job, url, headers, target, overwrite), daemon=True).start()
    return web.json_response({"id": job["id"]})


@routes.get("/nightshift/dl/status")
async def dl_status(request):
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    with _lock:
        items = [_public_job(j) for j in sorted(_jobs.values(), key=lambda j: -j["started_at"])]
    return web.json_response({"downloads": items})


@routes.post("/nightshift/dl/cancel")
async def dl_cancel(request):
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "JSON이 아니에요."}, status=400)
    with _lock:
        job = _jobs.get(body.get("id"))
        if job is None:
            return web.json_response({"error": "없는 다운로드예요."}, status=404)
        job["cancel"] = True
    return web.json_response({"ok": True})


# ---- 노드팩 설치·ComfyUI 재시작(v3) --------------------------------------------
# 노드팩은 ComfyUI를 다시 시작해야 들어온다. 파드를 다시 만들면 받아 둔 모델까지 다 사라지므로, 켜진 파드에서
# 노드팩만 설치하고 ComfyUI 프로세스만 다시 띄운다. 설치 방식은 파드 시작 스크립트(model_download._nodepack_script)와
# 같다 — 폴더가 있으면 clone을, .nightshift_pip_ok가 있으면 pip를 건너뛴다.
PACK_DIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
GITHUB_REPO_RE = re.compile(r"^https://github\.com/[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$")
GIT_TIMEOUT = 300
PIP_TIMEOUT = 1500
_np = {"status": "idle", "packs": [], "started_at": None, "finished_at": None}


def _tail(text, n=400):
    return (text or "").strip()[-n:]


def _install_pack(item):
    d = os.path.join(os.path.dirname(HERE), item["name"])   # 이 노드가 있는 custom_nodes 폴더
    if not os.path.isdir(d):
        tmp = d + ".nstmp"
        shutil.rmtree(tmp, ignore_errors=True)
        r = subprocess.run(["git", "clone", "--depth", "1", "--recurse-submodules", "--shallow-submodules", item["url"], tmp],
                           capture_output=True, text=True, timeout=GIT_TIMEOUT)
        if r.returncode:
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError("git clone 실패: " + _tail(r.stderr))
        os.replace(tmp, d)
    if os.path.isfile(os.path.join(d, ".gitmodules")):
        r = subprocess.run(["git", "submodule", "update", "--init", "--recursive", "--depth", "1"],
                           capture_output=True, text=True, timeout=GIT_TIMEOUT, cwd=d)
        if r.returncode:
            raise RuntimeError("서브모듈 설치 실패: " + _tail(r.stderr or r.stdout))
    req, mark = os.path.join(d, "requirements.txt"), os.path.join(d, ".nightshift_pip_ok")
    if os.path.isfile(req) and not os.path.exists(mark):
        item["status"] = "pip"
        # ComfyUI를 돌리는 그 python(이미지의 venv)에 깐다
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-r", req],
                           capture_output=True, text=True, timeout=PIP_TIMEOUT, cwd=d)
        if r.returncode:
            raise RuntimeError("패키지 설치 실패: " + _tail(r.stderr or r.stdout))
        open(mark, "w").close()


def _install_packs(items):
    for item in items:
        item["status"] = "installing"
        try:
            _install_pack(item)
            item["status"] = "done"
        except subprocess.TimeoutExpired:
            item["status"], item["error"] = "error", "시간 안에 끝나지 않았어요."
        except Exception as e:
            item["status"], item["error"] = "error", str(e)[:500]
    with _lock:
        _np["status"] = "done"
        _np["finished_at"] = time.time()


@routes.post("/nightshift/nodepacks/install")
async def np_install(request):
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "JSON이 아니에요."}, status=400)
    items = []
    for p in body.get("packs") or []:
        name, url = (p or {}).get("name"), (p or {}).get("url")
        if not (isinstance(name, str) and PACK_DIR_RE.match(name) and isinstance(url, str) and GITHUB_REPO_RE.match(url)):
            return web.json_response({"error": "노드팩 이름이나 github 주소 형식이 올바르지 않아요."}, status=400)
        items.append({"name": name, "url": url, "status": "queued", "error": None})
    if not items:
        return web.json_response({"error": "설치할 노드팩이 없어요."}, status=400)
    with _lock:
        if _np["status"] == "running":
            return web.json_response({"error": "이미 노드팩을 설치하는 중이에요."}, status=409)
        _np.update(status="running", packs=items, started_at=time.time(), finished_at=None)
    threading.Thread(target=_install_packs, args=(items,), daemon=True).start()
    return web.json_response({"ok": True})


@routes.get("/nightshift/nodepacks/status")
async def np_status(request):
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    with _lock:
        return web.json_response({k: _np[k] for k in ("status", "started_at", "finished_at")}
                                 | {"packs": [dict(p) for p in _np["packs"]]})


def _reexec():
    time.sleep(1)   # 응답이 나간 뒤에
    sys.stdout.flush()
    # ComfyUI-Manager의 재시작과 같은 방식 — 같은 PID로 다시 실행하므로 runpod/comfyui 시작 스크립트의
    # wait도 그대로 이어지고 컨테이너는 살아 있다. 소켓은 기본이 상속 안 됨(PEP 446)이라 8188 포트도 다시 잡힌다.
    os.execv(sys.executable, [sys.executable] + sys.argv)


@routes.post("/nightshift/restart")
async def comfy_restart(request):
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    with _lock:
        if _np["status"] == "running":
            return web.json_response({"error": "노드팩을 설치하는 중이에요 — 끝난 뒤 다시 시작하세요."}, status=409)
    threading.Thread(target=_reexec, daemon=True).start()
    return web.json_response({"ok": True})


# ---- 인물 LoRA 학습(v7) ---------------------------------------------------------
# 캐릭터 스튜디오의 배우 LoRA를 이 파드에서 학습한다. 학습 자료는 nightshift가 /upload/image로 input/ 아래에 올려 두고,
# 이 라우트가 그 그림을 학습 폴더로 옮겨 캡션을 붙인 뒤 kohya sd-scripts(SDXL LoRA)로 학습한다. 설치는 처음 한 번
# /workspace/nightshift_train/ 아래 따로 만든 venv에 한다(ComfyUI의 python 패키지는 건드리지 않는다).
# 학습 전에 얼굴이 정확히 하나 보이지 않는 그림은 뺀다(뒷모습·여럿·얼굴 잘림). 결과는 models/loras/<name>.safetensors와
# output/nightshift_train/<name>.safetensors(nightshift가 /view로 받아 가는 사본)에 둔다.
TRAIN_ROOT = "/workspace/nightshift_train"
SD_SCRIPTS_REPO = "https://github.com/kohya-ss/sd-scripts.git"
# 2026-11-17 수동 실험에서 되던 그대로: 시스템 python3로 venv를 만들고 cu124 torch(그때 2.6.0으로 풀림)를 받는다.
# ComfyUI의 python으로 만든 venv에 버전을 못박았더니 pip가 의존성을 풀지 못했다(ResolutionImpossible).
TORCH_ARGS = ["torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/cu124"]
TRAIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
TRIGGER_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")
CAPTION_MAX = 400
MIN_IMAGES, MAX_IMAGES = 8, 80
_train = {"status": "idle", "phase": "", "face_filter": "", "name": None, "step": 0, "total": 0, "kept": 0, "dropped": [],
          "error": None, "log": "", "started_at": None, "finished_at": None, "output": None}
STEP_RE = re.compile(r"steps:\s+\d+%\|.*?\|\s*(\d+)/(\d+)")


def _train_set(**values):
    with _lock:
        _train.update(values)


def _run_logged(cmd, cwd=None, timeout=None, env=None):
    """명령을 돌리며 출력 끝부분과 학습 스텝을 상태에 적는다. 실패하면 RuntimeError."""
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    tail, started = "", time.time()
    for line in proc.stdout:
        tail = (tail + line)[-4000:]
        for chunk in line.split("\r"):
            m = STEP_RE.search(chunk)
            if m:
                _train_set(step=int(m.group(1)), total=int(m.group(2)))
        _train_set(log=tail[-1500:])
        if timeout and time.time() - started > timeout:
            proc.kill()
            raise RuntimeError("시간 안에 끝나지 않았어요.")
    if proc.wait():
        raise RuntimeError(_tail(tail, 800))


def _clean_env():
    """학습용 venv에는 ComfyUI 이미지의 pip 설정을 물려주지 않는다 — PIP_CONSTRAINT가 ComfyUI의 torch 버전을 못박아
    학습 도구의 torch를 깔 수 없었다(2026-11-17 실제 파드)."""
    return {k: v for k, v in os.environ.items() if not k.upper().startswith(("PIP_", "UV_", "VIRTUAL_ENV", "PYTHON"))}


def _train_install():
    venv_py = os.path.join(TRAIN_ROOT, "venv", "bin", "python")
    mark = os.path.join(TRAIN_ROOT, ".installed")
    if os.path.exists(mark):
        return venv_py
    os.makedirs(TRAIN_ROOT, exist_ok=True)
    repo = os.path.join(TRAIN_ROOT, "sd-scripts")
    if not os.path.isdir(repo):
        _run_logged(["git", "clone", "--depth", "1", SD_SCRIPTS_REPO, repo], timeout=GIT_TIMEOUT)
    if not os.path.exists(venv_py):
        base_python = shutil.which("python3", path="/usr/bin:/bin") or sys.executable
        _run_logged([base_python, "-m", "venv", os.path.join(TRAIN_ROOT, "venv")], timeout=300, env=_clean_env())
    env = _clean_env()
    _run_logged([venv_py, "-m", "pip", "install", "-q", "--upgrade", "pip"], timeout=PIP_TIMEOUT, env=env)
    _run_logged([venv_py, "-m", "pip", "install", *TORCH_ARGS], timeout=PIP_TIMEOUT, env=env)
    _run_logged([venv_py, "-m", "pip", "install", "-q", "-r", "requirements.txt"], cwd=repo, timeout=PIP_TIMEOUT, env=env)
    open(mark, "w").close()
    return venv_py


def _count_faces(paths):
    """그림마다 얼굴 수(Impact-Subpack의 얼굴 탐지 모델). 모델이나 ultralytics가 없으면 None — 거르지 않는다."""
    model_path = os.path.join(_folder_base("ultralytics") or "", "bbox", "face_yolov8m.pt")
    try:
        from ultralytics import YOLO
        if not os.path.isfile(model_path):
            return None
        model = YOLO(model_path)
        return [sum(1 for box in r.boxes if float(box.conf) >= 0.5) for r in model(paths, verbose=False)]
    except Exception:
        return None


def _free_vram():
    try:
        import comfy.model_management as mm
        mm.unload_all_models()
        mm.soft_empty_cache()
    except Exception:
        pass


def _train_run(spec):
    name, trigger = spec["name"], spec["trigger"]
    try:
        _train_set(phase="dataset")
        data_dir = os.path.join(TRAIN_ROOT, "data", name)
        shutil.rmtree(data_dir, ignore_errors=True)
        dest = os.path.join(data_dir, "%d_%s" % (spec["repeats"], trigger))
        os.makedirs(dest)
        sources = [os.path.join(folder_paths.get_input_directory(), *item["file"].split("/")) for item in spec["images"]]
        faces = _count_faces(sources) if spec["one_face"] else None
        _train_set(face_filter="on" if faces is not None else ("off" if not spec["one_face"] else "unavailable"))
        kept, dropped = 0, []
        for index, (item, src) in enumerate(zip(spec["images"], sources)):
            if faces is not None and faces[index] != 1:
                dropped.append({"file": item["file"], "faces": faces[index]})
                continue
            ext = os.path.splitext(src)[1].lower() or ".png"
            shutil.copyfile(src, os.path.join(dest, "%03d%s" % (index, ext)))
            with open(os.path.join(dest, "%03d.txt" % index), "w", encoding="utf-8") as f:
                f.write(("%s, %s" % (trigger, item["caption"])).strip(", "))
            kept += 1
        _train_set(kept=kept, dropped=dropped)
        if kept < MIN_IMAGES:
            raise RuntimeError("얼굴이 하나만 보이는 그림이 %d장뿐이라 학습할 수 없어요(%d장 이상 필요)." % (kept, MIN_IMAGES))
        _train_set(phase="install")
        venv_py = _train_install()
        _train_set(phase="train", step=0, total=0)
        _free_vram()
        loras = _folder_base("loras")
        ckpt = os.path.join(_folder_base("checkpoints"), *spec["checkpoint"].split("/"))
        if not os.path.isfile(ckpt):
            raise RuntimeError("학습할 바탕 모델이 이 파드에 없어요: " + spec["checkpoint"])
        out_name = name
        cmd = [os.path.join(TRAIN_ROOT, "venv", "bin", "accelerate"), "launch", "--num_cpu_threads_per_process", "4",
               "sdxl_train_network.py", "--pretrained_model_name_or_path=" + ckpt, "--train_data_dir=" + data_dir,
               "--resolution=1024,1024", "--train_batch_size=2", "--network_module=networks.lora",
               "--network_dim=%d" % spec["dim"], "--network_alpha=%d" % spec["alpha"], "--network_train_unet_only",
               "--optimizer_type=AdamW8bit", "--learning_rate=%g" % spec["lr"], "--lr_scheduler=cosine", "--lr_warmup_steps=50",
               "--max_train_epochs=%d" % spec["epochs"], "--mixed_precision=bf16", "--save_precision=fp16",
               "--cache_latents", "--cache_text_encoder_outputs", "--gradient_checkpointing", "--sdpa", "--no_half_vae",
               "--min_snr_gamma=5", "--seed=%d" % spec["seed"], "--save_model_as=safetensors",
               "--output_dir=" + loras, "--output_name=" + out_name]
        _run_logged(cmd, cwd=os.path.join(TRAIN_ROOT, "sd-scripts"), timeout=spec["timeout"],
                    env={**_clean_env(), "PYTHONUNBUFFERED": "1"})
        result = os.path.join(loras, out_name + ".safetensors")
        if not os.path.isfile(result):
            raise RuntimeError("학습은 끝났는데 결과 파일이 없어요.")
        copy_dir = os.path.join(folder_paths.get_output_directory(), "nightshift_train")
        os.makedirs(copy_dir, exist_ok=True)
        shutil.copyfile(result, os.path.join(copy_dir, out_name + ".safetensors"))
        _train_set(status="done", phase="done", finished_at=time.time(),
                   output={"name": out_name + ".safetensors", "size": os.path.getsize(result), "subfolder": "nightshift_train"})
    except subprocess.TimeoutExpired:
        _train_set(status="error", error="시간 안에 끝나지 않았어요.", finished_at=time.time())
    except Exception as e:
        _train_set(status="error", error=str(e)[:1000], finished_at=time.time())


def _int_in(value, default, low, high):
    try:
        value = int(value if value is not None else default)
    except (TypeError, ValueError):
        raise ValueError("숫자 값이 올바르지 않아요.")
    if not low <= value <= high:
        raise ValueError("값은 %d~%d여야 해요." % (low, high))
    return value


@routes.post("/nightshift/train/start")
async def train_start(request):
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "JSON이 아니에요."}, status=400)
    name, trigger, checkpoint = body.get("name"), body.get("trigger"), body.get("checkpoint")
    if not (isinstance(name, str) and TRAIN_NAME_RE.match(name)):
        return web.json_response({"error": "LoRA 이름은 영문·숫자·밑줄·하이픈 64자까지예요."}, status=400)
    if not (isinstance(trigger, str) and TRIGGER_RE.match(trigger)):
        return web.json_response({"error": "트리거는 영문·숫자·밑줄 40자까지예요."}, status=400)
    if not (isinstance(checkpoint, str) and checkpoint.endswith(".safetensors")
            and all(p not in ("", ".", "..") and SEGMENT_RE.match(p) for p in checkpoint.split("/"))):
        return web.json_response({"error": "바탕 모델 이름이 올바르지 않아요."}, status=400)
    images = body.get("images")
    if not isinstance(images, list) or not MIN_IMAGES <= len(images) <= MAX_IMAGES:
        return web.json_response({"error": "학습 그림은 %d~%d장이어야 해요." % (MIN_IMAGES, MAX_IMAGES)}, status=400)
    clean = []
    input_dir = os.path.realpath(folder_paths.get_input_directory())
    for item in images:
        file, caption = (item or {}).get("file"), (item or {}).get("caption", "")
        parts = file.split("/") if isinstance(file, str) else []
        if not parts or any(p in ("", ".", "..") or not SEGMENT_RE.match(p) for p in parts) \
                or not parts[-1].lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            return web.json_response({"error": "학습 그림 이름이 올바르지 않아요."}, status=400)
        path = os.path.realpath(os.path.join(input_dir, *parts))
        if os.path.commonpath([input_dir, path]) != input_dir or not os.path.isfile(path):
            return web.json_response({"error": "올라오지 않은 학습 그림이 있어요: " + file}, status=400)
        if not isinstance(caption, str) or len(caption) > CAPTION_MAX or "\n" in caption:
            return web.json_response({"error": "캡션은 한 줄 %d자까지예요." % CAPTION_MAX}, status=400)
        clean.append({"file": file, "caption": caption.strip()})
    try:
        spec = {"name": name, "trigger": trigger, "checkpoint": checkpoint, "images": clean,
                "repeats": _int_in(body.get("repeats"), 10, 1, 40), "epochs": _int_in(body.get("epochs"), 8, 1, 40),
                "dim": _int_in(body.get("dim"), 16, 4, 128), "alpha": _int_in(body.get("alpha"), 8, 1, 128),
                "seed": _int_in(body.get("seed"), 42, 0, 2**31 - 1), "timeout": _int_in(body.get("timeout"), 3 * 3600, 600, 12 * 3600),
                "lr": float(body.get("lr") or 1e-4), "one_face": body.get("one_face", True) is not False}
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    if not 1e-6 <= spec["lr"] <= 1e-2:
        return web.json_response({"error": "학습률이 올바르지 않아요."}, status=400)
    with _lock:
        if _train["status"] == "running":
            return web.json_response({"error": "이미 학습하는 중이에요."}, status=409)
        _train.update(status="running", phase="queued", name=name, step=0, total=0, kept=0, dropped=[], error=None, log="",
                      started_at=time.time(), finished_at=None, output=None)
    threading.Thread(target=_train_run, args=(spec,), daemon=True).start()
    return web.json_response({"ok": True})


@routes.get("/nightshift/train/status")
async def train_status(request):
    if not _authorized(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    with _lock:
        return web.json_response(dict(_train))


# ── 노드: 사람 조각(SEGS)을 남녀로 나누기(v8) ──
# 두 인물 그림을 LoRA 없이 그린 뒤 사람마다 배우를 입힐 때, 남녀가 어느 쪽에 섰는지는 칸 나누기로도 반반이었다. 일반 성별 분류
# (Impact Pack 프리셋)는 애니메이션 미남을 여자로 봐서 Danbooru 태거로 1boy·1girl 점수를 비교한다. 모델 파일은 표준 transformers
# Swinv2라 원격 코드 없이 고정 커밋에서 받고, 저장소의 사용자 정의 전처리(흰 바탕 정사각형·448px·BGR·-1~1)는 여기 옮겨 적었다.
GENDER_TAGGER_REPO = "p1atdev/wd-swinv2-tagger-v3-hf"
GENDER_TAGGER_REVISION = "b09fdef86a3f9d4bf974ccaa5c87dcf846d3a2fd"
GENDER_MIN = 0.35   # 두 조각을 비교해 나눌 때도 그 성별 점수가 이만큼은 돼야 그 배우를 입힌다
_tagger = {}


def _gender_tagger():
    if "model" not in _tagger:
        import torch
        from transformers import AutoModelForImageClassification
        model = AutoModelForImageClassification.from_pretrained(GENDER_TAGGER_REPO, revision=GENDER_TAGGER_REVISION)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        labels = {name: int(i) for i, name in model.config.id2label.items()}
        _tagger.update(model=model.to(device).eval(), device=device, boy=labels["1boy"], girl=labels["1girl"])
    return _tagger


def _tagger_input(crop, mask):
    """사람 조각(0~1, H×W×3) → 태거 입력. 사람 마스크 밖은 흰색으로(옆 사람이 조각에 같이 들어와도 섞이지 않게)."""
    import numpy as np
    from PIL import Image
    image = np.asarray(crop, dtype=np.float32)
    if image.ndim == 4:
        image = image[0]
    if mask is not None:
        m = np.asarray(mask, dtype=np.float32)
        if m.ndim == 3:
            m = m[0]
        if m.shape == image.shape[:2]:
            image = image * m[..., None] + (1.0 - m[..., None])
    pil = Image.fromarray((image.clip(0, 1) * 255).astype(np.uint8))
    side = max(pil.size)
    square = Image.new("RGB", (side, side), (255, 255, 255))
    square.paste(pil, ((side - pil.size[0]) // 2, (side - pil.size[1]) // 2))
    array = np.asarray(square.resize((448, 448), Image.BICUBIC), dtype=np.float32)[:, :, ::-1]   # RGB → BGR
    array = (array / 255.0 - 0.5) / 0.5
    return array.transpose(2, 0, 1).copy()


class NightshiftSEGSGenderSplit:
    """사람 조각을 남녀로 나눈다. 두 조각이면 (1boy - 1girl)이 큰 쪽이 남자, 그 밖에는 조각마다 1boy가 더 높으면 남자."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"segs": ("SEGS",)},
                "optional": {"expected": ("INT", {"default": 0, "min": 0, "max": 8})}}

    RETURN_TYPES = ("SEGS", "SEGS", "STRING")
    RETURN_NAMES = ("male_segs", "female_segs", "scores")
    FUNCTION = "split"
    CATEGORY = "nightshift"

    def split(self, segs, expected=0):
        import numpy as np
        import torch
        shape, items = segs
        male, female, scores = [], [], []
        if expected:
            # 잡힌 사람 전체에서 큰 순으로 고른다. 따로 잡히지 않았거나(겹친 두 사람이 한 덩어리) 세 번째 사람이 진짜 인물 크기면
            # 누가 누군지 모르니 다시 그리지 않게 비운다(2026-11-19: 세 사람 그림에서 여자에게 남자 배우가 입혀졌다).
            def area(seg):
                x1, y1, x2, y2 = seg.crop_region
                return max(0, x2 - x1) * max(0, y2 - y1)
            items = sorted(items, key=area, reverse=True)
            crowded = len(items) > expected and area(items[expected]) > 0.3 * area(items[expected - 1])
            if len(items) < expected or crowded:
                print(f"[nightshift] 남녀 나누기: 조각 {len(items)}개(기대 {expected}개) — 건너뜀", flush=True)
                return ((shape, []), (shape, []), f"skipped {len(items)}/{expected}")
            items = items[:expected]
        if items:
            tagger = _gender_tagger()
            batch = np.stack([_tagger_input(seg.cropped_image, seg.cropped_mask) for seg in items])
            with torch.no_grad():
                logits = tagger["model"](pixel_values=torch.from_numpy(batch).to(tagger["device"])).logits
                probs = torch.sigmoid(logits).float().cpu().numpy()
            boys = [float(row[tagger["boy"]]) for row in probs]
            girls = [float(row[tagger["girl"]]) for row in probs]
            margins = [b - g for b, g in zip(boys, girls)]
            if len(items) == 2:
                # 두 사람이면 서로 비교한다 — 붙어 선 두 사람은 남자 조각에도 여자 머리카락이 들어가 1girl이 높게 나왔다(2026-11-18).
                # 다만 남자 쪽은 1boy, 여자 쪽은 1girl 점수가 GENDER_MIN 이상일 때만 — 둘 다 여자(또는 남자)면 한쪽을 비운다.
                order = sorted(range(2), key=lambda i: margins[i], reverse=True)
                male = [items[order[0]]] if boys[order[0]] >= GENDER_MIN else []
                female = [items[order[1]]] if girls[order[1]] >= GENDER_MIN else []
            else:
                for seg, margin in zip(items, margins):
                    (male if margin > 0 else female).append(seg)
            for seg, row in zip(items, probs):
                scores.append(f"x={seg.crop_region[0]} 1boy={float(row[tagger['boy']]):.2f} 1girl={float(row[tagger['girl']]):.2f}")
        print("[nightshift] 남녀 나누기:", "; ".join(scores), flush=True)
        return ((shape, male), (shape, female), "; ".join(scores))


NODE_CLASS_MAPPINGS = {"NightshiftSEGSGenderSplit": NightshiftSEGSGenderSplit}
NODE_DISPLAY_NAME_MAPPINGS = {"NightshiftSEGSGenderSplit": "Nightshift: 사람 조각 남녀로 나누기"}
