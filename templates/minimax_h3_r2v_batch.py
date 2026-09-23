"""
영상 생성(시드 반복) 템플릿 — MiniMax-H3 Reference to Video 워크플로우로 영상 하나를
시드만 바꿔가며 VIDEO_COUNT번 반복 실행한다. minimax_h3_i2v_batch.py와 같은 ComfyUI
연동 방식을 쓴다(자기완결성 컨벤션에 따라 헬퍼를 그대로 복사했다).

이 템플릿이 쓰는 워크플로우는 server/workflow_builder_minimax_h3.py의 build_r2v_workflow()가
매번 새로 조립한다 — 참조 이미지/비디오/오디오 개수(spec["ref_image_count"] 최대 9,
["ref_video_count"] 최대 3, ["ref_audio_count"] 최대 3)는 마법사에서 빌드 타임에 정해지므로,
그 그래프에 실제로 있는 만큼의 "ref_image_1".."ref_image_9"/"ref_video_1".."ref_video_3"/
"ref_audio_1".."ref_audio_3" 제목 노드에만 값이 들어간다(그래프에 없는 번호는 조용히
건너뜀). width/height는 "참조 이미지 크기에 맞춤"으로 고정돼 있어 옵션이 없다.

REF_IMAGE/VIDEO/AUDIO_N은 전부 선택(비어 있으면 그 슬롯은 건너뜀) — "최소 하나는 있어야
한다"는 이미 빌드 타임(build_r2v_workflow)에서 검증됐으므로 여기서는 다시 확인하지 않는다.
비디오 참조 노드는 빌드 시점에 고른 방식(VHS_LoadVideo 또는 LoadVideo)에 따라 필드명이
다르다("video" vs "file") — apply_ref_video가 노드의 실제 class_type을 보고 알아서 고른다.

환경변수:
    WORKFLOW_PATH      (필수) ComfyUI API 형식 workflow json 경로 (nightshift가 주입)
    VIDEO_COUNT        (필수) 반복 생성할 영상 개수 (템플릿 옵션 "video_count")
    REF_IMAGE_1..9      참조 이미지 1~9 파일명 (템플릿 옵션 "ref_image_1".."ref_image_9",
                       비어 있으면 건너뜀)
    REF_VIDEO_1..3      참조 비디오 1~3 파일명 (템플릿 옵션 "ref_video_1".."ref_video_3",
                       비어 있으면 건너뜀 — 입력 비디오 풀(input_assets.py)에 있어야 함)
    REF_AUDIO_1..3      참조 오디오 1~3 파일명 (템플릿 옵션 "ref_audio_1".."ref_audio_3",
                       비어 있으면 건너뜀 — 입력 오디오 풀에 있어야 함)
    USER_PROMPT        프롬프트 원문 (템플릿 옵션 "user_prompt", 기본 빈 값)
    VIDEO_DURATION     영상 길이(초) (템플릿 옵션 "video_duration", 기본 빈 값 — 비워두면
                       빌드 시점 기본값(15초)을 그대로 씀)
    NIGHTSHIFT_INPUT_IMAGES_DIR, NIGHTSHIFT_ASSETS_DIR, COMFY_URL, JOB_ID,
    NIGHTSHIFT_URL, NIGHTSHIFT_API_KEY, POLL_INTERVAL_SEC, POLL_MISSING_GRACE_SEC
                       minimax_h3_i2v_batch.py와 같음

결과물 파일명 규칙:
    SaveVideo 노드의 filename_prefix를 "<JOB_ID>/minimax_h3_r2v_<순번>_seed<시드값>"로 채운다.
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


def input_images_dir():
    override = env("NIGHTSHIFT_INPUT_IMAGES_DIR")
    if override:
        return Path(override)
    assets_dir = env("NIGHTSHIFT_ASSETS_DIR", "/workspace/dataset/assets")
    return Path(assets_dir) / "input"


def _resolve_ref(name, label, extensions):
    """이름이 비어 있으면 None(그 슬롯은 안 씀 — 전부 선택이라 에러가 아님), 이름이 있는데
    파일을 못 찾으면 즉시 종료(잘못 지정된 참조를 조용히 무시하면 안 됨)."""
    if not name:
        return None
    path = input_images_dir() / name
    if not path.is_file() or path.suffix.lower() not in extensions:
        print(f"[minimax_h3_r2v] {label} '{name}'를 찾을 수 없습니다 ({path}).", file=sys.stderr)
        sys.exit(1)
    return path


def resolve_input_image(name, label):
    return _resolve_ref(name, label, IMAGE_EXTENSIONS)


def resolve_input_video(name, label):
    return _resolve_ref(name, label, VIDEO_EXTENSIONS)


def resolve_input_audio(name, label):
    return _resolve_ref(name, label, AUDIO_EXTENSIONS)


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
        print("[minimax_h3_r2v] 경고: 시드를 넣을 노드를 찾지 못했습니다 (RandomNoise 없음).", file=sys.stderr)
        return
    node.setdefault("inputs", {})["noise_seed"] = seed


def apply_main_prompt(workflow, prompt):
    prompt = (prompt or "").strip()
    if not prompt:
        return
    node_id, node = find_node(workflow, title_substring="main_prompt")
    if node is None:
        print("[minimax_h3_r2v] 경고: main_prompt 노드를 찾지 못했습니다.", file=sys.stderr)
        return
    node.setdefault("inputs", {})["value"] = prompt


def apply_duration(workflow, duration_raw):
    duration_raw = (duration_raw or "").strip()
    if not duration_raw:
        return
    try:
        duration = float(duration_raw)
    except ValueError:
        print(f"[minimax_h3_r2v] 경고: VIDEO_DURATION 값 '{duration_raw}'을 숫자로 변환하지 못했습니다.", file=sys.stderr)
        return
    node_id, node = find_node(workflow, title_substring="video_duration")
    if node is None:
        print("[minimax_h3_r2v] 경고: video_duration 노드를 찾지 못했습니다.", file=sys.stderr)
        return
    node.setdefault("inputs", {})["value"] = duration


def apply_filename_prefix(workflow, index, seed):
    node_id, node = find_node(workflow, class_types=("SaveVideo",))
    if node is None:
        return
    prefix = f"minimax_h3_r2v_{index}_seed{seed}"
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
    이름을 돌려준다 — 이미지뿐 아니라 비디오/오디오 업로드에도(VHS_LoadVideo 등 커스텀
    노드가 흔히 재사용하는 방식) 그대로 쓸 수 있다. 폼 필드 이름은 여전히 "image"다
    (ComfyUI 쪽 구현이 필드 이름으로 종류를 가리지 않음)."""
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
    """VHS_LoadVideo("video" 필드) 또는 네이티브 LoadVideo("file" 필드) 둘 중 실제로
    그래프에 있는 쪽을 찾아 채운다 — 어느 쪽인지는 빌드 시점에 고른 video_ref_mode에
    따라 이미 그래프에 하나만 있다."""
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
        print(f"[minimax_h3_r2v] 경고: 진행 상황 보고 실패: {e}", file=sys.stderr)


def run_once(base_workflow, comfy_url, index, seed, user_prompt, duration,
             ref_image_paths, ref_video_paths, ref_audio_paths):
    workflow = copy.deepcopy(base_workflow)
    apply_seed(workflow, seed)
    apply_main_prompt(workflow, user_prompt)
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
    apply_filename_prefix(workflow, index, seed)

    prompt_id = queue_prompt(comfy_url, workflow)
    print(f"[minimax_h3_r2v] [{index}] seed={seed} 큐 등록 (prompt_id={prompt_id})")
    wait_for_completion(comfy_url, prompt_id)
    print(f"[minimax_h3_r2v] [{index}] 완료")


def main():
    workflow_path = env("WORKFLOW_PATH")
    video_count_raw = env("VIDEO_COUNT")
    if not workflow_path or not video_count_raw:
        print("[minimax_h3_r2v] WORKFLOW_PATH와 VIDEO_COUNT 환경변수가 모두 필요합니다.", file=sys.stderr)
        sys.exit(1)

    video_count = int(video_count_raw)
    ref_image_paths = [resolve_input_image(env(f"REF_IMAGE_{i}", ""), f"참조 이미지 {i}") for i in range(1, MAX_REF_IMAGES + 1)]
    ref_video_paths = [resolve_input_video(env(f"REF_VIDEO_{i}", ""), f"참조 비디오 {i}") for i in range(1, MAX_REF_VIDEOS + 1)]
    ref_audio_paths = [resolve_input_audio(env(f"REF_AUDIO_{i}", ""), f"참조 오디오 {i}") for i in range(1, MAX_REF_AUDIOS + 1)]

    user_prompt = env("USER_PROMPT", "")
    duration = env("VIDEO_DURATION", "")

    base_workflow = load_workflow(workflow_path)
    comfy_url = env("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
    job_id = env("JOB_ID")
    nightshift_url = env("NIGHTSHIFT_URL", "http://127.0.0.1:8000")

    counts = (sum(1 for p in ref_image_paths if p), sum(1 for p in ref_video_paths if p), sum(1 for p in ref_audio_paths if p))
    print(f"[minimax_h3_r2v] 총 {video_count}건 제출 예정 (참조 이미지 {counts[0]}장, 비디오 {counts[1]}개, 오디오 {counts[2]}개)")
    report_progress(job_id, nightshift_url, video_count, 0)

    done = 0
    for index in range(1, video_count + 1):
        seed = random.randint(0, 2**31 - 1)
        run_once(base_workflow, comfy_url, index, seed, user_prompt, duration,
                  ref_image_paths, ref_video_paths, ref_audio_paths)
        done += 1
        report_progress(job_id, nightshift_url, video_count, done)

    print(f"[minimax_h3_r2v] 총 {video_count}건 완료")


if __name__ == "__main__":
    main()
