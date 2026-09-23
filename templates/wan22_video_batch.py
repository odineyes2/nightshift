"""
영상 생성(시드 반복) 템플릿 — WAN2.2 i2v/flf2v 워크플로우로 영상 하나를 시드만
바꿔가며 VIDEO_COUNT번 반복 실행한다. seed_batch.py/input_image_batch.py와 같은
ComfyUI 연동 방식(워크플로우 노드 찾기/업로드/제출/폴링/진행률 보고)을 쓴다.

이 템플릿이 쓰는 워크플로우는 사용자가 업로드하는 임의의 파일이 아니라, nightshift가
함께 배포하는 고정 그래프다(templates/video_workflows/wan22_i2v.json,
wan22_flf2v.json — GET /api/video-workflows/{name}). 프론트엔드가 템플릿을 고르는
순간 자동으로 채워 넣으므로, "워크플로우" 첨부 칸을 사용자가 직접 건드릴 일이 없다.
그래서 다른 배치 템플릿과 달리 제목이 모호한(ComfyUI 기본값 "이미지 로드"처럼 여러
노드가 같은 제목을 쓰는) 문제를 겪지 않는다 — 두 워크플로우 모두 이 스크립트가
찾는 노드에 고유한 제목(start_image/end_image/user_prompt/user_lora_high/
user_lora_low/fast_4step)을 미리 붙여뒀다. i2v는 end_image/fast_4step 노드가
아예 없고, flf2v는 fast_4step 노드가 없다 — find_node가 못 찾으면 조용히 건너뛴다
(i2v 작업에 END_IMAGE를 안 주는 것/flf2v에 FAST_4STEP을 주는 것 모두 정상 동작).

두 그래프 다 WAN2.2 특유의 "high-noise 모델 + low-noise 모델" 2단계 샘플링을 쓴다
— KSamplerAdvanced가 두 개(첫 패스 add_noise=enable이 진짜 시드를 갖고, 이어지는
2패스 add_noise=disable은 noise_seed=0 고정) 있는데, 이 스크립트는 항상 먼저
나오는(=add_noise=enable인) 쪽에 시드를 넣는다(find_node가 파일에 먼저 나오는
노드를 우선하도록 두 JSON 모두 순서를 맞춰뒀다).

프롬프트 텍스트 인핸스(user_prompt → 시스템 프롬프트와 합쳐 Krea2 CLIP의
TextGenerate 노드로 확장)는 기본적으로 ComfyUI 그래프 안에서 전부 처리된다 —
"user_prompt" 노드의 원문 텍스트만 채워 넣으면 된다(seed_batch.py의
apply_main_prompt와 같은 방식이지만 대상 제목이 다르다). TEXT_ENHANCE를 "off"로
주면 이 인핸스 체인을 건너뛴다 — 원문 프롬프트를 긍정 프롬프트(CLIPTextEncode,
제목에 "positive prompt" 포함) 노드에 직접 꽂아 넣어서, TextGenerate가 만드는
확장된 프롬프트 대신 사용자가 입력한 문장을 그대로 쓴다(TextGenerate 노드
자체는 계속 실행되지만 그 결과는 이제 아무 데도 연결돼 있지 않아 버려진다 —
그래프를 고쳐 다시 배선하는 대신 결과만 무시하는 쪽이 더 간단하고 안전하다).

**FAST_4STEP은 i2v 전용이다** — flf2v 워크플로우에는 애초에 이 스위치(및 "느린
20스텝 경로"에 해당하는 대체 스텝/CFG 값)가 없다: lightx2v 4-step 가속 LoRA가
무조건 걸려 있는 경로 하나뿐이다(i2v처럼 on/off 두 경로를 고르는 ComfySwitchNode가
없음). 그래서 flf2v 템플릿의 옵션 목록에는 fast_4step이 아예 없고, 혹시 있는
워크플로우 파일로 교체해도 이 스크립트는 "fast_4step" 제목의 노드를 못 찾으면
조용히 건너뛴다(경고 없이 무시).

LoRA는 high/low 노이즈 모델 두 갈래에 항상 쌍으로 걸려 있는 WAN2.2 관례를 따라,
LORA_NAME/LORA_STRENGTH를 "user_lora_high"/"user_lora_low" 두 노드 모두에 같이
적용한다(한쪽만 바꾸면 하나는 예전 LoRA로 남아 out-of-sync 상태가 됨).

영상 해상도(VIDEO_WIDTH/VIDEO_HEIGHT)는 원래 두 워크플로우 다 WanImageToVideo/
WanFirstLastFrameToVideo 노드에 특정 값(i2v 1536×704, flf2v 704×1536)이 그대로
박혀 있었다 — 입력 이미지의 실제 크기와 무관하게 항상 그 값으로 강제 리사이즈됐다는
뜻이다. VIDEO_WIDTH/VIDEO_HEIGHT를 비워두면(기본) 대신 START_IMAGE의 실제 픽셀
크기를 읽어 그 비율 그대로 쓴다(compute_video_dims 참고) — 두 변 모두 16의
배수로 반올림하고(WAN 계열 비디오 모델의 일반적인 정렬 단위), 원본 워크플로우와
같은 총 픽셀 예산(1536×704 ≈ 108만 픽셀)을 넘으면 비율을 유지한 채 축소한다
(작은 이미지를 확대하지는 않는다 — 어차피 WanImageToVideo가 지정한 해상도로
다시 리샘플링하므로 확대해봐야 화질이 좋아지지 않고 VRAM만 더 쓴다).
VIDEO_WIDTH/VIDEO_HEIGHT를 둘 다 채우면 그 값을 그대로 쓴다(자동 계산을
건너뜀) — 하나만 채우면 경고만 남기고 자동 계산으로 돌아간다.

옵션 이름을 "width"/"height"가 아니라 "video_width"/"video_height"로 지은
이유: 프론트엔드가 템플릿 옵션 목록에 "width"와 "height"가 함께 있으면(다른
이미지 생성 템플릿들의 관례) 자동으로 화면비·해상도 프리셋 UI로 묶어버린다
(renderOptionFields의 hasResolutionPair 로직) — 이 프리셋 UI는 항상 값을 채운
채로 시작해서 "비워두면 자동 계산" 동작과 충돌한다. 이름을 다르게 지어 그
자동 병합을 피했다.

환경변수:
    WORKFLOW_PATH      (필수) ComfyUI API 형식 workflow json 경로 (nightshift가 주입 —
                       항상 GET /api/video-workflows/{name}로 받아온 고정 그래프)
    VIDEO_COUNT        (필수) 반복 생성할 영상 개수 (템플릿 옵션 "video_count")
    START_IMAGE        (필수) 시작 이미지 파일명 (템플릿 옵션 "start_image" —
                       NIGHTSHIFT_INPUT_IMAGES_DIR 바로 아래에 있어야 함)
    END_IMAGE          끝 이미지 파일명 (템플릿 옵션 "end_image", flf2v 템플릿에만
                       있음 — i2v 템플릿에서는 이 옵션 자체가 없어 항상 빈 값)
    USER_PROMPT        프롬프트 원문 (템플릿 옵션 "user_prompt", 기본 빈 값 —
                       비워두면 워크플로우에 이미 들어있는 값을 그대로 씀)
    LORA_NAME          LoRA 파일명 (템플릿 옵션 "lora_name", 기본 빈 값 — 비워두면
                       user_lora_high/low에 이미 들어있는 LoRA를 그대로 씀)
    LORA_STRENGTH      LoRA 강도 strength_model (템플릿 옵션 "lora_strength", 기본
                       빈 값 — 비워두면 그대로 씀)
    TEXT_ENHANCE       "off"면 프롬프트 텍스트 인핸스를 끄고 원문을 그대로 쓴다
                       (템플릿 옵션 "text_enhance", 기본 "on")
    FAST_4STEP         "on"이면 i2v 워크플로우의 "4-step LoRA 가속" 스위치를 켠다
                       (템플릿 옵션 "fast_4step", i2v 기본 "on"). flf2v 워크플로우에는
                       이 스위치 자체가 없어(모듈 설명 참고) 옵션 자체가 없고, 값을
                       줘도 조용히 무시된다.
    VIDEO_WIDTH, VIDEO_HEIGHT  영상 해상도 (템플릿 옵션 "video_width"/"video_height",
                       기본 빈 값 — 둘 다 비우면 START_IMAGE의 실제 크기에서 자동
                       계산함, 모듈 설명 참고)
    NIGHTSHIFT_INPUT_IMAGES_DIR  입력 이미지들이 있는 폴더 (기본 NIGHTSHIFT_ASSETS_DIR/input)
    NIGHTSHIFT_ASSETS_DIR  위 override가 없을 때 쓰는 상위 디렉토리 (기본 /workspace/dataset/assets)
    COMFY_URL          ComfyUI 서버 주소 (기본 http://127.0.0.1:8188)
    JOB_ID             nightshift가 주입하는 이 작업의 id (진행 상황 보고/출력 폴더용)
    NIGHTSHIFT_URL     nightshift 자신의 주소 (진행 상황 보고용, 기본 http://127.0.0.1:8000)
    NIGHTSHIFT_API_KEY 설정돼 있으면 진행 상황 보고에 X-API-Key로 실어 보냄 (선택)
    POLL_INTERVAL_SEC  히스토리 폴링 간격 초 (기본 2)
    POLL_MISSING_GRACE_SEC  ComfyUI 대기열/히스토리 양쪽에서 모두 사라진 채로 있어야 실패로
                       치는 유예 초(기본 60) — 오래 걸리는 작업 자체는 시간과 무관하게 기다림

결과물 파일명 규칙:
    SaveVideo 노드의 filename_prefix를 "<JOB_ID>/wan22_video_<순번>_seed<시드값>"로
    채운다 — nightshift 영상 갤러리의 "작업별 보기"가 이 하위 폴더 이름으로 묶는다
    (output_videos.py 참고).
"""

import copy
import io
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from PIL import Image

INPUT_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
# 원본 워크플로우가 원래 쓰던 총 픽셀 수(1536×704) — 자동 계산한 해상도가 이걸
# 넘으면 비율을 유지한 채 줄인다(모듈 설명의 "영상 해상도" 절 참고).
DEFAULT_MAX_VIDEO_PIXELS = 1536 * 704
VIDEO_DIM_MULTIPLE = 16


def env(name, default=None):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def load_workflow(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def input_images_dir():
    override = env("NIGHTSHIFT_INPUT_IMAGES_DIR")
    if override:
        return Path(override)
    assets_dir = env("NIGHTSHIFT_ASSETS_DIR", "/workspace/dataset/assets")
    return Path(assets_dir) / "input"


def find_node(workflow, title_substring=None, class_types=()):
    """title_substring이 있으면 그 부분 문자열을 제목에 포함한 노드를(class_types가
    있으면 그중에서도 class_type이 맞는 것만) 우선 찾고, 없으면 class_types에 속한
    첫 번째 노드를 돌려준다. 이 템플릿이 쓰는 워크플로우는 사용자 업로드가 아니라
    이 스크립트와 함께 관리되는 고정 그래프라(모듈 설명 참고), 각 제목이 그래프
    안에서 유일해서 seed_batch.py류가 하는 "실제로 배선됐는지" 판별까지는 필요 없다."""
    title_substring = (title_substring or "").lower()
    fallback = None
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        if class_types and node.get("class_type") not in class_types:
            continue
        meta_title = str(node.get("_meta", {}).get("title", "")).lower()
        if title_substring and title_substring in meta_title:
            return node_id, node
        if not title_substring and fallback is None:
            fallback = (node_id, node)
    return fallback if fallback else (None, None)


def apply_seed(workflow, seed):
    node_id, node = find_node(workflow, title_substring="KSampler", class_types=("KSampler", "KSamplerAdvanced"))
    if node is None:
        print("[wan22_video] 경고: 시드를 넣을 노드를 찾지 못했습니다.", file=sys.stderr)
        return
    field = "noise_seed" if node.get("class_type") == "KSamplerAdvanced" else "seed"
    node.setdefault("inputs", {})[field] = seed


def apply_user_prompt(workflow, prompt):
    prompt = (prompt or "").strip()
    if not prompt:
        return  # 비워두면 워크플로우에 이미 들어있는 프롬프트를 그대로 둔다.
    node_id, node = find_node(workflow, title_substring="user_prompt")
    if node is None:
        print("[wan22_video] 경고: user_prompt 노드를 찾지 못했습니다.", file=sys.stderr)
        return
    node.setdefault("inputs", {})["value"] = prompt


def apply_text_enhance(workflow, prompt, enabled):
    if enabled:
        return  # 기본 동작 — TextGenerate 인핸스 체인을 그대로 둔다.
    prompt = (prompt or "").strip()
    if not prompt:
        return  # 끄더라도 대신 넣을 원문이 없으면(USER_PROMPT 비움) 켠 것과 동일하게 둔다.
    node_id, node = find_node(workflow, title_substring="positive prompt", class_types=("CLIPTextEncode",))
    if node is None:
        print("[wan22_video] 경고: 긍정 프롬프트 노드를 찾지 못해 텍스트 인핸스를 끄지 못했습니다.", file=sys.stderr)
        return
    node.setdefault("inputs", {})["text"] = prompt


def compute_video_dims(image_path, max_pixels=DEFAULT_MAX_VIDEO_PIXELS, multiple=VIDEO_DIM_MULTIPLE):
    """image_path의 실제 픽셀 크기를 읽어 영상 해상도로 쓸 (width, height)를 계산한다.
    비율은 유지하되, 총 픽셀 수가 max_pixels를 넘으면(넘을 때만) 축소하고, 두 변
    모두 multiple의 배수로 반올림한다(WAN 계열 비디오 모델이 흔히 요구하는 정렬
    단위). 작은 이미지를 확대하지는 않는다 — 어차피 WanImageToVideo류 노드가
    지정한 해상도로 다시 리샘플링하므로 확대해봐야 화질 이득이 없다."""
    with Image.open(image_path) as img:
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
        print("[wan22_video] 경고: 영상 해상도를 넣을 노드를 찾지 못했습니다.", file=sys.stderr)
        return
    inputs = node.setdefault("inputs", {})
    inputs["width"] = width
    inputs["height"] = height


def resolve_video_dims(width_raw, height_raw, start_image_path):
    """VIDEO_WIDTH/VIDEO_HEIGHT 옵션과 시작 이미지로 최종 (width, height)를 정한다.
    둘 다 채워졌으면 그 값을 그대로 쓰고, 하나만 채워졌으면 경고 후 자동 계산으로
    돌아간다(다른 템플릿들의 "width/height는 둘 다 채워야 적용됨" 규칙과 동일)."""
    width_raw = (width_raw or "").strip() if isinstance(width_raw, str) else width_raw
    height_raw = (height_raw or "").strip() if isinstance(height_raw, str) else height_raw
    has_width = width_raw not in (None, "")
    has_height = height_raw not in (None, "")
    if has_width and has_height:
        try:
            return int(float(width_raw)), int(float(height_raw))
        except (TypeError, ValueError):
            print(f"[wan22_video] 경고: VIDEO_WIDTH/VIDEO_HEIGHT 값 '{width_raw}x{height_raw}'을 정수로 변환하지 못했습니다 — 자동 계산으로 대체합니다.", file=sys.stderr)
    elif has_width or has_height:
        print("[wan22_video] 경고: VIDEO_WIDTH/VIDEO_HEIGHT는 둘 다 채워야 적용됩니다 (하나만 비어 있음) — 자동 계산으로 대체합니다.", file=sys.stderr)

    try:
        dims = compute_video_dims(start_image_path)
    except Exception as e:
        print(f"[wan22_video] 경고: 시작 이미지 크기를 읽지 못해 해상도를 자동 계산하지 못했습니다: {e}", file=sys.stderr)
        return None
    return dims


def apply_lora(workflow, lora_name, lora_strength):
    if not lora_name and not lora_strength:
        return
    for title in ("user_lora_high", "user_lora_low"):
        node_id, node = find_node(workflow, title_substring=title, class_types=("LoraLoaderModelOnly",))
        if node is None:
            continue  # 있을 수 없는 경우지만(고정 그래프), 방어적으로 건너뛴다.
        inputs = node.setdefault("inputs", {})
        if lora_name:
            inputs["lora_name"] = lora_name
        if lora_strength:
            try:
                inputs["strength_model"] = float(lora_strength)
            except ValueError:
                print(f"[wan22_video] 경고: LORA_STRENGTH 값 '{lora_strength}'을 숫자로 변환하지 못했습니다.", file=sys.stderr)


def apply_fast_4step(workflow, raw):
    if raw is None:
        return
    node_id, node = find_node(workflow, title_substring="fast_4step")
    if node is None:
        return  # flf2v 워크플로우에는 이 스위치가 없다 — 정상.
    node.setdefault("inputs", {})["value"] = (str(raw).strip().lower() == "on")


def apply_filename_prefix(workflow, index, seed):
    node_id, node = find_node(workflow, class_types=("SaveVideo",))
    if node is None:
        return
    prefix = f"wan22_video_{index}_seed{seed}"
    job_id = env("JOB_ID")
    if job_id:
        prefix = f"{job_id}/{prefix}"
    node.setdefault("inputs", {})["filename_prefix"] = prefix


# RunPod의 프록시 주소는 Cloudflare가 앞단에 있는데, Cloudflare의 봇 차단이 파이썬
# urllib 기본 User-Agent("Python-urllib/3.x")를 403으로 막는다 — ComfyUI로 보내는
# 요청에는 전부 이 헤더를 실어 보낸다(값은 흔한 브라우저 UA면 충분하다).
COMFY_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ComfyUI에 이미 올린 이미지의 (키 -> ComfyUI가 돌려준 파일명) 캐시. 같은 시작/끝
# 이미지를 VIDEO_COUNT번 반복해서 올리지 않으려고 실행 단위로 캐시한다.
_uploaded_image_cache = {}


def upload_image_to_comfy(comfy_url, image_path):
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
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": COMFY_USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    uploaded_name = result["name"]
    if cache_key is not None:
        _uploaded_image_cache[cache_key] = uploaded_name
    return uploaded_name


def apply_image(workflow, comfy_url, title, image_path):
    node_id, node = find_node(workflow, title_substring=title, class_types=("LoadImage",))
    if node is None:
        return False
    uploaded_name = upload_image_to_comfy(comfy_url, image_path)
    node.setdefault("inputs", {})["image"] = uploaded_name
    return True


def queue_prompt(comfy_url, workflow):
    payload = json.dumps({"prompt": workflow, "client_id": str(uuid.uuid4())}).encode("utf-8")
    req = urllib.request.Request(
        f"{comfy_url}/prompt",
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": COMFY_USER_AGENT},
        method="POST",
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
        req = urllib.request.Request(
            f"{nightshift_url}/api/jobs/{job_id}/progress",
            data=payload,
            headers=headers,
            method="PUT",
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"[wan22_video] 경고: 진행 상황 보고 실패: {e}", file=sys.stderr)


def resolve_input_image(name, label):
    if not name:
        print(f"[wan22_video] {label} 파일명이 비어 있습니다.", file=sys.stderr)
        sys.exit(1)
    path = input_images_dir() / name
    if not path.is_file() or path.suffix.lower() not in INPUT_IMAGE_EXTENSIONS:
        print(f"[wan22_video] {label} '{name}'를 찾을 수 없습니다 ({path}).", file=sys.stderr)
        sys.exit(1)
    return path


def run_once(base_workflow, comfy_url, index, seed, user_prompt, text_enhance, start_image_path, end_image_path,
             lora_name, lora_strength, fast_4step, video_dims):
    workflow = copy.deepcopy(base_workflow)
    apply_seed(workflow, seed)
    apply_user_prompt(workflow, user_prompt)
    apply_text_enhance(workflow, user_prompt, text_enhance)
    apply_image(workflow, comfy_url, "start_image", start_image_path)
    if end_image_path is not None:
        if not apply_image(workflow, comfy_url, "end_image", end_image_path):
            print("[wan22_video] 경고: 이 워크플로우에는 end_image 노드가 없어 끝 이미지를 넣지 못했습니다.", file=sys.stderr)
    if video_dims is not None:
        apply_video_size(workflow, *video_dims)
    apply_lora(workflow, lora_name, lora_strength)
    apply_fast_4step(workflow, fast_4step)
    apply_filename_prefix(workflow, index, seed)

    prompt_id = queue_prompt(comfy_url, workflow)
    print(f"[wan22_video] [{index}] seed={seed} 큐 등록 (prompt_id={prompt_id})")
    wait_for_completion(comfy_url, prompt_id)
    print(f"[wan22_video] [{index}] 완료")


def main():
    workflow_path = env("WORKFLOW_PATH")
    video_count_raw = env("VIDEO_COUNT")
    start_image_name = env("START_IMAGE")
    if not workflow_path or not video_count_raw or not start_image_name:
        print("[wan22_video] WORKFLOW_PATH, VIDEO_COUNT, START_IMAGE 환경변수가 모두 필요합니다.", file=sys.stderr)
        sys.exit(1)

    video_count = int(video_count_raw)
    start_image_path = resolve_input_image(start_image_name, "시작 이미지")

    end_image_name = env("END_IMAGE", "")
    end_image_path = resolve_input_image(end_image_name, "끝 이미지") if end_image_name else None

    user_prompt = env("USER_PROMPT", "")
    text_enhance = (env("TEXT_ENHANCE", "on") or "on").strip().lower() != "off"
    lora_name = (env("LORA_NAME", "") or "").strip()
    lora_strength = (env("LORA_STRENGTH", "") or "").strip()
    fast_4step = env("FAST_4STEP")
    video_dims = resolve_video_dims(env("VIDEO_WIDTH"), env("VIDEO_HEIGHT"), start_image_path)

    base_workflow = load_workflow(workflow_path)
    comfy_url = env("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
    job_id = env("JOB_ID")
    nightshift_url = env("NIGHTSHIFT_URL", "http://127.0.0.1:8000")

    detail = f"시작 이미지: {start_image_name}" + (f", 끝 이미지: {end_image_name}" if end_image_path else "")
    detail += f", 해상도: {video_dims[0]}x{video_dims[1]}" if video_dims else " (해상도는 워크플로우 값 그대로)"
    print(f"[wan22_video] 총 {video_count}건 제출 예정 ({detail})")
    report_progress(job_id, nightshift_url, video_count, 0)

    done = 0
    for index in range(1, video_count + 1):
        seed = random.randint(0, 2**31 - 1)
        run_once(
            base_workflow, comfy_url, index, seed, user_prompt, text_enhance,
            start_image_path, end_image_path, lora_name, lora_strength, fast_4step, video_dims,
        )
        done += 1
        report_progress(job_id, nightshift_url, video_count, done)

    print(f"[wan22_video] 총 {video_count}건 완료")


if __name__ == "__main__":
    main()
