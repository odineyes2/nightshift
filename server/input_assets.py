"""
img2img/USDU(Ultimate SD Upscale)용 "입력 이미지" 저장소 — ref_assets.py의
pose/depth/lineart와 달리 char_no/세트 계층이 없는 평평한(flat) 파일 목록이다.

ControlNet 참조 이미지는 "인물 수 x 포즈 종류"처럼 미리 분류해서 대량으로 쌓아두고
템플릿이 그중 하나를 순차/랜덤으로 뽑아 쓰는 용도라 계층 구조가 의미 있지만, img2img/
USDU의 입력 이미지는 보통 "이 그림 한 장을 원본으로 변형/업스케일한다"처럼 사용자가
그때그때 특정 파일 하나를 지목해서 쓰는 용도라 세트 개념이 필요 없다 — 그래서 별도
모듈로 둔다(ref_assets.py를 억지로 확장하지 않음).

디렉토리 구조 (계층 없음):
    NIGHTSHIFT_INPUT_IMAGES_DIR(기본 NIGHTSHIFT_ASSETS_DIR/input, 그 기본값은
    /workspace/dataset/assets/input)/
        image1.png
        image2.jpg
        ...

이 모듈은 목록 조회(GET /api/input-images)와 업로드 시점 검증에 쓰인다. 실제로
파일을 골라 ComfyUI에 주입하는 로직은 templates/input_image_batch.py 등에
독립적으로 구현돼 있다(템플릿 자기완결성 컨벤션 — ref_assets.py 모듈 설명 참고).

이미지를 이 폴더에 넣는 방법은 두 가지다: (1) POST /api/input-images로 브라우저에서
파일을 직접 올리거나(save_input_image), (2) POST /api/input-images/import-from-output로
결과 이미지 갤러리에서 마음에 든 이미지를 사본으로 가져온다(ref_assets.py의
"참조 세트로 보내기"와 같은 패턴 — 원본은 지우지 않음). 둘 다 영상 생성(WAN2.2
i2v/flf2v) 작업의 "이미지 선택" 단계가 쓴다.

환경변수:
    NIGHTSHIFT_INPUT_IMAGES_DIR  입력 이미지들이 있는 폴더 (기본
                                  NIGHTSHIFT_ASSETS_DIR/input)
    NIGHTSHIFT_ASSETS_DIR        위 override가 없을 때 쓰는 상위 디렉토리
                                  (기본 /workspace/dataset/assets)
"""

import os
from pathlib import Path

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


class InputAssetError(Exception):
    """입력 이미지 이름이 올바르지 않거나 찾을 수 없을 때."""


def input_images_dir() -> Path:
    override = os.environ.get("NIGHTSHIFT_INPUT_IMAGES_DIR")
    if override:
        return Path(override)
    assets_dir = os.environ.get("NIGHTSHIFT_ASSETS_DIR", "/workspace/dataset/assets")
    return Path(assets_dir) / "input"


def list_input_images() -> list[dict]:
    """[{"name", "size"}, ...] (파일명 정렬). 폴더가 없으면 빈 리스트(에러 아님 —
    아직 이미지를 안 올려둔 상태일 수 있음)."""
    d = input_images_dir()
    if not d.is_dir():
        return []
    return [
        {"name": p.name, "size": p.stat().st_size}
        for p in sorted(d.iterdir())
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    ]


def resolve_input_image(name: str) -> Path:
    """이름 하나를 실제 파일 경로로 해석한다 — 존재하지 않거나 하위 디렉토리를
    벗어나려는 이름(예: "../etc/passwd")이면 InputAssetError."""
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise InputAssetError(f"올바르지 않은 입력 이미지 이름이에요: '{name}'")
    path = input_images_dir() / name
    if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise InputAssetError(f"입력 이미지 '{name}'를 찾을 수 없어요.")
    return path


def save_input_image(filename: str, content: bytes) -> str:
    """업로드된 이미지를 입력 이미지 폴더에 저장한다. ref_assets.save_ref_image와
    같은 규칙 — Path(filename).name만 써서 경로 조작을 막고, 이미 같은 이름의
    파일이 있으면 지우지 않고 파일명 끝에 _2, _3...을 붙여 저장한다. 실제로
    저장한 파일명을 돌려준다."""
    safe_name = Path(filename or "").name
    stem, suffix = Path(safe_name).stem, Path(safe_name).suffix
    if not stem or suffix.lower() not in IMAGE_EXTENSIONS:
        raise InputAssetError(f"올바르지 않은 이미지 파일이에요: '{filename}' (png/jpg/jpeg/webp만 가능).")

    d = input_images_dir()
    d.mkdir(parents=True, exist_ok=True)
    candidate = safe_name
    n = 2
    while (d / candidate).exists():
        candidate = f"{stem}_{n}{suffix}"
        n += 1
    (d / candidate).write_bytes(content)
    return candidate


def delete_input_image(name: str) -> None:
    resolve_input_image(name).unlink()
