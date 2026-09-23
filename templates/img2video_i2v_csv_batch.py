"""
이미지→영상 복합 CSV 배치 템플릿 — CSV 한 행마다 (1) 사용자가 올린 이미지 생성
워크플로우로 이미지를 하나 생성하고, (2) 그 결과 이미지를 로컬 디스크를 거치지
않고 ComfyUI 서버 안에서 바로 WAN2.2 i2v 영상 워크플로우의 시작 이미지로 넘겨
영상을 만든다. 이미지 생성 단계의 프롬프트/노드 매칭 로직은 csv_batch.py와,
영상 생성 단계는 wan22_video_csv_batch.py와 각각 같다(템플릿 자기완결성
컨벤션에 따라 그대로 복사했다 — 셋 중 하나를 고치면 나머지도 맞춰 고쳐야 한다).

이미지→영상 연계 방법:
    이미지 생성 결과를 /history/{prompt_id}에서 받은 output 파일 정보(filename/
    subfolder/type)로 찾아 ComfyUI의 /view로 그 바이트를 그대로 받아온 뒤,
    /upload/image로 같은 ComfyUI 서버에 다시 올려 영상 워크플로우의 LoadImage
    노드(start_image)에 꽂는다. nightshift의 입력 이미지 풀
    (NIGHTSHIFT_INPUT_IMAGES_DIR)을 전혀 거치지 않는다 — 생성한 이미지이 그
    자리에서 바로 영상 시작 프레임이 된다.

    영상 해상도는 방금 받은 이미지 바이트의 실제 크기에서 자동 계산한다
    (wan22_video_csv_batch.py의 compute_video_dims와 같은 규칙 — 최대 픽셀수를
    넘을 때만 축소, 16의 배수로 반올림). 따로 영상 width/height 컬럼은 두지
    않았다 — 이미지 생성 단계의 width/height/resolution 컬럼으로 생성되는
    이미지의 해상도를 정하면, 그 결과가 그대로 영상 해상도 계산의 입력이 되므로
    사실상 영상 해상도도 함께 정해지는 셈이다.

만화 선화(lineart) 안전장치 (자동, line_safety 옵션으로 끌 수 있음, 기본 on):
    이미지 생성 결과가 영상의 시작 프레임이 되므로, 동작을 표현하려고 넣는
    집중선/강조선/겹선 같은 만화적 왜곡이 있으면 영상 전체에 그 왜곡이 깔린
    정지 잔상처럼 보인다. LINE_SAFETY가 켜져 있으면(기본) 매 행의
    negative_prompt에 AUTO_NEGATIVE_SAFETY_TAGS를, main_prompt(또는 prompt)에
    AUTO_POSITIVE_SAFETY_TAGS를 자동으로 덧붙인다(이미 같은 표현이 있으면
    중복 추가하지 않는다).

CSV 컬럼:
    title           결과 파일명 접두사 (선택, name도 허용)
    main_prompt(=prompt)   이미지 생성 프롬프트 (필수 — 비면 그 행 전체를 건너뜀)
    trigger_prompt, negative_prompt
                    이미지 생성 보조 프롬프트 (선택, csv_batch.py와 동일한
                    컬럼/매칭 규칙 — 값이 있는데 매칭되는 노드가 없으면 경고만
                    찍고 그 필드만 건너뛴다. 행 처리나 큐 자체는 멈추지 않음)
    quality_prompt  이미지 생성 부가 프롬프트 (선택 — 위와 동일하게 있어도 되고
                    없어도 되는 부가 컬럼. csv_batch.py와의 컬럼 호환을 위해
                    남겨둔 것일 뿐, 이 템플릿에서 필수로 요구하는 컬럼이 아니다)
    seed            이미지 생성 단계 시드 (선택, 비어 있으면 무작위)
    width, height   이미지 생성 해상도 (선택, 둘 다 채워야 적용)
    resolution      이미지 생성 해상도 프리셋/WxH (선택, width/height 없을 때만)
    user_prompt(=video_prompt)
                    영상 생성 프롬프트 (선택 — 비어 있으면 main_prompt/prompt를
                    그대로 재사용한다. video_prompt는 예전 컬럼명과의 호환을
                    위해 계속 받아준다)
    video_seed      영상 생성 시드 (선택, 비어 있으면 무작위)

    위 컬럼들 외에 CSV에 다른 컬럼이 더 있어도 그냥 무시된다 — 그것 때문에
    행이 건너뛰어지거나 큐 처리가 멈추는 일은 없다.

    영상 해상도는 위/아래 설명대로 항상 시작 이미지(방금 생성한 이미지)의
    실제 크기에서 자동 계산되며, 별도 컬럼으로 지정할 수 없다.

환경변수:
    WORKFLOW_PATH   (필수) 이미지 생성 워크플로우 json 경로 (nightshift가 주입,
                    사용자가 화면에서 올린 파일)
    VIDEO_WORKFLOW_UPLOAD_PATH
                    (선택) 영상 생성 워크플로우 json 경로 — nightshift 화면의
                    "영상 생성 워크플로우" 첨부 칸에 사용자가 파일을 올렸을 때만
                    주입된다. 안 올리면 이 환경변수 자체가 없고, 아래 "번들 영상
                    워크플로우" 절의 내장 기본값을 대신 쓴다.
    CSV_PATH        (필수)
    CHECKPOINT, LORA_NAME, LORA_STRENGTH
                    이미지 생성 단계 모델 옵션 (csv_batch.py와 동일)
    VIDEO_LORA_NAME, VIDEO_LORA_STRENGTH
                    영상 생성 단계 LoRA (user_lora_high/user_lora_low에 적용)
    TEXT_ENHANCE    영상 프롬프트 텍스트 인핸스 on/off (기본 on)
    FAST_4STEP      영상 4-step LoRA 가속 on/off (기본 on)
    LINE_SAFETY     만화 선화 안전장치 on/off (기본 on)
    COMFY_URL, JOB_ID, NIGHTSHIFT_URL, NIGHTSHIFT_API_KEY,
    POLL_INTERVAL_SEC, POLL_MISSING_GRACE_SEC   나머지 템플릿들과 동일

번들 영상 워크플로우:
    영상 단계는 기본적으로 이 스크립트가 저장소에 고정으로 들어있는
    templates/video_workflows/wan22_i2v.json을 직접 읽어서 쓴다 — 사용자가
    화면에서 영상 생성 워크플로우를 따로 올리지 않아도 된다는 뜻이다. 다만
    nightshift 화면의 "영상 생성 워크플로우" 첨부 칸(선택)에 직접 워크플로우를
    올리면 VIDEO_WORKFLOW_UPLOAD_PATH로 그 파일 경로가 주입되고, 이 스크립트는
    그걸 번들 기본값 대신 쓴다 — 즉 이미지 생성/영상 생성 워크플로우를 각각
    독립적으로 올릴 수 있다. 사용자가 올린 영상 워크플로우에도 start_image/
    user_prompt/fast_4step/user_lora_high/user_lora_low 등 이 스크립트가 title로
    찾는 노드 제목은 그대로 있어야 한다(templates/video_workflows/wan22_i2v.json을
    참고해서 만들 것).

결과물 파일명 규칙:
    이미지: "<JOB_ID>/<title>_img_<순번>_seed<이미지시드>"
    영상:   "<JOB_ID>/<title>_video_<순번>_seed<영상시드>"
    둘 다 같은 JOB_ID 폴더 밑에 저장되므로(이미지는 이미지 갤러리, 영상은 영상
    갤러리에) "작업별 보기"로 함께 묶여 보인다.
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
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from PIL import Image

VIDEO_WORKFLOW_PATH = Path(__file__).resolve().parent / "video_workflows" / "wan22_i2v.json"
DEFAULT_MAX_VIDEO_PIXELS = 1536 * 704
VIDEO_DIM_MULTIPLE = 16

AUTO_NEGATIVE_SAFETY_TAGS = (
    "speed lines, motion lines, emphasis lines, focus lines, action lines, "
    "impact lines, duplicate lineart, rough lineart, messy lineart, "
    "multiple outlines, motion blur lines"
)
AUTO_POSITIVE_SAFETY_TAGS = "clean lineart, sharp lineart, smooth lineart"

# ── 이미지 생성 단계 (csv_batch.py와 동일, 자기완결성 컨벤션) ────────────
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
    value = os.environ.get(name)
    return value if value not in (None, "") else default


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
    text = (text or fallback or "img2video").strip()
    text = re.sub(r"[^\w\-가-힣 ]+", "_", text)
    return text[:80] or fallback


def apply_line_safety(row, enabled):
    """LINE_SAFETY가 켜져 있으면(기본) 이미지 생성 프롬프트에 만화 선화 안전
    태그를 자동으로 덧붙인 새 dict을 돌려준다(원본 row는 건드리지 않음). 이미
    같은 표현이 들어있으면 중복 추가하지 않는다."""
    if not enabled:
        return row
    row = dict(row)
    for field in ("main_prompt", "prompt"):
        value = (row.get(field) or "").strip()
        if value and AUTO_POSITIVE_SAFETY_TAGS not in value:
            row[field] = f"{value}, {AUTO_POSITIVE_SAFETY_TAGS}"
    negative = (row.get("negative_prompt") or "").strip()
    if AUTO_NEGATIVE_SAFETY_TAGS not in negative:
        row["negative_prompt"] = f"{negative}, {AUTO_NEGATIVE_SAFETY_TAGS}" if negative else AUTO_NEGATIVE_SAFETY_TAGS
    return row


def apply_prompts(workflow, row):
    applied = False
    for field, title_substring in PROMPT_FIELD_TITLES.items():
        value = (row.get(field) or "").strip()
        if not value:
            continue
        node_id, node = find_node(workflow, title_substring=title_substring, allow_class_fallback=False)
        input_field = primitive_value_field(node) if node is not None else None
        if input_field is None:
            print(f"[img2video_i2v_csv] 경고: '{field}' 값을 넣을 노드를 찾지 못했습니다.", file=sys.stderr)
            continue
        node.setdefault("inputs", {})[input_field] = value
        applied = True
    if not applied:
        print("[img2video_i2v_csv] 경고: 이 행에 이미지 생성 프롬프트 컬럼 값이 하나도 없습니다.", file=sys.stderr)


def apply_seed(workflow, seed):
    node_id, node = find_node(workflow, title_substring="KSampler", class_types=("KSampler", "KSamplerAdvanced"))
    if node is None:
        print("[img2video_i2v_csv] 경고: 이미지 생성 시드를 넣을 노드를 찾지 못했습니다.", file=sys.stderr)
        return
    try:
        node.setdefault("inputs", {})["seed"] = int(seed)
    except (TypeError, ValueError):
        print(f"[img2video_i2v_csv] 경고: seed 값 '{seed}'을 정수로 변환하지 못했습니다.", file=sys.stderr)


def resolve_resolution(width, height, resolution):
    width = (width or "").strip()
    height = (height or "").strip()
    resolution = (resolution or "").strip()
    if width and height:
        try:
            return int(width), int(height)
        except ValueError:
            print(
                f"[img2video_i2v_csv] 경고: width/height 값 '{width}x{height}'을 정수로 변환하지 못했습니다. "
                "resolution 컬럼으로 대체합니다.", file=sys.stderr,
            )
    elif width or height:
        print(
            "[img2video_i2v_csv] 경고: width/height는 둘 다 채워야 적용됩니다. resolution 컬럼으로 대체합니다.",
            file=sys.stderr,
        )
    if not resolution:
        return None
    preset = RESOLUTION_PRESETS.get(resolution.lower())
    if preset:
        return preset
    match = re.match(r"^(\d+)\s*[xX]\s*(\d+)$", resolution)
    if not match:
        print(f"[img2video_i2v_csv] 경고: resolution 값 '{resolution}'을 해석하지 못했습니다.", file=sys.stderr)
        return None
    return int(match.group(1)), int(match.group(2))


def apply_resolution(workflow, width, height, resolution):
    resolved = resolve_resolution(width, height, resolution)
    if resolved is None:
        return
    node_id, node = find_node(workflow, title_substring="latent", class_types=("EmptyLatentImage",), prefer_connected=True)
    if node is None:
        print("[img2video_i2v_csv] 경고: 이미지 해상도를 넣을 노드를 찾지 못했습니다.", file=sys.stderr)
        return
    width_value, height_value = resolved
    set_linked_value(workflow, node, "width", width_value)
    set_linked_value(workflow, node, "height", height_value)


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
    node_id, node = find_loader_node(workflow, "", CHECKPOINT_CLASS_TYPES)
    if node is None:
        print("[img2video_i2v_csv] 경고: 체크포인트를 넣을 노드를 찾지 못했습니다.", file=sys.stderr)
        return
    set_linked_value(workflow, node, "ckpt_name", ckpt)


def apply_lora(workflow):
    lora = (env("LORA_NAME", "") or "").strip()
    strength = (env("LORA_STRENGTH", "") or "").strip()
    if not lora and not strength:
        return
    node_id, node = find_loader_node(workflow, "", LORA_CLASS_TYPES)
    if node is None:
        print("[img2video_i2v_csv] 경고: 이미지 생성 LoRA를 넣을 노드를 찾지 못했습니다.", file=sys.stderr)
        return
    if lora:
        set_linked_value(workflow, node, "lora_name", lora)
    if strength:
        try:
            set_linked_value(workflow, node, "strength_model", float(strength))
        except ValueError:
            print(f"[img2video_i2v_csv] 경고: LORA_STRENGTH 값 '{strength}'을 숫자로 변환하지 못했습니다.", file=sys.stderr)


def apply_image_filename_prefix(workflow, title, suffix, index, seed):
    node_id, node = find_node(workflow, title_substring="Save", class_types=("SaveImage", "SaveImageWebsocket"))
    if node is None:
        return
    prefix = sanitize_prefix(title, f"img2video_{index}")
    prefix = f"{prefix}_{suffix}_seed{seed}"
    job_id = env("JOB_ID")
    if job_id:
        prefix = f"{job_id}/{prefix}"
    node.setdefault("inputs", {})["filename_prefix"] = prefix


# ── 영상 생성 단계 (wan22_video_csv_batch.py와 동일) ─────────────────────

def resolve_video_prompt(row):
    """영상 생성에 쓸 프롬프트를 결정한다: user_prompt(=video_prompt)가 있으면
    그 값을, 비어 있으면 이미지 생성에 쓴 main_prompt/prompt를 그대로
    재사용한다."""
    value = (row.get("user_prompt") or row.get("video_prompt") or "").strip()
    if value:
        return value
    return (row.get("main_prompt") or row.get("prompt") or "").strip()


def apply_video_user_prompt(workflow, prompt):
    prompt = (prompt or "").strip()
    if not prompt:
        return
    node_id, node = find_node(workflow, title_substring="user_prompt")
    if node is None:
        print("[img2video_i2v_csv] 경고: user_prompt 노드를 찾지 못했습니다.", file=sys.stderr)
        return
    node.setdefault("inputs", {})["value"] = prompt


def apply_video_seed(workflow, seed):
    node_id, node = find_node(workflow, title_substring="KSampler", class_types=("KSampler", "KSamplerAdvanced"))
    if node is None:
        print("[img2video_i2v_csv] 경고: 영상 시드를 넣을 노드를 찾지 못했습니다.", file=sys.stderr)
        return
    field = "noise_seed" if node.get("class_type") == "KSamplerAdvanced" else "seed"
    node.setdefault("inputs", {})[field] = seed


def apply_video_lora(workflow, lora_name, lora_strength):
    if not lora_name and not lora_strength:
        return
    for title in ("user_lora_high", "user_lora_low"):
        node_id, node = find_node(workflow, title_substring=title, class_types=("LoraLoaderModelOnly",))
        if node is None:
            continue
        inputs = node.setdefault("inputs", {})
        if lora_name:
            inputs["lora_name"] = lora_name
        if lora_strength:
            try:
                inputs["strength_model"] = float(lora_strength)
            except ValueError:
                print(f"[img2video_i2v_csv] 경고: VIDEO_LORA_STRENGTH 값 '{lora_strength}'을 숫자로 변환하지 못했습니다.", file=sys.stderr)


def apply_text_enhance(workflow, prompt, enabled):
    if enabled:
        return
    prompt = (prompt or "").strip()
    if not prompt:
        return
    node_id, node = find_node(workflow, title_substring="positive prompt", class_types=("CLIPTextEncode",))
    if node is None:
        print("[img2video_i2v_csv] 경고: 긍정 프롬프트 노드를 찾지 못해 텍스트 인핸스를 끄지 못했습니다.", file=sys.stderr)
        return
    node.setdefault("inputs", {})["text"] = prompt


def apply_fast_4step(workflow, raw):
    if raw is None:
        return
    node_id, node = find_node(workflow, title_substring="fast_4step")
    if node is None:
        return
    node.setdefault("inputs", {})["value"] = (str(raw).strip().lower() == "on")


def compute_video_dims_from_bytes(image_bytes, max_pixels=DEFAULT_MAX_VIDEO_PIXELS, multiple=VIDEO_DIM_MULTIPLE):
    with Image.open(io.BytesIO(image_bytes)) as img:
        width, height = img.size
    if width <= 0 or height <= 0:
        return None
    scale = 1.0
    if width * height > max_pixels:
        scale = (max_pixels / (width * height)) ** 0.5
    w = max(multiple, round(width * scale / multiple) * multiple)
    h = max(multiple, round(height * scale / multiple) * multiple)
    return w, h


def apply_video_size(workflow, width, height):
    node_id, node = find_node(workflow, class_types=("WanImageToVideo", "WanFirstLastFrameToVideo"))
    if node is None:
        print("[img2video_i2v_csv] 경고: 영상 해상도를 넣을 노드를 찾지 못했습니다.", file=sys.stderr)
        return
    inputs = node.setdefault("inputs", {})
    inputs["width"] = width
    inputs["height"] = height


def apply_video_filename_prefix(workflow, title, index, seed):
    node_id, node = find_node(workflow, class_types=("SaveVideo",))
    if node is None:
        return
    prefix = sanitize_prefix(title, f"img2video_{index}")
    prefix = f"{prefix}_video_seed{seed}"
    job_id = env("JOB_ID")
    if job_id:
        prefix = f"{job_id}/{prefix}"
    node.setdefault("inputs", {})["filename_prefix"] = prefix


COMFY_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def queue_prompt(comfy_url, workflow):
    payload = json.dumps({"prompt": workflow, "client_id": str(uuid.uuid4())}).encode("utf-8")
    req = urllib.request.Request(
        f"{comfy_url}/prompt", data=payload,
        headers={"Content-Type": "application/json", "User-Agent": COMFY_USER_AGENT}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "error" in data:
        raise RuntimeError(f"ComfyUI가 프롬프트를 거부했습니다: {data['error']}")
    return data["prompt_id"]


def _prompt_in_comfy_queue(comfy_url, prompt_id):
    # ComfyUI 자체 대기열(실행 중+대기 중)에 이 prompt_id가 아직 있는지 본다. /queue
    # 조회 자체가 잠깐 실패한 거면 "없어졌다"고 단정하지 않는다(다음 폴링에서 다시 본다).
    req = urllib.request.Request(f"{comfy_url}/queue", headers={"User-Agent": COMFY_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            q = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError):
        return True
    ids = {item[1] for item in q.get("queue_running", []) + q.get("queue_pending", []) if len(item) > 1}
    return prompt_id in ids


def wait_for_completion(comfy_url, prompt_id):
    interval = float(env("POLL_INTERVAL_SEC", "2"))
    # 이 프롬프트보다 먼저 넣은 다른 작업(특히 오래 걸리는 영상 생성)이 ComfyUI 자체
    # 대기열에서 아직 돌고 있으면, 이 프롬프트는 몇십 분이고 자기 차례를 기다릴 수
    # 있다 — 그 대기는 정상이라 시간으로 실패 처리하면 안 된다(대기열은 밤새 혼자
    # 돌아가라고 있는 것이므로). 그래서 "얼마나 기다렸나"가 아니라 "ComfyUI가 이
    # 프롬프트를 아직 알고 있나"로 판단한다 — /history에 결과가 뜰 때까지, 또는
    # /queue(대기·실행 중)에 남아 있는 동안은 계속 기다리고, 실패는 ComfyUI가 실제로
    # status_str="error"를 돌려줬을 때만 처리한다. 대기열과 히스토리 양쪽에서 모두
    # 사라진 채(예: ComfyUI가 재시작돼 큐를 잃어버림) POLL_MISSING_GRACE_SEC를 넘기면
    # 그때만 "잃어버림"으로 실패 처리한다.
    missing_grace = float(env("POLL_MISSING_GRACE_SEC", "60"))
    missing_since = None
    while True:
        req = urllib.request.Request(
            f"{comfy_url}/history/{prompt_id}", headers={"User-Agent": COMFY_USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                history = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise RuntimeError(f"ComfyUI 히스토리 조회 실패: {e}") from e
        if prompt_id in history:
            entry = history[prompt_id]
            status = (entry.get("status") or {}).get("status_str")
            if status == "error":
                messages = (entry.get("status") or {}).get("messages") or []
                raise RuntimeError(f"ComfyUI가 이 작업을 실패로 처리했어요(prompt_id={prompt_id}): {messages}")
            return entry
        if _prompt_in_comfy_queue(comfy_url, prompt_id):
            missing_since = None
        else:
            if missing_since is None:
                missing_since = time.time()
            elif time.time() - missing_since > missing_grace:
                raise TimeoutError(
                    f"prompt_id={prompt_id}가 ComfyUI 대기열과 히스토리 어디에도 없어요"
                    f"({missing_grace:.0f}초 동안 확인) — ComfyUI가 재시작됐을 수 있어요.")
        time.sleep(interval)


def report_progress(job_id, nightshift_url, total, done):
    if not job_id or not nightshift_url:
        return
    try:
        payload = json.dumps({"total": total, "done": done}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        api_key = os.environ.get("NIGHTSHIFT_API_KEY", "").strip()
        if api_key:
            headers["X-API-Key"] = api_key
        req = urllib.request.Request(f"{nightshift_url}/api/jobs/{job_id}/progress", data=payload, headers=headers, method="PUT")
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"[img2video_i2v_csv] 경고: 진행 상황 보고 실패: {e}", file=sys.stderr)


IMAGE_OUTPUT_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff")


def find_first_output_image(history_result):
    """history_result에서 이미지 확장자를 가진 첫 출력 파일을 찾는다. "images" 키
    안에 이미지가 아닌 파일(.mp4 등)이 들어있는 경우도 있어서(예: 영상 워크플로우를
    이미지 생성 워크플로우 칸에 잘못 올렸을 때) 확장자까지 확인한다 — 그래야
    나중에 PIL이 그 바이트를 못 열어서 알아보기 힘든 에러를 내는 대신, 여기서
    바로 원인을 짚어주는 에러를 낼 수 있다. 이미지가 하나도 없으면 (None, None,
    None, 발견된 비이미지 파일명 목록)을 돌려준다."""
    outputs = history_result.get("outputs", {}) if isinstance(history_result, dict) else {}
    non_image_filenames = []
    for node_output in outputs.values():
        images = node_output.get("images") if isinstance(node_output, dict) else None
        for img in images or []:
            filename = img.get("filename") or ""
            if filename.lower().endswith(IMAGE_OUTPUT_EXTENSIONS):
                return filename, img.get("subfolder", ""), img.get("type", "output"), non_image_filenames
            non_image_filenames.append(filename)
    return None, None, None, non_image_filenames


def fetch_image_bytes(comfy_url, filename, subfolder, type_):
    query = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": type_})
    req = urllib.request.Request(f"{comfy_url}/view?{query}", headers={"User-Agent": COMFY_USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def upload_image_bytes_to_comfy(comfy_url, filename, image_bytes):
    boundary = uuid.uuid4().hex
    body = io.BytesIO()

    def write(chunk):
        body.write(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)

    write(f"--{boundary}\r\n")
    write(f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n')
    write("Content-Type: application/octet-stream\r\n\r\n")
    write(image_bytes)
    write("\r\n")
    write(f"--{boundary}\r\n")
    write('Content-Disposition: form-data; name="overwrite"\r\n\r\n')
    write("true\r\n")
    write(f"--{boundary}--\r\n")

    req = urllib.request.Request(
        f"{comfy_url}/upload/image", data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": COMFY_USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    return result["name"]


def generate_image(image_workflow, comfy_url, row, title, index, suffix, seed):
    """이미지 생성 한 건을 돌리고, 결과 이미지를 ComfyUI 서버 안에서 바로 받아와
    재업로드한 뒤 (재업로드된 파일명, 원본 이미지 바이트)를 돌려준다."""
    workflow = copy.deepcopy(image_workflow)
    apply_prompts(workflow, row)
    apply_seed(workflow, seed)
    apply_checkpoint(workflow)
    apply_lora(workflow)
    apply_resolution(workflow, row.get("width"), row.get("height"), row.get("resolution"))
    apply_image_filename_prefix(workflow, title, suffix, index, seed)

    prompt_id = queue_prompt(comfy_url, workflow)
    print(f"[img2video_i2v_csv] [{index}] {suffix} 이미지 생성 큐 등록 (prompt_id={prompt_id})")
    history_result = wait_for_completion(comfy_url, prompt_id)
    filename, subfolder, type_, non_image_filenames = find_first_output_image(history_result)
    if filename is None:
        if non_image_filenames:
            raise RuntimeError(
                f"[{index}] {suffix} 이미지 생성 결과에 이미지 파일이 없고 다른 형식만 있습니다: "
                f"{non_image_filenames} — 여기 올린 '이미지 생성 워크플로우'가 실제로는 영상(.mp4 등)을 "
                "출력하고 있는 것 같습니다. 이미지 생성 단계에는 이미지를 저장(SaveImage)하는 워크플로우를 "
                "올려야 합니다 (영상 단계 워크플로우는 nightshift가 내장한 것을 자동으로 씁니다)."
            )
        raise RuntimeError(f"[{index}] {suffix} 이미지 생성 결과에서 출력 이미지를 찾지 못했습니다.")
    image_bytes = fetch_image_bytes(comfy_url, filename, subfolder, type_)
    uploaded_name = upload_image_bytes_to_comfy(comfy_url, filename, image_bytes)
    print(f"[img2video_i2v_csv] [{index}] {suffix} 이미지 생성 완료 → {uploaded_name}")
    return uploaded_name, image_bytes


def run_once(image_workflow, video_workflow, comfy_url, row, title, index, image_seed, video_seed,
             video_lora_name, video_lora_strength, text_enhance, fast_4step):
    uploaded_name, image_bytes = generate_image(image_workflow, comfy_url, row, title, index, "img", image_seed)

    video = copy.deepcopy(video_workflow)
    node_id, node = find_node(video, title_substring="start_image", class_types=("LoadImage",))
    if node is None:
        raise RuntimeError("영상 워크플로우에 start_image 노드가 없습니다.")
    node.setdefault("inputs", {})["image"] = uploaded_name

    video_dims = compute_video_dims_from_bytes(image_bytes)
    if video_dims is not None:
        apply_video_size(video, *video_dims)

    video_prompt = resolve_video_prompt(row)
    apply_video_user_prompt(video, video_prompt)
    apply_text_enhance(video, video_prompt, text_enhance)
    apply_video_seed(video, video_seed)
    apply_video_lora(video, video_lora_name, video_lora_strength)
    apply_fast_4step(video, fast_4step)
    apply_video_filename_prefix(video, title, index, video_seed)

    prompt_id = queue_prompt(comfy_url, video)
    print(f"[img2video_i2v_csv] [{index}] 영상 생성 큐 등록 (prompt_id={prompt_id})")
    wait_for_completion(comfy_url, prompt_id)
    print(f"[img2video_i2v_csv] [{index}] 영상 생성 완료")


def main():
    workflow_path = env("WORKFLOW_PATH")
    csv_path = env("CSV_PATH")
    if not workflow_path or not csv_path:
        print("[img2video_i2v_csv] WORKFLOW_PATH와 CSV_PATH 환경변수가 모두 필요합니다.", file=sys.stderr)
        sys.exit(1)

    # 영상 생성 워크플로우는 사용자가 직접 올렸으면(VIDEO_WORKFLOW_UPLOAD_PATH,
    # nightshift 화면의 "영상 생성 워크플로우" 첨부 칸) 그걸 쓰고, 안 올렸으면
    # 지금까지처럼 번들 기본값(VIDEO_WORKFLOW_PATH)을 쓴다.
    video_workflow_upload_path = env("VIDEO_WORKFLOW_UPLOAD_PATH")
    if video_workflow_upload_path:
        video_workflow_path = Path(video_workflow_upload_path)
        if not video_workflow_path.is_file():
            print(f"[img2video_i2v_csv] 업로드된 영상 워크플로우를 찾지 못했습니다: {video_workflow_path}", file=sys.stderr)
            sys.exit(1)
    else:
        video_workflow_path = VIDEO_WORKFLOW_PATH
        if not video_workflow_path.is_file():
            print(f"[img2video_i2v_csv] 번들 영상 워크플로우를 찾지 못했습니다: {video_workflow_path}", file=sys.stderr)
            sys.exit(1)

    image_workflow = load_workflow(workflow_path)
    video_workflow = load_workflow(video_workflow_path)
    rows = load_rows(csv_path)
    if not rows:
        print("[img2video_i2v_csv] CSV에 처리할 행이 없습니다.")
        return

    line_safety = (env("LINE_SAFETY", "on") or "on").strip().lower() != "off"
    video_lora_name = (env("VIDEO_LORA_NAME", "") or "").strip()
    video_lora_strength = (env("VIDEO_LORA_STRENGTH", "") or "").strip()
    text_enhance = (env("TEXT_ENHANCE", "on") or "on").strip().lower() != "off"
    fast_4step = env("FAST_4STEP", "on")
    comfy_url = env("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
    job_id = env("JOB_ID")
    nightshift_url = env("NIGHTSHIFT_URL", "http://127.0.0.1:8000")

    plan = []
    for line_no, row in enumerate(rows, start=2):
        main_prompt = (row.get("main_prompt") or row.get("prompt") or "").strip()
        if not main_prompt:
            print(f"[img2video_i2v_csv] 건너뜀 (main_prompt/prompt 없음, {line_no}번째 줄): {row}")
            continue
        row = apply_line_safety(row, line_safety)
        title = (row.get("title") or row.get("name") or "").strip()
        image_seed_raw = (row.get("seed") or "").strip()
        image_seed = int(image_seed_raw) if image_seed_raw else random.randint(0, 2**31 - 1)
        video_seed_raw = (row.get("video_seed") or "").strip()
        video_seed = int(video_seed_raw) if video_seed_raw else random.randint(0, 2**31 - 1)
        plan.append((row, title, image_seed, video_seed))

    print(f"[img2video_i2v_csv] 총 {len(plan)}건 제출 예정")
    report_progress(job_id, nightshift_url, len(plan), 0)

    done = 0
    for index, (row, title, image_seed, video_seed) in enumerate(plan, start=1):
        run_once(image_workflow, video_workflow, comfy_url, row, title, index, image_seed, video_seed,
                 video_lora_name, video_lora_strength, text_enhance, fast_4step)
        done += 1
        report_progress(job_id, nightshift_url, len(plan), done)

    print(f"[img2video_i2v_csv] 총 {len(plan)}건 완료")


if __name__ == "__main__":
    main()
