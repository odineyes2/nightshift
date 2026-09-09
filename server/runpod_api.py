"""
RunPod API로 파드 메타데이터를 보충한다 — 이름(nightshift가 준 이름 말고 RunPod가
자동으로 붙인 이름), GPU 종류, 시간당 비용, 생성/최근 시작 일시. nightshift가 이미
알고 있는 것(연결 상태, url)과는 무관한 "RunPod 콘솔에서나 보이던 정보"를 대시보드
카드에 얹기 위한 것뿐이라, 실패해도 카드 자체엔 영향이 없어야 한다 — 그래서 이
모듈의 모든 함수는 조회에 실패하면 예외를 삼키고 None/빈 dict를 돌려준다.

## 켜는 법

RUNPOD_API_KEY 환경변수가 없으면 이 기능은 통째로 꺼진다(조용히 None). RunPod
콘솔(Settings → API Keys)에서 발급받아 .env에 넣으면 된다.

## pod id를 어디서 얻는가

파드 레코드에 별도 필드를 추가하지 않는다 — 파드의 url이 RunPod 프록시 주소
(`https://{POD_ID}-{PORT}.proxy.runpod.net/`)라면 그 자체에 pod id가 박혀 있으므로
거기서 뽑아 쓴다. url이 그 형태가 아니면(로컬 ComfyUI, 다른 클라우드 등) 이 기능은
그냥 아무것도 하지 않는다.

## GPU 모델명이 REST API에는 안 실려 온다 (실측 확인됨)

`GET /v1/pods/{id}`의 실제 응답을 사용자가 "RunPod 정보 테스트" 버튼으로 찍어봐
확인한 결과, `machine` 필드가 빈 객체(`{}`)로 온다 — `gpuCount`(GPU 개수)만 있고
어떤 GPU인지(모델명)는 REST 쪽에 없다. 반면 레거시 GraphQL API
(`https://api.runpod.io/graphql`)의 `pod.machine.gpuDisplayName`에는 있다고 알려져
있어서, REST 응답에 GPU 모델명이 비어 있을 때만 그쪽으로 한 번 더 물어본다
(`_fetch_gpu_display_name`). 이것도 실패하면 조용히 빈 채로 둔다 — GPU 이름 하나
때문에 카드의 나머지 정보(이름/비용/일시)까지 못 뜨면 안 되므로.

VRAM은 이 모듈이 다루지 않는다 — ComfyUI 자체의 `/system_stats`에서 이미 가져오고
있다(`drivers/comfyui.py`의 `card()`, 대시보드 카드의 주소 옆 숫자).
"""

import json
import os
import time
import urllib.error
import urllib.request
import re

RUNPOD_API_KEY = (os.environ.get("RUNPOD_API_KEY") or "").strip()
RUNPOD_API_BASE = "https://rest.runpod.io/v1"
RUNPOD_GRAPHQL_URL = "https://api.runpod.io/graphql"

# REST 응답에 GPU 모델명이 없을 때만 여기로 한 번 더 물어본다(모듈 docstring 참고).
_GPU_DISPLAY_NAME_QUERY = (
    "query PodGpu($podId: String!) { pod(input: {podId: $podId}) { machine { gpuDisplayName } } }"
)

# ComfyUI 쪽과 같은 이유(Cloudflare가 기본 UA를 막을 수 있다)로 흔한 브라우저 UA를
# 실어 보낸다. drivers/comfyui.py의 COMFY_USER_AGENT와 값은 같지만, 그쪽을 import하면
# 순환(comfyui.py -> comfy_outputs.py -> ...)까지는 아니라도 이 모듈이 드라이버 계층에
# 얹히는 모양이 되어 그냥 따로 둔다.
RUNPOD_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT_SEC = 6

# 이름/GPU/비용/생성일시는 사실상 안 바뀌고, lastStartedAt만 파드를 껐다 켤 때
# 바뀐다 — 그래도 대시보드 카드 캐시(POD_CARD_TTL_SEC=20s)보다 더 자주 RunPod API를
# 두드릴 필요는 없으니 여유 있게 잡는다. 매 카드 새로고침마다 이 캐시도 함께
# 확인하므로, 여기 값이 실질적인 "RunPod API 호출 간격"이 된다.
CACHE_TTL_SEC = 120
_cache: dict[str, dict] = {}

_PROXY_HOST_RE = re.compile(r"^([a-z0-9]+)-\d+\.proxy\.runpod\.net$")


def extract_pod_id(url: str) -> str | None:
    """RunPod 프록시 주소(https://{POD_ID}-{PORT}.proxy.runpod.net/)에서 POD_ID를
    뽑는다. 그 형태가 아니면 None — 로컬 ComfyUI나 다른 호스팅일 수 있으므로."""
    if not url:
        return None
    import urllib.parse
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return None
    m = _PROXY_HOST_RE.match(host.lower())
    return m.group(1) if m else None


def _fetch_pod_raw(pod_id: str) -> dict | None:
    req = urllib.request.Request(
        f"{RUNPOD_API_BASE}/pods/{pod_id}",
        headers={
            "Authorization": f"Bearer {RUNPOD_API_KEY}",
            "User-Agent": RUNPOD_USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _fetch_gpu_display_name(pod_id: str) -> str | None:
    """REST의 machine이 비어 있을 때 레거시 GraphQL API로 GPU 모델명만 보충한다.
    이 호출 하나가 실패해도(네트워크/스키마 변경 등) 조용히 None — 호출부가 REST
    쪽 데이터는 그대로 쓸 수 있어야 하므로."""
    body = json.dumps({
        "query": _GPU_DISPLAY_NAME_QUERY,
        "variables": {"podId": pod_id},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{RUNPOD_GRAPHQL_URL}?api_key={RUNPOD_API_KEY}",
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": RUNPOD_USER_AGENT,
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    pod = (data.get("data") or {}).get("pod") or {}
    machine = pod.get("machine") or {}
    return machine.get("gpuDisplayName") or None


def _first(d: dict, *paths):
    """paths 중 첫 번째로 값이 있는 걸 돌려준다. path는 "a.b" 형태의 점 표기.
    REST API 필드가 정확히 뭔지 확신이 없어서(모듈 docstring 참고) 후보를 여러 개
    시도하도록 이렇게 짰다."""
    for path in paths:
        cur = d
        for key in path.split("."):
            if not isinstance(cur, dict) or key not in cur:
                cur = None
                break
            cur = cur[key]
        if cur is not None:
            return cur
    return None


def _normalize(raw: dict) -> dict:
    info = {
        "pod_name": _first(raw, "name"),
        "gpu_type": _first(raw, "machine.gpuDisplayName", "gpuDisplayName", "gpuTypeId", "gpu.displayName"),
        "cost_per_hr": _first(raw, "costPerHr", "costPerHour", "adjustedCostPerHr"),
        "created_at": _first(raw, "createdAt", "created_at"),
        "last_started_at": _first(raw, "lastStartedAt", "last_started_at", "lastStartAt"),
    }
    return {k: v for k, v in info.items() if v is not None}


def debug_probe(url: str) -> dict:
    """진단 전용 — 캐시를 타지 않고 매번 새로 조회하며, get_runpod_info()와 달리
    실패 이유를 삼키지 않고 그대로 드러낸다(HTTP 상태 코드, 응답 본문, 키 미설정,
    주소 형식 불일치 등). "카드에 정보가 안 뜨는데 왜 안 뜨는지 직접 확인하고
    싶다"는 요청에 답하기 위한 것 — 화면의 "RunPod 정보 테스트" 버튼이 이걸 부른다."""
    pod_id = extract_pod_id(url)
    result: dict = {
        "url": url,
        "api_key_set": bool(RUNPOD_API_KEY),
        "extracted_pod_id": pod_id,
    }
    if not RUNPOD_API_KEY:
        result["error"] = "RUNPOD_API_KEY가 설정되지 않았어요."
        return result
    if not pod_id:
        result["error"] = (
            "이 주소는 RunPod 프록시 주소 형식이 아니에요 "
            "(https://{POD_ID}-{PORT}.proxy.runpod.net/)."
        )
        return result

    req = urllib.request.Request(
        f"{RUNPOD_API_BASE}/pods/{pod_id}",
        headers={
            "Authorization": f"Bearer {RUNPOD_API_KEY}",
            "User-Agent": RUNPOD_USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8")
            result["status_code"] = resp.status
            try:
                raw = json.loads(body)
            except json.JSONDecodeError:
                result["error"] = "응답이 JSON이 아니에요."
                result["raw_body"] = body[:2000]
                return result
            result["raw_response"] = raw
            normalized = _normalize(raw)
            if not normalized.get("gpu_type"):
                gpu = _fetch_gpu_display_name(pod_id)
                result["graphql_gpu_type"] = gpu   # REST에 없어서 시도한 결과 — None이면 그것도 실패
                if gpu:
                    normalized["gpu_type"] = gpu
            result["normalized"] = normalized
    except urllib.error.HTTPError as e:
        result["status_code"] = e.code
        try:
            result["raw_body"] = e.read().decode("utf-8")[:2000]
        except Exception:
            pass
        result["error"] = f"HTTP {e.code} {e.reason}"
    except urllib.error.URLError as e:
        result["error"] = f"연결 실패: {e.reason}"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def get_runpod_info(url: str) -> dict | None:
    """url이 RunPod 프록시 주소이고 RUNPOD_API_KEY가 설정돼 있으면 그 파드의 RunPod
    메타데이터를, 아니면 None을 돌려준다. 실패(키 없음/네트워크/파싱)는 전부 조용히
    None으로 처리한다 — 이 조회 하나가 실패했다고 카드 전체가 안 뜨면 안 되므로."""
    if not RUNPOD_API_KEY:
        return None
    pod_id = extract_pod_id(url)
    if not pod_id:
        return None

    now = time.monotonic()
    cached = _cache.get(pod_id)
    if cached and now - cached["at"] < CACHE_TTL_SEC:
        return cached["data"] or None

    raw = _fetch_pod_raw(pod_id)
    data = _normalize(raw) if raw else None
    if data is not None and not data.get("gpu_type"):
        gpu = _fetch_gpu_display_name(pod_id)
        if gpu:
            data["gpu_type"] = gpu
    _cache[pod_id] = {"data": data, "at": now}
    return data


__all__ = ["get_runpod_info", "debug_probe", "extract_pod_id", "RUNPOD_API_KEY"]
