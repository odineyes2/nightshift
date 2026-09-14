"""
서버에 쌓인 결과 동영상(NIGHTSHIFT_OUTPUT_DIR)에 대한 공용 작업 — 목록 조회, 전체 삭제.
output_images.py와 같은 폴더(NIGHTSHIFT_OUTPUT_DIR)를 보되 IMAGE_EXTENSIONS 대신
VIDEO_EXTENSIONS로 걸러서 뽑는다 — 지금은 동영상을 만드는 파드가 없지만, 장차 생기면
같은 출력 폴더에 함께 저장될 것을 대비해 이미지와 나란히 훑는다.

OutputFolderError는 output_images.py의 것을 그대로 쓴다 — "출력 폴더 자체가 없다"는
사실은 이미지든 동영상이든 같은 폴더 얘기라 따로 정의할 이유가 없다.

환경변수:
    NIGHTSHIFT_OUTPUT_DIR  동영상이 쌓이는 폴더 (기본 /workspace/output, 이미지와 공유)
"""

from pathlib import Path

from output_images import OUTPUT_DIR, OutputFolderError  # noqa: F401 (재노출 — app.py가 여기서 가져다 씀)

VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v"}


def list_output_videos(search_dir: str | None = None) -> list[Path]:
    """search_dir(기본 OUTPUT_DIR)의 동영상 파일 목록. 이미지와 같은 폴더 구조를
    쓰므로(작업별 하위 폴더 포함) 하위 폴더까지 재귀적으로 훑는다. 폴더 자체가
    없으면 에러, 동영상이 0개면 빈 리스트를 돌려준다."""
    search_dir = search_dir or OUTPUT_DIR
    d = Path(search_dir)
    if not d.exists():
        raise OutputFolderError(f"폴더를 찾을 수 없습니다: {search_dir}")
    return [
        p for p in sorted(d.rglob("*"))
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    ]


def delete_output_videos(search_dir: str | None = None) -> int:
    """search_dir의 동영상 파일을 모두 지우고 지운 개수를 반환한다."""
    files = list_output_videos(search_dir)
    for f in files:
        f.unlink()
    return len(files)


def find_video_files(search_dir: str) -> list[Path]:
    """list_output_videos와 같지만, 동영상이 하나도 없으면 에러로 취급한다
    (zip 전체 다운로드처럼 '뭔가 있어야' 의미 있는 동작에서 사용)."""
    files = list_output_videos(search_dir)
    if not files:
        raise OutputFolderError(f"{search_dir}에서 동영상 파일을 찾지 못했습니다.")
    return files
