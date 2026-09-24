"""
영상 생성(CSV 배치) 템플릿 — minimax_h3_r2v_batch.py의 시드 반복 대신, CSV 행마다 다른
프롬프트/참조 이미지·비디오·오디오/시드/영상 길이로 MiniMax-H3 Reference to Video 영상을
만든다. 워크플로우 찾기/주입/ComfyUI 연동 로직은 minimax_h3_r2v_batch.py와 완전히 같다
(템플릿 자기완결성 컨벤션에 따라 그대로 복사했다 — 고치면 두 파일 다 같이 고쳐야 한다).

CSV 컬럼:
    title           결과 파일명 접두사로 쓰일 제목 (선택, name도 허용)
    user_prompt     프롬프트 원문 (prompt도 동일하게 취급, 둘 중 하나는 있어야 함)
    prompt          user_prompt가 없을 때 쓰이는 대체 컬럼 (선택)
    seed            지정하면 그 값을 시드로 사용, 비어 있으면 랜덤 시드
    ref_image_1..9  참조 이미지 1~9 파일명 (전부 선택 — 없으면 그 슬롯은 건너뜀. "최소
                    하나는 있어야 한다"는 빌드 타임에 이미 검증됐다)
    ref_video_1..3  참조 비디오 1~3 파일명 (선택, 입력 비디오 풀에 있어야 함)
    ref_audio_1..3  참조 오디오 1~3 파일명 (선택, 입력 오디오 풀에 있어야 함)
    video_duration  영상 길이(초) (선택, 비워두면 빌드 시점 기본값(15초)을 그대로 씀)

환경변수:
    WORKFLOW_PATH, CSV_PATH  (둘 다 필수, nightshift가 주입)
    NIGHTSHIFT_INPUT_IMAGES_DIR, NIGHTSHIFT_ASSETS_DIR, COMFY_URL, JOB_ID,
    NIGHTSHIFT_URL, NIGHTSHIFT_API_KEY, POLL_INTERVAL_SEC, POLL_MISSING_GRACE_SEC
                    minimax_h3_r2v_batch.py와 같음

결과물 파일명 규칙:
    "<JOB_ID>/<title(안전한 문자로 치환, 없으면 minimax_h3_r2v_<순번>)>_seed<시드값>"
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

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac"}
MAX_REF_IMAGES = 9
MAX_REF_VIDEOS = 3
MAX_REF_AUDIOS = 3


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


def _resolve_ref(name, extensions):
    """이름이 비어 있으면 None(그 슬롯은 안 씀), 있는데 파일을 못 찾으면 ValueError."""
    if not name:
        return None
    if "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(f"올바르지 않은 파일 이름이에요: '{name}'")
    path = input_images_dir() / name
    if not path.is_file() or path.suffix.lower() not in extensions:
        raise ValueError(f"파일 '{name}'를 찾을 수 없어요.")
    return path


def resolve_input_image(name):
    return _resolve_ref(name, IMAGE_EXTENSIONS)


def resolve_input_video(name):
    return _resolve_ref(name, VIDEO_EXTENSIONS)


def resolve_input_audio(name):
    return _resolve_ref(name, AUDIO_EXTENSIONS)


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


def find_prompt_node(workflow):
    """프롬프트를 넣을 노드 — nightshift 마법사가 만든 워크플로우는 "main_prompt", ComfyUI 공식 MiniMax-H3
    워크플로우는 "user_prompt"라는 제목을 쓴다. 공식 파일을 그대로 올려도 프롬프트가 빠지지 않게 둘 다 찾는다."""
    for title in ("main_prompt", "user_prompt"):
        node_id, node = find_node(workflow, title_substring=title)
        if node is not None:
            return node_id, node
    return None, None


def find_duration_node(workflow):
    """영상 길이(초) 노드 — 마법사는 "video_duration", 공식 워크플로우는 "duration"이다. 공식 파일에는
    "Float (duration)"처럼 그 값을 연결로 받기만 하는 중계 노드도 있어서, 값이 직접 들어 있는 쪽을 고른다."""
    node_id, node = find_node(workflow, title_substring="video_duration")
    if node is not None:
        return node_id, node
    for node_id, node in workflow.items():
        if not isinstance(node, dict) or node.get("class_type") != "PrimitiveFloat":
            continue
        title = str(node.get("_meta", {}).get("title", "")).lower()
        if "duration" in title and not isinstance((node.get("inputs") or {}).get("value"), list):
            return node_id, node
    return None, None


def apply_seed(workflow, seed):
    node_id, node = find_node(workflow, class_types=("RandomNoise",))
    if node is None:
        print("[minimax_h3_r2v_csv] 경고: 시드를 넣을 노드를 찾지 못했습니다 (RandomNoise 없음).", file=sys.stderr)
        return
    node.setdefault("inputs", {})["noise_seed"] = seed


def apply_main_prompt(workflow, prompt):
    prompt = (prompt or "").strip()
    if not prompt:
        return
    node_id, node = find_prompt_node(workflow)
    if node is None:
        print("[minimax_h3_r2v_csv] 경고: main_prompt 노드를 찾지 못했습니다.", file=sys.stderr)
        return
    node.setdefault("inputs", {})["value"] = prompt


def apply_duration(workflow, duration_raw):
    duration_raw = (duration_raw or "").strip()
    if not duration_raw:
        return
    try:
        duration = float(duration_raw)
    except ValueError:
        print(f"[minimax_h3_r2v_csv] 경고: video_duration 값 '{duration_raw}'을 숫자로 변환하지 못했습니다.", file=sys.stderr)
        return
    node_id, node = find_duration_node(workflow)
    if node is None:
        print("[minimax_h3_r2v_csv] 경고: video_duration 노드를 찾지 못했습니다.", file=sys.stderr)
        return
    node.setdefault("inputs", {})["value"] = duration


def sanitize_prefix(text, fallback):
    text = (text or fallback or "minimax_h3_r2v").strip()
    text = re.sub(r"[^\w\-가-힣 ]+", "_", text)
    return text[:80] or fallback


def apply_filename_prefix(workflow, title, index, seed):
    node_id, node = find_node(workflow, class_types=("SaveVideo",))
    if node is None:
        return
    prefix = sanitize_prefix(title, f"minimax_h3_r2v_{index}")
    prefix = f"{prefix}_seed{seed}"
    job_id = env("JOB_ID")
    if job_id:
        prefix = f"{job_id}/{prefix}"
    node.setdefault("inputs", {})["filename_prefix"] = prefix


COMFY_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_uploaded_file_cache = {}


def upload_file_to_comfy(comfy_url, file_path):
    """ComfyUI의 /upload/image는 콘텐츠 종류를 가리지 않고 그대로 입력 폴더에 저장해
    이름을 돌려준다 — 비디오/오디오 업로드에도 그대로 쓸 수 있다(폼 필드 이름은
    여전히 "image")."""
    try:
        stat = file_path.stat()
        cache_key = (comfy_url, str(file_path.resolve()), stat.st_size, stat.st_mtime_ns)
    except OSError:
        cache_key = None
    if cache_key is not None and cache_key in _uploaded_file_cache:
        return _uploaded_file_cache[cache_key]

    boundary = uuid.uuid4().hex
    with open(file_path, "rb") as f:
        file_bytes = f.read()

    body = io.BytesIO()

    def write(chunk):
        body.write(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)

    write(f"--{boundary}\r\n")
    write(f'Content-Disposition: form-data; name="image"; filename="{file_path.name}"\r\n')
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
        _uploaded_file_cache[cache_key] = uploaded_name
    return uploaded_name


def apply_image(workflow, comfy_url, title, image_path):
    node_id, node = find_node(workflow, title_substring=title, class_types=("LoadImage",))
    if node is None:
        return False
    uploaded_name = upload_file_to_comfy(comfy_url, image_path)
    node.setdefault("inputs", {})["image"] = uploaded_name
    return True


def apply_ref_video(workflow, comfy_url, title, video_path):
    node_id, node = find_node(workflow, title_substring=title, class_types=("VHS_LoadVideo", "LoadVideo"))
    if node is None:
        return False
    uploaded_name = upload_file_to_comfy(comfy_url, video_path)
    field = "video" if node.get("class_type") == "VHS_LoadVideo" else "file"
    node.setdefault("inputs", {})[field] = uploaded_name
    return True


def apply_ref_audio(workflow, comfy_url, title, audio_path):
    node_id, node = find_node(workflow, title_substring=title, class_types=("LoadAudio",))
    if node is None:
        return False
    uploaded_name = upload_file_to_comfy(comfy_url, audio_path)
    node.setdefault("inputs", {})["audio"] = uploaded_name
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
        print(f"[minimax_h3_r2v_csv] 경고: 진행 상황 보고 실패: {e}", file=sys.stderr)


def run_once(base_workflow, comfy_url, title, seed, index, prompt,
             ref_image_paths, ref_video_paths, ref_audio_paths, duration):
    workflow = copy.deepcopy(base_workflow)
    apply_seed(workflow, seed)
    apply_main_prompt(workflow, prompt)
    apply_duration(workflow, duration)
    for i, path in enumerate(ref_image_paths, start=1):
        if path is not None:
            apply_image(workflow, comfy_url, f"ref_image_{i}", path)
    for i, path in enumerate(ref_video_paths, start=1):
        if path is not None:
            apply_ref_video(workflow, comfy_url, f"ref_video_{i}", path)
    for i, path in enumerate(ref_audio_paths, start=1):
        if path is not None:
            apply_ref_audio(workflow, comfy_url, f"ref_audio_{i}", path)
    apply_filename_prefix(workflow, title, index, seed)

    prompt_id = queue_prompt(comfy_url, workflow)
    print(f"[minimax_h3_r2v_csv] [{index}] title={title!r} seed={seed} 큐 등록 (prompt_id={prompt_id})")
    wait_for_completion(comfy_url, prompt_id)
    print(f"[minimax_h3_r2v_csv] [{index}] 완료")


def _row_refs(row, line_no, prefix, count, resolver):
    paths = []
    for i in range(1, count + 1):
        name = (row.get(f"{prefix}_{i}") or "").strip()
        try:
            paths.append(resolver(name))
        except ValueError as e:
            print(f"[minimax_h3_r2v_csv] {line_no}번째 줄: {e}", file=sys.stderr)
            sys.exit(1)
    return paths


def main():
    workflow_path = env("WORKFLOW_PATH")
    csv_path = env("CSV_PATH")
    if not workflow_path or not csv_path:
        print("[minimax_h3_r2v_csv] WORKFLOW_PATH와 CSV_PATH 환경변수가 모두 필요합니다.", file=sys.stderr)
        sys.exit(1)

    base_workflow = load_workflow(workflow_path)
    rows = load_rows(csv_path)
    if not rows:
        print("[minimax_h3_r2v_csv] CSV에 처리할 행이 없습니다.")
        return

    comfy_url = env("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
    job_id = env("JOB_ID")
    nightshift_url = env("NIGHTSHIFT_URL", "http://127.0.0.1:8000")

    plan = []
    for line_no, row in enumerate(rows, start=2):
        prompt = (row.get("user_prompt") or row.get("prompt") or "").strip()
        if not prompt:
            print(f"[minimax_h3_r2v_csv] 건너뜀 (user_prompt/prompt 없음, {line_no}번째 줄): {row}")
            continue

        ref_image_paths = _row_refs(row, line_no, "ref_image", MAX_REF_IMAGES, resolve_input_image)
        ref_video_paths = _row_refs(row, line_no, "ref_video", MAX_REF_VIDEOS, resolve_input_video)
        ref_audio_paths = _row_refs(row, line_no, "ref_audio", MAX_REF_AUDIOS, resolve_input_audio)

        title = (row.get("title") or row.get("name") or "").strip()
        row_seed = (row.get("seed") or "").strip()
        seed = int(row_seed) if row_seed else random.randint(0, 2**31 - 1)
        duration = (row.get("video_duration") or "").strip()
        plan.append((title, seed, prompt, ref_image_paths, ref_video_paths, ref_audio_paths, duration))

    print(f"[minimax_h3_r2v_csv] 총 {len(plan)}건 제출 예정")
    report_progress(job_id, nightshift_url, len(plan), 0)

    done = 0
    for index, (title, seed, prompt, ref_image_paths, ref_video_paths, ref_audio_paths, duration) in enumerate(plan, start=1):
        run_once(base_workflow, comfy_url, title, seed, index, prompt,
                  ref_image_paths, ref_video_paths, ref_audio_paths, duration)
        done += 1
        report_progress(job_id, nightshift_url, len(plan), done)

    print(f"[minimax_h3_r2v_csv] 총 {len(plan)}건 완료")


if __name__ == "__main__":
    main()
