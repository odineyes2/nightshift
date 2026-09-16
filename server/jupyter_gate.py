"""
주피터(jupyterlab pm2 앱) 앞을 지키는 작은 로그인 게이트.

## 왜 필요한가

주피터는 노트북에서 임의 코드를 실행할 수 있는, 이 홈서버에서 가장 강한 권한을
가진 화면이다. 그런데 ecosystem.config.js가 `--ip=0.0.0.0`으로 띄우고 있어서,
Cloudflare Tunnel(jupyter.lomebrote.com → localhost:8888)을 그대로 통과해 로그인
화면 없이 노출돼 있었다. nightshift 웹 화면(static/index.html)에는 이미 미니멀
센터드 로그인 게이트가 있는데, 주피터 쪽에는 그런 게 전혀 없었던 것.

## 어떻게 막는가

1. 주피터 자체는 내부 전용 포트(JUPYTER_UPSTREAM_PORT, 기본 18888)의 127.0.0.1로만
   묶는다 — 이 게이트를 거치지 않고는 이 머신 밖에서 절대 닿을 수 없다.
2. 터널이 원래 가리키던 8888번(JUPYTER_GATE_PORT)을 이 게이트가 대신 받는다.
3. 이 게이트가 발급한 세션 쿠키가 있는 요청만 주피터로 그대로 흘려보낸다 — HTTP는
   물론, 커널/터미널이 쓰는 WebSocket도 양방향으로 중계한다(주피터는 이 두 프로토콜
   없이는 거의 아무것도 못 하므로 프록시가 반드시 둘 다 지원해야 한다).
4. 주피터 자신의 토큰 인증(JUPYTER_TOKEN)은 여전히 켜져 있다 — 이 게이트가 모든
   업스트림 요청에 그 토큰을 자동으로 실어 보내므로, 사람은 절대 그 값을 보거나
   입력할 일이 없다. 게이트를 건너뛰고 내부 포트에 바로 붙는 경로가 있더라도
   (이 머신에 이미 들어와 있는 사람 등) 주피터 쪽 인증이 한 번 더 남는 이중 방어.

로그인 화면이 없으면 nightshift 웹 화면과 같은 스타일(미니멀 센터드)의 페이지를
보여주고, 여기서 JUPYTER_GATE_KEY를 입력해야 들어갈 수 있다.

## 실행

ecosystem.config.js가 별도 pm2 앱(jupyter-gate)으로 띄운다. 직접 띄우려면:
    python3 jupyter_gate.py
"""

import asyncio
import hashlib
import hmac
import os

import httpx
import uvicorn
import websockets
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

GATE_KEY = os.environ.get("JUPYTER_GATE_KEY", "").strip()
INTERNAL_TOKEN = os.environ.get("JUPYTER_INTERNAL_TOKEN", "").strip()
GATE_PORT = int(os.environ.get("JUPYTER_GATE_PORT", "8888").strip() or "8888")
UPSTREAM_PORT = int(os.environ.get("JUPYTER_UPSTREAM_PORT", "18888").strip() or "18888")
UPSTREAM_HTTP = f"http://127.0.0.1:{UPSTREAM_PORT}"
UPSTREAM_WS = f"ws://127.0.0.1:{UPSTREAM_PORT}"

COOKIE_NAME = "jupyter_gate"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30일 — 매번 다시 로그인하지 않도록

if not GATE_KEY:
    raise RuntimeError(
        "JUPYTER_GATE_KEY가 비어 있다 — 이 값이 없으면 게이트가 아무나 통과시키는 "
        "꼴이라 아예 시작을 막는다. .env에 설정하세요."
    )
if not INTERNAL_TOKEN:
    raise RuntimeError(
        "JUPYTER_INTERNAL_TOKEN이 비어 있다 — 이 게이트와 내부 주피터 사이에서만 "
        "쓰는 값이라 사람이 볼 일은 없지만, 없으면 주피터 자체 토큰 인증을 통과할 "
        "방법이 없다. .env에 설정하고 ecosystem.config.js가 같은 값을 주피터의 "
        "JUPYTER_TOKEN 환경변수로 넘기게 하세요."
    )


def _session_token() -> str:
    """키로부터 결정론적으로 세션 값을 만든다. 서버가 세션을 따로 저장하지 않아도
    키가 같으면 항상 같은 쿠키 값이 나오고, 키를 바꾸면 기존에 발급된 쿠키가 전부
    자동으로 무효화된다 — nightshift API 키와 같은 무상태 방식."""
    return hmac.new(GATE_KEY.encode(), b"jupyter-gate-session-v1", hashlib.sha256).hexdigest()


def _is_authed(request: Request) -> bool:
    cookie = request.cookies.get(COOKIE_NAME, "")
    return bool(cookie) and hmac.compare_digest(cookie, _session_token())


LOGIN_PAGE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Jupyter</title>
<link rel="icon" href="data:,">
<style>
  /* nightshift 웹 화면(static/index.html)의 미니멀 센터드 로그인 게이트와 같은
     디자인 — 토큰만 이 파일 안에 그대로 옮겨왔다(별도 CSS 파일을 두지 않음). */
  :root{
    --bg: #F7F8FA; --panel: #FFFFFF; --border: #E3E6EB;
    --text: #1B1F24; --text-dim: #6B7280;
    --queued: #2563EB; --queued-rgb: 37,99,235; --failed: #DC2626;
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
      radial-gradient(circle at 50% 42%, rgba(var(--queued-rgb),0.05), transparent 60%),
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
  .field-shell:focus-within{ border-color:var(--queued); box-shadow:0 0 0 4px rgba(var(--queued-rgb),0.14); }
  #key-input{
    width:100%; border:none; outline:none; background:none; text-align:center;
    font-family:var(--mono); font-size:14px; letter-spacing:0.03em; color:var(--text);
  }
  #key-input::placeholder{ color:var(--text-dim); opacity:0.7; }
  .enter-hint{ display:flex; align-items:center; justify-content:center; gap:6px; font-size:11px; color:var(--text-dim); opacity:0.6; }
  .enter-hint kbd{
    font-family:var(--mono); font-size:10px; color:var(--text-dim); background:var(--bg);
    border:1px solid var(--border); border-radius:4px; padding:1px 5px;
  }
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
  <form class="content" method="post" action="/__gate/login">
    <svg class="mark" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a6.8 6.8 0 1 0 10.5 10.5Z" fill="currentColor"/>
    </svg>
    <div class="heading">
      <div class="wordmark">Jupyter</div>
      <p class="tagline">관리자 전용 구역이에요. 게이트 키를 입력하세요.</p>
    </div>
    <div class="access-field">
      <div class="field-shell">
        <input id="key-input" name="key" type="password" placeholder="게이트 키" autocomplete="off" spellcheck="false" autofocus aria-label="게이트 키">
      </div>
      <div class="enter-hint"><kbd>Enter</kbd> 를 눌러 계속</div>
    </div>
    <div class="login-error">__ERROR__</div>
  </form>
  <div class="watermark">Private &middot; Admin Only</div>
</div>
<script>
  // 버튼 없이 Enter로만 제출하는 입력창이라, 브라우저의 암묵적 제출에만 기대지
  // 않고 명시적으로도 눌러준다(nightshift 웹 화면의 로그인 게이트와 동일한 처리).
  document.getElementById('key-input').addEventListener('keydown', function(e){
    if(e.key === 'Enter'){
      e.preventDefault();
      e.target.form.requestSubmit();
    }
  });
</script>
</body>
</html>"""


def _login_html(error: bool) -> str:
    return LOGIN_PAGE.replace("__ERROR__", "키가 올바르지 않아요." if error else "")


async def login_form(request: Request):
    if _is_authed(request):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(_login_html(error=request.query_params.get("error") == "1"))


async def login_submit(request: Request):
    form = await request.form()
    key = str(form.get("key") or "").strip()
    if not key or not hmac.compare_digest(key, GATE_KEY):
        return RedirectResponse("/__gate/login?error=1", status_code=303)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        COOKIE_NAME, _session_token(),
        max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax", secure=True,
    )
    return resp


# 프록시 단계에서 그대로 넘기면 안 되는 헤더들 — hop-by-hop이거나(RFC 7230),
# 업스트림 응답의 실제 바디 길이/인코딩과 어긋나면 클라이언트가 응답을 자르거나
# 못 읽게 되는 것들(content-length는 StreamingResponse가 다시 계산해서 붙인다).
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
}

_http_client = httpx.AsyncClient(timeout=None)


async def proxy_http(request: Request):
    if not _is_authed(request):
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            return RedirectResponse("/__gate/login", status_code=303)
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
    cookie = websocket.cookies.get(COOKIE_NAME, "")
    if not cookie or not hmac.compare_digest(cookie, _session_token()):
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
