"""
파드 드라이버 모음 — 파드 레코드의 kind로 "이 워커를 어떻게 다루는가"를 고른다.

지금은 comfyui 하나뿐이다. 이질적인 워커(글 쓰기, 지도 그리기, 임의 스크립트 실행 등)를
붙일 때는 여기 모듈을 하나 더 만들고 DRIVERS에 등록하면 되고, 나머지(레지스트리·큐·화면)는
그대로 쓴다. 자세한 배경은 base.py와 multipod_plan.md 참고.
"""

from .base import DriverError, PodDriver
from .comfyui import ComfyUIDriver

DRIVERS: dict[str, type[PodDriver]] = {
    ComfyUIDriver.kind: ComfyUIDriver,
}


def get_driver(kind: str) -> type[PodDriver]:
    driver = DRIVERS.get((kind or "").strip())
    if driver is None:
        raise DriverError(f"모르는 파드 종류예요: {kind!r}")
    return driver


def driver_for(pod: dict) -> type[PodDriver]:
    return get_driver(pod.get("kind"))


def driver_kinds() -> list[dict]:
    """화면의 "파드 종류" 선택지."""
    return [{"kind": d.kind, "label": d.label} for d in DRIVERS.values()]


__all__ = ["DRIVERS", "DriverError", "PodDriver", "ComfyUIDriver",
           "get_driver", "driver_for", "driver_kinds"]
