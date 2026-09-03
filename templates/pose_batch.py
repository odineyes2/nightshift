"""
포즈 참조 배치 템플릿 — ControlNet(OpenPose 등)에 쓸 포즈 레퍼런스 이미지를 서버에
미리 쌓아둔 폴더(포즈 세트)에서 순차 또는 랜덤으로 뽑아 LoadImage 노드에 주입하면서,
워크플로우 하나를 pose_count번 반복 실행한다. seed_batch.py와 같은 ComfyUI 연동
방식(워크플로우 노드 찾기/제출/폴링/진행률 보고)을 쓰되, 매 반복마다 시드뿐 아니라
포즈 레퍼런스 이미지도 함께 바꾼다.

포즈 레퍼런스 폴더 구조:
    NIGHTSHIFT_POSES_DIR(기본 /workspace/dataset/poses)/
        1/                          이 템플릿은 항상 여기(1인물/solo)만 본다
            <POSE_SET 이름>/
                image1.png
                image2.jpg
                ...
        2/                          (다른 템플릿용 — 이 템플릿은 안 봄)
            ...
    이 템플릿은 처음부터 1인물(solo) 참조 전용으로 설계됐고 char_no 옵션이
    없다 — 여러 인물이 함께 그려진 포즈 세트(2인물 이상)를 쓰려면 CSV 행마다
    포즈와 char_no를 지정할 수 있는 pose_csv_batch 템플릿을 쓴다. nightshift
    웹 UI의 "포즈 세트" 드롭다운은 POSES_DIR/1 아래의 하위 폴더 목록을
    보여주고(GET /api/assets), 고른 세트 이름이 POSE_SET 환경변수로 전달된다.
    세트 존재 여부/이미지 유무는 업로드 시점에 nightshift(app.py + pose_assets.py)가
    이미 검증했으므로 큐 시작 이후 그것 때문에 실패하는 일은 없지만, 실행 중 폴더가
    바뀌는 등의 만일의 상황을 대비해 이 스크립트도 시작할 때 한 번 더 확인한다.

포즈 선택 방식(POSE_MODE):
    sequential  파일명 정렬 순서대로 순환. pose_count가 이미지 수보다 많으면
                처음으로 되돌아가 반복한다.
    random      매번 무작위로 뽑되, 폴더가 다 소진될 때까지는 같은 이미지를 다시
                뽑지 않는다(완전 무작위보다 다양성이 보장됨). 다 뽑고 나면
                다시 전체를 섞어서 처음부터 뽑는다.

ComfyUI로의 이미지 주입 방식:
    LoadImage 노드가 참조하는 파일은 ComfyUI 자신의 input 폴더에 있어야 한다.
    nightshift와 ComfyUI가 파일시스템을 공유한다는 보장이 없으므로(예: ComfyUI가
    다른 컨테이너/파드로 분리돼 있을 수 있음) 파일을 직접 복사하는 대신, 매번
    ComfyUI의 POST /upload/image API로 업로드하고 응답으로 받은 파일명을 LoadImage
    노드의 image 입력에 그대로 넣는다(아래 upload_image_to_comfy). 이미지마다 HTTP
    업로드가 한 번씩 더 들어가 느리지만, 파일시스템 공유 여부와 무관하게 항상 동작한다.

환경변수:
    WORKFLOW_PATH      (필수) ComfyUI API 형식 workflow json 경로 (nightshift가 주입)
    POSE_COUNT         (필수) 반복 생성할 이미지 개수 (nightshift가 템플릿 옵션 "pose_count"로 주입)
    POSE_SET           (필수) 포즈 세트 폴더 이름 (nightshift가 템플릿 옵션 "pose_set"으로 주입)
    POSE_MODE          "sequential" 또는 "random" (nightshift가 템플릿 옵션 "pose_mode"로 주입, 기본 sequential)
    NIGHTSHIFT_POSES_DIR   포즈 세트들이 있는 상위 폴더 (기본 /workspace/dataset/poses).
                       nightshift 서버(pose_assets.py)와 같은 값을 봐야 하므로 손대지 않는 게 안전함
    NIGHTSHIFT_OUTPUT_DIR  ComfyUI가 이미지를 저장하는 폴더 (기본 /workspace/output).
                       재현성 기록용 jsonl(pose_batch_manifest.jsonl)을 여기 같이 남긴다
    COMFY_URL          ComfyUI 서버 주소 (기본 http://127.0.0.1:8188)
    JOB_ID             nightshift가 주입하는 이 작업의 id (진행 상황 보고용, 없으면 보고 생략)
    NIGHTSHIFT_URL     nightshift 자신의 주소 (진행 상황 보고용, 기본 http://127.0.0.1:8000)
    SEED_NODE_TITLE    시드를 주입할 노드의 _meta.title 부분일치 (기본 "KSampler")
    POSE_NODE_TITLE    포즈 이미지를 주입할 LoadImage 노드의 _meta.title 부분일치
                       (기본 "Load" — 정확히 일치하는 노드가 없으면, 워크플로우에
                       LoadImage 노드가 하나뿐일 때 그 노드를 대신 쓴다. 여러 개인
                       워크플로우라면 이 값을 실제 노드 제목에 맞게 조정해야 한다)
    SAVE_NODE_TITLE    파일명 접두사를 주입할 노드의 _meta.title 부분일치 (기본 "Save")
    POLL_INTERVAL_SEC  히스토리 폴링 간격 초 (기본 2)
    POLL_TIMEOUT_SEC   개별 작업 완료 대기 제한 초 (기본 600)

진행 상황 보고:
    pose_count를 그대로 예상 총 이미지 수로 보고 PUT /api/jobs/{job_id}/progress로
    {"total": N, "done": M}을 보고한다. seed_batch.py와 달리 EmptyLatentImage의
    batch_size는 반영하지 않는다(포즈 배치는 보통 한 번에 1장씩 생성하는 용도라
    단순하게 유지) — batch_size를 키워 쓰는 워크플로우라면 총 개수가 실제 이미지
    수보다 적게 잡힐 수 있다.

재현성 기록:
    매 반복마다 어떤 포즈 이미지를 썼는지 stdout 로그에 남기고, SaveImage의
    filename_prefix에도 포즈 파일명(확장자 제외, 안전한 문자로 치환)을 포함시킨다
    (예: pose_batch_3_seed482913_standing_01.png). 추가로 NIGHTSHIFT_OUTPUT_DIR에
    pose_batch_manifest.jsonl을 이어쓰기(append)로 남겨, 한 줄마다
    {timestamp, job_id, index, pose_set, pose_file, seed, prompt_id}를 기록한다 —
    나중에 "이 컷이 왜 이렇게 나왔는지" 추적할 때 쓴다. 기록에 실패해도(출력 폴더가
    아직 없는 등) 배치 자체는 계속 진행한다.
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

POSE_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

# 이 템플릿은 char_no 개념이 없는 1인물(solo) 전용 — pose_assets.py의
# DEFAULT_CHAR_NO와 반드시 같은 값이어야 한다(다르면 이 템플릿과 nightshift
# 서버가 서로 다른 폴더를 보게 됨).
POSE_CHAR_NO = "1"


def env(name, default=None):
    return os.environ.get(name, default)


def load_workflow(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def connected_node_ids(workflow):
    """다른 노드의 입력 링크로 실제 연결되어 있는(=출력이 쓰이고 있는) 노드 id 집합.
    csv_batch.py와 동일한 로직 — 같은 class_type의 노드가 여럿 남아있을 때(예: 다른
    워크플로우 실험의 흔적) 실제로 쓰이는 노드를 가려내는 데 쓴다."""
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
    return text[:40] or "pose"


# 포즈 파일 선택 전략 ---------------------------------------------------------

class SequentialPicker:
    """파일명 정렬 순서대로 순환. 개수를 넘기면 처음부터 다시."""

    def __init__(self, files):
        self.files = files
        self.i = 0

    def pick(self):
        f = self.files[self.i % len(self.files)]
        self.i += 1
        return f


class RandomNoRepeatPicker:
    """폴더가 다 소진될 때까지 중복 없이 뽑고, 다 뽑으면 다시 섞어서 리셋."""

    def __init__(self, files):
        self.files = files
        self.pool = []

    def pick(self):
        if not self.pool:
            self.pool = self.files[:]
            random.shuffle(self.pool)
        return self.pool.pop()


def make_picker(mode, files):
    if mode == "random":
        return RandomNoRepeatPicker(files)
    return SequentialPicker(files)


def list_pose_images(poses_dir, pose_set):
    d = Path(poses_dir) / POSE_CHAR_NO / pose_set
    if not d.is_dir():
        return []
    return sorted(
        p for p in d.iterdir()
        if p.is_file() and p.suffix.lower() in POSE_IMAGE_EXTENSIONS
    )


# ComfyUI 연동 ----------------------------------------------------------------

def upload_image_to_comfy(comfy_url, image_path):
    """ComfyUI 자신의 input 폴더에 이미지를 올리고, LoadImage에서 참조할 파일명을
    돌려받는다. 모듈 docstring의 "ComfyUI로의 이미지 주입 방식" 참고."""
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


def apply_pose_image(workflow, comfy_url, pose_path):
    node_id, node = find_node(
        workflow,
        title_substring=env("POSE_NODE_TITLE", "Load"),
        class_types=("LoadImage",),
        prefer_connected=True,
    )
    if node is None:
        print("[pose_batch] 경고: 포즈 이미지를 넣을 노드를 찾지 못했습니다 (LoadImage 없음)", file=sys.stderr)
        return
    uploaded_name = upload_image_to_comfy(comfy_url, pose_path)
    node.setdefault("inputs", {})["image"] = uploaded_name


def apply_seed(workflow, seed):
    node_id, node = find_node(
        workflow,
        title_substring=env("SEED_NODE_TITLE", "KSampler"),
        class_types=("KSampler", "KSamplerAdvanced"),
    )
    if node is None:
        print("[pose_batch] 경고: 시드를 넣을 노드를 찾지 못했습니다 (KSampler 없음)", file=sys.stderr)
        return
    node.setdefault("inputs", {})["seed"] = seed


def apply_filename_prefix(workflow, index, seed, pose_path):
    node_id, node = find_node(
        workflow,
        title_substring=env("SAVE_NODE_TITLE", "Save"),
        class_types=("SaveImage", "SaveImageWebsocket"),
    )
    if node is None:
        return
    pose_stem = sanitize_stem(pose_path.stem)
    node.setdefault("inputs", {})["filename_prefix"] = f"pose_batch_{index}_seed{seed}_{pose_stem}"


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
        print(f"[pose_batch] 경고: 진행 상황 보고 실패: {e}", file=sys.stderr)


def append_manifest(output_dir, record):
    try:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        path = Path(output_dir) / "pose_batch_manifest.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[pose_batch] 경고: 재현성 기록 실패: {e}", file=sys.stderr)


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


def run_once(base_workflow, comfy_url, seed, pose_path, index, job_id, output_dir):
    workflow = copy.deepcopy(base_workflow)
    apply_seed(workflow, seed)
    apply_pose_image(workflow, comfy_url, pose_path)
    apply_filename_prefix(workflow, index, seed, pose_path)

    prompt_id = queue_prompt(comfy_url, workflow)
    print(f"[pose_batch] [{index}] seed={seed} pose={pose_path.name} 큐 등록 (prompt_id={prompt_id})")
    wait_for_completion(comfy_url, prompt_id)
    print(f"[pose_batch] [{index}] 완료")

    append_manifest(output_dir, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "job_id": job_id,
        "index": index,
        "pose_set": pose_path.parent.name,
        "pose_file": pose_path.name,
        "seed": seed,
        "prompt_id": prompt_id,
    })


def main():
    workflow_path = env("WORKFLOW_PATH")
    pose_count_raw = env("POSE_COUNT")
    pose_set = env("POSE_SET")
    if not workflow_path or not pose_count_raw or not pose_set:
        print("[pose_batch] WORKFLOW_PATH, POSE_COUNT, POSE_SET 환경변수가 모두 필요합니다.", file=sys.stderr)
        sys.exit(1)

    pose_count = int(pose_count_raw)
    pose_mode = env("POSE_MODE", "sequential")
    poses_dir = env("NIGHTSHIFT_POSES_DIR", "/workspace/dataset/poses")
    output_dir = env("NIGHTSHIFT_OUTPUT_DIR", "/workspace/output")

    pose_files = list_pose_images(poses_dir, pose_set)
    if not pose_files:
        # nightshift가 업로드 시점에 이미 확인했어야 하지만, 그 사이 폴더가 비워졌을
        # 수도 있으니 실행 시점에도 한 번 더 확인한다.
        print(f"[pose_batch] '{pose_set}' 포즈 세트에 이미지가 없습니다 ({poses_dir}/{POSE_CHAR_NO}/{pose_set})", file=sys.stderr)
        sys.exit(1)

    base_workflow = load_workflow(workflow_path)
    comfy_url = env("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
    job_id = env("JOB_ID")
    nightshift_url = env("NIGHTSHIFT_URL", "http://127.0.0.1:8000")

    picker = make_picker(pose_mode, pose_files)

    print(f"[pose_batch] 총 {pose_count}건 제출 예정 (포즈 세트: {pose_set}, {len(pose_files)}장, 방식: {pose_mode})")
    report_progress(job_id, nightshift_url, pose_count, 0)

    done = 0
    for index in range(1, pose_count + 1):
        seed = random.randint(0, 2**31 - 1)
        pose_path = picker.pick()
        run_once(base_workflow, comfy_url, seed, pose_path, index, job_id, output_dir)
        done += 1
        report_progress(job_id, nightshift_url, pose_count, done)

    print(f"[pose_batch] 총 {pose_count}건 완료")


if __name__ == "__main__":
    main()
