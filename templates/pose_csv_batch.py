"""
CSV + 포즈 배치 템플릿 — csv_batch.py의 CSV 기반 배치 제출(프롬프트 여러 개, 시드,
해상도, 배치수)에 pose_batch.py의 포즈 레퍼런스 주입을 결합한다. CSV 행마다 pose
컬럼으로 ControlNet에 쓸 포즈 레퍼런스 이미지를 지정할 수 있고, 비어 있으면 그
행은 ControlNet 없이(비활성화) 생성한다.

주의: csv_batch.py, pose_batch.py와 마찬가지로 이 저장소의 템플릿은 서로 임포트하지
않고 필요한 로직을 그대로 복사해서 자기 완결적으로 작성하는 게 컨벤션이다. 특히 아래
"포즈 참조 해석" 섹션(PoseSetRef/PoseFileRef/resolve_pose_reference 등)은
pose_assets.py의 동명 함수와 완전히 같은 알고리즘이어야 하는 단일 소스를 복사해온
것이다 — pose_assets.py의 resolve_pose_reference를 고치면 여기도 반드시 같이
고쳐야 한다(app.py는 업로드 시점에 pose_assets.py 쪽을 직접 호출해서 검증하고,
이 스크립트는 실행 시점에 이 사본으로 다시 검증한다 — 둘이 다르게 판단하면 안 됨).

CSV 컬럼 (csv_batch.py와 동일 + pose):
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
    pose             이 행에 쓸 포즈 레퍼런스. 비어 있으면 이 행은 ControlNet을
                     비활성화(strength=0)한 채로 생성하며, 이때 char_no는 의미가
                     없으므로 무시된다. 값이 있으면 아래 "포즈 참조 해석" 규칙으로
                     char_no가 가리키는 폴더 안에서 해석한다.
    char_no          이 행이 몇 인물용 포즈 참조인지(예: "1"=solo, "2"=duo).
                     비어 있으면 "1"로 취급한다. pose가 비어 있으면 이 컬럼은
                     안 읽는다. 정수로 안 바뀌면 pose 해석 실패와 같은 수준으로
                     취급해 배치 전체를 에러로 중단한다(아래 참고).

포즈 레퍼런스 폴더 구조(char_no로 스코프됨):
    NIGHTSHIFT_POSES_DIR/
        1/                  1인물(solo) 세트들
            <세트>/<파일>...
        2/                  2인물(duo) 세트들 — 1과 완전히 분리된 별개의 이름공간
            <세트>/<파일>...
    char_no가 다르면 같은 이름의 세트(예: "1/battle"과 "2/battle")가 있어도
    서로 완전히 다른 폴더로 취급된다 — CSV 작성자가 duo용 세트를 쓰려다 실수로
    solo용 세트를 섞어 넣는 사고를, 폴더 구조 자체로 막는 게 이 스코프의
    목적이다(기능 확장이 아니라 오사용 방지). 이 범위는 "포즈 스켈레톤 이미지
    1장에 여러 인물이 이미 함께 그려져 있어 ControlNet 노드 1개로 그대로 처리
    가능한 경우"로 한정하며, 캐릭터별로 별도 ControlNet을 붙이는 멀티
    ControlNet 구조는 다루지 않는다.

포즈 참조 해석(pose 컬럼 값, resolve_pose_reference(poses_dir, char_no, name)):
    1. "/"가 있으면 "<세트>/<파일명>" 형식의, char_no 폴더 안에서의 정확한
       경로로 취급한다.
    2. "/"가 없고 NIGHTSHIFT_POSES_DIR/char_no 아래 세트 폴더 이름과 정확히
       일치하면 "세트 지정"으로 취급한다 — 그 세트 전용 피커(POSE_MODE에 따라
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

    pose/char_no 값 해석은 잡을 큐에 올리는 시점(app.py 업로드 검증)에 CSV
    전체를 미리 한 번 돌려 확인하지만, 그 사이 포즈 폴더 내용이 바뀌었을 수
    있으므로 이 스크립트도 실행 시작 시 전체 CSV에 대해 해석을 다시 수행한다
    (제출을 시작하기 전에 — 일부만 제출된 채 실패하는 일이 없도록). 한 행이라도
    해석에 실패하면 그 행만 건너뛰지 않고 배치 전체를 에러로 중단한다.

환경변수:
    WORKFLOW_PATH      (필수) ComfyUI API 형식 workflow json 경로 (nightshift가 주입)
    CSV_PATH           (필수) 위 컬럼을 가진 csv 경로 (nightshift가 주입)
    POSE_MODE          "sequential" 또는 "random" (nightshift가 템플릿 옵션 "pose_mode"로
                       주입, 기본 sequential) — pose 컬럼에 세트 이름만 지정한 행에 적용
    NIGHTSHIFT_POSES_DIR   포즈 세트들이 있는 상위 폴더 (기본 /workspace/dataset/poses).
                       nightshift 서버(pose_assets.py)와 같은 값을 봐야 하므로 손대지 않는 게 안전함
    NIGHTSHIFT_OUTPUT_DIR  ComfyUI가 이미지를 저장하는 폴더 (기본 /workspace/output).
                       재현성 기록용 jsonl(pose_csv_batch_manifest.jsonl)을 여기 같이 남긴다
    COMFY_URL          ComfyUI 서버 주소 (기본 http://127.0.0.1:8188)
    JOB_ID             nightshift가 주입하는 이 작업의 id (진행 상황 보고용, 없으면 보고 생략)
    NIGHTSHIFT_URL     nightshift 자신의 주소 (진행 상황 보고용, 기본 http://127.0.0.1:8000)
    SEED_NODE_TITLE    시드를 주입할 노드의 _meta.title 부분일치 (기본 "KSampler")
    LATENT_NODE_TITLE  해상도/배치수를 주입할 노드의 _meta.title 부분일치 (기본 "latent")
    SAVE_NODE_TITLE    파일명 접두사를 주입할 노드의 _meta.title 부분일치 (기본 "Save")
    POSE_NODE_TITLE    포즈 이미지를 주입할 LoadImage 노드의 _meta.title 부분일치
                       (기본 "Load" — 일치하는 노드가 없으면 워크플로우에 LoadImage가
                       하나뿐일 때 그 노드를 대신 쓴다)
    CONTROLNET_NODE_TITLE  pose가 비어 있을 때 비활성화(strength=0)할 노드의
                       _meta.title 부분일치 (기본 "ControlNet" — 마찬가지로 일치하는
                       노드가 없으면 ControlNetApplyAdvanced/ControlNetApply 노드가
                       하나뿐일 때 그 노드를 대신 쓴다)
    POLL_INTERVAL_SEC  히스토리 폴링 간격 초 (기본 2)
    POLL_TIMEOUT_SEC   개별 작업 완료 대기 제한 초 (기본 600)

ComfyUI로의 포즈 이미지 주입 방식:
    pose_batch.py와 동일 — LoadImage가 참조하는 파일은 ComfyUI 자신의 input
    폴더에 있어야 하므로, 파일시스템 공유 여부와 무관하게 항상 동작하도록 매번
    ComfyUI의 POST /upload/image API로 업로드하고 응답받은 파일명을 주입한다.

ControlNet 비활성화(pose가 비어 있는 행):
    그래프를 재배선하지 않고, apply_seed와 같은 패턴으로 ControlNetApplyAdvanced류
    노드는 그대로 둔 채 strength 입력값만 0으로 덮어써서 사실상 꺼진 것과 같은
    효과를 낸다.

진행 상황 보고:
    csv_batch.py와 같은 방식(총 예상 이미지 수를 미리 계산해 PUT
    /api/jobs/{job_id}/progress로 보고)이되, 행마다 시드 하나 = 반복 한 번이라
    별도의 "케이스당 시드 개수" 개념이 없다 — 유효한 행 수가 곧 반복 횟수다(단
    batch_no로 한 행에서 여러 장을 만들면 그만큼 이미지 수는 늘어남).

재현성 기록:
    pose_batch.py와 같은 형식으로 NIGHTSHIFT_OUTPUT_DIR에
    pose_csv_batch_manifest.jsonl을 이어쓰기(append)한다. pose가 비어 있던 행은
    char_no/pose_set/pose_file을 모두 null로 남겨 "의도적으로 ControlNet 없이
    생성했다"는 걸 구분한다. char_no가 기본값(1)이 아니면 SaveImage의
    filename_prefix에도 "_char<N>"이 붙는다(결과물을 인물 수 기준으로 정리할 때 씀).
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
# 포즈 참조 해석 — pose_assets.py의 resolve_pose_reference와 완전히 같은 알고리즘의
# 사본. pose_assets.py 쪽을 고치면 이 블록도 반드시 같이 고칠 것.
# ============================================================================

POSE_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

# pose_assets.py의 DEFAULT_CHAR_NO 사본 — char_no 컬럼이 비어 있을 때(1인물/solo).
DEFAULT_CHAR_NO = "1"


class PoseReferenceError(Exception):
    pass


class PoseReferenceNotFoundError(PoseReferenceError):
    pass


class PoseReferenceAmbiguousError(PoseReferenceError):
    pass


@dataclass
class PoseSetRef:
    set_name: str


@dataclass
class PoseFileRef:
    set_name: str
    path: Path


def _char_dir(poses_dir, char_no):
    return Path(poses_dir) / str(char_no)


def _pose_set_dir(poses_dir, char_no, name):
    return _char_dir(poses_dir, char_no) / name


def list_pose_images_in_set(poses_dir, char_no, name):
    d = _pose_set_dir(poses_dir, char_no, name)
    if not d.is_dir():
        return []
    return sorted(
        p for p in d.iterdir()
        if p.is_file() and p.suffix.lower() in POSE_IMAGE_EXTENSIONS
    )


def parse_char_no(raw):
    """char_no 컬럼 값을 해석한다. 빈 값이면 기본값(1인물/solo). 정수로 바뀌지
    않으면 ValueError — pose 해석 실패와 같은 심각도로 다뤄서 그 행만 건너뛰지
    않고 배치 전체를 에러로 중단시킨다(build_plan 참고)."""
    raw = (raw or "").strip()
    if not raw:
        return DEFAULT_CHAR_NO
    return str(int(raw))


def resolve_pose_reference(poses_dir, char_no, name):
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
        path = _pose_set_dir(poses_dir, char_no, set_name) / filename
        if not path.is_file() or path.suffix.lower() not in POSE_IMAGE_EXTENSIONS:
            raise PoseReferenceNotFoundError(f"'{name}' 파일을 찾을 수 없어요 (char_no={char_no}).")
        return PoseFileRef(set_name=set_name, path=path)

    if _pose_set_dir(poses_dir, char_no, name).is_dir():
        if not list_pose_images_in_set(poses_dir, char_no, name):
            raise PoseReferenceNotFoundError(f"'{name}' 포즈 세트에 이미지가 하나도 없어요 (char_no={char_no}).")
        return PoseSetRef(set_name=name)

    matches = []
    base = _char_dir(poses_dir, char_no)
    if base.is_dir():
        for entry in sorted(base.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            candidate = entry / name
            if candidate.is_file() and candidate.suffix.lower() in POSE_IMAGE_EXTENSIONS:
                matches.append(entry.name)

    if len(matches) == 1:
        return PoseFileRef(set_name=matches[0], path=_pose_set_dir(poses_dir, char_no, matches[0]) / name)
    if len(matches) > 1:
        raise PoseReferenceAmbiguousError(
            f"'{name}'가 여러 세트({', '.join(matches)})에 있어요. "
            f"'<세트>/{name}' 형식으로 명시해주세요."
        )
    raise PoseReferenceNotFoundError(f"'{name}'를 세트 이름으로도 파일명으로도 찾지 못했어요 (char_no={char_no}).")


# 포즈 파일 선택 전략 — pose_batch.py의 사본 -------------------------------

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
# csv_batch.py / pose_batch.py 공통 헬퍼 — 사본
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
    return text[:40] or "pose"


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
                f"[pose_csv_batch] 경고: '{field}' 값을 넣을 노드를 찾지 못했습니다 "
                f"(제목에 '{title_substring}'가 포함된 CLIPTextEncode/Primitive 텍스트 노드 없음)",
                file=sys.stderr,
            )
            continue
        node.setdefault("inputs", {})[input_field] = value
        applied = True
    if not applied:
        print("[pose_csv_batch] 경고: 이 행에 프롬프트 컬럼 값이 하나도 없습니다.", file=sys.stderr)


def apply_seed(workflow, seed):
    node_id, node = find_node(
        workflow,
        title_substring=env("SEED_NODE_TITLE", "KSampler"),
        class_types=("KSampler", "KSamplerAdvanced"),
    )
    if node is None:
        print("[pose_csv_batch] 경고: 시드를 넣을 노드를 찾지 못했습니다 (KSampler 없음)", file=sys.stderr)
        return
    try:
        node.setdefault("inputs", {})["seed"] = int(seed)
    except (TypeError, ValueError):
        print(f"[pose_csv_batch] 경고: seed 값 '{seed}'을 정수로 변환하지 못했습니다", file=sys.stderr)


def parse_batch_size(batch_no):
    batch_no = (batch_no or "").strip()
    if not batch_no:
        return None
    try:
        value = int(batch_no)
    except ValueError:
        print(f"[pose_csv_batch] 경고: batch_no 값 '{batch_no}'을 정수로 변환하지 못했습니다", file=sys.stderr)
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
        print("[pose_csv_batch] 경고: batch_no를 넣을 노드를 찾지 못했습니다 (EmptyLatentImage 없음)", file=sys.stderr)
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
                f"[pose_csv_batch] 경고: width/height 값 '{width}x{height}'을 정수로 변환하지 못했습니다. "
                "resolution 컬럼으로 대체합니다.",
                file=sys.stderr,
            )
    elif width or height:
        print(
            "[pose_csv_batch] 경고: width/height는 둘 다 채워야 적용됩니다 (하나만 비어 있음). "
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
            f"[pose_csv_batch] 경고: resolution 값 '{resolution}'을 해석하지 못했습니다 "
            f"(WxH 형식이거나 {list(RESOLUTION_PRESETS)} 중 하나여야 함)",
            file=sys.stderr,
        )
        return None
    return int(match.group(1)), int(match.group(2))


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
        print("[pose_csv_batch] 경고: 해상도를 넣을 노드를 찾지 못했습니다 (EmptyLatentImage 없음)", file=sys.stderr)
        return
    width_value, height_value = resolved
    set_linked_value(workflow, node, "width", width_value)
    set_linked_value(workflow, node, "height", height_value)


def apply_filename_prefix(workflow, title, index, seed, char_no, pose_path):
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
    if pose_path is not None and char_no is not None and char_no != DEFAULT_CHAR_NO:
        prefix = f"{prefix}_char{char_no}"
    if pose_path is not None:
        prefix = f"{prefix}_{sanitize_stem(pose_path.stem)}"
    node.setdefault("inputs", {})["filename_prefix"] = prefix


def report_progress(job_id, nightshift_url, total, done):
    if not job_id or not nightshift_url:
        return
    try:
        payload = json.dumps({"total": total, "done": done}).encode("utf-8")
        req = urllib.request.Request(
            f"{nightshift_url}/api/jobs/{job_id}/progress",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"[pose_csv_batch] 경고: 진행 상황 보고 실패: {e}", file=sys.stderr)


def append_manifest(output_dir, record):
    try:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        path = Path(output_dir) / "pose_csv_batch_manifest.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[pose_csv_batch] 경고: 재현성 기록 실패: {e}", file=sys.stderr)


# ============================================================================
# ComfyUI 연동 — pose_batch.py의 사본
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


def apply_pose_image(workflow, comfy_url, pose_path):
    node_id, node = find_node(
        workflow,
        title_substring=env("POSE_NODE_TITLE", "Load"),
        class_types=("LoadImage",),
        prefer_connected=True,
    )
    if node is None:
        print("[pose_csv_batch] 경고: 포즈 이미지를 넣을 노드를 찾지 못했습니다 (LoadImage 없음)", file=sys.stderr)
        return
    uploaded_name = upload_image_to_comfy(comfy_url, pose_path)
    node.setdefault("inputs", {})["image"] = uploaded_name


def disable_controlnet(workflow):
    """pose 컬럼이 비어 있는 행 — 그래프를 재배선하지 않고 ControlNetApplyAdvanced류
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
            "[pose_csv_batch] 경고: ControlNet 노드를 찾지 못해 비활성화를 건너뜁니다 "
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

def build_plan(base_workflow, rows, poses_dir, pose_mode):
    """CSV 행마다 (row, title, seed, batch_size, char_no, pose_path)를 미리
    계산한다. pose/char_no 컬럼 해석은 여기서 전부 끝내둔다 — 세트 지정 행은
    이 단계에서 피커를 소비해 실제 파일을 정하고, 해석에 실패하는 행이
    하나라도 있으면 그 자리에서 배치 전체를 중단한다(건너뛰지 않음).

    피커는 (char_no, set_name) 조합별로 하나씩 lazy하게 만든다 — char_no가
    다르면 같은 이름의 세트라도 완전히 다른 폴더(다른 파일 목록)이므로, set_name만
    으로 캐시하면 서로 다른 세트가 피커 하나를 잘못 공유하게 된다."""
    default_batch_size = get_default_batch_size(base_workflow)
    pickers = {}

    def get_picker(char_no, set_name):
        key = (char_no, set_name)
        if key not in pickers:
            files = list_pose_images_in_set(poses_dir, char_no, set_name)
            if not files:
                raise PoseReferenceNotFoundError(f"'{set_name}' 포즈 세트에 이미지가 없습니다 (char_no={char_no}).")
            pickers[key] = make_picker(pose_mode, files)
        return pickers[key]

    plan = []
    for line_no, row in enumerate(rows, start=2):  # 헤더가 1번 줄
        title = (row.get("title") or row.get("name") or "").strip()
        main_prompt = (row.get("main_prompt") or row.get("prompt") or "").strip()
        if not main_prompt:
            print(f"[pose_csv_batch] 건너뜀 (main_prompt/prompt 없음, {line_no}번째 줄): {row}")
            continue

        batch_size = parse_batch_size(row.get("batch_no"))
        if batch_size is None:
            batch_size = default_batch_size

        row_seed = (row.get("seed") or "").strip()
        seed = row_seed if row_seed else random.randint(0, 2**31 - 1)

        pose_raw = (row.get("pose") or "").strip()
        char_no = None
        pose_path = None
        if pose_raw:
            try:
                char_no = parse_char_no(row.get("char_no"))
            except ValueError:
                print(
                    f"[pose_csv_batch] 오류: {line_no}번째 줄의 char_no 값 "
                    f"'{row.get('char_no')}'을 정수로 변환하지 못했습니다.",
                    file=sys.stderr,
                )
                sys.exit(1)
            try:
                ref = resolve_pose_reference(poses_dir, char_no, pose_raw)
                if isinstance(ref, PoseFileRef):
                    pose_path = ref.path
                else:
                    pose_path = get_picker(char_no, ref.set_name).pick()
            except PoseReferenceError as e:
                print(
                    f"[pose_csv_batch] 오류: {line_no}번째 줄의 pose 값 '{pose_raw}' "
                    f"(char_no={char_no})을 해석하지 못했습니다: {e}",
                    file=sys.stderr,
                )
                sys.exit(1)

        plan.append({
            "row": row,
            "title": title,
            "seed": seed,
            "batch_size": batch_size,
            "char_no": char_no,
            "pose_path": pose_path,
        })

    return plan


def run_once(base_workflow, comfy_url, row, title, seed, char_no, pose_path, index, job_id, output_dir):
    workflow = copy.deepcopy(base_workflow)
    apply_prompts(workflow, row)
    apply_seed(workflow, seed)
    apply_batch_size(workflow, row.get("batch_no"))
    apply_resolution(workflow, row.get("width"), row.get("height"), row.get("resolution"))

    if pose_path is not None:
        apply_pose_image(workflow, comfy_url, pose_path)
    else:
        disable_controlnet(workflow)

    apply_filename_prefix(workflow, title, index, seed, char_no, pose_path)

    prompt_id = queue_prompt(comfy_url, workflow)
    pose_label = f"{pose_path.name}(char_no={char_no})" if pose_path is not None else "(없음, ControlNet 비활성화)"
    print(f"[pose_csv_batch] [{index}] title={title!r} seed={seed} pose={pose_label} 큐 등록 (prompt_id={prompt_id})")
    wait_for_completion(comfy_url, prompt_id)
    print(f"[pose_csv_batch] [{index}] 완료")

    append_manifest(output_dir, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "job_id": job_id,
        "index": index,
        "char_no": char_no,
        "pose_set": pose_path.parent.name if pose_path is not None else None,
        "pose_file": pose_path.name if pose_path is not None else None,
        "seed": seed,
        "prompt_id": prompt_id,
    })


def main():
    workflow_path = env("WORKFLOW_PATH")
    csv_path = env("CSV_PATH")
    if not workflow_path or not csv_path:
        print("[pose_csv_batch] WORKFLOW_PATH와 CSV_PATH 환경변수가 모두 필요합니다.", file=sys.stderr)
        sys.exit(1)

    base_workflow = load_workflow(workflow_path)
    rows = load_rows(csv_path)
    if not rows:
        print("[pose_csv_batch] CSV에 처리할 행이 없습니다.")
        return

    pose_mode = env("POSE_MODE", "sequential")
    poses_dir = env("NIGHTSHIFT_POSES_DIR", "/workspace/dataset/poses")
    output_dir = env("NIGHTSHIFT_OUTPUT_DIR", "/workspace/output")
    comfy_url = env("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
    job_id = env("JOB_ID")
    nightshift_url = env("NIGHTSHIFT_URL", "http://127.0.0.1:8000")

    plan = build_plan(base_workflow, rows, poses_dir, pose_mode)
    if not plan:
        print("[pose_csv_batch] 제출할 행이 없습니다 (모두 건너뜀).")
        return

    total_images = sum(item["batch_size"] for item in plan)
    print(f"[pose_csv_batch] 총 {len(plan)}건 제출 예정, 이미지 {total_images}장 예상")
    report_progress(job_id, nightshift_url, total_images, 0)

    done_images = 0
    for index, item in enumerate(plan, start=1):
        run_once(
            base_workflow, comfy_url, item["row"], item["title"], item["seed"],
            item["char_no"], item["pose_path"], index, job_id, output_dir,
        )
        done_images += item["batch_size"]
        report_progress(job_id, nightshift_url, total_images, done_images)

    print(f"[pose_csv_batch] 총 {len(plan)}건 완료 (이미지 {done_images}장)")


if __name__ == "__main__":
    main()
