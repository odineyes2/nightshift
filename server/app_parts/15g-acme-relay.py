# Let's Encrypt HTTP-01 확인 요청(/.well-known/acme-challenge/)을 이 PC의 Caddy(tailnet.Caddyfile, 127.0.0.1:8081)로 넘긴다
# app.py가 app_parts/*.py를 번호 순서대로 한 네임스페이스에서 실행한다 — 앞 파일의 이름을 import 없이 쓴다.
# Caddy는 Tailscale 주소에서만 nightshift.lomebrote.com을 받으므로, Let's Encrypt가 공개 주소로 보내는 확인 요청은
# Cloudflare 터널을 거쳐 여기(nightshift)로 온다. 토큰 모양만 확인하고 그대로 Caddy에 물어 답을 돌려준다(로그인 없음 —
# 확인 값은 원래 공개되는 값이다). Caddy가 없거나 모르는 토큰이면 404.
ACME_RELAY_URL = os.environ.get("NIGHTSHIFT_ACME_RELAY_URL", "http://127.0.0.1:8081").rstrip("/")
_ACME_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


@app.get("/.well-known/acme-challenge/{token}", include_in_schema=False)
def acme_challenge_relay(token: str, request: Request):
    if not _ACME_TOKEN_RE.match(token):
        raise HTTPException(404)
    req = urllib.request.Request(f"{ACME_RELAY_URL}/.well-known/acme-challenge/{token}",
                                 headers={"Host": request.headers.get("host", "")})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read(4096)
    except (urllib.error.URLError, OSError):
        raise HTTPException(404)
    return Response(body, media_type="text/plain")
