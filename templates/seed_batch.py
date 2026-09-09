"""
시드 반복 템플릿 — 워크플로우 하나를 시드만 바꿔가며 seed_count번 반복 실행한다.

csv_batch.py와 같은 ComfyUI 연동 방식(워크플로우 노드 찾기/제출/폴링)을 쓰되,
CSV 입력과 프롬프트 주입 없이 시드만 바꾸는 가장 단순한 형태다. 워크플로우 JSON의
노드 제목/구조가 SEED_NODE_TITLE / SAVE_NODE_TITLE 매칭과 맞지 않으면 조정이 필요하다.

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
    둘 다 채워야 적용되며(하나만 채우면 무시하고 경고), 둘 다 비어 있으면
    워크플로우에 이미 들어있는 값을 그대로 둔다. 적용 대상은 EmptyLatentImage류
    노드의 width/height 입력이다 — 그 입력이 리터럴 숫자면 직접 덮어쓰고, 별도
    PrimitiveInt 노드로 링크돼 있으면(예: "width"라는 이름의 정수 노드를 여러
    곳에서 참조) 링크는 그대로 두고 연결된 노드의 값을 갱신한다(set_linked_value
    참고) — 같은 노드를 참조하는 다른 곳에도 일관되게 반영되게 하기 위함이다.

EmptyLatentImage 노드가 여러 개인 워크플로우:
    해상도 프리셋을 바꿔가며 테스트하다 보면 EmptyLatentImage 노드가 여러 개
    남아있고 그 중 하나만 실제로 KSampler에 배선돼 있는 경우가 있다(예: 다른
    비율을 테스트하려고 만든 노드가 배선만 안 된 채 남음). 이런 경우
    LATENT_NODE_TITLE로 제목 매칭이 안 되면, 아무 EmptyLatentImage나 고르지
    않고 실제로 다른 노드의 입력에 연결된(=워크플로우 실행에 쓰이는) 노드를
    우선으로 고른다(find_node의 prefer_connected, batch_size 기본값 계산에도
    똑같이 적용됨) — 안 그러면 연결 안 된 노드에 값을 써봤자 실제 생성 결과에는
    반영되지 않는다.

환경변수:
    WORKFLOW_PATH   (필수) ComfyUI API 형식 workflow json 경로 (nightshift가 주입)
    SEED_COUNT      (필수) 반복할 시드 개수 (nightshift가 템플릿 옵션 "seed_count"로 주입)
    SEED_MODE       시드를 정하는 방식 (nightshift가 템플릿 옵션 "seed_mode"로 주입,
                    기본 "random") — "random"이면 이미지마다 매번 무작위 시드를,
                    "sequential"이면 0부터 1씩 증가하는 시드(0, 1, 2, ...)를 쓴다.
                    재현 가능한 비교(같은 시드로 여러 설정 비교 등)가 필요하면
                    "sequential"을 쓴다.
    MAIN_PROMPT     메인 프롬프트 (nightshift가 템플릿 옵션 "main_prompt"로 주입, 기본
                    빈 값 — 비워두면 워크플로우의 프롬프트를 그대로 씀)
    DANBOORU_SEED_PROMPTS  시드별로 다르게 쓸 프롬프트 목록(JSON 문자열 배열, nightshift가
                    템플릿 옵션 "danbooru_seed_prompts"로 주입). 작업 관리 화면의 "시드마다
                    Danbooru로 다른 프롬프트 생성" 체크박스를 켰을 때만 채워지며, 큐에
                    추가하는 시점에 이미 시드 개수만큼 뽑아둔 값이다. i번째 시드는
                    이 배열의 (i-1)번째 값을 쓰고, 비어 있거나 길이가 모자라면 그
                    시드는 MAIN_PROMPT로 대체한다(체크 안 하면 기본이 빈 값이라
                    예전처럼 모든 시드가 MAIN_PROMPT 하나를 그대로 씀).
    WIDTH, HEIGHT   해상도 (nightshift가 템플릿 옵션 "width"/"height"로 주입, 기본 빈 값
                    — 둘 다 비워두면 워크플로우의 값을 그대로 씀)
    CHECKPOINT      체크포인트 파일명 (nightshift가 템플릿 옵션 "checkpoint"로 주입, 기본 빈 값 —
                    비워두면 워크플로우에 들어있는 체크포인트를 그대로 씀). 화면의 드롭다운은
                    ComfyUI에 실제로 설치된 목록에서만 고르게 돼 있다.
    LORA_NAME       LoRA 파일명 (템플릿 옵션 "lora_name", 기본 빈 값 — 비워두면 그대로 씀).
                    LoRA 로더가 여러 개면 그중 하나에만 적용된다(apply_lora 참고).
    LORA_STRENGTH   LoRA 강도 strength_model (템플릿 옵션 "lora_strength", 기본 빈 값 —
                    비워두면 그대로 씀). strength_clip은 건드리지 않는다.
    CHECKPOINT_NODE_TITLE  체크포인트를 주입할 노드의 _meta.title 부분일치
                    (기본 없음 — 실제로 배선된 로더 노드를 자동으로 고름)
    LORA_NODE_TITLE LoRA를 주입할 노드의 _meta.title 부분일치 (기본 없음)
    COMFY_URL       ComfyUI 서버 주소 (기본 http://127.0.0.1:8188)
    JOB_ID          nightshift가 주입하는 이 작업의 id (진행 상황 보고용, 없으면 보고 생략)
    NIGHTSHIFT_URL  nightshift 자신의 주소 (진행 상황 보고용, 기본 http://127.0.0.1:8000)
    NIGHTSHIFT_API_KEY 설정돼 있으면 진행 상황 보고에 X-API-Key로 실어 보냄 (선택)
    SEED_NODE_TITLE    시드를 주입할 노드의 _meta.title 부분일치 (기본 "KSampler")
    LATENT_NODE_TITLE  해상도를 주입할 노드의 _meta.title 부분일치 (기본 "latent")
    SAVE_NODE_TITLE    파일명 접두사를 주입할 노드의 _meta.title 부분일치 (기본 "Save")
    POLL_INTERVAL_SEC  히스토리 폴링 간격 초 (기본 2)
    POLL_TIMEOUT_SEC   개별 작업 완료 대기 제한 초 (기본 600)

진행 상황 보고:
    실행 전에 워크플로우의 EmptyLatentImage류 노드에 설정된 batch_size를 읽어
    (seed_count x batch_size)로 예상 총 이미지 수를 계산하고, nightshift의
    PUT /api/jobs/{job_id}/progress로 {"total": N, "done": M}을 보고한다.
    nightshift 웹 UI가 이 값을 폴링해서 진행률을 보여준다. 보고에 실패해도
    (nightshift가 죽어있거나 JOB_ID가 없는 등) 작업 자체는 계속 진행된다.

결과물 파일명 규칙:
    SaveImage(SAVE_NODE_TITLE로 찾은 노드)의 filename_prefix를
    "<JOB_ID>/seed_batch_<순번>_seed<시드값>" 형식으로 채운다 (예:
    ca6e661b/seed_batch_3_seed482913). ComfyUI가 filename_prefix의 "/"를
    하위 폴더로 해석해 출력 폴더 밑에 JOB_ID 이름의 폴더를 만들고 그 안에
    저장하므로(작업마다 폴더가 자동으로 나뉨), 실제 저장 시 여기에 자기
    카운터를 덧붙이면 최종 경로는 "ca6e661b/seed_batch_3_seed482913_00001_.png"
    처럼 나온다 — nightshift 갤러리의 "작업별 보기"가 이 폴더 이름으로 묶어서
    보여준다(output_images.py 참고). JOB_ID가 없으면(스크립트를 nightshift
    밖에서 직접 실행한 경우 등) 하위 폴더 없이 기존처럼 출력 폴더 바로 밑에 저장한다.
"""

import copy
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
import uuid


def env(name, default=None):
    return os.environ.get(name, default)


def load_workflow(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


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


def find_node(workflow, title_substring=None, class_types=(), prefer_connected=False):
    title_substring = (title_substring or "").lower()
    connected = connected_node_ids(workflow) if (class_types and prefer_connected) else None
    fallback = None
    fallback_connected = None
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        meta_title = str(node.get("_meta", {}).get("title", "")).lower()
        class_type = node.get("class_type", "")
        if title_substring and title_substring in meta_title:
            return node_id, node
        if class_types and class_type in class_types:
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


def apply_seed(workflow, seed):
    node_id, node = find_node(
        workflow,
        title_substring=env("SEED_NODE_TITLE", "KSampler"),
        class_types=("KSampler", "KSamplerAdvanced"),
    )
    if node is None:
        print("[seed_batch] 경고: 시드를 넣을 노드를 찾지 못했습니다 (KSampler 없음)", file=sys.stderr)
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
            "[seed_batch] 경고: MAIN_PROMPT를 넣을 노드를 찾지 못했습니다 "
            "(제목에 'main_prompt'가 포함된 CLIPTextEncode/Primitive 텍스트 노드 없음)",
            file=sys.stderr,
        )
        return
    node.setdefault("inputs", {})[field] = main_prompt


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
        print("[seed_batch] 경고: 체크포인트를 넣을 노드를 찾지 못했습니다 (CheckpointLoader 없음)", file=sys.stderr)
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
        print("[seed_batch] 경고: LoRA를 넣을 노드를 찾지 못했습니다 (LoraLoader 없음)", file=sys.stderr)
        return
    total = sum(
        1 for n in workflow.values()
        if isinstance(n, dict) and n.get("class_type") in LORA_CLASS_TYPES
    )
    if total > 1:
        print(
            f"[seed_batch] 안내: LoRA 로더가 {total}개라 그중 노드 {node_id}에만 적용했습니다 "
            "(다른 노드에 넣으려면 LORA_NODE_TITLE로 지정하세요).",
            file=sys.stderr,
        )
    if lora:
        set_linked_value(workflow, node, "lora_name", lora)
    if strength:
        try:
            strength_value = float(strength)
        except ValueError:
            print(f"[seed_batch] 경고: LORA_STRENGTH 값 '{strength}'을 숫자로 변환하지 못했습니다.", file=sys.stderr)
        else:
            set_linked_value(workflow, node, "strength_model", strength_value)


def apply_resolution(workflow, width, height):
    width = (width or "").strip()
    height = (height or "").strip()
    if not width and not height:
        # 둘 다 비어 있으면 워크플로우에 이미 들어있는 해상도를 그대로 둔다.
        return
    if not width or not height:
        print(
            "[seed_batch] 경고: WIDTH/HEIGHT는 둘 다 채워야 적용됩니다 (하나만 비어 있음). 무시합니다.",
            file=sys.stderr,
        )
        return
    try:
        width_value = int(float(width))
        height_value = int(float(height))
    except ValueError:
        print(f"[seed_batch] 경고: WIDTH/HEIGHT 값 '{width}x{height}'을 정수로 변환하지 못했습니다.", file=sys.stderr)
        return

    node_id, node = find_node(
        workflow,
        title_substring=env("LATENT_NODE_TITLE", "latent"),
        class_types=("EmptyLatentImage",),
        prefer_connected=True,
    )
    if node is None:
        print("[seed_batch] 경고: 해상도를 넣을 노드를 찾지 못했습니다 (EmptyLatentImage 없음)", file=sys.stderr)
        return
    set_linked_value(workflow, node, "width", width_value)
    set_linked_value(workflow, node, "height", height_value)


def get_default_batch_size(workflow):
    node_id, node = find_node(workflow, class_types=("EmptyLatentImage",), prefer_connected=True)
    if node is None:
        return 1
    try:
        return max(1, int(node.get("inputs", {}).get("batch_size", 1)))
    except (TypeError, ValueError):
        return 1


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
        print(f"[seed_batch] 경고: 진행 상황 보고 실패: {e}", file=sys.stderr)


def apply_filename_prefix(workflow, index, seed):
    node_id, node = find_node(
        workflow,
        title_substring=env("SAVE_NODE_TITLE", "Save"),
        class_types=("SaveImage", "SaveImageWebsocket"),
    )
    if node is None:
        return
    prefix = f"seed_batch_{index}_seed{seed}"
    # JOB_ID가 있으면(nightshift가 큐로 실행할 때는 항상 있음) ComfyUI 출력 폴더 밑에
    # 그 job_id 이름의 하위 폴더를 만들어 그 안에 저장한다 — filename_prefix에 "/"가
    # 있으면 ComfyUI SaveImage가 하위 폴더로 해석해 자동으로 만들어준다. nightshift
    # 갤러리는 이 하위 폴더 이름으로 "작업별 보기"를 구성한다(output_images.py 참고).
    job_id = env("JOB_ID")
    if job_id:
        prefix = f"{job_id}/{prefix}"
    node.setdefault("inputs", {})["filename_prefix"] = prefix


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


def run_once(base_workflow, comfy_url, seed, index, main_prompt, width, height):
    workflow = copy.deepcopy(base_workflow)
    apply_seed(workflow, seed)
    apply_checkpoint(workflow)
    apply_lora(workflow)
    apply_main_prompt(workflow, main_prompt)
    apply_resolution(workflow, width, height)
    apply_filename_prefix(workflow, index, seed)

    prompt_id = queue_prompt(comfy_url, workflow)
    print(f"[seed_batch] [{index}] seed={seed} 큐 등록 (prompt_id={prompt_id})")
    wait_for_completion(comfy_url, prompt_id)
    print(f"[seed_batch] [{index}] 완료")


def main():
    workflow_path = env("WORKFLOW_PATH")
    seed_count_raw = env("SEED_COUNT")
    if not workflow_path or not seed_count_raw:
        print("[seed_batch] WORKFLOW_PATH와 SEED_COUNT 환경변수가 모두 필요합니다.", file=sys.stderr)
        sys.exit(1)

    seed_count = int(seed_count_raw)
    seed_mode = env("SEED_MODE", "random")
    main_prompt = env("MAIN_PROMPT", "")
    # 작업 관리 화면의 "시드마다 Danbooru로 다른 프롬프트 생성" 체크박스가 켜져
    # 있었으면, nightshift가 큐에 추가하는 시점에 이미 시드 개수만큼 뽑아둔
    # 프롬프트 목록(JSON 문자열 배열)을 여기로 넘긴다 — 시드마다 그중 하나씩
    # 쓴다. 비어 있거나 파싱에 실패하면(체크 안 한 경우가 기본) 예전처럼
    # main_prompt 하나를 모든 시드에 그대로 쓴다.
    danbooru_seed_prompts = []
    danbooru_seed_prompts_raw = env("DANBOORU_SEED_PROMPTS", "")
    if danbooru_seed_prompts_raw:
        try:
            parsed = json.loads(danbooru_seed_prompts_raw)
            if isinstance(parsed, list):
                danbooru_seed_prompts = [str(p) for p in parsed]
        except json.JSONDecodeError:
            print("[seed_batch] 경고: DANBOORU_SEED_PROMPTS가 올바른 JSON이 아니어서 무시합니다.", file=sys.stderr)
    width = env("WIDTH", "")
    height = env("HEIGHT", "")
    base_workflow = load_workflow(workflow_path)
    comfy_url = env("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
    job_id = env("JOB_ID")
    nightshift_url = env("NIGHTSHIFT_URL", "http://127.0.0.1:8000")

    default_batch_size = get_default_batch_size(base_workflow)
    total_images = seed_count * default_batch_size
    print(f"[seed_batch] 총 {seed_count}건 제출 예정, 이미지 {total_images}장 예상")
    report_progress(job_id, nightshift_url, total_images, 0)

    done_images = 0
    for index in range(1, seed_count + 1):
        seed = random.randint(0, 2**31 - 1) if seed_mode == "random" else index - 1
        prompt_for_seed = (
            danbooru_seed_prompts[index - 1]
            if index - 1 < len(danbooru_seed_prompts)
            else main_prompt
        )
        run_once(base_workflow, comfy_url, seed, index, prompt_for_seed, width, height)
        done_images += default_batch_size
        report_progress(job_id, nightshift_url, total_images, done_images)

    print(f"[seed_batch] 총 {seed_count}건 완료 (이미지 {done_images}장)")


if __name__ == "__main__":
    main()
