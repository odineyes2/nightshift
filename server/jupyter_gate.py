"""
주피터(jupyterlab pm2 앱) 앞을 지키는 로그인 게이트 — nightshift 관리자 계정으로만 들어올 수 있다.

## 왜 필요한가

주피터는 노트북에서 임의 코드를 실행할 수 있는, 이 홈서버에서 가장 강한 권한을
가진 화면이다. 그런데 ecosystem.config.js가 `--ip=0.0.0.0`으로 띄우고 있어서,
Cloudflare Tunnel(jupyter.lomebrote.com → localhost:8888)을 그대로 통과해 로그인
화면 없이 노출돼 있었다.

## 어떻게 막는가 — 별도 계정 없이 nightshift **관리자** 세션만 통과시킨다

1. 주피터 자체는 내부 전용 포트(JUPYTER_UPSTREAM_PORT, 기본 18888)의 127.0.0.1로만
   묶는다 — 이 게이트를 거치지 않고는 이 머신 밖에서 절대 닿을 수 없다.
2. 터널이 원래 가리키던 8888번(JUPYTER_GATE_PORT)을 이 게이트가 대신 받는다.
3. nightshift가 로그인 쿠키(ns_session)를 발급할 때 Domain을 NIGHTSHIFT_COOKIE_DOMAIN
   (예: lomebrote.com)으로 찍어 두면 같은 브라우저가 jupyter.lomebrote.com에도 그 쿠키를
   보낸다. 이 게이트는 그 쿠키로 nightshift의 `/api/auth/me`에 물어 로그인 여부와
   **role이 admin인지**까지 확인한다 — 일반 회원(가입 승인만 받은 계정)은 이 게이트가
   막는다. 아예 로그인이 안 돼 있으면 로그인 폼을 보여주고, 입력값은 nightshift의 진짜
   `/api/auth/login`으로 그대로 넘긴다(회원가입 폼은 없다 — 주피터 접근 여부는 계정을
   admin으로 만드느냐로만 정해지고, 그건 가입 화면이 아니라 서버 쪽에서 정하는 일이다).
   "DB는 app.py 프로세스만 연다"는 기존 원칙대로, 이 게이트도 세션 확인·로그인 모두
   REST 호출로만 처리하고 sqlite를 직접 열지 않는다.
4. 이 게이트를 통과한 요청만 주피터로 그대로 흘려보낸다 — HTTP는 물론, 커널/터미널이
   쓰는 WebSocket도 양방향으로 중계한다(주피터는 이 두 프로토콜 없이는 거의 아무것도
   못 하므로 프록시가 반드시 둘 다 지원해야 한다).
5. 주피터 자신의 토큰 인증(JUPYTER_TOKEN)은 여전히 켜져 있다 — 이 게이트가 모든
   업스트림 요청에 그 토큰을 자동으로 실어 보내므로, 사람은 절대 그 값을 보거나
   입력할 일이 없다. 게이트를 건너뛰고 내부 포트에 바로 붙는 경로가 있더라도
   (이 머신에 이미 들어와 있는 사람 등) 주피터 쪽 인증이 한 번 더 남는 이중 방어.

## 실행

ecosystem.config.js가 별도 pm2 앱(jupyter-gate)으로 띄운다. 직접 띄우려면:
    python3 jupyter_gate.py
"""

import asyncio
import html
import os
import time
from urllib.parse import quote

import httpx
import uvicorn
import websockets
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

INTERNAL_TOKEN = os.environ.get("JUPYTER_INTERNAL_TOKEN", "").strip()
GATE_PORT = int(os.environ.get("JUPYTER_GATE_PORT", "8888").strip() or "8888")
UPSTREAM_PORT = int(os.environ.get("JUPYTER_UPSTREAM_PORT", "18888").strip() or "18888")
UPSTREAM_HTTP = f"http://127.0.0.1:{UPSTREAM_PORT}"
UPSTREAM_WS = f"ws://127.0.0.1:{UPSTREAM_PORT}"
NIGHTSHIFT_URL = os.environ.get("NIGHTSHIFT_INTERNAL_URL", "http://127.0.0.1:8000").strip().rstrip("/")
SESSION_COOKIE = "ns_session"

if not INTERNAL_TOKEN:
    raise RuntimeError(
        "JUPYTER_INTERNAL_TOKEN이 비어 있다 — 이 게이트와 내부 주피터 사이에서만 "
        "쓰는 값이라 사람이 볼 일은 없지만, 없으면 주피터 자체 토큰 인증을 통과할 "
        "방법이 없다. .env에 설정하고 ecosystem.config.js가 같은 값을 주피터의 "
        "JUPYTER_TOKEN 환경변수로 넘기게 하세요."
    )

_http_client = httpx.AsyncClient(timeout=30.0)

# nightshift에게 물어본 결과를 잠깐 기억해 둔다 — 같은 쿠키로 15초 안에 다시 오면 또 묻지 않는다.
_AUTH_CACHE_TTL = 15.0
_auth_cache: dict[str, tuple[str, float]] = {}  # cookie -> ("anon"|"user"|"admin", 만료 시각)


def _client_ip(request: Request) -> str:
    return (request.headers.get("cf-connecting-ip")
            or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else ""))


def _safe_next(value: str) -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


async def _auth_status(cookie_value: str) -> str:
    """이 요청의 쿠키가 "anon"(비로그인) / "user"(로그인했지만 관리자 아님) / "admin" 중 뭔지."""
    if not cookie_value:
        return "anon"
    now = time.monotonic()
    cached = _auth_cache.get(cookie_value)
    if cached and cached[1] > now:
        return cached[0]
    if len(_auth_cache) > 500:
        _auth_cache.clear()
    status = "anon"
    try:
        resp = await _http_client.get(
            f"{NIGHTSHIFT_URL}/api/auth/me",
            headers={"Cookie": f"{SESSION_COOKIE}={cookie_value}"},
        )
        user = resp.json().get("user") if resp.status_code == 200 else None
        if user:
            status = "admin" if user.get("role") == "admin" else "user"
    except httpx.HTTPError:
        status = "anon"
    _auth_cache[cookie_value] = (status, now + _AUTH_CACHE_TTL)
    return status


async def _request_status(request: Request) -> str:
    return await _auth_status(request.cookies.get(SESSION_COOKIE, ""))


async def _ws_status(websocket: WebSocket) -> str:
    return await _auth_status(websocket.cookies.get(SESSION_COOKIE, ""))


PAGE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Jupyter</title>
<link rel="icon" href="data:,">
<style>
  :root{
    --bg: #F7F8FA; --panel: #FFFFFF; --border: #E3E6EB;
    --text: #1B1F24; --text-dim: #6B7280;
    --accent: #2563EB; --accent-rgb: 37,99,235; --failed: #DC2626;
    --sans: 'Inter', -apple-system, sans-serif;
    --mono: 'IBM Plex Mono', 'SFMono-Regular', Menlo, Consolas, monospace;
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
  .watermark{
    position:absolute; left:0; right:0; bottom:max(22px, env(safe-area-inset-bottom,0px));
    text-align:center; font-family:var(--mono); font-size:10px; letter-spacing:0.22em;
    text-transform:uppercase; color:var(--text-dim); opacity:0.45; pointer-events:none;
  }
</style>
</head>
<body>
<div class="stage">
  __BODY__
  <div class="watermark">Private &middot; Admin Only</div>
</div>
</body>
</html>"""

LOGIN_FORM_BODY = """<form class="content" method="post" action="/__gate/login">
    <svg class="mark" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a6.8 6.8 0 1 0 10.5 10.5Z" fill="currentColor"/>
    </svg>
    <div class="heading">
      <div class="wordmark">Jupyter</div>
      <p class="tagline">관리자 전용 구역이에요. nightshift 관리자 계정으로 로그인하세요.</p>
    </div>
    <div class="access-field">
      <div class="field-shell"><input name="username" type="text" placeholder="아이디" autocomplete="username" autofocus required></div>
      <div class="field-shell"><input name="password" type="password" placeholder="비밀번호" autocomplete="current-password" required></div>
      <input type="hidden" name="next" value="__NEXT__">
      <button class="submit-btn" type="submit">로그인</button>
    </div>
    <div class="login-error">__ERROR__</div>
  </form>"""

NOT_ADMIN_BODY = """<div class="content">
    <svg class="mark" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a6.8 6.8 0 1 0 10.5 10.5Z" fill="currentColor"/>
    </svg>
    <div class="heading">
      <div class="wordmark">Jupyter</div>
      <p class="tagline">이 nightshift 계정은 관리자가 아니라서 들어올 수 없어요.</p>
    </div>
    <div class="login-error">__ERROR__</div>
  </div>"""


def _login_html(error: str, next_path: str) -> str:
    body = LOGIN_FORM_BODY.replace("__ERROR__", html.escape(error)).replace("__NEXT__", html.escape(next_path))
    return PAGE.replace("__BODY__", body)


def _not_admin_html() -> str:
    return PAGE.replace("__BODY__", NOT_ADMIN_BODY.replace("__ERROR__", ""))


async def login_form(request: Request):
    nxt = _safe_next(request.query_params.get("next", ""))
    status = await _request_status(request)
    if status == "admin":
        return RedirectResponse(nxt, status_code=303)
    if status == "user":
        return HTMLResponse(_not_admin_html())
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

    user = resp.json().get("user") or {}
    set_cookie_headers = [v for k, v in resp.headers.multi_items() if k.lower() == "set-cookie"]

    if user.get("role") != "admin":
        # 로그인 자체는 됐으니(nightshift 쪽 세션은 정상 생성) 쿠키는 그대로 심어 주되,
        # 여기서는 들여보내지 않는다 — 다음 방문 때도 이 쿠키로 곧장 "관리자 아님" 안내가 뜬다.
        out = HTMLResponse(_not_admin_html(), status_code=403)
        for v in set_cookie_headers:
            out.headers.append("set-cookie", v)
        return out

    out = RedirectResponse(nxt, status_code=303)
    for v in set_cookie_headers:
        out.headers.append("set-cookie", v)
    return out


_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
}


async def proxy_http(request: Request):
    status = await _request_status(request)
    if status != "admin":
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            path = "/" + request.path_params.get("path", "")
            full_path = path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/__gate/login?next={quote(full_path, safe='')}", status_code=303)
        return Response(status_code=401)

    path = "/" + request.path_params.get("path", "")
    url = f"{UPSTREAM_HTTP}{path}"
    headers = [
        (k, v) for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() != "host"
    ]
    headers.append(("Authorization", f"token {INTERNAL_TOKEN}"))

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

    # dict(...)로 접거나 .items()를 쓰면 안 된다 — 주피터는 Set-Cookie를 한 응답에
    # 두 개(세션용 username-... 쿠키, XSRF 검사용 _xsrf 쿠키) 실어 보내는데, dict은
    # 같은 키를 하나로 뭉개고, httpx의 .items()조차 편의상 중복 헤더를 콤마로 이어
    # 붙인 "하나의" Set-Cookie로 돌려준다(Set-Cookie는 콤마로 합치면 안 되는 유일한
    # 예외 헤더라 RFC 위반). 그 결과 브라우저가 쿠키 한 줄을 통째로 못 알아듣고
    # 버려서, 모든 POST/PUT이 "_xsrf argument missing" 403으로 죽었다.
    # multi_items()가 중복 헤더를 쪼개진 상태 그대로 돌려주는 httpx 쪽 우회 API다.
    resp = StreamingResponse(body(), status_code=upstream_resp.status_code)
    for k, v in upstream_resp.headers.multi_items():
        if k.lower() not in _HOP_BY_HOP:
            resp.headers.append(k, v)
    return resp


async def proxy_ws(websocket: WebSocket):
    if await _ws_status(websocket) != "admin":
        await websocket.close(code=4401)
        return

    # 주피터의 커널 채널은 서브프로토콜에 따라 메시지를 완전히 다르게 담는다 —
    # "v1.kernel.websocket.jupyter.org"가 뽑히면 클라이언트 JS가 모든 메시지를
    # (JSON 하나뿐이어도) 바이너리 봉투에 담아 보낸다. 클라이언트에게는 이 게이트가
    # 뽑아준 서브프로토콜을, 업스트림에는 우리가 부른 것을 각자 다르게 협상해버리면
    # 한쪽은 바이너리 봉투를 기대하고 다른 쪽은 안 쓰는 상태가 돼서 프레임이
    # 깨진다(실제로 이렇게 짰다가 jupyter_server가 "struct.error: unpack requires
    # a buffer of ..." 로 즉시 죽는 걸 봤다). 그래서 클라이언트가 제안한 후보
    # 목록을 업스트림에도 그대로 제안하고, 업스트림이 실제로 고른 것을 그대로
    # 클라이언트에게 돌려준다 — 양쪽이 항상 같은 프로토콜로 맞춰지게.
    offered = [p.strip() for p in (websocket.headers.get("sec-websocket-protocol") or "").split(",") if p.strip()]

    path = "/" + websocket.path_params.get("path", "")
    url = f"{UPSTREAM_WS}{path}"
    if websocket.url.query:
        url += f"?{websocket.url.query}"

    try:
        async with websockets.connect(
            url, max_size=None,
            subprotocols=offered or None,
            additional_headers={"Authorization": f"token {INTERNAL_TOKEN}"},
        ) as upstream:
            await websocket.accept(subprotocol=upstream.subprotocol)

            async def client_to_upstream():
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.disconnect":
                            break
                        if msg.get("text") is not None:
                            await upstream.send(msg["text"])
                        elif msg.get("bytes") is not None:
                            await upstream.send(msg["bytes"])
                except WebSocketDisconnect:
                    pass

            async def upstream_to_client():
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            pumps = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
            _, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
    except Exception:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


routes = [
    Route("/__gate/login", login_form, methods=["GET"]),
    Route("/__gate/login", login_submit, methods=["POST"]),
    WebSocketRoute("/{path:path}", proxy_ws),
    Route("/{path:path}", proxy_http, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]),
]

app = Starlette(routes=routes)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=GATE_PORT)
