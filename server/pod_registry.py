"""
파드(워커) 레지스트리 — nightshift가 관리하는 워커들의 목록을 pods.json에 담는다.

## 파드란

파드는 "ComfyUI 한 대"가 아니라 **드라이버가 붙은 엔드포인트**다. 레코드에 들어 있는
kind가 어떤 드라이버로 이 파드를 다룰지를 정하고(drivers/ 참고), 드라이버가 "살아 있나 /
뭘 할 수 있나 / 작업을 어떻게 돌리나 / 결과물을 어떻게 가져오나"를 안다. 지금은 comfyui
드라이버 하나뿐이지만, 앞으로 이미지가 아닌 다른 일을 하는 워커가 같은 목록에 나란히
등록된다(multipod_plan.md 참고).

## url이 비어 있다는 뜻

comfyui 파드의 url이 비어 있으면 "COMFY_URL 환경변수 → 127.0.0.1 자동 탐지" 순으로
찾으라는 뜻이다. A-1에서 정한 우선순위(화면 설정 > 환경변수 > 자동 탐지)를 그대로
유지하기 위한 것으로, 파드 레코드의 url이 그 "화면 설정" 자리를 차지한다.

## 기본 파드

파드가 여러 개여도 "어느 파드인지 지정되지 않은 일"은 존재한다(Prompt Enhance, 헤더의
연결 상태 배지 등). 그런 경우 default_pod()가 쓰인다 — enabled인 첫 파드, 없으면 그냥
첫 파드다. 파드가 하나뿐이면 항상 그 파드이므로, 다중 파드 이전과 동작이 같다.

## 저장 형식 (pods.json)

    {"pods": [{id, name, kind, url, enabled, tags, max_concurrent, pull_outputs,
               note, created_at, updated_at}, ...]}

환경변수:
    NIGHTSHIFT_PODS_FILE  레지스트리 파일 경로 (기본 <저장소>/data/pods.json)
"""

import json
import os
import threading
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

from data_paths import data_path

PODS_FILE = Path(os.environ.get("NIGHTSHIFT_PODS_FILE") or data_path("pods.json"))
# 이 파일이 있고 pods.json이 없으면, 그 설정을 파드 1개짜리 레지스트리로 옮겨 담는다.
LEGACY_ENDPOINT_FILE = data_path("comfy_endpoint.json")

DEFAULT_KIND = "comfyui"
DEFAULT_POD_NAME = "기본 파드"

_lock = threading.RLock()
_pods: list[dict] = []


class PodError(Exception):
    """파드 레코드가 유효하지 않을 때 (호출부가 400으로 바꿔 쓴다)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_pod_url(raw: str, kind: str = DEFAULT_KIND) -> str:
    """주소를 저장 형태로 다듬는다. **무엇이 올바른 주소인지는 워커 종류마다 다르므로**
    (ComfyUI는 http 엔드포인트, 셸 파드는 ssh 대상) 판단은 드라이버에 맡긴다.
    빈 값은 "기본값에 맡긴다"는 뜻이라 어느 종류든 통과시킨다. 형식이 틀리면 PodError."""
    from drivers import DriverError, get_driver   # 순환 import를 피해 여기서 가져온다
    try:
        driver = get_driver(kind)
    except DriverError as e:
        raise PodError(str(e))
    try:
        return driver.normalize_url(raw)
    except ValueError as e:
        raise PodError(str(e))


def normalize_pod(raw: dict) -> dict:
    """사용자/파일에서 온 dict를 완전한 파드 레코드로 만든다. 빠진 값은 기본값으로
    채우므로, 저장 형식이 나중에 늘어나도 옛 pods.json을 그대로 읽을 수 있다."""
    if not isinstance(raw, dict):
        raise PodError("파드는 JSON 객체여야 해요.")

    name = str(raw.get("name") or "").strip()
    if not name:
        raise PodError("파드 이름을 지어주세요.")

    kind = str(raw.get("kind") or DEFAULT_KIND).strip() or DEFAULT_KIND

    tags = raw.get("tags") or []
    if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
        raise PodError("tags는 문자열 배열이어야 해요.")

    # `or 1`로 기본값을 채우면 0이 조용히 1로 바뀐다 — 안 넣은 것과 0을 구분한다.
    raw_max = raw.get("max_concurrent")
    if raw_max is None or raw_max == "":
        raw_max = 1
    try:
        max_concurrent = int(raw_max)
    except (TypeError, ValueError):
        raise PodError("max_concurrent는 정수여야 해요.")
    if max_concurrent < 1:
        raise PodError("max_concurrent는 1 이상이어야 해요.")

    return {
        "id": str(raw.get("id") or "").strip() or str(uuid.uuid4())[:8],
        "name": name,
        "kind": kind,
        "url": normalize_pod_url(raw.get("url", ""), kind),
        "enabled": bool(raw.get("enabled", True)),
        "tags": [t.strip() for t in tags if t.strip()],
        "max_concurrent": max_concurrent,
        "pull_outputs": bool(raw.get("pull_outputs")),
        "note": str(raw.get("note") or ""),
        "created_at": raw.get("created_at") or _now_iso(),
        "updated_at": raw.get("updated_at") or _now_iso(),
    }


def _migrated_from_legacy() -> list[dict] | None:
    """comfy_endpoint.json(단일 접속 주소 설정)이 남아 있으면 파드 1개로 옮겨 담는다.
    사용자가 화면에서 저장해 둔 pod 주소를 손으로 다시 적게 만들지 않기 위한 것이다."""
    if not LEGACY_ENDPOINT_FILE.exists():
        return None
    try:
        with open(LEGACY_ENDPOINT_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return [normalize_pod({
        "name": DEFAULT_POD_NAME,
        "kind": DEFAULT_KIND,
        "url": data.get("url") or "",
        "pull_outputs": data.get("pull_outputs"),
        "updated_at": data.get("updated_at"),
    })]


def load():
    """pods.json을 읽어 메모리에 올린다. 파일이 없으면 (a) 옛 comfy_endpoint.json을
    옮겨 담거나 (b) 그것도 없으면 url이 빈 기본 파드 하나를 만든다 — 어느 쪽이든
    "파드가 0개인 상태"는 만들지 않는다. 파드가 없으면 화면이 아무것도 못 하기 때문."""
    with _lock:
        _pods.clear()
        loaded = None
        if PODS_FILE.exists():
            try:
                with open(PODS_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                raw_pods = data.get("pods") if isinstance(data, dict) else None
                if isinstance(raw_pods, list):
                    loaded = []
                    for raw in raw_pods:
                        try:
                            loaded.append(normalize_pod(raw))
                        except PodError:
                            # 한 줄이 깨졌다고 전체를 못 읽으면 곤란하다 — 그 파드만 버린다.
                            continue
            except (OSError, json.JSONDecodeError):
                loaded = None

        if not loaded:
            loaded = _migrated_from_legacy() or [normalize_pod({
                "name": DEFAULT_POD_NAME,
                "kind": DEFAULT_KIND,
                "url": "",
            })]
            _pods.extend(loaded)
            save()
            return
        _pods.extend(loaded)


def save():
    with _lock:
        tmp = PODS_FILE.with_suffix(PODS_FILE.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"pods": _pods}, f, indent=2, ensure_ascii=False)
        os.replace(tmp, PODS_FILE)


def list_pods() -> list[dict]:
    with _lock:
        return [dict(p) for p in _pods]


def get_pod(pod_id: str) -> dict | None:
    with _lock:
        for p in _pods:
            if p["id"] == pod_id:
                return dict(p)
    return None


def require_pod(pod_id: str) -> dict:
    pod = get_pod(pod_id)
    if pod is None:
        raise PodError("없는 파드예요.")
    return pod


def default_pod() -> dict:
    """어느 파드인지 지정되지 않은 일에 쓸 파드. enabled인 첫 파드, 없으면 첫 파드.
    load()가 항상 최소 1개를 보장하므로 None을 돌려주지 않는다."""
    with _lock:
        for p in _pods:
            if p.get("enabled"):
                return dict(p)
        return dict(_pods[0])


def create_pod(raw: dict) -> dict:
    pod = normalize_pod({**raw, "id": ""})  # id는 항상 새로 발급한다
    with _lock:
        _pods.append(pod)
        save()
    return dict(pod)


def update_pod(pod_id: str, raw: dict) -> dict:
    """주어진 필드만 덮어쓴다(부분 수정) — 주소만 바꾸려는 요청이 조용히 다른 설정을
    기본값으로 되돌리면 안 되므로."""
    with _lock:
        for i, p in enumerate(_pods):
            if p["id"] != pod_id:
                continue
            merged = {**p, **{k: v for k, v in raw.items() if k not in ("id", "created_at")}}
            merged["id"] = p["id"]
            merged["created_at"] = p["created_at"]
            merged["updated_at"] = _now_iso()
            _pods[i] = normalize_pod(merged)
            save()
            return dict(_pods[i])
    raise PodError("없는 파드예요.")


def delete_pod(pod_id: str) -> dict:
    """파드를 지운다. 마지막 하나는 지울 수 없다 — 파드가 0개면 작업을 아무 데도
    보낼 수 없어서 화면이 통째로 무의미해진다(쓰지 않을 파드는 enabled=false로 둔다)."""
    with _lock:
        if len(_pods) <= 1:
            raise PodError("마지막 파드는 지울 수 없어요. 쓰지 않으려면 '사용 안 함'으로 바꿔주세요.")
        for i, p in enumerate(_pods):
            if p["id"] == pod_id:
                removed = _pods.pop(i)
                save()
                return dict(removed)
    raise PodError("없는 파드예요.")
