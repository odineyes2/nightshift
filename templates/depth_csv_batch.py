"""
CSV + depth 배치 템플릿 — csv_batch.py의 CSV 기반 배치 제출(프롬프트 여러 개, 시드,
해상도, 배치수)에 depth_batch.py의 depth 레퍼런스 주입을 결합한다. CSV 행마다 depth
컬럼으로 ControlNet에 쓸 depth 레퍼런스 이미지를 지정할 수 있고, 비어 있으면 그
행은 ControlNet 없이(비활성화) 생성한다. pose_csv_batch.py와 완전히 동일한 구조를
갖는 독립 스크립트다 — "주(main) 참조"만 depth로 고정돼 있다.

주의: csv_batch.py, pose_csv_batch.py와 마찬가지로 이 저장소의 템플릿은 서로 임포트하지
않고 필요한 로직을 그대로 복사해서 자기 완결적으로 작성하는 게 컨벤션이다. 특히 아래
"참조 해석" 섹션(RefSetRef/RefFileRef/resolve_ref_reference 등)은
ref_assets.py의 resolve_ref와 완전히 같은 알고리즘이어야 하는 단일 소스를 복사해온
것이다 — ref_assets.py의 resolve_ref를 고치면 여기도 반드시 같이
고쳐야 한다(app.py는 업로드 시점에 ref_assets.py 쪽을 직접 호출해서 검증하고,
이 스크립트는 실행 시점에 이 사본으로 다시 검증한다 — 둘이 다르게 판단하면 안 됨).

CSV 컬럼 (csv_batch.py와 동일 + depth):
    title           결과 파일명 접두사로 쓰일 제목 (선택, name도 허용)
    trigger_prompt   트리거워드 프롬프트 (선택)
    main_prompt      본문 프롬프트 (prompt와 동일하게 취급, 둘 중 하나는 있어야 함)
    quality_prompt   퀄리티 태그 프롬프트 (선택)
    negative_prompt  네거티브 프롬프트 (선택)
    prompt           main_prompt가 없을 때 쓰이는 대체 컬럼 (하위 호환용, 선택)
    seed             지정하면 그 값을 시드로 사용, 비어 있으면 행마다 랜덤 시드 1개
                     (csv_batch.py와 달리 SEEDS_PER_CASE 개념이 없다 — 행 하나 =
                     반복 한 번. 아래 "진행 상황 보고" 참고)
    batch_no         한 번에 생성할 이미지 수 (EmptyLatentImage류 노드의 batch_size)
    width, height    해상도를 가로/세로 각각 픽셀 값으로 직접 지정 (둘 다 채워야 적용)
    resolution       width/height가 비어 있을 때 대신 쓰이는 해상도 (WxH 또는 프리셋)
    depth            이 행에 쓸 depth 레퍼런스. 비어 있으면 이 행은 ControlNet을
                     비활성화(strength=0)한 채로 생성하며, 이때 char_no는 의미가
                     없으므로 무시된다. 값이 있으면 아래 "참조 해석" 규칙으로
                     char_no가 가리키는 폴더 안에서 해석한다.
    char_no          이 행이 몇 인물용 depth 참조인지(예: "1"=solo, "2"=duo).
                     비어 있으면 "1"로 취급한다. depth가 비어 있으면 이 컬럼은
                     안 읽는다. 정수로 안 바뀌면 depth 해석 실패와 같은 수준으로
                     취급해 배치 전체를 에러로 중단한다(아래 참고).
    secondary_ref    (선택) 이 행에 함께 쓸 보조 참조 이미지 — SECONDARY_KIND
                     환경변수(job 전체에 하나, 아래 "보조 참조" 절 참고)가
                     "none"이 아닐 때만 읽힌다. depth 컬럼과 완전히 같은 규칙
                     (<세트>/<파일명> 또는 세트 이름 또는 파일명 단독)으로
                     SECONDARY_KIND 종류의 폴더 안에서 해석한다. 비어 있으면
                     이 행은 보조 참조 없이 생성한다. 값이 있는데 해석에
                     실패하면(존재하지 않는 세트/파일 등) depth 컬럼과 마찬가지로
                     배치 전체를 에러로 중단한다.
    secondary_char_no (선택) secondary_ref가 몇 인물용인지. 비어 있으면 "1".
                     secondary_ref가 비어 있으면 이 컬럼은 안 읽는다.

depth 레퍼런스 폴더 구조(char_no로 스코프됨):
    NIGHTSHIFT_ASSETS_DIR(기본 /workspace/dataset/assets)/depth/
    (레거시/개별 override: NIGHTSHIFT_DEPTH_DIR을 설정하면 대신 그 값을 씀)
        1/                  1인물(solo) 세트들
            <세트>/<파일>...
        2/                  2인물(duo) 세트들 — 1과 완전히 분리된 별개의 이름공간
            <세트>/<파일>...
    char_no가 다르면 같은 이름의 세트(예: "1/battle"과 "2/battle")가 있어도
    서로 완전히 다른 폴더로 취급된다 — CSV 작성자가 duo용 세트를 쓰려다 실수로
    solo용 세트를 섞어 넣는 사고를, 폴더 구조 자체로 막는 게 이 스코프의
    목적이다(기능 확장이 아니라 오사용 방지). 이 범위는 "depth 이미지 1장에
    여러 인물이 이미 함께 표현돼 있어 ControlNet 노드 1개로 그대로 처리
    가능한 경우"로 한정하며, 캐릭터별로 별도 ControlNet을 붙이는 멀티
    ControlNet 구조는 다루지 않는다.

참조 해석(depth/secondary_ref 컬럼 값, resolve_ref_reference(kind_dir, char_no, name)):
    1. "/"가 있으면 "<세트>/<파일명>" 형식의, char_no 폴더 안에서의 정확한
       경로로 취급한다.
    2. "/"가 없고 kind_dir/char_no 아래 세트 폴더 이름과 정확히
       일치하면 "세트 지정"으로 취급한다 — 그 세트 전용 피커(DEPTH_MODE에 따라
       순차/랜덤)에서 하나를 뽑는다. 같은 (char_no, 세트 이름) 조합이 여러
       행에 나오면 피커 하나를 공유해서 순서대로 소비한다(행을 처리하는 순서
       = CSV 순서). char_no가 다르면 세트 이름이 같아도 별개의 피커다.
    3. "/"가 없고 세트 이름과는 안 맞지만 char_no 아래 어느 한 세트 안에 그
       파일명이 있으면 "파일명 단독 지정"으로 취급한다(같은 char_no의 세트만
       통틀어 검색 — 다른 char_no는 안 봄). 같은 파일명이 그 안에서 둘 이상의
       세트에 동시에 있으면 에러.
    4. 위 어디에도 안 맞으면 에러 — 존재하지 않는 char_no(예: solo 세트만 있는데
       char_no=2로 참조)를 넘기면 자연스럽게 여기로 떨어져 실패한다. 이게
       char_no 스코프의 핵심 안전장치다.
    정확한 경로(1)/파일명 단독 지정(3)인 행은 피커 상태에 영향을 주지 않는다(그
    세트를 "소비"하지 않음) — 세트 지정(2)인 행만 피커를 소비한다.

    depth/char_no 값 해석은 잡을 큐에 올리는 시점(app.py 업로드 검증)에 CSV
    전체를 미리 한 번 돌려 확인하지만, 그 사이 depth 폴더 내용이 바뀌었을 수
    있으므로 이 스크립트도 실행 시작 시 전체 CSV에 대해 해석을 다시 수행한다
    (제출을 시작하기 전에 — 일부만 제출된 채 실패하는 일이 없도록). 한 행이라도
    해석에 실패하면 그 행만 건너뛰지 않고 배치 전체를 에러로 중단한다.

환경변수:
    WORKFLOW_PATH      (필수) ComfyUI API 형식 workflow json 경로 (nightshift가 주입)
    CSV_PATH           (필수) 위 컬럼을 가진 csv 경로 (nightshift가 주입)
    DEPTH_MODE         "sequential" 또는 "random" (nightshift가 템플릿 옵션 "depth_mode"로
                       주입, 기본 sequential) — depth 컬럼에 세트 이름만 지정한 행에 적용
                       (secondary_ref에 세트 이름만 지정한 행에도 같은 값을 씀)
    SECONDARY_KIND      "none"(기본, 꺼짐)/"pose"/"depth"/"lineart" — CSV의
                       secondary_ref/secondary_char_no 컬럼을 어느 종류의 폴더에서
                       해석할지 (nightshift가 템플릿 옵션 "secondary_kind"로 주입).
                       job 전체에 하나만 고를 수 있다(행마다 다른 종류를 쓸 수는 없음).
    SECONDARY_NODE_TITLE 보조 참조 이미지를 주입할 LoadImage 노드의 _meta.title
                       부분일치 (기본 "secondary" — DEPTH_NODE_TITLE과 달리 정확히
                       이 제목을 가진 노드가 없으면 다른 노드로 대체하지 않고 건너뜀)
    NIGHTSHIFT_ASSETS_DIR  pose/depth/lineart 종류별 폴더들이 있는 상위 디렉토리
                       (기본 /workspace/dataset/assets)
    NIGHTSHIFT_DEPTH_DIR   (선택 override) depth 종류의 세트들이 있는 상위 폴더를
                       NIGHTSHIFT_ASSETS_DIR/depth 대신 개별 지정. nightshift
                       서버(ref_assets.py)와 같은 값을 봐야 하므로 손대지 않는 게 안전함
    NIGHTSHIFT_POSES_DIR   (선택) pose 종류의 세트들이 있는 상위 폴더를 개별 지정
                       (보조 참조로 pose를 쓸 때만 의미가 있음)
    NIGHTSHIFT_LINEART_DIR (선택) lineart 종류의 세트들이 있는 상위 폴더를 개별 지정
                       (보조 참조로 lineart를 쓸 때만 의미가 있음)
    NIGHTSHIFT_OUTPUT_DIR  ComfyUI가 이미지를 저장하는 폴더 (기본 /workspace/output).
                       재현성 기록용 jsonl(depth_csv_batch_manifest.jsonl)을 여기 같이 남긴다
    CHECKPOINT         체크포인트 파일명 (nightshift가 템플릿 옵션 "checkpoint"로 주입, 기본 빈 값 —
                       비워두면 워크플로우에 들어있는 체크포인트를 그대로 씀). 화면의 드롭다운은
                       ComfyUI에 실제로 설치된 목록에서만 고르게 돼 있다.
    LORA_NAME          LoRA 파일명 (템플릿 옵션 "lora_name", 기본 빈 값 — 비워두면 그대로 씀).
                       LoRA 로더가 여러 개면 그중 하나에만 적용된다(apply_lora 참고).
    LORA_STRENGTH      LoRA 강도 strength_model (템플릿 옵션 "lora_strength", 기본 빈 값 —
                       비워두면 그대로 씀). strength_clip은 건드리지 않는다.
    CHECKPOINT_NODE_TITLE  체크포인트를 주입할 노드의 _meta.title 부분일치
                       (기본 없음 — 실제로 배선된 로더 노드를 자동으로 고름)
    LORA_NODE_TITLE    LoRA를 주입할 노드의 _meta.title 부분일치 (기본 없음)
    COMFY_URL          ComfyUI 서버 주소 (기본 http://127.0.0.1:8188)
    JOB_ID             nightshift가 주입하는 이 작업의 id (진행 상황 보고용, 없으면 보고 생략)
    NIGHTSHIFT_URL     nightshift 자신의 주소 (진행 상황 보고용, 기본 http://127.0.0.1:8000)
    NIGHTSHIFT_API_KEY 설정돼 있으면 진행 상황 보고에 X-API-Key로 실어 보냄 (선택)
    SEED_NODE_TITLE    시드를 주입할 노드의 _meta.title 부분일치 (기본 "KSampler")
    LATENT_NODE_TITLE  해상도/배치수를 주입할 노드의 _meta.title 부분일치 (기본 "latent")
    SAVE_NODE_TITLE    파일명 접두사를 주입할 노드의 _meta.title 부분일치 (기본 "Save")
    DEPTH_NODE_TITLE   depth 이미지를 주입할 LoadImage 노드의 _meta.title 부분일치
                       (기본 "Load" — 일치하는 노드가 없으면 워크플로우에 LoadImage가
                       하나뿐일 때 그 노드를 대신 쓴다)
    CONTROLNET_NODE_TITLE  depth가 비어 있을 때 비활성화(strength=0)할 노드의
                       _meta.title 부분일치 (기본 "ControlNet" — 마찬가지로 일치하는
                       노드가 없으면 ControlNetApplyAdvanced/ControlNetApply 노드가
                       하나뿐일 때 그 노드를 대신 쓴다)
    POLL_INTERVAL_SEC  히스토리 폴링 간격 초 (기본 2)
    POLL_TIMEOUT_SEC   개별 작업 완료 대기 제한 초 (기본 600)

ComfyUI로의 depth 이미지 주입 방식:
    depth_batch.py와 동일 — LoadImage가 참조하는 파일은 ComfyUI 자신의 input
    폴더에 있어야 하므로, 파일시스템 공유 여부와 무관하게 항상 동작하도록 매번
    ComfyUI의 POST /upload/image API로 업로드하고 응답받은 파일명을 주입한다.

ControlNet 비활성화(depth가 비어 있는 행):
    그래프를 재배선하지 않고, apply_seed와 같은 패턴으로 ControlNetApplyAdvanced류
    노드는 그대로 둔 채 strength 입력값만 0으로 덮어써서 사실상 꺼진 것과 같은
    효과를 낸다.

진행 상황 보고:
    csv_batch.py와 같은 방식(총 예상 이미지 수를 미리 계산해 PUT
    /api/jobs/{job_id}/progress로 보고)이되, 행마다 시드 하나 = 반복 한 번이라
    별도의 "케이스당 시드 개수" 개념이 없다 — 유효한 행 수가 곧 반복 횟수다(단
    batch_no로 한 행에서 여러 장을 만들면 그만큼 이미지 수는 늘어남).

재현성 기록:
    depth_batch.py와 같은 형식으로 NIGHTSHIFT_OUTPUT_DIR에
    depth_csv_batch_manifest.jsonl을 이어쓰기(append)한다. depth가 비어 있던 행은
    char_no/depth_set/depth_file을 모두 null로 남겨 "의도적으로 ControlNet 없이
    생성했다"는 걸 구분한다. char_no가 기본값(1)이 아니면 SaveImage의
    filename_prefix에도 "_char<N>"이 붙는다(결과물을 인물 수 기준으로 정리할 때 씀).
    보조 참조를 쓴 행은 secondary_kind/secondary_set/secondary_file도 함께 남긴다.
    JOB_ID가 있으면 이 filename_prefix 앞에 "<JOB_ID>/"를 붙여 ComfyUI가 출력 폴더
    밑에 그 job_id 폴더를 만들고 그 안에 저장하게 한다 — nightshift 갤러리의
    "작업별 보기"가 이 폴더 이름으로 묶어서 보여준다(output_images.py 참고).

보조 참조(SECONDARY_KIND 등) — depth와 pose/lineart를 한 생성에 같이 쓰기:
    이 템플릿의 "주(main) 참조"는 항상 depth다(위 depth/char_no 컬럼). 그와 별개로,
    SECONDARY_KIND를 "none"이 아닌 값으로 설정하면 CSV의 secondary_ref/
    secondary_char_no 컬럼이 활성화되어, 행마다 pose나 lineart 레퍼런스를
    "보조 참조"로 하나 더 얹을 수 있다. 워크플로우에서 제목에 SECONDARY_NODE_TITLE
    (기본 "secondary")이 포함된 LoadImage 노드를 찾아 주입하며, 이 노드는 depth_batch.py와
    마찬가지로 "정확히 제목이 일치해야만" 쓴다(class_type 대체 없음 — 워크플로우에
    LoadImage가 하나뿐일 때 주 참조 노드를 실수로 덮어쓰는 걸 막기 위함). 이 노드가
    없으면 경고만 남기고 그 실행은 보조 참조 없이 진행한다(best-effort). 다만 CSV의
    secondary_ref 값 자체는(노드 존재 여부와 별개로) depth 컬럼과 같은 엄격함으로
    해석한다 — 값이 있는데 세트/파일을 찾지 못하면 배치 전체를 에러로 중단한다.
"""

import copy
import csv
import io
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# ============================================================================
# 참조 이미지 해석 — ref_assets.py의 resolve_ref와 완전히 같은 알고리즘의
# 사본(아래 함수들은 kind_dir을 그냥 "어떤 종류든 그 종류의 루트 폴더"로 받는
# 제네릭한 구현이라 주 참조(depth)/보조 참조 해석에 똑같이 재사용한다).
# ref_assets.py 쪽을 고치면 이 블록도 반드시 같이 고칠 것.
# ============================================================================

REF_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

# ref_assets.py의 DEFAULT_CHAR_NO 사본 — char_no 컬럼이 비어 있을 때(1인물/solo).
DEFAULT_CHAR_NO = "1"

# ref_assets.py의 REF_KINDS/_KIND_ENV_OVERRIDE 사본 — 템플릿은 서버 모듈을 import하지
# 않는 관례라 여기 그대로 복제해둔다. 보조 참조 종류를 어떤 폴더 아래에서 찾을지
# 계산하는 데 쓴다.
REF_KINDS = ("pose", "depth", "lineart")
REF_KIND_ENV_OVERRIDE = {
    "pose": "NIGHTSHIFT_POSES_DIR",
    "depth": "NIGHTSHIFT_DEPTH_DIR",
    "lineart": "NIGHTSHIFT_LINEART_DIR",
}


def ref_kind_dir(kind):
    """kind("pose"/"depth"/"lineart")의 세트들이 있는 상위 폴더. 개별 override
    환경변수(NIGHTSHIFT_DEPTH_DIR 등)가 있으면 그걸, 없으면 NIGHTSHIFT_ASSETS_DIR/kind를
    쓴다 — ref_assets.py의 kind_dir()과 같은 규칙."""
    override = os.environ.get(REF_KIND_ENV_OVERRIDE.get(kind, ""))
    if override:
        return override
    assets_dir = os.environ.get("NIGHTSHIFT_ASSETS_DIR", "/workspace/dataset/assets")
    return str(Path(assets_dir) / kind)


class RefReferenceError(Exception):
    pass


class RefReferenceNotFoundError(RefReferenceError):
    pass


class RefReferenceAmbiguousError(RefReferenceError):
    pass


@dataclass
class RefSetRef:
    set_name: str


@dataclass
class RefFileRef:
    set_name: str
    path: Path


def _char_dir(kind_dir, char_no):
    return Path(kind_dir) / str(char_no)


def _ref_set_dir(kind_dir, char_no, name):
    return _char_dir(kind_dir, char_no) / name


def list_ref_images_in_set(kind_dir, char_no, name):
    d = _ref_set_dir(kind_dir, char_no, name)
    if not d.is_dir():
        return []
    return sorted(
        p for p in d.iterdir()
        if p.is_file() and p.suffix.lower() in REF_IMAGE_EXTENSIONS
    )


def parse_char_no(raw):
    """char_no 컬럼 값을 해석한다. 빈 값이면 기본값(1인물/solo). 정수로 바뀌지
    않으면 ValueError — 참조 해석 실패와 같은 심각도로 다뤄서 그 행만 건너뛰지
    않고 배치 전체를 에러로 중단시킨다(build_plan 참고)."""
    raw = (raw or "").strip()
    if not raw:
        return DEFAULT_CHAR_NO
    return str(int(raw))


def resolve_ref_reference(kind_dir, char_no, name):
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
        path = _ref_set_dir(kind_dir, char_no, set_name) / filename
        if not path.is_file() or path.suffix.lower() not in REF_IMAGE_EXTENSIONS:
            raise RefReferenceNotFoundError(f"'{name}' 파일을 찾을 수 없어요 (char_no={char_no}).")
        return RefFileRef(set_name=set_name, path=path)

    if _ref_set_dir(kind_dir, char_no, name).is_dir():
        if not list_ref_images_in_set(kind_dir, char_no, name):
            raise RefReferenceNotFoundError(f"'{name}' 세트에 이미지가 하나도 없어요 (char_no={char_no}).")
        return RefSetRef(set_name=name)

    matches = []
    base = _char_dir(kind_dir, char_no)
    if base.is_dir():
        for entry in sorted(base.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            candidate = entry / name
            if candidate.is_file() and candidate.suffix.lower() in REF_IMAGE_EXTENSIONS:
                matches.append(entry.name)

    if len(matches) == 1:
        return RefFileRef(set_name=matches[0], path=_ref_set_dir(kind_dir, char_no, matches[0]) / name)
    if len(matches) > 1:
        raise RefReferenceAmbiguousError(
            f"'{name}'가 여러 세트({', '.join(matches)})에 있어요. "
            f"'<세트>/{name}' 형식으로 명시해주세요."
        )
    raise RefReferenceNotFoundError(f"'{name}'를 세트 이름으로도 파일명으로도 찾지 못했어요 (char_no={char_no}).")


# 참조 파일 선택 전략 — depth_batch.py의 사본 -------------------------------

class SequentialPicker:
    """파일명 정렬 순서대로 순환. 개수를 넘기면 처음부터 다시."""

    def __init__(self, files):
        self.files = files
        self.i = 0

    def pick(self):
        f = self.files[self.i % len(self.files)]
        self.i += 1
        return f


class RandomNoRepeatPicker:
    """폴더가 다 소진될 때까지 중복 없이 뽑고, 다 뽑으면 다시 섞어서 리셋."""

    def __init__(self, files):
        self.files = files
        self.pool = []

    def pick(self):
        if not self.pool:
            self.pool = self.files[:]
            random.shuffle(self.pool)
        return self.pool.pop()


def make_picker(mode, files):
    if mode == "random":
        return RandomNoRepeatPicker(files)
    return SequentialPicker(files)


# ============================================================================
# csv_batch.py / depth_batch.py 공통 헬퍼 — 사본
# ============================================================================

PROMPT_FIELD_TITLES = {
    "trigger_prompt": "trigger_prompt",
    "main_prompt": "main_prompt",
    "quality_prompt": "quality_prompt",
    "negative_prompt": "negative_prompt",
    "prompt": "prompt",
}

RESOLUTION_PRESETS = {
    "square": (1024, 1024),
    "portrait": (832, 1216),
    "landscape": (1216, 832),
    "9:16": (768, 1344),
    "16:9": (1344, 768),
}


def env(name, default=None):
    return os.environ.get(name, default)


def load_workflow(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def connected_node_ids(workflow):
    ids = set()
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        for value in node.get("inputs", {}).values():
            if isinstance(value, list) and len(value) == 2:
                ids.add(str(value[0]))
    return ids


def find_node(workflow, title_substring=None, class_types=(), allow_class_fallback=True, prefer_connected=False):
    title_substring = (title_substring or "").lower()
    connected = connected_node_ids(workflow) if (allow_class_fallback and prefer_connected) else None
    fallback = None
    fallback_connected = None
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        meta_title = str(node.get("_meta", {}).get("title", "")).lower()
        class_type = node.get("class_type", "")
        if title_substring and title_substring in meta_title:
            return node_id, node
        if allow_class_fallback and class_types and class_type in class_types:
            if connected is not None and node_id in connected:
                if fallback_connected is None:
                    fallback_connected = (node_id, node)
            elif fallback is None:
                fallback = (node_id, node)
    if fallback_connected is not None:
        return fallback_connected
    return fallback if fallback else (None, None)


# 텍스트/숫자 값을 그대로 담아두는 노드(Primitive 계열)가 실제로 값을 받는
# 입력 필드 이름은 class_type마다 다르다: CLIPTextEncode는 "text",
# PrimitiveStringMultiline/PrimitiveInt 등은 보통 "value"를 쓴다. 매핑에 없는
# class_type이라도 그 노드에 이미 들어있는 입력 키("text" 또는 "value")로
# 추정하므로, 새 종류의 Primitive 노드가 나와도 이 매핑을 매번 늘릴 필요는 없다.
PRIMITIVE_VALUE_FIELDS = {
    "CLIPTextEncode": "text",
    "PrimitiveStringMultiline": "value",
    "PrimitiveString": "value",
    "PrimitiveInt": "value",
    "PrimitiveFloat": "value",
}


def primitive_value_field(node):
    field = PRIMITIVE_VALUE_FIELDS.get(node.get("class_type", ""))
    if field:
        return field
    inputs = node.get("inputs", {})
    if "text" in inputs:
        return "text"
    if "value" in inputs:
        return "value"
    return None


def set_linked_value(workflow, node, field, value):
    """node.inputs[field]에 value를 쓴다. 그 입력이 다른 노드로 연결돼 있으면
    (예: width/height를 별도 PrimitiveInt 노드로 뽑아 여러 곳에 공급하는
    워크플로우) 링크는 그대로 두고 연결된 노드의 값 입력을 갱신해서, 같은
    노드를 참조하는 다른 곳에도 일관되게 반영되게 한다. 연결된 노드의 값
    입력을 알 수 없으면(알 수 없는 노드 구조) 안전하게 이 노드에 리터럴로
    덮어쓴다."""
    inputs = node.setdefault("inputs", {})
    current = inputs.get(field)
    if isinstance(current, list) and len(current) == 2:
        source_node = workflow.get(str(current[0]))
        if isinstance(source_node, dict):
            source_field = primitive_value_field(source_node)
            if source_field:
                source_node.setdefault("inputs", {})[source_field] = value
                return
    inputs[field] = value


def sanitize_prefix(text, fallback):
    text = (text or fallback or "batch").strip()
    text = re.sub(r"[^\w\-가-힣 ]+", "_", text)
    return text[:80] or fallback


def sanitize_stem(text):
    text = re.sub(r"[^\w\-가-힣]+", "_", text or "")
    return text[:40] or "depth"


def apply_prompts(workflow, row):
    applied = False
    for field, title_substring in PROMPT_FIELD_TITLES.items():
        value = (row.get(field) or "").strip()
        if not value:
            continue
        node_id, node = find_node(
            workflow,
            title_substring=title_substring,
            allow_class_fallback=False,
        )
        input_field = primitive_value_field(node) if node is not None else None
        if input_field is None:
            print(
                f"[depth_csv_batch] 경고: '{field}' 값을 넣을 노드를 찾지 못했습니다 "
                f"(제목에 '{title_substring}'가 포함된 CLIPTextEncode/Primitive 텍스트 노드 없음)",
                file=sys.stderr,
            )
            continue
        node.setdefault("inputs", {})[input_field] = value
        applied = True
    if not applied:
        print("[depth_csv_batch] 경고: 이 행에 프롬프트 컬럼 값이 하나도 없습니다.", file=sys.stderr)


def apply_seed(workflow, seed):
    node_id, node = find_node(
        workflow,
        title_substring=env("SEED_NODE_TITLE", "KSampler"),
        class_types=("KSampler", "KSamplerAdvanced"),
    )
    if node is None:
        print("[depth_csv_batch] 경고: 시드를 넣을 노드를 찾지 못했습니다 (KSampler 없음)", file=sys.stderr)
        return
    try:
        node.setdefault("inputs", {})["seed"] = int(seed)
    except (TypeError, ValueError):
        print(f"[depth_csv_batch] 경고: seed 값 '{seed}'을 정수로 변환하지 못했습니다", file=sys.stderr)


def parse_batch_size(batch_no):
    batch_no = (batch_no or "").strip()
    if not batch_no:
        return None
    try:
        value = int(batch_no)
    except ValueError:
        print(f"[depth_csv_batch] 경고: batch_no 값 '{batch_no}'을 정수로 변환하지 못했습니다", file=sys.stderr)
        return None
    return max(1, value)


def get_default_batch_size(workflow):
    node_id, node = find_node(workflow, class_types=("EmptyLatentImage",), prefer_connected=True)
    if node is None:
        return 1
    try:
        return max(1, int(node.get("inputs", {}).get("batch_size", 1)))
    except (TypeError, ValueError):
        return 1


def apply_batch_size(workflow, batch_no):
    batch_size = parse_batch_size(batch_no)
    if batch_size is None:
        return
    node_id, node = find_node(
        workflow,
        title_substring=env("LATENT_NODE_TITLE", "latent"),
        class_types=("EmptyLatentImage",),
        prefer_connected=True,
    )
    if node is None:
        print("[depth_csv_batch] 경고: batch_no를 넣을 노드를 찾지 못했습니다 (EmptyLatentImage 없음)", file=sys.stderr)
        return
    node.setdefault("inputs", {})["batch_size"] = batch_size


def resolve_resolution(width, height, resolution):
    width = (width or "").strip()
    height = (height or "").strip()
    resolution = (resolution or "").strip()

    if width and height:
        try:
            return int(width), int(height)
        except ValueError:
            print(
                f"[depth_csv_batch] 경고: width/height 값 '{width}x{height}'을 정수로 변환하지 못했습니다. "
                "resolution 컬럼으로 대체합니다.",
                file=sys.stderr,
            )
    elif width or height:
        print(
            "[depth_csv_batch] 경고: width/height는 둘 다 채워야 적용됩니다 (하나만 비어 있음). "
            "resolution 컬럼으로 대체합니다.",
            file=sys.stderr,
        )

    if not resolution:
        return None

    preset = RESOLUTION_PRESETS.get(resolution.lower())
    if preset:
        return preset

    match = re.match(r"^(\d+)\s*[xX]\s*(\d+)$", resolution)
    if not match:
        print(
            f"[depth_csv_batch] 경고: resolution 값 '{resolution}'을 해석하지 못했습니다 "
            f"(WxH 형식이거나 {list(RESOLUTION_PRESETS)} 중 하나여야 함)",
            file=sys.stderr,
        )
        return None
    return int(match.group(1)), int(match.group(2))


def find_loader_node(workflow, title_substring, class_types):
    """모델 로더 노드 찾기 — class_types에 속한 노드만 후보로 보고, 그중에서
    (1) 제목이 맞는 노드 → (2) 실제로 배선된 노드 → (3) 남은 아무 노드 순으로 고른다.

    find_node와 달리 제목이 맞아도 종류가 다르면 후보로 치지 않는다. find_node는
    제목만 맞으면 class_type과 무관하게 그 노드를 돌려주는데, 그러면 예를 들어
    LORA_NODE_TITLE="quality"가 "quality_prompt"라는 텍스트 노드에 걸려서 그 노드에
    lora_name을 써넣는 사고가 난다(모델 이름 주입은 엉뚱한 노드에 쓰면 워크플로우가
    조용히 망가지므로 제목보다 종류를 우선한다)."""
    title_substring = (title_substring or "").lower()
    connected = connected_node_ids(workflow)
    titled = None
    first_connected = None
    first_any = None
    for node_id, node in workflow.items():
        if not isinstance(node, dict) or node.get("class_type") not in class_types:
            continue
        meta_title = str(node.get("_meta", {}).get("title", "")).lower()
        if title_substring and title_substring in meta_title and titled is None:
            titled = (node_id, node)
        if node_id in connected:
            if first_connected is None:
                first_connected = (node_id, node)
        elif first_any is None:
            first_any = (node_id, node)
    return titled or first_connected or first_any or (None, None)


CHECKPOINT_CLASS_TYPES = ("CheckpointLoaderSimple", "CheckpointLoader", "unCLIPCheckpointLoader")
LORA_CLASS_TYPES = ("LoraLoader", "LoraLoaderModelOnly")


def apply_checkpoint(workflow):
    """CHECKPOINT가 비어 있으면(기본) 워크플로우에 이미 들어있는 체크포인트를 그대로
    쓰고, 값이 있으면 체크포인트 로더의 ckpt_name을 덮어쓴다. 화면의 드롭다운이
    ComfyUI에 정말 설치돼 있는 목록에서만 고르게 해주므로(app.py의 comfy_model 옵션),
    여기서는 이름을 다시 검증하지 않는다."""
    ckpt = (env("CHECKPOINT", "") or "").strip()
    if not ckpt:
        return
    node_id, node = find_loader_node(workflow, env("CHECKPOINT_NODE_TITLE", ""), CHECKPOINT_CLASS_TYPES)
    if node is None:
        print("[depth_csv_batch] 경고: 체크포인트를 넣을 노드를 찾지 못했습니다 (CheckpointLoader 없음)", file=sys.stderr)
        return
    set_linked_value(workflow, node, "ckpt_name", ckpt)


def apply_lora(workflow):
    """LORA_NAME/LORA_STRENGTH를 LoRA 로더 노드에 주입한다(둘 다 비어 있으면 아무것도
    안 함). LoRA 로더가 여러 개 체인으로 걸려 있으면 그중 하나만 고를 수밖에 없어서,
    제목이 맞는 노드(LORA_NODE_TITLE)를 우선 쓰고 없으면 실제로 배선된 것 중 파일에
    먼저 나오는 노드를 쓴다 — 어느 노드를 바꿨는지 로그로 알려주므로, 다른 노드를
    바꾸고 싶으면 LORA_NODE_TITLE로 지정하면 된다. strength_clip은 건드리지 않는다
    (모델 강도와 클립 강도를 다르게 쓰는 워크플로우가 흔하다)."""
    lora = (env("LORA_NAME", "") or "").strip()
    strength = (env("LORA_STRENGTH", "") or "").strip()
    if not lora and not strength:
        return
    node_id, node = find_loader_node(workflow, env("LORA_NODE_TITLE", ""), LORA_CLASS_TYPES)
    if node is None:
        print("[depth_csv_batch] 경고: LoRA를 넣을 노드를 찾지 못했습니다 (LoraLoader 없음)", file=sys.stderr)
        return
    total = sum(
        1 for n in workflow.values()
        if isinstance(n, dict) and n.get("class_type") in LORA_CLASS_TYPES
    )
    if total > 1:
        print(
            f"[depth_csv_batch] 안내: LoRA 로더가 {total}개라 그중 노드 {node_id}에만 적용했습니다 "
            "(다른 노드에 넣으려면 LORA_NODE_TITLE로 지정하세요).",
            file=sys.stderr,
        )
    if lora:
        set_linked_value(workflow, node, "lora_name", lora)
    if strength:
        try:
            strength_value = float(strength)
        except ValueError:
            print(f"[depth_csv_batch] 경고: LORA_STRENGTH 값 '{strength}'을 숫자로 변환하지 못했습니다.", file=sys.stderr)
        else:
            set_linked_value(workflow, node, "strength_model", strength_value)


def apply_resolution(workflow, width, height, resolution):
    resolved = resolve_resolution(width, height, resolution)
    if resolved is None:
        return
    node_id, node = find_node(
        workflow,
        title_substring=env("LATENT_NODE_TITLE", "latent"),
        class_types=("EmptyLatentImage",),
        prefer_connected=True,
    )
    if node is None:
        print("[depth_csv_batch] 경고: 해상도를 넣을 노드를 찾지 못했습니다 (EmptyLatentImage 없음)", file=sys.stderr)
        return
    width_value, height_value = resolved
    set_linked_value(workflow, node, "width", width_value)
    set_linked_value(workflow, node, "height", height_value)


def apply_filename_prefix(workflow, title, index, seed, char_no, depth_path):
    node_id, node = find_node(
        workflow,
        title_substring=env("SAVE_NODE_TITLE", "Save"),
        class_types=("SaveImage", "SaveImageWebsocket"),
    )
    if node is None:
        return
    prefix = sanitize_prefix(title, f"batch_{index}")
    prefix = f"{prefix}_seed{seed}"
    # 기본값(1인물/solo)일 때는 기존 파일명 그대로 두고, 2인물 이상일 때만
    # char_no를 덧붙여 결과물 정리 시 인물 수로 구분할 수 있게 한다.
    if depth_path is not None and char_no is not None and char_no != DEFAULT_CHAR_NO:
        prefix = f"{prefix}_char{char_no}"
    if depth_path is not None:
        prefix = f"{prefix}_{sanitize_stem(depth_path.stem)}"
    # JOB_ID가 있으면 ComfyUI 출력 폴더 밑에 그 job_id 하위 폴더를 만들어 저장한다 —
    # filename_prefix의 "/"를 ComfyUI SaveImage가 하위 폴더로 해석한다. nightshift
    # 갤러리는 이 폴더 이름으로 "작업별 보기"를 구성한다(output_images.py 참고).
    job_id = env("JOB_ID")
    if job_id:
        prefix = f"{job_id}/{prefix}"
    node.setdefault("inputs", {})["filename_prefix"] = prefix


def report_progress(job_id, nightshift_url, total, done):
    if not job_id or not nightshift_url:
        return
    try:
        payload = json.dumps({"total": total, "done": done}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        # nightshift가 NIGHTSHIFT_API_KEY로 인증을 켜두면 /api/*가 전부 이 키를
        # 요구한다. 키 없이 보내면 401로 조용히 실패해서(경고만 찍고 계속 돈다)
        # 진행률 바가 영영 안 움직인다 — 홈서버처럼 밖에서 닿는 곳에 띄운다면 키를
        # 켜는 쪽이 정상이므로 여기서 같이 실어 보낸다.
        api_key = os.environ.get("NIGHTSHIFT_API_KEY", "").strip()
        if api_key:
            headers["X-API-Key"] = api_key
        req = urllib.request.Request(
            f"{nightshift_url}/api/jobs/{job_id}/progress",
            data=payload,
            headers=headers,
            method="PUT",
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"[depth_csv_batch] 경고: 진행 상황 보고 실패: {e}", file=sys.stderr)


def append_manifest(output_dir, record):
    try:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        path = Path(output_dir) / "depth_csv_batch_manifest.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[depth_csv_batch] 경고: 재현성 기록 실패: {e}", file=sys.stderr)


# ============================================================================
# ComfyUI 연동 — depth_batch.py의 사본
# ============================================================================

def upload_image_to_comfy(comfy_url, image_path):
    boundary = uuid.uuid4().hex
    with open(image_path, "rb") as f:
        file_bytes = f.read()

    body = io.BytesIO()

    def write(chunk):
        body.write(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)

    write(f"--{boundary}\r\n")
    write(f'Content-Disposition: form-data; name="image"; filename="{image_path.name}"\r\n')
    write("Content-Type: application/octet-stream\r\n\r\n")
    write(file_bytes)
    write("\r\n")
    write(f"--{boundary}\r\n")
    write('Content-Disposition: form-data; name="overwrite"\r\n\r\n')
    write("true\r\n")
    write(f"--{boundary}--\r\n")

    req = urllib.request.Request(
        f"{comfy_url}/upload/image",
        data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    return result["name"]


def apply_depth_image(workflow, comfy_url, depth_path):
    node_id, node = find_node(
        workflow,
        title_substring=env("DEPTH_NODE_TITLE", "Load"),
        class_types=("LoadImage",),
        prefer_connected=True,
    )
    if node is None:
        print("[depth_csv_batch] 경고: depth 이미지를 넣을 노드를 찾지 못했습니다 (LoadImage 없음)", file=sys.stderr)
        return
    uploaded_name = upload_image_to_comfy(comfy_url, depth_path)
    node.setdefault("inputs", {})["image"] = uploaded_name


def apply_secondary_reference(workflow, comfy_url, secondary_kind, ref_path):
    """보조 참조(SECONDARY_KIND) 이미지 주입 — best-effort. 주 참조(apply_depth_image)와
    달리 class_type 대체(fallback) 없이 SECONDARY_NODE_TITLE과 제목이 정확히 일치하는
    노드만 쓴다(모듈 docstring "보조 참조" 절 참고) — 워크플로우에 LoadImage가 하나뿐일
    때 주 참조 노드를 실수로 덮어쓰는 걸 막기 위함이다."""
    title = env("SECONDARY_NODE_TITLE", "secondary")
    node_id, node = find_node(workflow, title_substring=title, allow_class_fallback=False)
    if node is None:
        print(
            f"[depth_csv_batch] 안내: 보조 참조({secondary_kind}) 이미지를 넣을 노드(제목에 "
            f"'{title}' 포함)를 찾지 못해 이번 실행은 보조 참조 없이 진행합니다.",
            file=sys.stderr,
        )
        return
    if node.get("class_type") != "LoadImage":
        print(
            f"[depth_csv_batch] 경고: SECONDARY_NODE_TITLE('{title}')로 찾은 노드가 LoadImage가 "
            "아니라 보조 참조를 건너뜁니다.",
            file=sys.stderr,
        )
        return
    uploaded_name = upload_image_to_comfy(comfy_url, ref_path)
    node.setdefault("inputs", {})["image"] = uploaded_name


def disable_controlnet(workflow):
    """depth 컬럼이 비어 있는 행 — 그래프를 재배선하지 않고 ControlNetApplyAdvanced류
    노드의 strength만 0으로 덮어써서 사실상 꺼진 것과 같은 효과를 낸다(apply_seed와
    동일한 "노드는 그대로, 값만 덮어쓰기" 패턴)."""
    node_id, node = find_node(
        workflow,
        title_substring=env("CONTROLNET_NODE_TITLE", "ControlNet"),
        class_types=("ControlNetApplyAdvanced", "ControlNetApply"),
        prefer_connected=True,
    )
    if node is None:
        print(
            "[depth_csv_batch] 경고: ControlNet 노드를 찾지 못해 비활성화를 건너뜁니다 "
            "(ControlNetApplyAdvanced/ControlNetApply 없음)",
            file=sys.stderr,
        )
        return
    node.setdefault("inputs", {})["strength"] = 0


def queue_prompt(comfy_url, workflow):
    payload = json.dumps({"prompt": workflow, "client_id": str(uuid.uuid4())}).encode("utf-8")
    req = urllib.request.Request(
        f"{comfy_url}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "error" in data:
        raise RuntimeError(f"ComfyUI가 프롬프트를 거부했습니다: {data['error']}")
    return data["prompt_id"]


def wait_for_completion(comfy_url, prompt_id):
    interval = float(env("POLL_INTERVAL_SEC", "2"))
    timeout = float(env("POLL_TIMEOUT_SEC", "600"))
    deadline = time.time() + timeout
    while time.time() < deadline:
        req = urllib.request.Request(f"{comfy_url}/history/{prompt_id}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                history = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise RuntimeError(f"ComfyUI 히스토리 조회 실패: {e}") from e
        if prompt_id in history:
            return history[prompt_id]
        time.sleep(interval)
    raise TimeoutError(f"prompt_id={prompt_id} 완료 대기 시간({timeout}s) 초과")


# ============================================================================
# 실행 계획 수립 + 제출
# ============================================================================

def build_plan(base_workflow, rows, depth_dir, depth_mode, secondary_kind=None, secondary_dir=None):
    """CSV 행마다 (row, title, seed, batch_size, char_no, depth_path, secondary_path)를
    미리 계산한다. depth/char_no(및 secondary_kind가 설정됐으면 secondary_ref/
    secondary_char_no) 컬럼 해석은 여기서 전부 끝내둔다 — 세트 지정 행은 이 단계에서
    피커를 소비해 실제 파일을 정하고, 해석에 실패하는 행이 하나라도 있으면 그
    자리에서 배치 전체를 중단한다(건너뛰지 않음) — depth와 secondary_ref 둘 다 같은
    엄격함으로 다룬다(모듈 docstring "보조 참조" 절 참고).

    피커는 (char_no, set_name) 조합별로 하나씩 lazy하게 만든다 — char_no가
    다르면 같은 이름의 세트라도 완전히 다른 폴더(다른 파일 목록)이므로, set_name만
    으로 캐시하면 서로 다른 세트가 피커 하나를 잘못 공유하게 된다. 주 참조(depth)와
    보조 참조는 폴더 자체가 다를 수 있으므로 피커 캐시도 서로 별도로 둔다."""
    default_batch_size = get_default_batch_size(base_workflow)
    pickers = {}
    secondary_pickers = {}

    def get_picker(cache, base_dir, char_no, set_name, label):
        key = (char_no, set_name)
        if key not in cache:
            files = list_ref_images_in_set(base_dir, char_no, set_name)
            if not files:
                raise RefReferenceNotFoundError(f"'{set_name}' {label} 세트에 이미지가 없습니다 (char_no={char_no}).")
            cache[key] = make_picker(depth_mode, files)
        return cache[key]

    plan = []
    for line_no, row in enumerate(rows, start=2):  # 헤더가 1번 줄
        title = (row.get("title") or row.get("name") or "").strip()
        main_prompt = (row.get("main_prompt") or row.get("prompt") or "").strip()
        if not main_prompt:
            print(f"[depth_csv_batch] 건너뜀 (main_prompt/prompt 없음, {line_no}번째 줄): {row}")
            continue

        batch_size = parse_batch_size(row.get("batch_no"))
        if batch_size is None:
            batch_size = default_batch_size

        row_seed = (row.get("seed") or "").strip()
        seed = row_seed if row_seed else random.randint(0, 2**31 - 1)

        depth_raw = (row.get("depth") or "").strip()
        char_no = None
        depth_path = None
        if depth_raw:
            try:
                char_no = parse_char_no(row.get("char_no"))
            except ValueError:
                print(
                    f"[depth_csv_batch] 오류: {line_no}번째 줄의 char_no 값 "
                    f"'{row.get('char_no')}'을 정수로 변환하지 못했습니다.",
                    file=sys.stderr,
                )
                sys.exit(1)
            try:
                ref = resolve_ref_reference(depth_dir, char_no, depth_raw)
                if isinstance(ref, RefFileRef):
                    depth_path = ref.path
                else:
                    depth_path = get_picker(pickers, depth_dir, char_no, ref.set_name, "depth").pick()
            except RefReferenceError as e:
                print(
                    f"[depth_csv_batch] 오류: {line_no}번째 줄의 depth 값 '{depth_raw}' "
                    f"(char_no={char_no})을 해석하지 못했습니다: {e}",
                    file=sys.stderr,
                )
                sys.exit(1)

        secondary_path = None
        if secondary_kind:
            secondary_raw = (row.get("secondary_ref") or "").strip()
            if secondary_raw:
                try:
                    secondary_char_no = parse_char_no(row.get("secondary_char_no"))
                except ValueError:
                    print(
                        f"[depth_csv_batch] 오류: {line_no}번째 줄의 secondary_char_no 값 "
                        f"'{row.get('secondary_char_no')}'을 정수로 변환하지 못했습니다.",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                try:
                    ref = resolve_ref_reference(secondary_dir, secondary_char_no, secondary_raw)
                    if isinstance(ref, RefFileRef):
                        secondary_path = ref.path
                    else:
                        secondary_path = get_picker(
                            secondary_pickers, secondary_dir, secondary_char_no, ref.set_name, f"보조 참조({secondary_kind})"
                        ).pick()
                except RefReferenceError as e:
                    print(
                        f"[depth_csv_batch] 오류: {line_no}번째 줄의 secondary_ref 값 '{secondary_raw}' "
                        f"(secondary_char_no={secondary_char_no})을 해석하지 못했습니다: {e}",
                        file=sys.stderr,
                    )
                    sys.exit(1)

        plan.append({
            "row": row,
            "title": title,
            "seed": seed,
            "batch_size": batch_size,
            "char_no": char_no,
            "depth_path": depth_path,
            "secondary_path": secondary_path,
        })

    return plan


def run_once(base_workflow, comfy_url, row, title, seed, char_no, depth_path, index, job_id, output_dir,
             secondary_kind=None, secondary_path=None):
    workflow = copy.deepcopy(base_workflow)
    apply_prompts(workflow, row)
    apply_seed(workflow, seed)
    apply_checkpoint(workflow)
    apply_lora(workflow)
    apply_batch_size(workflow, row.get("batch_no"))
    apply_resolution(workflow, row.get("width"), row.get("height"), row.get("resolution"))

    if depth_path is not None:
        apply_depth_image(workflow, comfy_url, depth_path)
    else:
        disable_controlnet(workflow)

    if secondary_path is not None:
        apply_secondary_reference(workflow, comfy_url, secondary_kind, secondary_path)

    apply_filename_prefix(workflow, title, index, seed, char_no, depth_path)

    prompt_id = queue_prompt(comfy_url, workflow)
    depth_label = f"{depth_path.name}(char_no={char_no})" if depth_path is not None else "(없음, ControlNet 비활성화)"
    secondary_log = f" secondary({secondary_kind})={secondary_path.name}" if secondary_path is not None else ""
    print(f"[depth_csv_batch] [{index}] title={title!r} seed={seed} depth={depth_label}{secondary_log} 큐 등록 (prompt_id={prompt_id})")
    wait_for_completion(comfy_url, prompt_id)
    print(f"[depth_csv_batch] [{index}] 완료")

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "job_id": job_id,
        "index": index,
        "char_no": char_no,
        "depth_set": depth_path.parent.name if depth_path is not None else None,
        "depth_file": depth_path.name if depth_path is not None else None,
        "seed": seed,
        "prompt_id": prompt_id,
    }
    if secondary_path is not None:
        record["secondary_kind"] = secondary_kind
        record["secondary_set"] = secondary_path.parent.name
        record["secondary_file"] = secondary_path.name
    append_manifest(output_dir, record)


def main():
    workflow_path = env("WORKFLOW_PATH")
    csv_path = env("CSV_PATH")
    if not workflow_path or not csv_path:
        print("[depth_csv_batch] WORKFLOW_PATH와 CSV_PATH 환경변수가 모두 필요합니다.", file=sys.stderr)
        sys.exit(1)

    base_workflow = load_workflow(workflow_path)
    rows = load_rows(csv_path)
    if not rows:
        print("[depth_csv_batch] CSV에 처리할 행이 없습니다.")
        return

    depth_mode = env("DEPTH_MODE", "sequential")
    depth_dir = ref_kind_dir("depth")
    output_dir = env("NIGHTSHIFT_OUTPUT_DIR", "/workspace/output")
    comfy_url = env("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
    job_id = env("JOB_ID")
    nightshift_url = env("NIGHTSHIFT_URL", "http://127.0.0.1:8000")

    secondary_kind = env("SECONDARY_KIND", "none")
    secondary_dir = None
    if secondary_kind not in REF_KINDS:
        if secondary_kind != "none":
            print(f"[depth_csv_batch] 경고: 알 수 없는 SECONDARY_KIND '{secondary_kind}' — 보조 참조를 끕니다.", file=sys.stderr)
        secondary_kind = None
    else:
        secondary_dir = ref_kind_dir(secondary_kind)

    plan = build_plan(base_workflow, rows, depth_dir, depth_mode, secondary_kind, secondary_dir)
    if not plan:
        print("[depth_csv_batch] 제출할 행이 없습니다 (모두 건너뜀).")
        return

    total_images = sum(item["batch_size"] for item in plan)
    print(f"[depth_csv_batch] 총 {len(plan)}건 제출 예정, 이미지 {total_images}장 예상")
    if secondary_kind:
        print(f"[depth_csv_batch] 보조 참조 사용 가능: 종류={secondary_kind}")
    report_progress(job_id, nightshift_url, total_images, 0)

    done_images = 0
    for index, item in enumerate(plan, start=1):
        run_once(
            base_workflow, comfy_url, item["row"], item["title"], item["seed"],
            item["char_no"], item["depth_path"], index, job_id, output_dir,
            secondary_kind=secondary_kind, secondary_path=item["secondary_path"],
        )
        done_images += item["batch_size"]
        report_progress(job_id, nightshift_url, total_images, done_images)

    print(f"[depth_csv_batch] 총 {len(plan)}건 완료 (이미지 {done_images}장)")


if __name__ == "__main__":
    main()
