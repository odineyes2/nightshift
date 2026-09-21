"""
모델 내려받기 — Civitai / Hugging Face / 직접 주소에서 파드로.

ComfyUI에는 모델을 받는 API가 없고 nightshift는 파드 안에서 명령을 돌릴 수 없어(파드와는 HTTP만 오간다),
파드 쪽에 작은 커스텀 노드(templates/comfy_nodes/nightshift_downloader)를 한 번 설치해 두고 그 노드의
HTTP 라우트를 부른다. 이 모듈은
  - 주소를 풀어서(resolve) 받을 파일/종류/메타데이터를 알아내고,
  - 파드별 토큰과 설치 스크립트를 만들고,
  - 파드의 노드를 호출한다.

서버가 직접 접속하는 곳은 Civitai/Hugging Face(고정 호스트)뿐이다 — 회원이 넣은 임의 주소를 서버가 열면
서버 안쪽을 찌르는 통로가 되기 때문이다. 임의 주소는 파드가 직접 받는다.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import db
from drivers.comfyui import COMFY_USER_AGENT, ComfyUIDriver

NODE_DIR = Path(__file__).resolve().parent.parent / "templates" / "comfy_nodes" / "nightshift_downloader"
NODE_VERSION = 1
FETCH_TIMEOUT = 15
MODEL_EXT = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf")
API_HOSTS = {"civitai.com", "www.civitai.com", "huggingface.co", "www.huggingface.co"}

# Civitai 모델 type -> 등록부 종류
CIVITAI_KINDS = {
    "checkpoint": "checkpoints", "lora": "loras", "locon": "loras", "dora": "loras", "vae": "vae",
    "controlnet": "controlnet", "upscaler": "upscale_models",
}


class DownloadError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---- 파드별 토큰 --------------------------------------------------------------

def _secret() -> str:
    with db.connect() as conn:
        value = db.get_meta(conn, "downloader_secret")
        if not value:
            value = secrets.token_hex(32)
            db.set_meta(conn, "downloader_secret", value)
    return value


def pod_token(pod_id: str) -> str:
    """파드마다 다른 토큰 — 저장하지 않고 서버 비밀값과 파드 id로 매번 계산한다(pods.json에 안 남는다)."""
    return hmac.new(_secret().encode(), f"downloader:{pod_id}".encode(), hashlib.sha256).hexdigest()[:40]


def install_script(pod_id: str) -> str:
    """파드의 터미널에 붙여 넣는 설치 스크립트 — 노드 소스와 이 파드의 토큰이 들어 있다."""
    source = (NODE_DIR / "__init__.py").read_bytes()
    b64 = base64.b64encode(source).decode()
    wrapped = "\n".join(b64[i:i + 76] for i in range(0, len(b64), 76))
    return f"""#!/bin/bash
# nightshift 모델 다운로더 설치 (v{NODE_VERSION}) — ComfyUI가 있는 파드의 터미널에서 실행하세요.
set -e
find_dir() {{
  for d in "$1" /workspace/ComfyUI /workspace/runpod-slim/ComfyUI /ComfyUI "$HOME/ComfyUI"; do
    if [ -n "$d" ] && [ -d "$d/custom_nodes" ]; then echo "$d"; return; fi
  done
}}
COMFY_DIR="$(find_dir "$COMFY_DIR")"
if [ -z "$COMFY_DIR" ]; then
  echo "ComfyUI 폴더를 못 찾았어요. COMFY_DIR=/ComfyUI/경로 를 앞에 붙여 다시 실행하세요."; exit 1
fi
DEST="$COMFY_DIR/custom_nodes/nightshift_downloader"
mkdir -p "$DEST"
base64 -d > "$DEST/__init__.py" <<'NIGHTSHIFT_B64'
{wrapped}
NIGHTSHIFT_B64
printf '%s' '{pod_token(pod_id)}' > "$DEST/token"
chmod 600 "$DEST/token"
echo "설치 완료: $DEST"
echo "ComfyUI를 다시 시작해야 적용돼요."
"""


# ---- 주소 풀기 ------------------------------------------------------------------

def _get_json(url: str, token: str | None = None):
    host = urllib.parse.urlparse(url).hostname or ""
    if host not in API_HOSTS:
        raise DownloadError("지원하지 않는 호스트예요.")
    headers = {"User-Agent": COMFY_USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise DownloadError("접근 권한이 없어요 — 토큰이 필요한 모델일 수 있어요.")
        if e.code == 404:
            raise DownloadError("주소에서 모델을 찾을 수 없어요.")
        raise DownloadError(f"조회에 실패했어요(HTTP {e.code}).")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise DownloadError(f"조회에 실패했어요: {getattr(e, 'reason', e)}")


def _clean_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._ ()+\[\]-]", "_", name.strip()).lstrip("._- ") or "model.safetensors"


def _resolve_civitai(parsed, token):
    path = parsed.path
    query = urllib.parse.parse_qs(parsed.query)
    version_id = None
    m = re.match(r"^/api/download/models/(\d+)", path)
    if m:
        version_id = m.group(1)
    else:
        m = re.match(r"^/models/(\d+)", path)
        if not m:
            raise DownloadError("Civitai 모델 페이지 주소(civitai.com/models/…)를 넣어주세요.")
        version_id = (query.get("modelVersionId") or [None])[0]
        if not version_id:
            model = _get_json(f"https://civitai.com/api/v1/models/{m.group(1)}", token)
            versions = model.get("modelVersions") or []
            if not versions:
                raise DownloadError("이 모델에는 받을 수 있는 버전이 없어요.")
            version_id = str(versions[0]["id"])
    v = _get_json(f"https://civitai.com/api/v1/model-versions/{int(version_id)}", token)
    files = [f for f in (v.get("files") or []) if str(f.get("name", "")).lower().endswith(MODEL_EXT)]
    if not files:
        raise DownloadError("받을 수 있는 모델 파일이 없어요.")
    files.sort(key=lambda f: (not f.get("primary"), f.get("sizeKB") or 0))
    f = files[0]
    model = v.get("model") or {}
    ctype = str(model.get("type") or "").lower()
    images = [i.get("url") for i in (v.get("images") or []) if i.get("type", "image") == "image" and i.get("url")]
    return {
        "source": "civitai",
        "name": f"{model.get('name') or ''} {v.get('name') or ''}".strip(),
        "download_url": f"https://civitai.com/api/download/models/{int(version_id)}",
        "filename": _clean_filename(f["name"]),
        "kind": CIVITAI_KINDS.get(ctype),
        "civitai_type": model.get("type"),
        "base_model": v.get("baseModel") or "",
        "trigger_keyword": ", ".join(v.get("trainedWords") or []) if CIVITAI_KINDS.get(ctype) == "loras" else "",
        "size_bytes": int((f.get("sizeKB") or 0) * 1024) or None,
        "preview_url": images[0] if images else None,
        "page_url": f"https://civitai.com/models/{model.get('id')}?modelVersionId={version_id}" if model.get("id") else "",
        "needs_token": bool(v.get("earlyAccessEndsAt")) or None,
    }


def _resolve_hf(parsed, token):
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise DownloadError("Hugging Face 저장소 주소를 넣어주세요(huggingface.co/작성자/저장소).")
    repo = "/".join(parts[:2])
    if len(parts) >= 5 and parts[2] in ("blob", "resolve"):
        rev = parts[3]
        file_path = "/".join(parts[4:])
        if not file_path.lower().endswith(MODEL_EXT):
            raise DownloadError("모델 파일(.safetensors 등)을 가리키는 주소를 넣어주세요.")
        quoted = urllib.parse.quote(file_path)
        return {
            "source": "huggingface",
            "name": f"{repo}/{file_path.split('/')[-1]}",
            "download_url": f"https://huggingface.co/{repo}/resolve/{urllib.parse.quote(rev)}/{quoted}",
            "filename": _clean_filename(file_path.split("/")[-1]),
            "kind": None, "base_model": "", "trigger_keyword": "", "size_bytes": None, "preview_url": None,
            "page_url": f"https://huggingface.co/{repo}/blob/{rev}/{file_path}",
        }
    info = _get_json(f"https://huggingface.co/api/models/{repo}", token)
    candidates = [s["rfilename"] for s in (info.get("siblings") or []) if str(s.get("rfilename", "")).lower().endswith(MODEL_EXT)]
    if not candidates:
        raise DownloadError("이 저장소에서 모델 파일을 찾지 못했어요.")
    return {"source": "huggingface", "name": repo, "candidates": candidates[:100], "repo": repo,
            "choose_file": True, "download_url": None, "filename": None, "kind": None}


def resolve(url: str, token: str | None = None) -> dict:
    parsed = urllib.parse.urlparse((url or "").strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise DownloadError("https:// 로 시작하는 주소를 넣어주세요.")
    host = parsed.hostname.lower()
    if host in ("civitai.com", "www.civitai.com"):
        return _resolve_civitai(parsed, token)
    if host in ("huggingface.co", "www.huggingface.co"):
        return _resolve_hf(parsed, token)
    name = _clean_filename(urllib.parse.unquote(parsed.path.rsplit("/", 1)[-1]))
    if not name.lower().endswith(MODEL_EXT):
        raise DownloadError("파일명을 알 수 없어요 — 모델 파일 주소이거나 Civitai/Hugging Face 주소여야 해요.")
    return {"source": "direct", "name": name, "download_url": url.strip(), "filename": name, "kind": None,
            "base_model": "", "trigger_keyword": "", "size_bytes": None, "preview_url": None, "page_url": url.strip()}


def auth_header_for(url: str, token: str | None) -> dict:
    """토큰은 Civitai/Hugging Face 받기 주소에만 붙인다(직접 주소에 붙이면 남의 서버에 토큰을 보내게 된다)."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if token and host in API_HOSTS:
        return {"Authorization": f"Bearer {token}"}
    return {}


# ---- 파드의 노드 호출 -----------------------------------------------------------

def call_node(pod: dict, method: str, path: str, body: dict | None = None, timeout: float = 20):
    """파드의 nightshift_downloader 노드를 부른다. 안 깔려 있으면 DownloadError(404)."""
    url, _source = ComfyUIDriver.configured(pod)
    if not url:
        raise DownloadError("파드 주소가 설정돼 있지 않아요.")
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": COMFY_USER_AGENT, "X-Nightshift-Token": pod_token(pod["id"])}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{url.rstrip('/')}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error")
        except Exception:
            detail = None
        if e.code in (404, 405, 501) and not detail:
            raise DownloadError("이 파드에는 다운로더가 설치돼 있지 않아요(설치 후 ComfyUI 재시작 필요).", 404)
        if e.code == 401:
            raise DownloadError("다운로더 토큰이 맞지 않아요 — 이 파드에 설치 스크립트를 다시 실행해 주세요.", 409)
        raise DownloadError(detail or f"파드가 오류를 돌려줬어요(HTTP {e.code}).", 409 if e.code == 409 else 400)
    except (urllib.error.URLError, TimeoutError, OSError):
        raise DownloadError("파드에 연결하지 못했어요.", 502)


def node_status(pod: dict) -> dict:
    """{installed, connected, version?, folders?, reason?}"""
    try:
        info = call_node(pod, "GET", "/nightshift/dl/ping", timeout=10)
    except DownloadError as e:
        return {"installed": False, "connected": e.status != 502, "reason": str(e)}
    return {"installed": True, "connected": True, "version": info.get("version"), "folders": info.get("folders") or {},
            "outdated": (info.get("version") or 0) < NODE_VERSION}
