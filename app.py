"""
RunPod Job Queue — 등록된 스크립트 템플릿을 큐에 쌓아두면 워커가 순서대로 하나씩 실행한다.

실행:
    pip install -r requirements.txt
    python3 app.py
    (RunPod라면 8188 등 이미 쓰는 포트와 겹치지 않게 8000번을 열어둠)

    pm2로 백그라운드 실행 + 코드 변경 자동 반영 + 깔끔한 로그를 원하면
    `npm install && npm start`를 대신 쓴다 (README "실행 방법" 참고,
    설정은 ecosystem.config.js).

접속:
    브라우저에서 http://<pod-ip>:8000  (RunPod는 포트 8000을 프록시로 노출해야 함)
"""

import asyncio
import csv
import hashlib
import io
import json
import logging
import os
import queue
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile
from PIL import Image

from email_sender import EmailSendError, find_image_files, send_output_images
from output_images import (
    IMAGE_EXTENSIONS,
    OUTPUT_DIR,
    OutputFolderError,
    delete_output_images,
    list_output_images,
    rotate_landscape_images,
)
from pose_assets import (
    DEFAULT_CHAR_NO,
    PoseAssetError,
    PoseReferenceError,
    list_assets_tree,
    list_char_nos,
    parse_char_no,
    resolve_pose_reference,
    validate_pose_set,
)

# 프론트엔드(static/index.html)가 작업 목록/ComfyUI 연결 상태를 실시간처럼 보여주려고
# 브라우저 탭마다 GET /api/jobs를 2초, GET /api/comfy-status를 5초 간격으로 계속
# 폴링한다. uvicorn은 기본으로 모든 요청을 access log에 남기는데, 이 두 요청은
# 정상적으로 계속 반복되는 게 원래 동작이라 로그를 채우기만 하고(특히 pm2로 오래
# 띄워두면 파일에 계속 쌓임) 업로드/삭제/에러 같은 실제로 봐야 할 로그를 파묻는다.
# 다른 요청은 그대로 로그에 남기고 이 두 개만 걸러낸다.
class _SuppressPollingAccessLogs(logging.Filter):
    NOISY_REQUEST_LINES = ('"GET /api/jobs HTTP/1.1"', '"GET /api/comfy-status HTTP/1.1"')

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(line in message for line in self.NOISY_REQUEST_LINES)


logging.getLogger("uvicorn.access").addFilter(_SuppressPollingAccessLogs())

BASE_DIR = Path(__file__).parent
JOBS_DIR = BASE_DIR / "jobs"
LOGS_DIR = BASE_DIR / "logs"
TEMPLATES_DIR = BASE_DIR / "templates"
MANIFEST_PATH = TEMPLATES_DIR / "manifest.json"
STATE_FILE = BASE_DIR / "jobs_state.json"
JOBS_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

# "워크플로우 (.json)" 슬롯 옆의 "최근 워크플로우" 버튼이 쓰는, 지금까지 업로드된
# 워크플로우 파일들의 사본. 작업(jobs/)과는 독립적인 저장소다 — 작업이 삭제되거나
# DELETED_JOBS_RETENTION을 넘겨 완전히 정리돼도 여기 사본은 남아있어서, 어떤 작업에
# 썼었는지와 무관하게 "최근에 올렸던 워크플로우 파일" 자체를 다시 고를 수 있다.
RECENT_WORKFLOWS_DIR = BASE_DIR / "recent_workflows"
RECENT_WORKFLOWS_STATE_FILE = BASE_DIR / "recent_workflows_state.json"
RECENT_WORKFLOWS_DIR.mkdir(exist_ok=True)
RECENT_WORKFLOWS_RETENTION = int(os.environ.get("NIGHTSHIFT_RECENT_WORKFLOWS_RETENTION", "30"))
recent_workflows: list[dict] = []  # 최신순. [{id, filename, stored_filename, uploaded_at, content_hash}, ...]

# ComfyUI는 같은 파드 안에서 돌아가지만 설치 방식에 따라 포트가 다를 수 있어, 이 후보들을
# 순서대로 짧은 타임아웃으로 찔러보고 처음 응답하는 곳을 채택한다. COMFY_URL 환경변수가
# 명시적으로 설정돼 있으면 이 감지 과정을 건너뛰고 그 값을 그대로 쓴다.
COMFY_CANDIDATE_URLS = ["http://127.0.0.1:8188", "http://127.0.0.1:8000"]
COMFY_CHECK_TIMEOUT = 2

# 템플릿 스크립트가 자기 자신의 진행 상황(PUT /api/jobs/{job_id}/progress)을
# 보고할 때 사용하는, nightshift 자신의 주소. 서버가 항상 이 포트로 뜨므로 고정값.
SELF_URL = "http://127.0.0.1:8000"

# "새 작업 추가"의 메인 프롬프트 필드에 있는 "Prompt Enhance" 버튼이 쓰는, 프롬프트를
# 다듬어주는 전용 ComfyUI 워크플로우. 배치 템플릿과 달리 사용자가 매번 업로드하는 게
# 아니라 저장소에 고정으로 들어있는 자산이다 — user_prompt라는 제목의 노드에 원문을
# 넣고 실행한 뒤, 미리보기(PreviewAny) 노드의 출력을 개선된 프롬프트로 읽어온다.
# isDanbooru_sys? 라는 제목의 ComfySwitchNode가 "자연어로 다듬기(7번 노드)" /
# "그 결과를 다시 Danbooru 태그로 변환(13번 노드)" 두 경로를 고르므로, 요청받은
# 모드(자연어/Danbooru)에 맞춰 이 스위치 노드의 switch 입력을 켜고 끈다.
ENHANCER_WORKFLOW_PATH = BASE_DIR / "prompt_enhancer.json"
ENHANCER_INPUT_NODE_TITLE = "user_prompt"
ENHANCER_OUTPUT_NODE_TITLE = os.environ.get("NIGHTSHIFT_ENHANCER_OUTPUT_NODE_TITLE", "미리보기")
ENHANCER_MODE_NODE_TITLE = os.environ.get("NIGHTSHIFT_ENHANCER_MODE_NODE_TITLE", "isDanbooru_sys")
ENHANCE_TIMEOUT_SEC = float(os.environ.get("NIGHTSHIFT_ENHANCE_TIMEOUT_SEC", "120"))
ENHANCE_POLL_INTERVAL_SEC = float(os.environ.get("NIGHTSHIFT_ENHANCE_POLL_INTERVAL_SEC", "1"))

# "작업 목록"에서 삭제한 작업은 실제로는 지우지 않고 deleted 플래그만 세워서
# (소프트 삭제) 상단의 "삭제된 작업 설정 불러오기" 드롭다운에서 계속 고를 수 있게
# 한다. 디스크가 무한정 늘어나지 않도록 가장 최근 이만큼만 남기고, 그보다 오래
# 삭제된 작업은 워크플로우/CSV 파일까지 완전히 지운다.
DELETED_JOBS_RETENTION = int(os.environ.get("NIGHTSHIFT_DELETED_JOBS_RETENTION", "30"))

job_queue: "queue.Queue[str]" = queue.Queue()
jobs: dict[str, dict] = {}
lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_state():
    # 큐 자체는 프로세스가 죽으면 사라지지만, 이력 조회는 재시작 후에도 가능하게 기록만 남긴다.
    with lock:
        with open(STATE_FILE, "w") as f:
            json.dump(jobs, f, indent=2, default=str)


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            jobs.update(json.load(f))


def save_recent_workflows_state():
    with open(RECENT_WORKFLOWS_STATE_FILE, "w") as f:
        json.dump(recent_workflows, f, indent=2, default=str)


def load_recent_workflows_state():
    if RECENT_WORKFLOWS_STATE_FILE.exists():
        with open(RECENT_WORKFLOWS_STATE_FILE) as f:
            recent_workflows.extend(json.load(f))


def record_recent_workflow(filename: str, content: bytes):
    # /api/upload가 어떤 템플릿이든 워크플로우를 성공적으로 받을 때마다 호출한다.
    # 같은 내용(해시)의 워크플로우가 이미 목록에 있으면 새로 저장하지 않고 맨 앞으로
    # 올리기만 한다 — 안 그러면 같은 파일을 반복해서 재사용할 때마다 사본이 계속
    # 쌓여서 "최근 워크플로우" 목록이 중복으로 도배된다.
    content_hash = hashlib.sha256(content).hexdigest()
    with lock:
        existing = next((w for w in recent_workflows if w["content_hash"] == content_hash), None)
        if existing:
            recent_workflows.remove(existing)
            existing["uploaded_at"] = now_iso()
            existing["filename"] = filename
            recent_workflows.insert(0, existing)
        else:
            entry_id = str(uuid.uuid4())[:8]
            stored_filename = f"{entry_id}_{filename}"
            (RECENT_WORKFLOWS_DIR / stored_filename).write_bytes(content)
            recent_workflows.insert(0, {
                "id": entry_id,
                "filename": filename,
                "stored_filename": stored_filename,
                "uploaded_at": now_iso(),
                "content_hash": content_hash,
            })
        while len(recent_workflows) > RECENT_WORKFLOWS_RETENTION:
            old = recent_workflows.pop()
            (RECENT_WORKFLOWS_DIR / old["stored_filename"]).unlink(missing_ok=True)
        save_recent_workflows_state()


def prune_deleted_jobs():
    # 소프트 삭제된 작업 중 DELETED_JOBS_RETENTION개를 넘는 오래된 것들은 이제
    # 완전히 정리한다(레코드 + 워크플로우/CSV 파일). lock을 쥔 채로 호출해야 한다.
    deleted = sorted(
        (job for job in jobs.values() if job.get("deleted")),
        key=lambda job: job.get("deleted_at") or "",
        reverse=True,
    )
    for job in deleted[DELETED_JOBS_RETENTION:]:
        for field in ("workflow_filename", "csv_filename"):
            filename = job.get(field)
            if filename:
                (JOBS_DIR / filename).unlink(missing_ok=True)
        del jobs[job["id"]]


def load_templates_list() -> list[dict]:
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_templates_map() -> dict[str, dict]:
    return {t["id"]: t for t in load_templates_list()}


def check_comfy_url(url: str) -> bool:
    try:
        req = urllib.request.Request(f"{url.rstrip('/')}/system_stats")
        with urllib.request.urlopen(req, timeout=COMFY_CHECK_TIMEOUT) as resp:
            return resp.status == 200
    except Exception:
        return False


def resolve_comfy_url() -> tuple[str | None, bool]:
    override = os.environ.get("COMFY_URL")
    if override:
        return override, check_comfy_url(override)
    for candidate in COMFY_CANDIDATE_URLS:
        if check_comfy_url(candidate):
            return candidate, True
    return None, False


# ---- 프롬프트 개선(Text Enhance) ----------------------------------------------
# templates/*.py의 find_node()/primitive_value_field()와 같은 알고리즘의 사본이다
# (그쪽은 배치 작업을 큐에 올려 python3 서브프로세스로 실행하는 것과 달리, 이건
# HTTP 요청 하나 처리하는 동안 서버가 직접 ComfyUI에 동기적으로 물어보고 기다리는
# 별개의 경로라 템플릿 스크립트를 그대로 재사용할 수 없다).
_ENHANCER_PRIMITIVE_VALUE_FIELDS = {
    "CLIPTextEncode": "text",
    "PrimitiveStringMultiline": "value",
    "PrimitiveString": "value",
}


def _enhancer_find_node(workflow: dict, title_substring: str | None = None, class_types: tuple = ()):
    title_substring = (title_substring or "").lower()
    fallback = None
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        meta_title = str(node.get("_meta", {}).get("title", "")).lower()
        class_type = node.get("class_type", "")
        if title_substring and title_substring in meta_title:
            return node_id, node
        if class_types and class_type in class_types and fallback is None:
            fallback = (node_id, node)
    return fallback if fallback else (None, None)


def _enhancer_primitive_value_field(node: dict) -> str | None:
    field = _ENHANCER_PRIMITIVE_VALUE_FIELDS.get(node.get("class_type", ""))
    if field:
        return field
    inputs = node.get("inputs", {})
    if "text" in inputs:
        return "text"
    if "value" in inputs:
        return "value"
    return None


def _enhancer_extract_text(history_entry: dict, node_id: str) -> str | None:
    # PreviewAny처럼 OUTPUT_NODE=True인 커스텀 노드는 보통 {"text": ["..."]}
    # 형태로 history의 outputs에 결과를 남기지만, 커스텀 노드마다 키 이름이
    # 다를 수 있어(예: "string", "value") 키 이름은 보지 않고 문자열 리스트인
    # 첫 값을 그대로 쓴다.
    outputs = (history_entry or {}).get("outputs", {}) or {}
    node_output = outputs.get(node_id)
    if not isinstance(node_output, dict):
        return None
    for value in node_output.values():
        if isinstance(value, list) and value and isinstance(value[0], str):
            return value[0]
    return None


def _enhance_prompt_sync(user_prompt: str, mode: str = "natural") -> str:
    """ComfyUI에 prompt_enhancer.json 워크플로우를 제출하고 완료될 때까지 동기적으로
    기다린 뒤 개선된 프롬프트 문자열을 돌려준다. mode가 "danbooru"면 자연어로 다듬은
    결과를 다시 Danbooru 태그 목록으로 변환하는 경로를 태운다(isDanbooru_sys? 스위치
    노드). urllib(블로킹 I/O)를 쓰므로 반드시 asyncio.to_thread로 감싸서 호출해야 한다."""
    if not ENHANCER_WORKFLOW_PATH.exists():
        raise HTTPException(500, "프롬프트 개선용 워크플로우(prompt_enhancer.json)가 서버에 없어요.")
    try:
        workflow = json.loads(ENHANCER_WORKFLOW_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"프롬프트 개선용 워크플로우가 올바른 JSON이 아니에요: {e}")

    input_node_id, input_node = _enhancer_find_node(workflow, title_substring=ENHANCER_INPUT_NODE_TITLE)
    input_field = _enhancer_primitive_value_field(input_node) if input_node is not None else None
    if input_field is None:
        raise HTTPException(
            500,
            f"프롬프트 개선용 워크플로우에서 입력 노드를 찾지 못했어요 "
            f"(제목에 '{ENHANCER_INPUT_NODE_TITLE}'가 포함된 텍스트 노드 없음).",
        )
    input_node.setdefault("inputs", {})[input_field] = user_prompt

    # 모드를 고를 스위치 노드가 없는(예전 워크플로우로 되돌린) 경우에도 전체 기능이
    # 깨지지 않도록, 못 찾으면 조용히 건너뛰고 워크플로우에 이미 설정된 기본 경로를
    # 그대로 쓴다.
    mode_node_id, mode_node = _enhancer_find_node(
        workflow,
        title_substring=ENHANCER_MODE_NODE_TITLE,
        class_types=("ComfySwitchNode",),
    )
    if mode_node is not None:
        mode_node.setdefault("inputs", {})["switch"] = (mode == "danbooru")

    output_node_id, output_node = _enhancer_find_node(
        workflow,
        title_substring=ENHANCER_OUTPUT_NODE_TITLE,
        class_types=("PreviewAny",),
    )
    if output_node is None:
        raise HTTPException(500, "프롬프트 개선용 워크플로우에서 결과를 읽어올 출력 노드를 찾지 못했어요.")

    comfy_url, connected = resolve_comfy_url()
    if not connected:
        raise HTTPException(503, "ComfyUI 서버에 연결할 수 없어요.")

    payload = json.dumps({"prompt": workflow, "client_id": str(uuid.uuid4())}).encode("utf-8")
    req = urllib.request.Request(
        f"{comfy_url}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            submit_data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise HTTPException(502, f"ComfyUI에 프롬프트를 제출하지 못했어요: {e}")
    if "error" in submit_data:
        raise HTTPException(502, f"ComfyUI가 프롬프트를 거부했어요: {submit_data['error']}")
    prompt_id = submit_data["prompt_id"]

    deadline = time.time() + ENHANCE_TIMEOUT_SEC
    while time.time() < deadline:
        try:
            hist_req = urllib.request.Request(f"{comfy_url}/history/{prompt_id}")
            with urllib.request.urlopen(hist_req, timeout=30) as resp:
                history = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise HTTPException(502, f"ComfyUI 히스토리 조회에 실패했어요: {e}")
        if prompt_id in history:
            text = _enhancer_extract_text(history[prompt_id], output_node_id)
            if text is None:
                raise HTTPException(500, "프롬프트 개선 결과를 읽지 못했어요 (출력 노드에 텍스트가 없음).")
            return text.strip()
        time.sleep(ENHANCE_POLL_INTERVAL_SEC)
    raise HTTPException(504, f"프롬프트 개선이 {int(ENHANCE_TIMEOUT_SEC)}초 안에 끝나지 않았어요.")


def worker_loop():
    while True:
        job_id = job_queue.get()
        with lock:
            job = jobs[job_id]
            job["status"] = "running"
            job["started_at"] = now_iso()
            job["progress"] = None
        save_state()

        log_path = LOGS_DIR / f"{job_id}.log"
        script_path = TEMPLATES_DIR / job["script_filename"]

        comfy_url, comfy_connected = resolve_comfy_url()
        if not comfy_connected:
            log_path.write_text("ComfyUI 서버에 연결할 수 없습니다\n")
            with lock:
                job["status"] = "failed"
                job["returncode"] = -1
                job["finished_at"] = now_iso()
            save_state()
            job_queue.task_done()
            continue

        extra_env = {"COMFY_URL": comfy_url, "JOB_ID": job_id, "NIGHTSHIFT_URL": SELF_URL}
        if job.get("workflow_filename"):
            extra_env["WORKFLOW_PATH"] = str((JOBS_DIR / job["workflow_filename"]).resolve())
        if job.get("csv_filename"):
            extra_env["CSV_PATH"] = str((JOBS_DIR / job["csv_filename"]).resolve())
        for name, value in job.get("options", {}).items():
            extra_env[name.upper()] = str(value)
        env = {**os.environ, **extra_env}

        with open(log_path, "w") as logf:
            try:
                proc = subprocess.run(
                    ["python3", "-u", str(script_path)],
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    env=env,
                )
                returncode = proc.returncode
            except Exception as e:
                logf.write(f"\n[runner error] {e}\n")
                returncode = -1

        with lock:
            job["status"] = "done" if returncode == 0 else "failed"
            job["returncode"] = returncode
            job["finished_at"] = now_iso()
        save_state()
        job_queue.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_state()
    load_recent_workflows_state()
    # 재시작 전에 running/queued 상태로 남아있던 기록은 재실행되지 않으므로 상태만 정리
    with lock:
        for job in jobs.values():
            if job["status"] in ("queued", "running"):
                job["status"] = "interrupted"
    save_state()
    threading.Thread(target=worker_loop, daemon=True).start()
    yield


app = FastAPI(title="RunPod Job Queue", lifespan=lifespan)


@app.get("/api/templates")
def list_templates():
    return load_templates_list()


@app.get("/api/comfy-status")
async def comfy_status():
    url, connected = await asyncio.to_thread(resolve_comfy_url)
    return {"url": url, "connected": connected}


@app.post("/api/enhance-prompt")
async def enhance_prompt(request: Request):
    # "새 작업 추가"의 메인 프롬프트 옆 "Prompt Enhance" 버튼이 호출한다. 큐에 올리는
    # 배치 작업과 달리 응답을 바로 화면에 보여줘야 하므로 워커 큐를 거치지 않고
    # 이 요청을 처리하는 동안 ComfyUI에 동기적으로(스레드로 감싸서) 물어본다.
    data = await request.json()
    prompt = str(data.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "개선할 프롬프트를 입력하세요.")
    mode = data.get("mode") if data.get("mode") in ("natural", "danbooru") else "natural"
    enhanced = await asyncio.to_thread(_enhance_prompt_sync, prompt, mode)
    return {"enhanced": enhanced}


@app.get("/api/assets")
def list_assets():
    # 업로드 폼의 "인물 수"/"포즈 세트" 캐스케이딩 드롭다운을 채우는 용도. char_no별
    # 포즈 세트 목록을 트리로 한 번에 돌려줘서, 프론트엔드가 "인물 수"를 바꿀 때마다
    # 서버에 다시 요청하지 않고도 "포즈 세트" 드롭다운을 그 자리에서 다시 채울 수
    # 있게 한다. 매 호출마다 폴더를 다시 스캔해서 방금 새로 올려둔 세트도 반영한다.
    return {"char_nos": list_assets_tree()}


def coerce_option(option: dict, raw: str | None, options_so_far: dict):
    if raw is None or raw == "":
        raw = option.get("default")
    opt_type = option.get("type")

    if opt_type == "number":
        try:
            num = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(400, f"'{option['label']}' 값이 올바른 숫자가 아니에요.")
        return int(num) if num.is_integer() else num

    if opt_type == "number_optional":
        # width/height처럼 비워두면 워크플로우에 이미 들어있는 값을 그대로 두는
        # 게 정상 동작인 숫자 옵션. "number"와 달리 빈 값을 기본값으로 치환하지
        # 않고(위에서 raw = default로 대체됐더라도 default 자체가 빈 문자열이면
        # 그대로 빈 채로) 그대로 통과시킨다.
        if raw is None or str(raw).strip() == "":
            return ""
        try:
            num = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(400, f"'{option['label']}' 값이 올바른 숫자가 아니에요.")
        return int(num) if num.is_integer() else num

    if opt_type == "select":
        choices = option.get("choices") or []
        if raw not in choices:
            raise HTTPException(400, f"'{option['label']}' 값은 {choices} 중 하나여야 해요.")
        return raw

    if opt_type == "char_no":
        # "인물 수" 드롭다운 — 실제 존재하는 POSES_DIR 하위 숫자 폴더 중 하나여야 함.
        value = "" if raw is None else str(raw)
        if value not in list_char_nos():
            raise HTTPException(400, f"'{option['label']}' 값이 올바르지 않아요.")
        return value

    if opt_type == "asset_folder":
        # 잡을 큐에 올리는 시점(업로드 시)에 포즈 세트 폴더가 실제로 있고 이미지가
        # 있는지 미리 확인해서, 큐 시작 이후에야 실패하는 일이 없게 한다. 같은
        # 템플릿에 "char_no" 타입 옵션이 있으면(manifest에서 이 옵션보다 앞에
        # 선언돼 있어야 함) 그 값을 스코프로 쓰고, 없으면 DEFAULT_CHAR_NO를 쓴다.
        value = "" if raw is None else str(raw)
        char_no = options_so_far.get("char_no", DEFAULT_CHAR_NO)
        try:
            validate_pose_set(value, char_no)
        except PoseAssetError as e:
            raise HTTPException(400, str(e))
        return value

    return "" if raw is None else str(raw)


def validate_pose_csv_rows(csv_bytes: bytes):
    # pose_csv_batch 전용 업로드 시점 검증: CSV의 모든 행을 미리 훑어 pose(및
    # char_no) 컬럼 값이 실제로 해석 가능한지(resolve_pose_reference) 확인한다.
    # 스크립트가 실행되다가 특정 행에서야 실패하는 일이 없도록, 한 행이라도
    # 문제가 있으면 업로드 자체를 거부한다. char_no는 pose를 지정한 행에서만
    # 의미가 있으므로(포즈 폴더의 탐색 루트일 뿐), pose가 비어 있는 행은 건드리지 않는다.
    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "CSV가 유효한 UTF-8 텍스트가 아니에요.")

    rows = list(csv.DictReader(io.StringIO(text)))
    errors = []
    for line_no, row in enumerate(rows, start=2):  # 헤더가 1번 줄
        pose = (row.get("pose") or "").strip()
        if not pose:
            continue
        try:
            char_no = parse_char_no(row.get("char_no"))
        except ValueError:
            errors.append(f"{line_no}번째 줄(char_no='{row.get('char_no')}'): 정수가 아니에요.")
            continue
        try:
            resolve_pose_reference(pose, char_no)
        except PoseReferenceError as e:
            errors.append(f"{line_no}번째 줄(pose='{pose}', char_no='{char_no}'): {e}")

    if errors:
        raise HTTPException(400, "CSV의 pose/char_no 컬럼을 확인하세요.\n" + "\n".join(errors))


def csv_has_pose_reference(csv_bytes: bytes) -> bool:
    # pose_csv_batch는 CSV 행마다 pose가 비어 있으면 그 행은 의도적으로 ControlNet
    # 없이 생성한다(README "CSV + 포즈 배치" 절 참고) — 모든 행의 pose가 비어 있으면
    # 워크플로우에 LoadImage 노드가 없어도 문제가 없으므로, 그런 경우까지 아래
    # validate_workflow_has_pose_node()가 막아버리지 않도록 미리 구분해둔다.
    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False  # 디코딩 자체는 validate_pose_csv_rows가 이미 걸러줌
    rows = csv.DictReader(io.StringIO(text))
    return any((row.get("pose") or "").strip() for row in rows)


POSE_NODE_REQUIRED_TEMPLATES = {"pose_batch", "pose_csv_batch"}


def find_pose_load_image_node(workflow: dict, title_substring: str):
    """templates/pose_batch.py·pose_csv_batch.py의 apply_pose_image()가 실행 시점에
    실제로 어떤 노드를 골라 포즈 이미지를 주입할지 업로드 시점에 미리 예측한다 —
    두 스크립트의 find_node()와 정확히 같은 알고리즘이다: 제목에 title_substring이
    포함된 노드가 있으면 class_type과 무관하게 그 노드를 최우선으로 고르고(그래서
    "Load Checkpoint"처럼 우연히 제목에 "Load"가 들어간 LoadImage가 아닌 노드가
    먼저 골라질 수 있다 — 이 경우도 아래에서 "포즈 노드 없음"으로 취급해야 함),
    없으면 다른 노드의 입력에 실제로 연결된 LoadImage 노드를 우선으로 고른다."""
    title_substring = (title_substring or "").lower()
    connected = set()
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        for value in node.get("inputs", {}).values():
            if isinstance(value, list) and len(value) == 2:
                connected.add(str(value[0]))
    fallback = None
    fallback_connected = None
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        meta_title = str(node.get("_meta", {}).get("title", "")).lower()
        class_type = node.get("class_type", "")
        if title_substring and title_substring in meta_title:
            return node
        if class_type == "LoadImage":
            if node_id in connected:
                if fallback_connected is None:
                    fallback_connected = node
            elif fallback is None:
                fallback = node
    return fallback_connected or fallback


def validate_workflow_has_pose_node(workflow_bytes: bytes):
    # pose_batch/pose_csv_batch는 포즈 레퍼런스 이미지를 LoadImage 노드에 주입해야
    # ControlNet이 실제로 동작한다. 이 노드가 없는 워크플로우를 잘못 올리면, 실행
    # 스크립트는 stderr에 경고만 남기고 포즈 없이 이미지 생성을 계속 진행한다 —
    # 큐에 올리기 전에 미리 걸러서 그런 사고를 막는다.
    try:
        workflow = json.loads(workflow_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise HTTPException(400, f"워크플로우가 올바른 JSON이 아니에요: {e}")
    if not isinstance(workflow, dict):
        raise HTTPException(400, "워크플로우가 올바른 ComfyUI API 형식(JSON 객체)이 아니에요.")

    title_substring = os.environ.get("POSE_NODE_TITLE", "Load")
    node = find_pose_load_image_node(workflow, title_substring)
    if node is None or node.get("class_type") != "LoadImage":
        raise HTTPException(
            400,
            "이 워크플로우에는 포즈 이미지를 넣을 LoadImage 노드가 없어요. "
            f"(제목에 '{title_substring}'가 포함된 노드가 있다면 LoadImage가 아니고, "
            "그런 노드가 아예 없다면 다른 LoadImage 노드도 찾지 못했어요.) 포즈 참조 "
            "없이 이미지가 생성되는 사고를 막기 위해 업로드를 거부했어요 — 워크플로우에 "
            "LoadImage 노드를 추가하거나 제목을 확인한 뒤 다시 업로드하세요.",
        )


@app.get("/api/recent-workflows")
def list_recent_workflows():
    # "새 작업 추가"의 워크플로우 슬롯 옆 "최근 워크플로우" 버튼이 호출한다.
    with lock:
        return {
            "workflows": [
                {"id": w["id"], "filename": w["filename"], "uploaded_at": w["uploaded_at"]}
                for w in recent_workflows
            ],
            "retention": RECENT_WORKFLOWS_RETENTION,
        }


@app.get("/api/recent-workflows/{workflow_id}")
def get_recent_workflow(workflow_id: str):
    with lock:
        entry = next((w for w in recent_workflows if w["id"] == workflow_id), None)
    if entry is None:
        raise HTTPException(404, "해당 워크플로우를 찾을 수 없어요.")
    path = RECENT_WORKFLOWS_DIR / entry["stored_filename"]
    if not path.exists():
        raise HTTPException(404, "워크플로우 파일이 서버에 없어요.")
    return Response(content=path.read_text(encoding="utf-8"), media_type="application/json")


@app.post("/api/upload")
async def upload(request: Request):
    form = await request.form()

    template_id = form.get("template_id")
    if not template_id:
        raise HTTPException(400, "template_id는 필수예요.")
    template = load_templates_map().get(template_id)
    if template is None:
        raise HTTPException(400, "존재하지 않는 템플릿이에요.")

    workflow = form.get("workflow")
    if not isinstance(workflow, UploadFile) or not workflow.filename or not workflow.filename.endswith(".json"):
        raise HTTPException(400, "워크플로우는 json 파일만 업로드할 수 있어요.")

    requires_csv = bool(template.get("requires_csv"))
    csv_file = form.get("csv")
    if requires_csv and (not isinstance(csv_file, UploadFile) or not csv_file.filename or not csv_file.filename.endswith(".csv")):
        raise HTTPException(400, "이 템플릿은 csv 파일이 필요해요.")

    # UploadFile은 한 번만 읽을 수 있으므로, 검증에도 쓰고 저장에도 쓸 수 있게
    # 여기서 미리 한 번만 읽어둔다.
    workflow_bytes = await workflow.read()
    csv_bytes = await csv_file.read() if requires_csv else None
    if template_id == "pose_csv_batch" and csv_bytes is not None:
        validate_pose_csv_rows(csv_bytes)

    if template_id in POSE_NODE_REQUIRED_TEMPLATES:
        needs_pose_node = True
        if template_id == "pose_csv_batch":
            needs_pose_node = csv_bytes is not None and csv_has_pose_reference(csv_bytes)
        if needs_pose_node:
            validate_workflow_has_pose_node(workflow_bytes)

    options = {}
    for option in template.get("options", []):
        raw = form.get(option["name"])
        options[option["name"]] = coerce_option(option, raw if isinstance(raw, str) else None, options)

    job_id = str(uuid.uuid4())[:8]

    workflow_dest_name = f"{job_id}_{workflow.filename}"
    (JOBS_DIR / workflow_dest_name).write_bytes(workflow_bytes)
    record_recent_workflow(workflow.filename, workflow_bytes)

    csv_dest_name = None
    csv_original_name = None
    if requires_csv:
        csv_dest_name = f"{job_id}_{csv_file.filename}"
        (JOBS_DIR / csv_dest_name).write_bytes(csv_bytes)
        csv_original_name = csv_file.filename

    with lock:
        jobs[job_id] = {
            "id": job_id,
            "template_id": template_id,
            "template_label": template["label"],
            "script_filename": template["script_filename"],
            "options": options,
            "workflow_filename": workflow_dest_name,
            "workflow_original_name": workflow.filename,
            "csv_filename": csv_dest_name,
            "csv_original_name": csv_original_name,
            "status": "pending",
            "queued_at": now_iso(),
            "started_at": None,
            "finished_at": None,
            "returncode": None,
            "progress": None,
            "deleted": False,
            "deleted_at": None,
        }
    save_state()
    return jobs[job_id]


@app.post("/api/queue/start")
def start_queue():
    with lock:
        pending = sorted(
            (j for j in jobs.values() if j["status"] == "pending"),
            key=lambda j: j["queued_at"],
        )
        for job in pending:
            job["status"] = "queued"
    save_state()
    for job in pending:
        job_queue.put(job["id"])
    return {"started": len(pending)}


@app.get("/api/jobs")
def list_jobs():
    with lock:
        ordered = sorted(
            (j for j in jobs.values() if not j.get("deleted")),
            key=lambda j: j["queued_at"],
            reverse=True,
        )
    pending_ids = list(job_queue.queue)
    return {"jobs": ordered, "pending_count": len(pending_ids)}


@app.get("/api/jobs/deleted")
def list_deleted_jobs():
    # "작업 목록"에서 삭제한 작업들 — 상단의 "삭제된 작업 설정 불러오기" 드롭다운을
    # 채우는 용도. 소프트 삭제이므로 워크플로우/CSV는 prune_deleted_jobs()가 지우기
    # 전까지 GET /api/jobs/{id}/workflow·csv로 계속 읽을 수 있다.
    with lock:
        ordered = sorted(
            (j for j in jobs.values() if j.get("deleted")),
            key=lambda j: j.get("deleted_at") or "",
            reverse=True,
        )
    return {"jobs": ordered, "retention": DELETED_JOBS_RETENTION}


@app.get("/api/jobs/{job_id}/log")
def job_log(job_id: str, tail: int = 200):
    log_path = LOGS_DIR / f"{job_id}.log"
    if not log_path.exists():
        return {"log": ""}
    lines = log_path.read_text(errors="replace").splitlines()
    return {"log": "\n".join(lines[-tail:])}


@app.put("/api/jobs/{job_id}/progress")
async def update_job_progress(job_id: str, request: Request):
    # 실행 중인 템플릿 스크립트가 자기 진행 상황(전체/완료 이미지 수)을 스스로 보고하는
    # 용도. 매 이미지마다 호출될 수 있어 디스크 쓰기(save_state)는 하지 않고 메모리만 갱신한다
    # — 서버가 재시작되면 어차피 그 작업은 interrupted 처리되어 진행률의 의미가 없어진다.
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")

    total = data.get("total")
    done = data.get("done")
    if not isinstance(total, int) or not isinstance(done, int) or total < 0 or done < 0:
        raise HTTPException(400, "total/done은 0 이상의 정수여야 해요.")

    with lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "없는 작업이에요.")
        job["progress"] = {"total": total, "done": done}

    return {"ok": True}


def read_job_attachment(job_id: str, field: str, missing_msg: str) -> str:
    with lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "없는 작업이에요.")
        filename = job.get(field)

    if not filename:
        raise HTTPException(404, missing_msg)
    path = JOBS_DIR / filename
    if not path.exists():
        raise HTTPException(404, "파일을 찾을 수 없어요.")
    return path.read_text(encoding="utf-8")


async def write_job_attachment(job_id: str, field: str, request: Request, validate, missing_msg: str):
    body = await request.body()
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "유효한 UTF-8 텍스트가 아니에요.")
    validate(text)

    with lock:
        job = jobs.get(job_id)
        if not job or job.get("deleted"):
            raise HTTPException(404, "없는 작업이에요.")
        if job["status"] != "pending":
            raise HTTPException(400, "대기 중인 작업만 수정할 수 있어요.")
        filename = job.get(field)
        if not filename:
            raise HTTPException(404, missing_msg)
        (JOBS_DIR / filename).write_text(text, encoding="utf-8")


def validate_json_text(text: str):
    try:
        json.loads(text)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"유효한 JSON이 아니에요: {e}")


def validate_csv_text(text: str):
    rows = [row for row in csv.reader(io.StringIO(text)) if row]
    if not rows:
        raise HTTPException(400, "CSV 내용이 비어 있어요.")
    header_len = len(rows[0])
    for i, row in enumerate(rows[1:], start=2):
        if len(row) != header_len:
            raise HTTPException(400, f"{i}번째 줄의 열 개수가 헤더와 맞지 않아요 (헤더 {header_len}개, 이 줄 {len(row)}개).")


@app.get("/api/jobs/{job_id}/workflow")
def get_job_workflow(job_id: str):
    text = read_job_attachment(job_id, "workflow_filename", "워크플로우 파일이 없어요.")
    return Response(content=text, media_type="application/json")


@app.put("/api/jobs/{job_id}/workflow")
async def update_job_workflow(job_id: str, request: Request):
    await write_job_attachment(job_id, "workflow_filename", request, validate_json_text, "워크플로우 파일이 없어요.")
    return {"ok": True}


@app.get("/api/jobs/{job_id}/csv")
def get_job_csv(job_id: str):
    text = read_job_attachment(job_id, "csv_filename", "CSV 파일이 없어요.")
    return Response(content=text, media_type="text/csv")


@app.put("/api/jobs/{job_id}/csv")
async def update_job_csv(job_id: str, request: Request):
    await write_job_attachment(job_id, "csv_filename", request, validate_csv_text, "CSV 파일이 없어요.")
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    with lock:
        job = jobs.get(job_id)
        if not job or job.get("deleted"):
            raise HTTPException(404, "없는 작업이에요.")
        if job["status"] in ("running", "queued"):
            raise HTTPException(400, "실행 중이거나 이미 시작된 작업은 지울 수 없어요.")
        # 실제로 지우지 않고 소프트 삭제만 한다 — "작업 목록"에서는 사라지지만,
        # 상단의 "삭제된 작업 설정 불러오기" 드롭다운에서는 (보관 기간 안이면)
        # 계속 고를 수 있다. prune_deleted_jobs()가 오래된 것부터 완전히 정리한다.
        job["deleted"] = True
        job["deleted_at"] = now_iso()
        prune_deleted_jobs()
    save_state()
    return {"ok": True}


@app.post("/api/send-email")
async def send_email(request: Request):
    # SMTP 계정 정보는 이 요청 처리에만 쓰고 어디에도 저장하지 않는다 — jobs_state.json은
    # git으로 버전 관리되므로 비밀번호를 job 데이터에 남기면 커밋 이력에 그대로 남는다.
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")

    smtp_user = (data.get("smtp_user") or "").strip()
    smtp_password = data.get("smtp_password") or ""
    to_email = (data.get("to_email") or "").strip()

    if not smtp_user or not smtp_password or not to_email:
        raise HTTPException(400, "보내는 메일 계정/비밀번호/받는 메일 계정을 모두 입력하세요.")

    max_mb = data.get("max_mb", 20)
    try:
        max_mb = int(max_mb)
        if max_mb <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(400, "메일당 최대 용량(MB)은 1 이상의 정수여야 해요.")

    try:
        result = await asyncio.to_thread(send_output_images, smtp_user, smtp_password, to_email, max_mb)
    except EmailSendError as e:
        raise HTTPException(400, str(e))
    return result


def list_output_images_meta() -> list[dict]:
    # 갤러리 탭을 채우는 용도. zip/이메일 발송과 달리 폴더가 비어 있거나 아직 없는 것도
    # 정상 상태로 취급한다(뭔가 있어야 의미 있는 동작이 아니라, 그냥 목록을 보여줄 뿐이므로).
    try:
        files = list_output_images(OUTPUT_DIR)
    except OutputFolderError:
        return []
    items = []
    for f in files:
        stat = f.stat()
        items.append({
            "name": f.name,
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items


@app.get("/api/output-images")
def list_output_images_api():
    return {"images": list_output_images_meta()}


def resolve_output_image(filename: str) -> Path:
    # 파일명만 받고 경로 조작(예: "../../etc/passwd")은 막는다 — Path(filename).name으로
    # 디렉터리 구분자를 다 잘라낸 뒤, 원래 요청과 완전히 같을 때만(=애초에 순수 파일명
    # 이었을 때만) 통과시킨다.
    safe_name = Path(filename).name
    if not safe_name or safe_name != filename:
        raise HTTPException(400, "올바르지 않은 파일명이에요.")
    path = Path(OUTPUT_DIR) / safe_name
    if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise HTTPException(404, "이미지를 찾을 수 없어요.")
    return path


@app.get("/api/output-images/{filename}")
def get_output_image(filename: str):
    # 갤러리 라이트박스에서 원본 크기로 보여주는 용도.
    return FileResponse(resolve_output_image(filename))


@app.get("/api/output-images/{filename}/thumbnail")
def get_output_image_thumbnail(filename: str, size: int = 320):
    # 갤러리 격자를 채우는 용도 — 원본을 그대로 내려받으면 느리고 대역폭을 낭비하므로,
    # 매 요청마다 그 자리에서 축소본을 만들어 돌려준다(디스크에 캐시하지 않음 — 이
    # 도구 규모에서는 매번 다시 만들어도 충분히 빠름).
    path = resolve_output_image(filename)
    size = max(64, min(size, 800))
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail((size, size))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
    except Exception as e:
        raise HTTPException(500, f"썸네일을 만들지 못했어요: {e}")
    return Response(content=buf.getvalue(), media_type="image/jpeg")


@app.delete("/api/output-images/{filename}")
def delete_output_image(filename: str):
    # 갤러리에서 이미지 하나만 지우는 용도 — 되돌릴 수 없는 삭제라서, 확인 절차는
    # 프론트엔드(버튼 클릭 시 confirm 창)가 맡는다.
    resolve_output_image(filename).unlink()
    return {"ok": True}


def parse_image_names_body(data: dict) -> list[str]:
    names = data.get("names")
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise HTTPException(400, "이미지 파일명 목록(names)이 필요해요.")
    return names


def build_zip_from_paths(paths: list[Path]) -> Path:
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    tmp_path = Path(tmp.name)
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in paths:
            zf.write(f, arcname=f.name)
    return tmp_path


def build_output_zip() -> Path:
    files = find_image_files(OUTPUT_DIR)
    return build_zip_from_paths(files)


@app.get("/api/download-images")
async def download_images():
    # 압축은 시간이 걸릴 수 있으니 이벤트 루프를 막지 않게 스레드에서 처리하고,
    # 임시로 만든 zip 파일은 응답이 끝난 뒤 백그라운드에서 지운다.
    try:
        zip_path = await asyncio.to_thread(build_output_zip)
    except EmailSendError as e:
        raise HTTPException(404, str(e))

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"nightshift_output_{timestamp}.zip",
        background=BackgroundTask(lambda: zip_path.unlink(missing_ok=True)),
    )


@app.post("/api/output-images/download-selected")
async def download_selected_images(request: Request):
    # 갤러리에서 여러 장을 선택해 한 번에 받는 용도 — 잘못된 이름이나 그 사이 지워진
    # 파일은 조용히 건너뛰고, 하나라도 남아있으면 그것만으로 zip을 만든다.
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    names = parse_image_names_body(data)

    paths = []
    for name in names:
        try:
            paths.append(resolve_output_image(name))
        except HTTPException:
            continue
    if not paths:
        raise HTTPException(404, "선택한 이미지를 찾을 수 없어요.")

    zip_path = await asyncio.to_thread(build_zip_from_paths, paths)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"nightshift_selected_{timestamp}.zip",
        background=BackgroundTask(lambda: zip_path.unlink(missing_ok=True)),
    )


@app.delete("/api/output-images")
async def delete_images():
    # 되돌릴 수 없는 삭제라서, 확인 절차는 프론트엔드(버튼 클릭 시 confirm 창)가 맡는다.
    try:
        deleted = await asyncio.to_thread(delete_output_images, OUTPUT_DIR)
    except OutputFolderError as e:
        raise HTTPException(404, str(e))
    return {"deleted": deleted}


@app.post("/api/output-images/delete-selected")
async def delete_selected_images(request: Request):
    # 갤러리에서 여러 장을 선택해 한 번에 지우는 용도 — 되돌릴 수 없는 삭제라서, 확인
    # 절차는 프론트엔드(버튼 클릭 시 confirm 창)가 맡는다. 잘못된 이름이나 그 사이 이미
    # 지워진 파일은 조용히 건너뛴다.
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    names = parse_image_names_body(data)

    deleted = 0
    for name in names:
        try:
            path = resolve_output_image(name)
        except HTTPException:
            continue
        path.unlink()
        deleted += 1
    return {"deleted": deleted}


@app.post("/api/output-images/rotate-landscape")
async def rotate_images():
    try:
        result = await asyncio.to_thread(rotate_landscape_images, OUTPUT_DIR)
    except OutputFolderError as e:
        raise HTTPException(404, str(e))
    return result


app.mount("/", StaticFiles(directory=str(BASE_DIR / "static"), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
