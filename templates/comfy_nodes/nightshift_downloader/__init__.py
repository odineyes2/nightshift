"""
Nightshift model downloader — ComfyUI 커스텀 노드(노드는 없고 HTTP 라우트만 추가한다).

ComfyUI에는 모델 파일을 내려받는 API가 없어서, nightshift가 파드에 모델을 받으려면 파드 안에서
다운로드를 대신 해 줄 프로그램이 필요하다. 이 폴더를 ComfyUI/custom_nodes/ 에 두고 ComfyUI를 다시
시작하면 아래 라우트가 생긴다(nightshift의 "Model download" 패널이 설치 스크립트를 만들어 준다).

  GET  /nightshift/dl/ping     설치 여부·버전·저장 가능한 폴더 종류
  POST /nightshift/dl/start    {url, folder, filename, headers?, overwrite?} 다운로드 시작
  GET  /nightshift/dl/status   진행 목록
  POST /nightshift/dl/cancel   {id}

파드 주소는 인터넷에 열려 있으므로 모든 라우트는 X-Nightshift-Token 헤더가 같은 폴더의 token 파일과
같아야만 동작한다(토큰은 nightshift가 설치 스크립트에 넣어 준다). 받는 곳은 ComfyUI가 그 종류에
쓰는 폴더 안으로만 제한하고, 모델 파일 확장자만 허용한다.
"""

import hmac
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from aiohttp import web

import folder_paths
from server import PromptServer

VERSION = 1
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
}
ALLOWED_EXT = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf")
SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ ()+\[\]-]*$")
ALLOWED_HEADERS = {"authorization"}
MAX_JOBS = 50
CHUNK = 1024 * 1024

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
    return None


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


def _run(job, url, headers, target, overwrite):
    part = target + ".part"
    try:
        with _slots:
            if job["cancel"]:
                job["status"] = "cancelled"
                return
            job["status"] = "downloading"
            os.makedirs(os.path.dirname(target), exist_ok=True)
            req = urllib.request.Request(url, headers=dict({"User-Agent": "nightshift-downloader/%d" % VERSION}, **headers))
            opener = urllib.request.build_opener(_RedirectHandler)
            with opener.open(req, timeout=60) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                job["total"] = total
                free = shutil.disk_usage(os.path.dirname(target)).free
                if total and total > free:
                    raise RuntimeError("디스크 여유 공간이 부족해요(필요 %d MB, 남음 %d MB)." % (total // 1048576, free // 1048576))
                with open(part, "wb") as out:
                    while True:
                        if job["cancel"]:
                            raise InterruptedError()
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
                        job["downloaded"] += len(chunk)
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


NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
