"""
RunPod Job Queue — 등록된 스크립트 템플릿을 큐에 쌓아두면 워커가 순서대로 하나씩 실행한다.

실행:
    pip install -r requirements.txt
    python3 app.py
    (RunPod라면 8188 등 이미 쓰는 포트와 겹치지 않게 8000번을 열어둠)

접속:
    브라우저에서 http://<pod-ip>:8000  (RunPod는 포트 8000을 프록시로 노출해야 함)
"""

import asyncio
import csv
import io
import json
import os
import queue
import subprocess
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile

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


def coerce_option(option: dict, raw: str | None):
    if raw is None or raw == "":
        raw = option.get("default")
    if option.get("type") == "number":
        try:
            num = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(400, f"'{option['label']}' 값이 올바른 숫자가 아니에요.")
        return int(num) if num.is_integer() else num
    return "" if raw is None else str(raw)


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

    options = {}
    for option in template.get("options", []):
        raw = form.get(option["name"])
        options[option["name"]] = coerce_option(option, raw if isinstance(raw, str) else None)

    job_id = str(uuid.uuid4())[:8]

    workflow_dest_name = f"{job_id}_{workflow.filename}"
    (JOBS_DIR / workflow_dest_name).write_bytes(await workflow.read())

    csv_dest_name = None
    csv_original_name = None
    if requires_csv:
        csv_dest_name = f"{job_id}_{csv_file.filename}"
        (JOBS_DIR / csv_dest_name).write_bytes(await csv_file.read())
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
        ordered = sorted(jobs.values(), key=lambda j: j["queued_at"], reverse=True)
    pending_ids = list(job_queue.queue)
    return {"jobs": ordered, "pending_count": len(pending_ids)}


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
        if not job:
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
        if not job:
            raise HTTPException(404, "없는 작업이에요.")
        if job["status"] in ("running", "queued"):
            raise HTTPException(400, "실행 중이거나 이미 시작된 작업은 지울 수 없어요.")
        del jobs[job_id]
    save_state()
    return {"ok": True}


app.mount("/", StaticFiles(directory=str(BASE_DIR / "static"), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
