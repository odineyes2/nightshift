# app.py가 app_parts/*.py를 번호 순서대로 한 네임스페이스에서 실행한다 — 앞 파일의 이름을 import 없이 쓴다.
# ---- 회원 관리 (admin 전용) ---------------------------------------------------------------

@app.get("/api/admin/users")
def admin_list_users(request: Request):
    admin_only(request)
    return {"users": auth.list_users()}


@app.post("/api/admin/users/{user_id}/status")
async def admin_set_user_status(user_id: int, request: Request):
    admin = admin_only(request)
    body = await read_json_object(request, allow_empty=False)
    try:
        return {"user": auth.set_status(user_id, str(body.get("status") or ""), admin["id"])}
    except auth.AuthError as e:
        raise _auth_error(e)


@app.post("/api/admin/users/{user_id}/reset-password")
async def admin_reset_password(user_id: int, request: Request):
    admin_only(request)
    body = await read_json_object(request, allow_empty=False)
    try:
        await asyncio.to_thread(auth.reset_password, user_id, body.get("new_password"))
    except auth.AuthError as e:
        raise _auth_error(e)
    return {"ok": True}


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int, request: Request):
    admin_only(request)
    try:
        auth.delete_user(user_id)
    except auth.AuthError as e:
        raise _auth_error(e)
    pod_registry.release_owner(user_id)   # 그 회원의 파드는 주인 없음(=관리자 것)이 된다
    return {"ok": True}


@app.get("/api/templates")
def list_templates():
    return load_templates_list()


# 헤더 배지가 5초마다 물어보는 연결 상태의 캐시. 이 요청이 매번 원격 pod까지
# 왕복하면 탭 수만큼 pod를 찌르고, 응답이 느린 날에는 요청 자체가 몇 초씩 걸린다.
# 그래서 캐시값을 즉시 돌려주고 오래됐으면 백그라운드로 다시 확인한다.
# 파드마다 하나씩. 파드가 여러 대면 대시보드가 전부를 한 번에 물어보므로, 캐시가
# 없으면 그 요청 하나가 파드 수만큼의 왕복이 된다.
_comfy_status_cache: dict[str, dict] = {}
_comfy_status_lock = threading.Lock()
_comfy_status_refreshing: set[str] = set()


def _status_entry(pod_id: str) -> dict:
    """호출 전에 _comfy_status_lock을 쥐고 있어야 한다."""
    return _comfy_status_cache.setdefault(pod_id, {
        "url": None, "connected": False, "source": "auto", "checked_at": 0.0, "fail_streak": 0,
    })


def pod_status_payload(pod: dict) -> dict:
    """캐시된 연결 상태 (호출 전에 _comfy_status_lock을 쥐고 있어야 한다)."""
    entry = _status_entry(pod["id"])
    checked_at = entry["checked_at"]
    return {
        "pod_id": pod["id"],
        "url": entry["url"],
        "connected": entry["connected"],
        "source": entry["source"],
        # 화면이 갤러리의 "결과 가져오기" 버튼을 보여줄지 정하는 데 쓴다 — 5초마다
        # 폴링하는 이 응답에 실어 주면 설정을 바꿨을 때 저절로 따라온다.
        "pull_outputs": bool(pod.get("pull_outputs")),
        # 이 상태가 몇 초 전에 실측된 것인지(캐시라는 사실을 숨기지 않는다).
        "checked_age_sec": round(time.monotonic() - checked_at, 1) if checked_at else None,
    }


def refresh_pod_status(pod: dict) -> dict:
    """실제로 그 파드를 찔러 보고 캐시를 갱신한다(블로킹).

    한 번 실패했다고 바로 "연결 안 됨"으로 뒤집지 않는다 — WAN에서는 패킷 하나만
    흘려도 실패로 보이는데, 그때마다 배지가 빨갛게 깜빡이면 아무도 안 믿게 된다.
    직전까지 같은 주소로 연결돼 있었다면 COMFY_STATUS_FAIL_STREAK번 연속 실패할
    때까지 "연결됨"을 유지한다(반대로 다시 붙는 건 즉시 반영한다)."""
    health = driver_for(pod).health(pod)
    url, connected, source = health["url"], health["ok"], health["source"]
    with _comfy_status_lock:
        entry = _status_entry(pod["id"])
        if connected:
            reported, streak = True, 0
        else:
            streak = entry["fail_streak"] + 1
            was_up = entry["connected"] and entry["url"] == url
            reported = was_up and streak < COMFY_STATUS_FAIL_STREAK
        entry.update({
            "url": url, "connected": reported, "source": source,
            "checked_at": time.monotonic(), "fail_streak": streak,
        })
        return pod_status_payload(pod)


def invalidate_comfy_status_cache(pod_id: str | None = None):
    """주소 설정이 바뀌었을 때 — 다음 조회가 옛 주소의 결과를 그대로 쓰지 않게 한다."""
    with _comfy_status_lock:
        targets = [pod_id] if pod_id else list(_comfy_status_cache)
        for pid in targets:
            _comfy_status_cache.pop(pid, None)


def _refresh_pod_status_bg(pod: dict):
    try:
        refresh_pod_status(pod)
    finally:
        with _comfy_status_lock:
            _comfy_status_refreshing.discard(pod["id"])


async def pod_status(pod: dict) -> dict:
    """캐시된 값을 즉시 돌려주고, 오래됐으면 백그라운드로 다시 확인한다."""
    pod_id = pod["id"]
    with _comfy_status_lock:
        entry = _status_entry(pod_id)
        first_time = not entry["checked_at"]
        stale = first_time or (time.monotonic() - entry["checked_at"]) >= COMFY_STATUS_TTL_SEC
        should_refresh = stale and pod_id not in _comfy_status_refreshing
        if should_refresh:
            _comfy_status_refreshing.add(pod_id)

    if first_time and should_refresh:
        # 기동 직후 그 파드의 첫 조회. 여기서만 실제 확인이 끝날 때까지 기다린다 —
        # 첫 화면에 근거 없는 "연결 안 됨"이 떴다가 5초 뒤에 바뀌는 것보다 낫다.
        try:
            return await asyncio.to_thread(refresh_pod_status, pod)
        finally:
            with _comfy_status_lock:
                _comfy_status_refreshing.discard(pod_id)

    if should_refresh:
        threading.Thread(target=_refresh_pod_status_bg, args=(pod,), daemon=True).start()
    with _comfy_status_lock:
        return pod_status_payload(pod)


@app.get("/api/comfy-status")
async def comfy_status(request: Request):
    """헤더 배지가 5초마다 물어보는 "지금 기본으로 쓰는 워커"의 연결 상태."""
    pod = default_pod_for_user(me(request))
    if pod is None:
        return {"pod_id": None, "url": None, "connected": False, "source": "none",
                "pull_outputs": False, "checked_age_sec": None}
    return await pod_status(pod)


def comfy_endpoint_payload(user: dict) -> dict:
    """접속 주소 설정 화면이 쓰는 현재 상태 — 기본 파드의 저장된 값, 실제로 쓰이는 값과
    그 출처. 다중 파드로 넘어간 뒤에도 이 화면(헤더의 연결 상태 배지)은 "지금 기본으로
    쓰는 워커"를 보여주는 자리로 남으므로, 응답 형식을 그대로 유지한다."""
    pod = default_pod_for_user(user)
    admin = auth.is_admin(user)
    if pod is None:
        return {"pod_id": None, "pod_name": None, "url": "", "effective_url": None, "source": "none", "env_url": "",
                "candidates": [], "updated_at": None, "pull_outputs": False, "output_dir": "",
                "output_sync": {"last_sync": None, "known": 0}}
    effective_url, source = ComfyUIDriver.configured(pod)
    return {
        "pod_id": pod["id"],                              # 어느 파드의 설정인지
        "pod_name": pod["name"],
        "url": pod.get("url") or "",                      # 저장된 설정값(비어 있으면 미설정)
        "effective_url": effective_url,                   # 설정/환경변수로 정해진 주소(자동 탐지면 null)
        "source": source,                                 # "setting" | "env" | "auto"
        # 서버 내부 정보(환경변수 주소·자동 탐지 후보·출력 폴더 경로)는 관리자에게만 알려 준다.
        "env_url": (os.environ.get("COMFY_URL") or "") if admin else "",
        "candidates": COMFY_CANDIDATE_URLS if admin else [],
        "updated_at": pod.get("updated_at"),
        "pull_outputs": bool(pod.get("pull_outputs")),    # 결과 이미지를 HTTP로 끌어올지
        "output_dir": OUTPUT_DIR if admin else "",        # 끌어온 이미지가 쌓이는 로컬 폴더
        "output_sync": sync_state_summary(),              # {"last_sync", "known"}
    }


@app.get("/api/comfy-endpoint")
def get_comfy_endpoint(request: Request):
    return comfy_endpoint_payload(me(request))


@app.put("/api/comfy-endpoint")
async def put_comfy_endpoint(request: Request):
    # ComfyUI 주소를 런타임에 바꾼다 — nightshift를 홈서버에 상시 띄워두고 ComfyUI만
    # 원격 pod에서 돌리는 구성에서는 pod를 새로 만들 때마다 주소가 바뀌므로, 서버를
    # 재시작하지 않고 화면에서 갈아끼울 수 있어야 한다. 빈 문자열을 보내면 설정을
    # 지우고 환경변수/자동 탐지로 되돌린다.
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    if not isinstance(data, dict):
        raise HTTPException(400, '{"url": "..."} 형태의 객체여야 해요.')
    # 이 엔드포인트는 "이 회원의 기본 파드의 주소를 바꾼다"는 뜻이다.
    user = me(request)
    pod = default_pod_for_user(user)
    if pod is None:
        raise HTTPException(400, "워커가 아직 없어요. 워커 화면에서 먼저 추가해 주세요.")
    patch = {"url": data.get("url", "")}
    if not auth.is_admin(user):
        await asyncio.to_thread(_require_public_pod_url, patch["url"], pod.get("kind"))
    # pull_outputs를 아예 안 보내면 지금 설정을 유지한다 — 주소만 바꾸려는 요청이
    # 조용히 "가져오기 끄기"로 동작하면 안 되므로.
    if "pull_outputs" in data:
        patch["pull_outputs"] = bool(data.get("pull_outputs"))
    try:
        pod_registry.update_pod(pod["id"], patch)
    except pod_registry.PodError as e:
        raise HTTPException(400, str(e))
    # 주소가 바뀌면 이전 서버에서 받아둔 모델/노드 목록은 더 이상 그 파드의 것이
    # 아니다. 캐시 키에 url이 들어 있어 자연히 미스가 나지만, 명시적으로 비워서
    # "바꾼 직후 잠깐 옛 목록이 보이는" 창을 없앤다.
    ComfyUIDriver.invalidate_capabilities(pod["id"])
    invalidate_comfy_status_cache()

    payload = comfy_endpoint_payload(user)
    effective_url = payload["effective_url"]
    if effective_url:
        payload["connected"] = await asyncio.to_thread(
            check_comfy_url, effective_url, COMFY_CHECK_TIMEOUT_INTERACTIVE
        )
    else:
        _, payload["connected"] = await asyncio.to_thread(resolve_comfy_url)
    return payload


@app.post("/api/comfy-endpoint/test")
async def test_comfy_endpoint(request: Request):
    # 저장하기 전에 "이 주소가 실제로 응답하는지"만 확인한다(설정은 건드리지 않음).
    # url을 비워서 보내면 지금 적용 중인 주소를 그대로 확인한다.
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    if not isinstance(data, dict):
        raise HTTPException(400, '{"url": "..."} 형태의 객체여야 해요.')
    user = me(request)
    try:
        url = pod_registry.normalize_pod_url(data.get("url", ""))
    except pod_registry.PodError as e:
        raise HTTPException(400, str(e))

    if not auth.is_admin(user):
        # 일반 회원이 입력한 주소로 이 서버가 대신 접속하므로 서버 안쪽 주소는 막는다.
        await asyncio.to_thread(_require_public_pod_url, url, pod_registry.DEFAULT_KIND)
    elif not url:
        url, _ = await asyncio.to_thread(resolve_comfy_url)
        if not url:
            return {"url": None, "connected": False, "detail": "확인할 주소가 없어요(자동 탐지도 실패)."}
    connected = await asyncio.to_thread(check_comfy_url, url, COMFY_CHECK_TIMEOUT_INTERACTIVE)
    return {"url": url, "connected": connected}


