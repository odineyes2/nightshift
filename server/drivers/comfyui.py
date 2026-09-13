"""
ComfyUI 파드 드라이버 — 지금까지 app.py에 흩어져 있던 "ComfyUI를 어떻게 다루는가"를
한 곳에 모은 것이다. 새로 만든 로직은 없고, 파드 레코드를 인자로 받게 바꿨을 뿐이다.

## 주소 우선순위 (A-1에서 정한 것 그대로)

    1. 파드 레코드의 url — 화면에서 저장한 값
    2. COMFY_URL 환경변수 — 배포 시점 기본값 (파드 url이 비어 있을 때만)
    3. 자동 탐지 — 127.0.0.1 후보들을 짧은 타임아웃으로 찔러본다

파드가 여러 대가 되면 2·3번은 사실상 "url을 아직 안 채운 파드"를 위한 폴백이다. 원격 pod를
여러 대 굴리는 구성에서는 파드마다 url이 채워져 있게 된다.

환경변수:
    NIGHTSHIFT_COMFY_TIMEOUT_SEC  명시적으로 지정된 주소의 연결 확인 타임아웃 (기본 5)
"""

import json
import os
import time
import urllib.request

from comfy_outputs import OutputSyncError, sync_outputs
from runpod_api import get_runpod_info

from .base import PodDriver

CANDIDATE_URLS = ["http://127.0.0.1:8188", "http://127.0.0.1:8000"]

# RunPod의 프록시 주소(https://{POD_ID}-{PORT}.proxy.runpod.net/)는 Cloudflare가
# 앞단에 있다. Cloudflare의 봇 차단이 파이썬 urllib의 기본 User-Agent
# ("Python-urllib/3.x")를 감지해 403으로 막는데, 이게 curl이나 브라우저로는
# 멀쩡히 열리는 pod가 nightshift에서만 "응답이 없어요"로 보이던 진짜 원인이었다
# (check_url()이 모든 예외를 뭉뚱그려 False로 돌려주는 바람에 403이라는 사실 자체가
# 화면에 드러나지 않았다). 그래서 ComfyUI로 보내는 요청에는 전부 이 헤더를 실어
# 보낸다 — 값 자체는 임의의 흔한 브라우저 UA면 충분하다(그 pod 앞단이 무엇을
# 필터링하든 브라우저는 원래 통과시켜야 하므로).
COMFY_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 연결 확인 타임아웃은 "어디를 찌르는지"에 따라 다르게 잡는다.
#   - 자동 탐지 후보는 정의상 전부 127.0.0.1이라 응답이 없으면 즉시 실패한다. 여기에 긴
#     타임아웃을 쓰면 ComfyUI가 없는 머신에서 후보 수만큼 초를 버린다.
#   - 반대로 명시적으로 지정된 주소는 원격 pod일 수 있다. WAN 왕복 + TLS 핸드셰이크가
#     붙으면 2초는 쉽게 넘고, 그러면 멀쩡히 떠 있는 pod가 "연결 안 됨"으로 깜빡인다.
CHECK_TIMEOUT_LOCAL = 2
CHECK_TIMEOUT = float(os.environ.get("NIGHTSHIFT_COMFY_TIMEOUT_SEC", "5"))
# 사용자가 "연결 테스트"/"저장"으로 명시적으로 확인할 때는 더 넉넉하게 기다린다 —
# 사람이 버튼을 누르고 결과를 기다리는 중이므로 몇 초 더 쓰는 편이 낫다.
CHECK_TIMEOUT_INTERACTIVE = 8

# /object_info는 커스텀 노드가 많이 깔린 서버에서 응답이 수 MB까지 커지고 모델 폴더를
# 훑느라 느리기도 해서 짧게 캐싱한다 — 모델을 새로 설치하는 일은 드물고, 필요하면
# force=True로 강제로 다시 받아온다. 파드마다 설치된 것이 다를 수 있으므로 파드별로 담는다.
OBJECT_INFO_TTL_SEC = 120
_object_info_cache: dict[str, dict] = {}


def check_url(url: str, timeout: float = CHECK_TIMEOUT) -> bool:
    try:
        req = urllib.request.Request(
            f"{url.rstrip('/')}/system_stats", headers={"User-Agent": COMFY_USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _fetch_json(url: str, path: str, timeout: float):
    req = urllib.request.Request(
        f"{url.rstrip('/')}{path}", headers={"User-Agent": COMFY_USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class ComfyUIDriver(PodDriver):
    kind = "comfyui"
    label = "ComfyUI"

    @staticmethod
    def configured(pod: dict) -> tuple[str | None, str]:
        """명시적으로 정해진 주소와 그 출처("setting"/"env"). 어느 쪽에도 없으면
        (None, "auto") — 이때만 자동 탐지로 넘어간다."""
        saved = (pod.get("url") or "").strip()
        if saved:
            return saved, "setting"
        env_url = (os.environ.get("COMFY_URL") or "").strip()
        if env_url:
            return env_url, "env"
        return None, "auto"

    @staticmethod
    def resolve(pod: dict) -> tuple[str | None, str]:
        """연결 확인 없이 "이 파드가 가리키는 주소"만 정한다. 자동 탐지인 경우에는
        찔러봐야 알 수 있으므로 health()에서 결정된다."""
        url, source = ComfyUIDriver.configured(pod)
        return url, source

    @staticmethod
    def health(pod: dict, timeout: float | None = None) -> dict:
        url, source = ComfyUIDriver.configured(pod)
        if url:
            ok = check_url(url, timeout if timeout is not None else CHECK_TIMEOUT)
            return {"ok": ok, "url": url, "source": source,
                    "detail": "" if ok else "응답이 없어요."}
        for candidate in CANDIDATE_URLS:
            if check_url(candidate, CHECK_TIMEOUT_LOCAL):
                return {"ok": True, "url": candidate, "source": "auto", "detail": ""}
        return {"ok": False, "url": None, "source": "auto",
                "detail": "자동 탐지 후보에서 ComfyUI를 찾지 못했어요."}

    @staticmethod
    def capabilities(pod: dict, force: bool = False) -> tuple[str | None, dict | None]:
        """(주소, /object_info). ComfyUI가 안 떠 있으면 (주소, None).

        /object_info는 그 서버에 설치된 모든 노드 타입과, 파일을 고르는 입력
        (체크포인트/LoRA/VAE 등)이 실제로 고를 수 있는 선택지까지 통째로 돌려준다.
        워크플로우 JSON은 결국 "노드 이름 + 입력값"일 뿐이라, 이 목록만 있으면 업로드된
        워크플로우가 이 서버에서 돌아갈 수 있는지 미리 검사할 수 있다."""
        health = ComfyUIDriver.health(pod)
        url = health["url"]
        if not health["ok"] or not url:
            return url, None

        pod_id = pod.get("id") or url
        cached = _object_info_cache.get(pod_id)
        now = time.time()
        if (
            not force
            and cached
            and cached.get("data") is not None
            and cached.get("url") == url
            and now - cached.get("fetched_at", 0.0) < OBJECT_INFO_TTL_SEC
        ):
            return url, cached["data"]

        data = _fetch_json(url, "/object_info", timeout=30)
        _object_info_cache[pod_id] = {"url": url, "data": data, "fetched_at": now}
        return url, data

    @staticmethod
    def invalidate_capabilities(pod_id: str | None = None):
        """주소가 바뀌었을 때 — 이전 서버에서 받아둔 목록이 이 파드의 것이 아니게 된다.
        pod_id를 주지 않으면 전부 비운다."""
        if pod_id is None:
            _object_info_cache.clear()
        else:
            _object_info_cache.pop(pod_id, None)

    @staticmethod
    def job_env(pod: dict, url: str) -> dict:
        return {"COMFY_URL": url}

    @staticmethod
    def collect(pod: dict, url: str, job_id: str) -> dict | None:
        """작업이 끝난 뒤 그 작업(job_id 하위 폴더)의 결과 이미지만 끌어온다.
        설정이 꺼져 있으면 아무것도 안 한다(comfy_outputs.py 참고)."""
        if not pod.get("pull_outputs"):
            return None
        return sync_outputs(url, only_subfolder=job_id)

    @staticmethod
    def card(pod: dict) -> dict:
        """대시보드 카드에 실을 ComfyUI 고유 정보 — GPU/VRAM(살아 있을 때만)과, url이
        RunPod 프록시 주소면 RunPod API로 보충한 메타데이터(파드가 꺼져 있어도 조회
        가능 — health와 무관하게 pod.url에서 바로 pod id를 뽑는다). 못 읽어도 카드
        자체는 떠야 하므로 각 조회의 실패는 조용히 빈 값으로 돌려준다."""
        card: dict = {}
        health = ComfyUIDriver.health(pod)
        if health["ok"] and health["url"]:
            try:
                stats = _fetch_json(health["url"], "/system_stats", timeout=CHECK_TIMEOUT)
                devices = stats.get("devices") or []
                if isinstance(devices, list) and devices:
                    dev = devices[0] if isinstance(devices[0], dict) else {}
                    card.update({
                        "device": dev.get("name"),
                        "vram_total": dev.get("vram_total"),
                        "vram_free": dev.get("vram_free"),
                    })
            except Exception:
                pass

        runpod_info = get_runpod_info(pod.get("url") or health.get("url") or "")
        if runpod_info:
            card["runpod"] = runpod_info
        return card


__all__ = ["ComfyUIDriver", "OutputSyncError", "check_url",
           "CANDIDATE_URLS", "CHECK_TIMEOUT", "CHECK_TIMEOUT_LOCAL",
           "CHECK_TIMEOUT_INTERACTIVE", "COMFY_USER_AGENT"]
