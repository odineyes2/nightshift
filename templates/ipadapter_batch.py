"""
IPAdapter 배치 템플릿 — "IPAdapter" 워크플로우 유형(프리셋 기반)에 쓴다. IPAdapter는
체크포인트마다 IPAdapter 모델/CLIP Vision 모델 배선이 달라 이 서버가 자동으로
조립하지 못하므로, ControlNet(OpenPose/Depth/Lineart)과 마찬가지로 family별로
관리자가 미리 만들어둔 워크플로우 프리셋을 그대로 쓴다(app.py의 workflow_presets,
"🎛 모델" 탭 참고) — 이 템플릿은 그 프리셋에 참조 이미지(스타일/캐릭터 일관성을
줄 사진 한 장)를 주입하고 image_count번 반복 실행하며 매번 시드만 바꾼다.

평평한(세트/char_no 계층 없는) 참조 이미지 목록에서 파일 하나를 골라 LoadImage
노드에 주입한다 — input_image_batch.py와 완전히 같은 저장소(input_assets.py,
NIGHTSHIFT_INPUT_IMAGES_DIR)를 공유하지만, 주입 대상 노드 제목이 다르다("input_image"
대신 "ipadapter_ref") — 같은 워크플로우 안에 img2img 입력 이미지와 IPAdapter 참조
이미지가 동시에 있어도(관리자가 그렇게 프리셋을 구성했다면) 서로 다른 LoadImage
노드에 독립적으로 주입되도록 제목을 분리했다.

여러 참조 이미지를 CSV 행마다 서로 다르게 쓰는 경우는 ipadapter_csv_batch
템플릿을 쓴다.

ComfyUI로의 이미지 주입 방식:
    pose_batch.py/input_image_batch.py와 동일 — nightshift와 ComfyUI가 파일시스템을
    공유한다는 보장이 없으므로, ComfyUI의 POST /upload/image API로 업로드하고
    응답으로 받은 파일명을 LoadImage 노드의 image 입력에 그대로 넣는다.

    같은 파일은 이 실행 안에서 한 번만 올리고, 이후에는 그때 받은 파일명을 재사용한다
    (ComfyUI가 원격 pod에 있으면 이 차이가 그대로 실행 시간이 된다).

메인 프롬프트(MAIN_PROMPT):
    비워두면(기본값) 업로드한 워크플로우 JSON에 이미 들어있는 프롬프트를 그대로
    쓴다. 값을 채우면 워크플로우에서 제목에 "main_prompt"가 포함된 노드 하나를
    찾아 그 값을 덮어쓴다 — pose_batch.py와 완전히 같은 규칙(apply_main_prompt).

환경변수:
    WORKFLOW_PATH      (필수) ComfyUI API 형식 workflow json 경로 (nightshift가 주입)
    IMAGE_COUNT        (필수) 반복 생성할 이미지 개수 (nightshift가 템플릿 옵션 "image_count"로 주입)
    IPADAPTER_REF      (필수) IPAdapter 참조 이미지 파일명 (nightshift가 템플릿 옵션
                       "ipadapter_ref"로 주입)
    MAIN_PROMPT        메인 프롬프트 (nightshift가 템플릿 옵션 "main_prompt"로 주입, 기본
                       빈 값 — 비워두면 워크플로우의 프롬프트를 그대로 씀)
    NIGHTSHIFT_INPUT_IMAGES_DIR  참조 이미지들이 있는 폴더 (기본
                       NIGHTSHIFT_ASSETS_DIR/input, input_image_batch.py와 공유).
                       nightshift 서버(input_assets.py)와 같은 값을 봐야 하므로
                       손대지 않는 게 안전함
    NIGHTSHIFT_ASSETS_DIR  위 override가 없을 때 쓰는 상위 디렉토리 (기본 /workspace/dataset/assets)
    NIGHTSHIFT_OUTPUT_DIR  ComfyUI가 이미지를 저장하는 폴더 (기본 /workspace/output).
                       재현성 기록용 jsonl(ipadapter_batch_manifest.jsonl)을 여기 같이 남긴다
    CHECKPOINT         체크포인트 파일명 (nightshift가 템플릿 옵션 "checkpoint"로 주입, 기본 빈 값 —
                       비워두면 워크플로우에 들어있는 체크포인트를 그대로 씀. 같은 family
                       안의 다른 체크포인트로 바꿀 때만 씀 — IPAdapter/CLIP Vision 모델
                       자체는 프리셋에 이미 배선돼 있어 여기서 건드리지 않는다)
    LORA_NAME          LoRA 파일명 (템플릿 옵션 "lora_name", 기본 빈 값 — 비워두면 그대로 씀)
    LORA_STRENGTH      LoRA 강도 strength_model (템플릿 옵션 "lora_strength", 기본 빈 값 —
                       비워두면 그대로 씀). strength_clip은 건드리지 않는다.
    CHECKPOINT_NODE_TITLE  체크포인트를 주입할 노드의 _meta.title 부분일치 (기본 없음)
    LORA_NODE_TITLE    LoRA를 주입할 노드의 _meta.title 부분일치 (기본 없음)
    COMFY_URL          ComfyUI 서버 주소 (기본 http://127.0.0.1:8188)
    JOB_ID             nightshift가 주입하는 이 작업의 id (진행 상황 보고용, 없으면 보고 생략)
    NIGHTSHIFT_URL     nightshift 자신의 주소 (진행 상황 보고용, 기본 http://127.0.0.1:8000)
    NIGHTSHIFT_API_KEY 설정돼 있으면 진행 상황 보고에 X-API-Key로 실어 보냄 (선택)
    SEED_NODE_TITLE    시드를 주입할 노드의 _meta.title 부분일치 (기본 "KSampler")
    IPADAPTER_NODE_TITLE  참조 이미지를 주입할 LoadImage 노드의 _meta.title 부분일치
                       (기본 "ipadapter_ref" — 정확히 일치하는 노드가 없으면, 워크플로우에
                       LoadImage 노드가 하나뿐일 때 그 노드를 대신 쓴다)
    SAVE_NODE_TITLE    파일명 접두사를 주입할 노드의 _meta.title 부분일치 (기본 "Save")
    POLL_INTERVAL_SEC  히스토리 폴링 간격 초 (기본 2)
    POLL_TIMEOUT_SEC   개별 작업 완료 대기 제한 초 (기본 600)

진행 상황 보고:
    image_count를 그대로 예상 총 이미지 수로 보고 PUT /api/jobs/{job_id}/progress로
    {"total": N, "done": M}을 보고한다.

재현성 기록:
    매 반복마다 어떤 참조 이미지를 썼는지 stdout 로그에 남기고, SaveImage의
    filename_prefix에도 참조 이미지 파일명(확장자 제외, 안전한 문자로 치환)을
    포함시킨다(예: ipadapter_batch_3_seed482913_char01.png). JOB_ID가 있으면 이
    filename_prefix 앞에 "<JOB_ID>/"를 붙인다(작업별 갤러리 보기용). 추가로
    NIGHTSHIFT_OUTPUT_DIR에 ipadapter_batch_manifest.jsonl을 이어쓰기로 남긴다.
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

IPADAPTER_REF_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def env(name, default=None):
    return os.environ.get(name, default)


def load_workflow(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


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


def sanitize_stem(text):
    text = re.sub(r"[^\w\-가-힣]+", "_", text or "")
    return text[:40] or "ipadapter_ref"


def input_images_dir():
    override = env("NIGHTSHIFT_INPUT_IMAGES_DIR")
    if override:
        return Path(override)
    assets_dir = env("NIGHTSHIFT_ASSETS_DIR", "/workspace/dataset/assets")
    return Path(assets_dir) / "input"


# ComfyUI 연동 ----------------------------------------------------------------

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


def apply_ipadapter_ref(workflow, comfy_url, image_path):
    node_id, node = find_node(
        workflow,
        title_substring=env("IPADAPTER_NODE_TITLE", "ipadapter_ref"),
        class_types=("LoadImage",),
        prefer_connected=True,
    )
    if node is None:
        print("[ipadapter_batch] 경고: 참조 이미지를 넣을 노드를 찾지 못했습니다 (LoadImage 없음)", file=sys.stderr)
        return
    uploaded_name = upload_image_to_comfy(comfy_url, image_path)
    node.setdefault("inputs", {})["image"] = uploaded_name


def apply_seed(workflow, seed):
    node_id, node = find_node(
        workflow,
        title_substring=env("SEED_NODE_TITLE", "KSampler"),
        class_types=("KSampler", "KSamplerAdvanced"),
    )
    if node is None:
        print("[ipadapter_batch] 경고: 시드를 넣을 노드를 찾지 못했습니다 (KSampler 없음)", file=sys.stderr)
        return
    node.setdefault("inputs", {})["seed"] = seed


def apply_main_prompt(workflow, main_prompt):
    main_prompt = (main_prompt or "").strip()
    if not main_prompt:
        return
    node_id, node = find_node(workflow, title_substring="main_prompt")
    field = primitive_value_field(node) if node is not None else None
    if field is None:
        print(
            "[ipadapter_batch] 경고: MAIN_PROMPT를 넣을 노드를 찾지 못했습니다 "
            "(제목에 'main_prompt'가 포함된 CLIPTextEncode/Primitive 텍스트 노드 없음)",
            file=sys.stderr,
        )
        return
    node.setdefault("inputs", {})[field] = main_prompt


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
        print("[ipadapter_batch] 경고: 체크포인트를 넣을 노드를 찾지 못했습니다 (CheckpointLoader 없음)", file=sys.stderr)
        return
    set_linked_value(workflow, node, "ckpt_name", ckpt)


def apply_lora(workflow):
    lora = (env("LORA_NAME", "") or "").strip()
    strength = (env("LORA_STRENGTH", "") or "").strip()
    if not lora and not strength:
        return
    node_id, node = find_loader_node(workflow, env("LORA_NODE_TITLE", ""), LORA_CLASS_TYPES)
    if node is None:
        print("[ipadapter_batch] 경고: LoRA를 넣을 노드를 찾지 못했습니다 (LoraLoader 없음)", file=sys.stderr)
        return
    total = sum(
        1 for n in workflow.values()
        if isinstance(n, dict) and n.get("class_type") in LORA_CLASS_TYPES
    )
    if total > 1:
        print(
            f"[ipadapter_batch] 안내: LoRA 로더가 {total}개라 그중 노드 {node_id}에만 적용했습니다 "
            "(다른 노드에 넣으려면 LORA_NODE_TITLE로 지정하세요).",
            file=sys.stderr,
        )
    if lora:
        set_linked_value(workflow, node, "lora_name", lora)
    if strength:
        try:
            strength_value = float(strength)
        except ValueError:
            print(f"[ipadapter_batch] 경고: LORA_STRENGTH 값 '{strength}'을 숫자로 변환하지 못했습니다.", file=sys.stderr)
        else:
            set_linked_value(workflow, node, "strength_model", strength_value)


def apply_filename_prefix(workflow, index, seed, image_path):
    node_id, node = find_node(
        workflow,
        title_substring=env("SAVE_NODE_TITLE", "Save"),
        class_types=("SaveImage", "SaveImageWebsocket"),
    )
    if node is None:
        return
    prefix = f"ipadapter_batch_{index}_seed{seed}_{sanitize_stem(image_path.stem)}"
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
        print(f"[ipadapter_batch] 경고: 진행 상황 보고 실패: {e}", file=sys.stderr)


def append_manifest(output_dir, record):
    try:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        path = Path(output_dir) / "ipadapter_batch_manifest.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[ipadapter_batch] 경고: 재현성 기록 실패: {e}", file=sys.stderr)


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


def run_once(base_workflow, comfy_url, seed, image_path, index, job_id, output_dir, main_prompt):
    workflow = copy.deepcopy(base_workflow)
    apply_seed(workflow, seed)
    apply_checkpoint(workflow)
    apply_lora(workflow)
    apply_main_prompt(workflow, main_prompt)
    apply_ipadapter_ref(workflow, comfy_url, image_path)
    apply_filename_prefix(workflow, index, seed, image_path)

    prompt_id = queue_prompt(comfy_url, workflow)
    print(f"[ipadapter_batch] [{index}] seed={seed} ipadapter_ref={image_path.name} 큐 등록 (prompt_id={prompt_id})")
    wait_for_completion(comfy_url, prompt_id)
    print(f"[ipadapter_batch] [{index}] 완료")

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "job_id": job_id,
        "index": index,
        "ipadapter_ref": image_path.name,
        "seed": seed,
        "prompt_id": prompt_id,
    }
    append_manifest(output_dir, record)


def main():
    workflow_path = env("WORKFLOW_PATH")
    image_count_raw = env("IMAGE_COUNT")
    ref_name = env("IPADAPTER_REF")
    if not workflow_path or not image_count_raw or not ref_name:
        print("[ipadapter_batch] WORKFLOW_PATH, IMAGE_COUNT, IPADAPTER_REF 환경변수가 모두 필요합니다.", file=sys.stderr)
        sys.exit(1)

    image_count = int(image_count_raw)
    main_prompt = env("MAIN_PROMPT", "")
    output_dir = env("NIGHTSHIFT_OUTPUT_DIR", "/workspace/output")

    image_path = input_images_dir() / ref_name
    if not image_path.is_file() or image_path.suffix.lower() not in IPADAPTER_REF_EXTENSIONS:
        # nightshift가 업로드 시점에 이미 확인했어야 하지만, 그 사이 파일이
        # 지워졌을 수도 있으니 실행 시점에도 한 번 더 확인한다.
        print(f"[ipadapter_batch] 참조 이미지 '{ref_name}'를 찾을 수 없습니다 ({image_path})", file=sys.stderr)
        sys.exit(1)

    base_workflow = load_workflow(workflow_path)
    comfy_url = env("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
    job_id = env("JOB_ID")
    nightshift_url = env("NIGHTSHIFT_URL", "http://127.0.0.1:8000")

    print(f"[ipadapter_batch] 총 {image_count}건 제출 예정 (참조 이미지: {ref_name})")
    report_progress(job_id, nightshift_url, image_count, 0)

    done = 0
    for index in range(1, image_count + 1):
        seed = random.randint(0, 2**31 - 1)
        run_once(base_workflow, comfy_url, seed, image_path, index, job_id, output_dir, main_prompt)
        done += 1
        report_progress(job_id, nightshift_url, image_count, done)

    print(f"[ipadapter_batch] 총 {image_count}건 완료")


if __name__ == "__main__":
    main()
