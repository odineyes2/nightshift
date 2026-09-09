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

## 필드 이름을 100% 확신하지 못한다

이 환경(샌드박스)에서는 RunPod의 API 도메인 자체에 나갈 수 없어 실제 응답을 직접
받아본 적이 없다 — GraphQL 기반 구버전 SDK 문서와 최신 REST API에 대한 간접적인
근거(검색 스니펫)만으로 아래 필드 이름을 정했다. 실제로 값이 하나도 안 뜨거나
다르게 뜬다면, 응답 원본을 한 번 찍어봐야 정확한 키 이름을 알 수 있다 — 그래서
normalize()가 후보 키를 여러 개 시도하도록 방어적으로 짜여 있다.
"""

import json
import os
import time
import urllib.error
import urllib.request
import re

RUNPOD_API_KEY = (os.environ.get("RUNPOD_API_KEY") or "").strip()
RUNPOD_API_BASE = "https://rest.runpod.io/v1"

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
    _cache[pod_id] = {"data": data, "at": now}
    return data


__all__ = ["get_runpod_info", "extract_pod_id", "RUNPOD_API_KEY"]
