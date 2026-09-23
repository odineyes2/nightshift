"""
영상 생성(CSV 배치) 템플릿 — wan22_video_batch.py의 시드 반복 대신, CSV 행마다
다른 프롬프트/시작·끝 이미지/시드로 WAN2.2 i2v/flf2v 영상을 만든다. 워크플로우
찾기/주입/ComfyUI 연동 로직은 wan22_video_batch.py와 완전히 같다(템플릿 자기완결성
컨벤션에 따라 그대로 복사했다 — 고치면 두 파일 다 같이 고쳐야 한다).

CSV 컬럼:
    title           결과 파일명 접두사로 쓰일 제목 (선택, name도 허용)
    user_prompt     프롬프트 원문 (prompt도 동일하게 취급, 둘 중 하나는 있어야 함)
    prompt          user_prompt가 없을 때 쓰이는 대체 컬럼 (선택)
    seed            지정하면 그 값을 시드로 사용, 비어 있으면 랜덤 시드 1개
    start_image     (필수, 모든 행) 시작 이미지 파일명 — NIGHTSHIFT_INPUT_IMAGES_DIR
                    바로 아래에 있어야 한다.
    end_image       flf2v 워크플로우일 때만 필수(워크플로우에 end_image 노드가
                    있는지로 자동 판단 — i2v면 이 컬럼 자체를 무시한다)
    width, height   영상 해상도 (선택, 둘 다 채워야 적용됨) — 비워두면(기본) 그 행의
                    start_image 실제 크기에서 행마다 자동 계산한다(wan22_video_batch.py
                    모듈 설명의 "영상 해상도" 절 참고 — compute_video_dims를 그대로
                    복사해 씀). 원래 두 워크플로우 다 특정 값(i2v 1536×704, flf2v
                    704×1536)이 고정돼 있어 입력 이미지 크기와 무관하게 강제
                    리사이즈됐던 것을 이걸로 대체한다.

환경변수:
    WORKFLOW_PATH, CSV_PATH  (둘 다 필수, nightshift가 주입)
    LORA_NAME, LORA_STRENGTH, FAST_4STEP  wan22_video_batch.py와 같음(모든 행에
                    공통 적용 — CSV 행마다 다르게 쓰려면 나중에 컬럼으로 확장하면 됨)
    NIGHTSHIFT_INPUT_IMAGES_DIR, NIGHTSHIFT_ASSETS_DIR, COMFY_URL, JOB_ID,
    NIGHTSHIFT_URL, NIGHTSHIFT_API_KEY, POLL_INTERVAL_SEC, POLL_MISSING_GRACE_SEC
                    wan22_video_batch.py와 같음

실행 전 전체 검증:
    input_image_csv_batch.py와 같은 정책 — CSV 전체를 미리 훑어 start_image/
    end_image 컬럼이 실제 파일로 해석되는지 확인한 뒤에 제출을 시작한다(일부만
    제출된 채 중간에 실패하는 일이 없도록).

결과물 파일명 규칙:
    "<JOB_ID>/<title(안전한 문자로 치환, 없으면 wan22_video_<순번>)>_seed<시드값>"
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

from PIL import Image

INPUT_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
DEFAULT_MAX_VIDEO_PIXELS = 1536 * 704
VIDEO_DIM_MULTIPLE = 16


def env(name, default=None):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


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


def resolve_input_image(name):
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(f"올바르지 않은 이미지 이름이에요: '{name}'")
    path = input_images_dir() / name
    if not path.is_file() or path.suffix.lower() not in INPUT_IMAGE_EXTENSIONS:
        raise ValueError(f"이미지 '{name}'를 찾을 수 없어요.")
    return path


def find_node(workflow, title_substring=None, class_types=()):
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
        print("[wan22_video_csv] 경고: 시드를 넣을 노드를 찾지 못했습니다.", file=sys.stderr)
        return
    field = "noise_seed" if node.get("class_type") == "KSamplerAdvanced" else "seed"
    node.setdefault("inputs", {})[field] = seed


def apply_user_prompt(workflow, prompt):
    prompt = (prompt or "").strip()
    if not prompt:
        return
    node_id, node = find_node(workflow, title_substring="user_prompt")
    if node is None:
        print("[wan22_video_csv] 경고: user_prompt 노드를 찾지 못했습니다.", file=sys.stderr)
        return
    node.setdefault("inputs", {})["value"] = prompt


def compute_video_dims(image_path, max_pixels=DEFAULT_MAX_VIDEO_PIXELS, multiple=VIDEO_DIM_MULTIPLE):
    """wan22_video_batch.py의 같은 이름 함수와 동일 — 자기완결성 컨벤션에 따라
    복사했다. 고치면 두 파일 다 같이 고쳐야 한다."""
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
        print("[wan22_video_csv] 경고: 영상 해상도를 넣을 노드를 찾지 못했습니다.", file=sys.stderr)
        return
    inputs = node.setdefault("inputs", {})
    inputs["width"] = width
    inputs["height"] = height


def resolve_video_dims(width_raw, height_raw, start_image_path):
    width_raw = (width_raw or "").strip()
    height_raw = (height_raw or "").strip()
    if width_raw and height_raw:
        try:
            return int(float(width_raw)), int(float(height_raw))
        except ValueError:
            print(f"[wan22_video_csv] 경고: width/height 값 '{width_raw}x{height_raw}'을 정수로 변환하지 못했습니다 — 자동 계산으로 대체합니다.", file=sys.stderr)
    elif width_raw or height_raw:
        print("[wan22_video_csv] 경고: width/height는 둘 다 채워야 적용됩니다 (하나만 비어 있음) — 자동 계산으로 대체합니다.", file=sys.stderr)

    try:
        return compute_video_dims(start_image_path)
    except Exception as e:
        print(f"[wan22_video_csv] 경고: 시작 이미지 크기를 읽지 못해 해상도를 자동 계산하지 못했습니다: {e}", file=sys.stderr)
        return None


def apply_lora(workflow, lora_name, lora_strength):
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
                print(f"[wan22_video_csv] 경고: LORA_STRENGTH 값 '{lora_strength}'을 숫자로 변환하지 못했습니다.", file=sys.stderr)


def apply_fast_4step(workflow, raw):
    if raw is None:
        return
    node_id, node = find_node(workflow, title_substring="fast_4step")
    if node is None:
        return
    node.setdefault("inputs", {})["value"] = (str(raw).strip().lower() == "on")


def sanitize_prefix(text, fallback):
    text = (text or fallback or "wan22_video").strip()
    text = re.sub(r"[^\w\-가-힣 ]+", "_", text)
    return text[:80] or fallback


def apply_filename_prefix(workflow, title, index, seed):
    node_id, node = find_node(workflow, class_types=("SaveVideo",))
    if node is None:
        return
    prefix = sanitize_prefix(title, f"wan22_video_{index}")
    prefix = f"{prefix}_seed{seed}"
    job_id = env("JOB_ID")
    if job_id:
        prefix = f"{job_id}/{prefix}"
    node.setdefault("inputs", {})["filename_prefix"] = prefix


COMFY_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

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
        print(f"[wan22_video_csv] 경고: 진행 상황 보고 실패: {e}", file=sys.stderr)


def run_once(base_workflow, comfy_url, title, seed, index, prompt, start_path, end_path, lora_name, lora_strength, fast_4step, video_dims):
    workflow = copy.deepcopy(base_workflow)
    apply_seed(workflow, seed)
    apply_user_prompt(workflow, prompt)
    apply_image(workflow, comfy_url, "start_image", start_path)
    if end_path is not None:
        apply_image(workflow, comfy_url, "end_image", end_path)
    if video_dims is not None:
        apply_video_size(workflow, *video_dims)
    apply_lora(workflow, lora_name, lora_strength)
    apply_fast_4step(workflow, fast_4step)
    apply_filename_prefix(workflow, title, index, seed)

    prompt_id = queue_prompt(comfy_url, workflow)
    print(f"[wan22_video_csv] [{index}] title={title!r} seed={seed} 큐 등록 (prompt_id={prompt_id})")
    wait_for_completion(comfy_url, prompt_id)
    print(f"[wan22_video_csv] [{index}] 완료")


def main():
    workflow_path = env("WORKFLOW_PATH")
    csv_path = env("CSV_PATH")
    if not workflow_path or not csv_path:
        print("[wan22_video_csv] WORKFLOW_PATH와 CSV_PATH 환경변수가 모두 필요합니다.", file=sys.stderr)
        sys.exit(1)

    base_workflow = load_workflow(workflow_path)
    # 워크플로우에 end_image 노드가 있으면(flf2v) CSV도 그 컬럼을 요구한다 —
    # i2v면 그런 노드가 아예 없으니 end_image 컬럼이 있어도 무시한다.
    needs_end_image = find_node(base_workflow, title_substring="end_image", class_types=("LoadImage",))[1] is not None

    rows = load_rows(csv_path)
    if not rows:
        print("[wan22_video_csv] CSV에 처리할 행이 없습니다.")
        return

    lora_name = (env("LORA_NAME", "") or "").strip()
    lora_strength = (env("LORA_STRENGTH", "") or "").strip()
    fast_4step = env("FAST_4STEP")
    comfy_url = env("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
    job_id = env("JOB_ID")
    nightshift_url = env("NIGHTSHIFT_URL", "http://127.0.0.1:8000")

    # input_image_csv_batch.py와 같은 정책 — 실행 전에 CSV 전체를 미리 훑어 이미지
    # 컬럼이 실제 파일로 해석되는지 확인한다(일부만 제출된 채 중간에 실패하지 않게).
    plan = []
    for line_no, row in enumerate(rows, start=2):
        prompt = (row.get("user_prompt") or row.get("prompt") or "").strip()
        if not prompt:
            print(f"[wan22_video_csv] 건너뜀 (user_prompt/prompt 없음, {line_no}번째 줄): {row}")
            continue

        start_name = (row.get("start_image") or "").strip()
        try:
            start_path = resolve_input_image(start_name)
        except ValueError as e:
            print(f"[wan22_video_csv] {line_no}번째 줄: {e}", file=sys.stderr)
            sys.exit(1)

        end_path = None
        if needs_end_image:
            end_name = (row.get("end_image") or "").strip()
            try:
                end_path = resolve_input_image(end_name)
            except ValueError as e:
                print(f"[wan22_video_csv] {line_no}번째 줄: {e}", file=sys.stderr)
                sys.exit(1)

        title = (row.get("title") or row.get("name") or "").strip()
        row_seed = (row.get("seed") or "").strip()
        seed = int(row_seed) if row_seed else random.randint(0, 2**31 - 1)
        video_dims = resolve_video_dims(row.get("width"), row.get("height"), start_path)
        plan.append((title, seed, prompt, start_path, end_path, video_dims))

    print(f"[wan22_video_csv] 총 {len(plan)}건 제출 예정")
    report_progress(job_id, nightshift_url, len(plan), 0)

    done = 0
    for index, (title, seed, prompt, start_path, end_path, video_dims) in enumerate(plan, start=1):
        run_once(base_workflow, comfy_url, title, seed, index, prompt, start_path, end_path, lora_name, lora_strength, fast_4step, video_dims)
        done += 1
        report_progress(job_id, nightshift_url, len(plan), done)

    print(f"[wan22_video_csv] 총 {len(plan)}건 완료")


if __name__ == "__main__":
    main()
