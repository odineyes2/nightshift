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
import queue
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile
from PIL import Image

from comfy_outputs import OutputSyncError, forget_downloaded, sync_outputs, sync_state_summary
from email_sender import EmailSendError, find_image_files, send_output_images
from workflow_builder import WorkflowBuildError, build_workflow
from output_images import (
    IMAGE_EXTENSIONS,
    OUTPUT_DIR,
    OutputFolderError,
    delete_output_images,
    list_output_images,
    rotate_landscape_images,
)
from ref_assets import (
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
from input_assets import InputAssetError, list_input_images, resolve_input_image

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
# "새 작업 추가" 마법사의 ControlNet 계열 워크플로우 유형(openpose_cn/depth_cn/
# lineart_cn)이 쓰는, family(베이스 모델)별로 미리 만들어 올려둔 워크플로우 JSON
# 저장소. 이 세 유형은 체크포인트마다 ControlNet 로더/가중치 배선이 달라 워크플로우
# 빌더가 안전하게 자동 조립할 수 없어서(잘못 배선하면 조용히 ControlNet 없이
# 돌아가는 사고가 남), 관리자가 한 번 만들어둔 워크플로우를 family+유형 조합별로
# 저장해뒀다가 그대로 재사용한다. 파일명 규칙은 preset_filename() 참고.
WORKFLOW_PRESETS_DIR = BASE_DIR / "workflow_presets"
JOBS_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)
WORKFLOW_PRESETS_DIR.mkdir(exist_ok=True)

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
    BASE_DIR / "recent_workflows",
    BASE_DIR / "recent_workflows_state.json",
    int(os.environ.get("NIGHTSHIFT_RECENT_WORKFLOWS_RETENTION", "30")),
)
recent_csvs_store = RecentFileStore(
    BASE_DIR / "recent_csvs",
    BASE_DIR / "recent_csvs_state.json",
    int(os.environ.get("NIGHTSHIFT_RECENT_CSVS_RETENTION", "30")),
)

# "Danbooru 프롬프트 조립" 탭의 영구 저장 대상 두 가지 — 태그 풀 편집(카테고리별
# 추가/삭제)과 조합 기록. 규칙 엔진·랜덤 조합·프롬프트 조립 자체는 클릭마다 즉시
# 반응해야 해서 static/index.html에 선언적 데이터+로직으로 들어있고, 여기서는
# "사용자가 편집/저장한 상태"만 그대로 보관했다 내려준다 (형태를 이해할 필요가 없음).
DANBOORU_TAG_EDITS_FILE = BASE_DIR / "danbooru_tag_edits.json"
DANBOORU_HISTORY_FILE = BASE_DIR / "danbooru_history.json"
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


# LoRA 파일 이름 -> {trigger, families} 매핑. ComfyUI는 LoRA가 설치돼 있다는 것만
# 알지 트리거 워드가 뭔지는 모르므로(그 LoRA를 만든 사람이 문서/civitai 페이지 등에
# 적어둔 값이라 사용자가 직접 입력해야 함), 여기 저장해두고 "워크플로우" 탭에서 그
# LoRA를 고르면 자동으로 긍정 프롬프트에 덧붙인다("🎛 LoRA" 탭에서 편집함).
# families(베이스 모델 family id 목록)는 "새 작업 추가" 마법사가 지금 고른 베이스
# 모델과 호환되는 LoRA만 보여주는 데 쓴다 — 빈 목록이면 "모든 베이스 모델과 호환"
# 취급(과거 데이터를 자동 이관한 항목이 전부 이 상태다. 아래 load_lora_triggers 참고).
#
# 예전 스키마는 {lora_filename: "trigger_word"}(문자열)였다. load_lora_triggers()가
# 시작할 때 문자열 값을 {trigger: 그 값, families: []}로 자동 이관하고 즉시 새
# 형식으로 다시 저장해, 그 뒤로는 항상 새 형식만 디스크에 남는다.
LORA_TRIGGERS_FILE = BASE_DIR / "lora_triggers.json"
lora_triggers: dict[str, dict] = {}  # {lora_filename: {"trigger": str, "families": [family_id, ...]}}


def normalize_lora_trigger_entry(value) -> dict:
    if isinstance(value, str):
        return {"trigger": value, "families": []}
    if isinstance(value, dict):
        trigger = value.get("trigger")
        families = value.get("families")
        return {
            "trigger": trigger if isinstance(trigger, str) else "",
            "families": [f for f in families if isinstance(f, str)] if isinstance(families, list) else [],
        }
    return {"trigger": "", "families": []}


def load_lora_triggers():
    if not LORA_TRIGGERS_FILE.exists():
        return
    with open(LORA_TRIGGERS_FILE) as f:
        raw = json.load(f)
    migrated = any(not isinstance(v, dict) for v in raw.values())
    lora_triggers.update({k: normalize_lora_trigger_entry(v) for k, v in raw.items()})
    if migrated:
        save_lora_triggers()


def save_lora_triggers():
    with lock:
        with open(LORA_TRIGGERS_FILE, "w") as f:
            json.dump(lora_triggers, f, indent=2, ensure_ascii=False)


# 베이스 모델 family(예: "wai-illustrious", "krea.2") 정의 — "새 작업 추가" 마법사의
# 1단계(베이스 모델 선택)가 이 목록에서 고른다. family 하나는 서로 호환되는(같은
# 아키텍처 계열) 체크포인트 파일 여러 개를 묶을 수 있다. LoRA/워크플로우 프리셋의
# "호환 family" 목록이 여기 family_id를 참조한다("🎛 LoRA" 탭, workflow_presets).
BASE_MODEL_FAMILIES_FILE = BASE_DIR / "base_model_families.json"
base_model_families: dict[str, dict] = {}  # {family_id: {"label": str, "checkpoints": [ckpt_filename, ...]}}


def load_base_model_families():
    if BASE_MODEL_FAMILIES_FILE.exists():
        with open(BASE_MODEL_FAMILIES_FILE) as f:
            base_model_families.update(json.load(f))


def save_base_model_families():
    with lock:
        with open(BASE_MODEL_FAMILIES_FILE, "w") as f:
            json.dump(base_model_families, f, indent=2, ensure_ascii=False)


# ComfyUI 접속 주소는 세 곳에서 올 수 있고, 아래 순서로 먼저 정해지는 것을 쓴다
# (configured_comfy_url/resolve_comfy_url 참고):
#
#   1. comfy_endpoint.json에 저장된 값 — 화면(헤더의 연결 상태 배지 클릭)에서
#      언제든 바꿀 수 있고 서버 재시작이 필요 없다. nightshift를 홈서버에 상시
#      띄워두고 ComfyUI만 원격 pod에서 도는 구성에서는 pod를 새로 만들 때마다
#      주소가 바뀌므로, "런타임에 바꿀 수 있는 설정"이 기본 경로다.
#   2. COMFY_URL 환경변수 — 배포 시점에 고정해두는 기본값(위 설정이 비어 있을 때만).
#   3. 자동 탐지 — 같은 머신에서 도는 경우를 위한 폴백. 후보들을 순서대로 짧은
#      타임아웃으로 찔러보고 처음 응답하는 곳을 채택한다.
COMFY_ENDPOINT_FILE = BASE_DIR / "comfy_endpoint.json"
COMFY_CANDIDATE_URLS = ["http://127.0.0.1:8188", "http://127.0.0.1:8000"]
COMFY_CHECK_TIMEOUT = 2
# 사용자가 "연결 테스트"/"저장"으로 명시적으로 확인할 때는 폴링(2초)보다 넉넉하게
# 기다린다 — 원격 pod는 첫 연결에 TLS 핸드셰이크까지 붙어 2초를 넘기기 쉽다.
COMFY_CHECK_TIMEOUT_INTERACTIVE = 8

# ComfyUI가 안 떠 있을 때, 큐에서 꺼낸 작업을 실패시키지 않고 붙잡아 두는 재확인
# 간격(초). nightshift를 홈서버에 상시 띄워두고 ComfyUI만 원격 GPU pod에서 돌리는
# 구성에서는 "pod가 아직 안 떠 있는" 시간이 비정상이 아니라 기본 상태다 — 그때
# 밤사이 쌓아둔 작업이 몇 초 만에 전부 failed로 떨어지면 큐를 쌓아두는 의미가
# 없어지므로, 연결될 때까지 이 간격으로 다시 확인하며 기다린다(waiting_for_comfy).
COMFY_WAIT_RETRY_SEC = float(os.environ.get("NIGHTSHIFT_COMFY_WAIT_RETRY_SEC", "15"))
# 기다리는 동안에도 "⏸ 정지"에는 빨리 반응해야 하니 위 간격을 이 단위로 쪼개 잔다.
COMFY_WAIT_TICK_SEC = 1.0

# pull_outputs: ComfyUI가 원격에 있어서 결과 이미지를 로컬 출력 폴더로 HTTP로
# 끌어와야 하는지(comfy_outputs.py). 같은 머신에서 도는 구성에서는 출력 폴더가 곧
# ComfyUI의 출력 폴더라 켤 필요가 없다 — 그래서 기본값은 꺼짐이고, 화면의 접속 주소
# 설정에서 켠다.
comfy_endpoint: dict = {"url": "", "updated_at": None, "pull_outputs": False}


def load_comfy_endpoint():
    if COMFY_ENDPOINT_FILE.exists():
        with open(COMFY_ENDPOINT_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            comfy_endpoint["url"] = str(data.get("url") or "")
            comfy_endpoint["updated_at"] = data.get("updated_at")
            comfy_endpoint["pull_outputs"] = bool(data.get("pull_outputs"))


def save_comfy_endpoint():
    with lock:
        with open(COMFY_ENDPOINT_FILE, "w") as f:
            json.dump(comfy_endpoint, f, indent=2, ensure_ascii=False)


def normalize_comfy_url(raw: str) -> str:
    """입력받은 주소를 저장 형태로 다듬는다(뒤 슬래시 제거). 빈 값은 "자동 탐지로
    되돌리기"를 뜻하므로 그대로 통과시키고, 형식이 틀리면 ValueError."""
    url = (raw or "").strip().rstrip("/")
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("http:// 또는 https:// 로 시작하는 주소여야 해요.")
    return url

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

# worker_loop는 한 번에 하나씩만 순차 실행하므로, 대기/실행 중인 작업이 한없이
# 쌓이는 걸 막을 안전장치가 없으면 (예: 반복 호출하는 스크립트나 LLM의 버그로)
# 큐가 통제 불능으로 불어날 수 있다 — 각 작업이 실제 GPU 시간을 쓰므로 위험이
# 크다. pending/queued/running 합계가 이 값 이상이면 새 작업 추가를 거부한다.
# 비밀값이 아니라서 기본값을 코드에 그대로 박아두되, .env에
# NIGHTSHIFT_MAX_ACTIVE_JOBS가 있으면 그 값이 우선한다.
MAX_ACTIVE_JOBS = int(os.environ.get("NIGHTSHIFT_MAX_ACTIVE_JOBS", "100"))

job_queue: "queue.Queue[str]" = queue.Queue()
jobs: dict[str, dict] = {}
lock = threading.Lock()

# 자동 실행 모드 — 켜져 있는 동안에는 POST /api/upload로 새로 추가되는 작업도
# pending에 머무르지 않고 바로 큐에 들어간다("▶ 시작"/"⏸ 정지" 토글). "⏸ 정지"는
# 새 작업을 더 안 받는 것과 동시에, 지금 실행 중인 작업의 서브프로세스도 즉시
# 종료 요청한다(아래 stop_current_job 참고). 서버가 재시작되면 큐에 남아있던
# 작업이 interrupted로 표시되는 것과 같은 이유로 이 값도 초기화된다(재시작
# 후 자동으로 다시 돌기 시작하면 안 되므로).
auto_run = False

# worker_loop가 지금 돌리고 있는 서브프로세스 — "⏸ 정지"가 이걸 종료시킬 수
# 있게 lock으로 보호된 상태로 들고 있는다. 실행 중인 작업이 없으면 둘 다 None.
current_job_id: str | None = None
current_process: subprocess.Popen | None = None


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


def check_comfy_url(url: str, timeout: float = COMFY_CHECK_TIMEOUT) -> bool:
    try:
        req = urllib.request.Request(f"{url.rstrip('/')}/system_stats")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def configured_comfy_url() -> tuple[str | None, str]:
    """명시적으로 지정된 ComfyUI 주소와 그 출처("setting"/"env"). 어느 쪽에도
    지정돼 있지 않으면 (None, "auto") — 이때만 자동 탐지로 넘어간다."""
    saved = (comfy_endpoint.get("url") or "").strip()
    if saved:
        return saved, "setting"
    env_url = (os.environ.get("COMFY_URL") or "").strip()
    if env_url:
        return env_url, "env"
    return None, "auto"


def resolve_comfy_url() -> tuple[str | None, bool]:
    """(지금 쓸 ComfyUI 주소, 연결 가능 여부). 우선순위는 COMFY_ENDPOINT_FILE 설명 참고."""
    url, _source = configured_comfy_url()
    if url:
        return url, check_comfy_url(url)
    for candidate in COMFY_CANDIDATE_URLS:
        if check_comfy_url(candidate):
            return candidate, True
    return None, False


# ---- ComfyUI 노드/모델 목록 조회 ----------------------------------------------
# ComfyUI의 /object_info는 그 서버에 설치된 모든 노드 타입과, 파일을 고르는 입력
# (체크포인트/LoRA/VAE 등)이 실제로 고를 수 있는 선택지까지 통째로 돌려준다.
# 워크플로우 JSON은 결국 "노드 이름 + 입력값"일 뿐이라, 이 목록만 있으면 업로드된
# 워크플로우가 이 서버에서 돌아갈 수 있는지 미리 검사할 수 있다.
# 커스텀 노드가 많이 깔린 서버에서는 응답이 수 MB까지 커지고 모델 폴더를 훑느라
# 느리기도 해서 짧게 캐싱한다 — 모델을 새로 설치하는 일은 드물고, 필요하면
# refresh=true로 강제로 다시 받아온다.
COMFY_OBJECT_INFO_TTL_SEC = 120
_object_info_cache: dict = {"url": None, "data": None, "fetched_at": 0.0}

# ComfyUI에 "설치된 모델 목록" 전용 API는 없어서, 각 종류를 대표하는 로더 노드의
# 입력 선택지를 그대로 읽어 쓴다(그 노드가 없는 서버면 빈 목록).
MODEL_LIST_SOURCES = {
    "checkpoints": ("CheckpointLoaderSimple", "ckpt_name"),
    "loras": ("LoraLoader", "lora_name"),
    "vae": ("VAELoader", "vae_name"),
    "controlnet": ("ControlNetLoader", "control_net_name"),
    "upscale_models": ("UpscaleModelLoader", "model_name"),
    "clip_vision": ("CLIPVisionLoader", "clip_name"),
}


def fetch_comfy_object_info(force: bool = False) -> tuple[str | None, dict | None]:
    """(comfy_url, object_info) — ComfyUI가 안 떠 있으면 (url, None)."""
    comfy_url, connected = resolve_comfy_url()
    if not connected or not comfy_url:
        return comfy_url, None
    now = time.time()
    if (
        not force
        and _object_info_cache["data"] is not None
        and _object_info_cache["url"] == comfy_url
        and now - _object_info_cache["fetched_at"] < COMFY_OBJECT_INFO_TTL_SEC
    ):
        return comfy_url, _object_info_cache["data"]
    req = urllib.request.Request(f"{comfy_url.rstrip('/')}/object_info")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    _object_info_cache.update({"url": comfy_url, "data": data, "fetched_at": now})
    return comfy_url, data


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


def stop_current_job() -> str | None:
    """지금 worker_loop가 돌리고 있는 서브프로세스에 종료를 요청한다("⏸ 정지").
    실행 중인 작업이 있었으면 그 job_id를, 없었으면 None을 반환한다. SIGTERM을
    무시하고 계속 살아있는 경우를 대비해 잠시 후에도 안 죽어있으면 강제 종료
    (kill)하는 감시 스레드를 하나 띄운다."""
    with lock:
        if current_process is None or current_job_id is None:
            return None
        job_id = current_job_id
        jobs[job_id]["_stop_requested"] = True
        proc = current_process

    proc.terminate()

    def _kill_if_still_alive():
        time.sleep(5)
        if proc.poll() is None:
            proc.kill()

    threading.Thread(target=_kill_if_still_alive, daemon=True).start()
    return job_id


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


def wait_for_comfy(job_id: str) -> str | None:
    """ComfyUI에 연결될 때까지 붙잡고 있다가, 연결되면 그때 쓸 주소를 돌려준다.

    예전에는 큐에서 꺼낸 작업을 곧바로 실행 상태로 바꾼 뒤 연결을 확인하고, 안 되면
    그 자리에서 failed 처리했다. ComfyUI가 같은 머신에서 항상 같이 떠 있던 시절에는
    그게 맞았지만, GPU pod가 따로 있는 구성에서는 pod가 잠깐 꺼져 있는 동안 대기 중인
    작업이 순식간에 전멸한다 — 큐가 한 번에 한 개씩 돌기 때문에 수십 개가 몇 초 만에
    차례로 실패한다. 그래서 이제는 실패시키지 않고 "queued"인 채로 기다린다.

    기다리기를 그만둬야 하는 경우에는 None을 돌려준다:
      - 그 사이 작업이 삭제됐다 → 그냥 건너뛴다.
      - "⏸ 정지"로 auto_run이 꺼졌다 → 작업을 "pending"으로 되돌린다. 다음 "▶ 시작"
        때 이어서 돌고, 무한정 기다리는 상태에서 빠져나오는 탈출구이기도 하다
        (대기 중인 작업은 status가 "queued"라 그대로는 삭제할 수 없다).
    """
    waited = False
    while True:
        comfy_url, connected = resolve_comfy_url()

        with lock:
            job = jobs.get(job_id)
            gone = job is None or job.get("deleted")
            if job is not None:
                if connected or gone:
                    set_comfy_wait_flag(job, False)
        if gone:
            if waited:
                save_state()
            return None
        if connected:
            if waited:
                save_state()
            return comfy_url

        with lock:
            keep_waiting = auto_run
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
            where = comfy_url or "자동 탐지 실패"
            (LOGS_DIR / f"{job_id}.log").write_text(
                f"ComfyUI(GPU) 서버에 연결할 수 없어 대기 목록으로 되돌렸어요 ({where}).\n"
                "서버가 켜진 걸 확인한 뒤 ▶ 시작을 누르면 이어서 실행됩니다.\n",
                encoding="utf-8",
            )
            return None
        waited = True

        # COMFY_WAIT_RETRY_SEC를 통째로 자면 그동안 "⏸ 정지"에 반응하지 못하므로
        # 잘게 쪼개 자면서 중간에 빠져나올 조건을 확인한다.
        slept = 0.0
        while slept < COMFY_WAIT_RETRY_SEC:
            time.sleep(COMFY_WAIT_TICK_SEC)
            slept += COMFY_WAIT_TICK_SEC
            with lock:
                job = jobs.get(job_id)
                if not auto_run or job is None or job.get("deleted"):
                    break


def pull_job_outputs(job_id: str, comfy_url: str, log_path: Path):
    """작업 하나가 끝난 뒤 그 작업(job_id 하위 폴더)의 결과 이미지만 끌어온다.

    설정이 꺼져 있으면 아무것도 안 한다. 실패해도 작업 상태에는 영향을 주지 않는다 —
    이미지는 원격에 그대로 남아 있고 갤러리 탭에서 수동으로 다시 가져올 수 있으므로,
    이미 끝난 작업을 실패로 뒤집을 이유가 없다. 대신 무슨 일이 있었는지 그 작업의
    로그 끝에 덧붙여, 이미지가 안 보일 때 이유를 찾을 수 있게 한다."""
    if not comfy_endpoint.get("pull_outputs"):
        return
    try:
        result = sync_outputs(comfy_url, only_subfolder=job_id)
    except OutputSyncError as e:
        note = f"[출력 동기화] 실패: {e}"
    else:
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


def worker_loop():
    global current_job_id, current_process
    while True:
        job_id = job_queue.get()

        # ComfyUI(GPU)가 아직 안 떠 있으면 여기서 붙잡아 둔다 — 작업은 "queued"인 채
        # waiting_for_comfy 표시만 붙고, 연결되는 순간 이어서 실행된다.
        comfy_url = wait_for_comfy(job_id)
        if comfy_url is None:
            job_queue.task_done()
            continue

        with lock:
            job = jobs.get(job_id)
            if job is None or job.get("deleted"):
                job_queue.task_done()
                continue
            job["status"] = "running"
            job["started_at"] = now_iso()
            job["progress"] = None
        save_state()

        log_path = LOGS_DIR / f"{job_id}.log"
        script_path = TEMPLATES_DIR / job["script_filename"]

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
                proc = subprocess.Popen(
                    ["python3", "-u", str(script_path)],
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    env=env,
                )
                with lock:
                    current_job_id = job_id
                    current_process = proc
                returncode = proc.wait()
            except Exception as e:
                logf.write(f"\n[runner error] {e}\n")
                returncode = -1
            finally:
                with lock:
                    stopped = current_job_id == job_id and job.pop("_stop_requested", False)
                    current_job_id = None
                    current_process = None

        with lock:
            job["status"] = "interrupted" if stopped else ("done" if returncode == 0 else "failed")
            job["returncode"] = returncode
            job["finished_at"] = now_iso()
        save_state()

        # ComfyUI가 원격이면 결과 이미지는 그쪽 디스크에만 있다 — 갤러리/zip/이메일은
        # 전부 로컬 출력 폴더를 읽으므로, 작업이 끝난 직후 그 작업 몫만 끌어온다.
        # 중간에 실패했더라도 그때까지 나온 이미지는 가져온다(returncode를 안 본다).
        pull_job_outputs(job_id, comfy_url, log_path)

        job_queue.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_state()
    recent_workflows_store.load()
    recent_csvs_store.load()
    load_danbooru_state()
    load_lora_triggers()
    load_base_model_families()
    load_comfy_endpoint()
    # 재시작 전에 running/queued 상태로 남아있던 기록은 재실행되지 않으므로 상태만 정리.
    # ComfyUI 연결을 기다리던 중이었다는 표시(waiting_for_comfy)도 함께 지운다 — 그
    # 대기는 worker_loop 안에서만 살아 있는 상태라 재시작하면 남아 있을 이유가 없다.
    with lock:
        for job in jobs.values():
            if job["status"] in ("queued", "running"):
                job["status"] = "interrupted"
            set_comfy_wait_flag(job, False)
    save_state()
    threading.Thread(target=worker_loop, daemon=True).start()
    yield


app = FastAPI(title="RunPod Job Queue", lifespan=lifespan)

# 이 앱은 원래 인증이 전혀 없었다(README "주의사항" 참고) — RunPod에 노출된 포트를
# 사람이 직접 브라우저로 조작하는 걸 전제로 했기 때문. 이제 사람 대신(또는 사람과
# 함께) LLM이 API를 호출해 잡 큐를 조작할 수 있게 하려는데, 그러려면 최소한
# "누구나 이 포트에 닿으면 잡을 큐잉/삭제할 수 있는" 상태는 막아야 한다.
#
# NIGHTSHIFT_API_KEY를 설정하면 모든 /api/* 요청에 X-API-Key(또는
# Authorization: Bearer) 헤더로 같은 값을 요구한다. 정적 파일(/)은 그대로 열어둔다
# — index.html 자체에는 민감한 정보가 없고, 막아봤자 브라우저에서 볼 수 있는
# 소스만 가리는 것이라 의미가 없다. 값이 비어 있으면(기존 동작 그대로) 인증 없이
# 실행되며, 시작 시 경고를 한 번 남긴다.
API_KEY = os.environ.get("NIGHTSHIFT_API_KEY", "").strip()
if not API_KEY:
    logging.getLogger("uvicorn.error").warning(
        "NIGHTSHIFT_API_KEY가 설정되지 않아 인증 없이 실행됩니다. "
        "외부에 노출하거나 LLM이 이 API를 직접 호출하게 할 계획이라면 "
        ".env에 NIGHTSHIFT_API_KEY를 설정하세요 (.env.example 참고)."
    )


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    if not API_KEY or not request.url.path.startswith("/api/"):
        return await call_next(request)

    provided = request.headers.get("x-api-key")
    if not provided:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            provided = auth_header[len("bearer "):]

    if not provided or not hmac.compare_digest(provided, API_KEY):
        return JSONResponse({"detail": "API 키가 없거나 올바르지 않아요."}, status_code=401)

    return await call_next(request)


@app.get("/api/templates")
def list_templates():
    return load_templates_list()


@app.get("/api/comfy-status")
async def comfy_status():
    url, connected = await asyncio.to_thread(resolve_comfy_url)
    _, source = configured_comfy_url()
    return {
        "url": url,
        "connected": connected,
        "source": source,
        # 화면이 갤러리의 "결과 가져오기" 버튼을 보여줄지 정하는 데 쓴다 — 5초마다
        # 폴링하는 이 응답에 실어 주면 설정을 바꿨을 때 저절로 따라온다.
        "pull_outputs": bool(comfy_endpoint.get("pull_outputs")),
    }


def comfy_endpoint_payload() -> dict:
    """설정 화면이 쓰는 현재 상태 — 저장된 값, 실제로 쓰이는 값과 그 출처."""
    effective_url, source = configured_comfy_url()
    return {
        "url": comfy_endpoint.get("url") or "",          # 저장된 설정값(비어 있으면 미설정)
        "effective_url": effective_url,                   # 설정/환경변수로 정해진 주소(자동 탐지면 null)
        "source": source,                                 # "setting" | "env" | "auto"
        "env_url": (os.environ.get("COMFY_URL") or ""),   # 참고용 — 설정을 비웠을 때 쓰일 값
        "candidates": COMFY_CANDIDATE_URLS,               # 자동 탐지가 훑는 후보들
        "updated_at": comfy_endpoint.get("updated_at"),
        "pull_outputs": bool(comfy_endpoint.get("pull_outputs")),  # 결과 이미지를 HTTP로 끌어올지
        "output_dir": OUTPUT_DIR,                         # 끌어온 이미지가 쌓이는 로컬 폴더
        "output_sync": sync_state_summary(),              # {"last_sync", "known"}
    }


@app.get("/api/comfy-endpoint")
def get_comfy_endpoint():
    return comfy_endpoint_payload()


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
    try:
        url = normalize_comfy_url(data.get("url", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))

    comfy_endpoint["url"] = url
    comfy_endpoint["updated_at"] = now_iso()
    # pull_outputs를 아예 안 보내면 지금 설정을 유지한다 — 주소만 바꾸려는 요청이
    # 조용히 "가져오기 끄기"로 동작하면 안 되므로.
    if "pull_outputs" in data:
        comfy_endpoint["pull_outputs"] = bool(data.get("pull_outputs"))
    save_comfy_endpoint()
    # 주소가 바뀌면 이전 서버에서 받아둔 모델/노드 목록은 더 이상 그 서버의 것이
    # 아니다. 캐시 키에 url이 들어 있어 자연히 미스가 나지만, 명시적으로 비워서
    # "바꾼 직후 잠깐 옛 목록이 보이는" 창을 없앤다.
    _object_info_cache.update({"url": None, "data": None, "fetched_at": 0.0})

    payload = comfy_endpoint_payload()
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
    try:
        url = normalize_comfy_url(data.get("url", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))

    if not url:
        url, _ = await asyncio.to_thread(resolve_comfy_url)
        if not url:
            return {"url": None, "connected": False, "detail": "확인할 주소가 없어요(자동 탐지도 실패)."}
    connected = await asyncio.to_thread(check_comfy_url, url, COMFY_CHECK_TIMEOUT_INTERACTIVE)
    return {"url": url, "connected": connected}


@app.get("/api/comfy-object-info")
async def comfy_object_info(refresh: bool = False):
    # 지금 연결된 ComfyUI에 설치된 노드 타입 이름들과 종류별 모델 목록만 추려서
    # 돌려준다(원본 /object_info는 입력 스펙까지 들어있어 수 MB가 되기도 해서
    # 그대로 브라우저로 넘기지 않는다). ComfyUI가 안 떠 있어도 에러가 아니라
    # connected=false + 빈 목록 — 화면에서 "연결 안 됨"으로 안내만 하면 되니까.
    try:
        comfy_url, object_info = await asyncio.to_thread(fetch_comfy_object_info, refresh)
    except Exception as e:
        raise HTTPException(502, f"ComfyUI 노드 목록을 가져오지 못했어요: {e}")
    if object_info is None:
        return {
            "connected": False,
            "url": comfy_url,
            "node_types": [],
            "models": {key: [] for key in MODEL_LIST_SOURCES},
        }
    return {
        "connected": True,
        "url": comfy_url,
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
    return lora_triggers


@app.put("/api/lora-triggers")
async def put_lora_triggers(request: Request):
    # "🎛 LoRA" 탭이 편집할 때마다 전체 매핑을 통째로 보내서 그대로 덮어쓴다 —
    # danbooru tag-edits와 같은 이유로(개수가 많지 않고 편집도 잦지 않아 부분
    # patch를 둘 이유가 없음). 각 값은 {trigger: str, families: [family_id, ...]}
    # 형태여야 한다(families가 빈 목록이면 모든 베이스 모델과 호환 취급).
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    if not isinstance(data, dict):
        raise HTTPException(400, "{LoRA 파일명: {trigger, families}} 형태의 객체여야 해요.")

    cleaned: dict[str, dict] = {}
    for name, value in data.items():
        if not isinstance(value, dict):
            raise HTTPException(400, f"'{name}' 값은 {{trigger, families}} 형태의 객체여야 해요.")
        trigger = value.get("trigger", "")
        families = value.get("families", [])
        if not isinstance(trigger, str):
            raise HTTPException(400, f"'{name}'의 trigger는 문자열이어야 해요.")
        if not isinstance(families, list) or not all(isinstance(f, str) for f in families):
            raise HTTPException(400, f"'{name}'의 families는 문자열 목록이어야 해요.")
        if trigger.strip() or families:
            cleaned[name] = {"trigger": trigger, "families": families}

    lora_triggers.clear()
    lora_triggers.update(cleaned)
    save_lora_triggers()
    return lora_triggers


@app.get("/api/input-images")
def get_input_images():
    # img2img/USDU 워크플로우 유형이 "입력 이미지" 선택 드롭다운을 채우는 데 쓴다.
    # ref_assets의 pose/depth/lineart와 달리 세트/char_no 구분이 없는 평평한 목록
    # (input_assets.py 모듈 설명 참고) — 업로드 API는 없고 조회만 한다.
    return {"images": list_input_images()}


@app.get("/api/base-model-families")
def get_base_model_families():
    return base_model_families


@app.put("/api/base-model-families")
async def put_base_model_families(request: Request):
    # "🎛 LoRA" 탭(베이스 모델 관리 부분)이 전체 family 목록을 통째로 보내서
    # 덮어쓴다 — lora-triggers와 같은 whole-blob 패턴.
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    if not isinstance(data, dict):
        raise HTTPException(400, "{family_id: {label, checkpoints}} 형태의 객체여야 해요.")

    cleaned: dict[str, dict] = {}
    for family_id, value in data.items():
        if not isinstance(value, dict):
            raise HTTPException(400, f"'{family_id}' 값은 {{label, checkpoints}} 형태의 객체여야 해요.")
        label = value.get("label", "")
        checkpoints = value.get("checkpoints", [])
        if not isinstance(label, str) or not label.strip():
            raise HTTPException(400, f"'{family_id}'의 label은 비어있지 않은 문자열이어야 해요.")
        if not isinstance(checkpoints, list) or not all(isinstance(c, str) for c in checkpoints):
            raise HTTPException(400, f"'{family_id}'의 checkpoints는 문자열 목록이어야 해요.")
        cleaned[family_id] = {"label": label, "checkpoints": checkpoints}

    base_model_families.clear()
    base_model_families.update(cleaned)
    save_base_model_families()
    return base_model_families


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
    ],
    "post": [
        {"id": "hires_fix", "label": "Hires Fix"},
        {"id": "usdu", "label": "Ultimate SD Upscale", "requires_node": "UltimateSDUpscaleNoUpscale"},
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


@app.put("/api/workflow-presets/{family_id}/{type_id}")
async def put_workflow_preset(family_id: str, type_id: str, request: Request):
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
def delete_workflow_preset(family_id: str, type_id: str):
    path = WORKFLOW_PRESETS_DIR / preset_filename(family_id, type_id)
    if not path.exists():
        raise HTTPException(404, "해당 조합의 프리셋 워크플로우가 없어요.")
    path.unlink()
    return {"ok": True}


@app.post("/api/build-workflow")
async def build_workflow_api(request: Request):
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
        _, object_info = await asyncio.to_thread(fetch_comfy_object_info, False)
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

        require_installed(str(spec.get("checkpoint") or "").strip(), "checkpoints", "체크포인트")
        require_installed(str(spec.get("vae") or "").strip(), "vae", "VAE")
        for lora in (spec.get("loras") or []):
            if isinstance(lora, dict):
                require_installed(str(lora.get("name") or "").strip(), "loras", "LoRA")
        for field, label in (("sampler_name", "샘플러"), ("scheduler", "스케줄러")):
            value = str(spec.get(field) or "").strip()
            choices = combo_choices(object_info, "KSampler", field)
            if value and choices and value not in choices:
                raise HTTPException(400, f"{label} '{value}'은(는) 이 ComfyUI가 지원하지 않아요.")

    try:
        workflow = build_workflow(spec)
    except WorkflowBuildError as e:
        raise HTTPException(400, str(e))
    return {"workflow": workflow}


@app.post("/api/validate-workflow")
async def validate_workflow(request: Request):
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
        comfy_url, object_info = await asyncio.to_thread(fetch_comfy_object_info, False)
    except Exception as e:
        raise HTTPException(502, f"ComfyUI 노드 목록을 가져오지 못했어요: {e}")
    if object_info is None:
        # 연결이 안 됐으면 "문제 없음"이 아니라 "확인 못 함"이다 — 화면에서 구분해서 안내한다.
        return {
            "connected": False, "url": comfy_url, "ok": True,
            "missing_nodes": [], "missing_values": [], "checked_nodes": 0,
        }

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
            continue  # 노드 자체가 없으면 입력값은 검사할 기준도 없다
        for field, value in (node.get("inputs") or {}).items():
            # 링크([노드id, 출력번호])나 숫자 입력은 검사 대상이 아니고, 목록에서
            # 고르는 입력(체크포인트/LoRA/샘플러 이름 등)만 실제 선택지와 대조한다.
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
            _, object_info = fetch_comfy_object_info(False)
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


@app.get("/api/recent-workflows")
def list_recent_workflows():
    # "새 작업 추가"의 워크플로우 슬롯 옆 "최근 워크플로우" 버튼이 호출한다.
    return {"workflows": recent_workflows_store.list_meta(), "retention": recent_workflows_store.retention}


@app.get("/api/recent-workflows/{workflow_id}")
def get_recent_workflow(workflow_id: str):
    entry = recent_workflows_store.get(workflow_id)
    if entry is None:
        raise HTTPException(404, "해당 워크플로우를 찾을 수 없어요.")
    path = recent_workflows_store.dir_path / entry["stored_filename"]
    if not path.exists():
        raise HTTPException(404, "워크플로우 파일이 서버에 없어요.")
    return Response(content=path.read_text(encoding="utf-8"), media_type="application/json")


@app.delete("/api/recent-workflows/{workflow_id}")
def delete_recent_workflow(workflow_id: str):
    if not recent_workflows_store.delete(workflow_id):
        raise HTTPException(404, "해당 워크플로우를 찾을 수 없어요.")
    return {"ok": True}


@app.get("/api/recent-csvs")
def list_recent_csvs():
    # "새 작업 추가"의 CSV 슬롯 옆 "최근 CSV" 버튼이 호출한다.
    return {"csvs": recent_csvs_store.list_meta(), "retention": recent_csvs_store.retention}


@app.get("/api/recent-csvs/{csv_id}")
def get_recent_csv(csv_id: str):
    entry = recent_csvs_store.get(csv_id)
    if entry is None:
        raise HTTPException(404, "해당 CSV를 찾을 수 없어요.")
    path = recent_csvs_store.dir_path / entry["stored_filename"]
    if not path.exists():
        raise HTTPException(404, "CSV 파일이 서버에 없어요.")
    return Response(content=path.read_text(encoding="utf-8"), media_type="text/csv")


@app.delete("/api/recent-csvs/{csv_id}")
def delete_recent_csv(csv_id: str):
    if not recent_csvs_store.delete(csv_id):
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
) -> dict:
    # POST /api/upload(사람이 브라우저에서 파일 첨부)와 POST /api/jobs(LLM 등
    # 프로그램이 JSON으로 호출)가 공유하는 실제 잡 생성 로직 — 두 경로 모두
    # 워크플로우/CSV를 이미 bytes로, 옵션을 이미 {name: 원본 문자열} 형태로
    # 만들어서 넘겨준다. 그 앞단(멀티파트 폼 파싱 vs JSON 파싱)만 다르다.
    with lock:
        active_count = sum(
            1 for j in jobs.values()
            if j["status"] in ("pending", "queued", "running") and not j.get("deleted")
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

    if primary_kind:
        needs_ref_node = True
        if primary_csv_column:
            needs_ref_node = csv_bytes is not None and csv_has_ref_value(csv_bytes, primary_csv_column)
        if needs_ref_node:
            validate_workflow_has_ref_node(workflow_bytes, primary_kind)

    flat_image_spec = FLAT_IMAGE_TEMPLATES.get(template_id)
    if flat_image_spec:
        if csv_bytes is not None:
            validate_flat_image_csv_rows(csv_bytes, flat_image_spec["column"])
        validate_workflow_has_flat_image_node(
            workflow_bytes, flat_image_spec["node_title_env"], flat_image_spec["default_title"], flat_image_spec["label"],
        )

    # comfy_model 옵션(체크포인트/LoRA 드롭다운)을 쓰는 템플릿이면 설치 목록으로
    # 값을 검증해야 한다. coerce_option은 동기 함수라, 여기서 미리 스레드로 받아
    # 캐시를 채워둔다 — 안 그러면 그 안의 ComfyUI 조회가 이벤트 루프를 막는다.
    # 못 받아오면(꺼져 있음 등) coerce_option이 검증을 건너뛴다.
    if any(o.get("type") == "comfy_model" for o in template.get("options", [])):
        try:
            await asyncio.to_thread(fetch_comfy_object_info, False)
        except Exception:
            pass

    options = {}
    for option in template.get("options", []):
        raw = raw_options.get(option["name"])
        options[option["name"]] = coerce_option(option, raw, options)

    job_id = str(uuid.uuid4())[:8]

    workflow_dest_name = f"{job_id}_{workflow_filename}"
    (JOBS_DIR / workflow_dest_name).write_bytes(workflow_bytes)
    recent_workflows_store.record(workflow_filename, workflow_bytes)

    csv_dest_name = None
    csv_original_name = None
    if requires_csv:
        csv_dest_name = f"{job_id}_{csv_filename}"
        (JOBS_DIR / csv_dest_name).write_bytes(csv_bytes)
        csv_original_name = csv_filename
        recent_csvs_store.record(csv_filename, csv_bytes)

    with lock:
        jobs[job_id] = {
            "id": job_id,
            "template_id": template_id,
            "template_label": template["label"],
            "script_filename": template["script_filename"],
            "options": options,
            "workflow_filename": workflow_dest_name,
            "workflow_original_name": workflow_filename,
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
        # 자동 실행 모드("▶ 시작"이 켜져 있는 동안)면 대기 목록에 머무르지 않고
        # 바로 실행 큐에 넣는다 — 그래야 켜놓은 동안 새로 추가하는 작업이 계속
        # 이어서 처리된다.
        auto_queued = auto_run
        if auto_queued:
            jobs[job_id]["status"] = "queued"
    save_state()
    if auto_queued:
        job_queue.put(job_id)
    return jobs[job_id]


@app.post("/api/upload")
async def upload(request: Request):
    form = await request.form()

    template = resolve_template(form.get("template_id"))

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

    raw_options = {}
    for option in template.get("options", []):
        value = form.get(option["name"])
        raw_options[option["name"]] = value if isinstance(value, str) else None

    return await create_job(
        template,
        workflow_bytes,
        workflow.filename,
        csv_bytes,
        csv_file.filename if requires_csv else None,
        raw_options,
    )


@app.post("/api/jobs")
async def create_job_from_json(request: Request):
    # /api/upload과 완전히 같은 파이프라인(create_job)을 파일 첨부 없이 JSON
    # 바디로 쓸 수 있게 한 것. 사람은 브라우저에서 파일을 고르지만, curl이나
    # LLM처럼 프로그램으로 호출하는 쪽엔 multipart/form-data보다 JSON이 훨씬
    # 다루기 쉽다 — 워크플로우는 POST /api/build-workflow로 만든 걸 그대로
    # 넣거나 직접 준 JSON을 쓰면 되고, CSV는 원문 문자열 그대로 준다(csv_batch.
    # sample.csv 같은 형식). 큐에 실제로 등록된다는 점은 /api/upload와 동일하다
    # — 미리보기가 필요하면 POST /api/validate-workflow를 먼저 불러볼 것.
    try:
        body = json.loads(await request.body())
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    if not isinstance(body, dict):
        raise HTTPException(400, "요청 본문이 JSON 객체여야 해요.")

    template = resolve_template(body.get("template_id"))

    workflow = body.get("workflow")
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

    return await create_job(template, workflow_bytes, workflow_filename, csv_bytes, csv_filename, raw_options)


@app.post("/api/queue/start")
def start_queue():
    # 자동 실행 모드를 켠다 — 지금 대기 중인 작업을 전부 큐에 넣는 것은 물론,
    # 켜져 있는 동안 POST /api/upload로 새로 추가되는 작업도 (auto_run 체크를
    # 통해) 계속 이어서 큐에 들어간다. "⏸ 정지"를 누르기 전까지는 계속 켜져 있다.
    global auto_run
    with lock:
        auto_run = True
        # deleted도 함께 확인해야 한다 — delete_job()은 소프트 삭제라 status를
        # "pending"으로 그대로 둔 채 deleted=True만 표시하므로, 이 필터가 없으면
        # 삭제된(그래서 화면에는 안 보이는) 작업이 여기서 다시 주워져 실행 큐에
        # 들어가는 사고가 난다.
        pending = sorted(
            (j for j in jobs.values() if j["status"] == "pending" and not j.get("deleted")),
            key=lambda j: j["queued_at"],
        )
        for job in pending:
            job["status"] = "queued"
    save_state()
    for job in pending:
        job_queue.put(job["id"])
    return {"running": True, "started": len(pending)}


@app.post("/api/queue/stop")
def stop_queue():
    # 자동 실행 모드를 끈다 — 이후 POST /api/upload로 추가되는 작업은 다시
    # "▶ 시작"을 누르기 전까지 pending 상태로 대기 목록에만 쌓인다. 이미 큐에
    # 들어가 있지만 아직 안 돈 작업(queued)은 그대로 대기 상태로 남고(다음
    # "▶ 시작" 때 이어서 돎), 지금 실행 중인(running) 작업 하나는
    # stop_current_job()으로 즉시 종료 요청한다 — 완전히 죽을 때까지 몇 초
    # 걸릴 수 있으니 job 상태가 "interrupted"로 바뀌는 건 GET /api/jobs로
    # 잠시 후 확인해야 한다.
    global auto_run
    with lock:
        auto_run = False
    stopped_job_id = stop_current_job()
    return {"running": False, "stopped_job_id": stopped_job_id}


@app.post("/api/jobs/clear-completed")
def clear_completed_jobs():
    # 다 끝난 작업(성공/실패/중단)을 목록에서 한꺼번에 치우고 싶을 때 쓴다 —
    # delete_job()과 같은 소프트 삭제라 "삭제된 작업 설정 불러오기"로 실수로
    # 지운 작업도 되돌릴 수 있다. pending/queued/running은 여기서 건드리지
    # 않는다 — 아직 시작 안 했거나 진행 중인 작업까지 같이 지우면 안 되므로.
    with lock:
        completed = [j for j in jobs.values() if j["status"] in ("done", "failed", "interrupted") and not j.get("deleted")]
        now = now_iso()
        for job in completed:
            job["deleted"] = True
            job["deleted_at"] = now
        prune_deleted_jobs()
    save_state()
    return {"cleared": len(completed)}


@app.get("/api/jobs")
def list_jobs():
    with lock:
        ordered = sorted(
            (j for j in jobs.values() if not j.get("deleted")),
            key=lambda j: j["queued_at"],
            reverse=True,
        )
        running = auto_run
    pending_ids = list(job_queue.queue)
    return {"jobs": ordered, "pending_count": len(pending_ids), "running": running}


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


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str):
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
    job_queue.put(job_id)
    return jobs[job_id]


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
    base = Path(OUTPUT_DIR)
    items = []
    for f in files:
        stat = f.stat()
        rel = f.relative_to(base)
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
            "name": rel.as_posix(),
            "job_id": job_id,
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "width": width,
            "height": height,
        })
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items


@app.get("/api/output-images")
def list_output_images_api():
    return {"images": list_output_images_meta()}


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
    # 원격 ComfyUI가 만든 결과 이미지를 로컬 출력 폴더로 끌어온다(comfy_outputs.py).
    # 작업이 끝날 때마다 자동으로도 돌지만(pull_outputs 설정), pod를 껐다 켠 뒤 밀린
    # 것을 한꺼번에 받거나 설정을 뒤늦게 켠 경우를 위해 수동으로도 돌릴 수 있다.
    data = await read_json_object(request)
    job_id = (data.get("job_id") or "").strip() or None
    force = bool(data.get("force"))

    url, connected = await asyncio.to_thread(resolve_comfy_url)
    if not url or not connected:
        raise HTTPException(503, "ComfyUI에 연결할 수 없어 결과 이미지를 가져올 수 없어요.")
    try:
        result = await asyncio.to_thread(sync_outputs, url, only_subfolder=job_id, force=force)
    except OutputSyncError as e:
        raise HTTPException(502, str(e))
    return {"url": url, **result}


@app.post("/api/comfy-outputs/forget")
async def forget_comfy_outputs(request: Request):
    # "한 번 받아온 이미지"라는 기록을 지운다 — 지운 이미지가 다음 동기화에서 되살아나지
    # 않게 하는 것이 이 기록의 목적이므로, 정말 다시 받고 싶을 때만 쓰는 탈출구다.
    # (한 번만 다시 받으면 되는 경우라면 sync의 force=true가 더 간단하다.)
    data = await read_json_object(request)
    job_id = (data.get("job_id") or "").strip() or None
    removed = await asyncio.to_thread(forget_downloaded, job_id)
    return {"forgotten": removed, **sync_state_summary()}


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
    return path


# :path 컨버터는 "/"를 포함해 뒤에 오는 걸 전부 욕심껏(greedy) 먹어버려서, 아래
# thumbnail 라우트를 원본 이미지 라우트보다 먼저 등록해야 한다 — 순서가 바뀌면
# "job_id/파일.png/thumbnail" 요청도 원본 이미지 라우트(filename:path)가 먼저
# 통째로 집어삼켜서 404가 난다(FastAPI/Starlette는 등록 순서대로 첫 매치를 씀).
@app.get("/api/output-images/{filename:path}/thumbnail")
def get_output_image_thumbnail(filename: str, request: Request, size: int = 320):
    # 갤러리 격자를 채우는 용도 — 원본을 그대로 내려받으면 느리고 대역폭을 낭비하므로,
    # 매 요청마다 그 자리에서 축소본을 만들어 돌려준다(디스크에 캐시하지 않음 — 이
    # 도구 규모에서는 매번 다시 만들어도 충분히 빠름). 대신 브라우저 캐시는 쓴다 —
    # 갤러리 격자는 삭제/새로고침/선택 상태 변화 때마다 통째로 다시 그려지므로
    # (renderGalleryGrid), 캐시 헤더가 없으면 그때마다 같은 썸네일을 다시 인코딩해서
    # 보내게 된다. ETag는 파일 mtime+size+요청한 size로 만들어서, 파일이 바뀌면
    # (예: "가로형 이미지 자동 회전"이 같은 파일에 덮어쓰면 mtime이 바뀜) 자동으로
    # 무효화된다.
    path = resolve_output_image(filename)
    size = max(64, min(size, 800))
    stat = path.stat()
    etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}-{size}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private, max-age=86400"})
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail((size, size))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
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


def parse_image_names_body(data: dict) -> list[str]:
    names = data.get("names")
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise HTTPException(400, "이미지 파일명 목록(names)이 필요해요.")
    return names


def build_zip_from_paths(paths: list[Path]) -> Path:
    # 작업별 하위 폴더 구조 없이 파일명만으로 평평하게 담는다 — 압축을 풀었을 때
    # 폴더 구조 없이 한 자리에 전부 모여있길 원해서다. 서로 다른 작업 폴더에서
    # 온 파일이 우연히 같은 이름이면 zip 안에서 이름이 겹치므로, 그런 경우에만
    # "이름 (1).ext"처럼 번호를 붙여 구분한다.
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
            zf.write(f, arcname=arcname)
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


def rotate_one_image(path: Path) -> bool:
    # 가로형(너비 > 높이)일 때만 시계 방향 90도로 돌려서 같은 파일에 덮어쓴다 —
    # rotate-landscape처럼 hires-fix 비율(1536x704 등) 자동 판정까지는 안 하지만,
    # 이미 세로형이거나 정사각형인 이미지를 눕혀버리는 건 막는다. 회전했으면
    # True, 세로형/정사각형이라 건드리지 않았으면 False를 돌려준다.
    with Image.open(path) as img:
        if img.width <= img.height:
            return False
        img.transpose(Image.Transpose.ROTATE_270).save(path)
    return True


@app.post("/api/output-images/rotate-selected")
async def rotate_selected_images(request: Request):
    # 갤러리에서 고른 이미지 중 가로형만 시계 방향 90도로 돌려서 같은 파일에
    # 덮어쓴다 — rotate-landscape처럼 hires-fix 비율 자동 판정까지는 하지 않지만,
    # 세로형/정사각형 이미지는 그대로 둔다.
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    names = parse_image_names_body(data)

    rotated = []
    skipped = []
    for name in names:
        try:
            path = resolve_output_image(name)
        except HTTPException:
            continue
        try:
            did_rotate = await asyncio.to_thread(rotate_one_image, path)
        except Exception as e:
            raise HTTPException(500, f"{name} 회전에 실패했어요: {e}")
        (rotated if did_rotate else skipped).append(name)
    if not rotated and not skipped:
        raise HTTPException(404, "선택한 이미지를 찾을 수 없어요.")
    return {"rotated": rotated, "skipped": skipped}


@app.get("/api/danbooru/tag-edits")
def get_danbooru_tag_edits():
    return danbooru_tag_edits


@app.put("/api/danbooru/tag-edits")
async def put_danbooru_tag_edits(request: Request):
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


app.mount("/", StaticFiles(directory=str(BASE_DIR / "static"), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
