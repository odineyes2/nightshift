"""대기열 자동 켜기 설정·기록 — 대기 작업이 모이면 이미지 워커의 RunPod 파드를 자동으로 켠다(켜진 뒤 정지는 자동 끄기 NS-8).
설정은 data/auto_power_on.json, 오늘 켠 횟수·실패 뒤 다시 시도할 시각 같은 기록은 data/auto_power_on_state.json에 원자적으로 쓴다."""
import json
import os
import tempfile
import threading

from data_paths import DATA_DIR

FILE = DATA_DIR / "auto_power_on.json"
STATE_FILE = DATA_DIR / "auto_power_on_state.json"
DEFAULTS = {"enabled": False, "min_jobs": 10, "max_wait_hours": 24, "daily_limit": 3, "pod_id": None}
LIMITS = {"min_jobs": (1, 200), "max_wait_hours": (0, 168), "daily_limit": (1, 24)}   # max_wait_hours 0 = 개수만 본다
BACKOFF_MINUTES = (10, 30, 60)   # 켜기 실패 뒤 다시 시도할 때까지(연속 실패마다 다음 값, 끝값 유지)
_lock = threading.Lock()


def validate(value: object) -> dict:
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("자동 켜기 설정을 확인해 주세요.")
    result = dict(DEFAULTS)
    if "enabled" in value:
        if not isinstance(value["enabled"], bool):
            raise ValueError("자동 켜기 사용 여부를 확인해 주세요.")
        result["enabled"] = value["enabled"]
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


def load() -> dict:
    try:
        return validate(_read(FILE, {}))
    except ValueError:
        return dict(DEFAULTS)


def save(value: object) -> dict:
    settings = validate(value)
    with _lock:
        _write(FILE, settings)
    return settings


def load_state() -> dict:
    state = _read(STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}
    starts = state.get("starts")
    return {"starts": [s for s in starts if isinstance(s, str)] if isinstance(starts, list) else [],
            "failures": state.get("failures") if isinstance(state.get("failures"), int) else 0,
            "retry_after": state.get("retry_after") if isinstance(state.get("retry_after"), str) else None,
            "last_error": state.get("last_error") if isinstance(state.get("last_error"), str) else None,
            "last_reason": state.get("last_reason") if isinstance(state.get("last_reason"), str) else None,
            "last_pod_id": state.get("last_pod_id") if isinstance(state.get("last_pod_id"), str) else None}


def save_state(state: dict) -> None:
    with _lock:
        state = {**load_state(), **state}
        state["starts"] = state["starts"][-50:]
        _write(STATE_FILE, state)
