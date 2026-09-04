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

환경변수:
    WORKFLOW_PATH   (필수) ComfyUI API 형식 workflow json 경로 (nightshift가 주입)
    SEED_COUNT      (필수) 반복할 시드 개수 (nightshift가 템플릿 옵션 "seed_count"로 주입)
    MAIN_PROMPT     메인 프롬프트 (nightshift가 템플릿 옵션 "main_prompt"로 주입, 기본
                    빈 값 — 비워두면 워크플로우의 프롬프트를 그대로 씀)
    WIDTH, HEIGHT   해상도 (nightshift가 템플릿 옵션 "width"/"height"로 주입, 기본 빈 값
                    — 둘 다 비워두면 워크플로우의 값을 그대로 씀)
    COMFY_URL       ComfyUI 서버 주소 (기본 http://127.0.0.1:8188)
    JOB_ID          nightshift가 주입하는 이 작업의 id (진행 상황 보고용, 없으면 보고 생략)
    NIGHTSHIFT_URL  nightshift 자신의 주소 (진행 상황 보고용, 기본 http://127.0.0.1:8000)
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
    "seed_batch_<순번>_seed<시드값>" 형식으로 채운다 (예: seed_batch_3_seed482913).
    ComfyUI가 실제 저장 시 여기에 자기 카운터를 덧붙이므로 최종 파일명은
    "seed_batch_3_seed482913_00001_.png"처럼 나온다 — 파일명만 보고도 몇 번째
    시도였는지와 어떤 시드였는지 바로 알 수 있다.
"""

import copy
import json
import os
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


def find_node(workflow, title_substring=None, class_types=()):
    title_substring = (title_substring or "").lower()
    fallback = None
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        meta_title = str(node.get("_meta", {}).get("title", "")).lower()
        class_type = node.get("class_type", "")
        if title_substring and title_substring in meta_title:
            return node_id, node
        if class_types and class_type in class_types and fallback is None:
            fallback = (node_id, node)
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
    )
    if node is None:
        print("[seed_batch] 경고: 해상도를 넣을 노드를 찾지 못했습니다 (EmptyLatentImage 없음)", file=sys.stderr)
        return
    set_linked_value(workflow, node, "width", width_value)
    set_linked_value(workflow, node, "height", height_value)


def get_default_batch_size(workflow):
    node_id, node = find_node(workflow, class_types=("EmptyLatentImage",))
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
        req = urllib.request.Request(
            f"{nightshift_url}/api/jobs/{job_id}/progress",
            data=payload,
            headers={"Content-Type": "application/json"},
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
    node.setdefault("inputs", {})["filename_prefix"] = f"seed_batch_{index}_seed{seed}"


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
    main_prompt = env("MAIN_PROMPT", "")
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
        run_once(base_workflow, comfy_url, index - 1, index, main_prompt, width, height)
        done_images += default_batch_size
        report_progress(job_id, nightshift_url, total_images, done_images)

    print(f"[seed_batch] 총 {seed_count}건 완료 (이미지 {done_images}장)")


if __name__ == "__main__":
    main()
