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
    JOB_QUEUE_API_KEY   app.py에 NIGHTSHIFT_API_KEY를 설정해뒀다면 같은 값
                        (설정 안 했으면 비워둠 — 그러면 헤더 자체를 안 보냄)
    MCP_SERVER_PORT     이 MCP 서버가 뜰 포트 (기본 8001, app.py의 8000과 겹치면 안 됨)
"""

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastmcp import FastMCP
from fastmcp.utilities.types import Image

BASE_URL = os.environ.get("JOB_QUEUE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
API_KEY = os.environ.get("JOB_QUEUE_API_KEY", "").strip()

# 상태/목록 조회류는 가볍게 10초, 워크플로우 빌드나 잡 등록처럼 서버 쪽에서
# 검증 작업(ComfyUI 조회 등)이 걸릴 수 있는 호출은 30초로 넉넉하게 잡는다.
STATUS_TIMEOUT = 10.0
HEAVY_TIMEOUT = 30.0

TERMINAL_JOB_STATUSES = {"done", "failed", "interrupted"}


def _headers() -> dict:
    return {"X-API-Key": API_KEY} if API_KEY else {}


def _client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=BASE_URL, headers=_headers(), timeout=timeout)


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
) -> dict:
    """워크플로우 JSON과 템플릿 옵션으로 잡을 큐에 등록한다(아직 실행 큐에는
    안 들어감 — start_queue를 불러야 실제로 돈다). template_id는 list_templates
    결과의 id 중 하나, options는 그 템플릿의 options 이름들을 key로 쓰는
    key-value(비워두면 각 옵션의 default가 쓰임). requires_csv가 true인
    템플릿이면 csv에 CSV 원문 텍스트를 실어야 한다."""
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


if __name__ == "__main__":
    port = int(os.environ.get("MCP_SERVER_PORT", "8001"))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
