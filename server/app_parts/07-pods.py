# app.py가 app_parts/*.py를 번호 순서대로 한 네임스페이스에서 실행한다 — 앞 파일의 이름을 import 없이 쓴다.
# ---- 파드(워커) 관리 ----------------------------------------------------------
# nightshift가 작업을 보낼 워커들의 목록(pod_registry.py). 지금은 파드 1개를 전제로
# 나머지 코드가 돌아가지만(기본 파드), 여기서 여러 개를 등록해 둘 수 있고 파드별 큐로
# 실제로 나눠 돌리는 것은 다음 단계다(multipod_plan.md의 P1).

def pod_payload(pod: dict) -> dict:
    """레코드 + 화면이 바로 쓸 수 있는 파생 정보(드라이버 이름, 실제로 쓰일 주소)."""
    try:
        driver = driver_for(pod)
    except DriverError:
        return {**pod, "kind_label": pod.get("kind"), "effective_url": None, "url_source": "unknown"}
    url, source = driver.resolve(pod)
    return {**pod, "kind_label": driver.label, "effective_url": url, "url_source": source}


def _require_public_pod_url(url: str, kind: str | None) -> None:
    """일반 회원의 파드 주소 검사 — 비어 있으면 안 되고(비면 이 서버 자신의 ComfyUI로 떨어진다) 서버 안쪽 주소도
    안 된다. 실패하면 HTTPException(400)."""
    url = (url or "").strip()
    if not url:
        raise HTTPException(400, "워커 주소를 적어주세요.")
    try:
        normalized = pod_registry.normalize_pod_url(url, kind or pod_registry.DEFAULT_KIND)
        pod_registry.assert_public_url(normalized)
    except pod_registry.PodError as e:
        raise HTTPException(400, str(e))


def _user_kinds(user: dict) -> list[dict]:
    """이 회원이 만들 수 있는 파드 종류 — 셸(서버에서 명령 실행)과 Claude 글쓰기(관리자 키 사용)는 관리자만."""
    kinds = driver_kinds()
    return kinds if auth.is_admin(user) else [k for k in kinds if k["kind"] == pod_registry.DEFAULT_KIND]


@app.get("/api/pods")
def list_pods_api(request: Request):
    user = me(request)
    pods = visible_pods_for(user)
    names = auth.usernames() if auth.is_admin(user) else {}
    default = default_pod_for_user(user)
    return {
        "pods": [{**pod_payload(p), "owner_name": names.get(p.get("owner_id"))} for p in pods],
        "default_pod_id": default["id"] if default else None,
        "kinds": _user_kinds(user),
    }


@app.post("/api/pods")
async def create_pod_api(request: Request):
    user = me(request)
    data = await read_json_object(request, allow_empty=False)
    data = {**data, "owner_id": user["id"]}   # 주인은 요청이 정하는 게 아니라 로그인한 회원이다
    kind = (data.get("kind") or pod_registry.DEFAULT_KIND).strip()
    try:
        driver = driver_for({"kind": kind})
    except DriverError as e:
        raise HTTPException(400, str(e))
    if not driver.available():
        raise HTTPException(400, driver.unavailable_reason())
    if not auth.is_admin(user):
        if kind != pod_registry.DEFAULT_KIND:
            raise HTTPException(403, "이 종류의 워커는 관리자만 만들 수 있어요.")
        await asyncio.to_thread(_require_public_pod_url, data.get("url"), kind)
    try:
        pod = pod_registry.create_pod(data)
    except pod_registry.PodError as e:
        raise HTTPException(400, str(e))
    ensure_runtime(pod)   # 큐와 워커 스레드를 바로 준비한다
    return pod_payload(pod)


@app.put("/api/pods/{pod_id}")
async def update_pod_api(pod_id: str, request: Request):
    # 부분 수정 — 보낸 필드만 바뀐다(이름만 바꾸려는 요청이 주소를 지우면 안 되므로).
    user = me(request)
    current = pod_or_404(user, pod_id)
    data = await read_json_object(request, allow_empty=False)
    data = {k: v for k, v in data.items() if k != "owner_id"}   # 주인은 바꿀 수 없다
    if not auth.is_admin(user):
        if data.get("kind") not in (None, pod_registry.DEFAULT_KIND):
            raise HTTPException(403, "이 종류의 워커는 관리자만 만들 수 있어요.")
        if "url" in data:
            await asyncio.to_thread(_require_public_pod_url, data.get("url"), current.get("kind"))
    try:
        pod = pod_registry.update_pod(pod_id, data)
    except pod_registry.PodError as e:
        raise HTTPException(400 if "없는 워커" not in str(e) else 404, str(e))
    # 주소나 종류가 바뀌었으면 그 파드에 대해 캐싱해 둔 것들은 더 이상 유효하지 않다.
    ComfyUIDriver.invalidate_capabilities(pod_id)
    invalidate_comfy_status_cache(pod_id)
    ensure_runtime(pod)   # max_concurrent를 늘렸으면 워커 스레드를 더 띄운다
    return pod_payload(pod)


@app.delete("/api/pods/{pod_id}")
def delete_pod_api(pod_id: str, request: Request):
    user = me(request)
    target = pod_or_404(user, pod_id)
    # 돌고 있는 작업이 있으면 막는다 — 지우는 순간 그 작업이 어디에도 속하지 않게 되고,
    # 서브프로세스만 남아 결과를 아무도 회수하지 않는다. 먼저 멈추게 한다.
    with lock:
        rt = pod_runtimes.get(pod_id)
        busy = list(rt.running) if rt else []
    if busy:
        raise HTTPException(400, f"이 워커에서 작업 {len(busy)}개가 실행 중이에요. 먼저 멈춰주세요.")
    # 이 파드에 배정돼 있던(아직 안 끝난) 작업은 같은 주인의 다른 파드로 되돌린다 — 갈 곳 없는
    # 작업이 영영 안 도는 상태로 남으면 안 되므로. 갈 파드가 없으면 지우지 못하게 막는다.
    others = [p for p in pod_registry.list_pods(target.get("owner_id")) if p["id"] != pod_id]
    fallback = next((p for p in others if p.get("enabled")), others[0] if others else None)
    with lock:
        waiting_jobs = any(j.get("pod_id") == pod_id and j["status"] in ("pending", "queued") and not j.get("deleted")
                           for j in jobs.values())
    if waiting_jobs and fallback is None:
        raise HTTPException(400, "이 워커에 대기 중인 작업이 있어요. 다른 워커를 먼저 추가하거나 작업을 지워주세요.")
    try:
        removed = pod_registry.delete_pod(pod_id)
    except pod_registry.PodError as e:
        raise HTTPException(404 if "없는 워커" in str(e) else 400, str(e))
    with lock:
        moved = []
        for job in jobs.values():
            if job.get("pod_id") == pod_id and job["status"] in ("pending", "queued") and fallback is not None:
                job["pod_id"] = fallback["id"]
                if job["status"] == "queued":
                    job["status"] = "pending"   # 이 파드의 큐와 함께 사라졌으므로
                    set_comfy_wait_flag(job, False)
                moved.append(job["id"])
    if moved:
        save_state()
    ComfyUIDriver.invalidate_capabilities(pod_id)
    invalidate_comfy_status_cache(pod_id)
    sync_runtimes()
    return {"deleted": removed["id"], "name": removed["name"],
            "moved_jobs": len(moved), "moved_to": fallback["id"] if fallback else None}


# 드라이버가 카드에 실어 주는 파드별 추가 정보(ComfyUI라면 GPU/VRAM). 연결 상태와 달리
# 이건 매번 필요하지도 않고 왕복이 한 번 더 드니, 더 길게 캐싱하고 백그라운드로만 갱신한다.
POD_CARD_TTL_SEC = 20
_pod_card_cache: dict[str, dict] = {}
_pod_card_lock = threading.Lock()
_pod_card_refreshing: set[str] = set()


def _refresh_pod_card_bg(pod: dict):
    pod_id = pod["id"]
    try:
        data = driver_for(pod).card(pod)
    except Exception:
        data = {}
    with _pod_card_lock:
        _pod_card_cache[pod_id] = {"data": data, "at": time.monotonic()}
        _pod_card_refreshing.discard(pod_id)


def pod_card_data(pod: dict) -> dict:
    """캐시된 값을 즉시 돌려주고, 오래됐으면 백그라운드로 다시 받아온다. 처음에는
    빈 dict가 나가고 다음 폴링에서 채워진다 — 카드가 뜨는 걸 막지 않는 게 우선이다."""
    pod_id = pod["id"]
    with _pod_card_lock:
        entry = _pod_card_cache.get(pod_id)
        fresh = entry and (time.monotonic() - entry["at"]) < POD_CARD_TTL_SEC
        if not fresh and pod_id not in _pod_card_refreshing:
            _pod_card_refreshing.add(pod_id)
            start = True
        else:
            start = False
    if start:
        threading.Thread(target=_refresh_pod_card_bg, args=(pod,), daemon=True).start()
    return (entry or {}).get("data") or {}


def recent_job_images(job_ids: list[str], limit: int = 3) -> list[str]:
    """그 작업들이 만든 결과 이미지 중 최근 것 몇 장의 상대 경로(갤러리 썸네일 API에
    그대로 넣을 수 있는 형태). 출력 폴더 전체를 훑지 않고 job_id 폴더만 들여다본다 —
    대시보드는 몇 초마다 폴링하므로 값싸야 한다."""
    base = Path(OUTPUT_DIR)
    names: list[str] = []
    for job_id in job_ids:
        folder = base / job_id
        try:
            entries = sorted(
                (e for e in os.scandir(folder)
                 if e.is_file() and Path(e.name).suffix.lower() in IMAGE_EXTENSIONS),
                key=lambda e: e.stat().st_mtime, reverse=True,
            )
        except OSError:
            continue
        for e in entries:
            names.append(f"{job_id}/{e.name}")
            if len(names) >= limit:
                return names
    return names


@app.get("/api/pods/summary")
async def pods_summary(request: Request):
    """대시보드가 폴링할 파드별 요약 — 레코드 + 연결 상태(캐시) + 큐/실행 상태 +
    오늘 만든 이미지 수. 값싼 것만 담는다: `/system_stats` 같은 파드별 추가 조회는
    캐시를 따로 붙여 대시보드 화면(P2)에서 넣는다."""
    user = me(request)
    pods = visible_pods_for(user)
    owner_names = auth.usernames() if auth.is_admin(user) else {}
    statuses = {}
    for pod in pods:
        statuses[pod["id"]] = await pod_status(pod)

    today = datetime.now(timezone.utc).date().isoformat()
    with lock:
        rows = []
        for pod in pods:
            rt = pod_runtimes.get(pod["id"])
            pod_jobs = [j for j in jobs.values()
                        if j.get("pod_id") == pod["id"] and not j.get("deleted")]
            running_jobs = [
                {"id": j["id"], "template_label": j.get("template_label"),
                 "progress": j.get("progress"), "started_at": j.get("started_at")}
                for j in pod_jobs if j["status"] == "running"
            ]
            waiting = sum(1 for j in pod_jobs if j.get("waiting_for_comfy"))
            done_today = sum(
                (j.get("progress") or {}).get("done") or 0
                for j in pod_jobs
                if (j.get("finished_at") or "").startswith(today)
            )
            finished = sorted(
                (j for j in pod_jobs if j["status"] in ("done", "failed", "interrupted")),
                key=lambda j: j.get("finished_at") or "", reverse=True,
            )
            recent_job_ids = [j["id"] for j in ([*(j for j in pod_jobs if j["status"] == "running")]
                                                + finished)[:3]]
            rows.append({
                **pod_payload(pod),   # 레코드 + kind_label/effective_url 같은 파생 정보
                "owner_name": owner_names.get(pod.get("owner_id")),
                "status": statuses[pod["id"]],
                "card": pod_card_data(pod),
                "recent_images": recent_job_images(recent_job_ids),
                "auto_run": bool(rt and rt.auto_run),
                "queue_len": rt.queue.qsize() if rt else 0,
                "running_jobs": running_jobs,
                "waiting_for_pod": waiting,
                "pending_count": sum(1 for j in pod_jobs if j["status"] == "pending"),
                "images_today": done_today,
            })
    totals = {
        "pods": len(rows),
        "online": sum(1 for r in rows if r["status"]["connected"]),
        "running_jobs": sum(len(r["running_jobs"]) for r in rows),
        "queued": sum(r["queue_len"] for r in rows),
        "pending": sum(r["pending_count"] for r in rows),
        "images_today": sum(r["images_today"] for r in rows),
    }
    default = default_pod_for_user(user)
    return {"pods": rows, "default_pod_id": default["id"] if default else None, "totals": totals}


@app.post("/api/pods/{pod_id}/test")
async def test_pod_api(pod_id: str, request: Request):
    """저장된 그대로의 파드가 실제로 응답하는지 확인한다(설정은 건드리지 않음).
    사람이 버튼을 누르고 기다리는 중이므로 폴링보다 넉넉한 타임아웃을 쓴다."""
    pod = pod_or_404(me(request), pod_id)
    try:
        driver = driver_for(pod)
    except DriverError as e:
        raise HTTPException(400, str(e))
    health = await asyncio.to_thread(driver.health, pod, COMFY_CHECK_TIMEOUT_INTERACTIVE)
    return {"pod_id": pod_id, **health}


@app.post("/api/pods/{pod_id}/runpod/{action}")
async def runpod_power(pod_id: str, action: str, request: Request):
    """파드 카드의 "RunPod 켜기/끄기" — 그 파드 주소에 박힌 RunPod pod를 RunPod API로 켜고 끈 뒤,
    파드 목록을 RunPod와 맞춘다(runpod_sync — 자동 등록 파드의 사용 여부·사용 내역 기록).
    돈이 드는 동작이라 관리자만. 끌 때는 그 파드에서 도는 작업이 있으면 막는다(작업이 중간에 죽으므로)."""
    user = admin_only(request)
    if action not in runpod_api.POD_ACTIONS:
        raise HTTPException(404, "알 수 없는 동작이에요.")
    pod = pod_or_404(user, pod_id)
    rp_id = runpod_api.extract_pod_id(pod.get("url") or "")
    if not rp_id:
        raise HTTPException(400, "RunPod 파드 주소가 아니에요(https://{POD_ID}-{PORT}.proxy.runpod.net).")
    if action == "stop" and _pod_has_running_job(pod_id):
        raise HTTPException(409, "이 워커에서 실행 중인 작업이 있어요. 작업을 먼저 멈춘 뒤 꺼 주세요.")
    status, error = await asyncio.to_thread(runpod_api.pod_action, rp_id, action)
    if error:
        raise HTTPException(502, error)
    if action == "start" and not pod.get("enabled"):
        # 사람이 일부러 켠 파드는 nightshift에서도 쓰는 게 당연하다(자동 등록 파드는 아래 동기화가 켠다).
        pod_registry.update_pod(pod_id, {"enabled": True})
    try:
        await asyncio.to_thread(runpod_sync.sync_runpod_pods, user["id"], False, ensure_runtime, _pod_has_running_job)
    except Exception:
        logging.getLogger("uvicorn.error").exception("RunPod 켜기/끄기 뒤 동기화 실패")
    with _pod_card_lock:
        _pod_card_cache.pop(pod_id, None)
    invalidate_comfy_status_cache(pod_id)
    ComfyUIDriver.invalidate_capabilities(pod_id)
    return {"ok": True, "pod_id": pod_id, "runpod_pod_id": rp_id, "status": status}


@app.get("/api/runpod/network-volumes")
async def runpod_network_volumes_api(request: Request):
    """워커 탭의 "RunPod 네트워크 볼륨" — 계정의 볼륨과, 그 볼륨을 붙여 쓰는 RunPod 파드 이름(관리자만)."""
    admin_only(request)
    (vols, err), (pods, _) = await asyncio.gather(asyncio.to_thread(runpod_api.list_network_volumes),
                                                    asyncio.to_thread(runpod_api.list_runpod_pods_verbose))
    if err:
        raise HTTPException(502, err)
    for v in vols:
        v["pods"] = [p["name"] or p["id"] for p in (pods or []) if p.get("network_volume_id") == v["id"]]
    return {"volumes": vols, "usd_per_gb_month": runpod_api.NETWORK_VOLUME_USD_PER_GB_MONTH, "pods_known": pods is not None}


@app.delete("/api/runpod/network-volumes/{volume_id}")
async def delete_runpod_network_volume_api(volume_id: str, request: Request):
    """볼륨 지우기 — 되돌릴 수 없어서 본문의 confirm_name이 볼륨 이름과 똑같아야 하고, 붙여 쓰는 파드가 있으면 거절한다."""
    admin_only(request)
    body = await read_json_object(request, allow_empty=False)
    vols, err = await asyncio.to_thread(runpod_api.list_network_volumes)
    if err:
        raise HTTPException(502, err)
    vol = next((v for v in vols if v["id"] == volume_id), None)
    if vol is None:
        raise HTTPException(404, "없는 볼륨이에요(이미 지워졌을 수 있어요).")
    if str(body.get("confirm_name") or "") != vol["name"]:
        raise HTTPException(400, "확인용 이름이 볼륨 이름과 달라요.")
    pods, pods_err = await asyncio.to_thread(runpod_api.list_runpod_pods_verbose)
    if pods_err:
        raise HTTPException(502, f"이 볼륨을 쓰는 파드가 있는지 확인하지 못해 지우지 않았어요 — {pods_err}")
    users = [p["name"] or p["id"] for p in pods if p.get("network_volume_id") == volume_id]
    if users:
        raise HTTPException(409, f"이 볼륨을 쓰는 파드가 있어요({', '.join(users)}) — 파드를 먼저 지워 주세요.")
    err = await asyncio.to_thread(runpod_api.delete_network_volume, volume_id)
    if err:
        raise HTTPException(502, err)
    return {"ok": True, "id": volume_id}


@app.post("/api/pods/{pod_id}/runpod-test")
async def test_pod_runpod_api(pod_id: str, request: Request):
    """카드에 뜨는 RunPod 메타데이터(card()의 get_runpod_info())는 실패를 전부 조용히
    삼키므로, "왜 안 뜨는지"를 직접 확인하고 싶을 때 이 엔드포인트로 캐시 없이 다시
    조회해 실패 이유(HTTP 코드/응답 본문/키 미설정 등)를 그대로 돌려준다. 관리자의 RunPod 키로 조회하므로 관리자만."""
    admin_only(request)
    pod = pod_or_404(me(request), pod_id)
    return await asyncio.to_thread(runpod_api.debug_probe, pod.get("url") or "")


def _pod_has_running_job(pod_id: str) -> bool:
    """그 파드에서 지금 실행 중인 작업이 있나 — runpod_sync가 auto 파드를 끄기 전에
    확인한다(돌고 있는 작업의 서브프로세스를 갑자기 고아로 만들면 안 되므로)."""
    with lock:
        return any(j.get("pod_id") == pod_id and j["status"] == "running" for j in jobs.values())


@app.post("/api/pods/sync-runpod")
async def sync_runpod_pods_api(request: Request):
    """RunPod에서 RUNNING인 ComfyUI pod를 찾아 파드 목록에 자동으로 등록/정리한다
    (runpod_sync.py). "사람 대신 도는 자동화"라 소유자를 호출한 관리자로 고정하고,
    셸 파드처럼 관리자만 쓸 수 있다."""
    user = admin_only(request)
    body = await read_json_object(request, allow_empty=True)
    result = await asyncio.to_thread(
        runpod_sync.sync_runpod_pods, user["id"], bool(body.get("dry_run")),
        ensure_runtime, _pod_has_running_job)
    if not result["dry_run"]:
        for item in result["added"] + result["reenabled"] + result["disabled"]:
            pod_id = item.get("id")
            if pod_id:
                invalidate_comfy_status_cache(pod_id)
                ComfyUIDriver.invalidate_capabilities(pod_id)
    return result


@app.get("/api/runpod-sessions")
def get_runpod_sessions(request: Request):
    """DB 탭 — RunPod 세션(사용 내역) 로그(runpod_sessions.py). 비용 정보라 회원
    관리와 같은 기준으로 관리자만 볼 수 있다."""
    admin_only(request)
    return {"sessions": runpod_sessions.list_sessions()}


@app.get("/api/git-log")
def get_git_log(request: Request):
    """DB 탭 — 업데이트 내역(git_log.py). nightshift 저장소의 git 커밋 로그를 그대로
    보여준다 — 따로 기록하는 동작이 없다(커밋 메시지가 곧 기록)."""
    admin_only(request)
    return {"commits": git_log.list_commits(REPO_ROOT)}


@app.get("/api/generation-log")
def get_generation_log(request: Request):
    """DB 탭 — 생성 정보(프롬프트 등) 기록(asset_meta.list_generation_log). nightshift
    큐로 만든 것과 RunPod의 ComfyUI를 직접 써서 만든 뒤 "결과 가져오기"로 받은 것 모두
    파일에 박힌 메타를 assets_index.sync()가 이미 읽어 뒀으므로(server/assets_index.py)
    여기서도 따로 기록하는 동작이 없다 — 있는 값을 최신순으로 보여줄 뿐이다."""
    admin_only(request)
    rows = asset_meta.list_generation_log(limit=300)
    for r in rows:
        pod_name = r.get("job_pod_name")
        if not pod_name and r.get("origin_pod_id"):
            pod = pod_registry.get_pod(r["origin_pod_id"])
            pod_name = pod["name"] if pod else r["origin_pod_id"]
        r["pod_name"] = pod_name
    return {"assets": rows}


def _runpod_sync_loop():
    """RUNPOD_SYNC_INTERVAL_SEC(0보다 클 때만 켜짐, sync_runpod_pods_api 옆의
    startup 코드가 이 스레드를 띄운다)마다 관리자 권한으로 자동 동기화를 돈다 —
    사람이 MCP로 sync_runpod_pods를 안 불러도 "RunPod에서 pod를 켜면 몇 분 안에
    나타난다"가 되게 하려는 것. 관리자가 아직 없으면(초기 설치 단계) 조용히
    건너뛴다. 실패해도 로그만 남기고 다음 주기에 다시 시도한다."""
    while True:
        time.sleep(RUNPOD_SYNC_INTERVAL_SEC)
        try:
            admin_id = auth.admin_id()
            if admin_id is not None:
                runpod_sync.sync_runpod_pods(admin_id, False, ensure_runtime, _pod_has_running_job)
        except Exception:
            logging.getLogger("uvicorn.error").exception("runpod 자동 동기화 실패")


@app.get("/api/comfy-object-info")
async def comfy_object_info(refresh: bool = False, pod_id: str | None = None):
    # 지금 연결된 ComfyUI에 설치된 노드 타입 이름들과 종류별 모델 목록만 추려서
    # 돌려준다(원본 /object_info는 입력 스펙까지 들어있어 수 MB가 되기도 해서
    # 그대로 브라우저로 넘기지 않는다). ComfyUI가 안 떠 있어도 에러가 아니라
    # connected=false + 빈 목록 — 화면에서 "연결 안 됨"으로 안내만 하면 되니까.
    try:
        comfy_url, object_info = await asyncio.to_thread(
            fetch_comfy_object_info, refresh, object_info_pod(pod_id))
    except Exception as e:
        raise HTTPException(502, f"ComfyUI 노드 목록을 가져오지 못했어요: {e}")
    # 파드가 없거나 꺼져 있어도 작업을 구상할 수 있게, 모델 등록부에 적힌 파일 이름도 종류별로 함께 준다.
    catalog: dict[str, list[str]] = {key: [] for key in MODEL_LIST_SOURCES}
    for entry in model_registry.list_entries():
        if entry["kind"] in catalog:
            catalog[entry["kind"]].append(entry["filename"])
    if object_info is None:
        return {
            "connected": False,
            "url": comfy_url,
            "node_types": [],
            "models": {key: [] for key in MODEL_LIST_SOURCES},
            "catalog": catalog,
        }
    return {
        "connected": True,
        "url": comfy_url,
        "catalog": catalog,
        "node_types": sorted(object_info.keys()),
        "models": {
            key: combo_choices(object_info, class_type, field)
            for key, (class_type, field) in MODEL_LIST_SOURCES.items()
        },
        # 워크플로우 빌더의 샘플러/스케줄러 드롭다운용 — 설치된 ComfyUI 버전이 실제로
        # 지원하는 값만 고르게 한다(버전마다 목록이 조금씩 다르다).
        "samplers": combo_choices(object_info, "KSampler", "sampler_name"),
        "schedulers": combo_choices(object_info, "KSampler", "scheduler"),
    }


@app.get("/api/lora-triggers")
def get_lora_triggers():
    return model_registry.lora_triggers()


@app.get("/api/models/inventory")
async def model_inventory(request: Request, refresh: bool = False):
    """내 ComfyUI 파드마다 설치된 모델 목록 — 파드 간 비교용. 꺼져 있는 파드는 connected=false."""
    user = me(request)
    pods = [p for p in visible_pods_for(user) if p.get("kind") == pod_registry.DEFAULT_KIND]

    def one(pod):
        entry = {"id": pod["id"], "name": pod.get("name") or pod["id"], "enabled": bool(pod.get("enabled")),
                 "connected": False, "models": {}}
        if not pod.get("enabled"):
            return entry
        try:
            _, info = fetch_comfy_object_info(refresh, pod)
        except Exception:
            info = None
        if info is not None:
            entry["connected"] = True
            entry["models"] = {k: combo_choices(info, *src) for k, src in MODEL_LIST_SOURCES.items()}
        return entry

    results = await asyncio.gather(*[asyncio.to_thread(one, p) for p in pods])
    return {"pods": list(results)}


@app.get("/api/models/usage")
def model_usage_api(request: Request):
    """모델별 사용 통계 — 내 결과물(관리자는 전체)에 박힌 메타를 세어서 돌려준다."""
    _sync_assets_quietly()
    return {"usage": asset_meta.model_usage(owner_id=auth.owner_scope(me(request)))}


