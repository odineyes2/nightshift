"""
CSV 배치 템플릿 — ComfyUI workflow API에 CSV의 (프롬프트 여러 개, 시드, 해상도, 배치수)
조합을 반복 제출한다.

주의: 이 스크립트는 실제 comfy_batch_template.py를 그대로 옮긴 것이 아니라,
매니페스트에 적힌 설명("CSV 배치 (제목/프롬프트 x 시드 반복)")을 바탕으로
best-effort로 새로 작성한 예시 구현이다. 워크플로우 JSON의 노드 제목/구조는
ComfyUI에서 어떻게 워크플로우를 구성했는지에 따라 다르므로, 아래 매칭 로직이
실제 워크플로우와 맞지 않으면 조정이 필요하다.

프롬프트 노드 매칭:
    많은 워크플로우가 프롬프트 하나를 여러 텍스트 노드로 나눠서
    (예: 트리거워드 / 본문 / 퀄리티 태그 / 네거티브) 구성한 뒤 다운스트림에서
    합치는 방식을 쓴다. 이 스크립트는 CSV에 아래 컬럼이 있으면, 그 컬럼 이름과
    "_meta.title"에 그 이름이 포함된 노드를 찾아 값을 주입한다. 노드 종류가
    CLIPTextEncode면 "text" 입력에, PrimitiveStringMultiline 같은 Primitive
    계열 텍스트 노드면 "value" 입력에 쓴다(primitive_value_field() 참고 — 새
    종류의 텍스트 노드가 나와도 그 노드에 이미 있는 입력 키로 알아서 판단한다).
    컬럼이 없거나 값이 비어 있으면 그 필드는 건드리지 않는다.

        trigger_prompt, main_prompt, quality_prompt, negative_prompt, prompt

    (예: 워크플로우에 "main_prompt", "negative_prompt"라는 제목의 텍스트 노드가
    있다면, CSV에 같은 이름의 컬럼을 두면 그대로 매칭된다.) 이름이 다르면
    워크플로우의 실제 노드 제목에 맞춰 위 목록이나 아래 PROMPT_FIELD_TITLES를
    조정한다. 프롬프트 노드가 여러 개일 수 있으므로, 이름이 일치하는 노드를
    못 찾으면 다른 노드로 대체하지 않고 건너뛴다(엉뚱한 노드를 덮어쓰는
    사고를 막기 위함).

환경변수:
    WORKFLOW_PATH   (필수) ComfyUI API 형식 workflow json 경로 (nightshift가 주입)
    CSV_PATH        (필수) 아래 CSV 컬럼을 가진 csv 경로 (nightshift가 주입)
    SEEDS_PER_CASE  (필수) csv 행에 seed 컬럼이 없을 때, 케이스(행)당 반복할 시드 개수
                    (nightshift가 템플릿 옵션 "seeds_per_case"로 주입)
    COMFY_URL       ComfyUI 서버 주소 (기본 http://127.0.0.1:8188)
    JOB_ID          nightshift가 주입하는 이 작업의 id (진행 상황 보고용, 없으면 보고 생략)
    NIGHTSHIFT_URL  nightshift 자신의 주소 (진행 상황 보고용, 기본 http://127.0.0.1:8000)
    SEED_NODE_TITLE    시드를 주입할 노드의 _meta.title 부분일치 (기본 "KSampler")
    LATENT_NODE_TITLE  해상도/배치수를 주입할 노드의 _meta.title 부분일치 (기본 "latent")
    SAVE_NODE_TITLE    파일명 접두사를 주입할 노드의 _meta.title 부분일치 (기본 "Save")
    POLL_INTERVAL_SEC  히스토리 폴링 간격 초 (기본 2)
    POLL_TIMEOUT_SEC   개별 작업 완료 대기 제한 초 (기본 600)

진행 상황 보고:
    실행 전에 제출 계획(각 행 x 시드 조합, batch_no 또는 워크플로우 기본
    batch_size)을 먼저 세워 예상 총 이미지 수를 계산하고, nightshift의
    PUT /api/jobs/{job_id}/progress로 {"total": N, "done": M}을 보고한다.
    nightshift 웹 UI가 이 값을 폴링해서 진행률을 보여준다. 보고에 실패해도
    (nightshift가 죽어있거나 JOB_ID가 없는 등) 작업 자체는 계속 진행된다.

결과물 파일명 규칙:
    SaveImage(SAVE_NODE_TITLE로 찾은 노드)의 filename_prefix를
    "<title(안전한 문자로 치환, 없으면 batch_<순번>)>_seed<시드값>" 형식으로 채운다
    (예: title="고양이"면 "고양이_seed482913"). ComfyUI가 실제 저장 시 여기에 자기
    카운터를 덧붙이므로 최종 파일명은 "고양이_seed482913_00001_.png"처럼 나온다 —
    파일명만 보고도 어느 행/시도였는지와 어떤 시드였는지 바로 알 수 있다.

CSV 컬럼:
    title           결과 파일명 접두사로 쓰일 제목 (선택, name도 허용)
    trigger_prompt   트리거워드 프롬프트 (선택)
    main_prompt      본문 프롬프트 (prompt와 동일하게 취급, 둘 중 하나는 있어야 함)
    quality_prompt   퀄리티 태그 프롬프트 (선택)
    negative_prompt  네거티브 프롬프트 (선택)
    prompt           main_prompt가 없을 때 쓰이는 대체 컬럼 (하위 호환용, 선택)
    seed             지정하면 그 시드 하나만 사용, 비어 있으면 SEEDS_PER_CASE개의
                     랜덤 시드로 반복 (선택)
    batch_no         한 번에 생성할 이미지 수 (EmptyLatentImage류 노드의
                     batch_size에 주입, 선택)
    width, height    해상도를 가로/세로 각각 픽셀 값으로 직접 지정 (선택). 둘 다 채워야
                     적용되며, 채워지면 resolution보다 우선한다
    resolution       width/height가 비어 있을 때 대신 쓰이는 해상도. "1024x1024"처럼
                     WxH 형식이거나 RESOLUTION_PRESETS에 정의된 이름
                     ("square"/"portrait"/"landscape"/"9:16"/"16:9") 중 하나 (선택)

해상도 결정 우선순위:
    1. width와 height 컬럼이 둘 다 채워져 있으면 그 값을 그대로 사용
    2. 아니면 resolution 컬럼(프리셋 이름 또는 WxH 형식)을 사용
    3. 셋 다 비어 있으면 워크플로우에 이미 들어있는 값을 그대로 둠(건드리지 않음)

EmptyLatentImage 노드가 여러 개인 워크플로우:
    해상도 프리셋을 바꿔가며 테스트하다 보면 EmptyLatentImage 노드가 여러 개
    남아있고 그 중 하나만 실제로 KSampler에 배선돼 있는 경우가 있다(예: 다른
    비율을 테스트하려고 만든 노드가 배선만 안 된 채 남음). 이런 경우
    LATENT_NODE_TITLE로 제목 매칭이 안 되면, 아무 EmptyLatentImage나 고르지
    않고 실제로 다른 노드의 입력에 연결된(=워크플로우 실행에 쓰이는) 노드를
    우선으로 고른다.

width/height가 별도 노드(PrimitiveInt 등)로 분리된 워크플로우:
    EmptyLatentImage의 width/height 입력이 리터럴 숫자가 아니라 다른 노드로의
    링크(예: PrimitiveInt 노드 하나를 "width"라는 이름으로 만들어 여러 곳에서
    같이 참조)인 경우, 그 링크를 끊고 EmptyLatentImage에 직접 리터럴을 쓰는
    대신 링크가 가리키는 노드의 값 입력을 갱신한다(set_linked_value() 참고).
    같은 PrimitiveInt 노드를 다른 곳(예: 업스케일 배율 계산)에서도 참조하고
    있다면 그쪽에도 새 값이 그대로 반영된다. 링크 대상 노드의 값 입력을 알 수
    없으면(알 수 없는 노드 구조) 안전하게 EmptyLatentImage 쪽에 리터럴로 쓴다.
"""

import copy
import csv
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

# CSV 컬럼 이름 = 매칭할 텍스트 노드(CLIPTextEncode 또는 PrimitiveStringMultiline
# 등)의 _meta.title 부분 문자열. 워크플로우의 실제 노드 제목이 다르면 이 목록을
# 맞춰서 조정한다.
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
    "9:16": (768, 1344),   # Portrait Widescreen
    "16:9": (1344, 768),   # Widescreen
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
    """다른 노드의 입력 링크로 실제 연결되어 있는(=출력이 쓰이고 있는) 노드 id 집합.
    워크플로우를 손으로 편집하다 보면 같은 class_type의 노드가 여럿 남아있는데
    그 중 하나만 실제로 연결돼 있는 경우가 흔해서(예: 해상도 프리셋을 바꿔보려고
    EmptyLatentImage를 여러 개 만들어두고 하나만 배선), class_type만으로 노드를
    고를 때 이 정보로 진짜 쓰이는 노드를 가려낸다."""
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


def apply_prompts(workflow, row):
    applied = False
    for field, title_substring in PROMPT_FIELD_TITLES.items():
        value = (row.get(field) or "").strip()
        if not value:
            continue
        # 프롬프트 노드가 여러 개(트리거/본문/퀄리티/네거티브)일 수 있으므로
        # 제목이 정확히 일치하지 않으면 다른 노드로 대체하지 않는다.
        node_id, node = find_node(
            workflow,
            title_substring=title_substring,
            allow_class_fallback=False,
        )
        input_field = primitive_value_field(node) if node is not None else None
        if input_field is None:
            print(
                f"[csv_batch] 경고: '{field}' 값을 넣을 노드를 찾지 못했습니다 "
                f"(제목에 '{title_substring}'가 포함된 CLIPTextEncode/Primitive 텍스트 노드 없음)",
                file=sys.stderr,
            )
            continue
        node.setdefault("inputs", {})[input_field] = value
        applied = True
    if not applied:
        print("[csv_batch] 경고: 이 행에 프롬프트 컬럼 값이 하나도 없습니다.", file=sys.stderr)


def apply_seed(workflow, seed):
    node_id, node = find_node(
        workflow,
        title_substring=env("SEED_NODE_TITLE", "KSampler"),
        class_types=("KSampler", "KSamplerAdvanced"),
    )
    if node is None:
        print("[csv_batch] 경고: 시드를 넣을 노드를 찾지 못했습니다 (KSampler 없음)", file=sys.stderr)
        return
    try:
        node.setdefault("inputs", {})["seed"] = int(seed)
    except (TypeError, ValueError):
        print(f"[csv_batch] 경고: seed 값 '{seed}'을 정수로 변환하지 못했습니다", file=sys.stderr)


def parse_batch_size(batch_no):
    batch_no = (batch_no or "").strip()
    if not batch_no:
        return None
    try:
        value = int(batch_no)
    except ValueError:
        print(f"[csv_batch] 경고: batch_no 값 '{batch_no}'을 정수로 변환하지 못했습니다", file=sys.stderr)
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
        print("[csv_batch] 경고: batch_no를 넣을 노드를 찾지 못했습니다 (EmptyLatentImage 없음)", file=sys.stderr)
        return
    node.setdefault("inputs", {})["batch_size"] = batch_size


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
        print(f"[csv_batch] 경고: 진행 상황 보고 실패: {e}", file=sys.stderr)


def resolve_resolution(width, height, resolution):
    """width/height를 각각 지정했으면 그 값을 최우선으로 쓰고, 둘 중 하나라도
    비어 있으면 resolution(프리셋 이름 또는 WxH 문자열)을 따른다. 셋 다 없으면
    None을 반환해 워크플로우에 이미 설정된 값을 그대로 둔다."""
    width = (width or "").strip()
    height = (height or "").strip()
    resolution = (resolution or "").strip()

    if width and height:
        try:
            return int(width), int(height)
        except ValueError:
            print(
                f"[csv_batch] 경고: width/height 값 '{width}x{height}'을 정수로 변환하지 못했습니다. "
                "resolution 컬럼으로 대체합니다.",
                file=sys.stderr,
            )
    elif width or height:
        print(
            "[csv_batch] 경고: width/height는 둘 다 채워야 적용됩니다 (하나만 비어 있음). "
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
            f"[csv_batch] 경고: resolution 값 '{resolution}'을 해석하지 못했습니다 "
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
        print("[csv_batch] 경고: 해상도를 넣을 노드를 찾지 못했습니다 (EmptyLatentImage 없음)", file=sys.stderr)
        return
    width_value, height_value = resolved
    set_linked_value(workflow, node, "width", width_value)
    set_linked_value(workflow, node, "height", height_value)


def apply_filename_prefix(workflow, title, index, seed):
    node_id, node = find_node(
        workflow,
        title_substring=env("SAVE_NODE_TITLE", "Save"),
        class_types=("SaveImage", "SaveImageWebsocket"),
    )
    if node is None:
        return
    prefix = sanitize_prefix(title, f"batch_{index}")
    node.setdefault("inputs", {})["filename_prefix"] = f"{prefix}_seed{seed}"


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


def run_once(base_workflow, comfy_url, row, title, seed, index):
    workflow = copy.deepcopy(base_workflow)
    apply_prompts(workflow, row)
    apply_seed(workflow, seed)
    apply_batch_size(workflow, row.get("batch_no"))
    apply_resolution(workflow, row.get("width"), row.get("height"), row.get("resolution"))
    apply_filename_prefix(workflow, title, index, seed)

    prompt_id = queue_prompt(comfy_url, workflow)
    print(f"[csv_batch] [{index}] title={title!r} seed={seed} 큐 등록 (prompt_id={prompt_id})")
    wait_for_completion(comfy_url, prompt_id)
    print(f"[csv_batch] [{index}] 완료")


def main():
    workflow_path = env("WORKFLOW_PATH")
    csv_path = env("CSV_PATH")
    if not workflow_path or not csv_path:
        print("[csv_batch] WORKFLOW_PATH와 CSV_PATH 환경변수가 모두 필요합니다.", file=sys.stderr)
        sys.exit(1)

    base_workflow = load_workflow(workflow_path)
    rows = load_rows(csv_path)
    if not rows:
        print("[csv_batch] CSV에 처리할 행이 없습니다.")
        return

    seeds_per_case = int(env("SEEDS_PER_CASE", "10"))
    comfy_url = env("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
    job_id = env("JOB_ID")
    nightshift_url = env("NIGHTSHIFT_URL", "http://127.0.0.1:8000")

    # 실행 전에 먼저 제출 계획을 세워서 전체 이미지 수를 계산한다. batch_no가 없는
    # 행은 워크플로우 자체의 기본 batch_size를 따른다.
    default_batch_size = get_default_batch_size(base_workflow)
    plan = []  # [(row, title, seed, batch_size), ...]
    for row in rows:
        title = (row.get("title") or row.get("name") or "").strip()
        main_prompt = (row.get("main_prompt") or row.get("prompt") or "").strip()
        if not main_prompt:
            print(f"[csv_batch] 건너뜀 (main_prompt/prompt 없음): {row}")
            continue

        batch_size = parse_batch_size(row.get("batch_no"))
        if batch_size is None:
            batch_size = default_batch_size

        row_seed = (row.get("seed") or "").strip()
        if row_seed:
            seeds = [row_seed]
        else:
            seeds = [random.randint(0, 2**31 - 1) for _ in range(seeds_per_case)]
        for seed in seeds:
            plan.append((row, title, seed, batch_size))

    total_images = sum(batch_size for _, _, _, batch_size in plan)
    print(f"[csv_batch] 총 {len(plan)}건 제출 예정, 이미지 {total_images}장 예상")
    report_progress(job_id, nightshift_url, total_images, 0)

    done_images = 0
    for index, (row, title, seed, batch_size) in enumerate(plan, start=1):
        run_once(base_workflow, comfy_url, row, title, seed, index)
        done_images += batch_size
        report_progress(job_id, nightshift_url, total_images, done_images)

    print(f"[csv_batch] 총 {len(plan)}건 완료 (이미지 {done_images}장)")


if __name__ == "__main__":
    main()
