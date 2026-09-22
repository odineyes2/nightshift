"""
OpenCut(포크, pm2 "opencut" 앱) 앞을 지키는 로그인 게이트 — nightshift와 같은 회원 시스템을 그대로 쓴다.

## 왜 필요한가

OpenCut은 계정도 DB도 없는 순수 클라이언트 편집기라, 그대로 두면 opencut.lomebrote.com 주소를
아는 아무나 편집기 화면을 열 수 있었다(다만 nightshift 공유 토큰 없이는 가져올 파일이 없다).
그래도 "로그인 화면 없이 그냥 열린다"는 인상 자체가 좋지 않아서, nightshift 계정으로 로그인해야만
들어갈 수 있게 막는다 — 단, OpenCut만의 새 계정을 만들지 않고 nightshift 계정을 그대로 쓴다.

## 어떻게 막는가 — 별도 계정을 만들지 않고 nightshift 세션을 그대로 공유한다

1. nightshift가 로그인 쿠키(ns_session)를 발급할 때 Domain을 NIGHTSHIFT_COOKIE_DOMAIN
   (예: lomebrote.com)으로 찍으면, 같은 브라우저가 nightshift.lomebrote.com과
   opencut.lomebrote.com 양쪽에 그 쿠키를 보낸다 — nightshift에 이미 로그인해 있으면
   OpenCut도 로그인 화면 없이 그대로 열린다(SSO). 갤러리의 "편집기로" 버튼으로 들어오는
   평소 경로는 거의 항상 이 경우다.
2. 로그인이 안 돼 있으면 이 게이트가 로그인 폼을 보여주고, 입력값을 그대로 nightshift의
   진짜 로그인 API(`/api/auth/login`)로 넘긴다. 아이디/비밀번호 검사와 세션 생성은 전부
   nightshift 서버(app.py + auth.py)가 하고, 이 게이트는 그 결과(Set-Cookie)를 브라우저에
   그대로 전달할 뿐이다 — "DB는 app.py 프로세스만 연다"는 원칙(db.py 참고)을 이 게이트도
   지켜서, 세션 확인이든 로그인이든 전부 REST 호출로 처리하고 sqlite를 직접 열지 않는다.
3. 로그인해 있으면(쿠키가 유효하면) 요청을 실제 OpenCut(Next.js, 내부 전용 포트)으로 그대로
   중계한다. 정적 자원(/_next/* 등 해시가 붙은 빌드 산출물)은 사용자별로 다른 내용이 없어서
   검사 없이 바로 흘려보낸다 — 매 요청마다 nightshift에 물어보면 페이지 하나 여는 데도
   수십 번 왕복하게 된다.

## 실행

ecosystem.config.js가 별도 pm2 앱(opencut-gate)으로 띄운다. 실제 OpenCut(Next.js)은 내부 전용
포트(OPENCUT_UPSTREAM_PORT, 기본 3101)의 127.0.0.1로만 묶고, Cloudflare Tunnel이 원래 가리키던
포트(OPENCUT_GATE_PORT, 기본 3100)를 이 게이트가 대신 받는다.
"""

import html
import os
import time
from urllib.parse import quote

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

GATE_PORT = int(os.environ.get("OPENCUT_GATE_PORT", "3100").strip() or "3100")
UPSTREAM_PORT = int(os.environ.get("OPENCUT_UPSTREAM_PORT", "3101").strip() or "3101")
UPSTREAM_HTTP = f"http://127.0.0.1:{UPSTREAM_PORT}"
NIGHTSHIFT_URL = os.environ.get("NIGHTSHIFT_INTERNAL_URL", "http://127.0.0.1:8000").strip().rstrip("/")
SESSION_COOKIE = "ns_session"

_http_client = httpx.AsyncClient(timeout=30.0)

# 최근에 확인한 쿠키 값은 잠깐 기억해 둔다 — 정적 자원이 아닌 요청마다 nightshift에 새로
# 물어보되, 같은 값이면 15초 안에는 다시 묻지 않는다(연속 네비게이션의 왕복을 줄인다).
_AUTH_CACHE_TTL = 15.0
_auth_cache: dict[str, tuple[bool, float]] = {}


def _client_ip(request: Request) -> str:
    return (request.headers.get("cf-connecting-ip")
            or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else ""))


def _safe_next(value: str) -> str:
    """리다이렉트 대상은 이 사이트 안의 경로만 허용한다(오픈 리다이렉트 방지)."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


async def _is_logged_in(cookie_value: str) -> bool:
    if not cookie_value:
        return False
    now = time.monotonic()
    cached = _auth_cache.get(cookie_value)
    if cached and cached[1] > now:
        return cached[0]
    if len(_auth_cache) > 500:
        _auth_cache.clear()
    try:
        resp = await _http_client.get(
            f"{NIGHTSHIFT_URL}/api/auth/me",
            headers={"Cookie": f"{SESSION_COOKIE}={cookie_value}"},
        )
        ok = resp.status_code == 200 and resp.json().get("user") is not None
    except httpx.HTTPError:
        ok = False
    _auth_cache[cookie_value] = (ok, now + _AUTH_CACHE_TTL)
    return ok


_STATIC_PREFIXES = ("/_next/", "/favicon", "/fonts/", "/images/", "/icons/")
_STATIC_EXTS = {
    "js", "css", "map", "svg", "png", "jpg", "jpeg", "gif", "webp", "ico",
    "woff", "woff2", "ttf", "json", "txt", "webmanifest",
}


def _is_static(path: str) -> bool:
    if path.startswith(_STATIC_PREFIXES):
        return True
    tail = path.rsplit("/", 1)[-1]
    return "." in tail and tail.rsplit(".", 1)[-1].lower() in _STATIC_EXTS


LOGIN_PAGE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenCut</title>
<link rel="icon" href="data:,">
<style>
  :root{
    --bg: #F7F8FA; --panel: #FFFFFF; --border: #E3E6EB;
    --text: #1B1F24; --text-dim: #6B7280;
    --accent: #2563EB; --accent-rgb: 37,99,235; --failed: #DC2626;
    --mono: 'IBM Plex Mono', 'SFMono-Regular', Menlo, Consolas, monospace;
    --sans: 'Inter', -apple-system, sans-serif;
  }
  @media (prefers-color-scheme: dark){
    :root{ --bg: #14161B; --panel: #1C1F26; --border: #2C3038; --text: #E7E9ED; --text-dim: #97A0AC; }
  }
  *{ box-sizing:border-box; }
  html, body{ height:100%; margin:0; }
  body{ background:var(--bg); color:var(--text); font-family:var(--sans); }
  .stage{
    position:fixed; inset:0; display:flex; align-items:center; justify-content:center;
    padding:24px;
    background:
      radial-gradient(circle at 50% 42%, rgba(var(--accent-rgb),0.05), transparent 60%),
      radial-gradient(var(--border) 1px, transparent 1px) 0 0/28px 28px,
      var(--bg);
  }
  .content{
    width:100%; max-width:320px; display:flex; flex-direction:column;
    align-items:center; text-align:center; gap:20px;
  }
  @media (prefers-reduced-motion: no-preference){
    .content{ animation: rise .5s cubic-bezier(.16,1,.3,1) both; }
  }
  @keyframes rise{ from{ opacity:0; transform:translateY(6px); } to{ opacity:1; transform:translateY(0); } }
  .mark{ width:20px; height:20px; color:var(--text); }
  .heading{ display:flex; flex-direction:column; gap:8px; }
  .wordmark{ font-family:var(--mono); font-size:12px; font-weight:600; letter-spacing:0.24em; text-transform:uppercase; }
  .tagline{ font-size:13.5px; color:var(--text-dim); line-height:1.6; max-width:30ch; }
  .access-field{ width:100%; display:flex; flex-direction:column; gap:10px; }
  .field-shell{
    width:100%; background:var(--panel); border:1px solid var(--border); border-radius:999px;
    padding:12px 20px; transition: border-color .2s ease, box-shadow .2s ease;
  }
  .field-shell:focus-within{ border-color:var(--accent); box-shadow:0 0 0 4px rgba(var(--accent-rgb),0.14); }
  .field-shell input{
    width:100%; border:none; outline:none; background:none; text-align:center;
    font-family:var(--sans); font-size:14px; letter-spacing:0.01em; color:var(--text);
  }
  .field-shell input::placeholder{ color:var(--text-dim); opacity:0.7; }
  .submit-btn{
    width:100%; padding:11px 20px; border:none; border-radius:999px;
    background:linear-gradient(135deg, #3B7DF5, #2054D6); color:#fff;
    font-family:var(--sans); font-size:14px; font-weight:600; cursor:pointer;
    transition: box-shadow .15s ease, transform .05s ease;
  }
  .submit-btn:hover{ box-shadow:0 4px 14px rgba(var(--accent-rgb),0.35); }
  .submit-btn:active{ transform:translateY(1px); }
  .login-error{ font-size:12.5px; color:var(--failed); min-height:1.2em; }
  .hint-link{ font-size:12.5px; color:var(--text-dim); }
  .hint-link a{ color:inherit; text-decoration:underline; text-underline-offset:2px; }
  .watermark{
    position:absolute; left:0; right:0; bottom:max(22px, env(safe-area-inset-bottom,0px));
    text-align:center; font-family:var(--mono); font-size:10px; letter-spacing:0.22em;
    text-transform:uppercase; color:var(--text-dim); opacity:0.45; pointer-events:none;
  }
</style>
</head>
<body>
<div class="stage">
  <form class="content" method="post" action="/__gate/login">
    <svg class="mark" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a6.8 6.8 0 1 0 10.5 10.5Z" fill="currentColor"/>
    </svg>
    <div class="heading">
      <div class="wordmark">OpenCut</div>
      <p class="tagline">nightshift 계정으로 로그인해 주세요. 같은 아이디·비밀번호를 씁니다.</p>
    </div>
    <div class="access-field">
      <div class="field-shell"><input name="username" type="text" placeholder="아이디" autocomplete="username" autofocus required></div>
      <div class="field-shell"><input name="password" type="password" placeholder="비밀번호" autocomplete="current-password" required></div>
      <input type="hidden" name="next" value="__NEXT__">
      <button class="submit-btn" type="submit">로그인</button>
    </div>
    <div class="login-error">__ERROR__</div>
    <div class="hint-link">계정이 없으면 <a href="https://nightshift.lomebrote.com" target="_blank" rel="noopener">nightshift</a>에서 먼저 가입해 주세요.</div>
  </form>
  <div class="watermark">nightshift account required</div>
</div>
</body>
</html>"""


def _login_html(error: str, next_path: str) -> str:
    return (LOGIN_PAGE
            .replace("__ERROR__", html.escape(error))
            .replace("__NEXT__", html.escape(next_path)))


async def login_form(request: Request):
    nxt = _safe_next(request.query_params.get("next", ""))
    if await _is_logged_in(request.cookies.get(SESSION_COOKIE, "")):
        return RedirectResponse(nxt, status_code=303)
    error = "아이디 또는 비밀번호가 올바르지 않아요." if request.query_params.get("error") == "1" else ""
    return HTMLResponse(_login_html(error, nxt))


async def login_submit(request: Request):
    form = await request.form()
    username = str(form.get("username") or "").strip()
    password = str(form.get("password") or "")
    nxt = _safe_next(str(form.get("next") or ""))

    try:
        resp = await _http_client.post(
            f"{NIGHTSHIFT_URL}/api/auth/login",
            json={"username": username, "password": password},
            headers={
                "X-Requested-With": "nightshift",
                "Content-Type": "application/json",
                "X-Forwarded-For": _client_ip(request),
            },
        )
    except httpx.HTTPError:
        return HTMLResponse(_login_html("nightshift 서버에 연결하지 못했어요.", nxt), status_code=502)

    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail") or "로그인에 실패했어요."
        except ValueError:
            detail = "로그인에 실패했어요."
        return HTMLResponse(_login_html(detail, nxt), status_code=401)

    out = RedirectResponse(nxt, status_code=303)
    for k, v in resp.headers.multi_items():
        if k.lower() == "set-cookie":
            out.headers.append("set-cookie", v)
    return out


_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
}


async def proxy_http(request: Request):
    path = "/" + request.path_params.get("path", "")
    if not _is_static(path):
        if not await _is_logged_in(request.cookies.get(SESSION_COOKIE, "")):
            if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                full_path = path + (f"?{request.url.query}" if request.url.query else "")
                return RedirectResponse(f"/__gate/login?next={quote(full_path, safe='')}", status_code=303)
            return Response(status_code=401)

    url = f"{UPSTREAM_HTTP}{path}"
    headers = [
        (k, v) for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() != "host"
    ]

    upstream_req = _http_client.build_request(
        request.method, url,
        params=request.url.query.encode(),
        headers=headers,
        content=request.stream(),
    )
    upstream_resp = await _http_client.send(upstream_req, stream=True)

    async def body():
        async for chunk in upstream_resp.aiter_raw():
            yield chunk
        await upstream_resp.aclose()

    resp = StreamingResponse(body(), status_code=upstream_resp.status_code)
    for k, v in upstream_resp.headers.multi_items():
        if k.lower() not in _HOP_BY_HOP:
            resp.headers.append(k, v)
    return resp


routes = [
    Route("/__gate/login", login_form, methods=["GET"]),
    Route("/__gate/login", login_submit, methods=["POST"]),
    Route("/{path:path}", proxy_http, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]),
]

app = Starlette(routes=routes)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=GATE_PORT)
