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
import io
import json
import logging
import os
import queue
import subprocess
import tempfile
import threading
import time
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

from email_sender import EmailSendError, find_image_files, send_output_images
from output_images import (
    OUTPUT_DIR,
    OutputFolderError,
    delete_output_images,
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

# ComfyUI는 같은 파드 안에서 돌아가지만 설치 방식에 따라 포트가 다를 수 있어, 이 후보들을
# 순서대로 짧은 타임아웃으로 찔러보고 처음 응답하는 곳을 채택한다. COMFY_URL 환경변수가
# 명시적으로 설정돼 있으면 이 감지 과정을 건너뛰고 그 값을 그대로 쓴다.
COMFY_CANDIDATE_URLS = ["http://127.0.0.1:8188", "http://127.0.0.1:8000"]
COMFY_CHECK_TIMEOUT = 2

# 템플릿 스크립트가 자기 자신의 진행 상황(PUT /api/jobs/{job_id}/progress)을
# 보고할 때 사용하는, nightshift 자신의 주소. 서버가 항상 이 포트로 뜨므로 고정값.
SELF_URL = "http://127.0.0.1:8000"

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


def build_output_zip() -> Path:
    files = find_image_files(OUTPUT_DIR)
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    tmp_path = Path(tmp.name)
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, arcname=f.name)
    return tmp_path


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


@app.delete("/api/output-images")
async def delete_images():
    # 되돌릴 수 없는 삭제라서, 확인 절차는 프론트엔드(버튼 클릭 시 confirm 창)가 맡는다.
    try:
        deleted = await asyncio.to_thread(delete_output_images, OUTPUT_DIR)
    except OutputFolderError as e:
        raise HTTPException(404, str(e))
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
