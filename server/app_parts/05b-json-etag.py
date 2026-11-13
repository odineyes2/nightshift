# GET /api/* JSON 응답에 내용 해시 ETag를 붙여, 바뀐 게 없으면 304(본문 없음)로 답하는 미들웨어
# app.py가 app_parts/*.py를 번호 순서대로 한 네임스페이스에서 실행한다 — 앞 파일의 이름을 import 없이 쓴다.
# 화면은 작업 목록(2초)·갤러리 목록(4초)을 계속 다시 받는데, 내용은 대개 그대로다. Cloudflare를 거치는 길이 느려
# 그 반복 전송이 이미지·영상을 받을 대역폭을 잡아먹었다. 브라우저 fetch는 ETag와 no-cache를 받으면 다음 요청에
# If-None-Match를 스스로 붙이고, 304가 오면 캐시해 둔 본문을 200처럼 돌려주므로 화면 코드는 고칠 게 없다.
# 서버는 응답을 그대로 다 만든 뒤 해시만 비교한다(계산은 줄지 않고 전송만 준다).
JSON_ETAG_MAX_BYTES = 8 * 1024 * 1024   # 이보다 큰 응답은 메모리에 모으지 않고 그대로 보낸다


def _etag_matches(header: str, etag: str) -> bool:
    # Cloudflare가 압축하면서 강한 ETag를 약한 것(W/"…")으로 바꿔 돌려보낼 수 있어 W/는 떼고 비교한다.
    return any(tag.strip().removeprefix("W/") == etag for tag in header.split(","))


@app.middleware("http")
async def json_etag(request: Request, call_next):
    response = await call_next(request)
    if (request.method != "GET" or not request.url.path.startswith("/api/") or response.status_code != 200
            or not response.headers.get("content-type", "").startswith("application/json")
            or "etag" in response.headers):
        return response
    length = response.headers.get("content-length")
    if length and int(length) > JSON_ETAG_MAX_BYTES:
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])
    etag = '"j' + hashlib.sha1(body).hexdigest()[:20] + '"'
    not_modified = _etag_matches(request.headers.get("if-none-match", ""), etag)
    drop = {b"content-length", b"etag", b"cache-control"} | ({b"content-type"} if not_modified else set())
    out = Response(content=b"" if not_modified else body, status_code=304 if not_modified else 200)
    # 원래 헤더를 그대로 옮긴다(Set-Cookie처럼 여러 번 나오는 헤더가 합쳐지지 않게). Response가 새로 만든
    # content-length는 304면 빼고 200이면 새 본문 길이 그대로 쓴다.
    out.raw_headers = ([(k, v) for k, v in response.raw_headers if k.lower() not in drop]
                       + [(k, v) for k, v in out.raw_headers if k == b"content-length" and not not_modified]
                       # private: 회원마다 내용이 다르니 Cloudflare 같은 중간 캐시에는 남기지 않는다. no-cache: 쓰기 전에 매번 확인한다.
                       + [(b"etag", etag.encode()), (b"cache-control", b"private, no-cache")])
    return out
