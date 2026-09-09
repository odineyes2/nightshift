"""
CSV + IPAdapter 배치 템플릿 — csv_batch.py의 CSV 기반 배치 제출(프롬프트 여러 개,
시드)에 ipadapter_batch.py의 참조 이미지 주입을 결합한다. "IPAdapter" 워크플로우
유형(프리셋 기반 — app.py의 workflow_presets, "🎛 모델" 탭 참고)에 쓴다. CSV 행마다
ipadapter_ref 컬럼으로 참조 이미지를 지정한다(세트 구분 없는 평평한 목록 —
ipadapter_batch.py 모듈 설명 참고).

주의: csv_batch.py, pose_csv_batch.py와 마찬가지로 이 저장소의 템플릿은 서로
임포트하지 않고 필요한 로직을 그대로 복사해서 자기 완결적으로 작성하는 게
컨벤션이다.

CSV 컬럼:
    title           결과 파일명 접두사로 쓰일 제목 (선택, name도 허용)
    trigger_prompt   트리거워드 프롬프트 (선택)
    main_prompt      본문 프롬프트 (prompt와 동일하게 취급, 둘 중 하나는 있어야 함)
    quality_prompt   퀄리티 태그 프롬프트 (선택)
    negative_prompt  네거티브 프롬프트 (선택)
    prompt           main_prompt가 없을 때 쓰이는 대체 컬럼 (하위 호환용, 선택)
    seed             지정하면 그 값을 시드로 사용, 비어 있으면 랜덤 시드 1개
    ipadapter_ref    (필수, 모든 행) 이 행에 쓸 IPAdapter 참조 이미지 파일명 —
                     NIGHTSHIFT_INPUT_IMAGES_DIR(기본 NIGHTSHIFT_ASSETS_DIR/input)
                     바로 아래에 있는 파일이어야 한다. pose 컬럼과 달리 비워두는
                     것을 허용하지 않는다 — IPAdapter는 참조 이미지 없이는 애초에
                     성립하지 않는 워크플로우이기 때문이다.

환경변수:
    WORKFLOW_PATH      (필수) ComfyUI API 형식 workflow json 경로 (nightshift가 주입)
    CSV_PATH           (필수) 위 컬럼을 가진 csv 경로 (nightshift가 주입)
    CHECKPOINT         체크포인트 파일명 (nightshift가 템플릿 옵션 "checkpoint"로 주입, 기본 빈 값 —
                       같은 family 안의 다른 체크포인트로 바꿀 때만 씀)
    LORA_NAME          LoRA 파일명 (템플릿 옵션 "lora_name", 기본 빈 값)
    LORA_STRENGTH      LoRA 강도 strength_model (템플릿 옵션 "lora_strength", 기본 빈 값)
    CHECKPOINT_NODE_TITLE  체크포인트를 주입할 노드의 _meta.title 부분일치 (기본 없음)
    LORA_NODE_TITLE    LoRA를 주입할 노드의 _meta.title 부분일치 (기본 없음)
    COMFY_URL          ComfyUI 서버 주소 (기본 http://127.0.0.1:8188)
    JOB_ID             nightshift가 주입하는 이 작업의 id (진행 상황 보고용, 없으면 보고 생략)
    NIGHTSHIFT_URL     nightshift 자신의 주소 (진행 상황 보고용, 기본 http://127.0.0.1:8000)
    NIGHTSHIFT_API_KEY 설정돼 있으면 진행 상황 보고에 X-API-Key로 실어 보냄 (선택)
    SEED_NODE_TITLE    시드를 주입할 노드의 _meta.title 부분일치 (기본 "KSampler")
    IPADAPTER_NODE_TITLE  참조 이미지를 주입할 LoadImage 노드의 _meta.title 부분일치
                       (기본 "ipadapter_ref")
    SAVE_NODE_TITLE    파일명 접두사를 주입할 노드의 _meta.title 부분일치 (기본 "Save")
    NIGHTSHIFT_INPUT_IMAGES_DIR  참조 이미지들이 있는 폴더 (기본 NIGHTSHIFT_ASSETS_DIR/input)
    NIGHTSHIFT_ASSETS_DIR  위 override가 없을 때 쓰는 상위 디렉토리 (기본 /workspace/dataset/assets)
    POLL_INTERVAL_SEC  히스토리 폴링 간격 초 (기본 2)
    POLL_TIMEOUT_SEC   개별 작업 완료 대기 제한 초 (기본 600)

진행 상황 보고:
    csv_batch.py와 같은 방식 — 행 하나 = 반복 한 번으로 총 개수를 미리 계산해
    PUT /api/jobs/{job_id}/progress로 보고한다.

결과물 파일명 규칙:
    csv_batch.py와 같다 — "<JOB_ID>/<title(안전한 문자로 치환, 없으면
    ipadapter_<순번>)>_seed<시드값>" 형식.
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
from pathlib import Path

IPADAPTER_REF_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

PROMPT_FIELD_TITLES = {
    "trigger_prompt": "trigger_prompt",
    "main_prompt": "main_prompt",
    "quality_prompt": "quality_prompt",
    "negative_prompt": "negative_prompt",
    "prompt": "prompt",
}


def env(name, default=None):
    return os.environ.get(name, default)


def load_workflow(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def input_images_dir():
    override = env("NIGHTSHIFT_INPUT_IMAGES_DIR")
    if override:
        return Path(override)
    assets_dir = env("NIGHTSHIFT_ASSETS_DIR", "/workspace/dataset/assets")
    return Path(assets_dir) / "input"


def resolve_ipadapter_ref(name):
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(f"올바르지 않은 참조 이미지 이름이에요: '{name}'")
    path = input_images_dir() / name
    if not path.is_file() or path.suffix.lower() not in IPADAPTER_REF_EXTENSIONS:
        raise ValueError(f"참조 이미지 '{name}'를 찾을 수 없어요.")
    return path


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
        node_id, node = find_node(workflow, title_substring=title_substring, allow_class_fallback=False)
        input_field = primitive_value_field(node) if node is not None else None
        if input_field is None:
            print(
                f"[ipadapter_csv_batch] 경고: '{field}' 값을 넣을 노드를 찾지 못했습니다 "
                f"(제목에 '{title_substring}'가 포함된 CLIPTextEncode/Primitive 텍스트 노드 없음)",
                file=sys.stderr,
            )
            continue
        node.setdefault("inputs", {})[input_field] = value
        applied = True
    if not applied:
        print("[ipadapter_csv_batch] 경고: 이 행에 프롬프트 컬럼 값이 하나도 없습니다.", file=sys.stderr)


def apply_seed(workflow, seed):
    node_id, node = find_node(
        workflow,
        title_substring=env("SEED_NODE_TITLE", "KSampler"),
        class_types=("KSampler", "KSamplerAdvanced"),
    )
    if node is None:
        print("[ipadapter_csv_batch] 경고: 시드를 넣을 노드를 찾지 못했습니다 (KSampler 없음)", file=sys.stderr)
        return
    try:
        node.setdefault("inputs", {})["seed"] = int(seed)
    except (TypeError, ValueError):
        print(f"[ipadapter_csv_batch] 경고: seed 값 '{seed}'을 정수로 변환하지 못했습니다", file=sys.stderr)


def find_loader_node(workflow, title_substring, class_types):
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
    ckpt = (env("CHECKPOINT", "") or "").strip()
    if not ckpt:
        return
    node_id, node = find_loader_node(workflow, env("CHECKPOINT_NODE_TITLE", ""), CHECKPOINT_CLASS_TYPES)
    if node is None:
        print("[ipadapter_csv_batch] 경고: 체크포인트를 넣을 노드를 찾지 못했습니다 (CheckpointLoader 없음)", file=sys.stderr)
        return
    set_linked_value(workflow, node, "ckpt_name", ckpt)


def apply_lora(workflow):
    lora = (env("LORA_NAME", "") or "").strip()
    strength = (env("LORA_STRENGTH", "") or "").strip()
    if not lora and not strength:
        return
    node_id, node = find_loader_node(workflow, env("LORA_NODE_TITLE", ""), LORA_CLASS_TYPES)
    if node is None:
        print("[ipadapter_csv_batch] 경고: LoRA를 넣을 노드를 찾지 못했습니다 (LoraLoader 없음)", file=sys.stderr)
        return
    total = sum(
        1 for n in workflow.values()
        if isinstance(n, dict) and n.get("class_type") in LORA_CLASS_TYPES
    )
    if total > 1:
        print(
            f"[ipadapter_csv_batch] 안내: LoRA 로더가 {total}개라 그중 노드 {node_id}에만 적용했습니다 "
            "(다른 노드에 넣으려면 LORA_NODE_TITLE로 지정하세요).",
            file=sys.stderr,
        )
    if lora:
        set_linked_value(workflow, node, "lora_name", lora)
    if strength:
        try:
            strength_value = float(strength)
        except ValueError:
            print(f"[ipadapter_csv_batch] 경고: LORA_STRENGTH 값 '{strength}'을 숫자로 변환하지 못했습니다.", file=sys.stderr)
        else:
            set_linked_value(workflow, node, "strength_model", strength_value)


def apply_ipadapter_ref(workflow, comfy_url, image_path):
    node_id, node = find_node(
        workflow,
        title_substring=env("IPADAPTER_NODE_TITLE", "ipadapter_ref"),
        class_types=("LoadImage",),
        prefer_connected=True,
    )
    if node is None:
        print("[ipadapter_csv_batch] 경고: 참조 이미지를 넣을 노드를 찾지 못했습니다 (LoadImage 없음)", file=sys.stderr)
        return
    uploaded_name = upload_image_to_comfy(comfy_url, image_path)
    node.setdefault("inputs", {})["image"] = uploaded_name


# ComfyUI에 이미 올린 이미지의 (키 -> ComfyUI가 돌려준 파일명) 캐시.
# 프로세스 하나가 작업 하나를 처리하고 끝나므로 실행 단위 캐시로 충분하다.
_uploaded_image_cache = {}


def upload_image_to_comfy(comfy_url, image_path):
    # 같은 이미지를 몇 번이고 다시 올리지 않는다. 이 템플릿은 이미지를 한 장 만들
    # 때마다 이 함수를 부르는데, 올리는 파일은 대개 실행 내내 같다(IMAGE_COUNT=50이면
    # 같은 파일을 50번 올린다). ComfyUI가 같은 머신에 있을 때는 눈에 안 띄었지만
    # 원격 pod에서는 그게 그대로 업로드 시간이 된다. 실행 중에 파일이 바뀌는 드문
    # 경우까지 감안해 크기/수정시각도 키에 넣는다.
    try:
        stat = image_path.stat()
        cache_key = (comfy_url, str(image_path.resolve()), stat.st_size, stat.st_mtime_ns)
    except OSError:
        cache_key = None
    if cache_key is not None and cache_key in _uploaded_image_cache:
        return _uploaded_image_cache[cache_key]

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
    uploaded_name = result["name"]
    if cache_key is not None:
        _uploaded_image_cache[cache_key] = uploaded_name
    return uploaded_name


def apply_filename_prefix(workflow, title, index, seed):
    node_id, node = find_node(
        workflow,
        title_substring=env("SAVE_NODE_TITLE", "Save"),
        class_types=("SaveImage", "SaveImageWebsocket"),
    )
    if node is None:
        return
    prefix = sanitize_prefix(title, f"ipadapter_{index}")
    prefix = f"{prefix}_seed{seed}"
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
        print(f"[ipadapter_csv_batch] 경고: 진행 상황 보고 실패: {e}", file=sys.stderr)


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


def run_once(base_workflow, comfy_url, row, title, seed, index, image_path):
    workflow = copy.deepcopy(base_workflow)
    apply_prompts(workflow, row)
    apply_seed(workflow, seed)
    apply_checkpoint(workflow)
    apply_lora(workflow)
    apply_ipadapter_ref(workflow, comfy_url, image_path)
    apply_filename_prefix(workflow, title, index, seed)

    prompt_id = queue_prompt(comfy_url, workflow)
    print(f"[ipadapter_csv_batch] [{index}] title={title!r} seed={seed} ipadapter_ref={image_path.name} 큐 등록 (prompt_id={prompt_id})")
    wait_for_completion(comfy_url, prompt_id)
    print(f"[ipadapter_csv_batch] [{index}] 완료")


def main():
    workflow_path = env("WORKFLOW_PATH")
    csv_path = env("CSV_PATH")
    if not workflow_path or not csv_path:
        print("[ipadapter_csv_batch] WORKFLOW_PATH와 CSV_PATH 환경변수가 모두 필요합니다.", file=sys.stderr)
        sys.exit(1)

    base_workflow = load_workflow(workflow_path)
    rows = load_rows(csv_path)
    if not rows:
        print("[ipadapter_csv_batch] CSV에 처리할 행이 없습니다.")
        return

    comfy_url = env("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
    job_id = env("JOB_ID")
    nightshift_url = env("NIGHTSHIFT_URL", "http://127.0.0.1:8000")

    # 실행 전에 CSV 전체를 미리 훑어 ipadapter_ref 값이 실제 파일로 해석되는지
    # 확인한다 — nightshift가 업로드 시점에 이미 확인했어야 하지만, 그 사이
    # 파일이 지워졌을 수도 있으니 일부만 제출된 채 실패하는 일이 없도록
    # 시작하기 전에 전부 다시 검증한다(pose_csv_batch.py와 같은 정책).
    plan = []  # [(row, title, seed, image_path), ...]
    for line_no, row in enumerate(rows, start=2):
        main_prompt = (row.get("main_prompt") or row.get("prompt") or "").strip()
        if not main_prompt:
            print(f"[ipadapter_csv_batch] 건너뜀 (main_prompt/prompt 없음, {line_no}번째 줄): {row}")
            continue

        ref_name = (row.get("ipadapter_ref") or "").strip()
        if not ref_name:
            print(f"[ipadapter_csv_batch] ipadapter_ref 컬럼이 비어 있습니다 ({line_no}번째 줄).", file=sys.stderr)
            sys.exit(1)
        try:
            image_path = resolve_ipadapter_ref(ref_name)
        except ValueError as e:
            print(f"[ipadapter_csv_batch] {line_no}번째 줄: {e}", file=sys.stderr)
            sys.exit(1)

        title = (row.get("title") or row.get("name") or "").strip()
        row_seed = (row.get("seed") or "").strip()
        seed = int(row_seed) if row_seed else random.randint(0, 2**31 - 1)
        plan.append((row, title, seed, image_path))

    print(f"[ipadapter_csv_batch] 총 {len(plan)}건 제출 예정")
    report_progress(job_id, nightshift_url, len(plan), 0)

    done = 0
    for index, (row, title, seed, image_path) in enumerate(plan, start=1):
        run_once(base_workflow, comfy_url, row, title, seed, index, image_path)
        done += 1
        report_progress(job_id, nightshift_url, len(plan), done)

    print(f"[ipadapter_csv_batch] 총 {len(plan)}건 완료")


if __name__ == "__main__":
    main()
