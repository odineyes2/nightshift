"""대기열 자동 켜기 설정·기록 — 대기 작업이 모이면 이미지·영상 워커의 RunPod 파드를 자동으로 켠다(켜진 뒤 정지는 자동 끄기 NS-8).
등급(tier)마다 따로: 이미지는 data/auto_power_on.json·auto_power_on_state.json, 영상은 auto_power_on_video*.json.
영상 설정에는 "영상 파드 사용"(video_jobs — 서비스 연동 영상 생성을 받을지, 기본 꺼짐)이 더 있다. 모두 원자적으로 쓴다."""
import json
import os
import tempfile
import threading

from data_paths import DATA_DIR

TIERS = ("image", "video")
FILE = DATA_DIR / "auto_power_on.json"
STATE_FILE = DATA_DIR / "auto_power_on_state.json"
VIDEO_FILE = DATA_DIR / "auto_power_on_video.json"
VIDEO_STATE_FILE = DATA_DIR / "auto_power_on_video_state.json"
# paid_immediate: 유료 회원 작업(관리자 파드로 정해진 것)이 하나라도 기다리면 enabled·min_jobs와 상관없이 바로 켠다(2026-11-15).
DEFAULTS = {"enabled": False, "min_jobs": 10, "max_wait_hours": 24, "daily_limit": 3, "pod_id": None, "paid_immediate": True}
VIDEO_DEFAULTS = {**DEFAULTS, "min_jobs": 3, "video_jobs": False}
LIMITS = {"min_jobs": (1, 200), "max_wait_hours": (0, 168), "daily_limit": (1, 24)}   # max_wait_hours 0 = 개수만 본다
BACKOFF_MINUTES = (10, 30, 60)   # 켜기 실패 뒤 다시 시도할 때까지(연속 실패마다 다음 값, 끝값 유지)
_lock = threading.Lock()


def _files(tier: str):
    if tier not in TIERS:
        raise ValueError("알 수 없는 워커 등급이에요.")
    return (FILE, STATE_FILE, DEFAULTS) if tier == "image" else (VIDEO_FILE, VIDEO_STATE_FILE, VIDEO_DEFAULTS)


def validate(value: object, tier: str = "image") -> dict:
    defaults = _files(tier)[2]
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise ValueError("자동 켜기 설정을 확인해 주세요.")
    result = dict(defaults)
    for key, label in (("enabled", "자동 켜기 사용 여부"), ("video_jobs", "영상 파드 사용 여부"), ("paid_immediate", "유료 작업 바로 켜기")):
        if key in value and key in defaults:
            if not isinstance(value[key], bool):
                raise ValueError(f"{label}를 확인해 주세요.")
            result[key] = value[key]
    names = {"min_jobs": "자동으로 켜는 대기 작업 개수", "max_wait_hours": "최대 대기 시간", "daily_limit": "하루 자동 켜기 횟수"}
    for key, (low, high) in LIMITS.items():
        if key in value:
            number = value[key]
            if isinstance(number, bool) or not isinstance(number, int) or not low <= number <= high:
                raise ValueError(f"{names[key]}는 {low}~{high} 사이로 입력해 주세요.")
            result[key] = number
    if value.get("pod_id") is not None:
        if not isinstance(value["pod_id"], str) or not 1 <= len(value["pod_id"]) <= 64:
            raise ValueError("켤 워커를 확인해 주세요.")
        result["pod_id"] = value["pod_id"]
    return result


def _read(path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return fallback


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.stem}-",
                                         suffix=".tmp", delete=False) as f:
            name = f.name
            json.dump(value, f, ensure_ascii=False)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def load(tier: str = "image") -> dict:
    path, _, defaults = _files(tier)
    try:
        return validate(_read(path, {}), tier)
    except ValueError:
        return dict(defaults)


def save(value: object, tier: str = "image") -> dict:
    settings = validate(value, tier)
    with _lock:
        _write(_files(tier)[0], settings)
    return settings


def load_state(tier: str = "image") -> dict:
    state = _read(_files(tier)[1], {})
    if not isinstance(state, dict):
        state = {}
    starts = state.get("starts")
    return {"starts": [s for s in starts if isinstance(s, str)] if isinstance(starts, list) else [],
            "failures": state.get("failures") if isinstance(state.get("failures"), int) else 0,
            "retry_after": state.get("retry_after") if isinstance(state.get("retry_after"), str) else None,
            "last_error": state.get("last_error") if isinstance(state.get("last_error"), str) else None,
            "last_reason": state.get("last_reason") if isinstance(state.get("last_reason"), str) else None,
            "last_pod_id": state.get("last_pod_id") if isinstance(state.get("last_pod_id"), str) else None}


def save_state(state: dict, tier: str = "image") -> None:
    with _lock:
        state = {**load_state(tier), **state}
        state["starts"] = state["starts"][-50:]
        _write(_files(tier)[1], state)
