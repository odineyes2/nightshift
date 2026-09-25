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
import hmac
import io
import json
import logging
import os
import posixpath
import queue
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile
from PIL import Image, ImageOps

import auth
import comfy_outputs
from comfy_outputs import OutputSyncError, forget_downloaded, sync_outputs, sync_state_summary, synced_pod_ids
from data_paths import data_dir, data_path
from drivers import DriverError, driver_for, driver_kinds
from drivers.comfyui import (
    CANDIDATE_URLS,
    CHECK_TIMEOUT,
    CHECK_TIMEOUT_INTERACTIVE,
    CHECK_TIMEOUT_LOCAL,
    COMFY_USER_AGENT,
    ComfyUIDriver,
    check_url,
)
import asset_meta
import assets_index
import share_sessions
import video_edit
import board_store
import db
import git_log
import model_download
import model_registry
import pod_registry
import projects as project_store
import runpod_api
import runpod_sessions
import runpod_sync
from email_sender import EmailSendError, find_image_files, send_output_images
from workflow_builder import WorkflowBuildError, build_workflow
import workflow_builder_krea2
import workflow_builder_minimax_h3
from output_images import (
    IMAGE_EXTENSIONS,
    OUTPUT_DIR,
    OutputFolderError,
    delete_output_images,
    list_output_images,
    rotate_image_file,
    rotate_landscape_images,
)
from output_videos import (
    VIDEO_EXTENSIONS,
    delete_output_videos,
    find_video_files,
    list_output_videos,
)
from ref_assets import (
    job_env_for_owner,
    DEFAULT_CHAR_NO,
    REF_KINDS,
    RefAssetError,
    RefReferenceError,
    list_assets_tree,
    list_char_nos,
    parse_char_no,
    resolve_ref,
    save_ref_image,
    validate_new_char_no,
    validate_new_folder_name,
    validate_ref_set,
)
from input_assets import (
    InputAssetError,
    delete_input_audio,
    delete_input_image,
    delete_input_video,
    list_input_audios,
    list_input_images,
    list_input_videos,
    resolve_input_audio,
    resolve_input_image,
    resolve_input_video,
    save_input_audio,
    save_input_image,
    save_input_video,
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

# 이 파일이 사는 server/ 아래에는 이 서버가 "가지고 도는" 파이썬 모듈과, 서버
# 자신이 쓰는 리소스(prompt_enhancer.json)가 있다. templates/(작업 스크립트)와
# static/(프론트엔드)는 서버 코드가 아니라 저장소 루트에 나란히 있는 별개
# 산출물이라 REPO_ROOT로 따로 가리킨다 — templates/*.py는 서버가 import하는
# 모듈이 아니라 서브프로세스로 실행되는 독립 프로그램이고, 원격 pod에서 돌 수도
# 있다. 돌면서 생기는 것(작업 기록·로그·최근 파일·설정)은 전부 data/ 아래다 —
# 무엇이 코드고 무엇이 생성물인지 경로만 봐도 갈리게 하려는 것이다
# (data_paths.py 참고).
BASE_DIR = Path(__file__).parent
REPO_ROOT = BASE_DIR.parent
JOBS_DIR = data_dir("jobs")
LOGS_DIR = data_dir("logs")
TEMPLATES_DIR = REPO_ROOT / "templates"
MANIFEST_PATH = TEMPLATES_DIR / "manifest.json"
# 영상 생성(WAN2.2 i2v/flf2v) 템플릿이 쓰는 고정 워크플로우 — family별 프리셋과
# 달리 관리자가 올려둔 게 아니라 nightshift 자체가 기능으로 함께 배포하는
# 워크플로우라 소스 트리(templates/) 안에, 버전 관리 대상으로 둔다. 프론트엔드가
# 해당 템플릿을 고르면 GET /api/video-workflows/{name}으로 받아서 워크플로우
# 업로드 칸에 자동으로 채운다(사용자가 파일을 고를 필요가 없음).
VIDEO_WORKFLOWS_DIR = TEMPLATES_DIR / "video_workflows"
VIDEO_WORKFLOW_NAMES = {"wan22_i2v", "wan22_flf2v"}
STATE_FILE = data_path("jobs_state.json")
STATE_FILE = data_path("jobs_state.json")
# "새 작업 추가" 마법사의 ControlNet 계열 워크플로우 유형(openpose_cn/depth_cn/
# lineart_cn)이 쓰는, family(베이스 모델)별로 미리 만들어 올려둔 워크플로우 JSON
# 저장소. 이 세 유형은 체크포인트마다 ControlNet 로더/가중치 배선이 달라 워크플로우
# 빌더가 안전하게 자동 조립할 수 없어서(잘못 배선하면 조용히 ControlNet 없이
# 돌아가는 사고가 남), 관리자가 한 번 만들어둔 워크플로우를 family+유형 조합별로
# 저장해뒀다가 그대로 재사용한다. 파일명 규칙은 preset_filename() 참고.
WORKFLOW_PRESETS_DIR = data_dir("workflow_presets")

class RecentFileStore:
    """업로드된 파일 사본을 "최근 N개, 내용 중복 제거" 정책으로 관리한다. 워크플로우
    (.json)/CSV 슬롯 옆 "최근 ○○" 버튼이 이 클래스의 인스턴스 하나씩을 쓴다. 작업
    (jobs/)과는 독립적인 저장소라, 작업이 삭제되거나 DELETED_JOBS_RETENTION을 넘겨
    완전히 정리돼도 여기 사본은 남아있어서 "최근에 올렸던 파일" 자체를 다시 고를 수
    있다."""

    def __init__(self, dir_path: Path, state_path: Path, retention: int):
        self.dir_path = dir_path
        self.state_path = state_path
        self.retention = retention
        self.items: list[dict] = []  # 최신순. [{id, filename, stored_filename, uploaded_at, content_hash}, ...]
        self.dir_path.mkdir(exist_ok=True)

    def load(self):
        if self.state_path.exists():
            with open(self.state_path) as f:
                self.items.extend(json.load(f))

    def save(self):
        with open(self.state_path, "w") as f:
            json.dump(self.items, f, indent=2, default=str)

    def record(self, filename: str, content: bytes):
        # 어떤 템플릿이든 업로드를 성공적으로 받을 때마다 호출한다. 같은 내용(해시)의
        # 파일이 이미 목록에 있으면 새로 저장하지 않고 맨 앞으로 올리기만 한다 — 안
        # 그러면 같은 파일을 반복해서 재사용할 때마다 사본이 계속 쌓여서 목록이
        # 중복으로 도배된다.
        content_hash = hashlib.sha256(content).hexdigest()
        with lock:
            existing = next((it for it in self.items if it["content_hash"] == content_hash), None)
            if existing:
                self.items.remove(existing)
                existing["uploaded_at"] = now_iso()
                existing["filename"] = filename
                self.items.insert(0, existing)
            else:
                entry_id = str(uuid.uuid4())[:8]
                stored_filename = f"{entry_id}_{filename}"
                (self.dir_path / stored_filename).write_bytes(content)
                self.items.insert(0, {
                    "id": entry_id,
                    "filename": filename,
                    "stored_filename": stored_filename,
                    "uploaded_at": now_iso(),
                    "content_hash": content_hash,
                })
            while len(self.items) > self.retention:
                old = self.items.pop()
                (self.dir_path / old["stored_filename"]).unlink(missing_ok=True)
            self.save()

    def list_meta(self) -> list[dict]:
        with lock:
            return [{"id": it["id"], "filename": it["filename"], "uploaded_at": it["uploaded_at"]} for it in self.items]

    def delete(self, item_id: str) -> bool:
        # "최근 워크플로우"/"최근 CSV" 모달의 휴지통 버튼이 호출한다. 목록에서 항목을
        # 지우고 사본 파일도 같이 지운다. 존재하지 않으면(이미 지워졌거나 애초에
        # 없는 id) False를 돌려주고 아무것도 하지 않는다.
        with lock:
            entry = next((it for it in self.items if it["id"] == item_id), None)
            if entry is None:
                return False
            self.items.remove(entry)
            (self.dir_path / entry["stored_filename"]).unlink(missing_ok=True)
            self.save()
            return True

    def get(self, item_id: str) -> dict | None:
        with lock:
            return next((it for it in self.items if it["id"] == item_id), None)


recent_workflows_store = RecentFileStore(
    data_dir("recent_workflows"),
    data_path("recent_workflows_state.json"),
    int(os.environ.get("NIGHTSHIFT_RECENT_WORKFLOWS_RETENTION", "30")),
)
recent_csvs_store = RecentFileStore(
    data_dir("recent_csvs"),
    data_path("recent_csvs_state.json"),
    int(os.environ.get("NIGHTSHIFT_RECENT_CSVS_RETENTION", "30")),
)

# "Danbooru 프롬프트 조립" 탭의 영구 저장 대상 두 가지 — 태그 풀 편집(카테고리별
# 추가/삭제)과 조합 기록. 규칙 엔진·랜덤 조합·프롬프트 조립 자체는 클릭마다 즉시
# 반응해야 해서 static/index.html에 선언적 데이터+로직으로 들어있고, 여기서는
# "사용자가 편집/저장한 상태"만 그대로 보관했다 내려준다 (형태를 이해할 필요가 없음).
DANBOORU_TAG_EDITS_FILE = data_path("danbooru_tag_edits.json")
DANBOORU_HISTORY_FILE = data_path("danbooru_history.json")
DANBOORU_HISTORY_LIMIT = 40

danbooru_tag_edits: dict = {}   # {categoryKey: {"added": [...], "removed": [...]}}
danbooru_history: list = []     # 최신순, 최대 DANBOORU_HISTORY_LIMIT개


def load_danbooru_state():
    if DANBOORU_TAG_EDITS_FILE.exists():
        with open(DANBOORU_TAG_EDITS_FILE) as f:
            danbooru_tag_edits.update(json.load(f))
    if DANBOORU_HISTORY_FILE.exists():
        with open(DANBOORU_HISTORY_FILE) as f:
            danbooru_history.extend(json.load(f))


def save_danbooru_tag_edits():
    with lock:
        with open(DANBOORU_TAG_EDITS_FILE, "w") as f:
            json.dump(danbooru_tag_edits, f, indent=2, ensure_ascii=False)


def save_danbooru_history():
    with lock:
        with open(DANBOORU_HISTORY_FILE, "w") as f:
            json.dump(danbooru_history, f, indent=2, ensure_ascii=False)


# LoRA 파일 이름 -> {trigger, base_id} 매핑. 예전에는 lora_triggers.json이었지만 이제
# 모델 등록부(model_registry.py, models 테이블)가 원본이고, 여기는 마법사/워크플로우 탭이
# 읽던 모양 그대로 돌려주는 뷰일 뿐이다. 옛 JSON은 시작할 때 한 번만 등록부로 옮기고
# .migrated로 남긴다(load_lora_triggers).
LORA_TRIGGERS_FILE = data_path("lora_triggers.json")


def load_lora_triggers():
    model_registry.import_legacy_lora_triggers(LORA_TRIGGERS_FILE)


# nightshift가 작업을 보낼 워커는 이제 "파드"로 관리한다(pod_registry.py). 파드마다
# 종류(kind)가 있고, 그 종류를 어떻게 다루는지는 드라이버가 안다(drivers/). ComfyUI
# 접속 주소를 찾는 우선순위(파드에 저장한 주소 > COMFY_URL 환경변수 > 127.0.0.1 자동
# 탐지)와 연결 확인 타임아웃도 전부 comfyui 드라이버로 옮겼다 — 아래는 이 파일 곳곳에서
# 쓰던 이름을 그대로 유지하기 위한 별칭이다(값의 출처만 바뀌었다).
#
# 파드가 여러 대가 되기 전(P1)까지, "어느 파드인지 지정되지 않은 일"은 전부 기본
# 파드(pod_registry.default_pod)를 쓴다. 파드가 하나뿐이면 예전과 동작이 같다.
COMFY_CANDIDATE_URLS = CANDIDATE_URLS
COMFY_CHECK_TIMEOUT_LOCAL = CHECK_TIMEOUT_LOCAL
COMFY_CHECK_TIMEOUT = CHECK_TIMEOUT
COMFY_CHECK_TIMEOUT_INTERACTIVE = CHECK_TIMEOUT_INTERACTIVE

# 헤더의 연결 상태 배지는 5초마다 /api/comfy-status를 폴링한다. 그 요청이 매번
# 원격 pod까지 왕복하면 (a) 탭 수만큼 pod를 찌르게 되고 (b) 응답이 느린 날에는
# 요청 자체가 몇 초씩 걸린다. 그래서 상태는 캐시해 두고 즉시 돌려주되, 오래된
# 값이면 백그라운드로 다시 확인한다(stale-while-revalidate).
COMFY_STATUS_TTL_SEC = 4
# 한 번 실패했다고 바로 "연결 안 됨"으로 뒤집지 않는다 — WAN에서는 패킷 하나만
# 흘려도 실패로 보이는데, 그때마다 배지가 빨갛게 깜빡이면 신뢰할 수 없게 된다.
# 연속으로 이만큼 실패해야 끊긴 것으로 본다(연결됨으로 돌아가는 건 즉시).
COMFY_STATUS_FAIL_STREAK = 2

# ComfyUI가 안 떠 있을 때, 큐에서 꺼낸 작업을 실패시키지 않고 붙잡아 두는 재확인
# 간격(초). nightshift를 홈서버에 상시 띄워두고 ComfyUI만 원격 GPU pod에서 돌리는
# 구성에서는 "pod가 아직 안 떠 있는" 시간이 비정상이 아니라 기본 상태다 — 그때
# 밤사이 쌓아둔 작업이 몇 초 만에 전부 failed로 떨어지면 큐를 쌓아두는 의미가
# 없어지므로, 연결될 때까지 이 간격으로 다시 확인하며 기다린다(waiting_for_comfy).
COMFY_WAIT_RETRY_SEC = float(os.environ.get("NIGHTSHIFT_COMFY_WAIT_RETRY_SEC", "15"))
# 기다리는 동안에도 "⏸ 정지"에는 빨리 반응해야 하니 위 간격을 이 단위로 쪼개 잔다.
COMFY_WAIT_TICK_SEC = 1.0

# 템플릿 스크립트가 자기 자신의 진행 상황(PUT /api/jobs/{job_id}/progress)을
# 보고할 때 쓰는, nightshift 자신의 주소. 예전에는 8000으로 박아뒀는데, 홈서버로
# 옮기면서 다른 포트로 띄우면 진행률 보고가 통째로 조용히 끊긴다(템플릿은 실패해도
# 경고만 찍고 계속 돈다). 그래서 실제로 뜨는 포트(NIGHTSHIFT_PORT, ecosystem.config.js가
# 같은 값으로 --port를 넘긴다)에서 유도하고, 그것도 안 맞는 특수한 배치(리버스 프록시
# 뒤 등)를 위해 NIGHTSHIFT_SELF_URL로 통째로 덮어쓸 수 있게 둔다.
SELF_PORT = os.environ.get("NIGHTSHIFT_PORT", "8000").strip() or "8000"
SELF_URL = (os.environ.get("NIGHTSHIFT_SELF_URL", "").strip().rstrip("/")
            or f"http://127.0.0.1:{SELF_PORT}")

# "새 작업 추가"의 메인 프롬프트 필드에 있는 "Prompt Enhance" 버튼이 쓰는, 프롬프트를
# 다듬어주는 전용 ComfyUI 워크플로우. 배치 템플릿과 달리 사용자가 매번 업로드하는 게
# 아니라 저장소에 고정으로 들어있는 자산이다 — user_prompt라는 제목의 노드에 원문을
# 넣고 실행한 뒤, 미리보기(PreviewAny) 노드의 출력을 개선된 프롬프트로 읽어온다.
# isDanbooru_sys? 라는 제목의 ComfySwitchNode가 "자연어로 다듬기(7번 노드)" /
# "그 결과를 다시 Danbooru 태그로 변환(13번 노드)" 두 경로를 고르므로, 요청받은
# 모드(자연어/Danbooru)에 맞춰 이 스위치 노드의 switch 입력을 켜고 끈다.
ENHANCER_WORKFLOW_PATH = BASE_DIR / "prompt_enhancer.json"  # server/ 자신의 리소스
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

# 워커는 파드마다 max_concurrent개씩만 돌므로, 대기/실행 중인 작업이 한없이
# 쌓이는 걸 막을 안전장치가 없으면 (예: 반복 호출하는 스크립트나 LLM의 버그로)
# 큐가 통제 불능으로 불어날 수 있다 — 각 작업이 실제 GPU 시간을 쓰므로 위험이
# 크다. pending/queued/running 합계가 이 값 이상이면 새 작업 추가를 거부한다.
# 비밀값이 아니라서 기본값을 코드에 그대로 박아두되, .env에
# NIGHTSHIFT_MAX_ACTIVE_JOBS가 있으면 그 값이 우선한다.
MAX_ACTIVE_JOBS = int(os.environ.get("NIGHTSHIFT_MAX_ACTIVE_JOBS", "100"))

jobs: dict[str, dict] = {}
lock = threading.Lock()

# ---- 파드별 실행 상태 ---------------------------------------------------------
# 예전에는 큐도 자동 실행 플래그도 "지금 돌고 있는 서브프로세스"도 전부 모듈 전역이었다
# — 워커가 하나뿐이었기 때문이다. 이제는 파드마다 하나씩 갖는다. 작업에는 pod_id가
# 붙고, 그 파드의 큐에만 들어간다(파드별 독립 큐 — multipod_plan.md 참고).
class PodRuntime:
    """파드 하나의 실행 상태. 필드는 전부 전역 lock 아래에서 읽고 쓴다."""

    def __init__(self, pod_id: str, max_concurrent: int = 1):
        self.pod_id = pod_id
        self.queue: "queue.Queue[str]" = queue.Queue()
        # 자동 실행 모드 — 켜져 있는 동안에는 이 파드로 새로 추가되는 작업이 pending에
        # 머무르지 않고 바로 큐에 들어간다("▶ 시작"/"⏸ 정지"). 서버가 재시작되면 큐에
        # 남아있던 작업이 interrupted로 표시되는 것과 같은 이유로 초기화된다(재시작 후
        # 저절로 다시 돌기 시작하면 안 되므로) — 그래서 디스크에 저장하지 않는다.
        self.auto_run = False
        # 지금 이 파드에서 돌고 있는 작업 {job_id: 서브프로세스}. "⏸ 정지"가 이걸
        # 종료시킨다. max_concurrent가 1이면 최대 한 개다.
        self.running: dict[str, subprocess.Popen] = {}
        self.max_concurrent = max(1, int(max_concurrent or 1))
        self.workers = 0        # 실제로 띄운 워커 스레드 수
        self.closed = False     # 파드가 지워지면 True — 스레드가 스스로 끝난다


pod_runtimes: dict[str, PodRuntime] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_state():
    # 큐 자체는 프로세스가 죽으면 사라지지만, 이력 조회는 재시작 후에도 가능하게 기록만 남긴다.
    # 작업 기록은 SQLite(db.py)에 산다 — 메모리 jobs dict를 통째로 맞추되 바뀐 것만 쓴다.
    # DB가 잠깐 안 써져도 워커 스레드가 죽으면 안 되므로 로그만 남기고 넘어간다(바뀐 것은
    # 저장에 성공할 때까지 계속 "바뀐 것"으로 남아서 다음 save_state()가 다시 쓴다).
    with lock:
        try:
            db.save_jobs(jobs)
        except Exception:
            logging.exception("작업 기록을 DB에 저장하지 못했어요")


def load_state():
    # 예전 jobs_state.json이 있으면 한 번만 DB로 가져오고(원본은 .migrated로 보존).
    db.init()
    db.import_legacy_jobs(STATE_FILE)
    jobs.update(db.load_jobs())


def prune_deleted_jobs():
    # 소프트 삭제된 작업 중 DELETED_JOBS_RETENTION개를 넘는 오래된 것들은 이제
    # 완전히 정리한다(레코드 + 워크플로우/CSV 파일). lock을 쥔 채로 호출해야 한다.
    deleted = sorted(
        (job for job in jobs.values() if job.get("deleted")),
        key=lambda job: job.get("deleted_at") or "",
        reverse=True,
    )
    for job in deleted[DELETED_JOBS_RETENTION:]:
        for field in ("workflow_filename", "video_workflow_filename", "csv_filename"):
            filename = job.get(field)
            if filename:
                (JOBS_DIR / filename).unlink(missing_ok=True)
        del jobs[job["id"]]


def load_templates_list() -> list[dict]:
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_templates_map() -> dict[str, dict]:
    return {t["id"]: t for t in load_templates_list()}


# 아래 세 함수는 "기본 파드"에 대한 얇은 껍데기다. 실제 로직은 전부 드라이버에 있고,
# 이 이름들을 남겨 두는 이유는 이 파일의 열두 곳이 이미 이 이름으로 부르고 있기 때문이다
# (파드를 골라 쓰는 것은 파드별 큐를 만드는 P1에서 한꺼번에 정리한다).
def check_comfy_url(url: str, timeout: float = COMFY_CHECK_TIMEOUT) -> bool:
    return check_url(url, timeout)


def configured_comfy_url() -> tuple[str | None, str]:
    """명시적으로 지정된 ComfyUI 주소와 그 출처("setting"/"env"). 어느 쪽에도
    지정돼 있지 않으면 (None, "auto") — 이때만 자동 탐지로 넘어간다."""
    return ComfyUIDriver.configured(pod_registry.default_pod())


def resolve_comfy_url() -> tuple[str | None, bool]:
    """(지금 쓸 주소, 연결 가능 여부) — 요청한 회원의 기본 파드 기준(로그인 없는 내부 호출은 서버 기본 파드)."""
    user = auth.current_user.get()
    pod = default_pod_for_user(user) if user else pod_registry.default_pod()
    if pod is None:
        return None, False
    health = driver_for(pod).health(pod)
    return health["url"], health["ok"]


# ---- ComfyUI 노드/모델 목록 조회 ----------------------------------------------
# ComfyUI의 /object_info는 그 서버에 설치된 모든 노드 타입과, 파일을 고르는 입력
# (체크포인트/LoRA/VAE 등)이 실제로 고를 수 있는 선택지까지 통째로 돌려준다.
# 워크플로우 JSON은 결국 "노드 이름 + 입력값"일 뿐이라, 이 목록만 있으면 업로드된
# 워크플로우가 이 서버에서 돌아갈 수 있는지 미리 검사할 수 있다.
# 커스텀 노드가 많이 깔린 서버에서는 응답이 수 MB까지 커지고 모델 폴더를 훑느라
# 느리기도 해서 짧게 캐싱한다 — 모델을 새로 설치하는 일은 드물고, 필요하면
# refresh=true로 강제로 다시 받아온다.
# ComfyUI에 "설치된 모델 목록" 전용 API는 없어서, 각 종류를 대표하는 로더 노드의
# 입력 선택지를 그대로 읽어 쓴다(그 노드가 없는 서버면 빈 목록).
MODEL_LIST_SOURCES = {
    "checkpoints": ("CheckpointLoaderSimple", "ckpt_name"),
    "diffusion_models": ("UNETLoader", "unet_name"),
    "loras": ("LoraLoader", "lora_name"),
    "vae": ("VAELoader", "vae_name"),
    "text_encoders": ("CLIPLoader", "clip_name"),
    "controlnet": ("ControlNetLoader", "control_net_name"),
    "upscale_models": ("UpscaleModelLoader", "model_name"),
    "clip_vision": ("CLIPVisionLoader", "clip_name"),
}


def object_info_pod(pod_id: str | None) -> dict:
    """object_info를 물어볼 파드를 고른다.

    화면이 파드 안으로 들어간 뒤로("#pod/{id}/builder", "#pod/{id}/lora") "이 파드에
    무엇이 설치돼 있나"를 묻는 것이 정상이라, 부르는 쪽이 파드를 지정한다. 지정이
    없거나 ComfyUI 파드가 아니면 기본 파드로 떨어진다 — 셸 파드처럼 노드 목록이라는
    개념 자체가 없는 워커에 물어봐야 의미가 없기 때문이다."""
    if pod_id == "auto":   # 파드를 정하지 않은 작업 구상 — 어느 파드의 목록도 아니다
        return NO_POD
    user = auth.current_user.get()
    if user is None:   # 로그인 없이 부르는 내부 경로(서버 시작 등)
        if pod_id:
            pod = pod_registry.get_pod(pod_id)
            if pod is not None and pod.get("kind") == pod_registry.DEFAULT_KIND:
                return pod
        return pod_registry.default_pod()
    if pod_id:
        pod = pod_registry.get_pod(pod_id)
        if pod is not None and auth.can_access(user, pod.get("owner_id")) and pod.get("kind") == pod_registry.DEFAULT_KIND:
            return pod
    return default_pod_for_user(user) or NO_POD


def fetch_comfy_object_info(force: bool = False, pod: dict | None = None) -> tuple[str | None, dict | None]:
    """(주소, object_info) — 그 파드가 안 떠 있으면 (주소, None). 캐싱은 드라이버가
    파드별로 한다(파드마다 설치된 노드/모델이 다를 수 있으므로).
    pod를 주지 않으면 기본 파드를 본다."""
    if pod is not None and pod.get("_none"):
        return None, None   # 쓸 파드가 없다 — 서버 자신의 ComfyUI로 떨어지지 않게 한다
    pod = pod or pod_registry.default_pod()
    return driver_for(pod).capabilities(pod, force)


def combo_choices(object_info: dict, class_type: str, field: str) -> list[str]:
    """그 노드의 그 입력이 고를 수 있는 선택지 목록. ComfyUI는 목록에서 고르는
    입력(COMBO)을 [["선택지1", "선택지2", ...], {옵션들}] 형태로 주고, 그냥 문자열/
    숫자 입력은 ["STRING", {...}]처럼 타입 이름을 준다 — 첫 원소가 리스트인지로
    둘을 구분한다. 목록형이 아니면 빈 리스트."""
    spec = (object_info.get(class_type) or {}).get("input", {})
    for section in ("required", "optional"):
        entry = (spec.get(section) or {}).get(field)
        if isinstance(entry, list) and entry and isinstance(entry[0], list):
            return [str(v) for v in entry[0]]
    return []


def workflow_missing(object_info: dict, workflow: dict) -> tuple[list[str], list[dict], int]:
    """워크플로우가 이 ComfyUI(object_info)에서 돌 수 있는지 — (없는 노드, 없는 모델/설정값, 검사한 노드 수).
    링크나 숫자 입력은 검사 대상이 아니고, 목록에서 고르는 입력(체크포인트/LoRA/샘플러 이름 등)만
    실제 선택지와 대조한다. 노드 자체가 없으면 그 노드의 입력값은 검사할 기준이 없어 건너뛴다."""
    missing_nodes: list[str] = []
    missing_values: list[dict] = []
    checked = 0
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        if not isinstance(class_type, str) or not class_type:
            continue
        checked += 1
        if class_type not in object_info:
            if class_type not in missing_nodes:
                missing_nodes.append(class_type)
            continue
        for field, value in (node.get("inputs") or {}).items():
            if not isinstance(value, str):
                continue
            choices = combo_choices(object_info, class_type, field)
            if choices and value not in choices:
                missing_values.append({
                    "node_id": str(node_id),
                    "class_type": class_type,
                    "field": field,
                    "value": value,
                })
    return missing_nodes, missing_values, checked


def annotate_available_on(missing_values: list[dict], user: dict, exclude_pod_id: str | None) -> None:
    """없는 모델마다 "내 다른 파드 중 어디에 있나"를 available_on으로 붙인다(제자리 수정).
    파드가 꺼져 있거나 목록을 못 받으면 그 파드는 조용히 뺀다 — 어디까지나 안내다."""
    if not missing_values:
        return
    candidates = [p for p in visible_pods_for(user)
                  if p.get("kind") == pod_registry.DEFAULT_KIND and p.get("enabled") and p["id"] != exclude_pod_id]

    def load(pod):
        try:
            _, info = fetch_comfy_object_info(False, pod)
            return pod, info
        except Exception:
            return pod, None

    infos = []
    if candidates:
        with ThreadPoolExecutor(max_workers=min(6, len(candidates))) as ex:
            infos = [(pod, info) for pod, info in ex.map(load, candidates) if info]
    reverse_kinds = {src: kind for kind, src in MODEL_LIST_SOURCES.items()}
    for item in missing_values:
        # 등록부(파드와 무관한 기준 데이터)에 받을 주소가 적혀 있으면 함께 알려 준다.
        kind = reverse_kinds.get((item["class_type"], item["field"]))
        entry = model_registry.get_entry(kind, item["value"]) if kind else None
        if entry and (entry["download_url"] or entry["page_url"]):
            item["kind"] = kind
            item["registry"] = {"download_url": entry["download_url"], "page_url": entry["page_url"]}
        item["available_on"] = [
            {"id": pod["id"], "name": pod.get("name") or pod["id"]}
            for pod, info in infos
            if item["value"] in combo_choices(info, item["class_type"], item["field"])
        ]


def default_video_workflow_bytes(template: dict) -> bytes | None:
    """영상 워크플로우를 안 올렸을 때 템플릿 스크립트가 쓰는 내장 기본값(있으면)."""
    if not template.get("optional_video_workflow"):
        return None
    name = "wan22_flf2v" if "flf2v" in str(template.get("script_filename") or "") else "wan22_i2v"
    path = VIDEO_WORKFLOWS_DIR / f"{name}.json"
    try:
        return path.read_bytes()
    except OSError:
        return None


def compute_preflight(pod: dict, user: dict, blobs: list[tuple[str, bytes | None]]) -> dict | None:
    """작업이 실제로 갈 파드에 필요한 노드/모델이 있는지 미리 본다. 파드가 꺼져 있어 확인을 못 하면 None
    (이 앱은 ComfyUI가 꺼진 동안 큐에 쌓아두는 게 정상이라 막지 않고, 실행 직전 워커가 다시 확인한다).
    결과는 안내용이라 작업 등록을 막지는 않는다."""
    _, object_info = fetch_comfy_object_info(False, pod)
    if object_info is None:
        return None
    missing_nodes: list[str] = []
    missing_values: list[dict] = []
    for label, data in blobs:
        if not data:
            continue
        try:
            workflow = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(workflow, dict):
            continue
        nodes, values, _ = workflow_missing(object_info, workflow)
        for n in nodes:
            if n not in missing_nodes:
                missing_nodes.append(n)
        for v in values:
            v["workflow"] = label
            missing_values.append(v)
    annotate_available_on(missing_values, user, pod.get("id"))
    return {
        "pod_id": pod.get("id"),
        "checked_at": now_iso(),
        "missing_nodes": missing_nodes,
        "missing_values": missing_values,
    }


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
        # User-Agent가 필요한 이유는 drivers/comfyui.py의 COMFY_USER_AGENT 주석 참고
        # — Cloudflare가 앞단에 있는 RunPod pod는 기본 urllib UA를 403으로 막는다.
        headers={"Content-Type": "application/json", "User-Agent": COMFY_USER_AGENT},
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
            hist_req = urllib.request.Request(
                f"{comfy_url}/history/{prompt_id}", headers={"User-Agent": COMFY_USER_AGENT})
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


def stop_pod_jobs(pod_id: str) -> list[str]:
    """그 파드에서 지금 돌고 있는 서브프로세스들에 종료를 요청한다("⏸ 정지").
    종료를 요청한 job_id 목록을 반환한다. SIGTERM을 무시하고 계속 살아있는 경우를
    대비해, 잠시 후에도 안 죽어있으면 강제 종료(kill)하는 감시 스레드를 띄운다."""
    with lock:
        rt = pod_runtimes.get(pod_id)
        if rt is None:
            return []
        targets = list(rt.running.items())
        for job_id, _proc in targets:
            job = jobs.get(job_id)
            if job is not None:
                job["_stop_requested"] = True

    for _job_id, proc in targets:
        proc.terminate()

        def _kill_if_still_alive(p=proc):
            time.sleep(5)
            if p.poll() is None:
                p.kill()

        threading.Thread(target=_kill_if_still_alive, daemon=True).start()
    return [job_id for job_id, _ in targets]


def stop_all_jobs() -> list[str]:
    with lock:
        pod_ids = list(pod_runtimes)
    stopped = []
    for pod_id in pod_ids:
        stopped.extend(stop_pod_jobs(pod_id))
    return stopped


def set_comfy_wait_flag(job: dict, waiting: bool) -> bool:
    """job의 "ComfyUI 연결 대기 중" 표시를 갱신하고, 값이 실제로 바뀌었는지 돌려준다
    (바뀔 때만 save_state()를 부르면 되므로 — 15초마다 상태 파일을 새로 쓸 이유가 없다).
    lock을 쥔 채 호출해야 한다."""
    if waiting:
        if job.get("waiting_for_comfy"):
            return False
        job["waiting_for_comfy"] = True
        job["waiting_since"] = now_iso()
        return True
    if not job.get("waiting_for_comfy"):
        return False
    job.pop("waiting_for_comfy", None)
    job.pop("waiting_since", None)
    return True


def wait_for_pod(pod_id: str, job_id: str) -> tuple[str, str | None]:
    """파드에 연결될 때까지 붙잡고 있다가, 연결되면 ("run", 주소)를 돌려준다.

    예전에는 큐에서 꺼낸 작업을 곧바로 실행 상태로 바꾼 뒤 연결을 확인하고, 안 되면
    그 자리에서 failed 처리했다. 워커가 같은 머신에서 항상 같이 떠 있던 시절에는
    그게 맞았지만, GPU pod가 따로 있는 구성에서는 pod가 잠깐 꺼져 있는 동안 대기 중인
    작업이 순식간에 전멸한다 — 큐가 한 번에 한 개씩 돌기 때문에 수십 개가 몇 초 만에
    차례로 실패한다. 그래서 이제는 실패시키지 않고 "queued"인 채로 기다린다.

    기다리기를 그만둬야 하는 경우:
      ("skip", None)   그 사이 작업이 삭제됐다 → 그냥 건너뛴다.
      ("moved", None)  그 사이 작업이 다른 파드로 옮겨졌다 → 그 파드 큐로 넘긴다.
      ("pending", None) "⏸ 정지"로 이 파드의 auto_run이 꺼졌다 → 작업을 "pending"으로
                       되돌린다. 다음 "▶ 시작" 때 이어서 돌고, 무한정 기다리는 상태에서
                       빠져나오는 탈출구이기도 하다(대기 중인 작업은 status가 "queued"라
                       그대로는 삭제할 수 없다).
    """
    waited = False
    while True:
        pod = pod_registry.get_pod(pod_id)
        if pod is None:
            return "moved", None      # 파드가 지워졌다 — 다시 배차한다
        health = driver_for(pod).health(pod)
        url, connected = health["url"], health["ok"]

        with lock:
            job = jobs.get(job_id)
            gone = job is None or job.get("deleted")
            moved = not gone and job.get("pod_id") != pod_id
            if job is not None and (connected or gone or moved):
                set_comfy_wait_flag(job, False)
        if gone:
            if waited:
                save_state()
            return "skip", None
        if moved:
            if waited:
                save_state()
            return "moved", None
        if connected:
            if waited:
                save_state()
            return "run", url

        with lock:
            rt = pod_runtimes.get(pod_id)
            if job.get("auto_assigned") and not (rt and rt.closed):
                pod_label = pod.get("name") or pod_id
                job["auto_assigned"] = False
                job["pod_id"] = None
                job["status"] = "queued"
                job["waiting_reason"] = f"'{pod_label}': 배정된 뒤 연결이 끊겼어요"
                set_comfy_wait_flag(job, False)
                reassigned = True
            else:
                reassigned = False
        if reassigned:
            save_state()
            poke_scheduler()
            return "moved", None
        with lock:
            rt = pod_runtimes.get(pod_id)
            keep_waiting = bool(rt and rt.auto_run and not rt.closed)
            if keep_waiting:
                changed = set_comfy_wait_flag(job, True)
            else:
                set_comfy_wait_flag(job, False)
                job["status"] = "pending"
                job["started_at"] = None
                changed = True
        if changed:
            save_state()
        if not keep_waiting:
            # 아무 설명 없이 대기 목록으로 되돌아가면 사용자가 이유를 알 길이 없으므로
            # 로그에 한 줄 남긴다(작업 행을 펼치면 그대로 보인다).
            where = url or "자동 탐지 실패"
            (LOGS_DIR / f"{job_id}.log").write_text(
                f"'{pod['name']}' 파드에 연결할 수 없어 대기 목록으로 되돌렸어요 ({where}).\n"
                "서버가 켜진 걸 확인한 뒤 ▶ 시작을 누르면 이어서 실행됩니다.\n",
                encoding="utf-8",
            )
            return "pending", None
        waited = True

        # COMFY_WAIT_RETRY_SEC를 통째로 자면 그동안 "⏸ 정지"에 반응하지 못하므로
        # 잘게 쪼개 자면서 중간에 빠져나올 조건을 확인한다.
        slept = 0.0
        while slept < COMFY_WAIT_RETRY_SEC:
            time.sleep(COMFY_WAIT_TICK_SEC)
            slept += COMFY_WAIT_TICK_SEC
            with lock:
                job = jobs.get(job_id)
                rt = pod_runtimes.get(pod_id)
                if (rt is None or not rt.auto_run or rt.closed
                        or job is None or job.get("deleted")
                        or job.get("pod_id") != pod_id):
                    break


def pull_job_outputs(pod: dict, job_id: str, comfy_url: str, log_path: Path):
    """작업 하나가 끝난 뒤 그 작업(job_id 하위 폴더)의 결과 이미지만 끌어온다.

    설정이 꺼져 있으면 아무것도 안 한다. 실패해도 작업 상태에는 영향을 주지 않는다 —
    이미지는 원격에 그대로 남아 있고 갤러리 탭에서 수동으로 다시 가져올 수 있으므로,
    이미 끝난 작업을 실패로 뒤집을 이유가 없다. 대신 무슨 일이 있었는지 그 작업의
    로그 끝에 덧붙여, 이미지가 안 보일 때 이유를 찾을 수 있게 한다."""
    try:
        result = driver_for(pod).collect(pod, comfy_url, job_id)
    except OutputSyncError as e:
        note = f"[출력 동기화] 실패: {e}"
    else:
        if result is None:
            return
        note = (
            f"[출력 동기화] {len(result['downloaded'])}장 가져옴 "
            f"(확인 {result['checked']}장, 이미 있음 {result['skipped_existing']}장, "
            f"기록됨 {result['skipped_known']}장)"
        )
        if result["errors"]:
            note += f" — 실패 {len(result['errors'])}건: {'; '.join(result['errors'][:3])}"
    try:
        with open(log_path, "a", encoding="utf-8") as logf:
            logf.write(f"\n{note}\n")
    except OSError:
        pass


# ---- 대기 큐 스케줄러 -----------------------------------------------------------------
# 작업은 파드를 정하지 않고 먼저 "대기 큐"(status=queued, pod_id=None)에 들어간다. 이 스케줄러가 주기적으로
# 그 큐를 훑어서, 작업을 돌릴 수 있는 파드가 생기면 그 파드로 배정한다. "돌릴 수 있다"는 다음을 전부 만족하는 것:
#   1) 그 회원의 파드이고 사용 중(enabled)이며 작업 템플릿이 쓰는 파드 종류와 맞는다(고정 파드가 있으면 그 파드만),
#   2) 지금 연결돼 있다,
#   3) 동시에 돌릴 자리가 남아 있다,
#   4) 작업이 필요로 하는 노드/모델 파일이 그 파드에 설치돼 있다.
# 어느 것도 못 채우면 작업은 대기 큐에 그대로 남고, 파드마다 왜 안 되는지를 waiting_reason에 적어 화면에 보여 준다.
# 파드가 하나도 없어도 작업을 만들어 둘 수 있다 — 파드가 생기고 살아나면 그때 돈다.
SCHED_INTERVAL_SEC = float(os.environ.get("NIGHTSHIFT_SCHED_INTERVAL_SEC", "5"))
MODEL_FILE_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx")
_sched_wake = threading.Event()

# 0(기본)이면 꺼짐 — RunPod pod 자동 동기화(runpod_sync.py)를 사람이 MCP로 부르지
# 않아도 이 간격마다 스스로 돌게 한다. 관리자 권한으로 돈다(그 계정 소유로 파드가
# 등록된다) — 자동화라 특정 사람이 부른 게 아니므로.
RUNPOD_SYNC_INTERVAL_SEC = float(os.environ.get("RUNPOD_SYNC_INTERVAL_SEC", "0"))


def poke_scheduler():
    _sched_wake.set()


def job_missing_on_pod(job: dict, pod: dict) -> list[str] | None:
    """이 작업이 그 파드에서 돌기 위해 없는 것들(없는 노드 · 없는 모델 파일). 비어 있으면 갖춘 것,
    None이면 확인 못 함(파드가 목록을 안 준다). ComfyUI가 아닌 파드는 검사할 게 없다.

    워크플로우 안의 값 중 **모델 파일**만 본다 — 입력 이미지처럼 실행 때 스크립트가 채우는 값까지 검사하면
    멀쩡히 도는 작업이 영영 못 도는 쪽으로 막히기 때문이다. 템플릿 옵션(체크포인트/LoRA 드롭다운)으로 덮어쓰는
    종류는 워크플로우에 적힌 값을 무시하고 고른 값 자체를 본다."""
    if pod.get("kind") != pod_registry.DEFAULT_KIND:
        return []
    try:
        _, info = fetch_comfy_object_info(False, pod)
    except Exception:
        return None
    if info is None:
        return None
    template = load_templates_map().get(job.get("template_id")) or {}
    problems: list[str] = []
    overridden: set[tuple[str, str]] = set()
    for option in template.get("options", []):
        if option.get("type") != "comfy_model":
            continue
        value = str((job.get("options") or {}).get(option["name"]) or "").strip()
        source = MODEL_LIST_SOURCES.get(option.get("model_kind"))
        if not value or source is None:
            continue
        overridden.add(source)
        installed = combo_choices(info, *source)
        if installed and value not in installed:
            problems.append(value)
    blobs: list[bytes | None] = []
    for field in ("workflow_filename", "video_workflow_filename"):
        name = job.get(field)
        if name:
            try:
                blobs.append((JOBS_DIR / name).read_bytes())
            except OSError:
                pass
    if not job.get("video_workflow_filename"):
        blobs.append(default_video_workflow_bytes(template))
    for data in blobs:
        if not data:
            continue
        try:
            workflow = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(workflow, dict):
            continue
        nodes, values, _ = workflow_missing(info, workflow)
        problems.extend(f"노드 {n}" for n in nodes)
        for v in values:
            if (v["class_type"], v["field"]) in overridden:
                continue
            if not str(v["value"]).lower().endswith(MODEL_FILE_EXTS):
                continue
            problems.append(v["value"])
    return list(dict.fromkeys(problems))


def _short_list(items: list[str], limit: int = 3) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" 외 {len(items) - limit}개" if len(items) > limit else "")


def _snapshot_pod_into_job(job: dict, pod: dict) -> None:
    """파드는 일시적이라 그 작업이 어디서 돌았나/얼마 들었나를 job에 찍어 둔다(lock 밖에서 부른다)."""
    gpu = cost = None
    try:
        info = runpod_api.get_runpod_info(pod.get("url") or "")
        if info:
            gpu = info.get("gpu_type")
            c = info.get("cost_per_hr")
            cost = float(c) if isinstance(c, (int, float)) else None
    except Exception:
        pass
    job["pod_name"] = pod.get("name")
    job["pod_kind"] = pod.get("kind")
    job["pod_gpu"] = gpu
    job["pod_cost_per_hr"] = cost


def unassign_job(job_id: str, reason: str) -> None:
    """배정됐던 작업을 대기 큐로 되돌린다(파드가 끊겼거나 필요한 모델이 없어졌을 때)."""
    with lock:
        job = jobs.get(job_id)
        if job is None or job.get("deleted") or job["status"] not in ("queued", "pending"):
            return
        job["status"] = "queued"
        job["pod_id"] = None
        job["auto_assigned"] = False
        job["waiting_reason"] = reason
        set_comfy_wait_flag(job, False)
    save_state()
    poke_scheduler()


def schedule_once() -> None:
    with lock:
        waiting = sorted((j for j in jobs.values()
                          if j["status"] == "queued" and not j.get("pod_id") and not j.get("deleted")),
                         key=lambda j: j["queued_at"])
    if not waiting:
        return
    templates = load_templates_map()
    owners = {j.get("owner_id") for j in waiting}
    pods_of = {o: [p for p in pod_registry.list_pods(o) if p.get("enabled")] for o in owners}

    # 파드마다 연결 확인은 한 번만 — 파드가 꺼져 있으면 확인이 몇 초 걸릴 수 있어 나란히 돌린다.
    all_pods = {p["id"]: p for plist in pods_of.values() for p in plist}
    connected: dict[str, bool] = {}
    if all_pods:
        def probe(pod):
            try:
                return pod["id"], bool(driver_for(pod).health(pod)["ok"])
            except Exception:
                return pod["id"], False
        with ThreadPoolExecutor(max_workers=min(8, len(all_pods))) as ex:
            connected = dict(ex.map(probe, all_pods.values()))

    free: dict[str, int] = {}
    for pid, pod in all_pods.items():
        rt = ensure_runtime(pod)
        with lock:
            free[pid] = rt.max_concurrent - (len(rt.running) + rt.queue.qsize())

    for job in waiting:
        template = templates.get(job.get("template_id")) or {}
        allowed = template.get("pod_kinds") or [pod_registry.DEFAULT_KIND]
        pinned = job.get("pinned_pod_id")
        candidates = [p for p in pods_of.get(job.get("owner_id"), [])
                      if p.get("kind") in allowed and (not pinned or p["id"] == pinned)]
        reasons: list[str] = []
        chosen = None
        # 연결돼 있고 자리도 있는데 모델만 없는 파드 중 가장 적게 모자란 곳 — 카드의 "조치 필요"와
        # "없는 모델 받기"(POST /api/jobs/{id}/fetch-missing)가 이 값을 쓴다.
        best_missing: tuple[dict, list[str]] | None = None
        if not candidates:
            if pinned:
                reasons.append("지정한 파드를 쓸 수 없어요(없어졌거나 사용 안 함)")
            else:
                reasons.append("사용할 수 있는 파드가 없어요 — 파드를 추가하거나 켜 주세요")
        for pod in sorted(candidates, key=lambda p: -free.get(p["id"], 0)):
            name = pod.get("name") or pod["id"]
            if not connected.get(pod["id"]):
                reasons.append(f"'{name}': 연결 안 됨")
                continue
            if free.get(pod["id"], 0) <= 0:
                reasons.append(f"'{name}': 다른 작업이 돌고 있어요")
                continue
            missing = job_missing_on_pod(job, pod)
            if missing is None:
                reasons.append(f"'{name}': 설치된 모델 목록을 못 읽었어요")
                continue
            if missing:
                reasons.append(f"'{name}': 없는 것 — {_short_list(missing)}")
                if best_missing is None or len(missing) < len(best_missing[1]):
                    best_missing = (pod, missing)
                continue
            chosen = pod
            break
        if chosen is None:
            reason = " · ".join(reasons)
            missing_info = ({"pod_id": best_missing[0]["id"], "pod_name": best_missing[0].get("name") or best_missing[0]["id"],
                             "names": best_missing[1]} if best_missing else None)
            with lock:
                if job.get("waiting_reason") == reason and job.get("missing_models") == missing_info:
                    continue
                job["waiting_reason"] = reason
                job["missing_models"] = missing_info
            save_state()
            continue
        _snapshot_pod_into_job(job, chosen)
        with lock:
            if job["status"] != "queued" or job.get("pod_id") or job.get("deleted"):
                continue   # 그 사이 사용자가 지웠거나 옮겼다
            job["pod_id"] = chosen["id"]
            job["auto_assigned"] = True
            job["waiting_reason"] = None
            job["missing_models"] = None
            job["fetching_models"] = None
            job["fetch_error"] = None
            job["preflight"] = None
            free[chosen["id"]] -= 1
        save_state()
        dispatch_job(job["id"], chosen["id"])


def scheduler_loop():
    while True:
        _sched_wake.wait(SCHED_INTERVAL_SEC)
        _sched_wake.clear()
        try:
            schedule_once()
        except Exception:
            logging.getLogger("uvicorn.error").exception("스케줄러 오류")


def dispatch_job(job_id: str, pod_id: str | None = None):
    """작업을 그 작업이 배정된 파드의 큐에 넣는다. 파드가 없거나 꺼져 있으면 기본
    파드로 되돌린다 — 큐에 못 들어가 영영 안 도는 작업이 생기면 안 되므로."""
    with lock:
        job = jobs.get(job_id)
        if job is None or job.get("deleted"):
            return
        target = pod_id or job.get("pod_id")
    pod = pod_registry.get_pod(target) if target else None
    if pod is None:
        with lock:
            owner = jobs[job_id].get("owner_id")
        pod = pod_registry.default_pod_for(owner)   # 같은 주인의 파드로만 되돌린다
        if pod is None:
            with lock:
                jobs[job_id]["status"] = "pending"
            save_state()
            return
    with lock:
        jobs[job_id]["pod_id"] = pod["id"]
        rt = pod_runtimes.get(pod["id"])
    if rt is None:
        rt = ensure_runtime(pod)
    rt.queue.put(job_id)


def pod_worker_loop(pod_id: str):
    """파드 하나를 담당하는 워커 스레드. 파드마다 max_concurrent개가 돈다.

    예전에는 이 루프가 프로세스 전체에 하나뿐이었다(전역 job_queue). 이제 큐도 자동
    실행 플래그도 파드별이라, 파드 하나가 멈춰도 다른 파드는 그대로 돈다."""
    while True:
        with lock:
            rt = pod_runtimes.get(pod_id)
            if rt is None or rt.closed:
                return
            q = rt.queue
        try:
            job_id = q.get(timeout=1.0)
        except queue.Empty:
            continue

        try:
            _run_one_job(pod_id, job_id)
        finally:
            q.task_done()


def _run_one_job(pod_id: str, job_id: str):
    # 파드에 연결될 때까지 붙잡아 둔다 — 작업은 "queued"인 채 waiting_for_comfy
    # 표시만 붙고, 연결되는 순간 이어서 실행된다.
    # 큐에 들어간 뒤 다른 파드로 옮겨졌을 수 있다. 그때는 여기서 **그냥 버린다** —
    # 다시 배차하지 않는다. 파드를 바꾼 쪽(POST /api/jobs/{id}/move)이 새 파드 큐에
    # 이미 넣었기 때문에, 여기서 또 넣으면 같은 작업이 두 번 돈다. 파드가 통째로
    # 지워진 경우도 마찬가지로 그쪽에서 pending으로 되돌려 두므로 버리면 된다.
    action, comfy_url = wait_for_pod(pod_id, job_id)
    if action != "run" or comfy_url is None:
        return

    pod = pod_registry.get_pod(pod_id)
    if pod is None:
        return

    # 시작하기 직전에 한 번 더 — 배정된 뒤 큐에서 기다리는 사이 모델이 사라졌거나, 파드를 손으로 지정해서 넣은
    # 작업이라 스케줄러 검사를 거치지 않았을 수 있다. 갖춰지지 않았으면 돌리지 않고 대기 큐로 되돌린다.
    with lock:
        snapshot = dict(jobs.get(job_id) or {})
    if snapshot and not snapshot.get("deleted") and snapshot.get("pod_id") == pod_id:
        missing = job_missing_on_pod(snapshot, pod)
        if missing:
            unassign_job(job_id, f"'{pod.get('name') or pod_id}': 없는 것 — {_short_list(missing)}")
            return

    with lock:
        job = jobs.get(job_id)
        if job is None or job.get("deleted") or job.get("pod_id") != pod_id:
            return
        job.pop("waiting_reason", None)
        job["status"] = "running"
        job["started_at"] = now_iso()
        job["progress"] = None
    save_state()

    log_path = LOGS_DIR / f"{job_id}.log"
    script_path = TEMPLATES_DIR / job["script_filename"]

    # 파드 종류마다 작업에 실어 보낼 환경변수가 다르다(ComfyUI는 COMFY_URL).
    # JOB_ID/NIGHTSHIFT_URL은 워커 종류와 무관하게 항상 필요한 것들이다.
    extra_env = {"JOB_ID": job_id, "NIGHTSHIFT_URL": SELF_URL,
                 **driver_for(pod).job_env(pod, comfy_url)}
    if job.get("workflow_filename"):
        extra_env["WORKFLOW_PATH"] = str((JOBS_DIR / job["workflow_filename"]).resolve())
    if job.get("video_workflow_filename"):
        extra_env["VIDEO_WORKFLOW_UPLOAD_PATH"] = str((JOBS_DIR / job["video_workflow_filename"]).resolve())
    if job.get("csv_filename"):
        extra_env["CSV_PATH"] = str((JOBS_DIR / job["csv_filename"]).resolve())
    for name, value in job.get("options", {}).items():
        extra_env[name.upper()] = str(value)
    extra_env.update(ref_assets_job_env(job.get("owner_id")))
    env = {**os.environ, **extra_env}

    stopped = False
    with open(log_path, "w") as logf:
        try:
            proc = subprocess.Popen(
                [sys.executable, "-u", str(script_path)],
                stdout=logf,
                stderr=subprocess.STDOUT,
                env=env,
            )
            with lock:
                rt = pod_runtimes.get(pod_id)
                if rt is not None:
                    rt.running[job_id] = proc
            returncode = proc.wait()
        except Exception as e:
            logf.write(f"\n[runner error] {e}\n")
            returncode = -1
        finally:
            with lock:
                rt = pod_runtimes.get(pod_id)
                if rt is not None:
                    rt.running.pop(job_id, None)
                stopped = bool(job.pop("_stop_requested", False))

    with lock:
        job["status"] = "interrupted" if stopped else ("done" if returncode == 0 else "failed")
        job["returncode"] = returncode
        job["finished_at"] = now_iso()
    save_state()

    # 워커가 원격이면 결과물은 그쪽 디스크에만 있다 — 갤러리/zip/이메일은 전부 로컬
    # 출력 폴더를 읽으므로, 작업이 끝난 직후 그 작업 몫만 끌어온다. 중간에 실패했더라도
    # 그때까지 나온 이미지는 가져온다(returncode를 안 본다).
    pull_job_outputs(pod, job_id, comfy_url, log_path)
    # 방금 끝난 작업의 결과물을 색인에 넣는다(그 job의 프로젝트를 물려받는다).
    _sync_assets_quietly(force=True)


def ref_assets_job_env(owner_id) -> dict[str, str]:
    """일반 회원의 작업이 그 회원의 참조·입력 이미지 폴더만 쓰게 하는 환경변수."""
    owner = auth.get_user(owner_id) if owner_id is not None else None
    if owner is None or auth.is_admin(owner):
        return {}
    return job_env_for_owner(owner_id)


def ensure_runtime(pod: dict) -> PodRuntime:
    """그 파드의 실행 상태와 워커 스레드를 준비한다(이미 있으면 그대로 쓴다).
    max_concurrent를 늘렸으면 모자란 만큼 스레드를 더 띄운다."""
    pod_id = pod["id"]
    with lock:
        rt = pod_runtimes.get(pod_id)
        if rt is None:
            rt = PodRuntime(pod_id, pod.get("max_concurrent", 1))
            pod_runtimes[pod_id] = rt
        rt.closed = False
        rt.max_concurrent = max(1, int(pod.get("max_concurrent") or 1))
        missing = rt.max_concurrent - rt.workers
        rt.workers += max(0, missing)
    for _ in range(max(0, missing)):
        threading.Thread(target=pod_worker_loop, args=(pod_id,), daemon=True).start()
    return rt


def sync_runtimes():
    """레지스트리에 있는 파드마다 런타임을 준비하고, 사라진 파드의 런타임은 닫는다."""
    pods = pod_registry.list_pods()
    for pod in pods:
        ensure_runtime(pod)
    alive = {p["id"] for p in pods}
    with lock:
        gone = [pid for pid in pod_runtimes if pid not in alive]
        for pid in gone:
            pod_runtimes[pid].closed = True
            del pod_runtimes[pid]


def _sync_assets_quietly(force: bool = False):
    # 색인은 부가 기능이라 실패해도 갤러리/작업 흐름을 막지 않는다.
    try:
        assets_index.sync(force=force)
    except Exception:
        logging.exception("결과물 색인(assets) 동기화에 실패했어요")


def _sync_key_mapper(item: dict, pod_id: str | None) -> str | None:
    """원격에서 받은 항목의 로컬 경로. nightshift 작업 폴더(job_id)는 그 작업의 주인의 파드에서만 받고, 그 밖의
    것(ComfyUI에서 직접 돌린 결과)은 파드 주인별 폴더(u<id>/)로 나눠 담아 회원끼리 섞이지도 덮어쓰지도 않게 한다."""
    pod = pod_registry.get_pod(pod_id) if pod_id else None
    owner = pod.get("owner_id") if pod else None
    subfolder = item.get("subfolder") or ""
    with lock:
        job = jobs.get(subfolder) if subfolder else None
    if job is not None:
        return item["key"] if job.get("owner_id") == owner else None   # 남의 작업 폴더에는 쓰지 않는다
    return f"u{owner}/{item['key']}" if owner is not None else item["key"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    comfy_outputs.set_key_mapper(_sync_key_mapper)
    load_state()
    admin = auth.ensure_admin()
    if admin is not None:
        pod_registry.load()
        pod_registry.assign_orphans(admin["id"])
        with lock:
            for job in jobs.values():
                if job.get("owner_id") is None:
                    job["owner_id"] = admin["id"]
        save_state()
    recent_workflows_store.load()
    recent_csvs_store.load()
    load_danbooru_state()
    load_lora_triggers()
    pod_registry.load()
    # 재시작 전에 running/queued 상태로 남아있던 기록은 재실행되지 않으므로 상태만 정리.
    # 파드 연결을 기다리던 중이었다는 표시(waiting_for_comfy)도 함께 지운다 — 그
    # 대기는 워커 스레드 안에서만 살아 있는 상태라 재시작하면 남아 있을 이유가 없다.
    # pod_id가 없는 옛 작업 기록은 기본 파드 것으로 본다(다중 파드 이전에 만들어진 것).
    # 실행 중이던 작업은 중단으로 표시하지만, 아직 시작 안 하고 큐에서 기다리던 작업은 살려 둔다 — 파드가 살아나기를
    # 기다리는 게 이제 대기 큐의 정상 동작이라, 서버 재시작이 그 줄을 없애면 안 된다. 어느 파드 큐(메모리)에
    # 들어가 있던 것은 그 큐가 사라졌으니 대기 큐로 되돌려 스케줄러가 다시 배정하게 한다.
    try:
        default_id = pod_registry.default_pod()["id"]
    except Exception:
        default_id = None   # 파드가 하나도 없다
    with lock:
        for job in jobs.values():
            if job["status"] == "running":
                job["status"] = "interrupted"
            elif job["status"] == "queued" and not job.get("deleted"):
                job["pod_id"] = None
                job["auto_assigned"] = False
            elif "pod_id" not in job:
                job["pod_id"] = default_id
            set_comfy_wait_flag(job, False)
    save_state()
    sync_runtimes()
    threading.Thread(target=scheduler_loop, daemon=True, name="scheduler").start()
    poke_scheduler()
    if RUNPOD_SYNC_INTERVAL_SEC > 0:
        threading.Thread(target=_runpod_sync_loop, daemon=True, name="runpod-sync").start()
    # 결과물 색인(assets)을 디스크와 맞춘다 — 파일이 많으면 시간이 걸릴 수 있으니
    # 서버가 뜨는 걸 붙잡지 않게 뒤에서 돌린다.
    threading.Thread(target=_sync_assets_quietly, kwargs={"force": True}, daemon=True).start()
    yield


app = FastAPI(title="RunPod Job Queue", lifespan=lifespan)

# ---- 인증: 회원 로그인(세션 쿠키) --------------------------------------------------------
# 예전에는 NIGHTSHIFT_API_KEY 하나로 /api/*를 막았다. 이제는 회원마다 아이디/비밀번호로 로그인해서 세션
# 쿠키를 받고(auth.py), 쿠키가 있는 요청만 /api/*를 쓸 수 있다. 정적 파일(/)은 그대로 열어둔다 — 화면
# 자체에는 민감한 정보가 없고, 로그인 화면도 거기서 뜬다. <img>/<video>/다운로드 링크는 쿠키가 알아서 실린다.
#
# 작업 스크립트(서브프로세스)가 진행 상황을 알리는 PUT /api/jobs/{id}/progress만은 로그인이 없으므로,
# 서버가 뜰 때마다 새로 만드는 내부 토큰을 X-API-Key로 받는다(스크립트가 이미 그 헤더를 쓴다 — 서버 환경변수
# NIGHTSHIFT_API_KEY를 그대로 물려받으므로 아래에서 그 값을 이 토큰으로 덮어쓴다). 이 토큰은 그 한 경로에만 통한다.
INTERNAL_TOKEN = secrets.token_urlsafe(32)
os.environ["NIGHTSHIFT_API_KEY"] = INTERNAL_TOKEN

# MCP 서버(mcp_server.py) 전용 내부 키 — MCP 서버가 사람의 admin 비밀번호로 로그인하지 않고도 admin
# 권한으로 API를 쓰게 한다(그 비밀번호는 JupyterLab 게이트의 열쇠이기도 해서 .env에 적어 두면 안 된다).
# 이 키는 서버 밖으로 나가지 않는 값이라는 점이 방어선의 전부다. 그래서:
# - 32자 미만이면 기능 자체를 끈다(빈 값이나 짧은 값으로 통과되는 일이 없게).
# - Cloudflare 터널로 들어온 요청은 cloudflared가 localhost로 넘겨 주므로 127.0.0.1에서 온 것처럼 보인다.
#   그래서 접속 주소만 보지 않고, Cloudflare가 반드시 붙이고 보내는 쪽이 지울 수 없는 CF-Connecting-IP
#   (그리고 프록시가 붙이는 X-Forwarded-For)가 있으면 키가 맞아도 거절한다.
# - 키로는 회원·세션을 다루는 /api/auth/*, /api/admin/*를 못 쓴다(비밀번호 변경·회원 관리 불가).
# - 작업 서브프로세스(셸 파드 등)가 물려받지 않도록 환경변수에서 빼 둔다.
MCP_INTERNAL_KEY = os.environ.pop("NIGHTSHIFT_MCP_KEY", "").strip()
MCP_KEY_HEADER = "x-nightshift-mcp-key"
MCP_KEY_MIN_LEN = 32
MCP_KEY_BLOCKED_RE = re.compile(r"^/api/(auth|admin)/")
MCP_KEY_PROXY_HEADERS = ("cf-connecting-ip", "x-forwarded-for")
LOOPBACK_HOSTS = {"127.0.0.1", "::1"}

# 로그인 쿠키에 Domain을 찍으면(예: lomebrote.com) 다른 서브도메인(opencut.lomebrote.com의
# 로그인 게이트 등)도 같은 쿠키를 받아 SSO가 된다. 비워 두면 지금처럼 이 서브도메인 전용(host-only)이다.
COOKIE_DOMAIN = os.environ.get("NIGHTSHIFT_COOKIE_DOMAIN", "").strip() or None

# 외부 편집기(OpenCut)가 다른 서브도메인에서 쓰는 공유 세션 API(/api/shared/*)는 로그인 쿠키 대신 세션 토큰으로 인증한다.
# 이 경로들은 아래 미들웨어가 로그인/CSRF 검사를 건너뛰고, 대신 허용한 편집기 출처에만 CORS를 열어 준다.
OPENCUT_URL = os.environ.get("NIGHTSHIFT_OPENCUT_URL", "https://opencut.lomebrote.com").strip().rstrip("/")
OPENCUT_ORIGINS = {o for o in ([OPENCUT_URL] + [x.strip().rstrip("/") for x in
                                                os.environ.get("NIGHTSHIFT_OPENCUT_EXTRA_ORIGINS", "").split(",")]) if o}
SHARED_API_RE = re.compile(r"^/api/shared/")
SHARE_UPLOAD_MAX_BYTES = int(os.environ.get("NIGHTSHIFT_SHARE_UPLOAD_MAX_MB", "2048")) * 1024 * 1024
PUBLIC_API_PATHS = {"/api/auth/register", "/api/auth/login", "/api/auth/logout", "/api/auth/me"}
INTERNAL_PROGRESS_RE = re.compile(r"^/api/jobs/[^/]+/progress$")
CSRF_HEADER, CSRF_VALUE = "x-requested-with", "nightshift"
MAX_REQUEST_BYTES = int(os.environ.get("NIGHTSHIFT_MAX_REQUEST_MB", "200")) * 1024 * 1024


@app.middleware("http")
async def authenticate_request(request: Request, call_next):
    path = request.url.path
    request.state.user = None
    request.state.internal = False
    if not path.startswith("/api/"):
        return await call_next(request)
    if SHARED_API_RE.match(path):
        origin = request.headers.get("origin", "").rstrip("/")
        cors = {"Cross-Origin-Resource-Policy": "cross-site", "Vary": "Origin"}
        if origin in OPENCUT_ORIGINS:
            cors.update({
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Range",
                "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
                "Access-Control-Max-Age": "600",
            })
        if request.method == "OPTIONS":
            return Response(status_code=204, headers=cors)
        response = await call_next(request)
        for key, value in cors.items():
            response.headers[key] = value
        return response

    # 다른 사이트가 로그인된 브라우저로 쓰기 요청을 날리는 걸 막는다 — 커스텀 헤더는 다른 출처에서
    # 사전 요청(preflight) 없이는 못 붙이므로, 화면(fetch 래퍼)이 붙이는 이 헤더가 있어야 쓰기가 통한다.
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        provided_key = request.headers.get("x-api-key", "")
        internal_ok = (INTERNAL_PROGRESS_RE.match(path) and request.method == "PUT" and provided_key
                       and hmac.compare_digest(provided_key, INTERNAL_TOKEN))
        if internal_ok:
            request.state.internal = True
            return await call_next(request)
        if request.headers.get(CSRF_HEADER, "").lower() != CSRF_VALUE:
            return JSONResponse({"detail": "요청이 올바르지 않아요(화면을 새로고침해 주세요)."}, status_code=403)

    try:
        if int(request.headers.get("content-length") or 0) > MAX_REQUEST_BYTES:
            return JSONResponse({"detail": "요청이 너무 커요."}, status_code=413)
    except ValueError:
        pass
    if request.headers.get(MCP_KEY_HEADER):
        user = await asyncio.to_thread(_mcp_key_user, request)
        if user is None:
            return JSONResponse({"detail": "MCP 내부 키가 올바르지 않거나, 이 경로로는 쓸 수 없어요."}, status_code=401)
        if MCP_KEY_BLOCKED_RE.match(path):
            return JSONResponse({"detail": "MCP 내부 키로는 계정·회원 관리 API를 쓸 수 없어요."}, status_code=403)
    else:
        user = await asyncio.to_thread(auth.user_for_token, request.cookies.get(auth.SESSION_COOKIE))
    request.state.user = user
    if user is None and path not in PUBLIC_API_PATHS:
        return JSONResponse({"detail": "로그인이 필요해요."}, status_code=401)
    ctx_token = auth.current_user.set(user)
    try:
        return await call_next(request)
    finally:
        auth.current_user.reset(ctx_token)


def _mcp_key_user(request: Request) -> dict | None:
    """X-Nightshift-MCP-Key가 맞고, 같은 머신에서 프록시를 거치지 않고 직접 온 요청이면 admin 회원을
    돌려준다(아니면 None). 조건은 MCP_INTERNAL_KEY 정의부 주석 참고."""
    if len(MCP_INTERNAL_KEY) < MCP_KEY_MIN_LEN:
        return None
    provided = request.headers.get(MCP_KEY_HEADER, "")
    if not hmac.compare_digest(provided.encode("utf-8"), MCP_INTERNAL_KEY.encode("utf-8")):
        return None
    if any(request.headers.get(h) for h in MCP_KEY_PROXY_HEADERS):
        return None
    if (request.client.host if request.client else "") not in LOOPBACK_HOSTS:
        return None
    admin_id = auth.admin_id()
    return auth.get_user(admin_id) if admin_id is not None else None


def me(request: Request) -> dict:
    """지금 요청을 보낸 로그인한 회원(없으면 401)."""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "로그인이 필요해요.")
    return user


def admin_only(request: Request) -> dict:
    user = me(request)
    if not auth.is_admin(user):
        raise HTTPException(403, "관리자만 할 수 있어요.")
    return user


def visible_pods_for(user: dict) -> list[dict]:
    """이 회원이 볼 수 있는 파드 — admin은 전부, 일반 회원은 자기 것만."""
    return pod_registry.list_pods() if auth.is_admin(user) else pod_registry.list_pods(user["id"])


def pod_or_404(user: dict, pod_id: str) -> dict:
    """이 회원이 쓸 수 있는 파드 하나 — 남의 파드는 있다는 사실도 알려 주지 않는다(404)."""
    pod = pod_registry.get_pod(pod_id) if pod_id else None
    if pod is None or not auth.can_access(user, pod.get("owner_id")):
        raise HTTPException(404, "없는 파드예요.")
    return pod


def default_pod_for_user(user: dict) -> dict | None:
    """파드를 지정하지 않았을 때 쓸 이 회원의 파드. 일반 회원이 파드가 하나도 없으면 None."""
    pod = pod_registry.default_pod_for(user["id"])
    if pod is None and auth.is_admin(user):
        return pod_registry.default_pod()
    return pod


def job_or_404(user: dict, job_id: str) -> dict:
    """이 회원이 볼 수 있는 작업 하나 — 남의 작업은 없는 것처럼 404. (jobs dict의 실제 객체를 돌려준다.)"""
    with lock:
        job = jobs.get(job_id)
    if job is None or not auth.can_access(user, job.get("owner_id")):
        raise HTTPException(404, "없는 작업이에요.")
    return job


def job_scope(user: dict):
    """작업 목록을 거를 때 쓰는 소유자 값 — admin은 None(전부), 일반 회원은 자기 id."""
    return auth.owner_scope(user)


def owned(entity: dict, scope) -> bool:
    return scope is None or entity.get("owner_id") == scope


NO_POD = {"id": "", "kind": "comfyui", "url": "", "enabled": False, "_none": True}   # "쓸 파드 없음"을 뜻하는 자리표시


def client_ip(request: Request) -> str:
    return (request.headers.get("cf-connecting-ip")
            or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else ""))


def _set_session_cookie(request: Request, response: Response, token: str) -> None:
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"
    response.set_cookie(auth.SESSION_COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True,
                        samesite="lax", secure=secure, path="/", domain=COOKIE_DOMAIN)


def _auth_error(e: "auth.AuthError") -> HTTPException:
    return HTTPException(e.status, e.message)


@app.get("/api/auth/me")
def auth_me(request: Request):
    # 화면이 처음 열릴 때 "로그인돼 있나"를 묻는 용도 — 로그인이 안 돼 있어도 401이 아니라 user=null이다.
    return {"user": request.state.user}


@app.post("/api/auth/register")
async def auth_register(request: Request):
    body = await read_json_object(request, allow_empty=False)
    try:
        auth.check_register_throttle(client_ip(request))
        user = await asyncio.to_thread(auth.register, body.get("username"), body.get("email"), body.get("password"))
    except auth.AuthError as e:
        raise _auth_error(e)
    return JSONResponse({"user": user, "message": "가입 신청이 접수됐어요. 관리자가 승인하면 로그인할 수 있어요."}, status_code=201)


@app.post("/api/auth/login")
async def auth_login(request: Request):
    body = await read_json_object(request, allow_empty=False)
    ip = client_ip(request)
    try:
        user = await asyncio.to_thread(auth.authenticate, body.get("username"), body.get("password"), ip)
    except auth.AuthError as e:
        raise _auth_error(e)
    token = await asyncio.to_thread(auth.create_session, user["id"], ip, request.headers.get("user-agent", ""))
    response = JSONResponse({"user": user})
    _set_session_cookie(request, response, token)
    return response


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    await asyncio.to_thread(auth.delete_session, request.cookies.get(auth.SESSION_COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.SESSION_COOKIE, path="/", domain=COOKIE_DOMAIN)
    return response


@app.post("/api/auth/change-password")
async def auth_change_password(request: Request):
    user = me(request)
    body = await read_json_object(request, allow_empty=False)
    try:
        await asyncio.to_thread(auth.change_password, user["id"], body.get("old_password"), body.get("new_password"),
                                request.cookies.get(auth.SESSION_COOKIE))
    except auth.AuthError as e:
        raise _auth_error(e)
    return {"ok": True}


@app.get("/api/auth/secrets")
def auth_get_secrets(request: Request):
    # 회원 각자의 API 키(civitai_token/runpod_api_key) — 계정 관리 모달에서 본인만 보고 고친다.
    # 아직 이 값을 실제로 쓰는 코드는 없다(저장만 해 둔다).
    user = me(request)
    return auth.get_secrets(user["id"])


@app.put("/api/auth/secrets")
async def auth_put_secrets(request: Request):
    user = me(request)
    body = await read_json_object(request, allow_empty=False)
    fields = {k: body[k] for k in auth.SECRET_FIELDS if k in body}
    try:
        return await asyncio.to_thread(auth.set_secrets, user["id"], fields)
    except auth.AuthError as e:
        raise _auth_error(e)


# ---- 회원 관리 (admin 전용) ---------------------------------------------------------------

@app.get("/api/admin/users")
def admin_list_users(request: Request):
    admin_only(request)
    return {"users": auth.list_users()}


@app.post("/api/admin/users/{user_id}/status")
async def admin_set_user_status(user_id: int, request: Request):
    admin = admin_only(request)
    body = await read_json_object(request, allow_empty=False)
    try:
        return {"user": auth.set_status(user_id, str(body.get("status") or ""), admin["id"])}
    except auth.AuthError as e:
        raise _auth_error(e)


@app.post("/api/admin/users/{user_id}/reset-password")
async def admin_reset_password(user_id: int, request: Request):
    admin_only(request)
    body = await read_json_object(request, allow_empty=False)
    try:
        await asyncio.to_thread(auth.reset_password, user_id, body.get("new_password"))
    except auth.AuthError as e:
        raise _auth_error(e)
    return {"ok": True}


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int, request: Request):
    admin_only(request)
    try:
        auth.delete_user(user_id)
    except auth.AuthError as e:
        raise _auth_error(e)
    pod_registry.release_owner(user_id)   # 그 회원의 파드는 주인 없음(=관리자 것)이 된다
    return {"ok": True}


@app.get("/api/templates")
def list_templates():
    return load_templates_list()


# 헤더 배지가 5초마다 물어보는 연결 상태의 캐시. 이 요청이 매번 원격 pod까지
# 왕복하면 탭 수만큼 pod를 찌르고, 응답이 느린 날에는 요청 자체가 몇 초씩 걸린다.
# 그래서 캐시값을 즉시 돌려주고 오래됐으면 백그라운드로 다시 확인한다.
# 파드마다 하나씩. 파드가 여러 대면 대시보드가 전부를 한 번에 물어보므로, 캐시가
# 없으면 그 요청 하나가 파드 수만큼의 왕복이 된다.
_comfy_status_cache: dict[str, dict] = {}
_comfy_status_lock = threading.Lock()
_comfy_status_refreshing: set[str] = set()


def _status_entry(pod_id: str) -> dict:
    """호출 전에 _comfy_status_lock을 쥐고 있어야 한다."""
    return _comfy_status_cache.setdefault(pod_id, {
        "url": None, "connected": False, "source": "auto", "checked_at": 0.0, "fail_streak": 0,
    })


def pod_status_payload(pod: dict) -> dict:
    """캐시된 연결 상태 (호출 전에 _comfy_status_lock을 쥐고 있어야 한다)."""
    entry = _status_entry(pod["id"])
    checked_at = entry["checked_at"]
    return {
        "pod_id": pod["id"],
        "url": entry["url"],
        "connected": entry["connected"],
        "source": entry["source"],
        # 화면이 갤러리의 "결과 가져오기" 버튼을 보여줄지 정하는 데 쓴다 — 5초마다
        # 폴링하는 이 응답에 실어 주면 설정을 바꿨을 때 저절로 따라온다.
        "pull_outputs": bool(pod.get("pull_outputs")),
        # 이 상태가 몇 초 전에 실측된 것인지(캐시라는 사실을 숨기지 않는다).
        "checked_age_sec": round(time.monotonic() - checked_at, 1) if checked_at else None,
    }


def refresh_pod_status(pod: dict) -> dict:
    """실제로 그 파드를 찔러 보고 캐시를 갱신한다(블로킹).

    한 번 실패했다고 바로 "연결 안 됨"으로 뒤집지 않는다 — WAN에서는 패킷 하나만
    흘려도 실패로 보이는데, 그때마다 배지가 빨갛게 깜빡이면 아무도 안 믿게 된다.
    직전까지 같은 주소로 연결돼 있었다면 COMFY_STATUS_FAIL_STREAK번 연속 실패할
    때까지 "연결됨"을 유지한다(반대로 다시 붙는 건 즉시 반영한다)."""
    health = driver_for(pod).health(pod)
    url, connected, source = health["url"], health["ok"], health["source"]
    with _comfy_status_lock:
        entry = _status_entry(pod["id"])
        if connected:
            reported, streak = True, 0
        else:
            streak = entry["fail_streak"] + 1
            was_up = entry["connected"] and entry["url"] == url
            reported = was_up and streak < COMFY_STATUS_FAIL_STREAK
        entry.update({
            "url": url, "connected": reported, "source": source,
            "checked_at": time.monotonic(), "fail_streak": streak,
        })
        return pod_status_payload(pod)


def invalidate_comfy_status_cache(pod_id: str | None = None):
    """주소 설정이 바뀌었을 때 — 다음 조회가 옛 주소의 결과를 그대로 쓰지 않게 한다."""
    with _comfy_status_lock:
        targets = [pod_id] if pod_id else list(_comfy_status_cache)
        for pid in targets:
            _comfy_status_cache.pop(pid, None)


def _refresh_pod_status_bg(pod: dict):
    try:
        refresh_pod_status(pod)
    finally:
        with _comfy_status_lock:
            _comfy_status_refreshing.discard(pod["id"])


async def pod_status(pod: dict) -> dict:
    """캐시된 값을 즉시 돌려주고, 오래됐으면 백그라운드로 다시 확인한다."""
    pod_id = pod["id"]
    with _comfy_status_lock:
        entry = _status_entry(pod_id)
        first_time = not entry["checked_at"]
        stale = first_time or (time.monotonic() - entry["checked_at"]) >= COMFY_STATUS_TTL_SEC
        should_refresh = stale and pod_id not in _comfy_status_refreshing
        if should_refresh:
            _comfy_status_refreshing.add(pod_id)

    if first_time and should_refresh:
        # 기동 직후 그 파드의 첫 조회. 여기서만 실제 확인이 끝날 때까지 기다린다 —
        # 첫 화면에 근거 없는 "연결 안 됨"이 떴다가 5초 뒤에 바뀌는 것보다 낫다.
        try:
            return await asyncio.to_thread(refresh_pod_status, pod)
        finally:
            with _comfy_status_lock:
                _comfy_status_refreshing.discard(pod_id)

    if should_refresh:
        threading.Thread(target=_refresh_pod_status_bg, args=(pod,), daemon=True).start()
    with _comfy_status_lock:
        return pod_status_payload(pod)


@app.get("/api/comfy-status")
async def comfy_status(request: Request):
    """헤더 배지가 5초마다 물어보는 "지금 기본으로 쓰는 워커"의 연결 상태."""
    pod = default_pod_for_user(me(request))
    if pod is None:
        return {"pod_id": None, "url": None, "connected": False, "source": "none",
                "pull_outputs": False, "checked_age_sec": None}
    return await pod_status(pod)


def comfy_endpoint_payload(user: dict) -> dict:
    """접속 주소 설정 화면이 쓰는 현재 상태 — 기본 파드의 저장된 값, 실제로 쓰이는 값과
    그 출처. 다중 파드로 넘어간 뒤에도 이 화면(헤더의 연결 상태 배지)은 "지금 기본으로
    쓰는 워커"를 보여주는 자리로 남으므로, 응답 형식을 그대로 유지한다."""
    pod = default_pod_for_user(user)
    admin = auth.is_admin(user)
    if pod is None:
        return {"pod_id": None, "pod_name": None, "url": "", "effective_url": None, "source": "none", "env_url": "",
                "candidates": [], "updated_at": None, "pull_outputs": False, "output_dir": "",
                "output_sync": {"last_sync": None, "known": 0}}
    effective_url, source = ComfyUIDriver.configured(pod)
    return {
        "pod_id": pod["id"],                              # 어느 파드의 설정인지
        "pod_name": pod["name"],
        "url": pod.get("url") or "",                      # 저장된 설정값(비어 있으면 미설정)
        "effective_url": effective_url,                   # 설정/환경변수로 정해진 주소(자동 탐지면 null)
        "source": source,                                 # "setting" | "env" | "auto"
        # 서버 내부 정보(환경변수 주소·자동 탐지 후보·출력 폴더 경로)는 관리자에게만 알려 준다.
        "env_url": (os.environ.get("COMFY_URL") or "") if admin else "",
        "candidates": COMFY_CANDIDATE_URLS if admin else [],
        "updated_at": pod.get("updated_at"),
        "pull_outputs": bool(pod.get("pull_outputs")),    # 결과 이미지를 HTTP로 끌어올지
        "output_dir": OUTPUT_DIR if admin else "",        # 끌어온 이미지가 쌓이는 로컬 폴더
        "output_sync": sync_state_summary(),              # {"last_sync", "known"}
    }


@app.get("/api/comfy-endpoint")
def get_comfy_endpoint(request: Request):
    return comfy_endpoint_payload(me(request))


@app.put("/api/comfy-endpoint")
async def put_comfy_endpoint(request: Request):
    # ComfyUI 주소를 런타임에 바꾼다 — nightshift를 홈서버에 상시 띄워두고 ComfyUI만
    # 원격 pod에서 돌리는 구성에서는 pod를 새로 만들 때마다 주소가 바뀌므로, 서버를
    # 재시작하지 않고 화면에서 갈아끼울 수 있어야 한다. 빈 문자열을 보내면 설정을
    # 지우고 환경변수/자동 탐지로 되돌린다.
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    if not isinstance(data, dict):
        raise HTTPException(400, '{"url": "..."} 형태의 객체여야 해요.')
    # 이 엔드포인트는 "이 회원의 기본 파드의 주소를 바꾼다"는 뜻이다.
    user = me(request)
    pod = default_pod_for_user(user)
    if pod is None:
        raise HTTPException(400, "파드가 아직 없어요. 파드 화면에서 먼저 추가해 주세요.")
    patch = {"url": data.get("url", "")}
    if not auth.is_admin(user):
        await asyncio.to_thread(_require_public_pod_url, patch["url"], pod.get("kind"))
    # pull_outputs를 아예 안 보내면 지금 설정을 유지한다 — 주소만 바꾸려는 요청이
    # 조용히 "가져오기 끄기"로 동작하면 안 되므로.
    if "pull_outputs" in data:
        patch["pull_outputs"] = bool(data.get("pull_outputs"))
    try:
        pod_registry.update_pod(pod["id"], patch)
    except pod_registry.PodError as e:
        raise HTTPException(400, str(e))
    # 주소가 바뀌면 이전 서버에서 받아둔 모델/노드 목록은 더 이상 그 파드의 것이
    # 아니다. 캐시 키에 url이 들어 있어 자연히 미스가 나지만, 명시적으로 비워서
    # "바꾼 직후 잠깐 옛 목록이 보이는" 창을 없앤다.
    ComfyUIDriver.invalidate_capabilities(pod["id"])
    invalidate_comfy_status_cache()

    payload = comfy_endpoint_payload(user)
    effective_url = payload["effective_url"]
    if effective_url:
        payload["connected"] = await asyncio.to_thread(
            check_comfy_url, effective_url, COMFY_CHECK_TIMEOUT_INTERACTIVE
        )
    else:
        _, payload["connected"] = await asyncio.to_thread(resolve_comfy_url)
    return payload


@app.post("/api/comfy-endpoint/test")
async def test_comfy_endpoint(request: Request):
    # 저장하기 전에 "이 주소가 실제로 응답하는지"만 확인한다(설정은 건드리지 않음).
    # url을 비워서 보내면 지금 적용 중인 주소를 그대로 확인한다.
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    if not isinstance(data, dict):
        raise HTTPException(400, '{"url": "..."} 형태의 객체여야 해요.')
    user = me(request)
    try:
        url = pod_registry.normalize_pod_url(data.get("url", ""))
    except pod_registry.PodError as e:
        raise HTTPException(400, str(e))

    if not auth.is_admin(user):
        # 일반 회원이 입력한 주소로 이 서버가 대신 접속하므로 서버 안쪽 주소는 막는다.
        await asyncio.to_thread(_require_public_pod_url, url, pod_registry.DEFAULT_KIND)
    elif not url:
        url, _ = await asyncio.to_thread(resolve_comfy_url)
        if not url:
            return {"url": None, "connected": False, "detail": "확인할 주소가 없어요(자동 탐지도 실패)."}
    connected = await asyncio.to_thread(check_comfy_url, url, COMFY_CHECK_TIMEOUT_INTERACTIVE)
    return {"url": url, "connected": connected}


# ---- 파드(워커) 관리 ----------------------------------------------------------
# nightshift가 작업을 보낼 워커들의 목록(pod_registry.py). 지금은 파드 1개를 전제로
# 나머지 코드가 돌아가지만(기본 파드), 여기서 여러 개를 등록해 둘 수 있고 파드별 큐로
# 실제로 나눠 돌리는 것은 다음 단계다(multipod_plan.md의 P1).

def pod_payload(pod: dict) -> dict:
    """레코드 + 화면이 바로 쓸 수 있는 파생 정보(드라이버 이름, 실제로 쓰일 주소)."""
    try:
        driver = driver_for(pod)
    except DriverError:
        return {**pod, "kind_label": pod.get("kind"), "effective_url": None, "url_source": "unknown"}
    url, source = driver.resolve(pod)
    return {**pod, "kind_label": driver.label, "effective_url": url, "url_source": source}


def _require_public_pod_url(url: str, kind: str | None) -> None:
    """일반 회원의 파드 주소 검사 — 비어 있으면 안 되고(비면 이 서버 자신의 ComfyUI로 떨어진다) 서버 안쪽 주소도
    안 된다. 실패하면 HTTPException(400)."""
    url = (url or "").strip()
    if not url:
        raise HTTPException(400, "파드 주소를 적어주세요.")
    try:
        normalized = pod_registry.normalize_pod_url(url, kind or pod_registry.DEFAULT_KIND)
        pod_registry.assert_public_url(normalized)
    except pod_registry.PodError as e:
        raise HTTPException(400, str(e))


def _user_kinds(user: dict) -> list[dict]:
    """이 회원이 만들 수 있는 파드 종류 — 셸(서버에서 명령 실행)과 Claude 글쓰기(관리자 키 사용)는 관리자만."""
    kinds = driver_kinds()
    return kinds if auth.is_admin(user) else [k for k in kinds if k["kind"] == pod_registry.DEFAULT_KIND]


@app.get("/api/pods")
def list_pods_api(request: Request):
    user = me(request)
    pods = visible_pods_for(user)
    names = auth.usernames() if auth.is_admin(user) else {}
    default = default_pod_for_user(user)
    return {
        "pods": [{**pod_payload(p), "owner_name": names.get(p.get("owner_id"))} for p in pods],
        "default_pod_id": default["id"] if default else None,
        "kinds": _user_kinds(user),
    }


@app.post("/api/pods")
async def create_pod_api(request: Request):
    user = me(request)
    data = await read_json_object(request, allow_empty=False)
    data = {**data, "owner_id": user["id"]}   # 주인은 요청이 정하는 게 아니라 로그인한 회원이다
    kind = (data.get("kind") or pod_registry.DEFAULT_KIND).strip()
    try:
        driver = driver_for({"kind": kind})
    except DriverError as e:
        raise HTTPException(400, str(e))
    if not driver.available():
        raise HTTPException(400, driver.unavailable_reason())
    if not auth.is_admin(user):
        if kind != pod_registry.DEFAULT_KIND:
            raise HTTPException(403, "이 종류의 파드는 관리자만 만들 수 있어요.")
        await asyncio.to_thread(_require_public_pod_url, data.get("url"), kind)
    try:
        pod = pod_registry.create_pod(data)
    except pod_registry.PodError as e:
        raise HTTPException(400, str(e))
    ensure_runtime(pod)   # 큐와 워커 스레드를 바로 준비한다
    return pod_payload(pod)


@app.put("/api/pods/{pod_id}")
async def update_pod_api(pod_id: str, request: Request):
    # 부분 수정 — 보낸 필드만 바뀐다(이름만 바꾸려는 요청이 주소를 지우면 안 되므로).
    user = me(request)
    current = pod_or_404(user, pod_id)
    data = await read_json_object(request, allow_empty=False)
    data = {k: v for k, v in data.items() if k != "owner_id"}   # 주인은 바꿀 수 없다
    if not auth.is_admin(user):
        if data.get("kind") not in (None, pod_registry.DEFAULT_KIND):
            raise HTTPException(403, "이 종류의 파드는 관리자만 만들 수 있어요.")
        if "url" in data:
            await asyncio.to_thread(_require_public_pod_url, data.get("url"), current.get("kind"))
    try:
        pod = pod_registry.update_pod(pod_id, data)
    except pod_registry.PodError as e:
        raise HTTPException(400 if "없는 파드" not in str(e) else 404, str(e))
    # 주소나 종류가 바뀌었으면 그 파드에 대해 캐싱해 둔 것들은 더 이상 유효하지 않다.
    ComfyUIDriver.invalidate_capabilities(pod_id)
    invalidate_comfy_status_cache(pod_id)
    ensure_runtime(pod)   # max_concurrent를 늘렸으면 워커 스레드를 더 띄운다
    return pod_payload(pod)


@app.delete("/api/pods/{pod_id}")
def delete_pod_api(pod_id: str, request: Request):
    user = me(request)
    target = pod_or_404(user, pod_id)
    # 돌고 있는 작업이 있으면 막는다 — 지우는 순간 그 작업이 어디에도 속하지 않게 되고,
    # 서브프로세스만 남아 결과를 아무도 회수하지 않는다. 먼저 멈추게 한다.
    with lock:
        rt = pod_runtimes.get(pod_id)
        busy = list(rt.running) if rt else []
    if busy:
        raise HTTPException(400, f"이 파드에서 작업 {len(busy)}개가 실행 중이에요. 먼저 멈춰주세요.")
    # 이 파드에 배정돼 있던(아직 안 끝난) 작업은 같은 주인의 다른 파드로 되돌린다 — 갈 곳 없는
    # 작업이 영영 안 도는 상태로 남으면 안 되므로. 갈 파드가 없으면 지우지 못하게 막는다.
    others = [p for p in pod_registry.list_pods(target.get("owner_id")) if p["id"] != pod_id]
    fallback = next((p for p in others if p.get("enabled")), others[0] if others else None)
    with lock:
        waiting_jobs = any(j.get("pod_id") == pod_id and j["status"] in ("pending", "queued") and not j.get("deleted")
                           for j in jobs.values())
    if waiting_jobs and fallback is None:
        raise HTTPException(400, "이 파드에 대기 중인 작업이 있어요. 다른 파드를 먼저 추가하거나 작업을 지워주세요.")
    try:
        removed = pod_registry.delete_pod(pod_id)
    except pod_registry.PodError as e:
        raise HTTPException(404 if "없는 파드" in str(e) else 400, str(e))
    with lock:
        moved = []
        for job in jobs.values():
            if job.get("pod_id") == pod_id and job["status"] in ("pending", "queued") and fallback is not None:
                job["pod_id"] = fallback["id"]
                if job["status"] == "queued":
                    job["status"] = "pending"   # 이 파드의 큐와 함께 사라졌으므로
                    set_comfy_wait_flag(job, False)
                moved.append(job["id"])
    if moved:
        save_state()
    ComfyUIDriver.invalidate_capabilities(pod_id)
    invalidate_comfy_status_cache(pod_id)
    sync_runtimes()
    return {"deleted": removed["id"], "name": removed["name"],
            "moved_jobs": len(moved), "moved_to": fallback["id"] if fallback else None}


# 드라이버가 카드에 실어 주는 파드별 추가 정보(ComfyUI라면 GPU/VRAM). 연결 상태와 달리
# 이건 매번 필요하지도 않고 왕복이 한 번 더 드니, 더 길게 캐싱하고 백그라운드로만 갱신한다.
POD_CARD_TTL_SEC = 20
_pod_card_cache: dict[str, dict] = {}
_pod_card_lock = threading.Lock()
_pod_card_refreshing: set[str] = set()


def _refresh_pod_card_bg(pod: dict):
    pod_id = pod["id"]
    try:
        data = driver_for(pod).card(pod)
    except Exception:
        data = {}
    with _pod_card_lock:
        _pod_card_cache[pod_id] = {"data": data, "at": time.monotonic()}
        _pod_card_refreshing.discard(pod_id)


def pod_card_data(pod: dict) -> dict:
    """캐시된 값을 즉시 돌려주고, 오래됐으면 백그라운드로 다시 받아온다. 처음에는
    빈 dict가 나가고 다음 폴링에서 채워진다 — 카드가 뜨는 걸 막지 않는 게 우선이다."""
    pod_id = pod["id"]
    with _pod_card_lock:
        entry = _pod_card_cache.get(pod_id)
        fresh = entry and (time.monotonic() - entry["at"]) < POD_CARD_TTL_SEC
        if not fresh and pod_id not in _pod_card_refreshing:
            _pod_card_refreshing.add(pod_id)
            start = True
        else:
            start = False
    if start:
        threading.Thread(target=_refresh_pod_card_bg, args=(pod,), daemon=True).start()
    return (entry or {}).get("data") or {}


def recent_job_images(job_ids: list[str], limit: int = 3) -> list[str]:
    """그 작업들이 만든 결과 이미지 중 최근 것 몇 장의 상대 경로(갤러리 썸네일 API에
    그대로 넣을 수 있는 형태). 출력 폴더 전체를 훑지 않고 job_id 폴더만 들여다본다 —
    대시보드는 몇 초마다 폴링하므로 값싸야 한다."""
    base = Path(OUTPUT_DIR)
    names: list[str] = []
    for job_id in job_ids:
        folder = base / job_id
        try:
            entries = sorted(
                (e for e in os.scandir(folder)
                 if e.is_file() and Path(e.name).suffix.lower() in IMAGE_EXTENSIONS),
                key=lambda e: e.stat().st_mtime, reverse=True,
            )
        except OSError:
            continue
        for e in entries:
            names.append(f"{job_id}/{e.name}")
            if len(names) >= limit:
                return names
    return names


@app.get("/api/pods/summary")
async def pods_summary(request: Request):
    """대시보드가 폴링할 파드별 요약 — 레코드 + 연결 상태(캐시) + 큐/실행 상태 +
    오늘 만든 이미지 수. 값싼 것만 담는다: `/system_stats` 같은 파드별 추가 조회는
    캐시를 따로 붙여 대시보드 화면(P2)에서 넣는다."""
    user = me(request)
    pods = visible_pods_for(user)
    owner_names = auth.usernames() if auth.is_admin(user) else {}
    statuses = {}
    for pod in pods:
        statuses[pod["id"]] = await pod_status(pod)

    today = datetime.now(timezone.utc).date().isoformat()
    with lock:
        rows = []
        for pod in pods:
            rt = pod_runtimes.get(pod["id"])
            pod_jobs = [j for j in jobs.values()
                        if j.get("pod_id") == pod["id"] and not j.get("deleted")]
            running_jobs = [
                {"id": j["id"], "template_label": j.get("template_label"),
                 "progress": j.get("progress"), "started_at": j.get("started_at")}
                for j in pod_jobs if j["status"] == "running"
            ]
            waiting = sum(1 for j in pod_jobs if j.get("waiting_for_comfy"))
            done_today = sum(
                (j.get("progress") or {}).get("done") or 0
                for j in pod_jobs
                if (j.get("finished_at") or "").startswith(today)
            )
            finished = sorted(
                (j for j in pod_jobs if j["status"] in ("done", "failed", "interrupted")),
                key=lambda j: j.get("finished_at") or "", reverse=True,
            )
            recent_job_ids = [j["id"] for j in ([*(j for j in pod_jobs if j["status"] == "running")]
                                                + finished)[:3]]
            rows.append({
                **pod_payload(pod),   # 레코드 + kind_label/effective_url 같은 파생 정보
                "owner_name": owner_names.get(pod.get("owner_id")),
                "status": statuses[pod["id"]],
                "card": pod_card_data(pod),
                "recent_images": recent_job_images(recent_job_ids),
                "auto_run": bool(rt and rt.auto_run),
                "queue_len": rt.queue.qsize() if rt else 0,
                "running_jobs": running_jobs,
                "waiting_for_pod": waiting,
                "pending_count": sum(1 for j in pod_jobs if j["status"] == "pending"),
                "images_today": done_today,
            })
    totals = {
        "pods": len(rows),
        "online": sum(1 for r in rows if r["status"]["connected"]),
        "running_jobs": sum(len(r["running_jobs"]) for r in rows),
        "queued": sum(r["queue_len"] for r in rows),
        "pending": sum(r["pending_count"] for r in rows),
        "images_today": sum(r["images_today"] for r in rows),
    }
    default = default_pod_for_user(user)
    return {"pods": rows, "default_pod_id": default["id"] if default else None, "totals": totals}


@app.post("/api/pods/{pod_id}/test")
async def test_pod_api(pod_id: str, request: Request):
    """저장된 그대로의 파드가 실제로 응답하는지 확인한다(설정은 건드리지 않음).
    사람이 버튼을 누르고 기다리는 중이므로 폴링보다 넉넉한 타임아웃을 쓴다."""
    pod = pod_or_404(me(request), pod_id)
    try:
        driver = driver_for(pod)
    except DriverError as e:
        raise HTTPException(400, str(e))
    health = await asyncio.to_thread(driver.health, pod, COMFY_CHECK_TIMEOUT_INTERACTIVE)
    return {"pod_id": pod_id, **health}


@app.post("/api/pods/{pod_id}/runpod/{action}")
async def runpod_power(pod_id: str, action: str, request: Request):
    """파드 카드의 "RunPod 켜기/끄기" — 그 파드 주소에 박힌 RunPod pod를 RunPod API로 켜고 끈 뒤,
    파드 목록을 RunPod와 맞춘다(runpod_sync — 자동 등록 파드의 사용 여부·사용 내역 기록).
    돈이 드는 동작이라 관리자만. 끌 때는 그 파드에서 도는 작업이 있으면 막는다(작업이 중간에 죽으므로)."""
    user = admin_only(request)
    if action not in runpod_api.POD_ACTIONS:
        raise HTTPException(404, "알 수 없는 동작이에요.")
    pod = pod_or_404(user, pod_id)
    rp_id = runpod_api.extract_pod_id(pod.get("url") or "")
    if not rp_id:
        raise HTTPException(400, "RunPod 파드 주소가 아니에요(https://{POD_ID}-{PORT}.proxy.runpod.net).")
    if action == "stop" and _pod_has_running_job(pod_id):
        raise HTTPException(409, "이 파드에서 실행 중인 작업이 있어요. 작업을 먼저 멈춘 뒤 꺼 주세요.")
    status, error = await asyncio.to_thread(runpod_api.pod_action, rp_id, action)
    if error:
        raise HTTPException(502, error)
    if action == "start" and not pod.get("enabled"):
        # 사람이 일부러 켠 파드는 nightshift에서도 쓰는 게 당연하다(자동 등록 파드는 아래 동기화가 켠다).
        pod_registry.update_pod(pod_id, {"enabled": True})
    try:
        await asyncio.to_thread(runpod_sync.sync_runpod_pods, user["id"], False, ensure_runtime, _pod_has_running_job)
    except Exception:
        logging.getLogger("uvicorn.error").exception("RunPod 켜기/끄기 뒤 동기화 실패")
    with _pod_card_lock:
        _pod_card_cache.pop(pod_id, None)
    invalidate_comfy_status_cache(pod_id)
    ComfyUIDriver.invalidate_capabilities(pod_id)
    return {"ok": True, "pod_id": pod_id, "runpod_pod_id": rp_id, "status": status}


@app.post("/api/pods/{pod_id}/runpod-test")
async def test_pod_runpod_api(pod_id: str, request: Request):
    """카드에 뜨는 RunPod 메타데이터(card()의 get_runpod_info())는 실패를 전부 조용히
    삼키므로, "왜 안 뜨는지"를 직접 확인하고 싶을 때 이 엔드포인트로 캐시 없이 다시
    조회해 실패 이유(HTTP 코드/응답 본문/키 미설정 등)를 그대로 돌려준다. 관리자의 RunPod 키로 조회하므로 관리자만."""
    admin_only(request)
    pod = pod_or_404(me(request), pod_id)
    return await asyncio.to_thread(runpod_api.debug_probe, pod.get("url") or "")


def _pod_has_running_job(pod_id: str) -> bool:
    """그 파드에서 지금 실행 중인 작업이 있나 — runpod_sync가 auto 파드를 끄기 전에
    확인한다(돌고 있는 작업의 서브프로세스를 갑자기 고아로 만들면 안 되므로)."""
    with lock:
        return any(j.get("pod_id") == pod_id and j["status"] == "running" for j in jobs.values())


@app.post("/api/pods/sync-runpod")
async def sync_runpod_pods_api(request: Request):
    """RunPod에서 RUNNING인 ComfyUI pod를 찾아 파드 목록에 자동으로 등록/정리한다
    (runpod_sync.py). "사람 대신 도는 자동화"라 소유자를 호출한 관리자로 고정하고,
    셸 파드처럼 관리자만 쓸 수 있다."""
    user = admin_only(request)
    body = await read_json_object(request, allow_empty=True)
    result = await asyncio.to_thread(
        runpod_sync.sync_runpod_pods, user["id"], bool(body.get("dry_run")),
        ensure_runtime, _pod_has_running_job)
    if not result["dry_run"]:
        for item in result["added"] + result["reenabled"] + result["disabled"]:
            pod_id = item.get("id")
            if pod_id:
                invalidate_comfy_status_cache(pod_id)
                ComfyUIDriver.invalidate_capabilities(pod_id)
    return result


@app.get("/api/runpod-sessions")
def get_runpod_sessions(request: Request):
    """DB 탭 — RunPod 세션(사용 내역) 로그(runpod_sessions.py). 비용 정보라 회원
    관리와 같은 기준으로 관리자만 볼 수 있다."""
    admin_only(request)
    return {"sessions": runpod_sessions.list_sessions()}


@app.get("/api/git-log")
def get_git_log(request: Request):
    """DB 탭 — 업데이트 내역(git_log.py). nightshift 저장소의 git 커밋 로그를 그대로
    보여준다 — 따로 기록하는 동작이 없다(커밋 메시지가 곧 기록)."""
    admin_only(request)
    return {"commits": git_log.list_commits(REPO_ROOT)}


@app.get("/api/generation-log")
def get_generation_log(request: Request):
    """DB 탭 — 생성 정보(프롬프트 등) 기록(asset_meta.list_generation_log). nightshift
    큐로 만든 것과 RunPod의 ComfyUI를 직접 써서 만든 뒤 "결과 가져오기"로 받은 것 모두
    파일에 박힌 메타를 assets_index.sync()가 이미 읽어 뒀으므로(server/assets_index.py)
    여기서도 따로 기록하는 동작이 없다 — 있는 값을 최신순으로 보여줄 뿐이다."""
    admin_only(request)
    rows = asset_meta.list_generation_log(limit=300)
    for r in rows:
        pod_name = r.get("job_pod_name")
        if not pod_name and r.get("origin_pod_id"):
            pod = pod_registry.get_pod(r["origin_pod_id"])
            pod_name = pod["name"] if pod else r["origin_pod_id"]
        r["pod_name"] = pod_name
    return {"assets": rows}


def _runpod_sync_loop():
    """RUNPOD_SYNC_INTERVAL_SEC(0보다 클 때만 켜짐, sync_runpod_pods_api 옆의
    startup 코드가 이 스레드를 띄운다)마다 관리자 권한으로 자동 동기화를 돈다 —
    사람이 MCP로 sync_runpod_pods를 안 불러도 "RunPod에서 pod를 켜면 몇 분 안에
    나타난다"가 되게 하려는 것. 관리자가 아직 없으면(초기 설치 단계) 조용히
    건너뛴다. 실패해도 로그만 남기고 다음 주기에 다시 시도한다."""
    while True:
        time.sleep(RUNPOD_SYNC_INTERVAL_SEC)
        try:
            admin_id = auth.admin_id()
            if admin_id is not None:
                runpod_sync.sync_runpod_pods(admin_id, False, ensure_runtime, _pod_has_running_job)
        except Exception:
            logging.getLogger("uvicorn.error").exception("runpod 자동 동기화 실패")


@app.get("/api/comfy-object-info")
async def comfy_object_info(refresh: bool = False, pod_id: str | None = None):
    # 지금 연결된 ComfyUI에 설치된 노드 타입 이름들과 종류별 모델 목록만 추려서
    # 돌려준다(원본 /object_info는 입력 스펙까지 들어있어 수 MB가 되기도 해서
    # 그대로 브라우저로 넘기지 않는다). ComfyUI가 안 떠 있어도 에러가 아니라
    # connected=false + 빈 목록 — 화면에서 "연결 안 됨"으로 안내만 하면 되니까.
    try:
        comfy_url, object_info = await asyncio.to_thread(
            fetch_comfy_object_info, refresh, object_info_pod(pod_id))
    except Exception as e:
        raise HTTPException(502, f"ComfyUI 노드 목록을 가져오지 못했어요: {e}")
    # 파드가 없거나 꺼져 있어도 작업을 구상할 수 있게, 모델 등록부에 적힌 파일 이름도 종류별로 함께 준다.
    catalog: dict[str, list[str]] = {key: [] for key in MODEL_LIST_SOURCES}
    for entry in model_registry.list_entries():
        if entry["kind"] in catalog:
            catalog[entry["kind"]].append(entry["filename"])
    if object_info is None:
        return {
            "connected": False,
            "url": comfy_url,
            "node_types": [],
            "models": {key: [] for key in MODEL_LIST_SOURCES},
            "catalog": catalog,
        }
    return {
        "connected": True,
        "url": comfy_url,
        "catalog": catalog,
        "node_types": sorted(object_info.keys()),
        "models": {
            key: combo_choices(object_info, class_type, field)
            for key, (class_type, field) in MODEL_LIST_SOURCES.items()
        },
        # 워크플로우 빌더의 샘플러/스케줄러 드롭다운용 — 설치된 ComfyUI 버전이 실제로
        # 지원하는 값만 고르게 한다(버전마다 목록이 조금씩 다르다).
        "samplers": combo_choices(object_info, "KSampler", "sampler_name"),
        "schedulers": combo_choices(object_info, "KSampler", "scheduler"),
    }


@app.get("/api/lora-triggers")
def get_lora_triggers():
    return model_registry.lora_triggers()


@app.get("/api/models/inventory")
async def model_inventory(request: Request, refresh: bool = False):
    """내 ComfyUI 파드마다 설치된 모델 목록 — 파드 간 비교용. 꺼져 있는 파드는 connected=false."""
    user = me(request)
    pods = [p for p in visible_pods_for(user) if p.get("kind") == pod_registry.DEFAULT_KIND]

    def one(pod):
        entry = {"id": pod["id"], "name": pod.get("name") or pod["id"], "enabled": bool(pod.get("enabled")),
                 "connected": False, "models": {}}
        if not pod.get("enabled"):
            return entry
        try:
            _, info = fetch_comfy_object_info(refresh, pod)
        except Exception:
            info = None
        if info is not None:
            entry["connected"] = True
            entry["models"] = {k: combo_choices(info, *src) for k, src in MODEL_LIST_SOURCES.items()}
        return entry

    results = await asyncio.gather(*[asyncio.to_thread(one, p) for p in pods])
    return {"pods": list(results)}


@app.get("/api/models/usage")
def model_usage_api(request: Request):
    """모델별 사용 통계 — 내 결과물(관리자는 전체)에 박힌 메타를 세어서 돌려준다."""
    _sync_assets_quietly()
    return {"usage": asset_meta.model_usage(owner_id=auth.owner_scope(me(request)))}


# ---- 모델 내려받기 (model_download.py) ----------------------------------------------

def _download_pod(user: dict, pod_id: str) -> dict:
    pod = pod_or_404(user, pod_id)
    if pod.get("kind") != pod_registry.DEFAULT_KIND:
        raise HTTPException(400, "ComfyUI 파드만 모델을 받을 수 있어요.")
    return pod


def _download_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except model_download.DownloadError as e:
        raise HTTPException(e.status, str(e))


_downloads_seen_done: set = set()


@app.post("/api/models/resolve")
async def resolve_model_source(request: Request):
    me(request)
    data = await read_json_object(request, allow_empty=False)
    token = str(data.get("token") or "").strip() or None
    return await asyncio.to_thread(_download_call, model_download.resolve, str(data.get("url") or ""), token)


@app.get("/api/models/downloader/status")
async def downloader_status(request: Request, pod_id: str):
    pod = _download_pod(me(request), pod_id)
    return await asyncio.to_thread(model_download.node_status, pod)


@app.get("/api/models/downloader/install-script")
def downloader_install_script(request: Request, pod_id: str):
    pod = _download_pod(me(request), pod_id)
    return Response(model_download.install_script(pod["id"]), media_type="text/plain; charset=utf-8",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/models/download")
async def start_model_download(request: Request):
    user = me(request)
    data = await read_json_object(request, allow_empty=False)
    pod = _download_pod(user, str(data.get("pod_id") or ""))
    url = str(data.get("url") or "").strip()
    kind = str(data.get("kind") or "")
    filename = str(data.get("filename") or "").strip()
    if kind not in model_registry.KIND_IDS:
        raise HTTPException(400, "모델 종류를 골라주세요.")
    if not url or not filename:
        raise HTTPException(400, "주소와 파일명이 필요해요.")
    token = str(data.get("token") or "").strip() or None
    body = {"url": url, "folder": kind, "filename": filename, "overwrite": bool(data.get("overwrite")),
            "headers": model_download.auth_header_for(url, token)}
    result = await asyncio.to_thread(_download_call, model_download.call_node, pod, "POST", "/nightshift/dl/start", body)
    # 등록부는 관리자만 고칠 수 있다 — 관리자가 받을 때만 Civitai/HF에서 알아낸 정보를 함께 적어 둔다.
    meta = data.get("meta")
    if auth.is_admin(user) and isinstance(meta, dict):
        fields = {k: meta[k] for k in model_registry.FIELDS if k in meta}
        try:
            model_registry.upsert(kind, filename, fields)
        except model_registry.RegistryError:
            pass
    return result


@app.get("/api/models/downloads")
async def list_model_downloads(request: Request, pod_id: str):
    pod = _download_pod(me(request), pod_id)
    result = await asyncio.to_thread(_download_call, model_download.call_node, pod, "GET", "/nightshift/dl/status")
    for item in result.get("downloads", []):
        key = (pod["id"], item.get("id"))
        if item.get("status") == "done" and key not in _downloads_seen_done:
            _downloads_seen_done.add(key)
            model_download.ComfyUIDriver.invalidate_capabilities(pod["id"])   # 새 파일이 설치 목록에 바로 보이게
    return result


@app.post("/api/models/downloads/cancel")
async def cancel_model_download(request: Request):
    user = me(request)
    data = await read_json_object(request, allow_empty=False)
    pod = _download_pod(user, str(data.get("pod_id") or ""))
    return await asyncio.to_thread(_download_call, model_download.call_node, pod, "POST",
                                   "/nightshift/dl/cancel", {"id": str(data.get("id") or "")})


@app.get("/api/models")
def get_model_registry():
    """모델 등록부 — 파일 종류 목록과 지금까지 정보를 적어 둔 항목들. 어느 파드에 뭐가 설치돼
    있는지는 /api/comfy-object-info가 알려 주고, 화면이 둘을 파일명으로 합친다."""
    return {
        "kinds": [{"id": k, "label": label} for k, label in model_registry.KINDS],
        "base_models": model_registry.BASE_MODELS,
        "items": model_registry.list_entries(),
    }


@app.put("/api/models")
async def put_model_registry_entry(request: Request):
    admin_only(request)
    try:
        data = json.loads((await request.body()).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    if not isinstance(data, dict):
        raise HTTPException(400, "{kind, filename, ...} 형태의 객체여야 해요.")
    fields = {k: data[k] for k in model_registry.FIELDS if k in data}
    try:
        entry = model_registry.upsert(str(data.get("kind") or ""), str(data.get("filename") or ""), fields)
    except model_registry.RegistryError as e:
        raise HTTPException(400, str(e))
    return {"entry": entry}


@app.get("/api/models/download-command")
def model_download_command(request: Request, kind: str, filename: str):
    # RunPod 자체 터미널에 붙여넣을 curl 명령 한 줄 — model_download.py의 파드 내
    # 다운로더 노드 인프라와는 별개다(그건 프론트에서 아직 안 쓴다). 명령에 civitai
    # 토큰이 그대로 드러나므로 등록 정보 수정과 같은 기준으로 관리자만 쓸 수 있다.
    admin_only(request)
    entry = model_registry.get_entry(kind, filename)
    if not entry or not entry.get("download_url"):
        raise HTTPException(400, "이 모델에는 다운로드 주소가 없어요.")
    url = entry["download_url"]
    # kind가 곧 ComfyUI의 실제 모델 폴더 이름이라 그대로 받을 폴더가 된다. filename에
    # "/"가 있으면 그 앞부분은 kind 폴더 밑의 하위 폴더(ComfyUI 로더들이 combo 값에
    # 쓰는 "하위폴더/이름" 표기와 같은 규칙), 없으면 kind 폴더 바로 밑이다.
    sub, base = posixpath.split(filename)
    target = f"/workspace/shared_models/{kind}" + (f"/{sub}" if sub else "") + f"/{base}"
    header = ""
    warning = None
    if "civitai" in (urllib.parse.urlparse(url).hostname or ""):
        token = os.environ.get("CIVITAI_TOKEN", "").strip()
        if token:
            header = f" -H {shlex.quote('Authorization: Bearer ' + token)}"
        else:
            warning = "CIVITAI_TOKEN이 .env에 없어요 — 토큰 없이 받아지는 파일만 될 거예요."
    command = f"curl -L --create-dirs{header} -o {shlex.quote(target)} {shlex.quote(url)}"
    return {"command": command, "target": target, "warning": warning}


@app.get("/api/input-images")
def get_input_images():
    # img2img/USDU 워크플로우 유형과 영상 생성(WAN2.2) 템플릿의 "입력 이미지"
    # 선택 드롭다운을 채우는 데 쓴다. ref_assets의 pose/depth/lineart와 달리
    # 세트/char_no 구분이 없는 평평한 목록(input_assets.py 모듈 설명 참고).
    return {"images": list_input_images()}


@app.post("/api/input-images")
async def upload_input_image(request: Request):
    # "이미지 선택 — 업로드" 경로(영상 생성 작업 화면, img2img "입력 이미지" 필드
    # 둘 다 이 풀을 공유한다). 파일 하나만 받는다 — 여러 장을 한 번에 올릴 일이
    # 없어서(그때그때 하나씩 골라 쓰는 용도) 단순하게 뒀다.
    form = await request.form()
    file = form.get("image")
    if not isinstance(file, UploadFile) or not file.filename:
        raise HTTPException(400, "이미지 파일을 첨부하세요.")
    content = await file.read()
    try:
        name = await asyncio.to_thread(save_input_image, file.filename, content)
    except InputAssetError as e:
        raise HTTPException(400, str(e))
    return {"name": name}


@app.delete("/api/input-images/{name}")
def delete_input_image_api(name: str):
    try:
        delete_input_image(name)
    except InputAssetError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@app.get("/api/input-images/{name}/raw")
def get_input_image_raw(name: str):
    # 참조 슬롯 카드 갤러리의 <img>가 직접 가리키는 주소 — 원본을 그대로 내려준다.
    # 입력 이미지는 보통 크지 않아 별도 축소본 생성 없이 원본으로 충분하다.
    try:
        path = resolve_input_image(name)
    except InputAssetError as e:
        raise HTTPException(404, str(e))
    return FileResponse(path)


# MiniMax-H3 r2v의 비디오/오디오 참조 선택 드롭다운을 채운다 — 이미지 풀과 같은 디렉토리를
# 공유하고 확장자로만 구분한다(input_assets.py 모듈 설명 참고). 결과 갤러리에서 가져오기는
# 참조용 원본이 보통 사용자 컴퓨터에 있어 두지 않았다(이미지의 import-from-output과 다름).
@app.get("/api/input-videos")
def get_input_videos():
    return {"videos": list_input_videos()}


@app.post("/api/input-videos")
async def upload_input_video(request: Request):
    form = await request.form()
    file = form.get("video")
    if not isinstance(file, UploadFile) or not file.filename:
        raise HTTPException(400, "비디오 파일을 첨부하세요.")
    content = await file.read()
    try:
        name = await asyncio.to_thread(save_input_video, file.filename, content)
    except InputAssetError as e:
        raise HTTPException(400, str(e))
    return {"name": name}


@app.delete("/api/input-videos/{name}")
def delete_input_video_api(name: str):
    try:
        delete_input_video(name)
    except InputAssetError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@app.get("/api/input-videos/{name}/raw")
def get_input_video_raw(name: str):
    # 참조 슬롯 카드 갤러리의 <video>가 직접 가리키는 주소 — 서버에서 썸네일을 만들지
    # 않고 원본을 그대로 내려주면, 브라우저가 preload="metadata"로 첫 프레임을 알아서
    # 그린다(ffmpeg 등 서버 쪽 의존성을 늘리지 않으려는 선택).
    try:
        path = resolve_input_video(name)
    except InputAssetError as e:
        raise HTTPException(404, str(e))
    return FileResponse(path)


@app.get("/api/input-audios")
def get_input_audios():
    return {"audios": list_input_audios()}


@app.post("/api/input-audios")
async def upload_input_audio(request: Request):
    form = await request.form()
    file = form.get("audio")
    if not isinstance(file, UploadFile) or not file.filename:
        raise HTTPException(400, "오디오 파일을 첨부하세요.")
    content = await file.read()
    try:
        name = await asyncio.to_thread(save_input_audio, file.filename, content)
    except InputAssetError as e:
        raise HTTPException(400, str(e))
    return {"name": name}


@app.delete("/api/input-audios/{name}")
def delete_input_audio_api(name: str):
    try:
        delete_input_audio(name)
    except InputAssetError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@app.post("/api/input-images/import-from-output")
async def import_output_images_to_input_pool(request: Request):
    # "이미지 선택 — 갤러리에서 선택" 경로 — 결과 이미지 갤러리에서 고른 이미지를
    # 입력 이미지 풀로 사본을 만든다. /api/assets/import-from-output(참조 세트로
    # 보내기)과 같은 패턴: 원본은 지우지 않고 복사만 하며, 그 사이 지워진 이미지는
    # 조용히 건너뛴다.
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    names = parse_image_names_body(data)

    added = []
    skipped = []
    for name in names:
        try:
            path = resolve_output_image(name)
        except HTTPException as e:
            skipped.append({"name": name, "reason": e.detail})
            continue
        # job_id별 하위 폴더에서 온 이름이라, 평평한 입력 이미지 풀에 맞게
        # "<job_id>_<원본파일명>"으로 합친다(참조 세트 가져오기와 같은 규칙).
        parts = name.split("/")
        dest_filename = f"{parts[0]}_{parts[-1]}" if len(parts) > 1 else parts[0]
        try:
            content = await asyncio.to_thread(path.read_bytes)
            # 같은 이미지로 영상을 여러 번 만들 때마다 _2, _3 사본이 쌓이지 않게, 이미 같은 이름·같은
            # 내용의 파일이 풀에 있으면 그걸 그대로 쓴다.
            try:
                existing = resolve_input_image(dest_filename)
                same = await asyncio.to_thread(existing.read_bytes) == content
            except InputAssetError:
                same = False
            stored_name = dest_filename if same else await asyncio.to_thread(save_input_image, dest_filename, content)
        except (OSError, InputAssetError) as e:
            skipped.append({"name": name, "reason": f"저장 실패: {e}"})
            continue
        added.append(stored_name)

    return {"added": added, "skipped": skipped}


@app.get("/api/base-model-families")
async def get_base_model_families(request: Request, pod_id: str | None = None):
    """새 작업 마법사 1단계 — 모델 등록부에서 base_model이 적힌 체크포인트를 그 값으로 묶어 준다.
    {id: {label, checkpoints}}: id는 base_model 값의 식별자(워크플로우 프리셋 파일 이름과 LoRA 걸러내기에 쓴다).
    쓸 수 있는 것만 보이게, 지금 들어가 있는 파드(없으면 내 연결된 파드 전체)에 설치된 체크포인트로 좁힌다. 파드에
    연결하지 못하면 좁힐 기준이 없으니 등록된 것을 전부 보여 준다."""
    user = me(request)
    if pod_id == "auto":   # 파드를 정하지 않고 구상하는 중 — 등록해 둔 체크포인트를 전부 보여 준다(배정은 스케줄러가 한다)
        return model_registry.checkpoint_groups(None)
    pods = [object_info_pod(pod_id)] if pod_id else [
        p for p in visible_pods_for(user) if p.get("kind") == pod_registry.DEFAULT_KIND and p.get("enabled")]
    installed: set[str] | None = None

    def installed_of(pod):
        try:
            _, info = fetch_comfy_object_info(False, pod)
        except Exception:
            return None
        if info is None:
            return None
        # 체크포인트(CheckpointLoaderSimple)와 디퓨전 모델(UNETLoader, krea.2/MiniMax-H3처럼
        # 체크포인트가 없는 family) 둘 다의 설치 목록을 합쳐야 두 kind의 family가 모두 걸러진다.
        return (set(combo_choices(info, *MODEL_LIST_SOURCES["checkpoints"]))
                | set(combo_choices(info, *MODEL_LIST_SOURCES["diffusion_models"])))

    results = await asyncio.gather(*[asyncio.to_thread(installed_of, p) for p in pods if not p.get("_none")])
    live = [r for r in results if r is not None]
    if live:
        installed = set().union(*live)
    return model_registry.checkpoint_groups(installed)


# "새 작업 추가" 마법사 2단계(워크플로우 유형)의 정적 카탈로그 — 세 그룹으로
# 나뉜다(workflow_builder.py의 조합 규칙과 정확히 대응):
#
#   base   — 첫 샘플링을 어디서 시작할지, 반드시 하나만 고른다(서로 배타적).
#            POST /api/build-workflow(workflow_builder.py)가 spec["base"]로 받는다.
#   post   — base 뒤에 이어 붙이는 후처리, 0개 이상 동시에 고를 수 있다(체이닝
#            가능 — 예: hires_fix+usdu를 같이 켜면 hires-fix 다음에 usdu가 실행됨).
#            spec["hires_fix"]/spec["usdu"]로 받는다.
#   preset — ControlNet/IPAdapter처럼 체크포인트마다 배선이 달라 이 서버가 자동
#            조립하지 못하는 유형. family별로 미리 올려둔 워크플로우(GET
#            /api/workflow-presets/{family}/{type})를 그대로 쓰므로, base/post와
#            동시에 쓸 수 없다(마법사가 이 배타 관계를 강제한다).
#
# template_ids는 이 유형(들)을 "시드 반복"/"CSV 순회" 중 어느 실행 방식으로 돌릴지에
# 따라 실제로 큐에 올릴 템플릿 id를 알려준다 — base 쪽만 갖고 있다(post는 base가
# 고른 템플릿을 그대로 쓴다: img2img가 필요로 하는 입력 이미지 주입은 base가
# img2img일 때만 필요하고, hires_fix/usdu는 워크플로우 안에서 완결되므로 별도
# 템플릿이 필요 없다 — 예전엔 usdu가 항상 외부 이미지를 요구하는 별도 유형이었지만,
# 이제는 base=txt2img+usdu처럼 입력 이미지 없이도 조합할 수 있다). requires_node가
# 있으면 그 클래스가 GET /api/comfy-object-info의 node_types에 없을 때 "설치 필요"
# 안내만 하고 선택 자체는 막지 않는다(화면에서 처리).
WORKFLOW_TYPES = {
    "base": [
        {
            "id": "txt2img", "label": "Text to Image",
            "template_ids": {"seed": "seed_batch", "csv": "csv_batch"},
        },
        {
            "id": "img2img", "label": "Image to Image", "requires_input_image": True,
            "template_ids": {"seed": "input_image_batch", "csv": "input_image_csv_batch"},
        },
        {
            # family_id가 있으면 그 family(base_id 기준)를 골랐을 때만 마법사 2단계에 보인다
            # (기존 txt2img/img2img는 family_id가 없어 전 family에 보임 — 그대로 유지).
            # architecture는 POST /api/build-workflow가 어느 빌더 모듈로 갈지 고르는 키다.
            "id": "krea2_t2i", "label": "Text to Image (krea.2)",
            "family_id": "krea.2", "architecture": "krea2",
            "template_ids": {"seed": "seed_batch", "csv": "csv_batch"},
        },
        {
            # checkpoint_match: MiniMax-H3에는 UNet 파일이 실제로 2개 있고(fl2va/ref2va),
            # 어느 워크플로우 유형을 쓸 수 있는지가 그 파일에 달려 있다(model_registry에는
            # 둘 다 base_model="MiniMax-H3"로 한 family에 묶여 있으므로 family_id만으로는
            # 못 가른다) — 마법사 1단계에서 고른 체크포인트 파일명에 이 부분 문자열이
            # 있어야만 2단계에 보인다(대소문자 무시, 프론트 renderWizardTypeModal 참고).
            "id": "minimax_h3_t2v", "label": "Text to Video (MiniMax-H3)",
            "family_id": "minimax-h3", "architecture": "minimax_h3_i2v", "checkpoint_match": "fl2va",
            "template_ids": {"seed": "minimax_h3_i2v_batch", "csv": "minimax_h3_i2v_csv_batch"},
        },
        {
            "id": "minimax_h3_i2v", "label": "First/Last Frame to Video (MiniMax-H3)",
            "family_id": "minimax-h3", "architecture": "minimax_h3_i2v", "checkpoint_match": "fl2va",
            "requires_input_image": True,
            "template_ids": {"seed": "minimax_h3_i2v_batch", "csv": "minimax_h3_i2v_csv_batch"},
        },
        {
            "id": "minimax_h3_r2v", "label": "Reference to Video (MiniMax-H3)",
            "family_id": "minimax-h3", "architecture": "minimax_h3_r2v", "checkpoint_match": "ref2va",
            "template_ids": {"seed": "minimax_h3_r2v_batch", "csv": "minimax_h3_r2v_csv_batch"},
        },
    ],
    "post": [
        # applies_to_base가 있으면 그 base를 골랐을 때만 보인다 — hires_fix/usdu는 SDXL식
        # (EmptyLatentImage+KSampler) 그래프 전용이라 krea2_t2i/minimax_h3_*에는 안 맞는다.
        {"id": "hires_fix", "label": "Hires Fix", "applies_to_base": ["txt2img", "img2img"]},
        {"id": "usdu", "label": "Ultimate SD Upscale", "requires_node": "UltimateSDUpscaleNoUpscale",
         "applies_to_base": ["txt2img", "img2img"]},
    ],
    "preset": [
        {
            "id": "openpose_cn", "label": "OpenPose ControlNet", "ref_kind": "pose",
            "template_ids": {"seed": "pose_batch", "csv": "pose_csv_batch"},
        },
        {
            "id": "depth_cn", "label": "Depth ControlNet", "ref_kind": "depth",
            "template_ids": {"seed": "depth_batch", "csv": "depth_csv_batch"},
        },
        {
            "id": "lineart_cn", "label": "Lineart ControlNet", "ref_kind": "lineart",
            "template_ids": {"seed": "lineart_batch", "csv": "lineart_csv_batch"},
        },
        {
            "id": "ipadapter", "label": "IPAdapter",
            "template_ids": {"seed": "ipadapter_batch", "csv": "ipadapter_csv_batch"},
        },
    ],
}


@app.get("/api/workflow-types")
def get_workflow_types():
    return WORKFLOW_TYPES


def preset_filename(family_id: str, type_id: str) -> str:
    for value, label in ((family_id, "family_id"), (type_id, "type_id")):
        if not value or not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise HTTPException(400, f"{label} 값이 올바르지 않아요 (영문/숫자/-/_/.만 가능).")
    return f"{family_id}__{type_id}.json"


@app.get("/api/workflow-presets")
def list_workflow_presets():
    # 마법사가 "이 family + 이 워크플로우 유형" 조합에 프리셋이 이미 있는지 한 번에
    # 확인할 수 있게, 저장된 모든 조합을 나열해서 돌려준다.
    presets = []
    for path in sorted(WORKFLOW_PRESETS_DIR.glob("*__*.json")):
        family_id, _, rest = path.stem.partition("__")
        if family_id and rest:
            presets.append({"family_id": family_id, "type_id": rest})
    return {"presets": presets}


@app.get("/api/workflow-presets/{family_id}/{type_id}")
def get_workflow_preset(family_id: str, type_id: str):
    path = WORKFLOW_PRESETS_DIR / preset_filename(family_id, type_id)
    if not path.exists():
        raise HTTPException(404, "해당 조합의 프리셋 워크플로우가 없어요.")
    return Response(content=path.read_text(encoding="utf-8"), media_type="application/json")


@app.get("/api/video-workflows/{name}")
def get_video_workflow(name: str):
    # 영상 생성 템플릿(wan22_i2v_batch 등)을 고르면 프론트엔드가 이걸 받아
    # 워크플로우 업로드 칸에 자동으로 채운다 — 사용자가 직접 파일을 고를 필요가
    # 없다. name은 고정된 화이트리스트라 경로 조작 걱정이 없다.
    if name not in VIDEO_WORKFLOW_NAMES:
        raise HTTPException(404, "해당 영상 워크플로우가 없어요.")
    path = VIDEO_WORKFLOWS_DIR / f"{name}.json"
    if not path.exists():
        raise HTTPException(404, "워크플로우 파일이 서버에 없어요.")
    return Response(content=path.read_text(encoding="utf-8"), media_type="application/json")


@app.put("/api/workflow-presets/{family_id}/{type_id}")
async def put_workflow_preset(family_id: str, type_id: str, request: Request):
    admin_only(request)
    # "🎛 LoRA" 탭(워크플로우 프리셋 관리 부분)이 워크플로우 JSON을 통째로 올려서
    # family+유형 조합 하나에 저장한다 — 업로드한 파일을 그대로 검증 없이 저장한다
    # (실제로 돌아가는지는 POST /api/validate-workflow를 별도로 안내하면 됨).
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    workflow = data.get("workflow") if isinstance(data, dict) and "workflow" in data else data
    if not isinstance(workflow, dict):
        raise HTTPException(400, "워크플로우 JSON(노드 id → 노드) 형식이 아니에요.")

    path = WORKFLOW_PRESETS_DIR / preset_filename(family_id, type_id)
    path.write_text(json.dumps(workflow, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "family_id": family_id, "type_id": type_id}


@app.delete("/api/workflow-presets/{family_id}/{type_id}")
def delete_workflow_preset(family_id: str, type_id: str, request: Request):
    admin_only(request)
    path = WORKFLOW_PRESETS_DIR / preset_filename(family_id, type_id)
    if not path.exists():
        raise HTTPException(404, "해당 조합의 프리셋 워크플로우가 없어요.")
    path.unlink()
    return {"ok": True}


@app.post("/api/build-workflow")
async def build_workflow_api(request: Request, pod_id: str | None = None):
    # "워크플로우 빌더" 탭 — 업로드 없이 스펙(체크포인트/LoRA/프롬프트/샘플러/해상도)
    # 만으로 워크플로우 JSON을 만들어 돌려준다. 만들어진 JSON은 화면에서 작업 관리
    # 탭의 워크플로우 슬롯에 그대로 채워지고, 그다음은 업로드한 파일과 완전히 같은
    # 경로(POST /api/upload)를 탄다 — 여기서 큐에 직접 넣지 않는 이유다.
    body = await request.body()
    try:
        spec = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    if not isinstance(spec, dict):
        raise HTTPException(400, "스펙이 JSON 객체가 아니에요.")

    try:
        _, object_info = await asyncio.to_thread(
            fetch_comfy_object_info, False, object_info_pod(pod_id))
    except Exception:
        object_info = None

    # ComfyUI가 떠 있으면 고른 값들이 실제로 설치/지원되는지 먼저 본다. 꺼져 있으면
    # 확인할 기준이 없으니 검증을 건너뛴다(다른 경로들과 같은 규칙).
    if object_info is not None:
        def require_installed(value: str, kind: str, label: str):
            if not value:
                return
            source = MODEL_LIST_SOURCES.get(kind)
            if source is None:
                return
            installed = combo_choices(object_info, *source)
            if installed and value not in installed:
                raise HTTPException(400, f"{label} '{value}'은(는) 지금 연결된 ComfyUI에 설치돼 있지 않아요.")

        architecture = str(spec.get("architecture") or "sdxl").strip()
        # krea.2/MiniMax-H3는 체크포인트가 아니라 UNETLoader(디퓨전 모델) 파일을 쓴다.
        checkpoint_kind = "checkpoints" if architecture == "sdxl" else "diffusion_models"
        # MiniMax-H3는 spec["checkpoint"](마법사 1단계에서 고른 파일 — family 표시/LoRA
        # 필터링용일 뿐)가 아니라 workflow_builder_minimax_h3.py가 정한 고정 UNet 파일을
        # 실제로 쓴다 — 그 파일이 설치돼 있는지를 확인해야 "설치돼 있다고 나왔는데 실행하면
        # 없다"는 불일치가 안 생긴다.
        minimax_required = {
            "minimax_h3_i2v": workflow_builder_minimax_h3.I2V_UNET_NAME,
            "minimax_h3_r2v": workflow_builder_minimax_h3.R2V_UNET_NAME,
        }.get(architecture)
        if minimax_required is not None:
            # 고른 UNet(파인튜닝 모델 포함)이 있으면 빌더가 그 파일을 쓰므로 그걸 확인한다.
            require_installed(workflow_builder_minimax_h3.unet_for(spec, minimax_required), checkpoint_kind, "체크포인트")
        else:
            require_installed(str(spec.get("checkpoint") or "").strip(), checkpoint_kind, "체크포인트")
        require_installed(str(spec.get("vae") or "").strip(), "vae", "VAE")
        for lora in (spec.get("loras") or []):
            if isinstance(lora, dict):
                require_installed(str(lora.get("name") or "").strip(), "loras", "LoRA")
        for field, label in (("sampler_name", "샘플러"), ("scheduler", "스케줄러")):
            value = str(spec.get(field) or "").strip()
            choices = combo_choices(object_info, "KSampler", field)
            if value and choices and value not in choices:
                raise HTTPException(400, f"{label} '{value}'은(는) 이 ComfyUI가 지원하지 않아요.")

    architecture = str(spec.get("architecture") or "sdxl").strip()
    try:
        if architecture == "sdxl":
            workflow = build_workflow(spec)
        elif architecture == "krea2":
            workflow = workflow_builder_krea2.build_workflow(spec)
        elif architecture == "minimax_h3_i2v":
            workflow = workflow_builder_minimax_h3.build_i2v_workflow(spec)
        elif architecture == "minimax_h3_r2v":
            workflow = workflow_builder_minimax_h3.build_r2v_workflow(spec)
        else:
            raise HTTPException(400, f"알 수 없는 architecture 값이에요: {architecture}")
    except WorkflowBuildError as e:
        raise HTTPException(400, str(e))
    return {"workflow": workflow}


@app.post("/api/validate-workflow")
async def validate_workflow(request: Request, pod_id: str | None = None):
    # 업로드하려는 워크플로우가 이 서버에서 돌아갈 수 있는지 미리 확인한다 —
    # 다른 ComfyUI 설치본에서 만든 워크플로우는 여기 없는 커스텀 노드를 쓰거나
    # 없는 체크포인트/LoRA 파일을 가리키기 쉬운데, 지금은 그걸 배치가 한참
    # 돌다가 실패해야 알 수 있다. 어디까지나 안내용이라 큐 등록 자체는 막지 않는다.
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    workflow = data.get("workflow") if isinstance(data, dict) and "workflow" in data else data
    if not isinstance(workflow, dict):
        raise HTTPException(400, "워크플로우 JSON(노드 id → 노드) 형식이 아니에요.")

    try:
        comfy_url, object_info = await asyncio.to_thread(
            fetch_comfy_object_info, False, object_info_pod(pod_id))
    except Exception as e:
        raise HTTPException(502, f"ComfyUI 노드 목록을 가져오지 못했어요: {e}")
    if object_info is None:
        # 연결이 안 됐으면 "문제 없음"이 아니라 "확인 못 함"이다 — 화면에서 구분해서 안내한다.
        return {
            "connected": False, "url": comfy_url, "ok": True, "auto": pod_id == "auto",
            "missing_nodes": [], "missing_values": [], "checked_nodes": 0,
        }

    missing_nodes, missing_values, checked = workflow_missing(object_info, workflow)
    if missing_values:
        user = me(request)
        pod_id_used = object_info_pod(pod_id).get("id")
        await asyncio.to_thread(annotate_available_on, missing_values, user, pod_id_used)
    return {
        "connected": True,
        "url": comfy_url,
        "ok": not missing_nodes and not missing_values,
        "missing_nodes": missing_nodes,
        "missing_values": missing_values,
        "checked_nodes": checked,
    }


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


def parse_ref_kind(raw: str | None, *, allow_none: bool = False) -> str:
    """요청의 kind 파라미터(pose/depth/lineart)를 검증한다. allow_none이면
    "none"도 허용한다(보조 참조를 안 쓰는 경우를 나타내는 값)."""
    choices = REF_KINDS + (("none",) if allow_none else ())
    if raw not in choices:
        raise HTTPException(400, f"kind는 {choices} 중 하나여야 해요.")
    return raw


@app.get("/api/assets")
def list_assets(kind: str = "pose"):
    # 업로드 폼의 "인물 수"/"세트" 캐스케이딩 드롭다운을 채우는 용도. kind(pose/
    # depth/lineart)별로 완전히 분리된 트리를 돌려준다 — char_no별 세트 목록을
    # 트리로 한 번에 돌려줘서, 프론트엔드가 "인물 수"를 바꿀 때마다 서버에 다시
    # 요청하지 않고도 "세트" 드롭다운을 그 자리에서 다시 채울 수 있게 한다. 매
    # 호출마다 폴더를 다시 스캔해서 방금 새로 올려둔 세트도 반영한다.
    kind = parse_ref_kind(kind)
    return {"char_nos": list_assets_tree(kind)}


@app.post("/api/assets/import-from-output")
async def import_output_images_to_ref_set(request: Request):
    # 갤러리에서 마음에 든 결과 이미지를 참조 세트(포즈/depth/lineart)로 보내는
    # 용도 — 지금까지는 이 세트들을 채우려면 서버 파일시스템에 직접 올려야
    # 했는데, 이 엔드포인트가 그 유일한 업로드 경로다. char_no/세트는 존재하지
    # 않으면(새 인물 수·새 세트) save_ref_image가 그대로 폴더를 만들어서, 이
    # 하나로 기존 세트 추가와 새 세트 생성을 둘 다 처리한다. 원본은 지우지
    # 않고 사본만 만든다(복사).
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")

    names = parse_image_names_body(data)
    kind = parse_ref_kind(data.get("kind", "pose"))
    try:
        char_no = validate_new_char_no(data.get("char_no"))
        ref_set = validate_new_folder_name(data.get("set_name"), "참조 세트")
    except RefAssetError as e:
        raise HTTPException(400, str(e))

    added = 0
    skipped = []
    for name in names:
        try:
            path = resolve_output_image(name)
        except HTTPException as e:
            skipped.append({"name": name, "reason": e.detail})
            continue

        # job_id별 하위 폴더(작업 정보 없으면 그대로)에서 온 이름이라, 참조 세트의
        # 평평한 구조에 맞게 "<job_id>_<원본파일명>"으로 합친다 — 어느 작업에서
        # 나온 참조인지 파일명만 보고 알 수 있게 하기 위함(output_images.py 참고).
        parts = name.split("/")
        dest_filename = f"{parts[0]}_{parts[-1]}" if len(parts) > 1 else parts[0]

        try:
            content = await asyncio.to_thread(path.read_bytes)
            await asyncio.to_thread(save_ref_image, kind, char_no, ref_set, dest_filename, content)
        except OSError as e:
            skipped.append({"name": name, "reason": f"저장 실패: {e}"})
            continue
        added += 1

    return {"added": added, "skipped": skipped}


def resolve_option_kind(option: dict, options_so_far: dict) -> str:
    """char_no/asset_folder 옵션이 참조할 종류(pose/depth/lineart/none)를 정한다.
    "kind"를 정적으로 선언했으면 그대로(주 참조), "kind_from"을 선언했으면
    (manifest에서 이 옵션보다 앞에 선언된) 다른 옵션이 고른 종류를 그대로
    따라간다(보조 참조 — 예: secondary_kind가 "depth"면 secondary_char_no/
    secondary_set도 depth 트리를 본다). 둘 다 없으면 예전 pose 전용 동작과
    같도록 "pose"를 기본값으로 쓴다."""
    if option.get("kind"):
        return option["kind"]
    kind_from = option.get("kind_from")
    if kind_from:
        return options_so_far.get(kind_from, "none")
    return "pose"


def coerce_option(option: dict, raw: str | None, options_so_far: dict, pod: dict | None = None):
    if raw is None or raw == "":
        raw = option.get("default")
    # 비면 그 작업이 애초에 성공할 수 없는 옵션(예: 셸 명령의 "명령")은 큐에 넣기 전에
    # 막는다 — 돌려봐야 실패할 작업이 대기 목록에 쌓이면 안 되므로.
    if option.get("required") and (raw is None or str(raw).strip() == ""):
        raise HTTPException(400, f"'{option['label']}'을(를) 입력하세요.")
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

    if opt_type == "comfy_model":
        # ComfyUI에 실제로 설치된 목록(MODEL_LIST_SOURCES의 종류)에서 고르는 드롭다운.
        # 비워두면 "워크플로우에 이미 들어있는 값을 그대로 쓴다"는 뜻이라 그냥 통과.
        value = "" if raw is None else str(raw).strip()
        if not value:
            return ""
        source = MODEL_LIST_SOURCES.get(option.get("model_kind"))
        if source is None:
            raise HTTPException(400, f"'{option['label']}' 옵션의 model_kind 설정이 올바르지 않아요.")
        try:
            # 그 작업이 실제로 갈 파드의 설치 목록으로 검증한다 — 다른 파드에만
            # 있는 체크포인트를 통과시키면 실행 직전에야 실패한다.
            _, object_info = fetch_comfy_object_info(False, pod)
        except Exception:
            object_info = None
        # ComfyUI가 꺼져 있으면 검증할 기준 자체가 없다. 이 앱은 ComfyUI가 안 떠 있는
        # 동안에도 큐에 미리 쌓아두는 걸 정상 동작으로 보므로(워커가 실행 직전에 다시
        # 확인함), 확인할 수 없을 때는 막지 않고 그대로 통과시킨다.
        if object_info is None:
            return value
        installed = combo_choices(object_info, *source)
        if installed and value not in installed:
            raise HTTPException(
                400,
                f"'{option['label']}' 값 '{value}'은(는) 지금 연결된 ComfyUI에 설치돼 있지 않아요.",
            )
        return value

    if opt_type == "char_no":
        # "인물 수" 드롭다운 — 실제 존재하는 <kind> 종류 하위 숫자 폴더 중 하나여야
        # 함. kind는 옵션이 정적으로 선언(주 참조, 예: "kind": "pose")하거나,
        # "kind_from"으로 다른 옵션(보조 참조 종류 선택)의 값을 그대로 따라간다 —
        # 그 옵션 값이 "none"(보조 참조 안 씀)이면 이 옵션도 의미가 없으니 빈
        # 값을 그대로 통과시킨다(검증하지 않음).
        kind = resolve_option_kind(option, options_so_far)
        if kind == "none":
            return ""
        value = "" if raw is None else str(raw)
        if value not in list_char_nos(kind):
            raise HTTPException(400, f"'{option['label']}' 값이 올바르지 않아요.")
        return value

    if opt_type == "input_image":
        # img2img/USDU 워크플로우 유형이 쓰는, 세트 구분 없는 평평한 입력 이미지
        # 목록(input_assets.py)에서 파일 하나를 고르는 드롭다운.
        value = "" if raw is None else str(raw).strip()
        if not value:
            raise HTTPException(400, f"'{option['label']}' 값을 선택해야 해요.")
        try:
            resolve_input_image(value)
        except InputAssetError as e:
            raise HTTPException(400, str(e))
        return value

    # 아래 세 개는 "안 써도 되는" 참조 슬롯(MiniMax-H3 r2v의 ref_image_1~9/ref_video_1~3/
    # ref_audio_1~3)이 쓴다 — 비어 있으면 그냥 통과(number_optional과 같은 "_optional
    # 접미사는 비어도 됨" 규칙), 값이 있으면 그 종류의 자산 풀에 실제로 있는지만 검증한다.
    if opt_type == "input_image_optional":
        value = "" if raw is None else str(raw).strip()
        if not value:
            return ""
        try:
            resolve_input_image(value)
        except InputAssetError as e:
            raise HTTPException(400, str(e))
        return value

    if opt_type == "input_video":
        value = "" if raw is None else str(raw).strip()
        if not value:
            return ""
        try:
            resolve_input_video(value)
        except InputAssetError as e:
            raise HTTPException(400, str(e))
        return value

    if opt_type == "input_audio":
        value = "" if raw is None else str(raw).strip()
        if not value:
            return ""
        try:
            resolve_input_audio(value)
        except InputAssetError as e:
            raise HTTPException(400, str(e))
        return value

    if opt_type == "asset_folder":
        # 잡을 큐에 올리는 시점(업로드 시)에 참조 세트 폴더가 실제로 있고 이미지가
        # 있는지 미리 확인해서, 큐 시작 이후에야 실패하는 일이 없게 한다. char_no는
        # option["char_no_option"](기본 "char_no")이 가리키는 다른 옵션의 이미
        # 처리된 값을 스코프로 쓰고, 없으면 DEFAULT_CHAR_NO를 쓴다. kind는 위
        # char_no와 같은 규칙("kind" 정적 선언 또는 "kind_from") — "none"이면
        # 보조 참조를 안 쓰는 경우이니 검증 없이 빈 값을 통과시킨다.
        kind = resolve_option_kind(option, options_so_far)
        if kind == "none":
            return ""
        value = "" if raw is None else str(raw)
        char_no_key = option.get("char_no_option", "char_no")
        char_no = options_so_far.get(char_no_key, DEFAULT_CHAR_NO)
        try:
            validate_ref_set(kind, value, char_no)
        except RefAssetError as e:
            raise HTTPException(400, str(e))
        return value

    return "" if raw is None else str(raw)


def validate_ref_csv_rows(csv_bytes: bytes, kind: str, ref_column: str, char_no_column: str = "char_no"):
    # *_csv_batch 공용 업로드 시점 검증: CSV의 모든 행을 미리 훑어 ref_column(및
    # char_no_column) 값이 실제로 해석 가능한지(resolve_ref) 확인한다. 스크립트가
    # 실행되다가 특정 행에서야 실패하는 일이 없도록, 한 행이라도 문제가 있으면
    # 업로드 자체를 거부한다. char_no_column은 ref_column을 지정한 행에서만
    # 의미가 있으므로(참조 폴더의 탐색 루트일 뿐), ref_column이 비어 있는 행은
    # 건드리지 않는다 — 주 참조든 보조 참조든 같은 규칙이라 이 함수 하나를
    # 컬럼 이름만 바꿔서 재사용한다.
    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "CSV가 유효한 UTF-8 텍스트가 아니에요.")

    rows = list(csv.DictReader(io.StringIO(text)))
    errors = []
    for line_no, row in enumerate(rows, start=2):  # 헤더가 1번 줄
        ref_value = (row.get(ref_column) or "").strip()
        if not ref_value:
            continue
        try:
            char_no = parse_char_no(row.get(char_no_column))
        except ValueError:
            errors.append(f"{line_no}번째 줄({char_no_column}='{row.get(char_no_column)}'): 정수가 아니에요.")
            continue
        try:
            resolve_ref(kind, ref_value, char_no)
        except RefReferenceError as e:
            errors.append(f"{line_no}번째 줄({ref_column}='{ref_value}', {char_no_column}='{char_no}'): {e}")

    if errors:
        raise HTTPException(400, f"CSV의 {ref_column}/{char_no_column} 컬럼을 확인하세요.\n" + "\n".join(errors))


def csv_has_ref_value(csv_bytes: bytes, column: str) -> bool:
    # *_csv_batch는 CSV 행마다 참조 컬럼이 비어 있으면 그 행은 의도적으로
    # ControlNet 없이 생성한다(README 참고) — 모든 행의 값이 비어 있으면 워크플로우에
    # LoadImage 노드가 없어도 문제가 없으므로, 그런 경우까지 아래
    # validate_workflow_has_ref_node()가 막아버리지 않도록 미리 구분해둔다.
    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False  # 디코딩 자체는 validate_ref_csv_rows가 이미 걸러줌
    rows = csv.DictReader(io.StringIO(text))
    return any((row.get(column) or "").strip() for row in rows)


# 템플릿 id -> 그 템플릿의 "주(main) 참조 종류". 값이 있는 템플릿만 "주 참조 필수"
# 템플릿이다(업로드 시점에 LoadImage 노드 존재를 강제 검증) — csv_batch처럼 참조
# 이미지가 아예 없는 템플릿은 여기 없다. 보조 참조(secondary_kind 옵션)는 있으면
# 좋고 없어도 그만인 선택 기능이라 이 딕셔너리와 무관하게 항상 best-effort다
# (노드가 없으면 실행 스크립트가 경고만 남기고 계속 진행 — MAIN_PROMPT 노드를
# 못 찾았을 때와 같은 관용).
TEMPLATE_PRIMARY_KIND = {
    "pose_batch": "pose",
    "pose_csv_batch": "pose",
    "depth_batch": "depth",
    "depth_csv_batch": "depth",
    "lineart_batch": "lineart",
    "lineart_csv_batch": "lineart",
}
REF_NODE_REQUIRED_TEMPLATES = set(TEMPLATE_PRIMARY_KIND)

# 주 참조 종류별로 LoadImage 노드 제목 매칭에 쓰는 환경변수 이름 — 템플릿
# 스크립트가 쓰는 것과 정확히 같은 이름이어야 업로드 시점 검증과 실행 시점 동작이
# 일치한다(templates/*_batch.py의 환경변수 문서 참고).
REF_KIND_NODE_TITLE_ENV = {"pose": "POSE_NODE_TITLE", "depth": "DEPTH_NODE_TITLE", "lineart": "LINEART_NODE_TITLE"}
REF_KIND_LABELS = {"pose": "포즈", "depth": "depth", "lineart": "lineart"}

# CSV 템플릿에서 주 참조를 지정하는 컬럼 이름 — pose_csv_batch는 하위호환을 위해
# "pose" 그대로 쓰고, 새 템플릿은 종류 이름을 그대로 컬럼명으로 쓴다.
CSV_PRIMARY_REF_COLUMN = {"pose_csv_batch": "pose", "depth_csv_batch": "depth", "lineart_csv_batch": "lineart"}


def find_ref_load_image_node(workflow: dict, title_substring: str):
    """templates/*_batch.py의 apply_ref_image()류가 실행 시점에 실제로 어떤
    노드를 골라 참조 이미지를 주입할지 업로드 시점에 미리 예측한다 — 각
    스크립트의 find_node()와 정확히 같은 알고리즘이다: 제목에 title_substring이
    포함된 노드가 있으면 class_type과 무관하게 그 노드를 최우선으로 고르고(그래서
    "Load Checkpoint"처럼 우연히 제목에 "Load"가 들어간 LoadImage가 아닌 노드가
    먼저 골라질 수 있다 — 이 경우도 아래에서 "참조 노드 없음"으로 취급해야 함),
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


def validate_workflow_has_ref_node(workflow_bytes: bytes, kind: str):
    # *_batch/*_csv_batch는 주 참조 이미지를 LoadImage 노드에 주입해야 ControlNet이
    # 실제로 동작한다. 이 노드가 없는 워크플로우를 잘못 올리면, 실행 스크립트는
    # stderr에 경고만 남기고 참조 없이 이미지 생성을 계속 진행한다 — 큐에 올리기
    # 전에 미리 걸러서 그런 사고를 막는다. (이 검사는 "주 참조"에만 적용된다 —
    # 보조 참조는 있으면 좋고 없어도 그만인 선택 기능이라 노드가 없어도 업로드를
    # 막지 않고 실행 시점에 경고만 남긴다.)
    try:
        workflow = json.loads(workflow_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise HTTPException(400, f"워크플로우가 올바른 JSON이 아니에요: {e}")
    if not isinstance(workflow, dict):
        raise HTTPException(400, "워크플로우가 올바른 ComfyUI API 형식(JSON 객체)이 아니에요.")

    title_substring = os.environ.get(REF_KIND_NODE_TITLE_ENV[kind], "Load")
    node = find_ref_load_image_node(workflow, title_substring)
    label = REF_KIND_LABELS[kind]
    if node is None or node.get("class_type") != "LoadImage":
        raise HTTPException(
            400,
            f"이 워크플로우에는 {label} 이미지를 넣을 LoadImage 노드가 없어요. "
            f"(제목에 '{title_substring}'가 포함된 노드가 있다면 LoadImage가 아니고, "
            "그런 노드가 아예 없다면 다른 LoadImage 노드도 찾지 못했어요.) 참조 "
            "없이 이미지가 생성되는 사고를 막기 위해 업로드를 거부했어요 — 워크플로우에 "
            "LoadImage 노드를 추가하거나 제목을 확인한 뒤 다시 업로드하세요.",
        )


# input_image_batch/input_image_csv_batch(img2img)와 ipadapter_batch/
# ipadapter_csv_batch(IPAdapter 프리셋) 전용 — pose/depth/lineart처럼
# ref_assets.py의 kind 계층을 쓰지 않으므로(input_assets.py의 평평한 목록)
# TEMPLATE_PRIMARY_KIND와는 별개로 다룬다. 템플릿 id별로 "어떤 제목의 LoadImage
# 노드에 주입하는지"/"CSV의 어느 컬럼을 읽는지"만 다르고 검증 로직은 완전히
# 같아서 하나의 함수 쌍을 재사용한다.
FLAT_IMAGE_TEMPLATES = {
    "input_image_batch": {"node_title_env": "INPUT_IMAGE_NODE_TITLE", "default_title": "input_image", "column": "input_image", "label": "입력 이미지"},
    "input_image_csv_batch": {"node_title_env": "INPUT_IMAGE_NODE_TITLE", "default_title": "input_image", "column": "input_image", "label": "입력 이미지"},
    "ipadapter_batch": {"node_title_env": "IPADAPTER_NODE_TITLE", "default_title": "ipadapter_ref", "column": "ipadapter_ref", "label": "IPAdapter 참조 이미지"},
    "ipadapter_csv_batch": {"node_title_env": "IPADAPTER_NODE_TITLE", "default_title": "ipadapter_ref", "column": "ipadapter_ref", "label": "IPAdapter 참조 이미지"},
}


def validate_workflow_has_flat_image_node(workflow_bytes: bytes, node_title_env: str, default_title: str, label: str):
    try:
        workflow = json.loads(workflow_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise HTTPException(400, f"워크플로우가 올바른 JSON이 아니에요: {e}")
    if not isinstance(workflow, dict):
        raise HTTPException(400, "워크플로우가 올바른 ComfyUI API 형식(JSON 객체)이 아니에요.")

    title_substring = os.environ.get(node_title_env, default_title)
    node = find_ref_load_image_node(workflow, title_substring)
    if node is None or node.get("class_type") != "LoadImage":
        raise HTTPException(
            400,
            f"이 워크플로우에는 {label}를 넣을 LoadImage 노드가 없어요. "
            f"(제목에 '{title_substring}'가 포함된 노드가 있다면 LoadImage가 아니고, "
            "그런 노드가 아예 없다면 다른 LoadImage 노드도 찾지 못했어요.) 이미지 없이 "
            f"실행되는 사고를 막기 위해 업로드를 거부했어요 — 워크플로우 빌더로 만들거나, "
            f"직접 만든 워크플로우라면 LoadImage 노드의 제목을 '{title_substring}'로 맞춰서 "
            "다시 업로드하세요.",
        )


def validate_flat_image_csv_rows(csv_bytes: bytes, column: str):
    # *_csv_batch 공용 검증과 같은 패턴(validate_ref_csv_rows) — CSV 전체를 미리
    # 훑어 그 컬럼 값이 실제로 존재하는 파일인지 확인한다.
    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "CSV가 유효한 UTF-8 텍스트가 아니에요.")

    rows = list(csv.DictReader(io.StringIO(text)))
    errors = []
    for line_no, row in enumerate(rows, start=2):  # 헤더가 1번 줄
        value = (row.get(column) or "").strip()
        if not value:
            continue
        try:
            resolve_input_image(value)
        except InputAssetError as e:
            errors.append(f"{line_no}번째 줄({column}='{value}'): {e}")

    if errors:
        raise HTTPException(400, f"CSV의 {column} 컬럼을 확인하세요.\n" + "\n".join(errors))


_recent_user_stores: dict[tuple[str, int], "RecentFileStore"] = {}


def recent_store(kind: str, user: dict) -> "RecentFileStore":
    """"최근 워크플로우/CSV" 저장소 — 관리자는 예전 것 그대로, 일반 회원은 자기 것을 따로 갖는다."""
    base = recent_workflows_store if kind == "workflows" else recent_csvs_store
    if auth.is_admin(user):
        return base
    key = (kind, user["id"])
    with lock:
        store = _recent_user_stores.get(key)
        if store is None:
            folder = base.dir_path / "users" / str(user["id"])
            folder.mkdir(parents=True, exist_ok=True)
            store = RecentFileStore(folder, base.state_path.with_name(f"{base.state_path.stem}_u{user['id']}.json"),
                                    base.retention)
            store.load()
            _recent_user_stores[key] = store
    return store


@app.get("/api/recent-workflows")
def list_recent_workflows(request: Request):
    # "새 작업 추가"의 워크플로우 슬롯 옆 "최근 워크플로우" 버튼이 호출한다.
    store = recent_store("workflows", me(request))
    return {"workflows": store.list_meta(), "retention": store.retention}


@app.get("/api/recent-workflows/{workflow_id}")
def get_recent_workflow(workflow_id: str, request: Request):
    store = recent_store("workflows", me(request))
    entry = store.get(workflow_id)
    if entry is None:
        raise HTTPException(404, "해당 워크플로우를 찾을 수 없어요.")
    path = store.dir_path / entry["stored_filename"]
    if not path.exists():
        raise HTTPException(404, "워크플로우 파일이 서버에 없어요.")
    return Response(content=path.read_text(encoding="utf-8"), media_type="application/json")


@app.delete("/api/recent-workflows/{workflow_id}")
def delete_recent_workflow(workflow_id: str, request: Request):
    if not recent_store("workflows", me(request)).delete(workflow_id):
        raise HTTPException(404, "해당 워크플로우를 찾을 수 없어요.")
    return {"ok": True}


@app.get("/api/recent-csvs")
def list_recent_csvs(request: Request):
    # "새 작업 추가"의 CSV 슬롯 옆 "최근 CSV" 버튼이 호출한다.
    store = recent_store("csvs", me(request))
    return {"csvs": store.list_meta(), "retention": store.retention}


@app.get("/api/recent-csvs/{csv_id}")
def get_recent_csv(csv_id: str, request: Request):
    store = recent_store("csvs", me(request))
    entry = store.get(csv_id)
    if entry is None:
        raise HTTPException(404, "해당 CSV를 찾을 수 없어요.")
    path = store.dir_path / entry["stored_filename"]
    if not path.exists():
        raise HTTPException(404, "CSV 파일이 서버에 없어요.")
    return Response(content=path.read_text(encoding="utf-8"), media_type="text/csv")


@app.delete("/api/recent-csvs/{csv_id}")
def delete_recent_csv(csv_id: str, request: Request):
    if not recent_store("csvs", me(request)).delete(csv_id):
        raise HTTPException(404, "해당 CSV를 찾을 수 없어요.")
    return {"ok": True}


def resolve_template(template_id) -> dict:
    if not isinstance(template_id, str) or not template_id:
        raise HTTPException(400, "template_id는 필수예요.")
    template = load_templates_map().get(template_id)
    if template is None:
        raise HTTPException(400, "존재하지 않는 템플릿이에요.")
    return template


async def create_job(
    template: dict,
    workflow_bytes: bytes,
    workflow_filename: str,
    csv_bytes: bytes | None,
    csv_filename: str | None,
    raw_options: dict,
    pod_id: str | None = None,
    video_workflow_bytes: bytes | None = None,
    video_workflow_filename: str | None = None,
    project_id: int | None = None,
    user: dict | None = None,
    start_paused: bool = False,
) -> dict:
    # POST /api/upload(사람이 브라우저에서 파일 첨부)와 POST /api/jobs(LLM 등
    # 프로그램이 JSON으로 호출)가 공유하는 실제 잡 생성 로직 — 두 경로 모두
    # 워크플로우/CSV를 이미 bytes로, 옵션을 이미 {name: 원본 문자열} 형태로
    # 만들어서 넘겨준다. 그 앞단(멀티파트 폼 파싱 vs JSON 파싱)만 다르다.
    if user is None:
        raise HTTPException(401, "로그인이 필요해요.")
    # 파드는 정해도 되고 안 정해도 된다. 안 정하면 작업이 파드 없이 대기 큐에 들어가고, 스케줄러가 필요한 모델을 갖춘
    # 파드가 살아 있을 때 배정한다(파드가 하나도 없어도 작업을 만들어 둘 수 있다). 작업은 그 작업을 만든 회원의
    # 파드에서만 돈다.
    pod = None
    if pod_id:
        pod = pod_registry.get_pod(pod_id)
        if pod is None or pod.get("owner_id") != user["id"]:
            raise HTTPException(400, "없는 파드예요.")
        if not pod.get("enabled"):
            raise HTTPException(400, f"'{pod['name']}' 파드는 지금 사용 안 함 상태예요.")

    # 프로젝트는 파드와 무관하게 작업을 묶는다. 없으면 "미분류"(project_id=None). 자기 프로젝트만 쓸 수 있다.
    if project_id is not None and not project_store.project_exists(project_id, owner_id=user["id"]):
        raise HTTPException(400, "없는 프로젝트예요.")

    # 템플릿이 특정 워커 종류 전용이면(셸 명령은 셸 파드에서만 뜻이 있다) 여기서 막는다.
    # 안 막으면 ComfyUI 파드에 셸 작업이 들어가 조용히 엉뚱하게 돈다.
    allowed_kinds = template.get("pod_kinds")
    if pod is not None and allowed_kinds and pod["kind"] not in allowed_kinds:
        raise HTTPException(
            400,
            f"'{template['label']}' 템플릿은 {', '.join(allowed_kinds)} 종류의 파드에서만 쓸 수 있어요 "
            f"(고른 파드 '{pod['name']}'는 {pod['kind']}).",
        )
    # 반대 방향도 막는다 — ComfyUI용 템플릿(대부분)은 셸 파드로 보낼 수 없다.
    if pod is not None and not allowed_kinds and pod["kind"] != pod_registry.DEFAULT_KIND:
        raise HTTPException(
            400,
            f"'{template['label']}' 템플릿은 {pod_registry.DEFAULT_KIND} 파드용이에요 "
            f"(고른 파드 '{pod['name']}'는 {pod['kind']}).",
        )

    with lock:
        active_count = sum(
            1 for j in jobs.values()
            if j["status"] in ("pending", "queued", "running") and not j.get("deleted")
            and j.get("owner_id") == user["id"]
        )
    if active_count >= MAX_ACTIVE_JOBS:
        raise HTTPException(
            429,
            f"대기/실행 중인 작업이 이미 {MAX_ACTIVE_JOBS}개예요 — 너무 많이 쌓였어요. "
            "완료된 작업을 정리하거나 잠시 후 다시 시도하세요.",
        )

    template_id = template["id"]
    requires_csv = bool(template.get("requires_csv"))
    if requires_csv and not csv_bytes:
        raise HTTPException(400, "이 템플릿은 csv가 필요해요.")

    primary_kind = TEMPLATE_PRIMARY_KIND.get(template_id)
    primary_csv_column = CSV_PRIMARY_REF_COLUMN.get(template_id)
    if primary_csv_column and csv_bytes is not None:
        validate_ref_csv_rows(csv_bytes, primary_kind, primary_csv_column)

        # 보조 참조(CSV 템플릿 전용) — secondary_kind가 "none"이 아니면 CSV의
        # secondary_ref/secondary_char_no 컬럼도 미리 검증한다. 아직 옵션을
        # coerce하기 전이라 원본 값을 직접 읽는다(정식 검증은 select 타입
        # 처리에서 한 번 더 함 — 여기서는 "CSV 컬럼 검사가 필요한지" 판단용으로만
        # 가볍게 읽는다).
        secondary_kind_raw = raw_options.get("secondary_kind")
        if secondary_kind_raw in REF_KINDS:
            validate_ref_csv_rows(csv_bytes, secondary_kind_raw, "secondary_ref", "secondary_char_no")

    if primary_kind and workflow_bytes is not None:
        needs_ref_node = True
        if primary_csv_column:
            needs_ref_node = csv_bytes is not None and csv_has_ref_value(csv_bytes, primary_csv_column)
        if needs_ref_node:
            validate_workflow_has_ref_node(workflow_bytes, primary_kind)

    flat_image_spec = FLAT_IMAGE_TEMPLATES.get(template_id)
    if flat_image_spec and workflow_bytes is not None:
        if csv_bytes is not None:
            validate_flat_image_csv_rows(csv_bytes, flat_image_spec["column"])
        validate_workflow_has_flat_image_node(
            workflow_bytes, flat_image_spec["node_title_env"], flat_image_spec["default_title"], flat_image_spec["label"],
        )

    # comfy_model 옵션(체크포인트/LoRA 드롭다운)을 쓰는 템플릿이면 설치 목록으로
    # 값을 검증해야 한다. coerce_option은 동기 함수라, 여기서 미리 스레드로 받아
    # 캐시를 채워둔다 — 안 그러면 그 안의 ComfyUI 조회가 이벤트 루프를 막는다.
    # 못 받아오면(꺼져 있음 등) coerce_option이 검증을 건너뛴다.
    if pod is not None and any(o.get("type") == "comfy_model" for o in template.get("options", [])):
        try:
            await asyncio.to_thread(fetch_comfy_object_info, False, pod)
        except Exception:
            pass

    options = {}
    for option in template.get("options", []):
        raw = raw_options.get(option["name"])
        # 파드를 안 정했으면 설치 목록으로 검증할 기준이 없다 — 값은 그대로 받고, 스케줄러가 배정할 때 그 파드에 있는지 본다.
        options[option["name"]] = coerce_option(option, raw, options, pod if pod is not None else NO_POD)

    job_id = str(uuid.uuid4())[:8]

    # 파드 스냅샷용 GPU/시간당 비용 — 대시보드가 이미 주기적으로 조회해 캐시해둔 값을
    # 쓰므로 보통 네트워크를 안 탄다. 캐시가 비어 있어도 작업 추가를 오래 붙잡지 않도록
    # 짧게만 기다리고, 못 얻으면 그냥 비워둔다(비용 집계에서 "모름"으로 남는다).
    pod_gpu = pod_cost_per_hr = None
    try:
        if pod is None:
            raise RuntimeError("파드 없음")
        info = await asyncio.wait_for(
            asyncio.to_thread(runpod_api.get_runpod_info, pod.get("url") or ""), timeout=3)
        if info:
            pod_gpu = info.get("gpu_type")
            cost = info.get("cost_per_hr")
            pod_cost_per_hr = float(cost) if isinstance(cost, (int, float)) else None
    except Exception:
        pass

    # 워크플로우가 필요 없는 템플릿도 있다(셸 명령처럼 ComfyUI를 아예 안 쓰는 것들).
    workflow_dest_name = None
    if workflow_bytes is not None:
        workflow_dest_name = f"{job_id}_{workflow_filename}"
        (JOBS_DIR / workflow_dest_name).write_bytes(workflow_bytes)
        recent_store("workflows", user).record(workflow_filename, workflow_bytes)

    csv_dest_name = None
    csv_original_name = None
    if requires_csv:
        csv_dest_name = f"{job_id}_{csv_filename}"
        (JOBS_DIR / csv_dest_name).write_bytes(csv_bytes)
        csv_original_name = csv_filename
        recent_store("csvs", user).record(csv_filename, csv_bytes)

    # img2video 복합 템플릿의 "영상 생성 워크플로우"는 완전히 선택 — 안 올리면
    # 템플릿 스크립트가 nightshift 내장 기본값(wan22_i2v.json/wan22_flf2v.json)을
    # 그대로 쓴다. 다른 템플릿들은 애초에 이 값을 보내지 않으므로 항상 None.
    video_workflow_dest_name = None
    if video_workflow_bytes is not None:
        video_workflow_dest_name = f"{job_id}_video_{video_workflow_filename}"
        (JOBS_DIR / video_workflow_dest_name).write_bytes(video_workflow_bytes)
        recent_store("workflows", user).record(video_workflow_filename, video_workflow_bytes)

    preflight = None
    if pod is not None and pod["kind"] == pod_registry.DEFAULT_KIND and workflow_bytes is not None:
        blobs = [("워크플로우", workflow_bytes),
                 ("영상 워크플로우", video_workflow_bytes if video_workflow_bytes is not None
                  else default_video_workflow_bytes(template))]
        try:
            preflight = await asyncio.wait_for(asyncio.to_thread(compute_preflight, pod, user, blobs), timeout=10)
        except Exception:
            preflight = None

    with lock:
        jobs[job_id] = {
            "id": job_id,
            "template_id": template_id,
            "template_label": template["label"],
            "script_filename": template["script_filename"],
            "options": options,
            "workflow_filename": workflow_dest_name,
            "workflow_original_name": workflow_filename if workflow_dest_name else None,
            "video_workflow_filename": video_workflow_dest_name,
            "video_workflow_original_name": video_workflow_filename if video_workflow_dest_name else None,
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
            "pod_id": pod["id"] if pod is not None else None,
            # 파드를 정해서 만든 작업은 그 파드에서만 돈다(스케줄러도 다른 파드로 보내지 않는다).
            "pinned_pod_id": pod["id"] if pod is not None else None,
            "waiting_reason": None,
            "project_id": project_id,
            "owner_id": user["id"],
            "preflight": preflight,
            # 파드는 일시적이라(지워지고 다시 안 쓴다) 나중에 "어디서 돌았나/얼마 들었나"를
            # 볼 수 있게 이 시점의 정보를 job에 찍어둔다. 파드를 안 정했으면 배정될 때 채운다.
            "pod_name": pod.get("name") if pod is not None else None,
            "pod_kind": pod.get("kind") if pod is not None else None,
            "pod_gpu": pod_gpu,
            "pod_cost_per_hr": pod_cost_per_hr,
        }
        # start_paused(브라우저의 "New job" 모달이 항상 켠다)면 pod_id/auto_run과
        # 무관하게 "pending"(일시정지) 그대로 둔다 — 대기 칸에 카드만 쌓아두고,
        # 사람이 카드의 ▶ 시작을 직접 눌러야 돈다. LLM/curl이 쓰는 POST /api/jobs는
        # 이 인자를 안 넘기므로(기본 False) 예전처럼 바로 큐에 들어가는 게 그대로다.
        if start_paused:
            auto_queued = False
        elif pod is None:
            # 파드 없이 만든 작업은 곧장 대기 큐로 간다("보내기") — 스케줄러가 갖춰진 파드를 찾는다.
            jobs[job_id]["status"] = "queued"
            jobs[job_id]["waiting_reason"] = "파드를 찾는 중이에요"
            auto_queued = False
        else:
            # 자동 실행 모드("▶ 시작"이 켜져 있는 동안)면 대기 목록에 머무르지 않고
            # 바로 그 파드의 실행 큐에 넣는다 — 그래야 켜놓은 동안 새로 추가하는 작업이
            # 계속 이어서 처리된다. 자동 실행은 파드마다 따로 켜고 끈다.
            rt = pod_runtimes.get(pod["id"])
            auto_queued = bool(rt and rt.auto_run)
            if auto_queued:
                jobs[job_id]["status"] = "queued"
    save_state()
    if start_paused:
        pass
    elif pod is None:
        poke_scheduler()
    elif auto_queued:
        dispatch_job(job_id, pod["id"])
    return jobs[job_id]


@app.post("/api/upload")
async def upload(request: Request):
    user = me(request)
    form = await request.form()

    template = resolve_template(form.get("template_id"))

    # 워크플로우가 필요 없는 템플릿(셸 명령 등)은 첨부를 요구하지 않는다.
    needs_workflow = template.get("requires_workflow", True)
    workflow = form.get("workflow")
    if needs_workflow and (not isinstance(workflow, UploadFile) or not workflow.filename
                           or not workflow.filename.endswith(".json")):
        raise HTTPException(400, "워크플로우는 json 파일만 업로드할 수 있어요.")

    requires_csv = bool(template.get("requires_csv"))
    csv_file = form.get("csv")
    if requires_csv and (not isinstance(csv_file, UploadFile) or not csv_file.filename or not csv_file.filename.endswith(".csv")):
        raise HTTPException(400, "이 템플릿은 csv 파일이 필요해요.")

    # 영상 생성 워크플로우는 완전히 선택(img2video 복합 템플릿에서만 의미가 있고,
    # 안 올리면 템플릿이 내장 기본값을 씀) — 있으면 .json인지만 확인한다.
    video_workflow = form.get("video_workflow")
    has_video_workflow = isinstance(video_workflow, UploadFile) and bool(video_workflow.filename)
    if has_video_workflow and not video_workflow.filename.endswith(".json"):
        raise HTTPException(400, "영상 생성 워크플로우는 json 파일만 업로드할 수 있어요.")

    # UploadFile은 한 번만 읽을 수 있으므로, 검증에도 쓰고 저장에도 쓸 수 있게
    # 여기서 미리 한 번만 읽어둔다.
    workflow_bytes = await workflow.read() if needs_workflow else None
    csv_bytes = await csv_file.read() if requires_csv else None
    video_workflow_bytes = await video_workflow.read() if has_video_workflow else None

    raw_options = {}
    for option in template.get("options", []):
        value = form.get(option["name"])
        raw_options[option["name"]] = value if isinstance(value, str) else None

    pod_id = form.get("pod_id")
    return await create_job(
        template,
        workflow_bytes,
        workflow.filename if needs_workflow else None,
        csv_bytes,
        csv_file.filename if requires_csv else None,
        raw_options,
        pod_id if isinstance(pod_id, str) and pod_id.strip() else None,
        video_workflow_bytes,
        video_workflow.filename if has_video_workflow else None,
        parse_project_id(form.get("project_id")),
        user=user,
        start_paused=form.get("start_paused") == "1",
    )


def parse_project_id(value) -> int | None:
    """요청에서 온 project_id(숫자/숫자 문자열/빈 값)를 int 또는 None(미분류)으로."""
    if value is None or value == "" or value == "null":
        return None
    if isinstance(value, bool):
        raise HTTPException(400, "project_id는 숫자여야 해요.")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, "project_id는 숫자여야 해요.")


@app.post("/api/jobs")
async def create_job_from_json(request: Request):
    # /api/upload과 완전히 같은 파이프라인(create_job)을 파일 첨부 없이 JSON
    # 바디로 쓸 수 있게 한 것. 사람은 브라우저에서 파일을 고르지만, curl이나
    # LLM처럼 프로그램으로 호출하는 쪽엔 multipart/form-data보다 JSON이 훨씬
    # 다루기 쉽다 — 워크플로우는 POST /api/build-workflow로 만든 걸 그대로
    # 넣거나 직접 준 JSON을 쓰면 되고, CSV는 원문 문자열 그대로 준다(csv_batch.
    # sample.csv 같은 형식). 큐에 실제로 등록된다는 점은 /api/upload와 동일하다
    # — 미리보기가 필요하면 POST /api/validate-workflow를 먼저 불러볼 것.
    user = me(request)
    try:
        body = json.loads(await request.body())
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    if not isinstance(body, dict):
        raise HTTPException(400, "요청 본문이 JSON 객체여야 해요.")

    template = resolve_template(body.get("template_id"))

    needs_workflow = template.get("requires_workflow", True)
    workflow = body.get("workflow")
    workflow_filename = None
    workflow_bytes = None
    if needs_workflow or workflow is not None:
        if not isinstance(workflow, dict):
            raise HTTPException(400, "workflow는 JSON 객체(워크플로우 자체)여야 해요.")
        workflow_filename = body.get("workflow_filename") or "workflow.json"
        if not isinstance(workflow_filename, str) or not workflow_filename.endswith(".json"):
            raise HTTPException(400, "workflow_filename은 .json으로 끝나야 해요.")
        workflow_bytes = json.dumps(workflow).encode("utf-8")

    csv_text = body.get("csv")
    csv_bytes = None
    csv_filename = None
    if csv_text is not None:
        if not isinstance(csv_text, str):
            raise HTTPException(400, "csv는 CSV 원문을 담은 문자열이어야 해요.")
        csv_filename = body.get("csv_filename") or "data.csv"
        if not isinstance(csv_filename, str) or not csv_filename.endswith(".csv"):
            raise HTTPException(400, "csv_filename은 .csv로 끝나야 해요.")
        csv_bytes = csv_text.encode("utf-8")

    raw_options_in = body.get("options") or {}
    if not isinstance(raw_options_in, dict):
        raise HTTPException(400, "options는 JSON 객체여야 해요.")
    raw_options = {k: (None if v is None else str(v)) for k, v in raw_options_in.items()}

    # 영상 생성 워크플로우는 완전히 선택(img2video 복합 템플릿 전용) — 안 주면
    # 템플릿이 내장 기본값을 쓴다.
    video_workflow = body.get("video_workflow")
    video_workflow_filename = None
    video_workflow_bytes = None
    if video_workflow is not None:
        if not isinstance(video_workflow, dict):
            raise HTTPException(400, "video_workflow는 JSON 객체(워크플로우 자체)여야 해요.")
        video_workflow_filename = body.get("video_workflow_filename") or "video_workflow.json"
        if not isinstance(video_workflow_filename, str) or not video_workflow_filename.endswith(".json"):
            raise HTTPException(400, "video_workflow_filename은 .json으로 끝나야 해요.")
        video_workflow_bytes = json.dumps(video_workflow).encode("utf-8")

    pod_id = body.get("pod_id")
    return await create_job(template, workflow_bytes, workflow_filename, csv_bytes, csv_filename,
                            raw_options, pod_id if isinstance(pod_id, str) and pod_id.strip() else None,
                            video_workflow_bytes, video_workflow_filename,
                            parse_project_id(body.get("project_id")), user=user)


def start_pods(pod_ids: list[str]) -> int:
    """그 파드들의 자동 실행 모드를 켜고, 그 파드로 배정된 대기 작업을 전부 큐에 넣는다.

    배정된 파드가 **사라졌거나 꺼져 있는** 작업만 켜는 파드 중 첫 번째로 되돌린다 —
    큐에 못 들어가 영영 안 도는 작업이 생기면 안 되므로. 반대로 배정된 파드가 멀쩡히
    살아 있으면 그건 남의 몫이라 손대지 않는다. 예전에는 "파드를 하나만 켤 때"를
    "다른 파드는 안 쓴다는 뜻"으로 읽고 전부 끌어왔는데, 작업 화면이 파드 안으로
    들어가면서 그 화면의 ▶ 시작이 늘 파드 하나만 켜게 됐다 — 그 규칙이 남아 있으면
    파드 A의 작업 화면에서 시작을 눌렀을 뿐인데 파드 B의 대기 작업이 A로 끌려온다.
    """
    targets = set(pod_ids)
    if not targets:
        return 0
    pod_owner = {pid: (pod_registry.get_pod(pid) or {}).get("owner_id") for pid in pod_ids}
    templates = load_templates_map()
    with lock:
        for pod_id in targets:
            rt = pod_runtimes.get(pod_id)
            if rt is not None:
                rt.auto_run = True
        # deleted도 함께 확인해야 한다 — delete_job()은 소프트 삭제라 status를
        # "pending"으로 그대로 둔 채 deleted=True만 표시하므로, 이 필터가 없으면
        # 삭제된(그래서 화면에는 안 보이는) 작업이 여기서 다시 주워져 실행 큐에
        # 들어가는 사고가 난다.
        pending = sorted(
            (j for j in jobs.values() if j["status"] == "pending" and not j.get("deleted")),
            key=lambda j: j["queued_at"],
        )
        started = []
        for job in pending:
            assigned = job.get("pod_id")
            if not assigned:
                continue   # 파드를 안 정한 작업은 스케줄러가 갖춰진 파드를 찾아 배정한다(start_queue가 대기 큐로 보낸다)
            if assigned not in targets:
                owner = pod_registry.get_pod(assigned) if assigned else None
                if owner is not None and owner.get("enabled"):
                    continue   # 그 파드가 살아 있다 — 그쪽을 켤 때 돈다
                # 갈 곳이 없어진 작업. 같은 주인의 파드로만, 그리고 되돌릴 파드의 종류가 맞을 때만 옮긴다 —
                # 남의 파드로 옮기면 안 되고, 셸 작업을 ComfyUI 파드에 넣으면(그 반대도) 조용히 엉뚱하게 돈다.
                mine = [pid for pid in pod_ids if pod_owner.get(pid) == job.get("owner_id")]
                if not mine:
                    continue
                fallback = mine[0]
                fallback_kind = (pod_registry.get_pod(fallback) or {}).get("kind")
                allowed = (templates.get(job["template_id"], {}).get("pod_kinds")
                           or [pod_registry.DEFAULT_KIND])
                if fallback_kind not in allowed:
                    continue
                assigned = fallback
                job["pod_id"] = assigned
            job["status"] = "queued"
            started.append((job["id"], assigned))
    save_state()
    for job_id, pod_id in started:
        dispatch_job(job_id, pod_id)
    return len(started)


# ---- 프로젝트 ------------------------------------------------------------------------
# 프로젝트는 파드와 무관하게 작업(job)과 결과물(asset)을 묶는다. project_id가 없는 것은
# "미분류". 프로젝트를 지워도 그 안의 작업/결과물은 지워지지 않고 미분류로 돌아온다.

def _clean_project_fields(body: dict, creating: bool) -> dict:
    fields: dict = {}
    if "name" in body or creating:
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(400, "프로젝트 이름이 필요해요.")
        fields["name"] = name.strip()
    if "description" in body:
        if not isinstance(body["description"], str):
            raise HTTPException(400, "description은 문자열이어야 해요.")
        fields["description"] = body["description"]
    if "defaults" in body:
        if not isinstance(body["defaults"], dict):
            raise HTTPException(400, "defaults는 JSON 객체여야 해요.")
        fields["defaults"] = body["defaults"]
    if "archived" in body:
        fields["archived"] = bool(body["archived"])
    if "is_mature" in body:
        fields["is_mature"] = bool(body["is_mature"])
    if "cover_asset_id" in body:
        cover = body["cover_asset_id"]
        if cover is not None and (not isinstance(cover, int) or isinstance(cover, bool)):
            raise HTTPException(400, "cover_asset_id는 숫자 또는 null이어야 해요.")
        fields["cover_asset_id"] = cover
    return fields


def project_or_404(user: dict, project_id: int) -> dict:
    project = project_store.get_project(project_id)
    if project is None or not auth.can_access(user, project.get("owner_id")):
        raise HTTPException(404, "없는 프로젝트예요.")
    return project


@app.get("/api/projects")
def list_projects_api(request: Request, include_archived: bool = False):
    user = me(request)
    scope = job_scope(user)
    projects = project_store.list_projects(include_archived, owner_id=scope)
    if scope is None:   # 관리자에게는 누구 프로젝트인지 알려 준다
        names = auth.usernames()
        projects = [{**p, "owner_name": names.get(p.get("owner_id"))} for p in projects]
    return {"projects": projects, "unassigned": project_store.unassigned_summary(owner_id=scope)}


@app.post("/api/projects")
async def create_project_api(request: Request):
    user = me(request)
    body = await read_json_object(request, allow_empty=False)
    fields = _clean_project_fields(body, creating=True)
    return project_store.create_project(fields["name"], fields.get("description", ""), fields.get("defaults"),
                                        owner_id=user["id"], is_mature=fields.get("is_mature", False))


@app.get("/api/projects/{project_id}")
def get_project_api(project_id: int, request: Request):
    return project_or_404(me(request), project_id)


@app.patch("/api/projects/{project_id}")
async def update_project_api(project_id: int, request: Request):
    project_or_404(me(request), project_id)
    body = await read_json_object(request, allow_empty=False)
    fields = _clean_project_fields(body, creating=False)
    if fields.get("cover_asset_id") is not None and not project_store.cover_candidate_ok(fields["cover_asset_id"], project_id):
        raise HTTPException(400, "이 프로젝트에 들어 있는 이미지만 대표 이미지로 고를 수 있어요.")
    project = project_store.update_project(project_id, fields)
    if project is None:
        raise HTTPException(404, "없는 프로젝트예요.")
    return project


@app.delete("/api/projects/{project_id}")
def delete_project_api(project_id: int, request: Request):
    project_or_404(me(request), project_id)
    if not project_store.delete_project(project_id):
        raise HTTPException(404, "없는 프로젝트예요.")
    # DB에서는 FK가 알아서 미분류로 돌렸지만 메모리 jobs dict는 그대로라 같이 맞춘다.
    with lock:
        for job in jobs.values():
            if job.get("project_id") == project_id:
                job["project_id"] = None
    save_state()
    return {"ok": True}


BOARD_NODE_KINDS = ("image", "video", "text", "job", "frame")   # frame = 묶음 틀(제목은 text)
BOARD_ACTIVE_JOB_STATUSES = ("pending", "queued", "running")   # 화면이 작업 카드를 계속 새로 받는 상태


def _board_node_or_404(project_id: int, node_id: int) -> None:
    """그 노드가 이 프로젝트 것인지 확인 — project_or_404가 이미 프로젝트 소유권을
    확인했으므로, 여기서는 node_id가 그 project_id 밑에 실제로 있는지만 본다."""
    if board_store.node_project_id(node_id) != project_id:
        raise HTTPException(404, "없는 카드예요.")


@app.get("/api/projects/{project_id}/board")
def get_board_api(project_id: int, request: Request):
    project_or_404(me(request), project_id)
    return board_store.list_board(project_id)


@app.post("/api/projects/{project_id}/board/nodes")
async def create_board_node_api(project_id: int, request: Request):
    user = me(request)
    project_or_404(user, project_id)
    body = await read_json_object(request, allow_empty=False)
    kind = body.get("kind")
    if kind not in BOARD_NODE_KINDS:
        raise HTTPException(400, f"kind는 {'/'.join(BOARD_NODE_KINDS)} 중 하나여야 해요.")
    asset_path = _board_asset_path_or_error(user, kind, body.get("asset_path"))
    job_id = None
    if kind == "job":
        job_id = str(body.get("job_id") or "").strip()
        if not job_id:
            raise HTTPException(400, "작업 카드는 job_id가 필요해요.")
        job_or_404(user, job_id)   # 자기(admin은 전부) 작업만 — 남의 작업 id를 짐작해 끌어오지 못하게
    try:
        x = float(body.get("x", 0))
        y = float(body.get("y", 0))
        width = float(body["width"]) if body.get("width") is not None else None
        height = float(body["height"]) if body.get("height") is not None else None
    except (TypeError, ValueError):
        raise HTTPException(400, "x/y/width/height는 숫자여야 해요.")
    text = str(body.get("text") or "")
    return board_store.create_node(project_id, kind, asset_path, text, x, y, width, height, job_id=job_id)


def _board_asset_path_or_error(user, kind: str, raw) -> str | None:
    """이미지/영상 카드가 가리킬 결과물 경로를 확인한다(텍스트 카드는 None).
    자기 소유의(관리자는 전부) 결과물만 카드로 놓을 수 있다 — 다른 회원의 파일
    경로를 짐작해 끌어오지 못하게 한다. 꼭 "이" 프로젝트 소속일 필요는 없다(다른
    프로젝트의 이미지를 참고 삼아 가져오는 것도 자연스러운 쓰임). 되살리기(restore)도
    같은 확인을 거친다 — 되살리기 요청에 남의 경로를 끼워 넣지 못하게."""
    if kind not in ("image", "video"):
        return None
    asset_path = (raw or "").strip()
    if not asset_path:
        raise HTTPException(400, "이미지/영상 카드는 asset_path가 필요해요.")
    _sync_assets_quietly()
    try:
        asset_meta.get_detail(asset_path, owner_id=auth.owner_scope(user))
    except asset_meta.AssetNotFound:
        raise HTTPException(404, "결과물을 찾을 수 없어요.")
    return asset_path


@app.patch("/api/projects/{project_id}/board/nodes/{node_id}")
async def update_board_node_api(project_id: int, node_id: int, request: Request):
    project_or_404(me(request), project_id)
    _board_node_or_404(project_id, node_id)
    body = await read_json_object(request, allow_empty=False)
    fields = {}
    try:
        for key in ("x", "y", "width", "height"):
            if key in body:
                fields[key] = float(body[key])
    except (TypeError, ValueError):
        raise HTTPException(400, "x/y/width/height는 숫자여야 해요.")
    if "text" in body:
        fields["text"] = str(body["text"] or "")
    node = board_store.update_node(node_id, fields)
    if node is None:
        raise HTTPException(404, "없는 카드예요.")
    return node


@app.delete("/api/projects/{project_id}/board/nodes/{node_id}")
def delete_board_node_api(project_id: int, node_id: int, request: Request):
    project_or_404(me(request), project_id)
    _board_node_or_404(project_id, node_id)
    if not board_store.delete_node(node_id):
        raise HTTPException(404, "없는 카드예요.")
    # 이 카드에 붙은 연결선은 DB가 같이 지운다(board_edges의 ON DELETE CASCADE).
    return {"ok": True}


def _board_ids(raw, what: str) -> list:
    if not isinstance(raw, list) or not raw or len(raw) > 1000:
        raise HTTPException(400, f"{what}는 1~1000개짜리 목록이어야 해요.")
    try:
        return [int(v) for v in raw]
    except (TypeError, ValueError):
        raise HTTPException(400, f"{what}는 숫자 목록이어야 해요.")


@app.patch("/api/projects/{project_id}/board/nodes")
async def move_board_nodes_api(project_id: int, request: Request):
    """여러 카드를 한 번에 옮긴다 — `{"nodes": [{"id", "x", "y"}, ...]}`. 여러 장 선택해 끌었을
    때와 되돌리기에서 쓴다. 한 장이라도 이 프로젝트 카드가 아니면 아무것도 안 바꾼다."""
    project_or_404(me(request), project_id)
    body = await read_json_object(request, allow_empty=False)
    items = body.get("nodes")
    if not isinstance(items, list) or not items or len(items) > 1000:
        raise HTTPException(400, "nodes는 1~1000개짜리 목록이어야 해요.")
    try:
        moves = [(int(it["id"]), float(it["x"]), float(it["y"])) for it in items]
    except (TypeError, ValueError, KeyError):
        raise HTTPException(400, "nodes의 각 항목에는 숫자 id/x/y가 있어야 해요.")
    try:
        board_store.move_nodes(project_id, moves)
    except board_store.BoardNotFound:
        raise HTTPException(404, "없는 카드가 섞여 있어요.")
    return {"ok": True}


@app.post("/api/projects/{project_id}/board/nodes/delete")
async def delete_board_nodes_api(project_id: int, request: Request):
    """여러 카드를 한 번에 지운다 — `{"ids": [...]}`(DELETE는 본문을 못 믿어서 POST). 붙은 선도 같이."""
    project_or_404(me(request), project_id)
    body = await read_json_object(request, allow_empty=False)
    ids = _board_ids(body.get("ids"), "ids")
    try:
        board_store.delete_nodes(project_id, ids)
    except board_store.BoardNotFound:
        raise HTTPException(404, "없는 카드가 섞여 있어요.")
    return {"ok": True}


@app.post("/api/projects/{project_id}/board/restore")
async def restore_board_api(project_id: int, request: Request):
    """지운 카드·선을 되살린다(되돌리기) — `{"nodes": [...], "edges": [...]}`. 각 카드는 지우기
    전에 받아 둔 카드 그대로(id 포함), 선은 `{id, from_node_id, to_node_id}`. 원래 id를 되도록
    그대로 쓰고, 못 쓰면 `id_map`(옛 id → 새 id)으로 알려 준다. 선은 보낸 순서대로 돌려준다."""
    user = me(request)
    project_or_404(user, project_id)
    body = await read_json_object(request, allow_empty=False)
    raw_nodes = body.get("nodes") or []
    raw_edges = body.get("edges") or []
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list) or not (raw_nodes or raw_edges)             or len(raw_nodes) > 1000 or len(raw_edges) > 5000:
        raise HTTPException(400, "nodes/edges 목록이 필요해요.")
    nodes = []
    try:
        for n in raw_nodes:
            kind = n.get("kind")
            if kind not in BOARD_NODE_KINDS:
                raise HTTPException(400, f"kind는 {'/'.join(BOARD_NODE_KINDS)} 중 하나여야 해요.")
            nodes.append({
                "id": int(n["id"]) if n.get("id") is not None else None,
                "kind": kind,
                "asset_path": _board_asset_path_or_error(user, kind, n.get("asset_path")),
                "job_id": _board_restore_job_id(user, kind, n.get("job_id")),
                "text": str(n.get("text") or ""),
                "x": float(n["x"]), "y": float(n["y"]),
                "width": float(n.get("width") or 220), "height": float(n.get("height") or 220),
                "z_index": int(n.get("z_index") or 0),
                "created_at": str(n["created_at"]) if n.get("created_at") else None,
            })
        edges = [{"id": int(e["id"]) if e.get("id") is not None else None,
                  "from_node_id": int(e["from_node_id"]), "to_node_id": int(e["to_node_id"]),
                  "created_at": str(e["created_at"]) if e.get("created_at") else None} for e in raw_edges]
    except (TypeError, ValueError, KeyError, AttributeError):
        raise HTTPException(400, "되살릴 카드/선 형식이 맞지 않아요.")
    try:
        return board_store.restore(project_id, nodes, edges)
    except board_store.BoardNotFound:
        raise HTTPException(404, "선의 양 끝 카드가 이 보드에 없어요.")


def _board_restore_job_id(user, kind: str, raw) -> str | None:
    """되살릴 작업 카드의 job_id. 그 사이 작업이 아예 사라졌으면 카드는 "지워진 작업"으로
    살린다(None). 남아 있는데 남의 작업이면 404 — 되살리기 요청에 남의 작업을 끼워 넣지 못하게."""
    if kind != "job" or not raw:
        return None
    job_id = str(raw)
    with lock:
        exists = job_id in jobs
    if not exists:
        return None
    job_or_404(user, job_id)
    return job_id


@app.get("/api/projects/{project_id}/board/jobs")
def board_jobs_api(project_id: int, request: Request):
    """보드의 작업 카드마다 지금 상태 — `{"cards": {node_id: {...}}}`. 화면이 작업 카드를 그릴 때와,
    대기·실행 중인 작업이 있는 동안 몇 초마다 부른다. 카드마다 status/template_label/prompt/progress/
    시각/파드와 최근 결과물 4개(results: [{path, kind, nsfw}])를 준다. 작업이 지워졌거나(휴지통 포함)
    볼 수 없으면 missing=true."""
    user = me(request)
    project_or_404(user, project_id)
    cards = board_store.job_nodes(project_id)
    if cards:
        _sync_assets_quietly()   # 방금 끝난 작업의 결과물이 색인에 올라오게(자체적으로 간격을 둔다)
    results = board_store.job_results(job_id for _, job_id in cards)
    out = {}
    with lock:
        for node_id, job_id in cards:
            job = jobs.get(job_id) if job_id else None
            if job is None or job.get("deleted") or not auth.can_access(user, job.get("owner_id")):
                out[node_id] = {"job_id": job_id, "missing": True}
                continue
            prompt = (job.get("options") or {}).get("main_prompt") or ""
            out[node_id] = {
                "job_id": job_id, "missing": False,
                "status": job.get("status"),
                "active": job.get("status") in BOARD_ACTIVE_JOB_STATUSES,
                "template_label": job.get("template_label") or job.get("template_id"),
                "prompt": str(prompt)[:300],
                "progress": job.get("progress"),
                "queued_at": job.get("queued_at"), "started_at": job.get("started_at"),
                "finished_at": job.get("finished_at"),
                "pod_name": job.get("pod_name"),
                "results": results.get(job_id, []),
            }
    return {"cards": out}


@app.post("/api/projects/{project_id}/board/edges")
async def create_board_edge_api(project_id: int, request: Request):
    project_or_404(me(request), project_id)
    body = await read_json_object(request, allow_empty=False)
    try:
        from_id = int(body.get("from_node_id"))
        to_id = int(body.get("to_node_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "from_node_id/to_node_id는 숫자여야 해요.")
    if from_id == to_id:
        raise HTTPException(400, "카드를 자기 자신과 이을 수는 없어요.")
    # 양 끝이 둘 다 이 프로젝트 카드여야 한다 — 다른 프로젝트 카드 id를 짐작해 잇지 못하게.
    owners = board_store.node_project_ids((from_id, to_id))
    if owners.get(from_id) != project_id or owners.get(to_id) != project_id:
        raise HTTPException(404, "없는 카드예요.")
    return board_store.create_edge(project_id, from_id, to_id)


@app.delete("/api/projects/{project_id}/board/edges/{edge_id}")
def delete_board_edge_api(project_id: int, edge_id: int, request: Request):
    project_or_404(me(request), project_id)
    if not board_store.delete_edge(project_id, edge_id):
        raise HTTPException(404, "없는 연결선이에요.")
    return {"ok": True}


@app.put("/api/jobs/{job_id}/project")
async def set_job_project(job_id: str, request: Request):
    # 작업을 다른 프로젝트로(또는 미분류로) 옮긴다 — 그 작업이 만든 결과물도 같이 옮겨간다.
    user = me(request)
    job = job_or_404(user, job_id)
    body = await read_json_object(request, allow_empty=False)
    if "project_id" not in body:
        raise HTTPException(400, "project_id가 필요해요(미분류로 옮기려면 null).")
    project_id = parse_project_id(body["project_id"])
    # 작업은 그 작업의 주인의 프로젝트로만 옮길 수 있다.
    if project_id is not None and not project_store.project_exists(project_id, owner_id=job.get("owner_id")):
        raise HTTPException(400, "없는 프로젝트예요.")
    with lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "없는 작업이에요.")
        job["project_id"] = project_id
        snapshot = dict(job)
    save_state()
    project_store.set_job_assets_project(job_id, project_id)
    return snapshot


def _split_tags(value: str | None) -> list[str]:
    return [t for t in (value or "").split(",") if t.strip()]


@app.get("/api/output-assets")
def list_assets_api(request: Request, project_id: str | None = None, kind: str | None = None,
                    job_id: str | None = None, favorite: bool | None = None,
                    q: str | None = None, tag: str | None = None, min_rating: int | None = None,
                    limit: int = 200, offset: int = 0):
    # 결과물 색인 조회 — project_id는 숫자 또는 "unassigned"(미분류). 프롬프트 등 PNG 메타데이터와
    # 태그·평점·메모까지 담아 온다. q는 공백으로 나눈 단어가 모두 (프롬프트·메모·태그·파일명·
    # 체크포인트·시드) 중 어딘가에 들어 있는 것만 남긴다.
    if kind not in (None, "image", "video"):
        raise HTTPException(400, "kind는 image 또는 video여야 해요.")
    project: str | int | None = None
    if project_id is not None:
        project = "unassigned" if project_id == "unassigned" else parse_project_id(project_id)
    scope = auth.owner_scope(me(request))
    _sync_assets_quietly()
    return {"assets": asset_meta.list_assets(q, _split_tags(tag), favorite, min_rating, kind, project, job_id,
                                             max(1, min(limit, 1000)), max(0, offset), owner_id=scope)}


def _asset_paths_from(body: dict) -> list[str]:
    paths = body.get("paths")
    if paths is None and isinstance(body.get("path"), str):
        paths = [body["path"]]
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p for p in paths):
        raise HTTPException(400, "paths(결과물 경로 목록)가 필요해요.")
    return paths


@app.get("/api/output-assets/detail")
def asset_detail_api(path: str, request: Request):
    # 라이트박스의 정보 패널용 — 프롬프트/시드/체크포인트/파라미터와 메모·평점·태그 전부.
    scope = auth.owner_scope(me(request))
    _sync_assets_quietly()
    try:
        return asset_meta.get_detail(path, owner_id=scope)
    except asset_meta.AssetNotFound:
        raise HTTPException(404, "결과물을 찾을 수 없어요.")


@app.post("/api/output-assets/update")
async def update_assets_api(request: Request):
    scope = auth.owner_scope(me(request))
    # 즐겨찾기/평점/메모를 바꾼다(paths 여러 개면 전부 같은 값으로). 메모는 한 장씩만.
    body = await read_json_object(request, allow_empty=False)
    paths = _asset_paths_from(body)
    if "note" in body and len(paths) > 1:
        raise HTTPException(400, "메모는 결과물 한 장씩만 바꿀 수 있어요.")
    if "note" in body and not isinstance(body["note"], str):
        raise HTTPException(400, "note는 문자열이어야 해요.")
    if "favorite" in body and not isinstance(body["favorite"], bool):
        raise HTTPException(400, "favorite은 true/false여야 해요.")
    if "nsfw" in body and not isinstance(body["nsfw"], bool):
        raise HTTPException(400, "nsfw는 true/false여야 해요.")
    kwargs = {}
    if "favorite" in body:
        kwargs["favorite"] = body["favorite"]
    if "nsfw" in body:
        kwargs["nsfw"] = body["nsfw"]
    if "rating" in body:
        r = body["rating"]
        if r is not None and (not isinstance(r, int) or isinstance(r, bool)):
            raise HTTPException(400, "rating은 0~5 숫자여야 해요.")
        kwargs["rating"] = r
    if "note" in body:
        kwargs["note"] = body["note"]
    try:
        changed = await asyncio.to_thread(asset_meta.update_assets, paths, owner_id=scope, **kwargs)
    except asset_meta.AssetNotFound:
        raise HTTPException(404, "결과물을 찾을 수 없어요.")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"updated": changed}


@app.post("/api/output-assets/move")
async def move_assets_api(request: Request):
    scope = auth.owner_scope(me(request))
    # 결과물(이미지/영상)을 다른 프로젝트로 옮긴다 — project_id가 null이면 미분류. 파일은 그대로다.
    body = await read_json_object(request, allow_empty=False)
    paths = _asset_paths_from(body)
    if "project_id" not in body:
        raise HTTPException(400, "project_id가 필요해요(미분류로 옮기려면 null).")
    project_id = parse_project_id(body["project_id"])
    if project_id is not None and not project_store.project_exists(project_id, owner_id=scope):
        raise HTTPException(400, "없는 프로젝트예요.")
    try:
        moved = await asyncio.to_thread(asset_meta.move_assets, paths, project_id, scope)
    except asset_meta.AssetNotFound:
        raise HTTPException(404, "결과물을 찾을 수 없어요.")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"moved": moved, "project_id": project_id}


@app.post("/api/output-assets/tags")
async def change_asset_tags_api(request: Request):
    scope = auth.owner_scope(me(request))
    # 태그를 붙이고(add)/떼고(remove) 바뀐 결과물의 태그 목록을 돌려준다.
    body = await read_json_object(request, allow_empty=False)
    paths = _asset_paths_from(body)
    for key in ("add", "remove"):
        if key in body and not (isinstance(body[key], list) and all(isinstance(t, str) for t in body[key])):
            raise HTTPException(400, f"{key}는 문자열 목록이어야 해요.")
    try:
        tags = await asyncio.to_thread(asset_meta.change_tags, paths, body.get("add"), body.get("remove"), scope)
    except asset_meta.AssetNotFound:
        raise HTTPException(404, "결과물을 찾을 수 없어요.")
    return {"tags": tags}


@app.get("/api/tags")
def list_tags_api(request: Request):
    # 태그 자동완성/필터 후보 — 결과물에 붙은 개수 순.
    return {"tags": asset_meta.list_tags(owner_id=auth.owner_scope(me(request)))}


@app.post("/api/queue/start")
def start_queue(request: Request, project_id: str | None = None):
    # 자동 실행 모드를 켠다 — 지금 대기 중인 작업을 전부 큐에 넣는 것은 물론,
    # 켜져 있는 동안 POST /api/upload로 새로 추가되는 작업도 계속 이어서 큐에
    # 들어간다. "⏸ 정지"를 누르기 전까지는 계속 켜져 있다.
    # 파드가 여러 개면 **사용 중인 파드 전부**를 켠다(화면의 "▶ 시작" 버튼 하나가
    # 전체를 켜는 것과 같다). 하나만 켜고 끄려면 /api/pods/{id}/queue/start를 쓴다.
    user = me(request)
    wanted_project = None
    if project_id is not None:
        wanted_project = "unassigned" if project_id == "unassigned" else parse_project_id(project_id)
    pod_ids = [p["id"] for p in pod_registry.list_pods(user["id"]) if p.get("enabled")]   # 내 파드만
    started = start_pods(pod_ids) if project_id is None else 0
    # 파드를 안 정한 채 쌓아 둔 작업은 대기 큐로 보낸다 — 스케줄러가 필요한 모델을 갖춘 파드가 살아 있을 때 배정한다.
    with lock:
        sent = [j for j in jobs.values() if j["status"] == "pending" and not j.get("deleted")
                and not j.get("pod_id") and j.get("owner_id") == user["id"]
                and (wanted_project is None
                     or j.get("project_id") == (None if wanted_project == "unassigned" else wanted_project))]
        for job in sent:
            job["status"] = "queued"
            job["waiting_reason"] = "파드를 찾는 중이에요"
    if sent:
        save_state()
    poke_scheduler()
    return {"running": True, "started": started + len(sent), "pods": pod_ids}


@app.post("/api/queue/stop")
def stop_queue(request: Request, project_id: str | None = None):
    # 자동 실행 모드를 끈다 — 이후 새로 추가되는 작업은 다시 "▶ 시작"을 누르기
    # 전까지 pending 상태로 대기 목록에만 쌓인다. 이미 큐에 들어가 있지만 아직 안 돈
    # 작업(queued)은 그대로 대기 상태로 남고(다음 "▶ 시작" 때 이어서 돎), 지금
    # 실행 중인(running) 작업들은 즉시 종료 요청한다 — 완전히 죽을 때까지 몇 초
    # 걸릴 수 있으니 job 상태가 "interrupted"로 바뀌는 건 GET /api/jobs로 잠시 후
    # 확인해야 한다.
    user = me(request)
    wanted_project = None
    if project_id is not None:
        wanted_project = "unassigned" if project_id == "unassigned" else parse_project_id(project_id)
    my_pod_ids = [p["id"] for p in pod_registry.list_pods(user["id"])] if project_id is None else []   # 내 파드만 멈춘다
    stopped: list[str] = []
    with lock:
        for pid in my_pod_ids:
            rt = pod_runtimes.get(pid)
            if rt is not None:
                rt.auto_run = False
        # 파드를 기다리던 작업은 대기 목록(pending)으로 되돌려 배정이 멈추게 한다 — 다음 "▶ 시작" 때 다시 대기 큐로 간다.
        for job in jobs.values():
            if (job["status"] == "queued" and not job.get("pod_id") and not job.get("deleted")
                    and job.get("owner_id") == user["id"]
                    and (wanted_project is None
                         or job.get("project_id") == (None if wanted_project == "unassigned" else wanted_project))):
                job["status"] = "pending"
                job["waiting_reason"] = None
    save_state()
    for pid in my_pod_ids:
        stopped.extend(stop_pod_jobs(pid))
    return {"running": False, "stopped_job_ids": stopped,
            "stopped_job_id": stopped[0] if stopped else None}


@app.post("/api/pods/{pod_id}/queue/start")
def start_pod_queue(pod_id: str, request: Request):
    """이 파드만 켠다 — 파드가 여러 대일 때 한 대씩 굴리기 위한 것."""
    pod = pod_or_404(me(request), pod_id)
    if not pod.get("enabled"):
        raise HTTPException(400, f"'{pod['name']}' 파드는 지금 사용 안 함 상태예요.")
    ensure_runtime(pod)
    return {"pod_id": pod_id, "running": True, "started": start_pods([pod_id])}


@app.post("/api/pods/{pod_id}/queue/stop")
def stop_pod_queue(pod_id: str, request: Request):
    """이 파드만 멈춘다. 다른 파드는 계속 돈다."""
    pod_or_404(me(request), pod_id)
    with lock:
        rt = pod_runtimes.get(pod_id)
        if rt is not None:
            rt.auto_run = False
    return {"pod_id": pod_id, "running": False, "stopped_job_ids": stop_pod_jobs(pod_id)}


@app.post("/api/jobs/clear-completed")
def clear_completed_jobs(request: Request, pod_id: str | None = None, project_id: str | None = None):
    # 다 끝난 작업(성공/실패/중단)을 목록에서 한꺼번에 치우고 싶을 때 쓴다 —
    # delete_job()과 같은 소프트 삭제라 "삭제된 작업 설정 불러오기"로 실수로
    # 지운 작업도 되돌릴 수 있다. pending/queued/running은 여기서 건드리지
    # 않는다 — 아직 시작 안 했거나 진행 중인 작업까지 같이 지우면 안 되므로.
    #
    # pod_id를 주면 그 파드의 작업만 치운다. 화면의 작업 목록이 파드 하나 것만
    # 보여주므로("#pod/{id}/jobs"), 거기 있는 "완료 삭제"가 화면에 보이지도 않는
    # 다른 파드의 작업까지 지워버리면 안 된다. 프로젝트 화면의 "완료 삭제"도 같은 이유로
    # project_id를 주면 그 프로젝트 것만("unassigned"면 미분류) 치운다.
    scope = job_scope(me(request))
    wanted_project = None
    if project_id is not None:
        wanted_project = "unassigned" if project_id == "unassigned" else parse_project_id(project_id)
    with lock:
        completed = [j for j in jobs.values()
                     if j["status"] in ("done", "failed", "interrupted") and not j.get("deleted")
                     and owned(j, scope)
                     and (pod_id is None or j.get("pod_id") == pod_id)
                     and (wanted_project is None
                          or j.get("project_id") == (None if wanted_project == "unassigned" else wanted_project))]
        now = now_iso()
        for job in completed:
            job["deleted"] = True
            job["deleted_at"] = now
        prune_deleted_jobs()
    save_state()
    return {"cleared": len(completed)}


@app.get("/api/jobs")
def list_jobs(request: Request, project_id: str | None = None):
    # project_id를 주면 그 프로젝트 것만("unassigned"면 미분류) — 안 주면 전부(내 것 전부, admin은 모두의 것).
    user = me(request)
    scope = job_scope(user)
    wanted = None
    if project_id is not None:
        wanted = "unassigned" if project_id == "unassigned" else parse_project_id(project_id)
    names = auth.usernames() if scope is None else {}
    visible_pod_ids = {p["id"] for p in visible_pods_for(user)}
    with lock:
        ordered = sorted(
            (j for j in jobs.values() if not j.get("deleted") and owned(j, scope)
             and (wanted is None or j.get("project_id") == (None if wanted == "unassigned" else wanted))),
            key=lambda j: j["queued_at"],
            reverse=True,
        )
        if scope is None:   # 관리자에게는 누구 작업인지 알려 준다
            ordered = [{**j, "owner_name": names.get(j.get("owner_id"))} for j in ordered]
        # 화면의 "▶ 시작/⏸ 정지" 버튼 하나는 "하나라도 돌고 있으면 켜진 것"으로 본다.
        my_runtimes = {pid: rt for pid, rt in pod_runtimes.items() if pid in visible_pod_ids}
        waiting_count = sum(1 for j in jobs.values() if j["status"] == "queued" and not j.get("pod_id")
                            and not j.get("deleted") and owned(j, scope))
        running = any(rt.auto_run for rt in my_runtimes.values()) or waiting_count > 0
        pending_count = sum(rt.queue.qsize() for rt in my_runtimes.values()) + waiting_count
        per_pod = {
            pid: {"running": rt.auto_run, "pending_count": rt.queue.qsize(),
                  "running_jobs": list(rt.running)}
            for pid, rt in my_runtimes.items()
        }
    return {"jobs": ordered, "pending_count": pending_count, "running": running,
            "pods": per_pod}


@app.get("/api/jobs/deleted")
def list_deleted_jobs(request: Request):
    # "작업 목록"에서 삭제한 작업들 — 상단의 "삭제된 작업 설정 불러오기" 드롭다운을
    # 채우는 용도. 소프트 삭제이므로 워크플로우/CSV는 prune_deleted_jobs()가 지우기
    # 전까지 GET /api/jobs/{id}/workflow·csv로 계속 읽을 수 있다.
    scope = job_scope(me(request))
    with lock:
        ordered = sorted(
            (j for j in jobs.values() if j.get("deleted") and owned(j, scope)),
            key=lambda j: j.get("deleted_at") or "",
            reverse=True,
        )
    return {"jobs": ordered, "retention": DELETED_JOBS_RETENTION}


@app.get("/api/jobs/{job_id}/log")
def job_log(job_id: str, request: Request, tail: int = 200):
    job_or_404(me(request), job_id)
    log_path = LOGS_DIR / f"{job_id}.log"
    if not log_path.exists():
        return {"log": ""}
    lines = log_path.read_text(errors="replace").splitlines()
    return {"log": "\n".join(lines[-tail:])}


@app.get("/api/jobs/{job_id}/text-result")
def job_text_result(job_id: str, request: Request):
    """이미지가 아니라 글을 만드는 워커(claude_writer)의 결과 — 템플릿이 직접
    NIGHTSHIFT_OUTPUT_DIR/<job_id>/output.md에 써 둔 것을 그대로 읽어 돌려준다.
    파드 화면의 "📝 결과" 탭이 이걸 부른다. 파일이 없으면(아직 실행 전/실패)
    빈 문자열 — 로그(job_log)를 보라고 굳이 에러를 내지 않는다."""
    job_or_404(me(request), job_id)
    path = Path(OUTPUT_DIR) / job_id / "output.md"
    if not path.is_file():
        return {"text": ""}
    return {"text": path.read_text(encoding="utf-8", errors="replace")}


@app.put("/api/jobs/{job_id}/progress")
async def update_job_progress(job_id: str, request: Request):
    # 실행 중인 템플릿 스크립트가 자기 진행 상황(전체/완료 이미지 수)을 스스로 보고하는
    # 용도. 매 이미지마다 호출될 수 있어 디스크 쓰기(save_state)는 하지 않고 메모리만 갱신한다
    # — 서버가 재시작되면 어차피 그 작업은 interrupted 처리되어 진행률의 의미가 없어진다.
    # 작업 스크립트(내부 토큰) 아니면 관리자만 — 진행률을 남이 바꿀 이유가 없다.
    if not request.state.internal:
        admin_only(request)
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


def read_job_attachment(job_id: str, field: str, missing_msg: str, user: dict | None = None) -> str:
    if user is not None:
        job_or_404(user, job_id)
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
    job_or_404(me(request), job_id)
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
def get_job_workflow(job_id: str, request: Request):
    text = read_job_attachment(job_id, "workflow_filename", "워크플로우 파일이 없어요.", me(request))
    return Response(content=text, media_type="application/json")


@app.put("/api/jobs/{job_id}/workflow")
async def update_job_workflow(job_id: str, request: Request):
    await write_job_attachment(job_id, "workflow_filename", request, validate_json_text, "워크플로우 파일이 없어요.")
    return {"ok": True}


@app.get("/api/jobs/{job_id}/video-workflow")
def get_job_video_workflow(job_id: str, request: Request):
    text = read_job_attachment(job_id, "video_workflow_filename", "영상 생성 워크플로우 파일이 없어요.", me(request))
    return Response(content=text, media_type="application/json")


@app.put("/api/jobs/{job_id}/video-workflow")
async def update_job_video_workflow(job_id: str, request: Request):
    await write_job_attachment(job_id, "video_workflow_filename", request, validate_json_text, "영상 생성 워크플로우 파일이 없어요.")
    return {"ok": True}


@app.get("/api/jobs/{job_id}/csv")
def get_job_csv(job_id: str, request: Request):
    text = read_job_attachment(job_id, "csv_filename", "CSV 파일이 없어요.", me(request))
    return Response(content=text, media_type="text/csv")


@app.put("/api/jobs/{job_id}/csv")
async def update_job_csv(job_id: str, request: Request):
    await write_job_attachment(job_id, "csv_filename", request, validate_csv_text, "CSV 파일이 없어요.")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str, request: Request):
    job_or_404(me(request), job_id)
    # 서버가 재시작되면서 queued/running이던 작업이 interrupted로 남았을 때, 처음부터
    # 새로 등록할 필요 없이 그 자리에서 다시 큐에 올린다. 워크플로우/CSV/옵션은 이미
    # JOBS_DIR에 남아있는 원래 값을 그대로 재사용한다. auto_run(▶ 시작/⏸ 정지) 상태와
    # 무관하게 이 버튼은 항상 즉시 큐에 넣는다 — 사용자가 특정 작업을 콕 집어 "지금
    # 다시 시도"하는 명시적 동작이기 때문이다.
    with lock:
        job = jobs.get(job_id)
        if not job or job.get("deleted"):
            raise HTTPException(404, "없는 작업이에요.")
        if job["status"] != "interrupted":
            raise HTTPException(400, "서버 재시작으로 중단된 작업만 재시작할 수 있어요.")
        job["status"] = "queued"
        job["queued_at"] = now_iso()
        job["started_at"] = None
        job["finished_at"] = None
        job["returncode"] = None
        job["progress"] = None
    save_state()
    dispatch_job(job_id)
    return jobs[job_id]


@app.post("/api/jobs/{job_id}/start")
def start_job(job_id: str, request: Request):
    # Job List 행의 "시작" — auto_run(전체 큐 자동 실행)과 무관하게 이 작업 하나만 콕
    # 집어 지금 돌린다. retry_job과 거의 같지만 pending/failed까지 넓힌 버전이다.
    # pod_id가 이미 있으면(정지됨/실패 — 예전에 어느 파드에서 돌았는지 앎) retry와
    # 똑같이 dispatch_job으로 그 파드 큐에 바로 넣는다. pod_id가 없으면(pending, 한
    # 번도 배정된 적 없음) dispatch_job으로 바로 넣지 않는다 — 그러면 스케줄러의
    # 모델/노드 적합성 검사를 건너뛰고 아무 기본 파드에나 꽂힌다. 대신
    # POST /api/queue/start와 같은 방식(queued + poke_scheduler)으로 보내 스케줄러가
    # 맞는 파드를 골라 배정하게 한다.
    job_or_404(me(request), job_id)
    with lock:
        job = jobs.get(job_id)
        if not job or job.get("deleted"):
            raise HTTPException(404, "없는 작업이에요.")
        if job["status"] not in ("pending", "interrupted", "failed"):
            raise HTTPException(400, "대기/정지/실패 상태의 작업만 시작할 수 있어요.")
        had_pod = bool(job.get("pod_id"))
        job["status"] = "queued"
        job["queued_at"] = now_iso()
        job["started_at"] = None
        job["finished_at"] = None
        job["returncode"] = None
        job["progress"] = None
        if not had_pod:
            job["waiting_reason"] = "파드를 찾는 중이에요"
    save_state()
    if had_pod:
        dispatch_job(job_id)
    else:
        poke_scheduler()
    return jobs[job_id]


@app.post("/api/jobs/{job_id}/pause")
def pause_job(job_id: str, request: Request):
    # 작업 카드의 ⏸ — 대기 큐에서 기다리는(queued) 작업 하나를 다시 일시정지(pending)로 돌린다.
    # 빈 파드가 생겨도 시작되지 않는다. 이미 파드 큐에 들어가 있어도 워커가 꺼낼 때 상태를 보고
    # 버리므로(wait_for_pod) 따로 빼낼 필요가 없다. 스케줄러가 골라 준 파드는 풀어서 다시 ▶할 때
    # 그 시점에 갖춰진 파드를 새로 찾게 하고, 사람이 고정한 파드(pinned_pod_id)는 그대로 둔다.
    job_or_404(me(request), job_id)
    with lock:
        job = jobs.get(job_id)
        if not job or job.get("deleted"):
            raise HTTPException(404, "없는 작업이에요.")
        if job["status"] != "queued":
            raise HTTPException(400, "대기 중인 작업만 일시정지할 수 있어요(실행 중이면 정지를 쓰세요).")
        job["status"] = "pending"
        job["waiting_reason"] = None
        job["missing_models"] = None
        if job.get("pod_id") and not job.get("pinned_pod_id"):
            job["pod_id"] = None
    save_state()
    return jobs[job_id]


def _registry_entry_for(name: str) -> dict | None:
    """없는 모델 이름(ComfyUI가 쓰는 하위 폴더 포함 경로)으로 등록부 항목을 찾는다 — 정확히 같은 이름이
    먼저, 없으면 파일 이름(마지막 경로 조각)이 같은 항목."""
    entries = model_registry.list_entries()
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    exact = [e for e in entries if e["filename"] == name]
    loose = [e for e in entries if e["filename"].replace("\\", "/").rsplit("/", 1)[-1] == base]
    for e in exact + loose:
        if e.get("download_url"):
            return e
    return (exact + loose or [None])[0]


def _watch_model_downloads(pod: dict, job_id: str, names: list[str]) -> None:
    """"없는 모델 받기"로 시작한 다운로드가 끝날 때까지 지켜보다가, 끝날 때마다 파드의 설치 목록 캐시를
    비우고 스케줄러를 깨운다 — 그래야 사람이 모델 탭을 안 열어도 다 받는 즉시 작업이 시작된다."""
    pending = set(names)
    errors: list[str] = []
    deadline = time.monotonic() + 6 * 3600
    while pending and time.monotonic() < deadline:
        time.sleep(10)
        try:
            result = model_download.call_node(pod, "GET", "/nightshift/dl/status")
        except Exception:
            continue
        finished = False
        for item in result.get("downloads", []):
            fname = str(item.get("filename") or "")
            match = next((n for n in pending if n == fname or n.endswith("/" + fname) or fname.endswith("/" + n)), None)
            if match and item.get("status") in ("done", "error", "failed", "cancelled"):
                pending.discard(match)
                finished = True
                if item.get("status") != "done":
                    errors.append(f"{match}: {item.get('error') or item.get('status')}")
        if finished:
            ComfyUIDriver.invalidate_capabilities(pod["id"])
            poke_scheduler()
    with lock:
        job = jobs.get(job_id)
        if job is not None:
            job["fetching_models"] = None
            # 실패한 받기는 이유를 남겨 카드에 보인다(없으면 받는 중 표시만 사라지고 원래 대기 이유로
            # 돌아가 "받았는데 왜 또 없다고 하지?"가 된다). 시간 안에 안 끝난 것도 알린다.
            if pending:
                errors.append(f"{', '.join(sorted(pending))}: 6시간 안에 끝나지 않았어요")
            job["fetch_error"] = " · ".join(errors) or None
            if not errors and job.get("status") == "queued" and not job.get("pod_id"):
                # 다 받았으면 옛 "없는 것 — …"을 지운다 — 스케줄러가 새 설치 목록으로 다시 볼 때까지(최대 몇 초)
                # 카드에 옛 이유가 다시 보여 "받았는데 또 없다고?"처럼 깜빡였다. 아직 모자라면 스케줄러가 다시 적는다.
                job["missing_models"] = None
                job["waiting_reason"] = "모델을 다 받았어요 — 곧 시작해요"
    save_state()
    ComfyUIDriver.invalidate_capabilities(pod["id"])
    poke_scheduler()


@app.post("/api/jobs/{job_id}/fetch-missing")
async def fetch_missing_models(job_id: str, request: Request):
    """카드의 "없는 모델 받기" — 스케줄러가 적어 둔 missing_models(그 파드에 없는 모델)를 등록부의 다운로드
    주소로 그 파드에 받는다. 주소가 없는 모델은 받지 않고 알려 준다. 다 받으면 작업은 저절로 시작된다."""
    user = me(request)
    job_or_404(user, job_id)
    with lock:
        job = jobs.get(job_id)
        info = dict(job.get("missing_models") or {}) if job else {}
    if not info.get("names"):
        raise HTTPException(400, "받을 모델이 없어요(이미 갖춰졌거나 아직 확인 전이에요).")
    pod = _download_pod(user, info["pod_id"])
    token = (auth.get_secrets(user["id"]) or {}).get("civitai_token") or None
    started, no_url, failed = [], [], []
    for name in info["names"]:
        if name.startswith("노드 "):
            failed.append({"name": name, "detail": "커스텀 노드는 여기서 받을 수 없어요 — 파드에 직접 설치하세요."})
            continue
        entry = _registry_entry_for(name)
        if not entry or not entry.get("download_url"):
            no_url.append(name)
            continue
        body = {"url": entry["download_url"], "folder": entry["kind"], "filename": name, "overwrite": False,
                "headers": model_download.auth_header_for(entry["download_url"], token)}
        try:
            await asyncio.to_thread(model_download.call_node, pod, "POST", "/nightshift/dl/start", body)
            started.append(name)
        except model_download.DownloadError as e:
            failed.append({"name": name, "detail": str(e)})
    if started:
        with lock:
            job = jobs.get(job_id)
            if job is not None:
                job["fetching_models"] = {"pod_id": pod["id"], "names": started, "started_at": now_iso()}
                job["fetch_error"] = None
        save_state()
        threading.Thread(target=_watch_model_downloads, args=(pod, job_id, started), daemon=True).start()
    return {"pod_id": pod["id"], "started": started, "no_url": no_url, "failed": failed}


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str, request: Request):
    # Job List 행의 "정지" — 그 파드의 auto_run이나 같은 파드에서 같이 도는 다른 작업은
    # 안 건드리고, 이 작업 하나의 서브프로세스만 종료 요청한다(stop_pod_jobs와 같은
    # terminate → 5초 뒤 감시 스레드로 kill 방식을 이 작업 하나에만 좁혀 적용).
    job_or_404(me(request), job_id)
    with lock:
        job = jobs.get(job_id)
        if not job or job.get("deleted"):
            raise HTTPException(404, "없는 작업이에요.")
        if job["status"] != "running":
            raise HTTPException(400, "진행 중인 작업만 정지할 수 있어요.")
        pod_id = job.get("pod_id")
        rt = pod_runtimes.get(pod_id) if pod_id else None
        proc = rt.running.get(job_id) if rt else None
        if proc is not None:
            job["_stop_requested"] = True
    if proc is None:
        raise HTTPException(409, "지금 실행 중인 프로세스를 찾지 못했어요(막 끝났을 수 있어요) — 잠시 후 다시 확인해 주세요.")
    proc.terminate()

    def _kill_if_still_alive(p=proc):
        time.sleep(5)
        if p.poll() is None:
            p.kill()

    threading.Thread(target=_kill_if_still_alive, daemon=True).start()
    return {"ok": True, "job_id": job_id}


@app.post("/api/jobs/{job_id}/move")
async def move_job(job_id: str, request: Request):
    """작업을 다른 파드로 옮긴다.

    파드마다 큐가 따로 도는 구조라, 파드 하나가 죽으면 그 큐만 멈춘다(옆 파드가 놀아도
    자동으로 안 넘어간다). 그때 손으로 풀 수 있는 탈출구다.

    이미 큐에 들어간(queued) 작업도 옮길 수 있다 — 파이썬 큐에서 꺼내 빼는 건 불가능하지만,
    워커가 작업을 꺼낼 때 "이 작업이 아직 내 것인가"를 확인하고 아니면 원래 주인에게
    다시 배차하기 때문이다(pod_worker_loop/wait_for_pod). 실행 중인 작업은 옮길 수 없다."""
    user = me(request)
    source_job = job_or_404(user, job_id)
    data = await read_json_object(request, allow_empty=False)
    target_id = (data.get("pod_id") or "").strip()
    if not target_id:
        # 파드 지정을 풀어 "갖춘 파드가 있으면 아무 파드나"로 되돌린다(대기 큐/대기 목록의 작업만).
        with lock:
            job = jobs.get(job_id)
            if not job or job.get("deleted"):
                raise HTTPException(404, "없는 작업이에요.")
            if job["status"] not in ("pending", "queued"):
                raise HTTPException(400, "대기 중인 작업만 파드 지정을 풀 수 있어요.")
            job["pinned_pod_id"] = None
            job["pod_id"] = None
            job["auto_assigned"] = False
            set_comfy_wait_flag(job, False)
            if job["status"] == "queued":
                job["waiting_reason"] = "파드를 찾는 중이에요"
        save_state()
        poke_scheduler()
        return jobs[job_id]
    target = pod_registry.get_pod(target_id)
    # 작업은 그 작업의 주인의 파드로만 옮길 수 있다.
    if target is None or target.get("owner_id") != source_job.get("owner_id"):
        raise HTTPException(404, "없는 파드예요.")
    if not target.get("enabled"):
        raise HTTPException(400, f"'{target['name']}' 파드는 지금 사용 안 함 상태예요.")

    with lock:
        job = jobs.get(job_id)
        if not job or job.get("deleted"):
            raise HTTPException(404, "없는 작업이에요.")
        if job["status"] == "running":
            raise HTTPException(400, "실행 중인 작업은 옮길 수 없어요. 먼저 멈춰주세요.")
        if job["status"] not in ("pending", "queued", "interrupted"):
            raise HTTPException(400, "대기 중이거나 중단된 작업만 옮길 수 있어요.")
        if job["status"] == "queued" and not job.get("pod_id"):
            # 대기 큐에서 파드를 기다리는 작업 — 그 파드로 고정만 하고, 배정은 스케줄러가 갖춰졌는지 보고 한다.
            job["pinned_pod_id"] = target_id
            job["waiting_reason"] = "파드를 찾는 중이에요"
            pinned_only = True
        else:
            pinned_only = False
            if job.get("pod_id") == target_id:
                return job
        if pinned_only:
            pass
        else:
            job["pinned_pod_id"] = target_id
    if pinned_only:
        save_state()
        poke_scheduler()
        return jobs[job_id]
    with lock:
        job = jobs[job_id]
        job["pod_id"] = target_id
        wf_names = [job.get("workflow_filename"), job.get("video_workflow_filename")]
        job_template_id = job.get("template_id")
        requeue = job["status"] == "queued"
        if requeue:
            # 옛 파드의 큐에 남은 항목은 그 파드 워커가 꺼낼 때 버려진다(소유권 확인).
            set_comfy_wait_flag(job, False)
    if target.get("kind") == pod_registry.DEFAULT_KIND:
        blobs = []
        for label, name in zip(("워크플로우", "영상 워크플로우"), wf_names):
            if name:
                try:
                    blobs.append((label, (JOBS_DIR / name).read_bytes()))
                except OSError:
                    pass
            elif label == "영상 워크플로우":
                blobs.append((label, default_video_workflow_bytes(load_templates_map().get(job_template_id) or {})))
        try:
            new_preflight = await asyncio.wait_for(
                asyncio.to_thread(compute_preflight, target, user, blobs), timeout=10) if blobs else None
        except Exception:
            new_preflight = None
        with lock:
            if jobs.get(job_id):
                jobs[job_id]["preflight"] = new_preflight
    save_state()
    if requeue:
        dispatch_job(job_id, target_id)
    return jobs[job_id]


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, request: Request):
    job_or_404(me(request), job_id)
    with lock:
        job = jobs.get(job_id)
        if not job or job.get("deleted"):
            raise HTTPException(404, "없는 작업이에요.")
        waiting_for_pod = job["status"] == "queued" and not job.get("pod_id")   # 아직 어느 파드 큐에도 안 들어갔다
        if job["status"] in ("running", "queued") and not waiting_for_pod:
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
        result = await asyncio.to_thread(send_output_images, smtp_user, smtp_password, to_email, max_mb,
                                         None, asset_meta.owned_paths(me(request)["id"]))   # 내 이미지만
    except EmailSendError as e:
        raise HTTPException(400, str(e))
    return result


def _own_paths(user: dict) -> set[str] | None:
    """이 회원이 손댈 수 있는 결과물 경로 집합 — 관리자는 None(전부)."""
    return None if auth.is_admin(user) else asset_meta.owned_paths(user["id"])


def _meta_resolver(owner_id: int | None = None):
    """갤러리 항목에 붙일 결과물 메타(프로젝트·즐겨찾기·평점·태그)를 돌려주는 함수를 만든다
    (name, job_id) -> dict. 결과물 색인(assets)이 진실이고, 아직 색인에 없는 새 파일은 그 job의
    프로젝트만 물려받은 기본값으로 본다."""
    try:
        by_path = asset_meta.meta_by_path(owner_id)
    except Exception:
        by_path = {}

    def resolve(name: str, job_id: str | None) -> dict:
        found = by_path.get(name)
        if found:
            return found
        project_id = None
        if job_id:
            with lock:
                job = jobs.get(job_id)
            if job:
                project_id = job.get("project_id")
        return {"asset_id": None, "favorite": False, "rating": None, "nsfw": False, "project_id": project_id, "tags": []}
    return resolve


def list_output_images_meta(user: dict) -> list[dict]:
    scope = auth.owner_scope(user)   # 일반 회원에게는 자기 결과물만 보인다
    names = auth.usernames() if scope is None else {}
    # 갤러리 탭을 채우는 용도. zip/이메일 발송과 달리 폴더가 비어 있거나 아직 없는 것도
    # 정상 상태로 취급한다(뭔가 있어야 의미 있는 동작이 아니라, 그냥 목록을 보여줄 뿐이므로).
    try:
        files = list_output_images(OUTPUT_DIR, only_paths=_own_paths(user))
    except OutputFolderError:
        return []
    base = Path(OUTPUT_DIR)
    # nightshift 큐를 거치지 않고 ComfyUI에서 직접 돌린 이미지는 job_id가 없어서
    # 파드 갤러리(프론트의 podIdForImage)가 어느 파드 것인지 알 길이 없었다 —
    # "⬇ 결과 가져오기"가 남긴 동기화 기록(comfy_output_sync.json)에 이제 파드
    # 정보가 있으니, job_id가 없을 때 쓸 수 있게 같이 넘긴다.
    pod_ids = synced_pod_ids()
    meta_of = _meta_resolver(scope)
    items = []
    for f in files:
        stat = f.stat()
        rel = f.relative_to(base)
        name = rel.as_posix()
        # 템플릿 스크립트가 JOB_ID 하위 폴더에 나눠 저장하므로(seed_batch.py 등),
        # 상대 경로가 여러 단계면 첫 번째 폴더 이름을 job_id로 노출한다 — 갤러리의
        # "작업별 보기"가 이 값으로 묶는다. 하위 폴더 없이 바로 밑에 있는(예전 방식
        # 또는 JOB_ID 없이 실행된) 이미지는 job_id가 없다.
        job_id = rel.parts[0] if len(rel.parts) > 1 else None
        # 갤러리 "자세히 보기"에 해상도를 보여주려고 읽는다 — PIL은 헤더만 읽고
        # 픽셀 데이터는 지연 로드하므로(.load()를 안 부르면) 전체 디코드보다 훨씬
        # 가볍다. 손상된 파일이어도 목록 자체는 계속 보여야 하니 실패하면 조용히
        # None으로 둔다.
        width = height = None
        try:
            with Image.open(f) as img:
                width, height = img.size
        except Exception:
            pass
        items.append({
            "name": name,
            "job_id": job_id,
            "synced_pod_id": pod_ids.get(name),
            **meta_of(name, job_id),
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "width": width,
            "height": height,
        })
    if scope is None:   # 관리자에게는 누구 것인지 알려 준다
        for it in items:
            it["owner_name"] = names.get(it.get("owner_id"))
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items


@app.get("/api/output-images")
def list_output_images_api(request: Request, q: str | None = None, tag: str | None = None,
                           favorite: bool | None = None, min_rating: int | None = None,
                           model: str | None = None, hide_nsfw: bool = False):
    # 갤러리가 4초마다 부르는 곳 — 색인(assets)이 디스크와 어긋나지 않게 짧은 간격
    # 안에서는 건너뛰는 sync를 같이 돌린다(회전/삭제/직접 넣은 파일이 여기서 따라잡힌다).
    # q(프롬프트·메모·태그·파일명·체크포인트·시드)/tag(쉼표로 여러 개, 모두 붙은 것)/favorite/
    # min_rating을 주면 그 조건에 맞는 것만 남긴다. hide_nsfw는 헤더의 NSFW 토글이 "숨김"일 때 붙는다.
    _sync_assets_quietly()
    user = me(request)
    items = list_output_images_meta(user)
    allowed = asset_meta.search_paths(q, _split_tags(tag), favorite, min_rating, kind="image",
                                      owner_id=auth.owner_scope(user), model=model or None, hide_nsfw=hide_nsfw)
    if allowed is not None:
        items = [i for i in items if i["name"] in allowed]
    return {"images": items}


async def read_json_object(request: Request, allow_empty: bool = True) -> dict:
    body = (await request.body()).strip()
    if not body:
        if allow_empty:
            return {}
        raise HTTPException(400, "JSON 본문이 필요해요.")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    if not isinstance(data, dict):
        raise HTTPException(400, "JSON 객체여야 해요.")
    return data


@app.post("/api/comfy-outputs/sync")
async def sync_comfy_outputs(request: Request):
    user = me(request)
    # 원격 ComfyUI가 만든 결과 이미지를 로컬 출력 폴더로 끌어온다(comfy_outputs.py).
    # 작업이 끝날 때마다 자동으로도 돌지만(pull_outputs 설정), pod를 껐다 켠 뒤 밀린
    # 것을 한꺼번에 받거나 설정을 뒤늦게 켠 경우를 위해 수동으로도 돌릴 수 있다.
    data = await read_json_object(request)
    job_id = (data.get("job_id") or "").strip() or None
    force = bool(data.get("force"))
    pod_id = (data.get("pod_id") or "").strip() or None
    if job_id:
        job_or_404(user, job_id)

    # pod_id를 주면 그 파드에서 가져온다(파드 갤러리의 "⬇ 결과 가져오기") — 안 주면
    # 이 회원의 기본 파드에서 가져온다(전역 갤러리가 여기 해당한다).
    if pod_id:
        pod = pod_or_404(user, pod_id)
    else:
        pod = default_pod_for_user(user)
        if pod is None:
            raise HTTPException(400, "파드가 아직 없어요. 파드 화면에서 먼저 추가해 주세요.")
        pod_id = pod["id"]
    health = await asyncio.to_thread(driver_for(pod).health, pod)
    url, connected = health["url"], health["ok"]
    if not url or not connected:
        raise HTTPException(503, "ComfyUI에 연결할 수 없어 결과 이미지를 가져올 수 없어요.")
    try:
        result = await asyncio.to_thread(sync_outputs, url, only_subfolder=job_id, force=force, pod_id=pod_id)
    except OutputSyncError as e:
        raise HTTPException(502, str(e))
    return {"url": url, **result}


@app.post("/api/comfy-outputs/forget")
async def forget_comfy_outputs(request: Request):
    user = me(request)
    # "한 번 받아온 이미지"라는 기록을 지운다 — 지운 이미지가 다음 동기화에서 되살아나지
    # 않게 하는 것이 이 기록의 목적이므로, 정말 다시 받고 싶을 때만 쓰는 탈출구다.
    # (한 번만 다시 받으면 되는 경우라면 sync의 force=true가 더 간단하다.)
    data = await read_json_object(request)
    job_id = (data.get("job_id") or "").strip() or None
    # 기록은 서버 전체가 함께 쓰는 것이라, 작업 하나(내 것)만 지우거나 관리자가 전체를 지울 때만 허용한다.
    if job_id:
        job_or_404(user, job_id)
    else:
        admin_only(request)
    removed = await asyncio.to_thread(forget_downloaded, job_id)
    return {"forgotten": removed, **sync_state_summary()}


def _require_output_owner(rel_path: str, not_found: str) -> None:
    """결과 파일 하나에 접근해도 되는지 — 관리자는 전부, 일반 회원은 자기 것만(남의 것은 없는 것처럼 404)."""
    user = auth.current_user.get()
    if user is None:
        raise HTTPException(401, "로그인이 필요해요.")
    if auth.is_admin(user):
        return
    if assets_index.owner_of(rel_path) != user["id"]:
        raise HTTPException(404, not_found)


def resolve_output_image(filename: str) -> Path:
    # filename은 "job_id/파일명.png"처럼 한 단계 하위 폴더를 포함할 수 있다(작업별
    # 폴더 구조, output_images.py 참고). ".."로 상위 폴더를 벗어나려는 시도나 절대
    # 경로는 막고, 정규화한 최종 경로가 여전히 OUTPUT_DIR 내부인지 다시 한번 확인한다
    # (심볼릭 링크 등으로 우회하는 경우까지 방어).
    if not filename or filename.startswith("/") or ".." in Path(filename).parts:
        raise HTTPException(400, "올바르지 않은 파일명이에요.")
    base = Path(OUTPUT_DIR).resolve()
    path = (base / filename).resolve()
    if not path.is_relative_to(base):
        raise HTTPException(400, "올바르지 않은 파일명이에요.")
    if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise HTTPException(404, "이미지를 찾을 수 없어요.")
    _require_output_owner(path.relative_to(base).as_posix(), "이미지를 찾을 수 없어요.")
    return path


# :path 컨버터는 "/"를 포함해 뒤에 오는 걸 전부 욕심껏(greedy) 먹어버려서, 아래
# thumbnail 라우트를 원본 이미지 라우트보다 먼저 등록해야 한다 — 순서가 바뀌면
# "job_id/파일.png/thumbnail" 요청도 원본 이미지 라우트(filename:path)가 먼저
# 통째로 집어삼켜서 404가 난다(FastAPI/Starlette는 등록 순서대로 첫 매치를 씀).
@app.get("/api/output-images/{filename:path}/thumbnail")
def get_output_image_thumbnail(filename: str, request: Request, size: int = 320, fit: str = "inside"):
    # 갤러리 격자를 채우는 용도 — 원본을 그대로 내려받으면 느리고 대역폭을 낭비하므로,
    # 매 요청마다 그 자리에서 축소본을 만들어 돌려준다(디스크에 캐시하지 않음 — 이
    # 도구 규모에서는 매번 다시 만들어도 충분히 빠름). 대신 브라우저 캐시는 쓴다 —
    # 갤러리 격자는 삭제/새로고침/선택 상태 변화 때마다 통째로 다시 그려지므로
    # (renderGalleryGrid), 캐시 헤더가 없으면 그때마다 같은 썸네일을 다시 인코딩해서
    # 보내게 된다. ETag는 파일 mtime+size+요청한 size로 만들어서, 파일이 바뀌면
    # (예: "가로형 이미지 자동 회전"이 같은 파일에 덮어쓰면 mtime이 바뀜) 자동으로
    # 무효화된다.
    # fit=cover면 긴 변이 아니라 정사각형으로 가운데를 잘라 size×size로 만든다 — 갤러리 칸이
    # 정사각형(object-fit:cover)이라 어차피 그렇게 잘려 보이는데, 긴 변 기준으로 줄이면 세로로
    # 긴 이미지의 짧은 변이 칸보다 작아져 CSS가 늘려 그리는 바람에 흐릿해졌다.
    if fit not in ("inside", "cover"):
        raise HTTPException(400, "fit은 inside 또는 cover여야 해요.")
    path = resolve_output_image(filename)
    size = max(64, min(size, 800))
    stat = path.stat()
    etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}-{size}-{fit}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private, max-age=86400"})
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            if fit == "cover":
                img = ImageOps.fit(img, (size, size), method=Image.LANCZOS, centering=(0.5, 0.5))
            else:
                img.thumbnail((size, size))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=82)
    except Exception as e:
        raise HTTPException(500, f"썸네일을 만들지 못했어요: {e}")
    return Response(
        content=buf.getvalue(),
        media_type="image/jpeg",
        headers={"ETag": etag, "Cache-Control": "private, max-age=86400"},
    )


@app.get("/api/output-images/{filename:path}")
def get_output_image(filename: str):
    # 갤러리 라이트박스에서 원본 크기로 보여주는 용도.
    return FileResponse(resolve_output_image(filename))


@app.delete("/api/output-images/{filename:path}")
def delete_output_image(filename: str):
    # 갤러리에서 이미지 하나만 지우는 용도 — 되돌릴 수 없는 삭제라서, 확인 절차는
    # 프론트엔드(버튼 클릭 시 confirm 창)가 맡는다.
    resolve_output_image(filename).unlink()
    return {"ok": True}


@app.post("/api/output-images/{filename:path}/rotate")
async def rotate_output_image(filename: str):
    # 갤러리 라이트박스에서 지금 보고 있는 이미지 한 장을 시계 방향 90도로 돌린다 —
    # rotate-landscape/download-selected-rotated와 달리 원본을 그 자리에서 실제로
    # 덮어쓴다(사용자가 직접 누른 명시적인 동작이므로).
    path = resolve_output_image(filename)
    await asyncio.to_thread(rotate_image_file, path)
    return {"ok": True}


def parse_image_names_body(data: dict) -> list[str]:
    names = data.get("names")
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise HTTPException(400, "이미지 파일명 목록(names)이 필요해요.")
    return names


def build_zip_from_paths(paths: list[Path], rotate_landscape: bool = False) -> Path:
    # 작업별 하위 폴더 구조 없이 파일명만으로 평평하게 담는다 — 압축을 풀었을 때
    # 폴더 구조 없이 한 자리에 전부 모여있길 원해서다. 서로 다른 작업 폴더에서
    # 온 파일이 우연히 같은 이름이면 zip 안에서 이름이 겹치므로, 그런 경우에만
    # "이름 (1).ext"처럼 번호를 붙여 구분한다.
    #
    # rotate_landscape=True면 가로형(너비>높이) 이미지만 시계 방향 90도로 돌려서
    # zip에 담는다 — 원본 파일은 절대 건드리지 않는다(메모리에서만 돌려서 그
    # 결과 바이트만 zip에 씀). "회전 후 다운로드" 버튼이 쓰는 옵션으로, 예전에는
    # 원본 파일 자체를 영구히 돌려버렸는데(디스크에 덮어씀) 그 회전이 이 다운로드
    # 한 번을 위한 것일 뿐이라 원본은 그대로 두고 다운로드본만 돌리도록 바꿨다.
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    tmp_path = Path(tmp.name)
    used_names: set[str] = set()
    with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in paths:
            arcname = f.name
            if arcname in used_names:
                stem, suffix = f.stem, f.suffix
                n = 1
                while f"{stem} ({n}){suffix}" in used_names:
                    n += 1
                arcname = f"{stem} ({n}){suffix}"
            used_names.add(arcname)

            rotated_bytes = None
            if rotate_landscape:
                with Image.open(f) as img:
                    if img.width > img.height:
                        rotated = img.transpose(Image.Transpose.ROTATE_270)
                        buf = io.BytesIO()
                        rotated.save(buf, format=img.format or "PNG")
                        rotated_bytes = buf.getvalue()
            if rotated_bytes is not None:
                zf.writestr(arcname, rotated_bytes)
            else:
                zf.write(f, arcname=arcname)
    return tmp_path


def build_output_zip(only_paths: set[str] | None = None) -> Path:
    files = find_image_files(OUTPUT_DIR, only_paths)
    return build_zip_from_paths(files)


@app.get("/api/download-images")
async def download_images(request: Request):
    only = asset_meta.owned_paths(me(request)["id"])
    # 압축은 시간이 걸릴 수 있으니 이벤트 루프를 막지 않게 스레드에서 처리하고,
    # 임시로 만든 zip 파일은 응답이 끝난 뒤 백그라운드에서 지운다.
    try:
        zip_path = await asyncio.to_thread(build_output_zip, only)
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
async def delete_images(request: Request):
    only = asset_meta.owned_paths(me(request)["id"])
    # 되돌릴 수 없는 삭제라서, 확인 절차는 프론트엔드(버튼 클릭 시 confirm 창)가 맡는다.
    try:
        deleted = await asyncio.to_thread(delete_output_images, OUTPUT_DIR, only)
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


@app.post("/api/output-images/rotate-selected")
async def rotate_selected_images(request: Request):
    # 갤러리에서 여러 장을 선택해 한 번에 돌리는 용도 — download-selected-rotated와
    # 달리 원본을 그 자리에서 실제로 덮어쓴다. 잘못된 이름이나 그 사이 지워진 파일은
    # 조용히 건너뛴다.
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    names = parse_image_names_body(data)

    rotated = 0
    errors = []
    for name in names:
        try:
            path = resolve_output_image(name)
        except HTTPException:
            continue
        try:
            await asyncio.to_thread(rotate_image_file, path)
        except Exception as e:
            errors.append(f"{name}: {e}")
            continue
        rotated += 1
    return {"rotated": rotated, "errors": errors}


@app.post("/api/output-images/rotate-landscape")
async def rotate_images(request: Request):
    only = asset_meta.owned_paths(me(request)["id"])
    try:
        result = await asyncio.to_thread(rotate_landscape_images, OUTPUT_DIR, only)
    except OutputFolderError as e:
        raise HTTPException(404, str(e))
    return result


@app.post("/api/output-images/download-selected-rotated")
async def download_selected_images_rotated(request: Request):
    # "회전 후 다운로드" 버튼용 — 가로형만 시계 방향 90도로 돌려서 zip에 담아
    # 내려준다. download-selected와 달리 원본 파일은 전혀 건드리지 않는다(예전엔
    # /api/output-images/rotate-selected로 원본을 영구히 덮어쓴 뒤 다운로드했는데,
    # 그 회전이 이 한 번의 다운로드만을 위한 것일 뿐 갤러리에 계속 남아있을
    # 이유가 없어서, 다운로드되는 바이트만 돌리고 저장된 원본은 그대로 두게
    # 바꿨다 — build_zip_from_paths의 rotate_landscape 참고).
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

    zip_path = await asyncio.to_thread(build_zip_from_paths, paths, rotate_landscape=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"nightshift_selected_rotated_{timestamp}.zip",
        background=BackgroundTask(lambda: zip_path.unlink(missing_ok=True)),
    )


def list_output_videos_meta(user: dict) -> list[dict]:
    scope = auth.owner_scope(user)
    names = auth.usernames() if scope is None else {}
    # 영상 갤러리 탭을 채우는 용도. 이미지와 같은 출력 폴더를 보되 동영상 확장자만
    # 걸러낸다 — width/height는 ffprobe 없이는 못 읽으므로(의도적으로 새 시스템
    # 의존성을 추가하지 않기로 함) 내지 않는다. 자세히 보기가 없는 이유도 같다.
    try:
        files = list_output_videos(OUTPUT_DIR, only_paths=_own_paths(user))
    except OutputFolderError:
        return []
    base = Path(OUTPUT_DIR)
    pod_ids = synced_pod_ids()  # list_output_images_meta 참고 — job_id 없는 영상용.
    meta_of = _meta_resolver(scope)
    items = []
    for f in files:
        stat = f.stat()
        rel = f.relative_to(base)
        name = rel.as_posix()
        job_id = rel.parts[0] if len(rel.parts) > 1 else None
        items.append({
            "name": name,
            "job_id": job_id,
            "synced_pod_id": pod_ids.get(name),
            **meta_of(name, job_id),
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })
    if scope is None:   # 관리자에게는 누구 것인지 알려 준다
        for it in items:
            it["owner_name"] = names.get(it.get("owner_id"))
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items


@app.get("/api/output-videos")
def list_output_videos_api(request: Request, q: str | None = None, tag: str | None = None,
                           favorite: bool | None = None, min_rating: int | None = None,
                           model: str | None = None, hide_nsfw: bool = False):
    _sync_assets_quietly()
    user = me(request)
    items = list_output_videos_meta(user)
    allowed = asset_meta.search_paths(q, _split_tags(tag), favorite, min_rating, kind="video",
                                      owner_id=auth.owner_scope(user), model=model or None, hide_nsfw=hide_nsfw)
    if allowed is not None:
        items = [i for i in items if i["name"] in allowed]
    return {"videos": items}


def resolve_output_video(filename: str) -> Path:
    # resolve_output_image와 같은 방식의 경로 검증(상위 폴더 탈출·심볼릭 링크 우회 방지).
    if not filename or filename.startswith("/") or ".." in Path(filename).parts:
        raise HTTPException(400, "올바르지 않은 파일명이에요.")
    base = Path(OUTPUT_DIR).resolve()
    path = (base / filename).resolve()
    if not path.is_relative_to(base):
        raise HTTPException(400, "올바르지 않은 파일명이에요.")
    if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise HTTPException(404, "동영상을 찾을 수 없어요.")
    _require_output_owner(path.relative_to(base).as_posix(), "동영상을 찾을 수 없어요.")
    return path


@app.get("/api/output-videos/{filename:path}")
def get_output_video(filename: str):
    # 영상 갤러리 라이트박스의 <video> 태그가 재생하는 용도. FileResponse는
    # HTTP Range 요청을 그대로 지원해서(Starlette 내장) 앞으로 감기·되감기가 된다.
    return FileResponse(resolve_output_video(filename))


@app.delete("/api/output-videos/{filename:path}")
def delete_output_video(filename: str):
    resolve_output_video(filename).unlink()
    return {"ok": True}


# ---- 영상 편집(자르기 / 이어 붙이기) — video_edit.py ---------------------------------------
# 갤러리에서 고른 영상으로 새 영상을 만든다(원본은 그대로). 작업은 백그라운드로 돌고 진행률을 폴링으로 본다.

def register_generated_video(rel: str, source_rels: list[str], scope, note: str) -> None:
    """서버가 새로 만든 영상(편집 결과, 외부 편집기가 올린 결과)을 색인에 넣고, 원본이 다 같은 프로젝트면 그 프로젝트로
    넣고, 메모를 남긴다. 어떤 실패도 부른 쪽을 실패시키지 않는다(파일은 이미 만들어졌다)."""
    try:
        assets_index.sync(force=True)
        if source_rels:
            with db.connect() as conn:
                rows = conn.execute(
                    f"SELECT DISTINCT project_id FROM assets WHERE path IN ({','.join('?' * len(source_rels))})", source_rels).fetchall()
            projects = {r["project_id"] for r in rows}
            if len(projects) == 1 and next(iter(projects)) is not None:
                asset_meta.move_assets([rel], next(iter(projects)), scope)
        asset_meta.update_assets([rel], note=note, owner_id=scope)
    except Exception:
        logging.getLogger("uvicorn.error").exception("생성된 영상 등록 실패: %s", rel)


def _slug(text: str, fallback: str) -> str:
    slug = re.sub(r"[^\w.-]+", "-", text.strip(), flags=re.UNICODE).strip("-._")[:40]
    return slug or fallback


@app.post("/api/video-edits")
async def create_video_edit(request: Request):
    user = me(request)
    data = await read_json_object(request, allow_empty=False)
    op = data.get("op")
    if op not in ("concat", "trim"):
        raise HTTPException(400, "op는 concat 또는 trim이어야 해요.")
    names = data.get("inputs")
    if not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names):
        raise HTTPException(400, "inputs(영상 이름 목록)가 필요해요.")
    if len(names) > video_edit.MAX_INPUTS:
        raise HTTPException(400, f"한 번에 {video_edit.MAX_INPUTS}개까지 이어 붙일 수 있어요.")
    sources = [resolve_output_video(n) for n in names]   # 남의 영상/없는 영상은 여기서 404
    start_s = end_s = None
    if op == "trim":
        try:
            start_s = float(data.get("start") or 0)
            end_s = float(data["end"]) if data.get("end") not in (None, "") else None
        except (TypeError, ValueError):
            raise HTTPException(400, "start/end는 초 단위 숫자여야 해요.")
        if start_s < 0 or (end_s is not None and end_s <= start_s):
            raise HTTPException(400, "끝 시각은 시작 시각보다 뒤여야 해요.")
    base = Path(OUTPUT_DIR).resolve()
    prefix = "" if auth.is_admin(user) else f"u{user['id']}/"
    label = _slug(str(data.get("name") or ""), "concat" if op == "concat" else "trim")
    rel = f"{prefix}edits/{datetime.now().strftime('%Y%m%d-%H%M%S')}_{label}_{uuid.uuid4().hex[:4]}.mp4"
    out_path = base / rel
    source_rels = [p.relative_to(base).as_posix() for p in sources]
    scope = auth.owner_scope(user)

    label_names = ", ".join(Path(r).name for r in source_rels[:5]) + (" …" if len(source_rels) > 5 else "")
    note = (f"{'이어 붙임' if op == 'concat' else '자름'}: {label_names}"
            + (f" ({start_s:g}s~{end_s:g}s)" if op == "trim" and end_s is not None else ""))

    def post(_out: Path) -> None:
        register_generated_video(rel, source_rels, scope, note)

    try:
        job_id = video_edit.start(op, sources, out_path, user["id"], start_s=start_s, end_s=end_s, out_rel=rel, post=post)
    except video_edit.EditError as e:
        raise HTTPException(400, str(e))
    return {"id": job_id}


def _edit_job_or_404(user: dict, job_id: str) -> dict:
    job = video_edit.get(job_id)
    if job is None or not (auth.is_admin(user) or job["owner_id"] == user["id"]):
        raise HTTPException(404, "없는 편집 작업이에요.")
    return job


@app.get("/api/video-edits/{job_id}")
def get_video_edit(job_id: str, request: Request):
    return video_edit.public(_edit_job_or_404(me(request), job_id))


@app.post("/api/video-edits/{job_id}/cancel")
def cancel_video_edit(job_id: str, request: Request):
    _edit_job_or_404(me(request), job_id)
    video_edit.cancel(job_id)
    return {"ok": True}


# ---- 외부 편집기(OpenCut)와 자원 공유 — share_sessions.py ----------------------------------------
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
SHARE_UPLOAD_EXT = {".mp4", ".webm", ".mov", ".m4v"}
SHARE_CONTENT_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
                       ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
                       ".gif": "image/gif", ".bmp": "image/bmp"}


@app.get("/api/share/config")
def share_config(request: Request):
    me(request)
    return {"opencut_url": OPENCUT_URL or None}


@app.post("/api/share/sessions")
async def create_share_session(request: Request):
    """갤러리에서 고른 이미지/영상을 편집기가 가져갈 수 있는 세션을 만든다 → {token, url}(편집기를 여는 주소)."""
    user = me(request)
    if not OPENCUT_URL:
        raise HTTPException(503, "외부 편집기가 설정돼 있지 않아요(NIGHTSHIFT_OPENCUT_URL).")
    data = await read_json_object(request, allow_empty=False)
    names = data.get("names")
    if not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names):
        raise HTTPException(400, "names(파일 이름 목록)가 필요해요.")
    if len(names) > share_sessions.MAX_FILES:
        raise HTTPException(400, f"한 번에 {share_sessions.MAX_FILES}개까지 보낼 수 있어요.")
    base = Path(OUTPUT_DIR).resolve()
    files = []
    for name in names:
        ext = Path(name).suffix.lower()
        path = resolve_output_video(name) if ext in VIDEO_EXTENSIONS else resolve_output_image(name)   # 남의 것/없는 것은 여기서 404
        files.append({"rel": path.relative_to(base).as_posix(), "kind": "video" if ext in VIDEO_EXTENSIONS else "image",
                      "name": path.name})
    rels = [f["rel"] for f in files]
    with db.connect() as conn:
        rows = conn.execute(f"SELECT DISTINCT project_id FROM assets WHERE path IN ({','.join('?' * len(rels))})", rels).fetchall()
    projects = {r["project_id"] for r in rows}
    project_id = next(iter(projects)) if len(projects) == 1 else None
    token = share_sessions.create(user["id"], files, project_id)
    return {"token": token, "url": f"{OPENCUT_URL}/nightshift?ns={token}", "expires_in": share_sessions.SESSION_TTL_SEC}


def _share_or_404(token: str) -> dict:
    session = share_sessions.get(token)
    if session is None:
        raise HTTPException(404, "만료됐거나 없는 공유 세션이에요.")
    return session


@app.get("/api/shared/sessions/{token}")
def get_share_manifest(token: str, request: Request):
    """편집기가 읽는 목록 — 파일마다 이 세션 안에서만 통하는 주소가 붙는다."""
    session = _share_or_404(token)
    base = Path(OUTPUT_DIR).resolve()
    origin = f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"
    files = []
    for i, f in enumerate(session["files"]):
        path = base / f["rel"]
        if not path.is_file():
            continue
        files.append({"index": i, "name": f["name"], "kind": f["kind"], "size": path.stat().st_size,
                      "url": f"{origin}/api/shared/sessions/{token}/files/{i}"})
    return {"files": files, "upload_url": f"{origin}/api/shared/sessions/{token}/upload",
            "expires_at": session["expires"], "uploads_left": share_sessions.MAX_UPLOADS - session["uploads"]}


@app.get("/api/shared/sessions/{token}/files/{index}")
def get_shared_file(token: str, index: int):
    session = _share_or_404(token)
    if not 0 <= index < len(session["files"]):
        raise HTTPException(404, "없는 파일이에요.")
    base = Path(OUTPUT_DIR).resolve()
    path = (base / session["files"][index]["rel"]).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise HTTPException(404, "파일을 찾을 수 없어요.")
    return FileResponse(path, media_type=SHARE_CONTENT_TYPES.get(path.suffix.lower()))


@app.post("/api/shared/sessions/{token}/upload")
async def upload_shared_result(token: str, request: Request, name: str = "edit.mp4"):
    """편집기가 내보낸 영상을 받아 세션 주인의 편집 폴더에 새 영상으로 저장한다(요청 본문이 곧 파일)."""
    session = _share_or_404(token)
    ext = Path(name).suffix.lower()
    if ext not in SHARE_UPLOAD_EXT:
        raise HTTPException(400, f"영상 파일({', '.join(sorted(SHARE_UPLOAD_EXT))})만 올릴 수 있어요.")
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > SHARE_UPLOAD_MAX_BYTES:
        raise HTTPException(413, "파일이 너무 커요.")
    if not share_sessions.count_upload(token):
        raise HTTPException(429, "이 세션으로는 더 올릴 수 없어요.")
    owner = auth.get_user(session["owner_id"])
    if owner is None or owner.get("status") != "active":
        raise HTTPException(403, "이 세션의 회원을 쓸 수 없어요.")
    base = Path(OUTPUT_DIR).resolve()
    prefix = "" if auth.is_admin(owner) else f"u{owner['id']}/"
    label = _slug(Path(name).stem, "opencut")
    rel = f"{prefix}edits/{datetime.now().strftime('%Y%m%d-%H%M%S')}_opencut_{label}_{uuid.uuid4().hex[:4]}{ext}"
    out_path = base / rel
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.stem + ".partial" + out_path.suffix)
    size = 0
    try:
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > SHARE_UPLOAD_MAX_BYTES:
                    raise HTTPException(413, "파일이 너무 커요.")
                f.write(chunk)
        if size == 0:
            raise HTTPException(400, "빈 파일이에요.")
        os.replace(tmp, out_path)
    finally:
        if tmp.exists():
            tmp.unlink()
    sources = [f["rel"] for f in session["files"]]
    note = "OpenCut에서 편집: " + ", ".join(Path(r).name for r in sources[:5]) + (" …" if len(sources) > 5 else "")
    scope = None if auth.is_admin(owner) else owner["id"]
    await asyncio.to_thread(register_generated_video, rel, sources, scope, note)
    return {"ok": True, "output": rel, "size": size}


def build_output_video_zip(only_paths: set[str] | None = None) -> Path:
    files = find_video_files(OUTPUT_DIR, only_paths)
    return build_zip_from_paths(files)


@app.get("/api/download-videos")
async def download_videos(request: Request):
    only = asset_meta.owned_paths(me(request)["id"])
    try:
        zip_path = await asyncio.to_thread(build_output_video_zip, only)
    except OutputFolderError as e:
        raise HTTPException(404, str(e))

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"nightshift_videos_{timestamp}.zip",
        background=BackgroundTask(lambda: zip_path.unlink(missing_ok=True)),
    )


@app.post("/api/output-videos/download-selected")
async def download_selected_videos(request: Request):
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    names = parse_image_names_body(data)

    paths = []
    for name in names:
        try:
            paths.append(resolve_output_video(name))
        except HTTPException:
            continue
    if not paths:
        raise HTTPException(404, "선택한 동영상을 찾을 수 없어요.")

    zip_path = await asyncio.to_thread(build_zip_from_paths, paths)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=f"nightshift_videos_selected_{timestamp}.zip",
        background=BackgroundTask(lambda: zip_path.unlink(missing_ok=True)),
    )


@app.delete("/api/output-videos")
async def delete_videos(request: Request):
    only = asset_meta.owned_paths(me(request)["id"])
    try:
        deleted = await asyncio.to_thread(delete_output_videos, OUTPUT_DIR, only)
    except OutputFolderError as e:
        raise HTTPException(404, str(e))
    return {"deleted": deleted}


@app.post("/api/output-videos/delete-selected")
async def delete_selected_videos(request: Request):
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    names = parse_image_names_body(data)

    deleted = 0
    for name in names:
        try:
            path = resolve_output_video(name)
        except HTTPException:
            continue
        path.unlink()
        deleted += 1
    return {"deleted": deleted}


@app.get("/api/danbooru/tag-edits")
def get_danbooru_tag_edits():
    return danbooru_tag_edits


@app.put("/api/danbooru/tag-edits")
async def put_danbooru_tag_edits(request: Request):
    admin_only(request)
    # 프론트엔드가 편집 상태 전체({categoryKey: {added, removed}})를 매번 통째로
    # 보내서 그대로 덮어쓴다 — 카테고리가 많지 않고 편집도 잦지 않아 부분 patch를
    # 둘 이유가 없다.
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    if not isinstance(data, dict):
        raise HTTPException(400, "카테고리별 편집 내역(객체)이어야 해요.")
    for key, value in data.items():
        if not isinstance(value, dict) or not all(
            isinstance(value.get(field, []), list) for field in ("added", "removed")
        ):
            raise HTTPException(400, f"'{key}' 항목 형식이 올바르지 않아요.")

    danbooru_tag_edits.clear()
    danbooru_tag_edits.update(data)
    save_danbooru_tag_edits()
    return danbooru_tag_edits


@app.get("/api/danbooru/history")
def get_danbooru_history():
    return {"history": danbooru_history}


@app.post("/api/danbooru/history")
async def add_danbooru_history(request: Request):
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")

    selection = data.get("selection")
    texts = data.get("texts")
    if not isinstance(selection, dict) or not isinstance(texts, dict):
        raise HTTPException(400, "selection/texts는 객체여야 해요.")
    name = data.get("name") or ""
    if not isinstance(name, str):
        raise HTTPException(400, "name은 문자열이어야 해요.")

    entry = {
        "id": str(uuid.uuid4())[:8],
        "name": name,
        "timestamp": int(time.time() * 1000),
        "selection": selection,
        "texts": texts,
    }
    with lock:
        danbooru_history.insert(0, entry)
        del danbooru_history[DANBOORU_HISTORY_LIMIT:]
    save_danbooru_history()
    return entry


@app.delete("/api/danbooru/history/{entry_id}")
def delete_danbooru_history(entry_id: str):
    with lock:
        entry = next((h for h in danbooru_history if h["id"] == entry_id), None)
        if entry is None:
            raise HTTPException(404, "없는 기록이에요.")
        danbooru_history.remove(entry)
    save_danbooru_history()
    return {"ok": True}


app.mount("/", StaticFiles(directory=str(REPO_ROOT / "static"), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
