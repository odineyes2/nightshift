# app.py가 app_parts/*.py를 번호 순서대로 한 네임스페이스에서 실행한다 — 앞 파일의 이름을 import 없이 쓴다.
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
import board_presets
import board_store
import db
import git_log
import model_download
import model_registry
import pod_registry
import pose_library
import projects as project_store
import runpod_api
import runpod_sessions
import runpod_sync
from email_sender import EmailSendError, find_image_files, send_output_images
from workflow_builder import WorkflowBuildError, build_workflow
import workflow_builder_krea2
import workflow_builder_unet
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
# 결과 이미지 축소본 디스크 캐시(14-output-images의 thumbnail 라우트). 상한을 넘으면
# 서버 시작 때 오래된 것부터 지운다(prune_thumb_cache).
THUMB_CACHE_DIR = data_dir("thumb_cache")
THUMB_CACHE_MAX_BYTES = 2 * 1024 ** 3
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
# "새 작업 추가" 마법사의 ControlNet 계열 워크플로우 유형(depth_cn/lineart_cn —
# OpenPose는 빌더가 조립하는 post.openpose로 옮겼다)이 쓰는, family(베이스 모델)별로 미리 만들어 올려둔 워크플로우 JSON
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

# 바깥 서비스(RunPod·ComfyUI·파드)가 실패했을 때의 응답 코드. 502를 쓰면 Cloudflare 터널이 응답을 자기 오류 화면으로
# 바꿔 detail(실패 이유)이 화면에 안 보였다(2026-10-07, RunPod 422가 "실패했어요(HTTP 502)."로만 보임). 4xx는 그대로 통과한다.
UPSTREAM_ERROR = 424

jobs: dict[str, dict] = {}
lock = threading.Lock()

