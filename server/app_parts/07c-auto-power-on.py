# 대기열 자동 켜기 — 워커를 기다리는 대기 작업이 N개 이상이거나 가장 오래된 작업이 정한 시간보다 오래 기다리면
# nightshift가 만든 그 등급(이미지·영상)의 RunPod 워커 하나를 켠다. 끄기는 기존 자동 끄기(NS-8)가 맡는다(켤 때 그 워커의 자동 끄기를 켠다).
# 등급은 따로 판단한다: 영상 작업(job["runpod_tier"]=="video", 서비스 연동 영상 생성)은 영상 등급 워커만, 나머지는 이미지 워커.
# 안전장치: 이미 켜진(또는 켜는 중인) 같은 등급 워커가 있으면 켜지 않음, 하루 상한, 실패하면 10→30→60분 뒤 재시도.
# 설정·기록 저장은 auto_power_settings.py, 확인 주기는 RUNPOD_AUTO_OFF_CHECK_SEC(자동 끄기와 같은 60초).
TIER_LABEL = {"image": "이미지", "video": "영상"}
# 회원마다 자기 RunPod 계정을 쓰므로(runpod_api.key_for_owner) 이 자동 켜기는 관리자 설정이자 관리자 계정 일이다:
# 관리자 워커만 켜고, 관리자 파드에서 돌 수 있는 작업(관리자·서비스 연동 계정의 작업)만 센다 — 일반 회원이 작업을
# 쌓아 관리자 파드가 켜지면 그 작업은 거기서 돌지도 못한 채 요금만 나간다.


def _admin_owner_ids() -> set:
    with db.read() as conn:
        ids = {row["id"] for row in conn.execute("SELECT id FROM users WHERE role='admin' AND status='active'")}
    return ids | {None}   # 주인 없음 = 관리자 것


def _runs_on_admin_pods(job: dict, admins: set) -> bool:
    owner = job.get("owner_id")
    return owner in admins or any(o in admins for o in service_tokens.pod_owner_ids_for_job(owner))


def _auto_power_waiting(now: datetime, tier: str = "image") -> tuple[int, float]:
    """관리자 그 등급 워커(comfyui)를 기다리는 대기 작업 수와 가장 오래 기다린 시간(시간 단위)."""
    templates = load_templates_map()
    count, oldest = 0, 0.0
    with lock:
        waiting = [j for j in jobs.values() if j["status"] == "queued" and not j.get("pod_id") and not j.get("deleted")]
    admins = _admin_owner_ids()
    waiting = [j for j in waiting if _runs_on_admin_pods(j, admins)]
    for job in waiting:
        allowed = (templates.get(job.get("template_id")) or {}).get("pod_kinds") or [pod_registry.DEFAULT_KIND]
        if pod_registry.DEFAULT_KIND not in allowed or (job.get("runpod_tier") == "video") != (tier == "video"):
            continue
        count += 1
        try:
            queued = datetime.fromisoformat(job["queued_at"])
            queued = queued if queued.tzinfo else queued.replace(tzinfo=timezone.utc)
            oldest = max(oldest, (now - queued).total_seconds() / 3600)
        except (KeyError, TypeError, ValueError):
            pass
    return count, oldest


def _auto_power_candidates(tier: str = "image") -> list[dict]:
    """자동으로 켤 수 있는 워커: nightshift가 만든 그 등급의 관리자 RunPod 워커."""
    admins = _admin_owner_ids()
    return [p for p in pod_registry.list_pods(pod_registry.ALL)
            if p.get("kind") == pod_registry.DEFAULT_KIND and p.get("runpod_tier") == tier and p.get("owner_id") in admins]


def _auto_power_image_on(tier: str = "image") -> dict | None:
    """이미 켜져 있거나 켜는 중인 그 등급 워커(있으면 자동으로 켜지 않는다). 이미지는 영상 워커를 뺀 모든 ComfyUI 워커,
    영상은 영상 등급 워커만 본다(영상 작업은 영상 워커에서만 돈다 — 04 schedule_once)."""
    def same(p):
        return (p.get("runpod_tier") == "video") == (tier == "video")
    admins = _admin_owner_ids()
    return next((p for p in pod_registry.list_pods(pod_registry.ALL)
                 if p.get("kind") == pod_registry.DEFAULT_KIND and same(p) and p.get("owner_id") in admins
                 and (p.get("enabled") or p.get("note") == "runpod:creating")), None)


def _auto_power_starts_today(state: dict, now: datetime) -> int:
    today = now.astimezone().date()
    count = 0
    for stamp in state["starts"]:
        try:
            if datetime.fromisoformat(stamp).astimezone().date() == today:
                count += 1
        except ValueError:
            pass
    return count


def _auto_power_pick(settings: dict, tier: str = "image") -> dict | None:
    candidates = [p for p in _auto_power_candidates(tier) if not p.get("enabled")]
    if settings["pod_id"]:
        return next((p for p in candidates if p["id"] == settings["pod_id"]), None)
    ready = [p for p in candidates if (p.get("note") or "") in ("runpod:off", "")]
    return max(ready, key=lambda p: p.get("updated_at") or "", default=None)   # 최근에 쓴 워커부터


def _auto_power_start(pod: dict, tier: str = "image") -> str | None:
    """그 워커에 새 RunPod 파드를 만들어 붙인다(사람의 "전원 켜기"와 같은 잠금·순서). 실패하면 에러 문구."""
    power = _try_power_lock(pod["id"])
    if power is None:
        return "이 워커는 이미 켜거나 끄는 중이에요."
    try:
        pod = pod_registry.get_pod(pod["id"])
        if pod is None or pod.get("enabled"):
            return "워커가 없거나 이미 켜져 있어요."
        old = runpod_api.extract_pod_id(pod.get("url") or "")
        if old:   # 예전 방식으로 꺼 둔 파드가 남아 있으면 먼저 지운다(_managed_runpod_power와 같다)
            err = runpod_api.terminate_pod(old, runpod_api.key_for_owner(pod.get("owner_id")))
            if err and "HTTP 404" not in err:
                return f"남아 있던 RunPod 파드를 지우지 못했어요 — {err}"
        # 일이 끝나면 꺼지도록 이 워커의 자동 끄기를 켠다.
        pod = pod_registry.update_pod(pod["id"], {"url": "", "note": "runpod:creating", "auto_power_off": True})
        base = os.environ.get("NIGHTSHIFT_PUBLIC_URL", "").strip().rstrip("/") or None
        _, _, err = _attach_new_runpod(base, pod, tier)
        if err:
            pod_registry.update_pod(pod["id"], {"url": "", "enabled": False, "note": "runpod:off"})
            return f"새 RunPod 파드를 만들지 못했어요 — {err}"
        with _pod_card_lock:
            _pod_card_cache.pop(pod["id"], None)
        invalidate_comfy_status_cache(pod["id"])
        ComfyUIDriver.invalidate_capabilities(pod["id"])
        return None
    finally:
        power.release()


def _auto_power_decide(settings: dict, state: dict, now: datetime, tier: str = "image") -> dict:
    """지금 켤지 판단만 한다(켜지는 않음). {"action": "start"|"wait"|"off", "reason": 화면용 문장, "pod": 워커}."""
    count, oldest = _auto_power_waiting(now, tier)
    info = {"waiting": count, "oldest_hours": round(oldest, 2), "starts_today": _auto_power_starts_today(state, now)}
    if tier == "video" and not settings.get("video_jobs"):
        return {**info, "action": "off", "reason": "영상 파드 사용이 꺼져 있어요."}
    if not settings["enabled"]:
        return {**info, "action": "off", "reason": "자동 켜기가 꺼져 있어요."}
    if not runpod_api.admin_key():
        return {**info, "action": "off", "reason": "관리자 RunPod API 키가 없어 자동으로 켤 수 없어요."}
    on = _auto_power_image_on(tier)
    if on:
        return {**info, "action": "wait", "reason": f"'{on.get('name') or on['id']}' 워커가 켜져 있어 그 워커가 처리해요."}
    if count == 0:
        return {**info, "action": "wait", "reason": "기다리는 작업이 없어요."}
    due = count >= settings["min_jobs"] or (settings["max_wait_hours"] and oldest >= settings["max_wait_hours"])
    if not due:
        wait = f" 또는 가장 오래된 작업이 {settings['max_wait_hours']}시간을 넘기면" if settings["max_wait_hours"] else ""
        return {**info, "action": "wait", "reason": f"작업 {count}/{settings['min_jobs']}개 모임 — {settings['min_jobs']}개가 모이면{wait} 켜요."}
    if info["starts_today"] >= settings["daily_limit"]:
        return {**info, "action": "wait", "reason": f"오늘 자동 켜기 {settings['daily_limit']}번을 다 썼어요. 내일 다시 켜거나 직접 켜 주세요."}
    if state["retry_after"] and now < datetime.fromisoformat(state["retry_after"]):
        local = datetime.fromisoformat(state["retry_after"]).astimezone().strftime("%H:%M")
        return {**info, "action": "wait", "reason": f"켜기에 실패해 {local}에 다시 시도해요 — {state['last_error'] or '원인 모름'}"}
    pod = _auto_power_pick(settings, tier)
    if pod is None:
        target = "고른 워커가 없거나 켜져 있어요." if settings["pod_id"] else f"꺼져 있는 {TIER_LABEL[tier]}용 RunPod 워커가 없어요."
        return {**info, "action": "wait", "reason": f"켤 워커를 찾지 못했어요 — {target}"}
    why = f"작업 {count}개가 모여" if count >= settings["min_jobs"] else f"가장 오래된 작업이 {int(oldest)}시간 넘게 기다려"
    return {**info, "action": "start", "reason": f"{why} '{pod.get('name') or pod['id']}' 워커를 켜요.", "pod": pod}


def _auto_power_on_check(now: datetime | None = None, tier: str = "image") -> dict:
    now = now or datetime.now(timezone.utc)
    settings, state = auto_power_settings.load(tier), auto_power_settings.load_state(tier)
    decision = _auto_power_decide(settings, state, now, tier)
    if decision["action"] != "start":
        return decision
    log = logging.getLogger("uvicorn.error")
    err = _auto_power_start(decision["pod"], tier)
    if err:
        failures = state["failures"] + 1
        minutes = auto_power_settings.BACKOFF_MINUTES[min(failures, len(auto_power_settings.BACKOFF_MINUTES)) - 1]
        auto_power_settings.save_state({"failures": failures, "last_error": err[:300],
                                        "retry_after": (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")}, tier)
        log.warning("자동 켜기 실패 — 워커 %s: %s (%d분 뒤 다시 시도)", decision["pod"]["id"], err, minutes)
        return {**decision, "action": "failed", "reason": err}
    auto_power_settings.save_state({"starts": state["starts"] + [now.isoformat(timespec="seconds")], "failures": 0,
                                    "retry_after": None, "last_error": None, "last_reason": decision["reason"],
                                    "last_pod_id": decision["pod"]["id"]}, tier)
    log.info("자동 켜기 — %s", decision["reason"])
    return {**decision, "action": "started"}


def _auto_power_on_loop():
    while True:
        time.sleep(RUNPOD_AUTO_OFF_CHECK_SEC)
        for tier in auto_power_settings.TIERS:
            try:
                _auto_power_on_check(tier=tier)
            except Exception:
                logging.getLogger("uvicorn.error").exception("자동 켜기 검사 실패(%s)", tier)


def _auto_power_payload(tier: str = "image") -> dict:
    settings, state = auto_power_settings.load(tier), auto_power_settings.load_state(tier)
    decision = _auto_power_decide(settings, state, datetime.now(timezone.utc), tier)
    decision.pop("pod", None)
    candidates = [{"id": p["id"], "name": p.get("name") or p["id"], "on": bool(p.get("enabled"))} for p in _auto_power_candidates(tier)]
    return {"settings": settings, "status": decision, "candidates": candidates, "limits": auto_power_settings.LIMITS,
            "last": {key: state[key] for key in ("last_reason", "last_error", "retry_after", "last_pod_id")},
            "api_key": bool(runpod_api.admin_key())}


@app.get("/api/generate/auto-power")
def generate_auto_power_status(request: Request):
    """외부 서비스(charchat 등)용 읽기 전용 상태 — 자동 켜기 사용 여부·기다리는 이미지 작업 수·켜는 기준·켜진 워커 유무.
    설정값을 바꾸거나 워커 이름·이유 문장 같은 내부 정보를 주지 않는다(서비스 토큰 허용 경로)."""
    me(request)
    settings, state = auto_power_settings.load(), auto_power_settings.load_state()
    decision = _auto_power_decide(settings, state, datetime.now(timezone.utc))
    return {"enabled": settings["enabled"] and bool(runpod_api.admin_key()), "waiting": decision["waiting"],
            "min_jobs": settings["min_jobs"], "max_wait_hours": settings["max_wait_hours"],
            "worker_on": _auto_power_image_on() is not None,
            "limit_reached": decision["starts_today"] >= settings["daily_limit"]}


@app.get("/api/generate/video/status")
def generate_video_status(request: Request):
    """외부 서비스용 영상 생성 상태(읽기 전용) — 영상 파드 사용 여부와 영상 자동 켜기 요약. 서비스 토큰 허용 경로."""
    me(request)
    settings, state = auto_power_settings.load("video"), auto_power_settings.load_state("video")
    decision = _auto_power_decide(settings, state, datetime.now(timezone.utc), "video")
    return {"video_jobs": settings["video_jobs"],
            "enabled": settings["video_jobs"] and settings["enabled"] and bool(runpod_api.admin_key()),
            "waiting": decision["waiting"], "min_jobs": settings["min_jobs"], "max_wait_hours": settings["max_wait_hours"],
            "worker_on": _auto_power_image_on("video") is not None,
            "limit_reached": decision["starts_today"] >= settings["daily_limit"]}


@app.get("/api/settings/auto-power/video")
def get_video_auto_power_settings(request: Request):
    """설정 > 자동 켜기의 영상 칸 — 영상 파드 사용(기본 꺼짐)과 영상 워커 자동 켜기(관리자만)."""
    admin_only(request)
    return _auto_power_payload("video")


@app.put("/api/settings/auto-power/video")
async def put_video_auto_power_settings(request: Request):
    admin_only(request)
    body = await read_json_object(request, allow_empty=False)
    if body.get("pod_id") and not any(p["id"] == body["pod_id"] for p in _auto_power_candidates("video")):
        raise HTTPException(400, "영상용 RunPod 워커만 고를 수 있어요.")
    try:
        auto_power_settings.save(body, "video")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _auto_power_payload("video")


@app.get("/api/settings/auto-power")
def get_auto_power_settings(request: Request):
    """설정 > 자동 켜기 — 설정값, 지금 판단(대기 작업 수·이유), 켤 수 있는 워커 목록(관리자만)."""
    admin_only(request)
    return _auto_power_payload()


@app.put("/api/settings/auto-power")
async def put_auto_power_settings(request: Request):
    admin_only(request)
    body = await read_json_object(request, allow_empty=False)
    if body.get("pod_id") and not any(p["id"] == body["pod_id"] for p in _auto_power_candidates()):
        raise HTTPException(400, "이미지용 RunPod 워커만 고를 수 있어요.")
    try:
        auto_power_settings.save(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _auto_power_payload()
