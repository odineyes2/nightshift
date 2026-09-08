"""
입력 이미지 배치 템플릿 — img2img/USDU(Ultimate SD Upscale) 워크플로우 유형에 쓴다.
평평한(세트/char_no 계층 없는) 입력 이미지 목록에서 파일 하나를 골라 LoadImage
노드에 주입한 뒤, 워크플로우 하나를 image_count번 반복 실행하며 매번 시드만 바꾼다
(예: 한 그림을 원본으로 여러 스타일 변형을 뽑을 때). seed_batch.py와 같은 ComfyUI
연동 방식(워크플로우 노드 찾기/제출/폴링/진행률 보고)을 쓰되, 시드뿐 아니라 입력
이미지도 함께 주입한다.

여러 입력 이미지를 CSV 행마다 서로 다르게 쓰는 경우는 input_image_csv_batch
템플릿을 쓴다.

입력 이미지 폴더 구조 (계층 없음, ref_assets.py의 pose/depth/lineart와 다름):
    NIGHTSHIFT_INPUT_IMAGES_DIR(기본 NIGHTSHIFT_ASSETS_DIR/input, 그 기본값은
    /workspace/dataset/assets/input)/
        image1.png
        image2.jpg
        ...
    nightshift 웹 UI의 "입력 이미지" 드롭다운이 이 폴더를 평평하게 나열해서
    보여준다(GET /api/input-images). 존재 여부는 업로드 시점에 nightshift
    (app.py + input_assets.py)가 이미 검증했으므로 큐 시작 이후 그것 때문에
    실패하는 일은 없지만, 실행 중 파일이 지워지는 등의 만일의 상황을 대비해
    이 스크립트도 시작할 때 한 번 더 확인한다.

ComfyUI로의 이미지 주입 방식:
    pose_batch.py와 동일 — nightshift와 ComfyUI가 파일시스템을 공유한다는 보장이
    없으므로, 매번 ComfyUI의 POST /upload/image API로 업로드하고 응답으로 받은
    파일명을 LoadImage 노드의 image 입력에 그대로 넣는다.

메인 프롬프트(MAIN_PROMPT):
    비워두면(기본값) 업로드한 워크플로우 JSON에 이미 들어있는 프롬프트를 그대로
    쓴다. 값을 채우면 워크플로우에서 제목에 "main_prompt"가 포함된 노드 하나를
    찾아 그 값을 덮어쓴다 — pose_batch.py와 완전히 같은 규칙(apply_main_prompt).

해상도(WIDTH/HEIGHT)를 다루지 않는 이유:
    workflow_builder.py가 img2img/usdu 모드로 만드는 워크플로우에는 애초에
    EmptyLatentImage 노드가 없다(이미지 크기는 입력 이미지 자체를 따름). 그래서
    이 템플릿은 다른 배치 템플릿과 달리 WIDTH/HEIGHT 옵션 자체를 두지 않는다
    (매번 "노드를 찾지 못했습니다" 경고만 찍힐 게 뻔해서).

환경변수:
    WORKFLOW_PATH      (필수) ComfyUI API 형식 workflow json 경로 (nightshift가 주입)
    IMAGE_COUNT        (필수) 반복 생성할 이미지 개수 (nightshift가 템플릿 옵션 "image_count"로 주입)
    INPUT_IMAGE        (필수) 입력 이미지 파일명 (nightshift가 템플릿 옵션 "input_image"로 주입)
    MAIN_PROMPT        메인 프롬프트 (nightshift가 템플릿 옵션 "main_prompt"로 주입, 기본
                       빈 값 — 비워두면 워크플로우의 프롬프트를 그대로 씀)
    NIGHTSHIFT_INPUT_IMAGES_DIR  입력 이미지들이 있는 폴더 (기본
                       NIGHTSHIFT_ASSETS_DIR/input). nightshift 서버(input_assets.py)와
                       같은 값을 봐야 하므로 손대지 않는 게 안전함
    NIGHTSHIFT_ASSETS_DIR  위 override가 없을 때 쓰는 상위 디렉토리 (기본 /workspace/dataset/assets)
    NIGHTSHIFT_OUTPUT_DIR  ComfyUI가 이미지를 저장하는 폴더 (기본 /workspace/output).
                       재현성 기록용 jsonl(input_image_batch_manifest.jsonl)을 여기 같이 남긴다
    CHECKPOINT         체크포인트 파일명 (nightshift가 템플릿 옵션 "checkpoint"로 주입, 기본 빈 값 —
                       비워두면 워크플로우에 들어있는 체크포인트를 그대로 씀)
    LORA_NAME          LoRA 파일명 (템플릿 옵션 "lora_name", 기본 빈 값 — 비워두면 그대로 씀)
    LORA_STRENGTH      LoRA 강도 strength_model (템플릿 옵션 "lora_strength", 기본 빈 값 —
                       비워두면 그대로 씀). strength_clip은 건드리지 않는다.
    CHECKPOINT_NODE_TITLE  체크포인트를 주입할 노드의 _meta.title 부분일치 (기본 없음)
    LORA_NODE_TITLE    LoRA를 주입할 노드의 _meta.title 부분일치 (기본 없음)
    COMFY_URL          ComfyUI 서버 주소 (기본 http://127.0.0.1:8188)
    JOB_ID             nightshift가 주입하는 이 작업의 id (진행 상황 보고용, 없으면 보고 생략)
    NIGHTSHIFT_URL     nightshift 자신의 주소 (진행 상황 보고용, 기본 http://127.0.0.1:8000)
    SEED_NODE_TITLE    시드를 주입할 노드의 _meta.title 부분일치 (기본 "KSampler")
    INPUT_IMAGE_NODE_TITLE  입력 이미지를 주입할 LoadImage 노드의 _meta.title 부분일치
                       (기본 "input_image" — workflow_builder.py가 img2img/usdu 모드로
                       만드는 워크플로우가 이 제목을 쓴다. 정확히 일치하는 노드가 없으면,
                       워크플로우에 LoadImage 노드가 하나뿐일 때 그 노드를 대신 쓴다)
    SAVE_NODE_TITLE    파일명 접두사를 주입할 노드의 _meta.title 부분일치 (기본 "Save")
    POLL_INTERVAL_SEC  히스토리 폴링 간격 초 (기본 2)
    POLL_TIMEOUT_SEC   개별 작업 완료 대기 제한 초 (기본 600)

진행 상황 보고:
    image_count를 그대로 예상 총 이미지 수로 보고 PUT /api/jobs/{job_id}/progress로
    {"total": N, "done": M}을 보고한다.

재현성 기록:
    매 반복마다 어떤 입력 이미지를 썼는지 stdout 로그에 남기고, SaveImage의
    filename_prefix에도 입력 이미지 파일명(확장자 제외, 안전한 문자로 치환)을
    포함시킨다(예: input_image_batch_3_seed482913_photo01.png). JOB_ID가 있으면 이
    filename_prefix 앞에 "<JOB_ID>/"를 붙인다(작업별 갤러리 보기용). 추가로
    NIGHTSHIFT_OUTPUT_DIR에 input_image_batch_manifest.jsonl을 이어쓰기로 남긴다.
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

INPUT_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


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
    return text[:40] or "input_image"


def input_images_dir():
    override = env("NIGHTSHIFT_INPUT_IMAGES_DIR")
    if override:
        return Path(override)
    assets_dir = env("NIGHTSHIFT_ASSETS_DIR", "/workspace/dataset/assets")
    return Path(assets_dir) / "input"


# ComfyUI 연동 ----------------------------------------------------------------

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


def apply_input_image(workflow, comfy_url, image_path):
    node_id, node = find_node(
        workflow,
        title_substring=env("INPUT_IMAGE_NODE_TITLE", "input_image"),
        class_types=("LoadImage",),
        prefer_connected=True,
    )
    if node is None:
        print("[input_image_batch] 경고: 입력 이미지를 넣을 노드를 찾지 못했습니다 (LoadImage 없음)", file=sys.stderr)
        return
    uploaded_name = upload_image_to_comfy(comfy_url, image_path)
    node.setdefault("inputs", {})["image"] = uploaded_name


def apply_seed(workflow, seed):
    # USDU 모드(workflow_builder.py)는 시드가 KSampler가 아니라
    # UltimateSDUpscaleNoUpscale 노드의 "seed" 입력에 있으므로 후보에 포함한다
    # (그 노드가 없는 일반 img2img 워크플로우면 그냥 무시됨).
    node_id, node = find_node(
        workflow,
        title_substring=env("SEED_NODE_TITLE", "KSampler"),
        class_types=("KSampler", "KSamplerAdvanced", "UltimateSDUpscaleNoUpscale", "UltimateSDUpscale"),
    )
    if node is None:
        print("[input_image_batch] 경고: 시드를 넣을 노드를 찾지 못했습니다 (KSampler/UltimateSDUpscale 없음)", file=sys.stderr)
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
            "[input_image_batch] 경고: MAIN_PROMPT를 넣을 노드를 찾지 못했습니다 "
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
        print("[input_image_batch] 경고: 체크포인트를 넣을 노드를 찾지 못했습니다 (CheckpointLoader 없음)", file=sys.stderr)
        return
    set_linked_value(workflow, node, "ckpt_name", ckpt)


def apply_lora(workflow):
    lora = (env("LORA_NAME", "") or "").strip()
    strength = (env("LORA_STRENGTH", "") or "").strip()
    if not lora and not strength:
        return
    node_id, node = find_loader_node(workflow, env("LORA_NODE_TITLE", ""), LORA_CLASS_TYPES)
    if node is None:
        print("[input_image_batch] 경고: LoRA를 넣을 노드를 찾지 못했습니다 (LoraLoader 없음)", file=sys.stderr)
        return
    total = sum(
        1 for n in workflow.values()
        if isinstance(n, dict) and n.get("class_type") in LORA_CLASS_TYPES
    )
    if total > 1:
        print(
            f"[input_image_batch] 안내: LoRA 로더가 {total}개라 그중 노드 {node_id}에만 적용했습니다 "
            "(다른 노드에 넣으려면 LORA_NODE_TITLE로 지정하세요).",
            file=sys.stderr,
        )
    if lora:
        set_linked_value(workflow, node, "lora_name", lora)
    if strength:
        try:
            strength_value = float(strength)
        except ValueError:
            print(f"[input_image_batch] 경고: LORA_STRENGTH 값 '{strength}'을 숫자로 변환하지 못했습니다.", file=sys.stderr)
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
    prefix = f"input_image_batch_{index}_seed{seed}_{sanitize_stem(image_path.stem)}"
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
        print(f"[input_image_batch] 경고: 진행 상황 보고 실패: {e}", file=sys.stderr)


def append_manifest(output_dir, record):
    try:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        path = Path(output_dir) / "input_image_batch_manifest.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[input_image_batch] 경고: 재현성 기록 실패: {e}", file=sys.stderr)


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
    apply_input_image(workflow, comfy_url, image_path)
    apply_filename_prefix(workflow, index, seed, image_path)

    prompt_id = queue_prompt(comfy_url, workflow)
    print(f"[input_image_batch] [{index}] seed={seed} input_image={image_path.name} 큐 등록 (prompt_id={prompt_id})")
    wait_for_completion(comfy_url, prompt_id)
    print(f"[input_image_batch] [{index}] 완료")

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "job_id": job_id,
        "index": index,
        "input_image": image_path.name,
        "seed": seed,
        "prompt_id": prompt_id,
    }
    append_manifest(output_dir, record)


def main():
    workflow_path = env("WORKFLOW_PATH")
    image_count_raw = env("IMAGE_COUNT")
    input_image_name = env("INPUT_IMAGE")
    if not workflow_path or not image_count_raw or not input_image_name:
        print("[input_image_batch] WORKFLOW_PATH, IMAGE_COUNT, INPUT_IMAGE 환경변수가 모두 필요합니다.", file=sys.stderr)
        sys.exit(1)

    image_count = int(image_count_raw)
    main_prompt = env("MAIN_PROMPT", "")
    output_dir = env("NIGHTSHIFT_OUTPUT_DIR", "/workspace/output")

    image_path = input_images_dir() / input_image_name
    if not image_path.is_file() or image_path.suffix.lower() not in INPUT_IMAGE_EXTENSIONS:
        # nightshift가 업로드 시점에 이미 확인했어야 하지만, 그 사이 파일이
        # 지워졌을 수도 있으니 실행 시점에도 한 번 더 확인한다.
        print(f"[input_image_batch] 입력 이미지 '{input_image_name}'를 찾을 수 없습니다 ({image_path})", file=sys.stderr)
        sys.exit(1)

    base_workflow = load_workflow(workflow_path)
    comfy_url = env("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
    job_id = env("JOB_ID")
    nightshift_url = env("NIGHTSHIFT_URL", "http://127.0.0.1:8000")

    print(f"[input_image_batch] 총 {image_count}건 제출 예정 (입력 이미지: {input_image_name})")
    report_progress(job_id, nightshift_url, image_count, 0)

    done = 0
    for index in range(1, image_count + 1):
        seed = random.randint(0, 2**31 - 1)
        run_once(base_workflow, comfy_url, seed, image_path, index, job_id, output_dir, main_prompt)
        done += 1
        report_progress(job_id, nightshift_url, image_count, done)

    print(f"[input_image_batch] 총 {image_count}건 완료")


if __name__ == "__main__":
    main()
