"""RunPod 패밀리별 GPU·클라우드 선택을 검증하고 데이터 폴더에 원자적으로 저장한다."""
import json
import os
import tempfile

from data_paths import DATA_DIR

FILE = DATA_DIR / "runpod_gpu_settings.json"
CLOUDS = ("SECURE", "COMMUNITY")


def validate(value: object) -> dict:
    """설정 형식과 순서를 확인한다. 일부 패밀리는 기본값을 그대로 쓸 수 있다."""
    from runpod_api import RUNPOD_TIERS

    if not isinstance(value, dict) or not value or set(value) - set(RUNPOD_TIERS):
        raise ValueError("이미지용·영상용 설정을 확인해 주세요.")
    result = {}
    for tier, spec in value.items():
        if not isinstance(spec, dict) or set(spec) != {"gpus", "clouds"}:
            raise ValueError(f"{tier} 설정에는 gpus와 clouds가 필요해요.")
        gpus, clouds = spec["gpus"], spec["clouds"]
        if (not isinstance(gpus, list) or not 1 <= len(gpus) <= 100
                or any(not isinstance(g, str) or not g.strip() or len(g) > 160 or g != g.strip() for g in gpus)
                or len(set(gpus)) != len(gpus)):
            raise ValueError(f"{tier} GPU 목록을 확인해 주세요.")
        if (not isinstance(clouds, list) or not 1 <= len(clouds) <= len(CLOUDS)
                or any(c not in CLOUDS for c in clouds) or len(set(clouds)) != len(clouds)):
            raise ValueError(f"{tier} 클라우드 목록을 확인해 주세요.")
        result[tier] = {"gpus": list(gpus), "clouds": list(clouds)}
    return result


def load() -> dict:
    try:
        return validate(json.loads(FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return {}


def save(value: object) -> dict:
    settings = validate(value)
    FILE.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=FILE.parent,
                                         prefix=".runpod-gpus-", suffix=".tmp", delete=False) as f:
            name = f.name
            json.dump(settings, f, ensure_ascii=False)
        os.replace(name, FILE)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)
    return settings


def effective(tier: str) -> dict:
    from runpod_api import RUNPOD_TIERS

    saved = load().get(tier)
    if saved:
        return {**saved, "configured": True}
    return {"gpus": list(RUNPOD_TIERS[tier]["gpus"]), "clouds": list(CLOUDS), "configured": False}
