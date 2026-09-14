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
TextGenerate 노드로 확장)는 ComfyUI 그래프 안에서 전부 처리된다 — 이 스크립트는
그냥 "user_prompt" 노드의 원문 텍스트만 채워 넣으면 된다(seed_batch.py의
apply_main_prompt와 같은 방식이지만 대상 제목이 다르다).

LoRA는 high/low 노이즈 모델 두 갈래에 항상 쌍으로 걸려 있는 WAN2.2 관례를 따라,
LORA_NAME/LORA_STRENGTH를 "user_lora_high"/"user_lora_low" 두 노드 모두에 같이
적용한다(한쪽만 바꾸면 하나는 예전 LoRA로 남아 out-of-sync 상태가 됨).

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
    FAST_4STEP         "on"이면 i2v 워크플로우의 "4-step LoRA 가속" 스위치를 켠다
                       (템플릿 옵션 "fast_4step", 기본 "off" — 워크플로우가 원래
                       기본으로 쓰던 20스텝 고화질 경로를 그대로 유지). flf2v에는
                       이 스위치 자체가 없어 무시된다.
    NIGHTSHIFT_INPUT_IMAGES_DIR  입력 이미지들이 있는 폴더 (기본 NIGHTSHIFT_ASSETS_DIR/input)
    NIGHTSHIFT_ASSETS_DIR  위 override가 없을 때 쓰는 상위 디렉토리 (기본 /workspace/dataset/assets)
    COMFY_URL          ComfyUI 서버 주소 (기본 http://127.0.0.1:8188)
    JOB_ID             nightshift가 주입하는 이 작업의 id (진행 상황 보고/출력 폴더용)
    NIGHTSHIFT_URL     nightshift 자신의 주소 (진행 상황 보고용, 기본 http://127.0.0.1:8000)
    NIGHTSHIFT_API_KEY 설정돼 있으면 진행 상황 보고에 X-API-Key로 실어 보냄 (선택)
    POLL_INTERVAL_SEC  히스토리 폴링 간격 초 (기본 2)
    POLL_TIMEOUT_SEC   개별 작업 완료 대기 제한 초 (기본 1800 — 영상은 이미지보다
                       오래 걸려서 seed_batch류의 기본값(600)보다 넉넉하게 잡음)

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

INPUT_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


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


def wait_for_completion(comfy_url, prompt_id):
    interval = float(env("POLL_INTERVAL_SEC", "2"))
    timeout = float(env("POLL_TIMEOUT_SEC", "1800"))
    deadline = time.time() + timeout
    while time.time() < deadline:
        req = urllib.request.Request(
            f"{comfy_url}/history/{prompt_id}", headers={"User-Agent": COMFY_USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                history = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise RuntimeError(f"ComfyUI 히스토리 조회 실패: {e}") from e
        if prompt_id in history:
            return history[prompt_id]
        time.sleep(interval)
    raise TimeoutError(f"prompt_id={prompt_id} 완료 대기 시간({timeout}s) 초과")


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


def run_once(base_workflow, comfy_url, index, seed, user_prompt, start_image_path, end_image_path,
             lora_name, lora_strength, fast_4step):
    workflow = copy.deepcopy(base_workflow)
    apply_seed(workflow, seed)
    apply_user_prompt(workflow, user_prompt)
    apply_image(workflow, comfy_url, "start_image", start_image_path)
    if end_image_path is not None:
        if not apply_image(workflow, comfy_url, "end_image", end_image_path):
            print("[wan22_video] 경고: 이 워크플로우에는 end_image 노드가 없어 끝 이미지를 넣지 못했습니다.", file=sys.stderr)
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
    lora_name = (env("LORA_NAME", "") or "").strip()
    lora_strength = (env("LORA_STRENGTH", "") or "").strip()
    fast_4step = env("FAST_4STEP")

    base_workflow = load_workflow(workflow_path)
    comfy_url = env("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
    job_id = env("JOB_ID")
    nightshift_url = env("NIGHTSHIFT_URL", "http://127.0.0.1:8000")

    detail = f"시작 이미지: {start_image_name}" + (f", 끝 이미지: {end_image_name}" if end_image_path else "")
    print(f"[wan22_video] 총 {video_count}건 제출 예정 ({detail})")
    report_progress(job_id, nightshift_url, video_count, 0)

    done = 0
    for index in range(1, video_count + 1):
        seed = random.randint(0, 2**31 - 1)
        run_once(
            base_workflow, comfy_url, index, seed, user_prompt,
            start_image_path, end_image_path, lora_name, lora_strength, fast_4step,
        )
        done += 1
        report_progress(job_id, nightshift_url, video_count, done)

    print(f"[wan22_video] 총 {video_count}건 완료")


if __name__ == "__main__":
    main()
