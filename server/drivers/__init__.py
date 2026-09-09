"""
파드 드라이버 모음 — 파드 레코드의 kind로 "이 워커를 어떻게 다루는가"를 고른다.

지금은 comfyui(이미지 생성)와 shell(임의 명령 실행) 둘이다. 성격이 완전히 다른 이 둘이
같은 레지스트리·같은 큐·같은 대시보드에서 나란히 도는 것이, 앞으로 글 쓰는 워커나 지도
그리는 워커를 붙일 수 있다는 증거다. 새 종류를 붙일 때는 여기 모듈을 하나 더 만들고
DRIVERS에 등록하면 되고, 나머지(레지스트리·큐·화면)는 그대로 쓴다. 자세한 배경은
base.py와 multipod_plan.md 참고.
"""

from .base import DriverError, PodDriver
from .comfyui import ComfyUIDriver
from .shell import ShellDriver

DRIVERS: dict[str, type[PodDriver]] = {
    ComfyUIDriver.kind: ComfyUIDriver,
    ShellDriver.kind: ShellDriver,
}


def get_driver(kind: str) -> type[PodDriver]:
    driver = DRIVERS.get((kind or "").strip())
    if driver is None:
        raise DriverError(f"모르는 파드 종류예요: {kind!r}")
    return driver


def driver_for(pod: dict) -> type[PodDriver]:
    return get_driver(pod.get("kind"))


def driver_kinds() -> list[dict]:
    """화면의 "파드 종류" 선택지. 지금 등록할 수 없는 종류(셸 파드가 꺼져 있는 경우 등)는
    빼고 돌려준다 — 고를 수 있게 해놓고 저장할 때 거절하는 것보다 낫다."""
    return [{"kind": d.kind, "label": d.label} for d in DRIVERS.values() if d.available()]


__all__ = ["DRIVERS", "DriverError", "PodDriver", "ComfyUIDriver", "ShellDriver",
           "get_driver", "driver_for", "driver_kinds"]
