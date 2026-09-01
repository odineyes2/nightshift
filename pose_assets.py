"""
포즈 레퍼런스 이미지 저장소 — ControlNet(OpenPose 등) LoadImage 노드에 주입할 참조
이미지를 미리 폴더에 쌓아두고, 템플릿이 그 중 하나를 순차/랜덤으로 골라 쓰게 한다.

디렉토리 구조:
    NIGHTSHIFT_POSES_DIR(기본 /workspace/dataset/poses)/
        <포즈 세트 이름>/
            image1.png
            image2.jpg
            ...
        <다른 포즈 세트>/
            ...

    포즈 세트별로 하위 폴더를 나눠두면(예: "casual_standing", "action_pose") 업로드
    폼의 드롭다운에서 세트를 선택할 수 있다. `/api/assets`가 이 하위 폴더 목록과
    각 폴더의 이미지 개수를 스캔해서 돌려준다.

이 모듈은 nightshift 서버(app.py) 쪽에서 폴더 목록 조회/업로드 시점 검증에만 쓰인다.
실제로 포즈 이미지를 고르고 ComfyUI에 주입하는 로직은 templates/pose_batch.py에
독립적으로 구현돼 있다(템플릿 스크립트는 서로 임포트하지 않고 복사해서 쓸 수 있게
자기 완결적으로 작성하는 게 이 저장소의 컨벤션이라, 같은 NIGHTSHIFT_POSES_DIR 환경변수를
템플릿도 스스로 읽어서 같은 결론에 도달하게 했다).

환경변수:
    NIGHTSHIFT_POSES_DIR  포즈 세트 폴더들이 있는 상위 디렉토리 (기본 /workspace/dataset/poses)
"""

import os
from pathlib import Path

POSES_DIR = os.environ.get("NIGHTSHIFT_POSES_DIR", "/workspace/dataset/poses")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


class PoseAssetError(Exception):
    """포즈 세트를 찾을 수 없거나 쓸 수 없는 상태(빈 폴더 등)일 때, 사용자가 읽을 메시지와 함께 전달."""


def _pose_set_dir(name: str) -> Path:
    return Path(POSES_DIR) / name


def list_pose_images(name: str) -> list[Path]:
    """포즈 세트 name 안의 이미지 파일 목록(정렬됨). 세트 폴더가 없으면 빈 리스트."""
    d = _pose_set_dir(name)
    if not d.is_dir():
        return []
    return sorted(
        p for p in d.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    )


def list_pose_sets() -> list[dict]:
    """POSES_DIR 바로 아래의 하위 폴더들을 스캔해서 [{"name", "count"}, ...]로 돌려준다.
    POSES_DIR 자체가 없으면 빈 리스트(에러 아님 — 아직 아무 세트도 안 올려둔 상태일 수 있음)."""
    base = Path(POSES_DIR)
    if not base.is_dir():
        return []
    sets = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        sets.append({"name": entry.name, "count": len(list_pose_images(entry.name))})
    return sets


def validate_pose_set(name: str) -> Path:
    """포즈 세트 name이 실제로 존재하고 이미지가 1장 이상 있는지 확인한다.
    잡을 큐에 올리는 시점(업로드 시)에 검사해서, 시작한 뒤에야 실패하는 일이 없게 한다."""
    name = (name or "").strip()
    if not name:
        raise PoseAssetError("포즈 세트를 선택하세요.")
    d = _pose_set_dir(name)
    if not d.is_dir():
        raise PoseAssetError(f"'{name}' 포즈 세트 폴더를 찾을 수 없어요.")
    if not list_pose_images(name):
        raise PoseAssetError(f"'{name}' 포즈 세트에 이미지가 하나도 없어요.")
    return d
