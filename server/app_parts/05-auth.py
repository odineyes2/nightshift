# app.py가 app_parts/*.py를 번호 순서대로 한 네임스페이스에서 실행한다 — 앞 파일의 이름을 import 없이 쓴다.
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
        response = await call_next(request)
        # 화면 파일은 매번 재검증한다(업데이트 뒤 옛 index.html과 새 JS가 섞이지 않게)
        if path == "/" or path.endswith((".html", ".js", ".css")):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response
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
        raise HTTPException(404, "없는 워커예요.")
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


