# 회원 워커 자동 켜기 — charchat처럼 연동 앱이 회원 대신 보낸 작업이 대기하면, 그 회원의 RunPod 키로 그 회원의 워커에 파드를 만든다.
# 2026-11-15 사용자 결정: 각자 자기 계정으로 생성한다. 워커 기록은 키를 저장할 때(꺼진 채) 만들어 두고(ensure_member_worker),
# 파드는 작업이 대기할 때 바로 켜며(기다리는 작가가 있으니 개수를 모으지 않는다), 끄기는 기존 자동 끄기(07-pods, 15분)가 파드를 지운다.
# 안전장치: 회원·등급마다 하루 3번, 실패하면 10→30→60분 뒤 다시. 실패 이유(잔액 부족 등)는 /api/auth/runpod-status로 보여 준다.
# 대상은 연동 앱이 보낸 작업(job.via_service)뿐 — nightshift를 직접 쓰는 회원은 워커를 직접 관리한다. 관리자는 07c(대기열 자동 켜기)가 맡는다.
MEMBER_AUTO_DAILY_LIMIT = 3
MEMBER_AUTO_BACKOFF_MIN = (10, 30, 60)
MEMBER_AUTO_OFF_MINUTES = 15
_member_auto_lock = threading.Lock()


def _member_auto_path():
    from data_paths import DATA_DIR
    return DATA_DIR / "member_auto_power.json"


def _member_auto_load() -> dict:
    try:
        data = json.loads(_member_auto_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _member_auto_save(data: dict) -> None:
    path = _member_auto_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _member_tier_of(job: dict) -> str:
    return "video" if job.get("runpod_tier") == "video" else "image"


def _member_workers(owner_id: int, tier: str) -> list[dict]:
    """자동으로 켜고 끌 수 있는 그 회원의 RunPod 워커(nightshift가 만든 그 등급). 손으로 주소를 넣은 워커는 건드리지 않는다."""
    return [p for p in pod_registry.list_pods(owner_id)
            if p.get("kind") == pod_registry.DEFAULT_KIND and p.get("runpod_tier") == tier]


def _member_running(owner_id: int, tier: str) -> bool:
    """그 등급 작업을 받을 워커가 이미 켜져 있거나 켜는 중인가 — 이미지는 영상 워커를 뺀 모든 ComfyUI 워커(손으로 넣은 것 포함)."""
    def fits(p):
        return (p.get("runpod_tier") == "video") == (tier == "video")
    return any(p.get("kind") == pod_registry.DEFAULT_KIND and fits(p) and (p.get("enabled") or p.get("note") == "runpod:creating")
               for p in pod_registry.list_pods(owner_id))


def ensure_member_worker(owner_id: int, tier: str = "image") -> dict | None:
    """그 회원의 그 등급 RunPod 워커 기록(꺼진 채, 주소 없음)이 없으면 만든다. 파드는 만들지 않는다(돈이 들지 않는다)."""
    user = auth.get_user(owner_id)
    if user is None or user["role"] == auth.ROLE_ADMIN or service_tokens.is_service_user(owner_id):
        return None
    if not runpod_api.key_for_owner(owner_id):
        return None
    existing = _member_workers(owner_id, tier)
    if existing:
        return existing[0]
    pod = pod_registry.create_pod({"kind": pod_registry.DEFAULT_KIND, "url": "", "name": "", "enabled": False,
                                   "pull_outputs": True, "auto_install_models": True, "auto_power_off": True,
                                   "auto_off_minutes": MEMBER_AUTO_OFF_MINUTES, "tags": ["runpod", "auto"],
                                   "note": "runpod:off", "owner_id": owner_id, "runpod_tier": tier})
    ensure_runtime(pod)
    return pod


def _friendly_runpod_error(err: str) -> str:
    if "HTTP 401" in err:
        return "RunPod 키가 올바르지 않거나 권한이 없어요. 키를 다시 확인해 주세요."
    if "HTTP 402" in err:
        return "RunPod 잔액이 부족해요. runpod.io에서 충전하면 다시 시도해요."
    if "재고 없음" in err or "빈 GPU가 없어요" in err:
        return "지금 RunPod에 빈 GPU가 없어요. 잠시 뒤 다시 시도해요."
    return err[:300]


def _member_auto_power_check(now: datetime | None = None) -> dict:
    """대기 중인 연동 작업이 있는 회원마다(등급별) 꺼진 자기 워커를 켠다. {f"{회원}:{등급}": 결과}."""
    now = now or datetime.now(timezone.utc)
    with lock:
        waiting = [j for j in jobs.values() if j["status"] == "queued" and not j.get("pod_id") and not j.get("deleted")
                   and j.get("via_service") and j.get("owner_id") is not None and j.get("billing") != "admin"]
    groups: dict[tuple[int, str], int] = {}
    for job in waiting:
        groups[(job["owner_id"], _member_tier_of(job))] = groups.get((job["owner_id"], _member_tier_of(job)), 0) + 1
    if not groups:
        return {}
    results = {}
    log = logging.getLogger("uvicorn.error")
    with _member_auto_lock:
        data = _member_auto_load()
        today = now.astimezone().date()
        for (owner, tier), count in groups.items():
            key = f"{owner}:{tier}"
            user = auth.get_user(owner)
            if user is None or user["role"] == auth.ROLE_ADMIN or not runpod_api.key_for_owner(owner):
                continue   # 관리자는 07c, 키가 없는 회원은 키를 넣을 때까지 기다린다
            state = data.setdefault(str(owner), {}).setdefault(tier, {"starts": [], "failures": 0, "retry_after": None, "last_error": None})
            workers = _member_workers(owner, tier)
            if _member_running(owner, tier):
                results[key] = "on"
                continue
            starts_today = sum(1 for s in state["starts"] if datetime.fromisoformat(s).astimezone().date() == today)
            if starts_today >= MEMBER_AUTO_DAILY_LIMIT:
                results[key] = "limit"
                continue
            if state.get("retry_after") and now < datetime.fromisoformat(state["retry_after"]):
                results[key] = "backoff"
                continue
            pod = next((p for p in workers if not p.get("enabled")), None) or ensure_member_worker(owner, tier)
            if pod is None:
                continue
            err = _auto_power_start(pod, tier)
            if err:
                state["failures"] = state.get("failures", 0) + 1
                minutes = MEMBER_AUTO_BACKOFF_MIN[min(state["failures"], len(MEMBER_AUTO_BACKOFF_MIN)) - 1]
                state["retry_after"] = (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")
                state["last_error"] = _friendly_runpod_error(err)
                log.warning("회원 워커 자동 켜기 실패 — 회원 %s(%s): %s (%d분 뒤 다시)", owner, tier, err, minutes)
                results[key] = "failed"
            else:
                state["starts"] = [s for s in state["starts"] if datetime.fromisoformat(s).astimezone().date() == today]
                state["starts"].append(now.isoformat(timespec="seconds"))
                state.update(failures=0, retry_after=None, last_error=None)
                log.info("회원 워커 자동 켜기 — 회원 %s(%s): 대기 작업 %d개", owner, tier, count)
                results[key] = "started"
        _member_auto_save(data)
    return results


def member_runpod_status(owner_id: int) -> dict:
    """회원 화면용(charchat Settings → RunPod): 키 여부, 등급별 워커 상태, 대기 작업 수, 자동 켜기 실패 이유."""
    with lock:
        waiting = [j for j in jobs.values() if j["status"] == "queued" and not j.get("pod_id") and not j.get("deleted")
                   and j.get("owner_id") == owner_id]
    state = _member_auto_load().get(str(owner_id), {})
    tiers = {}
    for tier in ("image", "video"):
        workers = _member_workers(owner_id, tier)
        s = state.get(tier, {})
        tiers[tier] = {
            "worker": bool(workers),
            "on": any(p.get("enabled") for p in workers),
            "starting": any(p.get("note") == "runpod:creating" for p in workers),
            "waiting": sum(1 for j in waiting if _member_tier_of(j) == tier),
            "last_error": s.get("last_error"),
            "retry_after": s.get("retry_after"),
        }
    return {"has_key": bool(runpod_api.key_for_owner(owner_id)), "daily_limit": MEMBER_AUTO_DAILY_LIMIT,
            "auto_off_minutes": MEMBER_AUTO_OFF_MINUTES, "tiers": tiers}


@app.get("/api/generate/billing")
def generate_billing(request: Request):
    """연동 앱(charchat)이 회원 대신 묻는 읽기 전용 값 둘 — 그 회원이 자기 RunPod 키를 넣었는지(own_runpod), 유료 회원인지(paid).
    charchat은 '키 있음 + 무료'인 회원에게만 하루 삽화 횟수를 스스로 정하게 한다(그 그림은 회원 자기 RunPod에서 돈다).
    키 자체나 한도·사용량은 주지 않는다(서비스 토큰 허용 경로)."""
    user = me(request)
    return {"own_runpod": bool(runpod_api.key_for_owner(user["id"])), "paid": paid_plan.plan(user["id"])["paid"]}


@app.get("/api/auth/runpod-status")
def auth_runpod_status(request: Request):
    user = me(request)
    return member_runpod_status(user["id"]) | {"plan": paid_plan.plan(user["id"])}


def _paid_promote_waiting() -> int:
    """대기 중인 연동 작업 중 아직 관리자 파드로 정해지지 않은 것을 다시 본다 — 회원이 유료가 됐거나 날이 바뀌어
    한도가 생겼으면 관리자 파드로 예약한다. 이미 회원 워커에 배정된 작업은 건드리지 않는다. 바꾼 개수."""
    with lock:
        candidates = [j for j in jobs.values() if j["status"] == "queued" and not j.get("pod_id") and not j.get("deleted")
                      and j.get("via_service") and j.get("billing") != "admin" and j.get("owner_id") is not None]
    moved = 0
    for job in sorted(candidates, key=lambda j: j["queued_at"]):
        kind, units = paid_plan.job_kind_units(job)
        if not paid_plan.reserve(job["owner_id"], job["id"], kind, units):
            continue
        with lock:
            if job["status"] == "queued" and not job.get("pod_id") and not job.get("deleted"):
                job["billing"] = "admin"
                job["waiting_reason"] = None
                moved += 1
            else:
                paid_plan.release(job["id"])
    if moved:
        save_state()
        poke_scheduler()
    return moved
