"""
ControlNet 레퍼런스 이미지 저장소 — pose(OpenPose 등)/depth/lineart 세 가지 "종류"의
참조 이미지를 종류별로 미리 폴더에 쌓아두고, 템플릿이 그중 하나를 순차/랜덤으로
골라 LoadImage 노드에 주입하게 한다. 원래 포즈(OpenPose) 전용이던 pose_assets.py를
depth/lineart까지 일반화한 것 — 함수 시그니처가 전부 kind(종류)를 받는 것만 빼면
동작은 예전 그대로다.

디렉토리 구조 (종류별로 완전히 독립된 이름공간):
    NIGHTSHIFT_ASSETS_DIR(기본 /workspace/dataset/assets)/
        pose/
            <char_no>/              예: "1"(1인물/solo), "2"(2인물/duo), ...
                <세트 이름>/
                    image1.png
                    ...
        depth/
            <char_no>/
                <세트 이름>/
                    ...
        lineart/
            <char_no>/
                ...

    char_no는 그 아래 세트들이 "몇 인물이 함께 그려진 참조인지"를 나타낸다. 폴더
    구조 자체로 인물 수를 강제해서, CSV 작성자가 duo용 세트를 쓰면서 실수로 solo용
    세트/프롬프트를 섞어 넣는 사고를 원천 차단하는 게 목적이다(기능 확장이 아니라
    오사용 방지). 이 범위는 "참조 이미지 1장에 여러 인물이 이미 함께 그려져 있어
    ControlNet 노드 1개로 그대로 처리 가능한 경우"로 한정한다 — 캐릭터별로 별도
    ControlNet을 붙이는 멀티 캐릭터 구조는 다루지 않는다(단, 한 캐릭터에 대해
    pose+depth+lineart처럼 서로 다른 "종류"를 동시에 쓰는 것은 템플릿 쪽(주 참조 +
    보조 참조)에서 지원한다 — templates/pose_batch.py 등 참고).

    각 배치 템플릿(pose_batch/depth_batch/lineart_batch)은 업로드 폼에서 "인물
    수"(char_no) 드롭다운을 먼저 고르고, 그 값에 따라 "세트" 드롭다운이 다시
    채워지는 캐스케이딩 방식이다. 기본값은 DEFAULT_CHAR_NO(="1"). CSV 버전
    (pose_csv_batch 등)은 CSV 행마다 참조 컬럼/char_no 컬럼으로 지정하며, 그
    해석은 resolve_ref(kind, name, char_no)가 담당한다.

    세트별로 하위 폴더를 나눠두면(예: "casual_standing", "action_pose") 업로드
    폼의 드롭다운에서 세트를 선택할 수 있다. GET /api/assets?kind=...가 char_no별
    하위 폴더 목록과 각 폴더의 이미지 개수를 스캔해서 트리 형태로 한 번에 돌려준다
    (list_assets_tree).

이 모듈은 nightshift 서버(app.py) 쪽에서 폴더 목록 조회/업로드 시점 검증에만 쓰인다.
실제로 참조 이미지를 고르고 ComfyUI에 주입하는 로직은 templates/pose_batch.py 등에
독립적으로 구현돼 있다(템플릿 스크립트는 서로 임포트하지 않고 복사해서 쓸 수 있게
자기 완결적으로 작성하는 게 이 저장소의 컨벤션이라, 같은 종류별 환경변수를 템플릿도
스스로 읽어서 같은 결론에 도달하게 했다).

CSV의 참조 컬럼처럼, 세트 이름이 아니라 "참조 이미지 하나를 지정하는 자유 형식
문자열"을 해석해야 하는 경우를 위해 resolve_ref()도 여기 둔다. 이 함수는 app.py
(업로드 시점 검증)와 templates/*_csv_batch.py(실행 시점 실제 해석) 양쪽에서 "같은
알고리즘"이어야 하는 단일 소스다 — 후자는 템플릿 자기완결성 컨벤션에 따라 본문을
그대로 복사해서 쓰므로, 이 함수를 고치면 각 *_csv_batch.py의 사본도 반드시 같이
고쳐야 한다.

환경변수:
    NIGHTSHIFT_ASSETS_DIR  종류별 폴더(pose/depth/lineart)들이 있는 상위 디렉토리
                           (기본 /workspace/dataset/assets)
    NIGHTSHIFT_POSES_DIR   (레거시) pose 종류만 이 값을 우선 쓴다 — 예전부터
                           NIGHTSHIFT_POSES_DIR/<char_no>/<세트> 구조로 이미 포즈
                           세트를 쌓아둔 배포라면, 파일을 옮기지 않고 그대로 이
                           환경변수만 유지하면 된다. 설정 안 하면
                           NIGHTSHIFT_ASSETS_DIR/pose를 쓴다.
    NIGHTSHIFT_DEPTH_DIR   depth 종류의 루트를 개별적으로 지정하고 싶을 때 (기본
                           NIGHTSHIFT_ASSETS_DIR/depth)
    NIGHTSHIFT_LINEART_DIR lineart 종류의 루트를 개별적으로 지정하고 싶을 때 (기본
                           NIGHTSHIFT_ASSETS_DIR/lineart)
"""

import os
from dataclasses import dataclass
from pathlib import Path

REF_KINDS = ("pose", "depth", "lineart")
ASSETS_DIR = os.environ.get("NIGHTSHIFT_ASSETS_DIR", "/workspace/dataset/assets")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

# 종류별로 폴더 루트를 개별 지정할 수 있는 환경변수 이름. "pose"만 예전 이름
# (NIGHTSHIFT_POSES_DIR, 복수형 POSES)을 그대로 쓴다 — 이미 그 이름으로 배포해둔
# 곳들이 있어서, 파일을 옮기지 않아도 되게 하기 위한 하위호환이다.
_KIND_ENV_OVERRIDE = {
    "pose": "NIGHTSHIFT_POSES_DIR",
    "depth": "NIGHTSHIFT_DEPTH_DIR",
    "lineart": "NIGHTSHIFT_LINEART_DIR",
}

# pose_batch류 템플릿(char_no 개념이 없는 1인물 전용 단순 템플릿)이 보는 고정 스코프.
DEFAULT_CHAR_NO = "1"


class RefAssetError(Exception):
    """세트를 찾을 수 없거나 쓸 수 없는 상태(빈 폴더 등)일 때, 사용자가 읽을 메시지와 함께 전달."""


class RefReferenceError(Exception):
    """resolve_ref()가 값 하나를 해석하지 못했을 때의 공통 베이스."""


class RefReferenceNotFoundError(RefReferenceError):
    """세트/파일 어느 쪽으로도 찾지 못했거나, 형식이 잘못됐을 때."""


class RefReferenceAmbiguousError(RefReferenceError):
    """파일명만으로 지정했는데 같은 이름의 파일이 세트 여러 개에 동시에 있을 때."""


def _validate_kind(kind: str) -> str:
    if kind not in REF_KINDS:
        raise RefAssetError(f"알 수 없는 참조 종류예요: {kind!r} (pose/depth/lineart 중 하나여야 해요).")
    return kind


def kind_dir(kind: str) -> Path:
    """이 종류(kind)의 루트 폴더. 종류별 override 환경변수가 있으면 그걸 그대로
    쓰고(레거시 NIGHTSHIFT_POSES_DIR 등), 없으면 ASSETS_DIR/<kind>를 쓴다."""
    _validate_kind(kind)
    override = os.environ.get(_KIND_ENV_OVERRIDE[kind])
    if override:
        return Path(override)
    return Path(ASSETS_DIR) / kind


def _char_dir(kind: str, char_no: str) -> Path:
    return kind_dir(kind) / str(char_no)


def _set_dir(kind: str, char_no: str, name: str) -> Path:
    return _char_dir(kind, char_no) / name


def list_ref_images(kind: str, char_no: str, name: str) -> list[Path]:
    """kind/char_no 아래 세트 name 안의 이미지 파일 목록(정렬됨). 세트 폴더가 없으면 빈 리스트."""
    d = _set_dir(kind, char_no, name)
    if not d.is_dir():
        return []
    return sorted(
        p for p in d.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    )


def list_ref_sets(kind: str, char_no: str = DEFAULT_CHAR_NO) -> list[dict]:
    """kind_dir(kind)/char_no 바로 아래의 하위 폴더들을 스캔해서 [{"name", "count"}, ...]로
    돌려준다. 그 폴더 자체가 없으면 빈 리스트(에러 아님 — 아직 세트를 안 올려둔
    상태일 수 있음)."""
    base = _char_dir(kind, char_no)
    if not base.is_dir():
        return []
    sets = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        sets.append({"name": entry.name, "count": len(list_ref_images(kind, char_no, entry.name))})
    return sets


def list_char_nos(kind: str) -> list[str]:
    """kind_dir(kind) 바로 아래의 하위 폴더 중 숫자 이름인 것들(char_no)을 정렬해서
    돌려준다. 그 폴더 자체가 없으면 빈 리스트."""
    base = kind_dir(kind)
    if not base.is_dir():
        return []
    return sorted(
        (e.name for e in base.iterdir() if e.is_dir() and e.name.isdigit()),
        key=int,
    )


def list_assets_tree(kind: str) -> list[dict]:
    """GET /api/assets?kind=...가 반환하는 트리 — 이 종류의 각 char_no와 그 아래
    세트 목록을 [{"name": char_no, "pose_sets": [{"name", "count"}, ...]}, ...]로
    한 번에 돌려준다. (키 이름 "pose_sets"는 예전 API와의 하위호환을 위해 종류와
    무관하게 그대로 유지 — 프론트엔드가 이미 이 키로 읽고 있다.) 프론트엔드가 이
    트리를 종류별로 한 번씩만 받아서 캐시해두면, "인물 수" 드롭다운을 바꿀 때마다
    서버에 다시 요청하지 않고도 "세트" 드롭다운을 그 자리에서 다시 채울 수 있다."""
    return [
        {"name": char_no, "pose_sets": list_ref_sets(kind, char_no)}
        for char_no in list_char_nos(kind)
    ]


def validate_new_char_no(raw: str | None) -> str:
    """갤러리 이미지를 참조 세트로 보낼 때 "새 인물 수" 입력값을 검증한다.
    list_char_nos()가 숫자 이름 폴더만 인식하므로(char_no.isdigit()), 새로
    만드는 값도 반드시 1 이상의 정수여야 다음에 다시 목록에 나타난다."""
    raw = (raw or "").strip()
    if not raw or not raw.isdigit() or int(raw) < 1:
        raise RefAssetError("인물 수는 1 이상의 숫자여야 해요.")
    return str(int(raw))  # "01" 같은 표기를 "1"로 정규화


def validate_new_folder_name(name: str | None, what: str) -> str:
    """새 세트 이름처럼, 사용자가 자유롭게 입력하는 폴더 이름을 검증한다.
    Path(name).name이 원래 값과 다르면(경로 구분자나 상위 폴더 이동이
    포함됐다는 뜻) 거부한다 — 두 플랫폼(POSIX "/", Windows "\\") 모두 방어."""
    name = (name or "").strip()
    if not name or name in (".", "..") or Path(name).name != name:
        raise RefAssetError(f"{what} 이름이 올바르지 않아요 (폴더 구분자나 '..'는 쓸 수 없어요).")
    return name


def save_ref_image(kind: str, char_no: str, set_name: str, filename: str, content: bytes) -> str:
    """kind/char_no/set_name 폴더에 content를 filename으로 저장한다. 폴더가 없으면
    (새 인물 수·새 세트여도) 그대로 만든다. 이미 같은 이름의 파일이 있으면
    지우지 않고 파일명 끝에 _2, _3...을 붙여 저장한다. 실제로 저장한 파일명을
    돌려준다(호출부가 화면에 "N장 추가" 결과를 보여줄 때 참고용)."""
    d = _set_dir(kind, char_no, set_name)
    d.mkdir(parents=True, exist_ok=True)
    stem, suffix = Path(filename).stem, Path(filename).suffix
    candidate = filename
    n = 2
    while (d / candidate).exists():
        candidate = f"{stem}_{n}{suffix}"
        n += 1
    (d / candidate).write_bytes(content)
    return candidate


def validate_ref_set(kind: str, name: str, char_no: str = DEFAULT_CHAR_NO) -> Path:
    """kind/char_no 아래 세트 name이 실제로 존재하고 이미지가 1장 이상 있는지
    확인한다. 잡을 큐에 올리는 시점(업로드 시)에 검사해서, 시작한 뒤에야
    실패하는 일이 없게 한다. char_no를 생략하면 DEFAULT_CHAR_NO를 본다."""
    name = (name or "").strip()
    if not name:
        raise RefAssetError("세트를 선택하세요.")
    d = _set_dir(kind, char_no, name)
    if not d.is_dir():
        raise RefAssetError(f"'{name}' 세트 폴더를 찾을 수 없어요.")
    if not list_ref_images(kind, char_no, name):
        raise RefAssetError(f"'{name}' 세트에 이미지가 하나도 없어요.")
    return d


def parse_char_no(raw: str | None) -> str:
    """CSV의 char_no 컬럼 값을 해석한다. 빈 값이면 기본값(1인물/solo). 정수로
    바뀌지 않으면 ValueError — 참조 해석 실패와 같은 심각도로 다뤄서 그 행만
    건너뛰지 않고 배치 전체를 에러로 중단시키는 게 호출부의 책임이다.
    "01"처럼 앞에 0이 붙은 값도 폴더명과 어긋나지 않도록 "1"로 정규화한다."""
    raw = (raw or "").strip()
    if not raw:
        return DEFAULT_CHAR_NO
    return str(int(raw))


@dataclass
class RefSetRef:
    """값이 세트 이름과 정확히 일치 — "이 세트 안에서 골라라"는 뜻이라 아직 특정
    파일 하나로 정해진 게 아니다. 실제로 어떤 파일을 쓸지는 호출부(피커)가 정한다."""
    set_name: str


@dataclass
class RefFileRef:
    """값이 파일 하나를 정확히 가리킴 — "<세트>/<파일명>" 조합으로 지정했거나,
    파일명만으로 지정했는데 전체 세트를 통틀어 그 이름의 파일이 하나뿐이었던 경우."""
    set_name: str
    path: Path


def resolve_ref(kind: str, name: str, char_no: str):
    """CSV의 참조 컬럼 같은, "참조 이미지 하나를 자유 형식으로 지정하는 문자열"을
    kind/char_no(kind_dir(kind)/char_no를 베이스로 삼음) 안에서 해석한다. 우선순위:

    1. "/"가 있으면 "<세트>/<파일명>" 형식의 정확한 경로로 취급한다.
    2. "/"가 없고 kind_dir(kind)/char_no 아래 세트 폴더 이름과 정확히 일치하면
       "세트 지정"으로 취급한다 (RefSetRef 반환 — 그 세트 안에서 어떤 파일을
       쓸지는 호출부의 피커가 정한다).
    3. "/"가 없고 세트 이름과는 안 맞지만 kind_dir(kind)/char_no 아래 어느 한
       세트 안에 그 파일명이 있으면 "파일명 단독 지정"으로 취급한다(같은
       char_no의 세트만 통틀어 검색 — 다른 char_no는 보지 않는다). 같은
       파일명이 둘 이상의 세트에 동시에 있으면 RefReferenceAmbiguousError.
    4. 위 어디에도 안 맞으면 RefReferenceNotFoundError.

    char_no로 탐색 루트 자체가 갈리므로, 존재하지 않는 char_no(예: solo 세트만
    있는데 char_no="2"로 조회)를 넘기면 자연스럽게 4번으로 떨어져 실패한다 —
    이게 char_no 스코프의 핵심 안전장치다(다른 인물 수용 세트를 실수로 섞어
    쓰는 것을 폴더 구조 자체로 막음).

    빈 값은 호출부가 미리 걸러야 한다 — "참조 없음(ControlNet 비활성화)"은 이
    함수가 아니라 호출부의 관심사다.
    """
    name = (name or "").strip()
    if not name:
        raise RefReferenceNotFoundError("참조 값이 비어 있어요.")

    if "/" in name:
        set_name, _, filename = name.partition("/")
        set_name = set_name.strip()
        filename = filename.strip()
        if not set_name or not filename:
            raise RefReferenceNotFoundError(
                f"'{name}' 형식이 올바르지 않아요 (<세트>/<파일명> 형식이어야 해요)."
            )
        path = _set_dir(kind, char_no, set_name) / filename
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            raise RefReferenceNotFoundError(f"'{name}' 파일을 찾을 수 없어요 (char_no={char_no}).")
        return RefFileRef(set_name=set_name, path=path)

    if _set_dir(kind, char_no, name).is_dir():
        if not list_ref_images(kind, char_no, name):
            raise RefReferenceNotFoundError(f"'{name}' 세트에 이미지가 하나도 없어요 (char_no={char_no}).")
        return RefSetRef(set_name=name)

    matches = []
    base = _char_dir(kind, char_no)
    if base.is_dir():
        for entry in sorted(base.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            candidate = entry / name
            if candidate.is_file() and candidate.suffix.lower() in IMAGE_EXTENSIONS:
                matches.append(entry.name)

    if len(matches) == 1:
        return RefFileRef(set_name=matches[0], path=_set_dir(kind, char_no, matches[0]) / name)
    if len(matches) > 1:
        raise RefReferenceAmbiguousError(
            f"'{name}'가 여러 세트({', '.join(matches)})에 있어요. "
            f"'<세트>/{name}' 형식으로 명시해주세요."
        )
    raise RefReferenceNotFoundError(f"'{name}'를 세트 이름으로도 파일명으로도 찾지 못했어요 (char_no={char_no}).")
