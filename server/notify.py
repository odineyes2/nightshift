"""관리자 전역 ntfy 설정과 잡 종료 알림. 전송 실패는 잡 실행에 영향을 주지 않는다."""
import json
import logging
import os
import re
import threading
import urllib.request
from datetime import datetime
from email.header import Header
from urllib.parse import urlsplit

import db

log = logging.getLogger(__name__)
_settings_lock = threading.Lock()
_counts = {"done": 0, "failed": 0}


def _read(conn):
    settings = {"enabled": False, "mode": "all", "server": "https://ntfy.sh",
                "topic": os.environ.get("NTFY_TOPIC", ""), "token": ""}
    raw = db.get_meta(conn, "notify_settings")
    if raw:
        settings.update(json.loads(raw))
    return settings


def get_settings():
    with db.connect() as conn:
        return _read(conn)


def public_settings(settings=None):
    settings = dict(settings if settings is not None else get_settings())
    settings["token_set"] = bool(settings.pop("token", ""))
    return settings


def save_settings(body):
    with _settings_lock, db.connect() as conn:
        settings = _read(conn)
        for key in ("enabled", "mode", "server", "topic"):
            if key in body:
                settings[key] = body[key]
        if type(settings["enabled"]) is not bool:
            raise ValueError("enabled는 참 또는 거짓이어야 해요.")
        if settings["mode"] not in ("each", "all"):
            raise ValueError("알림 방식은 each 또는 all이어야 해요.")
        topic = settings["topic"]
        if not isinstance(topic, str) or (topic and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", topic)):
            raise ValueError("토픽은 영문·숫자·밑줄·하이픈 1~64자로 입력해 주세요.")
        if settings["enabled"] and not topic:
            raise ValueError("알림을 켜려면 토픽을 입력해 주세요.")
        server = settings["server"]
        try:
            parsed = urlsplit(server) if isinstance(server, str) else None
            valid = (parsed and parsed.scheme in ("http", "https") and parsed.hostname
                     and not parsed.username and not parsed.password and not parsed.query
                     and not parsed.fragment and not re.search(r"[\s\x00-\x1f\x7f]", server))
            if parsed:
                parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("서버 주소는 http 또는 https URL이어야 해요.")
        settings["server"] = server.rstrip("/")
        token = body.get("token", "")
        if not isinstance(token, str) or re.search(r"[\x00-\x1f\x7f]", token):
            raise ValueError("올바른 토큰을 입력해 주세요.")
        if type(body.get("clear_token", False)) is not bool:
            raise ValueError("clear_token은 참 또는 거짓이어야 해요.")
        if body.get("clear_token"):
            settings["token"] = ""
        elif token:
            settings["token"] = token
        db.set_meta(conn, "notify_settings", json.dumps(settings, ensure_ascii=False))
    return public_settings(settings)


def send(settings, title, message):
    """동기 전송(시험 API용). 예외 원문에는 비밀값이 섞일 수 있어 종류만 반환한다."""
    if not settings.get("topic"):
        return {"ok": False, "error": "토픽을 입력해 주세요."}
    for attempt in range(2):
        try:
            headers = {"Title": Header(title, "utf-8").encode(), "Priority": "default",
                       "Tags": "warning" if title == "잡 실패" else "white_check_mark",
                       "Content-Type": "text/plain; charset=utf-8"}
            if settings.get("token"):
                headers["Authorization"] = "Bearer " + settings["token"]
            request = urllib.request.Request(settings["server"] + "/" + settings["topic"],
                                             data=message.encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=10):
                pass
            return {"ok": True}
        except Exception as exc:
            error = type(exc).__name__
    log.warning("ntfy 전송 실패 (%s)", error)
    return {"ok": False, "error": "알림 전송에 실패했어요: " + error}


def send_async(settings, title, message):
    threading.Thread(target=send, args=(dict(settings), title, message), daemon=True).start()


def job_finished(job, jobs):
    """종료 상태 변경과 같은 jobs lock 안에서 한 번 호출한다. 네트워크는 스레드에서 처리한다."""
    try:
        status = job.get("status")
        if status not in ("done", "failed") or job.get("deleted"):
            return
        settings = get_settings()
        if not settings["enabled"] or not settings["topic"]:
            _counts.update(done=0, failed=0)
            return
        if settings["mode"] == "each":
            _counts.update(done=0, failed=0)
            try:
                elapsed = max(0, int((datetime.fromisoformat(job["finished_at"]) -
                                      datetime.fromisoformat(job["started_at"])).total_seconds()))
                duration = f"{elapsed}초"
            except (KeyError, TypeError, ValueError):
                duration = "알 수 없음"
            name = job.get("name") or job.get("template_label") or job.get("template_id") or "잡"
            send_async(settings, "잡 완료" if status == "done" else "잡 실패",
                       f"{name}\nID: {job.get('id', '')}\n걸린 시간: {duration}")
        else:
            _counts[status] += 1
            if any(not j.get("deleted") and j.get("status") in ("queued", "running")
                   for j in jobs.values()):
                return
            message = f"완료 {_counts['done']} · 실패 {_counts['failed']}"
            _counts.update(done=0, failed=0)
            send_async(settings, "모든 잡 완료", message)
    except Exception as exc:
        log.warning("ntfy 종료 알림 처리 실패 (%s)", type(exc).__name__)
