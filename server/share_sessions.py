"""
외부 편집기(OpenCut)와 자원을 주고받는 "공유 세션".

nightshift 갤러리에서 고른 이미지/영상을 다른 서브도메인의 편집기가 브라우저에서 가져가고(GET), 편집 결과를 다시 올려
보낼 수 있게(POST) 하는 임시 열쇠다. 편집기는 nightshift에 로그인하지 않으므로 — 쿠키를 다른 사이트에 열어 주는 대신 —
세션 토큰 하나가 "이 파일들을 읽어도 되고, 결과를 몇 번까지 올려도 된다"는 것만 허락한다.

- 토큰은 예측할 수 없는 난수이고 URL로 전달된다. 세션은 메모리에만 있고(서버가 재시작되면 사라진다) 기본 2시간 뒤 만료된다.
- 파일은 세션을 만들 때 그 회원이 볼 수 있는 것으로 확인된 경로만 담는다(다른 경로는 못 가져간다).
- 업로드는 세션을 만든 회원의 편집 폴더에 새 파일로만 저장되고 횟수와 크기에 한도가 있다.
"""

import secrets
import threading
import time

SESSION_TTL_SEC = 2 * 60 * 60
MAX_FILES = 30
MAX_UPLOADS = 10

_sessions: dict[str, dict] = {}
_lock = threading.Lock()


def create(owner_id: int, files: list[dict], project_id) -> str:
    """files: [{"rel": 출력 폴더 기준 경로, "kind": "image"|"video", "name": 표시 이름}]"""
    token = secrets.token_urlsafe(24)
    now = time.time()
    with _lock:
        _purge(now)
        _sessions[token] = {"owner_id": owner_id, "files": files, "project_id": project_id,
                            "expires": now + SESSION_TTL_SEC, "uploads": 0, "created": now}
    return token


def get(token: str) -> dict | None:
    now = time.time()
    with _lock:
        _purge(now)
        s = _sessions.get(token)
        return dict(s) if s else None


def count_upload(token: str) -> bool:
    """업로드 한 번을 세고, 한도를 넘었으면 False."""
    with _lock:
        s = _sessions.get(token)
        if s is None or s["uploads"] >= MAX_UPLOADS:
            return False
        s["uploads"] += 1
        return True


def _purge(now: float) -> None:
    for t in [t for t, s in _sessions.items() if s["expires"] < now]:
        del _sessions[t]
