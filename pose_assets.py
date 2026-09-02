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
실제로 포즈 이미지를 고르고 ComfyUI에 주입하는 로직은 templates/pose_batch.py,
templates/pose_csv_batch.py에 독립적으로 구현돼 있다(템플릿 스크립트는 서로 임포트하지
않고 복사해서 쓸 수 있게 자기 완결적으로 작성하는 게 이 저장소의 컨벤션이라, 같은
NIGHTSHIFT_POSES_DIR 환경변수를 템플릿도 스스로 읽어서 같은 결론에 도달하게 했다).

pose_csv_batch.py의 CSV "pose" 컬럼처럼, 세트 이름이 아니라 "포즈 이미지 하나를
지정하는 자유 형식 문자열"을 해석해야 하는 경우를 위해 resolve_pose_reference()도
여기 둔다. 이 함수는 app.py(업로드 시점 검증)와 templates/pose_csv_batch.py(실행
시점 실제 해석) 양쪽에서 "같은 알고리즘"이어야 하는 단일 소스다 — 후자는 템플릿
자기완결성 컨벤션에 따라 본문을 그대로 복사해서 쓰므로, 이 함수를 고치면
pose_csv_batch.py의 사본도 반드시 같이 고쳐야 한다.

환경변수:
    NIGHTSHIFT_POSES_DIR  포즈 세트 폴더들이 있는 상위 디렉토리 (기본 /workspace/dataset/poses)
"""

import os
from dataclasses import dataclass
from pathlib import Path

POSES_DIR = os.environ.get("NIGHTSHIFT_POSES_DIR", "/workspace/dataset/poses")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


class PoseAssetError(Exception):
    """포즈 세트를 찾을 수 없거나 쓸 수 없는 상태(빈 폴더 등)일 때, 사용자가 읽을 메시지와 함께 전달."""


class PoseReferenceError(Exception):
    """resolve_pose_reference()가 값 하나를 해석하지 못했을 때의 공통 베이스."""


class PoseReferenceNotFoundError(PoseReferenceError):
    """세트/파일 어느 쪽으로도 찾지 못했거나, 형식이 잘못됐을 때."""


class PoseReferenceAmbiguousError(PoseReferenceError):
    """파일명만으로 지정했는데 같은 이름의 파일이 세트 여러 개에 동시에 있을 때."""


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


@dataclass
class PoseSetRef:
    """값이 세트 이름과 정확히 일치 — "이 세트 안에서 골라라"는 뜻이라 아직 특정
    파일 하나로 정해진 게 아니다. 실제로 어떤 파일을 쓸지는 호출부(피커)가 정한다."""
    set_name: str


@dataclass
class PoseFileRef:
    """값이 파일 하나를 정확히 가리킴 — "<세트>/<파일명>" 조합으로 지정했거나,
    파일명만으로 지정했는데 전체 세트를 통틀어 그 이름의 파일이 하나뿐이었던 경우."""
    set_name: str
    path: Path


def resolve_pose_reference(name: str):
    """CSV의 pose 컬럼 같은, "포즈 이미지 하나를 자유 형식으로 지정하는 문자열"을
    해석한다. 우선순위:

    1. "/"가 있으면 "<세트>/<파일명>" 형식의 정확한 경로로 취급한다.
    2. "/"가 없고 POSES_DIR 아래 세트 폴더 이름과 정확히 일치하면 "세트 지정"으로
       취급한다 (PoseSetRef 반환 — 그 세트 안에서 어떤 파일을 쓸지는 호출부의
       피커가 정한다).
    3. "/"가 없고 세트 이름과는 안 맞지만 어느 한 세트 안에 그 파일명이 있으면
       "파일명 단독 지정"으로 취급한다(전체 세트를 통틀어 검색). 같은 파일명이
       둘 이상의 세트에 동시에 있으면 PoseReferenceAmbiguousError.
    4. 위 어디에도 안 맞으면 PoseReferenceNotFoundError.

    빈 값은 호출부가 미리 걸러야 한다 — "포즈 없음(ControlNet 비활성화)"은 이
    함수가 아니라 호출부의 관심사다.
    """
    name = (name or "").strip()
    if not name:
        raise PoseReferenceNotFoundError("포즈 참조 값이 비어 있어요.")

    if "/" in name:
        set_name, _, filename = name.partition("/")
        set_name = set_name.strip()
        filename = filename.strip()
        if not set_name or not filename:
            raise PoseReferenceNotFoundError(
                f"'{name}' 형식이 올바르지 않아요 (<세트>/<파일명> 형식이어야 해요)."
            )
        path = _pose_set_dir(set_name) / filename
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            raise PoseReferenceNotFoundError(f"'{name}' 파일을 찾을 수 없어요.")
        return PoseFileRef(set_name=set_name, path=path)

    if _pose_set_dir(name).is_dir():
        if not list_pose_images(name):
            raise PoseReferenceNotFoundError(f"'{name}' 포즈 세트에 이미지가 하나도 없어요.")
        return PoseSetRef(set_name=name)

    matches = []
    base = Path(POSES_DIR)
    if base.is_dir():
        for entry in sorted(base.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            candidate = entry / name
            if candidate.is_file() and candidate.suffix.lower() in IMAGE_EXTENSIONS:
                matches.append(entry.name)

    if len(matches) == 1:
        return PoseFileRef(set_name=matches[0], path=_pose_set_dir(matches[0]) / name)
    if len(matches) > 1:
        raise PoseReferenceAmbiguousError(
            f"'{name}'가 여러 세트({', '.join(matches)})에 있어요. "
            f"'<세트>/{name}' 형식으로 명시해주세요."
        )
    raise PoseReferenceNotFoundError(f"'{name}'를 세트 이름으로도 파일명으로도 찾지 못했어요.")
