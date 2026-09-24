"""
nightshift 잡 큐 서버(app.py)를 MCP 도구로 감싸는 얇은 레이어.

app.py(FastAPI) 자체는 건드리지 않는다 — 여기서는 그 REST API를 그대로 호출만
해서, Claude 같은 MCP 클라이언트가 표준화된 도구 목록으로 잡 큐를 조작할 수
있게 한다. 웹 UI와 REST API는 기존 그대로 유지되고, 이 서버는 별도 프로세스
(별도 포트)로 떠서 app.py에 HTTP로 붙는다.

실행:
    pip install -r requirements.txt
    python3 mcp_server.py
    (pm2로 띄우려면 ecosystem.config.js의 nightshift-mcp 앱 참고)

환경변수:
    JOB_QUEUE_BASE_URL  app.py가 떠 있는 주소 (기본 http://127.0.0.1:8000 —
                        같은 머신에서 돈다면 내부 주소를 쓰는 게 RunPod 프록시
                        URL보다 빠르고 안정적이다)
    NIGHTSHIFT_MCP_KEY  (권장) app.py와 이 서버가 같이 읽는 내부 키(32자 이상 무작위 문자열).
                        설정하면 로그인하지 않고 요청마다 이 키를 X-Nightshift-MCP-Key 헤더로
                        실어 보내고, app.py는 같은 머신에서 직접 온 요청일 때만 admin으로 받는다
                        (app.py의 _mcp_key_user 참고). 사람의 admin 비밀번호를 .env에 적어 둘
                        필요가 없어진다 — 그 비밀번호는 JupyterLab(셸)의 열쇠이기도 해서다.
    JOB_QUEUE_USER      (NIGHTSHIFT_MCP_KEY가 없을 때만) app.py에 로그인할 회원 아이디
    JOB_QUEUE_PASSWORD  그 회원의 비밀번호. 이 회원의 권한·소유 범위 안에서만 도구가 동작한다
                        (일반 회원이면 자기 프로젝트/작업/결과물/파드만 보인다)
    MCP_SERVER_PORT     이 MCP 서버가 뜰 포트 (기본 8001, app.py의 8000과 겹치면 안 됨)
    MCP_SERVER_HOST     이 서버가 묶일 주소 (기본 127.0.0.1 — 외부 노출은 터널로만.
                        jupyterlab을 127.0.0.1에 묶은 것과 같은 이유, ecosystem.config.js 참고)
    MCP_AUTH_TOKEN      설정하면 이 값과 일치하는 Authorization: Bearer 헤더 또는
                        /mcp/<토큰> 경로로 온 요청만 통과시킨다(TokenAuthMiddleware
                        참고). 비워두면 인증이 아예 없다 — MCP_SERVER_HOST를
                        127.0.0.1로 두고 터널도 아직 안 걸었을 때만 그렇게 둔다.
"""

import asyncio
import json
import os
import secrets
import shutil
import sys
import tempfile
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastmcp import FastMCP
from fastmcp.utilities.types import Image
from starlette.middleware import Middleware
from starlette.responses import JSONResponse

BASE_URL = os.environ.get("JOB_QUEUE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
USERNAME = os.environ.get("JOB_QUEUE_USER", "").strip()
PASSWORD = os.environ.get("JOB_QUEUE_PASSWORD", "")
SESSION_COOKIE = "ns_session"
_session_token: str | None = None
MCP_INTERNAL_KEY = os.environ.get("NIGHTSHIFT_MCP_KEY", "").strip()
MCP_KEY_HEADER = "X-Nightshift-MCP-Key"

MCP_SERVER_HOST = os.environ.get("MCP_SERVER_HOST", "127.0.0.1").strip() or "127.0.0.1"
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "").strip()


class TokenAuthMiddleware:
    """MCP_AUTH_TOKEN이 설정돼 있으면 이 토큰을 확인하는 요청만 통과시킨다(설정 안
    했으면 그냥 통과 — 127.0.0.1 전용으로만 쓸 때를 위한 것).

    두 가지 방식을 다 받는다:
      1. `Authorization: Bearer <토큰>` 헤더.
      2. URL 경로에 토큰을 넣는 `/mcp/<토큰>` 형태.

    claude.ai의 "커스텀 커넥터 추가" 화면이 고정 헤더 입력을 지원하는지는 계정 종류에
    따라 다를 수 있다 — 공식 문서(2026-09-23 확인,
    https://claude.com/docs/connectors/building/authentication) 기준 `static_headers`
    (관리자가 커넥터 추가 시 입력하는 고정 Bearer/API 키 헤더)는 베타로 지원되지만,
    "organization administrator"가 입력한다는 표현이라 개인 계정 화면에 실제로 입력란이
    보이는지는 직접 확인해야 한다. 같은 문서는 URL에 토큰을 넣는 방식을 "권장하지 않음"
    이라고 명시한다(서버 로그·프록시·브라우저 기록에 남을 수 있어서, MCP 스펙도 쿼리
    스트링 토큰은 금지) — 그래도 헤더 입력란이 없는 계정에는 이게 유일한 자기서비스
    경로일 수 있어 대안으로 남겨 둔다. 헤더 입력란이 보이면 그쪽을 먼저 써라
    (README "MCP 서버" 절 참고)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not MCP_AUTH_TOKEN:
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth_header = headers.get(b"authorization", b"").decode("latin-1")
        path = scope.get("path", "")
        token_prefix = f"/mcp/{MCP_AUTH_TOKEN}"
        path_ok = path == token_prefix or path.startswith(token_prefix + "/")
        header_ok = auth_header.startswith("Bearer ") and secrets.compare_digest(auth_header[7:], MCP_AUTH_TOKEN)
        if not (path_ok or header_ok):
            response = JSONResponse({"error": "인증이 필요해요."}, status_code=401)
            await response(scope, receive, send)
            return
        if path_ok:
            # /mcp/<토큰>(/...) -> /mcp(/...) 로 벗겨서 실제 라우트에 전달한다.
            scope = dict(scope)
            scope["path"] = "/mcp" + path[len(token_prefix):]
        await self.app(scope, receive, send)

# 상태/목록 조회류는 가볍게 10초, 워크플로우 빌드나 잡 등록처럼 서버 쪽에서
# 검증 작업(ComfyUI 조회 등)이 걸릴 수 있는 호출은 30초로 넉넉하게 잡는다.
STATUS_TIMEOUT = 10.0
HEAVY_TIMEOUT = 30.0

TERMINAL_JOB_STATUSES = {"done", "failed", "interrupted"}


def _login() -> str | None:
    """app.py에 로그인해 세션 토큰을 받는다(실패하면 None — 그러면 호출이 401 안내로 끝난다)."""
    global _session_token
    if not USERNAME or not PASSWORD:
        return None
    try:
        resp = httpx.post(f"{BASE_URL}/api/auth/login", json={"username": USERNAME, "password": PASSWORD},
                          headers={"X-Requested-With": "nightshift"}, timeout=STATUS_TIMEOUT)
    except httpx.HTTPError:
        return None
    _session_token = _session_cookie_from(resp) if resp.status_code == 200 else None
    return _session_token


def _session_cookie_from(resp: httpx.Response) -> str | None:
    """Set-Cookie 헤더에서 세션 토큰을 직접 꺼낸다. resp.cookies를 쓰면 안 된다 — app.py가
    NIGHTSHIFT_COOKIE_DOMAIN(예: lomebrote.com, OpenCut SSO용)으로 쿠키에 Domain을 박으면,
    127.0.0.1로 요청한 httpx 쿠키 저장소가 도메인 불일치로 그 쿠키를 조용히 버려서 로그인은
    성공했는데 토큰이 None이 되고 모든 도구가 401로 끝난다. 여기서는 토큰 값만 필요하고
    보낼 때는 _client()가 쿠키를 직접 실어 보내므로 Domain 속성은 무시해도 된다."""
    for header in resp.headers.get_list("set-cookie"):
        jar = SimpleCookie()
        try:
            jar.load(header)
        except CookieError:
            continue
        if SESSION_COOKIE in jar:
            return jar[SESSION_COOKIE].value
    return None


async def _forget_expired_session(resp: httpx.Response) -> None:
    # 세션이 끝났으면(401) 토큰을 버려서 다음 호출이 다시 로그인하게 한다.
    global _session_token
    if resp.status_code == 401:
        _session_token = None


def _client(timeout: float) -> httpx.AsyncClient:
    headers = {"X-Requested-With": "nightshift"}
    if MCP_INTERNAL_KEY:
        # 내부 키가 있으면 로그인하지 않는다 — 비밀번호도 세션 쿠키도 쓰지 않는다.
        headers[MCP_KEY_HEADER] = MCP_INTERNAL_KEY
        return httpx.AsyncClient(base_url=BASE_URL, headers=headers, timeout=timeout)
    token = _session_token or _login()
    cookies = {SESSION_COOKIE: token} if token else None
    return httpx.AsyncClient(base_url=BASE_URL, headers=headers, cookies=cookies,
                             timeout=timeout, event_hooks={"response": [_forget_expired_session]})


def _error_from_response(resp: httpx.Response) -> dict:
    # app.py의 HTTPException은 항상 {"detail": "..."} 형태라 그걸 그대로 옮긴다.
    try:
        detail = resp.json().get("detail", resp.text)
    except (json.JSONDecodeError, ValueError):
        detail = resp.text
    return {"error": True, "status_code": resp.status_code, "detail": detail}


def _error_from_exception(exc: Exception) -> dict:
    # 서버 자체에 못 붙은 경우(연결 거부/타임아웃 등) — status_code가 없다.
    return {"error": True, "status_code": None, "detail": str(exc)}


def _encode_image_name(name: str) -> str:
    # output-images의 name은 "job_id/파일명"처럼 슬래시를 포함할 수 있는데,
    # app.py 라우트가 {filename:path}라 슬래시 자체는 구분자로 남기고 각
    # 구간만 인코딩해야 한다.
    return "/".join(quote(part, safe="") for part in name.split("/"))


mcp = FastMCP("nightshift")


@mcp.tool()
async def comfy_status() -> dict:
    """ComfyUI 백엔드 연결 상태를 확인한다."""
    try:
        async with _client(STATUS_TIMEOUT) as client:
            resp = await client.get("/api/comfy-status")
    except httpx.HTTPError as e:
        return _error_from_exception(e)
    if resp.status_code != 200:
        return _error_from_response(resp)
    return resp.json()


@mcp.tool()
async def list_templates() -> list[dict[str, Any]] | dict[str, Any]:
    """사용 가능한 배치 템플릿과 각 템플릿의 옵션 스키마를 반환한다(원본 그대로).
    옵션 이름/타입은 템플릿마다 다르고 나중에 바뀔 수 있으니, 매번 이 도구로
    새로 조회해서 submit_job의 options를 구성할 것 — 하드코딩하지 말 것."""
    try:
        async with _client(STATUS_TIMEOUT) as client:
            resp = await client.get("/api/templates")
    except httpx.HTTPError as e:
        return _error_from_exception(e)
    if resp.status_code != 200:
        return _error_from_response(resp)
    return resp.json()


@mcp.tool()
async def list_models() -> dict:
    """설치된 체크포인트/LoRA/VAE/ControlNet 모델 이름과 샘플러/스케줄러 목록을
    반환한다. 원본 응답의 node_types(수백 개)는 크기만 하고 쓸 데가 없어 제외한다."""
    try:
        async with _client(STATUS_TIMEOUT) as client:
            resp = await client.get("/api/comfy-object-info")
    except httpx.HTTPError as e:
        return _error_from_exception(e)
    if resp.status_code != 200:
        return _error_from_response(resp)
    data = resp.json()
    return {key: data.get(key) for key in ("connected", "models", "samplers", "schedulers")}


@mcp.tool()
async def build_workflow(
    checkpoint: str,
    positive: str,
    negative: str = "",
    loras: list[dict] | None = None,
    width: int = 832,
    height: int = 1216,
    batch_size: int = 1,
    steps: int = 28,
    cfg: float = 6.5,
    sampler_name: str = "euler_ancestral",
    scheduler: str = "karras",
    hires: dict | None = None,
) -> dict:
    """체크포인트/LoRA/프롬프트/샘플링 파라미터로 ComfyUI 워크플로우 JSON을
    조립한다. 결과의 "workflow" 값을 submit_job의 workflow 인자에 그대로 넘기면
    된다. loras는 [{"name", "strength_model", "strength_clip"}, ...] 형태,
    hires는 {"enabled", "scale_by", "denoise", "steps"} 형태(선택)."""
    spec: dict[str, Any] = {
        "checkpoint": checkpoint,
        "loras": loras or [],
        "positive": positive,
        "negative": negative,
        "width": width,
        "height": height,
        "batch_size": batch_size,
        "steps": steps,
        "cfg": cfg,
        "sampler_name": sampler_name,
        "scheduler": scheduler,
    }
    if hires is not None:
        spec["hires"] = hires
    try:
        async with _client(HEAVY_TIMEOUT) as client:
            resp = await client.post("/api/build-workflow", json=spec)
    except httpx.HTTPError as e:
        return _error_from_exception(e)
    if resp.status_code != 200:
        return _error_from_response(resp)
    return resp.json()


@mcp.tool()
async def submit_job(
    template_id: str,
    workflow: dict,
    options: dict,
    workflow_filename: str = "workflow.json",
    csv: str | None = None,
    project_id: int | None = None,
) -> dict:
    """워크플로우 JSON과 템플릿 옵션으로 잡을 큐에 등록한다(아직 실행 큐에는
    안 들어감 — start_queue를 불러야 실제로 돈다). template_id는 list_templates
    결과의 id 중 하나, options는 그 템플릿의 options 이름들을 key로 쓰는
    key-value(비워두면 각 옵션의 default가 쓰임). requires_csv가 true인
    템플릿이면 csv에 CSV 원문 텍스트를 실어야 한다. project_id는 list_projects의
    id — 주면 그 프로젝트에 속한 잡이 되고(결과물도 그 프로젝트로 모인다), 비우면 미분류."""
    try:
        async with _client(STATUS_TIMEOUT) as client:
            templates_resp = await client.get("/api/templates")
    except httpx.HTTPError as e:
        return _error_from_exception(e)
    if templates_resp.status_code != 200:
        return _error_from_response(templates_resp)

    template = next((t for t in templates_resp.json() if t["id"] == template_id), None)
    if template is None:
        return {"error": True, "status_code": 400, "detail": f"존재하지 않는 템플릿이에요: {template_id}"}

    requires_csv = bool(template.get("requires_csv"))
    if requires_csv and not csv:
        return {"error": True, "status_code": 400, "detail": "이 템플릿은 csv가 필요합니다"}

    tmpdir = tempfile.mkdtemp(prefix="nightshift_mcp_")
    try:
        workflow_path = Path(tmpdir) / workflow_filename
        workflow_path.write_text(json.dumps(workflow), encoding="utf-8")

        form_data = {"template_id": template_id}
        if project_id is not None:
            form_data["project_id"] = str(project_id)
        for option in template.get("options", []):
            value = options.get(option["name"])
            form_data[option["name"]] = "" if value is None else str(value)

        files = {"workflow": (workflow_filename, workflow_path.read_bytes(), "application/json")}
        if requires_csv:
            csv_path = Path(tmpdir) / "data.csv"
            csv_path.write_text(csv, encoding="utf-8")
            files["csv"] = ("data.csv", csv_path.read_bytes(), "text/csv")

        try:
            async with _client(HEAVY_TIMEOUT) as client:
                resp = await client.post("/api/upload", data=form_data, files=files)
        except httpx.HTTPError as e:
            return _error_from_exception(e)
        if resp.status_code != 200:
            return _error_from_response(resp)
        return resp.json()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@mcp.tool()
async def list_projects(include_archived: bool = False) -> dict:
    """프로젝트 목록(작업/결과물 수, 실행 중인 작업 수, 마지막 활동 포함)과 "미분류"
    요약을 반환한다. 프로젝트는 파드와 무관하게 작업과 결과물을 묶는다 —
    submit_job의 project_id에 여기 id를 넣으면 그 프로젝트로 들어간다."""
    try:
        async with _client(STATUS_TIMEOUT) as client:
            resp = await client.get("/api/projects", params={"include_archived": str(include_archived).lower()})
    except httpx.HTTPError as e:
        return _error_from_exception(e)
    if resp.status_code != 200:
        return _error_from_response(resp)
    return resp.json()


@mcp.tool()
async def create_project(name: str, description: str = "") -> dict:
    """새 프로젝트를 만든다. 반환값의 id를 submit_job의 project_id로 쓴다."""
    try:
        async with _client(STATUS_TIMEOUT) as client:
            resp = await client.post("/api/projects", json={"name": name, "description": description})
    except httpx.HTTPError as e:
        return _error_from_exception(e)
    if resp.status_code != 200:
        return _error_from_response(resp)
    return resp.json()


@mcp.tool()
async def list_jobs(include_deleted: bool = False) -> dict:
    """현재 활성 잡 목록(+ 대기 개수, 큐 실행 여부)을 반환한다. include_deleted가
    true면 소프트 삭제된 잡도 jobs 배열에 함께 담는다."""
    try:
        async with _client(STATUS_TIMEOUT) as client:
            resp = await client.get("/api/jobs")
            if resp.status_code == 200 and include_deleted:
                deleted_resp = await client.get("/api/jobs/deleted")
            else:
                deleted_resp = None
    except httpx.HTTPError as e:
        return _error_from_exception(e)
    if resp.status_code != 200:
        return _error_from_response(resp)
    data = resp.json()
    if include_deleted:
        if deleted_resp is None or deleted_resp.status_code != 200:
            return _error_from_response(deleted_resp) if deleted_resp is not None else data
        data["jobs"] = data["jobs"] + deleted_resp.json().get("jobs", [])
    return data


@mcp.tool()
async def get_job(job_id: str) -> dict:
    """특정 잡 하나의 상태를 조회한다(전용 GET 엔드포인트가 없어 list_jobs에서
    걸러낸다 — 삭제된 잡도 찾을 수 있게 include_deleted=true로 조회함)."""
    result = await list_jobs(include_deleted=True)
    if result.get("error"):
        return result
    for job in result.get("jobs", []):
        if job.get("id") == job_id:
            return job
    return {"error": True, "status_code": 404, "detail": f"job_id '{job_id}'를 찾을 수 없어요."}


@mcp.tool()
async def wait_for_job(job_id: str, poll_interval_seconds: float = 3, timeout_seconds: float = 300) -> dict:
    """잡이 done/failed/interrupted 상태가 될 때까지 poll_interval_seconds
    간격으로 폴링하며 기다린다. timeout_seconds를 넘기면 예외 없이, 그때까지
    확인한 마지막 상태(last_job)와 함께 에러를 반환한다."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_seconds
    last_job = None
    while True:
        last_job = await get_job(job_id)
        if last_job.get("error"):
            return last_job
        if last_job.get("status") in TERMINAL_JOB_STATUSES:
            return last_job
        if loop.time() >= deadline:
            return {
                "error": True,
                "status_code": None,
                "detail": f"{timeout_seconds}초 안에 끝나지 않았어요 (마지막 상태: {last_job.get('status')}).",
                "last_job": last_job,
            }
        await asyncio.sleep(poll_interval_seconds)


@mcp.tool()
async def start_queue() -> dict:
    """큐 자동 실행을 켠다 — 대기 중인 잡을 전부 실행 큐에 넣고, 이후 추가되는
    잡도 켜져 있는 동안은 자동으로 이어서 실행된다."""
    try:
        async with _client(STATUS_TIMEOUT) as client:
            resp = await client.post("/api/queue/start")
    except httpx.HTTPError as e:
        return _error_from_exception(e)
    if resp.status_code != 200:
        return _error_from_response(resp)
    return resp.json()


@mcp.tool()
async def stop_queue() -> dict:
    """큐 자동 실행을 끈다. 이미 큐에 들어갔지만 아직 안 돈 잡은 대기 상태로
    남고, 이후 새로 추가되는 잡도 대기 상태로 쌓인다 — 하지만 지금 실행 중인
    잡이 있으면 그 서브프로세스를 즉시 종료 요청해서 status가 interrupted가
    된다(완전히 멈추기까지 몇 초 걸릴 수 있음, get_job으로 확인). interrupted
    잡은 다시 큐에 올릴 수 있다(재시도는 웹 UI 또는 POST /api/jobs/{id}/retry)."""
    try:
        async with _client(STATUS_TIMEOUT) as client:
            resp = await client.post("/api/queue/stop")
    except httpx.HTTPError as e:
        return _error_from_exception(e)
    if resp.status_code != 200:
        return _error_from_response(resp)
    return resp.json()


@mcp.tool()
async def list_output_images(job_id: str | None = None) -> dict:
    """생성된 결과 이미지 목록을 반환한다. job_id를 주면 그 잡이 만든 이미지만
    걸러서 보여준다(서버가 필터를 지원하지 않아 전체를 받아 여기서 거른다)."""
    try:
        async with _client(STATUS_TIMEOUT) as client:
            resp = await client.get("/api/output-images")
    except httpx.HTTPError as e:
        return _error_from_exception(e)
    if resp.status_code != 200:
        return _error_from_response(resp)
    data = resp.json()
    if job_id:
        data["images"] = [img for img in data.get("images", []) if img.get("job_id") == job_id]
    return data


@mcp.tool()
async def get_output_image(name: str, thumbnail: bool = False) -> Image | dict:
    """이미지 하나를 base64로 인코딩된 이미지 콘텐츠로 반환한다(MCP 클라이언트가
    바로 렌더링할 수 있는 형태). name은 list_output_images에서 받은 name 값
    그대로("job_id/파일명.png" 형태 포함) 넘기면 된다. thumbnail=true면
    원본 대신 축소본(긴 변 800px)을 받아온다 — 원본이 5MB를 넘으면 자동으로
    축소본으로 대체한다."""
    encoded_name = _encode_image_name(name)
    path = f"/api/output-images/{encoded_name}"
    if thumbnail:
        path += "/thumbnail?size=800"
    try:
        async with _client(HEAVY_TIMEOUT) as client:
            resp = await client.get(path)
    except httpx.HTTPError as e:
        return _error_from_exception(e)
    if resp.status_code != 200:
        return _error_from_response(resp)

    content = resp.content
    content_type = resp.headers.get("content-type", "image/png")
    if not thumbnail and len(content) > 5 * 1024 * 1024:
        try:
            async with _client(HEAVY_TIMEOUT) as client:
                thumb_resp = await client.get(f"/api/output-images/{encoded_name}/thumbnail?size=800")
        except httpx.HTTPError as e:
            return _error_from_exception(e)
        if thumb_resp.status_code != 200:
            return _error_from_response(thumb_resp)
        content = thumb_resp.content
        content_type = thumb_resp.headers.get("content-type", "image/jpeg")

    fmt = content_type.split("/")[-1] if "/" in content_type else None
    return Image(data=content, format=fmt)


@mcp.tool()
async def clear_completed_jobs() -> dict:
    """완료/실패/중단된 잡을 한꺼번에 소프트 삭제한다(대기/실행 중인 잡은
    그대로 둠)."""
    try:
        async with _client(STATUS_TIMEOUT) as client:
            resp = await client.post("/api/jobs/clear-completed")
    except httpx.HTTPError as e:
        return _error_from_exception(e)
    if resp.status_code != 200:
        return _error_from_response(resp)
    return resp.json()


@mcp.tool()
async def list_recent_workflows() -> Any:
    """최근 업로드했던 워크플로우 목록(최근 30개)을 반환한다 — submit_job마다
    새 워크플로우를 만들지 않고 재사용하고 싶을 때 쓴다."""
    try:
        async with _client(STATUS_TIMEOUT) as client:
            resp = await client.get("/api/recent-workflows")
    except httpx.HTTPError as e:
        return _error_from_exception(e)
    if resp.status_code != 200:
        return _error_from_response(resp)
    return resp.json()


@mcp.tool()
async def list_pods() -> dict:
    """nightshift에 등록된 파드(워커) 목록을 반환한다. 각 항목은 id/name/url/enabled/
    tags/effective_url 등을 담는다. **주의: "파드"는 nightshift가 아는 주소 레코드일
    뿐이고, RunPod pod의 전원 상태와는 별개다** — 파드가 있어도 그 RunPod pod가 꺼져
    있으면 그냥 응답이 없는 것으로 보일 뿐이다(전원은 RunPod 커넥터로 따로 다룬다)."""
    try:
        async with _client(STATUS_TIMEOUT) as client:
            resp = await client.get("/api/pods")
    except httpx.HTTPError as e:
        return _error_from_exception(e)
    if resp.status_code != 200:
        return _error_from_response(resp)
    return resp.json()


@mcp.tool()
async def sync_runpod_pods(dry_run: bool = False) -> dict:
    """RunPod 계정에서 지금 RUNNING이고 ComfyUI 포트가 열린 pod를 찾아 nightshift 파드
    목록에 자동으로 등록/정리한다(멱등 — 이미 등록된 pod는 다시 안 만든다). **RunPod
    커넥터로 pod를 켠 직후 이 도구를 부르면 된다** — 사람이 로그인해서 화면에 주소를
    입력할 필요가 없다. dry_run=true면 아무것도 바꾸지 않고 무엇을 했을지만 보고한다.
    관리자 권한이 필요하다(이 MCP 서버가 로그인한 회원이 관리자가 아니면 403 에러)."""
    try:
        async with _client(HEAVY_TIMEOUT) as client:
            resp = await client.post("/api/pods/sync-runpod", json={"dry_run": dry_run})
    except httpx.HTTPError as e:
        return _error_from_exception(e)
    if resp.status_code != 200:
        return _error_from_response(resp)
    return resp.json()


@mcp.tool()
async def add_pod(url: str, name: str = "", pull_outputs: bool = False) -> dict:
    """파드를 하나 수동으로 등록한다(RunPod가 아닌 주소도 가능 — 다른 클라우드,
    로컬 등). RunPod pod를 자동으로 찾아 등록하려면 sync_runpod_pods를 대신 써라.
    name을 비우면 임의의 이름이 지어진다."""
    try:
        async with _client(STATUS_TIMEOUT) as client:
            resp = await client.post("/api/pods", json={"url": url, "name": name, "pull_outputs": pull_outputs})
    except httpx.HTTPError as e:
        return _error_from_exception(e)
    if resp.status_code != 200:
        return _error_from_response(resp)
    return resp.json()


@mcp.tool()
async def set_pod_enabled(pod_id: str, enabled: bool) -> dict:
    """이 파드로 새 작업을 보낼지(enabled)를 켜고 끈다 — nightshift 쪽 사용 여부일
    뿐이고, **RunPod pod의 전원(과금)과는 무관하다**. RunPod pod를 끄고 싶으면 RunPod
    커넥터를 따로 써야 한다."""
    try:
        async with _client(STATUS_TIMEOUT) as client:
            resp = await client.put(f"/api/pods/{pod_id}", json={"enabled": enabled})
    except httpx.HTTPError as e:
        return _error_from_exception(e)
    if resp.status_code != 200:
        return _error_from_response(resp)
    return resp.json()


@mcp.tool()
async def pod_health(pod_id: str) -> dict:
    """저장된 그대로의 파드 주소가 실제로 응답하는지 확인한다(설정은 안 바꿈).
    RunPod pod를 막 켠 직후에는 프록시가 아직 부팅 중이라 최대 1분 정도
    ok:false가 정상이다 — 잠시 후 다시 확인하면 된다."""
    try:
        async with _client(HEAVY_TIMEOUT) as client:
            resp = await client.post(f"/api/pods/{pod_id}/test")
    except httpx.HTTPError as e:
        return _error_from_exception(e)
    if resp.status_code != 200:
        return _error_from_response(resp)
    return resp.json()


if __name__ == "__main__":
    port = int(os.environ.get("MCP_SERVER_PORT", "8001"))
    if not MCP_AUTH_TOKEN and MCP_SERVER_HOST != "127.0.0.1":
        # 일반 print()는 콘솔 코드페이지(예: Windows cp949)가 이 문구의 특수문자를
        # 표현 못 하면 그 자리에서 UnicodeEncodeError로 서버가 죽는다 — 경고 한 줄
        # 때문에 서버가 안 뜨면 안 되므로 UTF-8 바이트를 stderr에 직접 쓴다.
        warning = (f"[경고] MCP_AUTH_TOKEN 없이 {MCP_SERVER_HOST}:{port}에 묶여요. "
                   f"터널 등으로 노출하면 주소를 아는 누구나 관리자 권한으로 nightshift를 조작할 수 있어요.")
        sys.stderr.buffer.write(warning.encode("utf-8", errors="replace") + b"\n")
    mcp.run(transport="streamable-http", host=MCP_SERVER_HOST, port=port,
            middleware=[Middleware(TokenAuthMiddleware)])
