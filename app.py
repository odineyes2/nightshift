"""
RunPod Job Queue — 등록된 스크립트 템플릿(또는 커스텀 .py)을 큐에 쌓아두면 워커가 순서대로 하나씩 실행한다.

실행:
    pip install -r requirements.txt
    python3 app.py
    (RunPod라면 8188 등 이미 쓰는 포트와 겹치지 않게 8000번을 열어둠)

접속:
    브라우저에서 http://<pod-ip>:8000  (RunPod는 포트 8000을 프록시로 노출해야 함)
"""

import json
import os
import queue
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).parent
JOBS_DIR = BASE_DIR / "jobs"
LOGS_DIR = BASE_DIR / "logs"
TEMPLATES_DIR = BASE_DIR / "templates"
MANIFEST_PATH = TEMPLATES_DIR / "manifest.json"
STATE_FILE = BASE_DIR / "jobs_state.json"
JOBS_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

CUSTOM_TEMPLATE_ID = "__custom__"

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


def worker_loop():
    while True:
        job_id = job_queue.get()
        with lock:
            job = jobs[job_id]
            job["status"] = "running"
            job["started_at"] = now_iso()
        save_state()

        log_path = LOGS_DIR / f"{job_id}.log"
        script_dir = TEMPLATES_DIR if job.get("template_id") else JOBS_DIR
        script_path = script_dir / job["script_filename"]

        extra_env = {}
        if job.get("workflow_filename"):
            extra_env["WORKFLOW_PATH"] = str((JOBS_DIR / job["workflow_filename"]).resolve())
        if job.get("csv_filename"):
            extra_env["CSV_PATH"] = str((JOBS_DIR / job["csv_filename"]).resolve())
        env = {**os.environ, **extra_env} if extra_env else None

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


@app.post("/api/upload")
async def upload(
    template_id: str = Form(...),
    workflow: UploadFile = File(...),
    file: UploadFile | None = File(None),
    csv: UploadFile | None = File(None),
):
    if not workflow.filename or not workflow.filename.endswith(".json"):
        raise HTTPException(400, "워크플로우는 json 파일만 업로드할 수 있어요.")

    job_id = str(uuid.uuid4())[:8]
    is_custom = template_id == CUSTOM_TEMPLATE_ID

    if is_custom:
        if file is None or not file.filename or not file.filename.endswith(".py"):
            raise HTTPException(400, "커스텀 스크립트는 python(.py) 파일이 필요해요.")
        script_filename = f"{job_id}_{file.filename}"
        (JOBS_DIR / script_filename).write_bytes(await file.read())
        template_label = None
        requires_csv = False
    else:
        template = load_templates_map().get(template_id)
        if template is None:
            raise HTTPException(400, "존재하지 않는 템플릿이에요.")
        script_filename = template["script_filename"]
        template_label = template["label"]
        requires_csv = bool(template.get("requires_csv"))
        if requires_csv and (csv is None or not csv.filename or not csv.filename.endswith(".csv")):
            raise HTTPException(400, "이 템플릿은 csv 파일이 필요해요.")

    workflow_dest_name = f"{job_id}_{workflow.filename}"
    (JOBS_DIR / workflow_dest_name).write_bytes(await workflow.read())

    csv_dest_name = None
    csv_original_name = None
    if requires_csv and csv is not None and csv.filename:
        csv_dest_name = f"{job_id}_{csv.filename}"
        (JOBS_DIR / csv_dest_name).write_bytes(await csv.read())
        csv_original_name = csv.filename

    with lock:
        jobs[job_id] = {
            "id": job_id,
            "template_id": None if is_custom else template_id,
            "template_label": template_label,
            "script_filename": script_filename,
            "script_display_name": file.filename if is_custom else script_filename,
            "workflow_filename": workflow_dest_name,
            "workflow_original_name": workflow.filename,
            "csv_filename": csv_dest_name,
            "csv_original_name": csv_original_name,
            "status": "queued",
            "queued_at": now_iso(),
            "started_at": None,
            "finished_at": None,
            "returncode": None,
        }
    save_state()
    job_queue.put(job_id)
    return jobs[job_id]


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


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    with lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "없는 작업이에요.")
        if job["status"] == "running":
            raise HTTPException(400, "실행 중인 작업은 지울 수 없어요.")
        del jobs[job_id]
    save_state()
    return {"ok": True}


app.mount("/", StaticFiles(directory=str(BASE_DIR / "static"), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
