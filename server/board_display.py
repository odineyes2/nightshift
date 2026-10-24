"""무드보드 화살표 머리·입구 점 크기 설정을 검증하고 원자적으로 저장한다."""
import json
import os
import tempfile

from data_paths import DATA_DIR


FILE = DATA_DIR / "board_display.json"
DEFAULT = {"arrow_size": "medium", "port_size": "medium"}
SIZES = {"small", "medium", "large"}


def validate(value: object) -> dict:
    """두 크기 단계가 모두 지정됐는지 확인한다."""
    if (not isinstance(value, dict) or set(value) != set(DEFAULT)
            or any(not isinstance(value[key], str) or value[key] not in SIZES for key in DEFAULT)):
        raise ValueError("화살표 끝과 입구 점 크기를 작게·보통·크게 중에서 골라 주세요.")
    return {key: value[key] for key in DEFAULT}


def load() -> dict:
    try:
        return validate(json.loads(FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return DEFAULT.copy()


def save(value: object) -> dict:
    settings = validate(value)
    FILE.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=FILE.parent,
                                         prefix=".board-display-", suffix=".tmp", delete=False) as f:
            name = f.name
            json.dump(settings, f, ensure_ascii=False)
        os.replace(name, FILE)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)
    return settings
