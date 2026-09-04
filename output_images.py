"""
서버에 쌓인 결과 이미지(NIGHTSHIFT_OUTPUT_DIR)에 대한 공용 작업 — 목록 조회, 전체 삭제,
가로형(hires-fix/USDU로 만들어진) 이미지 자동 회전. 이메일 발송(email_sender.py)과
zip 다운로드(app.py)도 이 모듈의 OUTPUT_DIR/list_output_images를 함께 쓴다.

템플릿 스크립트(templates/*.py의 apply_filename_prefix)가 작업마다 JOB_ID
이름의 하위 폴더에 결과 이미지를 나눠 저장하므로, list_output_images는
OUTPUT_DIR 바로 밑뿐 아니라 하위 폴더까지 재귀적으로 훑는다. app.py는 이
하위 폴더 이름을 "job_id"로 노출해 갤러리의 "작업별 보기"에 쓴다.

환경변수:
    NIGHTSHIFT_OUTPUT_DIR  이미지가 쌓이는 폴더 (기본 /workspace/output)
"""

import os
from pathlib import Path

from PIL import Image

OUTPUT_DIR = os.environ.get("NIGHTSHIFT_OUTPUT_DIR", "/workspace/output")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# base_v2_upscale.json 계열 워크플로우가 hires-fix 1단계에서 쓰는 가로형 기준 해상도.
# 여기서 나온 이미지 및 LatentUpscaleBy/USDU로 같은 비율(24:11)로 확대된 배수 해상도를
# "자동 회전 대상"으로 판단한다.
LANDSCAPE_BASE_SIZE = (1536, 704)
LANDSCAPE_RATIO_TOLERANCE = 0.02  # 업스케일 과정의 반올림 오차를 감안한 허용 오차


class OutputFolderError(Exception):
    """출력 폴더 자체를 찾을 수 없을 때."""


def list_output_images(search_dir: str | None = None) -> list[Path]:
    """search_dir(기본 OUTPUT_DIR)의 이미지 파일 목록. 템플릿 스크립트가 JOB_ID
    하위 폴더에 나눠 저장하므로(seed_batch.py 등의 apply_filename_prefix 참고)
    하위 폴더까지 재귀적으로 훑는다 — 폴더 구조가 한 단계든 여러 단계든, 혹은
    하위 폴더 없이 예전처럼 바로 밑에 있든 다 찾아낸다. 폴더 자체가 없으면 에러,
    이미지가 0개면 빈 리스트를 돌려준다 — 삭제/회전 입장에서는 할 일이 없을 뿐 에러가 아니다."""
    search_dir = search_dir or OUTPUT_DIR
    d = Path(search_dir)
    if not d.exists():
        raise OutputFolderError(f"폴더를 찾을 수 없습니다: {search_dir}")
    return [
        p for p in sorted(d.rglob("*"))
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    ]


def delete_output_images(search_dir: str | None = None) -> int:
    """search_dir의 이미지 파일을 모두 지우고 지운 개수를 반환한다."""
    files = list_output_images(search_dir)
    for f in files:
        f.unlink()
    return len(files)


def is_landscape_hiresfix_size(width: int, height: int) -> bool:
    if width <= height:
        return False
    target_ratio = LANDSCAPE_BASE_SIZE[0] / LANDSCAPE_BASE_SIZE[1]
    ratio = width / height
    return abs(ratio - target_ratio) / target_ratio <= LANDSCAPE_RATIO_TOLERANCE


def rotate_landscape_images(search_dir: str | None = None) -> dict:
    """LANDSCAPE_BASE_SIZE와 같은 비율(24:11)의 가로형 이미지를 시계 방향으로 90도
    회전해서 같은 파일에 덮어쓴다 (아래쪽 변이 왼쪽으로 오도록 — PIL의 ROTATE_270이
    시계 방향 90도 회전에 해당함). 이미 회전된(세로형) 이미지는 비율이 안 맞아 자동으로
    건너뛰므로 버튼을 여러 번 눌러도 안전하다(같은 이미지를 두 번 돌리지 않음)."""
    files = list_output_images(search_dir)
    rotated = []
    errors = []
    for f in files:
        try:
            with Image.open(f) as img:
                if not is_landscape_hiresfix_size(img.width, img.height):
                    continue
                img.transpose(Image.Transpose.ROTATE_270).save(f)
        except Exception as e:
            errors.append(f"{f.name}: {e}")
            continue
        rotated.append(f.name)
    return {"total_checked": len(files), "rotated": rotated, "errors": errors}
