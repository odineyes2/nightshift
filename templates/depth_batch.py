"""
depth 참조 배치 템플릿 — ControlNet(Depth 등)에 쓸 depth 레퍼런스 이미지를 서버에
미리 쌓아둔 폴더(depth 세트)에서 순차 또는 랜덤으로 뽑아 LoadImage 노드에 주입하면서,
워크플로우 하나를 depth_count번 반복 실행한다. pose_batch.py/seed_batch.py와 같은
ComfyUI 연동 방식(워크플로우 노드 찾기/제출/폴링/진행률 보고)을 쓰되, 매 반복마다
시드뿐 아니라 depth 레퍼런스 이미지도 함께 바꾼다. pose_batch.py와 완전히 동일한
구조를 갖는 독립 스크립트다(templates/ 아래 스크립트들은 서로 import하지 않고 각자
알고리즘 사본을 갖는다는 이 저장소의 관례) — "주(main) 참조"만 depth로 고정돼 있다.

depth 레퍼런스 폴더 구조(char_no로 스코프됨):
    NIGHTSHIFT_ASSETS_DIR(기본 /workspace/dataset/assets)/depth/
        1/                          인물 수 1(solo)
            <DEPTH_SET 이름>/
                image1.png
                image2.jpg
                ...
        2/                          인물 수 2(duo)
            ...
    (레거시/개별 override: NIGHTSHIFT_DEPTH_DIR을 설정하면 위 경로 대신 그 값을
    depth 종류의 루트로 쓴다.)
    nightshift 웹 UI에서 업로드할 때 "인물 수"(char_no) 드롭다운을 먼저 고르고,
    그 값에 따라 "depth 세트" 드롭다운이 그 폴더의 <char_no> 아래 하위 폴더
    목록으로 다시 채워지는 캐스케이딩 방식이다(GET /api/assets?kind=depth가 char_no별
    세트 목록을 트리로 돌려준다). 고른 인물 수와 세트 이름이 각각 CHAR_NO,
    DEPTH_SET 환경변수로 전달된다. 여러 인물이 CSV 행마다 서로 다른 depth/char_no를
    쓰는 경우는 depth_csv_batch 템플릿을 쓴다. 세트 존재 여부/이미지 유무는
    업로드 시점에 nightshift(app.py + ref_assets.py)가 이미 검증했으므로 큐
    시작 이후 그것 때문에 실패하는 일은 없지만, 실행 중 폴더가 바뀌는 등의
    만일의 상황을 대비해 이 스크립트도 시작할 때 한 번 더 확인한다.

depth 선택 방식(DEPTH_MODE):
    sequential  파일명 정렬 순서대로 순환. depth_count가 이미지 수보다 많으면
                처음으로 되돌아가 반복한다.
    random      매번 무작위로 뽑되, 폴더가 다 소진될 때까지는 같은 이미지를 다시
                뽑지 않는다(완전 무작위보다 다양성이 보장됨). 다 뽑고 나면
                다시 전체를 섞어서 처음부터 뽑는다.

ComfyUI로의 이미지 주입 방식:
    LoadImage 노드가 참조하는 파일은 ComfyUI 자신의 input 폴더에 있어야 한다.
    nightshift와 ComfyUI가 파일시스템을 공유한다는 보장이 없으므로(예: ComfyUI가
    다른 컨테이너/파드로 분리돼 있을 수 있음) 파일을 직접 복사하는 대신, 매번
    ComfyUI의 POST /upload/image API로 업로드하고 응답으로 받은 파일명을 LoadImage
    노드의 image 입력에 그대로 넣는다(아래 upload_image_to_comfy). 이미지마다 HTTP
    업로드가 한 번씩 더 들어가 느리지만, 파일시스템 공유 여부와 무관하게 항상 동작한다.

메인 프롬프트(MAIN_PROMPT):
    비워두면(기본값) 업로드한 워크플로우 JSON에 이미 들어있는 프롬프트를 그대로
    쓴다. 값을 채우면 워크플로우에서 제목에 "main_prompt"가 포함된 노드 하나를
    찾아 그 값을 덮어쓴다 — CLIPTextEncode면 "text" 입력을, PrimitiveStringMultiline
    같은 Primitive 계열 텍스트 노드면 "value" 입력을 쓴다(어느 필드를 쓸지는
    primitive_value_field()가 class_type으로 판단한다). 그런 노드를 못 찾으면
    (제목이 다르거나 값 입력을 알 수 없는 노드면) 경고만 남기고 워크플로우는
    건드리지 않는다 — 트리거/퀄리티/네거티브처럼 다른 프롬프트 노드가 여러 개
    있을 수 있으므로, 제목이 정확히 일치하지 않으면 엉뚱한 노드를 덮어쓰지
    않기 위함이다(csv_batch.py의 프롬프트 노드 매칭과 같은 방식).

해상도(WIDTH/HEIGHT):
    seed_batch.py와 같은 방식이다 — 둘 다 채워야 적용되며(하나만 채우면 무시하고
    경고), 둘 다 비어 있으면 워크플로우에 이미 들어있는 값을 그대로 둔다.
    EmptyLatentImage류 노드의 width/height 입력이 리터럴이면 직접 덮어쓰고, 별도
    PrimitiveInt 노드로 링크돼 있으면 링크는 그대로 두고 연결된 노드의 값을
    갱신한다(set_linked_value 참고).

환경변수:
    WORKFLOW_PATH      (필수) ComfyUI API 형식 workflow json 경로 (nightshift가 주입)
    DEPTH_COUNT        (필수) 반복 생성할 이미지 개수 (nightshift가 템플릿 옵션 "depth_count"로 주입)
    DEPTH_SET          (필수) depth 세트 폴더 이름 (nightshift가 템플릿 옵션 "depth_set"으로 주입)
    CHAR_NO            인물 수(depth 세트가 있는 폴더 하위 숫자 폴더 이름). nightshift가
                       템플릿 옵션 "char_no"로 주입, 기본 "1"(1인물/solo)
    DEPTH_MODE         "sequential" 또는 "random" (nightshift가 템플릿 옵션 "depth_mode"로 주입, 기본 sequential)
    MAIN_PROMPT        메인 프롬프트 (nightshift가 템플릿 옵션 "main_prompt"로 주입, 기본
                       빈 값 — 비워두면 워크플로우의 프롬프트를 그대로 씀)
    WIDTH, HEIGHT      해상도 (nightshift가 템플릿 옵션 "width"/"height"로 주입, 기본 빈 값
                       — 둘 다 비워두면 워크플로우의 값을 그대로 씀)
    SECONDARY_KIND      "none"(기본, 꺼짐)/"pose"/"depth"/"lineart" — 보조 참조 종류
                       (nightshift가 템플릿 옵션 "secondary_kind"로 주입)
    SECONDARY_CHAR_NO   보조 참조 세트의 인물 수 (nightshift가 템플릿 옵션
                       "secondary_char_no"로 주입, 기본 "1")
    SECONDARY_SET       보조 참조 세트 폴더 이름 (nightshift가 템플릿 옵션
                       "secondary_set"으로 주입, SECONDARY_KIND가 "none"이면 무시됨)
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
                       재현성 기록용 jsonl(depth_batch_manifest.jsonl)을 여기 같이 남긴다
    COMFY_URL          ComfyUI 서버 주소 (기본 http://127.0.0.1:8188)
    JOB_ID             nightshift가 주입하는 이 작업의 id (진행 상황 보고용, 없으면 보고 생략)
    NIGHTSHIFT_URL     nightshift 자신의 주소 (진행 상황 보고용, 기본 http://127.0.0.1:8000)
    SEED_NODE_TITLE    시드를 주입할 노드의 _meta.title 부분일치 (기본 "KSampler")
    LATENT_NODE_TITLE  해상도를 주입할 노드의 _meta.title 부분일치 (기본 "latent")
    DEPTH_NODE_TITLE   depth 이미지를 주입할 LoadImage 노드의 _meta.title 부분일치
                       (기본 "Load" — 정확히 일치하는 노드가 없으면, 워크플로우에
                       LoadImage 노드가 하나뿐일 때 그 노드를 대신 쓴다. 여러 개인
                       워크플로우라면 이 값을 실제 노드 제목에 맞게 조정해야 한다)
    SAVE_NODE_TITLE    파일명 접두사를 주입할 노드의 _meta.title 부분일치 (기본 "Save")
    POLL_INTERVAL_SEC  히스토리 폴링 간격 초 (기본 2)
    POLL_TIMEOUT_SEC   개별 작업 완료 대기 제한 초 (기본 600)

진행 상황 보고:
    depth_count를 그대로 예상 총 이미지 수로 보고 PUT /api/jobs/{job_id}/progress로
    {"total": N, "done": M}을 보고한다. seed_batch.py와 달리 EmptyLatentImage의
    batch_size는 반영하지 않는다(이 배치는 보통 한 번에 1장씩 생성하는 용도라
    단순하게 유지) — batch_size를 키워 쓰는 워크플로우라면 총 개수가 실제 이미지
    수보다 적게 잡힐 수 있다.

재현성 기록:
    매 반복마다 어떤 depth 이미지를 썼는지 stdout 로그에 남기고, SaveImage의
    filename_prefix에도 depth 파일명(확장자 제외, 안전한 문자로 치환)을 포함시킨다
    (예: depth_batch_3_seed482913_room_01.png). char_no가 기본값(1)이 아니면
    "_char<N>"도 덧붙인다(예: depth_batch_3_seed482913_room_01_char2.png —
    결과물을 인물 수 기준으로 정리할 때 씀). JOB_ID가 있으면 이 filename_prefix
    앞에 "<JOB_ID>/"를 붙여 ComfyUI가 출력 폴더 밑에 그 job_id 폴더를 만들고 그
    안에 저장하게 한다 — nightshift 갤러리의 "작업별 보기"가 이 폴더 이름으로
    묶어서 보여준다(output_images.py 참고). 추가로 NIGHTSHIFT_OUTPUT_DIR에
    depth_batch_manifest.jsonl을 이어쓰기(append)로 남겨, 한 줄마다
    {timestamp, job_id, index, char_no, depth_set, depth_file, seed, prompt_id,
    (보조 참조를 썼다면) secondary_kind, secondary_set, secondary_file}을
    기록한다 — 나중에 "이 컷이 왜 이렇게 나왔는지" 추적할 때 쓴다. 기록에
    실패해도(출력 폴더가 아직 없는 등) 배치 자체는 계속 진행한다.

보조 참조(SECONDARY_KIND 등) — depth와 pose/lineart를 한 생성에 같이 쓰기:
    이 템플릿의 "주(main) 참조"는 항상 depth다(위 DEPTH_SET/CHAR_NO). 그와 별개로,
    같은 생성에 pose나 lineart 레퍼런스를 "보조 참조"로 하나 더 얹을 수 있다 —
    예를 들어 워크플로우에 ControlNet(Depth)과 ControlNet(OpenPose)을 함께 쓰는
    노드 두 쌍이 있을 때, depth는 주 참조로, 포즈는 보조 참조로 넣는 식이다.
    SECONDARY_KIND가 "none"(기본값)이면 보조 참조 기능 자체가 꺼진다. 다른
    값이면 NIGHTSHIFT_ASSETS_DIR(또는 해당 종류의 개별 override) 아래에서
    SECONDARY_CHAR_NO/SECONDARY_SET에 해당하는 세트를 똑같은 방식(DEPTH_MODE에
    따라 순차/랜덤)으로 골라, 워크플로우에서 제목에 SECONDARY_NODE_TITLE(기본
    "secondary")이 포함된 LoadImage 노드를 찾아 주입한다. 주 참조(depth)와 달리
    이 노드는 "정확히 제목이 일치해야만" 쓴다 — class_type만으로 아무 LoadImage에나
    대신 꽂아버리면 워크플로우에 LoadImage가 하나뿐인 경우 주 참조 노드를 실수로
    덮어쓸 수 있기 때문이다. 그래서 보조 참조는 항상 best-effort다: 세트 안에
    이미지가 없거나, 워크플로우에 맞는 제목의 LoadImage 노드가 없으면 경고만
    남기고 그 실행은 보조 참조 없이 계속 진행한다(업로드 시점에 nightshift가
    CSV/세트 존재 여부는 검증하지만, 워크플로우 노드 존재 여부까지는 검증하지
    않는다 — MAIN_PROMPT 노드 매칭과 같은 정책).
"""

import copy
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
from datetime import datetime, timezone
from pathlib import Path

REF_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

# ref_assets.py의 DEFAULT_CHAR_NO 사본 — CHAR_NO 환경변수가 비어 있을 때(1인물/solo).
DEFAULT_CHAR_NO = "1"

# ref_assets.py의 REF_KINDS/_KIND_ENV_OVERRIDE 사본 — 템플릿은 서버 모듈을 import하지
# 않는 관례(모듈 docstring 참고)라 여기 그대로 복제해둔다. 보조 참조 종류를 어떤 폴더
# 아래에서 찾을지 계산하는 데 쓴다.
REF_KINDS = ("pose", "depth", "lineart")
REF_KIND_ENV_OVERRIDE = {
    "pose": "NIGHTSHIFT_POSES_DIR",
    "depth": "NIGHTSHIFT_DEPTH_DIR",
    "lineart": "NIGHTSHIFT_LINEART_DIR",
}


def env(name, default=None):
    return os.environ.get(name, default)


def load_workflow(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def connected_node_ids(workflow):
    """다른 노드의 입력 링크로 실제 연결되어 있는(=출력이 쓰이고 있는) 노드 id 집합.
    csv_batch.py와 동일한 로직 — 같은 class_type의 노드가 여럿 남아있을 때(예: 다른
    워크플로우 실험의 흔적) 실제로 쓰이는 노드를 가려내는 데 쓴다."""
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


def sanitize_stem(text):
    text = re.sub(r"[^\w\-가-힣]+", "_", text or "")
    return text[:40] or "depth"


# depth 파일 선택 전략 ---------------------------------------------------------

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


def ref_kind_dir(kind):
    """kind("pose"/"depth"/"lineart")의 세트들이 있는 상위 폴더. 개별 override
    환경변수(NIGHTSHIFT_DEPTH_DIR 등)가 있으면 그걸, 없으면 NIGHTSHIFT_ASSETS_DIR/kind를
    쓴다 — ref_assets.py의 kind_dir()과 같은 규칙."""
    override = env(REF_KIND_ENV_OVERRIDE.get(kind, ""))
    if override:
        return override
    assets_dir = env("NIGHTSHIFT_ASSETS_DIR", "/workspace/dataset/assets")
    return str(Path(assets_dir) / kind)


def list_ref_images(base_dir, char_no, set_name):
    d = Path(base_dir) / str(char_no) / set_name
    if not d.is_dir():
        return []
    return sorted(
        p for p in d.iterdir()
        if p.is_file() and p.suffix.lower() in REF_IMAGE_EXTENSIONS
    )


# ComfyUI 연동 ----------------------------------------------------------------

def upload_image_to_comfy(comfy_url, image_path):
    """ComfyUI 자신의 input 폴더에 이미지를 올리고, LoadImage에서 참조할 파일명을
    돌려받는다. 모듈 docstring의 "ComfyUI로의 이미지 주입 방식" 참고."""
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


def apply_depth_image(workflow, comfy_url, depth_path):
    node_id, node = find_node(
        workflow,
        title_substring=env("DEPTH_NODE_TITLE", "Load"),
        class_types=("LoadImage",),
        prefer_connected=True,
    )
    if node is None:
        print("[depth_batch] 경고: depth 이미지를 넣을 노드를 찾지 못했습니다 (LoadImage 없음)", file=sys.stderr)
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
            f"[depth_batch] 안내: 보조 참조({secondary_kind}) 이미지를 넣을 노드(제목에 "
            f"'{title}' 포함)를 찾지 못해 이번 실행은 보조 참조 없이 진행합니다.",
            file=sys.stderr,
        )
        return
    if node.get("class_type") != "LoadImage":
        print(
            f"[depth_batch] 경고: SECONDARY_NODE_TITLE('{title}')로 찾은 노드가 LoadImage가 "
            "아니라 보조 참조를 건너뜁니다.",
            file=sys.stderr,
        )
        return
    uploaded_name = upload_image_to_comfy(comfy_url, ref_path)
    node.setdefault("inputs", {})["image"] = uploaded_name


def apply_seed(workflow, seed):
    node_id, node = find_node(
        workflow,
        title_substring=env("SEED_NODE_TITLE", "KSampler"),
        class_types=("KSampler", "KSamplerAdvanced"),
    )
    if node is None:
        print("[depth_batch] 경고: 시드를 넣을 노드를 찾지 못했습니다 (KSampler 없음)", file=sys.stderr)
        return
    node.setdefault("inputs", {})["seed"] = seed


def apply_main_prompt(workflow, main_prompt):
    main_prompt = (main_prompt or "").strip()
    if not main_prompt:
        # 비워두면 업로드된 워크플로우의 프롬프트를 그대로 둔다.
        return
    # title_substring만 넘기므로(find_node가 title 매칭 실패 시 아무 노드로도
    # 대체하지 않음) 제목에 "main_prompt"가 없으면 조용히 건너뛴다.
    node_id, node = find_node(workflow, title_substring="main_prompt")
    field = primitive_value_field(node) if node is not None else None
    if field is None:
        print(
            "[depth_batch] 경고: MAIN_PROMPT를 넣을 노드를 찾지 못했습니다 "
            "(제목에 'main_prompt'가 포함된 CLIPTextEncode/Primitive 텍스트 노드 없음)",
            file=sys.stderr,
        )
        return
    node.setdefault("inputs", {})[field] = main_prompt


def apply_resolution(workflow, width, height):
    width = (width or "").strip()
    height = (height or "").strip()
    if not width and not height:
        # 둘 다 비어 있으면 워크플로우에 이미 들어있는 해상도를 그대로 둔다.
        return
    if not width or not height:
        print(
            "[depth_batch] 경고: WIDTH/HEIGHT는 둘 다 채워야 적용됩니다 (하나만 비어 있음). 무시합니다.",
            file=sys.stderr,
        )
        return
    try:
        width_value = int(float(width))
        height_value = int(float(height))
    except ValueError:
        print(f"[depth_batch] 경고: WIDTH/HEIGHT 값 '{width}x{height}'을 정수로 변환하지 못했습니다.", file=sys.stderr)
        return

    node_id, node = find_node(
        workflow,
        title_substring=env("LATENT_NODE_TITLE", "latent"),
        class_types=("EmptyLatentImage",),
        prefer_connected=True,
    )
    if node is None:
        print("[depth_batch] 경고: 해상도를 넣을 노드를 찾지 못했습니다 (EmptyLatentImage 없음)", file=sys.stderr)
        return
    set_linked_value(workflow, node, "width", width_value)
    set_linked_value(workflow, node, "height", height_value)


def apply_filename_prefix(workflow, index, seed, char_no, depth_path):
    node_id, node = find_node(
        workflow,
        title_substring=env("SAVE_NODE_TITLE", "Save"),
        class_types=("SaveImage", "SaveImageWebsocket"),
    )
    if node is None:
        return
    depth_stem = sanitize_stem(depth_path.stem)
    prefix = f"depth_batch_{index}_seed{seed}_{depth_stem}"
    if char_no != DEFAULT_CHAR_NO:
        prefix = f"{prefix}_char{char_no}"
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
        req = urllib.request.Request(
            f"{nightshift_url}/api/jobs/{job_id}/progress",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"[depth_batch] 경고: 진행 상황 보고 실패: {e}", file=sys.stderr)


def append_manifest(output_dir, record):
    try:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        path = Path(output_dir) / "depth_batch_manifest.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[depth_batch] 경고: 재현성 기록 실패: {e}", file=sys.stderr)


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


def run_once(base_workflow, comfy_url, seed, char_no, depth_path, index, job_id, output_dir, main_prompt, width, height,
             secondary_kind=None, secondary_path=None):
    workflow = copy.deepcopy(base_workflow)
    apply_seed(workflow, seed)
    apply_main_prompt(workflow, main_prompt)
    apply_resolution(workflow, width, height)
    apply_depth_image(workflow, comfy_url, depth_path)
    if secondary_path is not None:
        apply_secondary_reference(workflow, comfy_url, secondary_kind, secondary_path)
    apply_filename_prefix(workflow, index, seed, char_no, depth_path)

    prompt_id = queue_prompt(comfy_url, workflow)
    secondary_log = f" secondary({secondary_kind})={secondary_path.name}" if secondary_path is not None else ""
    print(f"[depth_batch] [{index}] seed={seed} char_no={char_no} depth={depth_path.name}{secondary_log} 큐 등록 (prompt_id={prompt_id})")
    wait_for_completion(comfy_url, prompt_id)
    print(f"[depth_batch] [{index}] 완료")

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "job_id": job_id,
        "index": index,
        "char_no": char_no,
        "depth_set": depth_path.parent.name,
        "depth_file": depth_path.name,
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
    depth_count_raw = env("DEPTH_COUNT")
    depth_set = env("DEPTH_SET")
    if not workflow_path or not depth_count_raw or not depth_set:
        print("[depth_batch] WORKFLOW_PATH, DEPTH_COUNT, DEPTH_SET 환경변수가 모두 필요합니다.", file=sys.stderr)
        sys.exit(1)

    depth_count = int(depth_count_raw)
    depth_mode = env("DEPTH_MODE", "sequential")
    char_no = env("CHAR_NO", DEFAULT_CHAR_NO)
    main_prompt = env("MAIN_PROMPT", "")
    width = env("WIDTH", "")
    height = env("HEIGHT", "")
    depth_dir = ref_kind_dir("depth")
    output_dir = env("NIGHTSHIFT_OUTPUT_DIR", "/workspace/output")

    depth_files = list_ref_images(depth_dir, char_no, depth_set)
    if not depth_files:
        # nightshift가 업로드 시점에 이미 확인했어야 하지만, 그 사이 폴더가 비워졌을
        # 수도 있으니 실행 시점에도 한 번 더 확인한다.
        print(f"[depth_batch] '{depth_set}' depth 세트에 이미지가 없습니다 ({depth_dir}/{char_no}/{depth_set})", file=sys.stderr)
        sys.exit(1)

    secondary_kind = env("SECONDARY_KIND", "none")
    secondary_picker = None
    if secondary_kind not in REF_KINDS:
        if secondary_kind != "none":
            print(f"[depth_batch] 경고: 알 수 없는 SECONDARY_KIND '{secondary_kind}' — 보조 참조를 끕니다.", file=sys.stderr)
        secondary_kind = None
    else:
        secondary_char_no = env("SECONDARY_CHAR_NO", DEFAULT_CHAR_NO)
        secondary_set = env("SECONDARY_SET", "")
        if not secondary_set:
            print("[depth_batch] 경고: SECONDARY_KIND가 설정됐지만 SECONDARY_SET이 비어 있어 보조 참조를 끕니다.", file=sys.stderr)
            secondary_kind = None
        else:
            secondary_dir = ref_kind_dir(secondary_kind)
            secondary_files = list_ref_images(secondary_dir, secondary_char_no, secondary_set)
            if not secondary_files:
                print(
                    f"[depth_batch] 경고: 보조 참조 세트 '{secondary_set}'에 이미지가 없어 "
                    f"({secondary_dir}/{secondary_char_no}/{secondary_set}) 보조 참조를 끕니다.",
                    file=sys.stderr,
                )
                secondary_kind = None
            else:
                secondary_picker = make_picker(depth_mode, secondary_files)

    base_workflow = load_workflow(workflow_path)
    comfy_url = env("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
    job_id = env("JOB_ID")
    nightshift_url = env("NIGHTSHIFT_URL", "http://127.0.0.1:8000")

    picker = make_picker(depth_mode, depth_files)

    print(f"[depth_batch] 총 {depth_count}건 제출 예정 (인물 수: {char_no}, depth 세트: {depth_set}, {len(depth_files)}장, 방식: {depth_mode})")
    if secondary_picker is not None:
        print(f"[depth_batch] 보조 참조 사용: 종류={secondary_kind}")
    report_progress(job_id, nightshift_url, depth_count, 0)

    done = 0
    for index in range(1, depth_count + 1):
        seed = random.randint(0, 2**31 - 1)
        depth_path = picker.pick()
        secondary_path = secondary_picker.pick() if secondary_picker is not None else None
        run_once(base_workflow, comfy_url, seed, char_no, depth_path, index, job_id, output_dir, main_prompt, width, height,
                 secondary_kind=secondary_kind, secondary_path=secondary_path)
        done += 1
        report_progress(job_id, nightshift_url, depth_count, done)

    print(f"[depth_batch] 총 {depth_count}건 완료")


if __name__ == "__main__":
    main()
