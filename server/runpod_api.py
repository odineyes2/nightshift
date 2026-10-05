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

## REST v2 (NS-33)

REST v1이 2026-11-15에 종료되어 v2(`api.runpod.io/v2`)를 쓴다 — 출처는 RunPod 이전 안내
(docs.runpod.io/api-reference-v2/migrate-from-v1)와 OpenAPI. v2 Pod는 `status`가 실제 수명주기
(PROVISIONING/STARTING/RUNNING/EXITED/ERROR/TERMINATED)라 "켜짐(과금 중)" 판정은 `is_on()` 하나로 한다.
필드는 `_pod_from_v2()`가 내부 키로 바꾼다. v2에는 종료 시각(`lastStatusChange`)이 없어 `last_status_change`는
항상 None이다. 오류는 RFC 9457 `{title, status, detail}` 형식이다.

## GPU 모델명

v2 Pod의 `gpu.id`가 표시 이름이다(예: "NVIDIA GeForce RTX 4090"). 비어 있을 때만 레거시 GraphQL API
(`https://api.runpod.io/graphql`)의 `pod.machine.gpuDisplayName`으로 한 번 더 물어본다
(`_fetch_gpu_display_name`). 이것도 실패하면 조용히 빈 채로 둔다 — GPU 이름 하나
때문에 카드의 나머지 정보(이름/비용/일시)까지 못 뜨면 안 되므로.

VRAM은 이 모듈이 다루지 않는다 — ComfyUI 자체의 `/system_stats`에서 이미 가져오고
있다(`drivers/comfyui.py`의 `card()`, 대시보드 카드의 주소 옆 숫자).
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import re

RUNPOD_API_KEY = (os.environ.get("RUNPOD_API_KEY") or "").strip()
RUNPOD_API_BASE = "https://api.runpod.io/v2"
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


ON_STATUSES = ("PROVISIONING", "STARTING", "RUNNING")


def is_on(status: str | None) -> bool:
    """v2 status가 "켜 둔 상태(시작 중 포함) = 과금 중"인지 — v1의 "원하는 상태 RUNNING"과 같은 뜻."""
    return status in ON_STATUSES


def _pod_from_v2(p: dict) -> dict:
    """v2 Pod 객체를 내부 키로 바꾼다. **env 필드는 절대 읽지 않는다** — 그 pod에 설정된 다른 환경변수
    (Jupyter 비밀번호 등)가 평문으로 들어 있다. 꺼진 파드의 cost는 0.0으로 오므로 None으로 둔다
    ("$0.00/hr"이나 0원 세션으로 남기지 않게)."""
    status = p.get("status") or ""
    cost = p.get("cost")
    if not is_on(status) and not cost:
        cost = None
    mounts = p.get("mounts") if isinstance(p.get("mounts"), dict) else {}
    persistent = mounts.get("persistent") if isinstance(mounts.get("persistent"), dict) else {}
    network = [m.get("volumeId") for m in (mounts.get("network") or []) if isinstance(m, dict) and m.get("volumeId")]
    gpu = p.get("gpu") if isinstance(p.get("gpu"), dict) else {}
    ports = p.get("ports")
    return {
        "id": p.get("id"),
        "name": p.get("name") or "",
        "status": status,
        "ports": [x for x in ports if isinstance(x, str)] if isinstance(ports, list) else [],
        "image": p.get("image") or "",
        "cost_per_hr": cost,
        # RFC 3339 원본 문자열(파싱은 runpod_sessions.py가 한다). v2에는 종료 시각 필드가 없다.
        "last_started_at": p.get("startedAt"),
        "last_status_change": None,
        "network_volume_id": network[0] if network else None,
        "gpu_type": gpu.get("id") or None,
        "created_at": p.get("createdAt"),
        "container_disk_gb": p.get("disk"),
        "volume_gb": persistent.get("size"),
        "data_center": p.get("dataCenterId") or "",
    }


def _fetch_pod_raw(pod_id: str) -> dict | None:
    raw, err = _rest("GET", f"/pods/{urllib.parse.quote(pod_id, safe='')}")
    return raw if not err and isinstance(raw, dict) else None


LIST_PAGE_LIMIT = 1000
LIST_MAX_PAGES = 20   # 커서가 끝나지 않는 이상 응답에 묶이지 않게


def list_runpod_pods_verbose() -> tuple[list[dict] | None, str | None]:
    """계정의 모든 pod를 훑어 (정규화된 목록, None) 또는 (None, 에러 문구)를 돌려준다 —
    runpod_sync.py(자동 등록)가 "실패 이유를 사람이 알 수 있어야" 해서 get_runpod_info와
    달리 에러를 삼키지 않는다. v2 응답은 `{"pods": [...], "pagination": {"nextCursor", "hasNextPage"}}`라
    다음 쪽이 있으면 cursor로 이어 받는다. 에러 문구에 raw 응답(env 비밀값)은 넣지 않는다."""
    pods, cursor = [], None
    for _ in range(LIST_MAX_PAGES):
        query = {"limit": LIST_PAGE_LIMIT, **({"cursor": cursor} if cursor else {})}
        raw, err = _rest("GET", "/pods?" + urllib.parse.urlencode(query))
        if err:
            return None, err
        if not isinstance(raw, dict) or not isinstance(raw.get("pods"), list):
            return None, "RunPod 응답 형식이 예상과 달라요."
        pods += [_pod_from_v2(p) for p in raw["pods"] if isinstance(p, dict) and p.get("id")]
        page = raw.get("pagination") if isinstance(raw.get("pagination"), dict) else {}
        cursor = page.get("nextCursor") if page.get("hasNextPage") else None
        if not cursor:
            return pods, None
    return None, "RunPod 목록이 너무 길어요(쪽 수 상한)."


# ---- 네트워크 볼륨 ----
# RunPod 네트워크 볼륨은 파드를 지워도 남아 매달 요금이 나간다(1TB 미만 GB당 월 $0.07 — RunPod 요금표, 2026-09).
# 목록을 보고 안 쓰는 것을 지우는 화면(워커 탭)이 쓴다. 응답 형식은 RunPod REST v2 OpenAPI 기준:
# GET /network-volumes → {"networkVolumes": [{id, name, size(GB), dataCenter, type}]}, DELETE /network-volumes/{id}.
NETWORK_VOLUME_USD_PER_GB_MONTH = 0.07


def _rest(method: str, path: str, timeout: float = REQUEST_TIMEOUT_SEC, body: dict | None = None):
    """(응답 JSON 또는 None, None) 또는 (None, 에러 문구). 에러 문구에 원 응답은 넣지 않는다(비밀값이 섞일 수 있어서)."""
    if not RUNPOD_API_KEY:
        return None, "RUNPOD_API_KEY가 설정되지 않았어요."
    headers = {"Authorization": f"Bearer {RUNPOD_API_KEY}", "User-Agent": RUNPOD_USER_AGENT, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{RUNPOD_API_BASE}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return (json.loads(body) if body.strip() else None), None
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8") or "{}")
            # v2는 RFC 9457 {title, status, detail}
            reason = str(err.get("detail") or err.get("title") or err.get("error") or err.get("message") or "")[:300] \
                if isinstance(err, dict) else ""
        except (json.JSONDecodeError, OSError):
            reason = ""
        return None, f"RunPod가 거절했어요(HTTP {e.code}){': ' + reason if reason else ''}"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        return None, f"RunPod에 요청하지 못했어요: {type(e).__name__}."


def list_network_volumes() -> tuple[list[dict] | None, str | None]:
    raw, err = _rest("GET", "/network-volumes")
    if err:
        return None, err
    if not isinstance(raw, dict) or not isinstance(raw.get("networkVolumes"), list):
        return None, "RunPod 응답 형식이 예상과 달라요."
    vols = []
    for v in raw["networkVolumes"]:
        if not isinstance(v, dict) or not v.get("id"):
            continue
        size = v.get("size") if isinstance(v.get("size"), (int, float)) else None
        vols.append({"id": v["id"], "name": v.get("name") or "", "size_gb": size, "data_center": v.get("dataCenter") or "",
                     "monthly_usd": round(size * NETWORK_VOLUME_USD_PER_GB_MONTH, 2) if size is not None else None})
    return vols, None


# ---- 충전 잔액(NS-31) ----
# REST(v1·v2)에는 잔액이 없어 GraphQL `myself.clientBalance`(남은 충전 금액, USD 숫자)만 쓴다 — NS-31-1에서 공식 스펙과
# 실측으로 확인했다. GraphQL은 2027년 초 종료 예정이라 그때 대체 경로로 옮겨야 한다.
_BALANCE_QUERY = "query { myself { clientBalance } }"


def get_client_balance() -> tuple[float | None, str | None]:
    """(남은 충전 금액 USD, None) 또는 (None, 에러 문구). 0달러는 성공이다 — 실패를 0으로 바꾸지 않는다."""
    if not RUNPOD_API_KEY:
        return None, "RUNPOD_API_KEY가 설정되지 않았어요."
    req = urllib.request.Request(
        RUNPOD_GRAPHQL_URL, data=json.dumps({"query": _BALANCE_QUERY}).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {RUNPOD_API_KEY}", "Content-Type": "application/json",
                 "User-Agent": RUNPOD_USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return None, f"RunPod가 거절했어요(HTTP {e.code})."
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        return None, f"RunPod에 요청하지 못했어요: {type(e).__name__}."
    if isinstance(raw, dict) and raw.get("errors"):
        return None, "RunPod가 잔액 조회를 거절했어요(키 권한을 확인해 주세요)."
    me = ((raw.get("data") if isinstance(raw, dict) else None) or {}).get("myself")
    bal = me.get("clientBalance") if isinstance(me, dict) else None
    if not isinstance(bal, (int, float)) or isinstance(bal, bool):
        return None, "RunPod 응답 형식이 예상과 달라요."
    return float(bal), None


def delete_network_volume(volume_id: str) -> str | None:
    """지우고 None, 실패하면 에러 문구. 되돌릴 수 없다 — 확인은 부르는 쪽(API)이 한다."""
    _body, err = _rest("DELETE", f"/network-volumes/{urllib.parse.quote(volume_id, safe='')}", timeout=30)
    return err


# ---- 파드 만들기·지우기(워커 추가 #12) ----
# 계획서 ~/.claude/plans/worker-runpod-create.md. 등급마다 GPU 후보를 싼·안정적인 순으로 두고, 지역은 서울에서 가까운
# AP-JP-1을 먼저 — Secure(일본) → Secure(아무 곳) → Community(일본) → Community(아무 곳) 순으로, 단계마다
# GPU 후보를 차례로 시도한다.
RUNPOD_COMFY_IMAGE = "runpod/comfyui:1.4.7-cuda13.0"   # 지금까지 손으로 만들어 쓰던 이미지
# 이미지가 CUDA 13을 쓰므로 드라이버가 받쳐 주는 호스트에만 만든다(아니면 ComfyUI가 시작하지 못한다 — NS-18).
# 이미지를 바꾸면 같이 바꾼다.
RUNPOD_CUDA_VERSIONS = ["13.0"]
RUNPOD_PORTS =["8188/http", "8888/http", "8080/http", "22/tcp"]   # ComfyUI · Jupyter · 파일 브라우저 · SSH
RUNPOD_PREFERRED_DATA_CENTERS = ["AP-JP-1"]
RUNPOD_TIERS = {
    "image": {"label": "이미지용", "vram": "16GB급", "disk_gb": 80, "price_hint": "시간당 약 $0.2~0.6",
              "gpus": ["NVIDIA RTX A4000", "NVIDIA RTX 4000 Ada Generation", "NVIDIA RTX A4500",
                       "NVIDIA RTX 2000 Ada Generation", "NVIDIA GeForce RTX 5080"]},
    "video": {"label": "영상용", "vram": "32GB급", "disk_gb": 150, "price_hint": "시간당 약 $0.35~1.0",
              "gpus": ["NVIDIA RTX PRO 4500 Blackwell", "NVIDIA GeForce RTX 5090"]},
}


def create_pod(name: str, tier: str, env: dict | None = None, entrypoint: list[str] | None = None) -> tuple[dict | None, str | None]:
    """등급(tier)대로 RunPod 파드를 만든다. ({id, name, gpu, cost_per_hr, data_center, cloud}, None) 또는 (None, 이유).
    응답의 env(비밀값)는 버린다."""
    spec = RUNPOD_TIERS.get(tier)
    if spec is None:
        return None, "알 수 없는 등급이에요."
    # v2는 gpu.id 하나만 받고 다른 GPU로 대체하지 않으므로 단계마다 후보를 하나씩 요청한다(실패 요청은 과금되지 않는다).
    # 400(자리 없음)·403(그 풀 권한 없음)·5xx는 다음 후보로, 401(키)·402(잔액)·422(요청 형식)·429(요청 제한)는
    # 어디서 해도 똑같이 실패하니 바로 멈춘다.
    errors = []
    for cloud, dcs in (("SECURE", RUNPOD_PREFERRED_DATA_CENTERS), ("SECURE", None),
                       ("COMMUNITY", RUNPOD_PREFERRED_DATA_CENTERS), ("COMMUNITY", None)):
        for gpu_id in spec["gpus"]:
            body = {"name": name, "image": RUNPOD_COMFY_IMAGE, "cloud": cloud,
                    "gpu": {"id": gpu_id, "count": 1, "allowedCudaVersions": RUNPOD_CUDA_VERSIONS},
                    "disk": spec["disk_gb"], "ports": RUNPOD_PORTS}   # mounts가 없으면 볼륨 없음
            if dcs:
                body["dataCenterIds"] = dcs
            if env:
                body["env"] = env
            if entrypoint:
                body["args"] = {"entrypoint": entrypoint}   # 이미지의 ENTRYPOINT를 바꾼다(CMD가 아니라 — runpod/comfyui는 CMD가 없다)
            raw, err = _rest("POST", "/pods", timeout=60, body=body)
            if err:
                errors.append(f"{cloud}{'·' + dcs[0] if dcs else ''}·{gpu_id}: {err}")
                if any(f"HTTP {c}" in err for c in (401, 402, 422, 429)):
                    return None, "파드를 만들지 못했어요 — " + " / ".join(errors)
                continue
            if not isinstance(raw, dict) or not raw.get("id"):
                errors.append(f"{cloud}·{gpu_id}: 응답 형식이 예상과 달라요")
                continue
            pod = _pod_from_v2(raw)
            return {"id": pod["id"], "name": pod["name"] or name, "cloud": cloud, "cost_per_hr": raw.get("cost"),
                    "gpu": pod["gpu_type"] or gpu_id, "data_center": pod["data_center"]}, None
    return None, "파드를 만들지 못했어요 — " + " / ".join(errors)


def terminate_pod(pod_id: str) -> str | None:
    """RunPod 파드를 지운다(terminate — 디스크까지). 되돌릴 수 없다. 실패하면 이유, 성공하면 None."""
    _cache.pop(pod_id, None)
    _body, err = _rest("DELETE", f"/pods/{urllib.parse.quote(pod_id, safe='')}", timeout=30)
    return err


def list_runpod_pods() -> list[dict] | None:
    """list_runpod_pods_verbose()의 목록만(에러 문구 없이) 돌려준다 — 실패하면 None."""
    pods, _error = list_runpod_pods_verbose()
    return pods


def _fetch_gpu_display_name(pod_id: str) -> str | None:
    """REST의 machine이 비어 있을 때 레거시 GraphQL API로 GPU 모델명만 보충한다.
    이 호출 하나가 실패해도(네트워크/스키마 변경 등) 조용히 None — 호출부가 REST
    쪽 데이터는 그대로 쓸 수 있어야 하므로.

    키는 쿼리스트링이 아니라 Authorization 헤더로 보낸다 — URL에 실으면 프록시/
    접속 로그에 평문으로 남을 수 있다(RunPod GraphQL 공식 문서가 권장하는 방식이
    Authorization: Bearer 헤더다. 쿼리스트링 api_key는 예전 예제에서만 보이는 방식)."""
    body = json.dumps({
        "query": _GPU_DISPLAY_NAME_QUERY,
        "variables": {"podId": pod_id},
    }).encode("utf-8")
    req = urllib.request.Request(
        RUNPOD_GRAPHQL_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {RUNPOD_API_KEY}",
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


# pod 하나에 배정된 GPU 모델은 그 pod의 수명 내내 안 바뀐다(정지했다 다시 켜도 같은
# 머신) — 매 sync마다 GraphQL을 다시 부르지 않게 프로세스 메모리에 캐시해 둔다.
_gpu_type_cache: dict[str, str] = {}


def get_gpu_type_cached(pod_id: str) -> str | None:
    if pod_id not in _gpu_type_cache:
        _gpu_type_cache[pod_id] = _fetch_gpu_display_name(pod_id) or ""
    return _gpu_type_cache[pod_id] or None


def _normalize(raw: dict) -> dict:
    """카드용 — _pod_from_v2의 키 중 카드가 쓰는 것만, 값이 있는 것만. 디스크(NS-9): 컨테이너 디스크는 끄거나
    지우면 사라지고, 볼륨은 끄면 남고, 네트워크 볼륨은 지워도 남는다."""
    p = _pod_from_v2(raw)
    info = {"pod_name": p["name"] or None, "status": p["status"] or None, "ports": p["ports"] if isinstance(raw.get("ports"), list) else None,
            **{k: p[k] for k in ("gpu_type", "cost_per_hr", "created_at", "last_started_at",
                                 "container_disk_gb", "volume_gb", "network_volume_id")}}
    return {k: v for k, v in info.items() if v is not None}


# 파드 카드·스코프 바의 바로가기 — RunPod 공식 ComfyUI 이미지가 여는 웹 서비스들. 파드에 실제로
# 열린 http 포트만 링크로 만든다(템플릿마다 다를 수 있으므로 추측해서 만들지 않는다).
PROXY_LINK_PORTS = [("8188", "comfyui", "ComfyUI"), ("8888", "jupyter", "JupyterLab"), ("8080", "files", "파일 브라우저")]


POD_ACTIONS = ("start", "stop")


def pod_action(pod_id: str, action: str) -> tuple[str | None, str | None]:
    """RunPod 파드를 켜거나(start) 끈다(stop) — v2 `POST /pods/{id}/action` {"action": ...}.
    (목표 상태 "RUNNING"/"EXITED", None) 또는 (None, 에러 문구). v2 응답 status는 STARTING일 수 있지만 화면은
    v1처럼 목표 상태를 기대하므로 그것을 돌려주고, env(비밀값)가 든 응답 본문은 버린다. 실패 문구는 RunPod가 준
    이유를 그대로 옮긴다 — "GPU가 모자라서 못 켠다" 같은 이유를 사람이 봐야 다음 행동을 정할 수 있어서다."""
    if action not in POD_ACTIONS:
        return None, "알 수 없는 동작이에요."
    _cache.pop(pod_id, None)   # 켜고 끈 직후 카드가 옛 상태를 보여주지 않게
    _body, err = _rest("POST", f"/pods/{urllib.parse.quote(pod_id, safe='')}/action", timeout=30, body={"action": action})
    if err:
        return None, err
    return ("RUNNING" if action == "start" else "EXITED"), None


def proxy_links(pod_id: str, ports: list[str]) -> list[dict]:
    """RunPod 파드의 열린 http 포트 중 아는 서비스만 프록시 주소 링크로 돌려준다."""
    open_http = {p.split("/", 1)[0] for p in ports if p.endswith("/http")}
    return [{"kind": kind, "label": label, "url": f"https://{pod_id}-{port}.proxy.runpod.net/"}
            for port, kind, label in PROXY_LINK_PORTS if port in open_http]


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
            # env 필드는 그 pod에 설정된 다른 환경변수(비밀번호 등)를 평문으로 담고
            # 있을 수 있어(list_runpod_pods_verbose와 같은 이유) 화면에 그대로 보여주지
            # 않는다 — 진단 목적(HTTP 코드/기타 필드 확인)에는 필요 없는 값이다.
            if isinstance(raw, dict) and "env" in raw:
                raw = {**raw, "env": "(생략 — 비밀값이 담길 수 있어 안 보여줌)"}
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


__all__ = ["get_runpod_info", "debug_probe", "extract_pod_id", "proxy_links", "pod_action", "RUNPOD_API_KEY",
           "list_runpod_pods", "list_runpod_pods_verbose", "get_gpu_type_cached", "is_on"]
