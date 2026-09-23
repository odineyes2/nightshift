"""
영상 생성(CSV 배치) 템플릿 — minimax_h3_i2v_batch.py의 시드 반복 대신, CSV 행마다 다른
프롬프트/입력 이미지/시드/영상 길이로 MiniMax-H3 Image to Video 영상을 만든다. 워크플로우
찾기/주입/ComfyUI 연동 로직은 minimax_h3_i2v_batch.py와 완전히 같다(템플릿 자기완결성
컨벤션에 따라 그대로 복사했다 — 고치면 두 파일 다 같이 고쳐야 한다).

CSV 컬럼:
    title           결과 파일명 접두사로 쓰일 제목 (선택, name도 허용)
    user_prompt     프롬프트 원문 (prompt도 동일하게 취급, 둘 중 하나는 있어야 함)
    prompt          user_prompt가 없을 때 쓰이는 대체 컬럼 (선택)
    seed            지정하면 그 값을 시드로 사용, 비어 있으면 랜덤 시드
    first_frame_image  첫 프레임 이미지 파일명 (선택 — 그래프에 그 노드가 있을 때만 의미
                    있음, 빌드 타임에 최소 하나는 있다고 이미 보장됨)
    last_frame_image   마지막 프레임 이미지 파일명 (선택)
    video_duration  영상 길이(초) (선택, 비워두면 빌드 시점 기본값(5초)을 그대로 씀)

환경변수:
    WORKFLOW_PATH, CSV_PATH  (둘 다 필수, nightshift가 주입)
    NIGHTSHIFT_INPUT_IMAGES_DIR, NIGHTSHIFT_ASSETS_DIR, COMFY_URL, JOB_ID,
    NIGHTSHIFT_URL, NIGHTSHIFT_API_KEY, POLL_INTERVAL_SEC, POLL_TIMEOUT_SEC
                    minimax_h3_i2v_batch.py와 같음

실행 전 전체 검증:
    input_image_csv_batch.py와 같은 정책 — CSV 전체를 미리 훑어 이미지 컬럼이 실제
    파일로 해석되는지 확인한 뒤에 제출을 시작한다(일부만 제출된 채 중간에 실패하지 않게).

결과물 파일명 규칙:
    "<JOB_ID>/<title(안전한 문자로 치환, 없으면 minimax_h3_i2v_<순번>)>_seed<시드값>"
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

INPUT_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


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
    """이름이 비어 있으면 None(그 슬롯은 안 씀), 있는데 못 찾으면 ValueError."""
    if not name:
        return None
    if "/" in name or "\\" in name or name in (".", ".."):
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
    node_id, node = find_node(workflow, class_types=("RandomNoise",))
    if node is None:
        print("[minimax_h3_i2v_csv] 경고: 시드를 넣을 노드를 찾지 못했습니다 (RandomNoise 없음).", file=sys.stderr)
        return
    node.setdefault("inputs", {})["noise_seed"] = seed


def apply_main_prompt(workflow, prompt):
    prompt = (prompt or "").strip()
    if not prompt:
        return
    node_id, node = find_node(workflow, title_substring="main_prompt")
    if node is None:
        print("[minimax_h3_i2v_csv] 경고: main_prompt 노드를 찾지 못했습니다.", file=sys.stderr)
        return
    node.setdefault("inputs", {})["value"] = prompt


def apply_duration(workflow, duration_raw):
    duration_raw = (duration_raw or "").strip()
    if not duration_raw:
        return
    try:
        duration = float(duration_raw)
    except ValueError:
        print(f"[minimax_h3_i2v_csv] 경고: video_duration 값 '{duration_raw}'을 숫자로 변환하지 못했습니다.", file=sys.stderr)
        return
    node_id, node = find_node(workflow, title_substring="video_duration")
    if node is None:
        print("[minimax_h3_i2v_csv] 경고: video_duration 노드를 찾지 못했습니다.", file=sys.stderr)
        return
    node.setdefault("inputs", {})["value"] = duration


def sanitize_prefix(text, fallback):
    text = (text or fallback or "minimax_h3_i2v").strip()
    text = re.sub(r"[^\w\-가-힣 ]+", "_", text)
    return text[:80] or fallback


def apply_filename_prefix(workflow, title, index, seed):
    node_id, node = find_node(workflow, class_types=("SaveVideo",))
    if node is None:
        return
    prefix = sanitize_prefix(title, f"minimax_h3_i2v_{index}")
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
        print(f"[minimax_h3_i2v_csv] 경고: 진행 상황 보고 실패: {e}", file=sys.stderr)


def run_once(base_workflow, comfy_url, title, seed, index, prompt, first_frame_path, last_frame_path, duration):
    workflow = copy.deepcopy(base_workflow)
    apply_seed(workflow, seed)
    apply_main_prompt(workflow, prompt)
    apply_duration(workflow, duration)
    if first_frame_path is not None:
        apply_image(workflow, comfy_url, "first_frame_image", first_frame_path)
    if last_frame_path is not None:
        apply_image(workflow, comfy_url, "last_frame_image", last_frame_path)
    apply_filename_prefix(workflow, title, index, seed)

    prompt_id = queue_prompt(comfy_url, workflow)
    print(f"[minimax_h3_i2v_csv] [{index}] title={title!r} seed={seed} 큐 등록 (prompt_id={prompt_id})")
    wait_for_completion(comfy_url, prompt_id)
    print(f"[minimax_h3_i2v_csv] [{index}] 완료")


def main():
    workflow_path = env("WORKFLOW_PATH")
    csv_path = env("CSV_PATH")
    if not workflow_path or not csv_path:
        print("[minimax_h3_i2v_csv] WORKFLOW_PATH와 CSV_PATH 환경변수가 모두 필요합니다.", file=sys.stderr)
        sys.exit(1)

    base_workflow = load_workflow(workflow_path)
    rows = load_rows(csv_path)
    if not rows:
        print("[minimax_h3_i2v_csv] CSV에 처리할 행이 없습니다.")
        return

    comfy_url = env("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
    job_id = env("JOB_ID")
    nightshift_url = env("NIGHTSHIFT_URL", "http://127.0.0.1:8000")

    plan = []
    for line_no, row in enumerate(rows, start=2):
        prompt = (row.get("user_prompt") or row.get("prompt") or "").strip()
        if not prompt:
            print(f"[minimax_h3_i2v_csv] 건너뜀 (user_prompt/prompt 없음, {line_no}번째 줄): {row}")
            continue

        try:
            first_frame_path = resolve_input_image((row.get("first_frame_image") or "").strip())
            last_frame_path = resolve_input_image((row.get("last_frame_image") or "").strip())
        except ValueError as e:
            print(f"[minimax_h3_i2v_csv] {line_no}번째 줄: {e}", file=sys.stderr)
            sys.exit(1)

        title = (row.get("title") or row.get("name") or "").strip()
        row_seed = (row.get("seed") or "").strip()
        seed = int(row_seed) if row_seed else random.randint(0, 2**31 - 1)
        duration = (row.get("video_duration") or "").strip()
        plan.append((title, seed, prompt, first_frame_path, last_frame_path, duration))

    print(f"[minimax_h3_i2v_csv] 총 {len(plan)}건 제출 예정")
    report_progress(job_id, nightshift_url, len(plan), 0)

    done = 0
    for index, (title, seed, prompt, first_frame_path, last_frame_path, duration) in enumerate(plan, start=1):
        run_once(base_workflow, comfy_url, title, seed, index, prompt, first_frame_path, last_frame_path, duration)
        done += 1
        report_progress(job_id, nightshift_url, len(plan), done)

    print(f"[minimax_h3_i2v_csv] 총 {len(plan)}건 완료")


if __name__ == "__main__":
    main()
