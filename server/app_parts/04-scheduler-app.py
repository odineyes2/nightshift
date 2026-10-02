# app.py가 app_parts/*.py를 번호 순서대로 한 네임스페이스에서 실행한다 — 앞 파일의 이름을 import 없이 쓴다.
# ---- 대기 큐 스케줄러 -----------------------------------------------------------------
# 작업은 파드를 정하지 않고 먼저 "대기 큐"(status=queued, pod_id=None)에 들어간다. 이 스케줄러가 주기적으로
# 그 큐를 훑어서, 작업을 돌릴 수 있는 파드가 생기면 그 파드로 배정한다. "돌릴 수 있다"는 다음을 전부 만족하는 것:
#   1) 그 회원의 파드이고 사용 중(enabled)이며 작업 템플릿이 쓰는 파드 종류와 맞는다(고정 파드가 있으면 그 파드만),
#   2) 지금 연결돼 있다,
#   3) 동시에 돌릴 자리가 남아 있다,
#   4) 작업이 필요로 하는 노드/모델 파일이 그 파드에 설치돼 있다.
# 어느 것도 못 채우면 작업은 대기 큐에 그대로 남고, 파드마다 왜 안 되는지를 waiting_reason에 적어 화면에 보여 준다.
# 파드가 하나도 없어도 작업을 만들어 둘 수 있다 — 파드가 생기고 살아나면 그때 돈다.
SCHED_INTERVAL_SEC = float(os.environ.get("NIGHTSHIFT_SCHED_INTERVAL_SEC", "5"))
MODEL_FILE_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx")
_sched_wake = threading.Event()

# 0(기본)이면 꺼짐 — RunPod pod 자동 동기화(runpod_sync.py)를 사람이 MCP로 부르지
# 않아도 이 간격마다 스스로 돌게 한다. 관리자 권한으로 돈다(그 계정 소유로 파드가
# 등록된다) — 자동화라 특정 사람이 부른 게 아니므로.
RUNPOD_SYNC_INTERVAL_SEC = float(os.environ.get("RUNPOD_SYNC_INTERVAL_SEC", "0"))
# 사용 내역(DB 탭 RunPod 세션)만 맞추는 가벼운 주기(초, 기본 5분). 0이면 끔. 위 전체 동기화가 켜져 있으면
# 그쪽이 같이 기록하므로 따로 돌리지 않는다(NS-2 — 전체 동기화가 꺼져 있어 기록이 멈췄었다).
RUNPOD_SESSION_LOG_SEC = float(os.environ.get("RUNPOD_SESSION_LOG_SEC", "300"))
# "작업이 끝나면 자동으로 끄기"(NS-8) — 이 간격(초)마다 보고, 마지막 작업이 끝난 뒤 워커의 auto_off_minutes(NS-16) 동안
# 새 작업이 없으면 끈다. 0이면 검사 자체를 끈다.
RUNPOD_AUTO_OFF_CHECK_SEC = float(os.environ.get("RUNPOD_AUTO_OFF_CHECK_SEC", "60"))
# 부팅 감시(NS-18) — nightshift가 만든 RunPod 파드가 켠 지 이만큼(분) 지나도 ComfyUI가 응답하지 않으면 한 번 다시 만들고,
# 또 그러면 지운다. 위 간격마다 본다(자동 끄기와 같은 주기).
RUNPOD_BOOT_TIMEOUT_MIN = float(os.environ.get("RUNPOD_BOOT_TIMEOUT_MIN", "15"))


def poke_scheduler():
    _sched_wake.set()


def job_missing_on_pod(job: dict, pod: dict) -> list[str] | None:
    """이 작업이 그 파드에서 돌기 위해 없는 것들(없는 노드 · 없는 모델 파일). 비어 있으면 갖춘 것,
    None이면 확인 못 함(파드가 목록을 안 준다). ComfyUI가 아닌 파드는 검사할 게 없다.

    워크플로우 안의 값 중 **모델 파일**만 본다 — 입력 이미지처럼 실행 때 스크립트가 채우는 값까지 검사하면
    멀쩡히 도는 작업이 영영 못 도는 쪽으로 막히기 때문이다. 템플릿 옵션(체크포인트/LoRA 드롭다운)으로 덮어쓰는
    종류는 워크플로우에 적힌 값을 무시하고 고른 값 자체를 본다."""
    if pod.get("kind") != pod_registry.DEFAULT_KIND:
        return []
    try:
        _, info = fetch_comfy_object_info(False, pod)
    except Exception:
        return None
    if info is None:
        return None
    template = load_templates_map().get(job.get("template_id")) or {}
    problems: list[str] = []
    overridden: set[tuple[str, str]] = set()
    for option in template.get("options", []):
        if option.get("type") != "comfy_model":
            continue
        value = str((job.get("options") or {}).get(option["name"]) or "").strip()
        source = MODEL_LIST_SOURCES.get(option.get("model_kind"))
        if not value or source is None:
            continue
        overridden.add(source)
        installed = combo_spec(info, *source)   # 로더는 있는데 목록이 비었으면(새 파드) 그것도 "없음"
        if installed is not None and value not in installed:
            problems.append(value)
    blobs: list[bytes | None] = []
    for field in ("workflow_filename", "video_workflow_filename"):
        name = job.get(field)
        if name:
            try:
                blobs.append((JOBS_DIR / name).read_bytes())
            except OSError:
                pass
    if not job.get("video_workflow_filename"):
        blobs.append(default_video_workflow_bytes(template))
    for data in blobs:
        if not data:
            continue
        try:
            workflow = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(workflow, dict):
            continue
        nodes, values, _ = workflow_missing(info, workflow)
        problems.extend(f"노드 {n}" for n in nodes)
        for v in values:
            if (v["class_type"], v["field"]) in overridden:
                continue
            if not str(v["value"]).lower().endswith(MODEL_FILE_EXTS):
                continue
            problems.append(v["value"])
    return list(dict.fromkeys(problems))


def _short_list(items: list[str], limit: int = 3) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" 외 {len(items) - limit}개" if len(items) > limit else "")


def _snapshot_pod_into_job(job: dict, pod: dict) -> None:
    """파드는 일시적이라 그 작업이 어디서 돌았나/얼마 들었나를 job에 찍어 둔다(lock 밖에서 부른다)."""
    gpu = cost = None
    try:
        info = runpod_api.get_runpod_info(pod.get("url") or "")
        if info:
            gpu = info.get("gpu_type")
            c = info.get("cost_per_hr")
            cost = float(c) if isinstance(c, (int, float)) else None
    except Exception:
        pass
    job["pod_name"] = pod.get("name")
    job["pod_kind"] = pod.get("kind")
    job["pod_gpu"] = gpu
    job["pod_cost_per_hr"] = cost


def unassign_job(job_id: str, reason: str) -> None:
    """배정됐던 작업을 대기 큐로 되돌린다(파드가 끊겼거나 필요한 모델이 없어졌을 때)."""
    with lock:
        job = jobs.get(job_id)
        if job is None or job.get("deleted") or job["status"] not in ("queued", "pending"):
            return
        job["status"] = "queued"
        job["pod_id"] = None
        job["auto_assigned"] = False
        job["waiting_reason"] = reason
        set_comfy_wait_flag(job, False)
    save_state()
    poke_scheduler()


def schedule_once() -> None:
    with lock:
        waiting = sorted((j for j in jobs.values()
                          if j["status"] == "queued" and not j.get("pod_id") and not j.get("deleted")),
                         key=lambda j: j["queued_at"])
    if not waiting:
        return
    templates = load_templates_map()
    owners = {j.get("owner_id") for j in waiting}
    pods_of = {o: [p for p in pod_registry.list_pods(o) if p.get("enabled")] for o in owners}

    # 파드마다 연결 확인은 한 번만 — 파드가 꺼져 있으면 확인이 몇 초 걸릴 수 있어 나란히 돌린다.
    all_pods = {p["id"]: p for plist in pods_of.values() for p in plist}
    connected: dict[str, bool] = {}
    if all_pods:
        def probe(pod):
            try:
                return pod["id"], bool(driver_for(pod).health(pod)["ok"])
            except Exception:
                return pod["id"], False
        with ThreadPoolExecutor(max_workers=min(8, len(all_pods))) as ex:
            connected = dict(ex.map(probe, all_pods.values()))

    free: dict[str, int] = {}
    for pid, pod in all_pods.items():
        rt = ensure_runtime(pod)
        with lock:
            free[pid] = rt.max_concurrent - (len(rt.running) + rt.queue.qsize())

    for job in waiting:
        template = templates.get(job.get("template_id")) or {}
        allowed = template.get("pod_kinds") or [pod_registry.DEFAULT_KIND]
        pinned = job.get("pinned_pod_id")
        candidates = [p for p in pods_of.get(job.get("owner_id"), [])
                      if p.get("kind") in allowed and (not pinned or p["id"] == pinned)]
        reasons: list[str] = []
        chosen = None
        # 연결돼 있고 자리도 있는데 모델만 없는 파드 중 가장 적게 모자란 곳 — 카드의 "조치 필요"와
        # "없는 모델 받기"(POST /api/jobs/{id}/fetch-missing)가 이 값을 쓴다.
        best_missing: tuple[dict, list[str]] | None = None
        if not candidates:
            if pinned:
                reasons.append("지정한 워커를 쓸 수 없어요(없어졌거나 사용 안 함)")
            else:
                reasons.append("사용할 수 있는 워커가 없어요 — 워커를 추가하거나 켜 주세요")
        for pod in sorted(candidates, key=lambda p: -free.get(p["id"], 0)):
            name = pod.get("name") or pod["id"]
            if not connected.get(pod["id"]):
                reasons.append(f"'{name}': 연결 안 됨")
                continue
            if free.get(pod["id"], 0) <= 0:
                reasons.append(f"'{name}': 다른 작업이 돌고 있어요")
                continue
            missing = job_missing_on_pod(job, pod)
            if missing is None:
                reasons.append(f"'{name}': 설치된 모델 목록을 못 읽었어요")
                continue
            if missing:
                reasons.append(f"'{name}': 없는 것 — {_short_list(missing)}")
                if best_missing is None or len(missing) < len(best_missing[1]):
                    best_missing = (pod, missing)
                continue
            chosen = pod
            break
        if chosen is None:
            reason = " · ".join(reasons)
            missing_info = ({"pod_id": best_missing[0]["id"], "pod_name": best_missing[0].get("name") or best_missing[0]["id"],
                             "names": best_missing[1]} if best_missing else None)
            if best_missing and best_missing[0].get("auto_install_models"):
                _maybe_auto_fetch(job["id"], best_missing[0], best_missing[1])   # 13-jobs-api.py
            with lock:
                if job.get("waiting_reason") == reason and job.get("missing_models") == missing_info:
                    continue
                job["waiting_reason"] = reason
                job["missing_models"] = missing_info
            save_state()
            continue
        _snapshot_pod_into_job(job, chosen)
        with lock:
            if job["status"] != "queued" or job.get("pod_id") or job.get("deleted"):
                continue   # 그 사이 사용자가 지웠거나 옮겼다
            job["pod_id"] = chosen["id"]
            job["auto_assigned"] = True
            job["waiting_reason"] = None
            job["missing_models"] = None
            job["fetching_models"] = None
            job["fetch_error"] = None
            job["preflight"] = None
            free[chosen["id"]] -= 1
        save_state()
        dispatch_job(job["id"], chosen["id"])


def scheduler_loop():
    while True:
        _sched_wake.wait(SCHED_INTERVAL_SEC)
        _sched_wake.clear()
        try:
            schedule_once()
        except Exception:
            logging.getLogger("uvicorn.error").exception("스케줄러 오류")


def dispatch_job(job_id: str, pod_id: str | None = None):
    """작업을 그 작업이 배정된 파드의 큐에 넣는다. 파드가 없거나 꺼져 있으면 기본
    파드로 되돌린다 — 큐에 못 들어가 영영 안 도는 작업이 생기면 안 되므로."""
    with lock:
        job = jobs.get(job_id)
        if job is None or job.get("deleted"):
            return
        target = pod_id or job.get("pod_id")
    pod = pod_registry.get_pod(target) if target else None
    if pod is None:
        with lock:
            owner = jobs[job_id].get("owner_id")
        pod = pod_registry.default_pod_for(owner)   # 같은 주인의 파드로만 되돌린다
        if pod is None:
            with lock:
                jobs[job_id]["status"] = "pending"
            save_state()
            return
    with lock:
        jobs[job_id]["pod_id"] = pod["id"]
        rt = pod_runtimes.get(pod["id"])
    if rt is None:
        rt = ensure_runtime(pod)
    rt.queue.put(job_id)


def pod_worker_loop(pod_id: str):
    """파드 하나를 담당하는 워커 스레드. 파드마다 max_concurrent개가 돈다.

    예전에는 이 루프가 프로세스 전체에 하나뿐이었다(전역 job_queue). 이제 큐도 자동
    실행 플래그도 파드별이라, 파드 하나가 멈춰도 다른 파드는 그대로 돈다."""
    while True:
        with lock:
            rt = pod_runtimes.get(pod_id)
            if rt is None or rt.closed:
                return
            q = rt.queue
        try:
            job_id = q.get(timeout=1.0)
        except queue.Empty:
            continue

        try:
            _run_one_job(pod_id, job_id)
        finally:
            q.task_done()


def _run_one_job(pod_id: str, job_id: str):
    # 파드에 연결될 때까지 붙잡아 둔다 — 작업은 "queued"인 채 waiting_for_comfy
    # 표시만 붙고, 연결되는 순간 이어서 실행된다.
    # 큐에 들어간 뒤 다른 파드로 옮겨졌을 수 있다. 그때는 여기서 **그냥 버린다** —
    # 다시 배차하지 않는다. 파드를 바꾼 쪽(POST /api/jobs/{id}/move)이 새 파드 큐에
    # 이미 넣었기 때문에, 여기서 또 넣으면 같은 작업이 두 번 돈다. 파드가 통째로
    # 지워진 경우도 마찬가지로 그쪽에서 pending으로 되돌려 두므로 버리면 된다.
    action, comfy_url = wait_for_pod(pod_id, job_id)
    if action != "run" or comfy_url is None:
        return

    pod = pod_registry.get_pod(pod_id)
    if pod is None:
        return

    # 시작하기 직전에 한 번 더 — 배정된 뒤 큐에서 기다리는 사이 모델이 사라졌거나, 파드를 손으로 지정해서 넣은
    # 작업이라 스케줄러 검사를 거치지 않았을 수 있다. 갖춰지지 않았으면 돌리지 않고 대기 큐로 되돌린다.
    with lock:
        snapshot = dict(jobs.get(job_id) or {})
    if snapshot and not snapshot.get("deleted") and snapshot.get("pod_id") == pod_id:
        missing = job_missing_on_pod(snapshot, pod)
        if missing:
            unassign_job(job_id, f"'{pod.get('name') or pod_id}': 없는 것 — {_short_list(missing)}")
            return

    with lock:
        job = jobs.get(job_id)
        if job is None or job.get("deleted") or job.get("pod_id") != pod_id:
            return
        job.pop("waiting_reason", None)
        job["status"] = "running"
        job["started_at"] = now_iso()
        job["progress"] = None
    save_state()

    log_path = LOGS_DIR / f"{job_id}.log"
    script_path = TEMPLATES_DIR / job["script_filename"]

    # 파드 종류마다 작업에 실어 보낼 환경변수가 다르다(ComfyUI는 COMFY_URL).
    # JOB_ID/NIGHTSHIFT_URL은 워커 종류와 무관하게 항상 필요한 것들이다.
    extra_env = {"JOB_ID": job_id, "NIGHTSHIFT_URL": SELF_URL,
                 **driver_for(pod).job_env(pod, comfy_url)}
    if job.get("workflow_filename"):
        extra_env["WORKFLOW_PATH"] = str((JOBS_DIR / job["workflow_filename"]).resolve())
    if job.get("video_workflow_filename"):
        extra_env["VIDEO_WORKFLOW_UPLOAD_PATH"] = str((JOBS_DIR / job["video_workflow_filename"]).resolve())
    if job.get("csv_filename"):
        extra_env["CSV_PATH"] = str((JOBS_DIR / job["csv_filename"]).resolve())
    for name, value in job.get("options", {}).items():
        extra_env[name.upper()] = str(value)
    extra_env.update(ref_assets_job_env(job.get("owner_id")))
    env = {**os.environ, **extra_env}

    stopped = False
    with open(log_path, "w") as logf:
        try:
            proc = subprocess.Popen(
                [sys.executable, "-u", str(script_path)],
                stdout=logf,
                stderr=subprocess.STDOUT,
                env=env,
            )
            with lock:
                rt = pod_runtimes.get(pod_id)
                if rt is not None:
                    rt.running[job_id] = proc
            returncode = proc.wait()
        except Exception as e:
            logf.write(f"\n[runner error] {e}\n")
            returncode = -1
        finally:
            with lock:
                rt = pod_runtimes.get(pod_id)
                if rt is not None:
                    rt.running.pop(job_id, None)
                stopped = bool(job.pop("_stop_requested", False))

    with lock:
        job["status"] = "interrupted" if stopped else ("done" if returncode == 0 else "failed")
        job["returncode"] = returncode
        job["finished_at"] = now_iso()
    save_state()

    # 워커가 원격이면 결과물은 그쪽 디스크에만 있다 — 갤러리/zip/이메일은 전부 로컬
    # 출력 폴더를 읽으므로, 작업이 끝난 직후 그 작업 몫만 끌어온다. 중간에 실패했더라도
    # 그때까지 나온 이미지는 가져온다(returncode를 안 본다).
    pull_job_outputs(pod, job_id, comfy_url, log_path)
    # 방금 끝난 작업의 결과물을 색인에 넣는다(그 job의 프로젝트를 물려받는다).
    _sync_assets_quietly(force=True)


def ref_assets_job_env(owner_id) -> dict[str, str]:
    """일반 회원의 작업이 그 회원의 참조·입력 이미지 폴더만 쓰게 하는 환경변수."""
    owner = auth.get_user(owner_id) if owner_id is not None else None
    if owner is None or auth.is_admin(owner):
        return {}
    return job_env_for_owner(owner_id)


def ensure_runtime(pod: dict) -> PodRuntime:
    """그 파드의 실행 상태와 워커 스레드를 준비한다(이미 있으면 그대로 쓴다).
    max_concurrent를 늘렸으면 모자란 만큼 스레드를 더 띄운다."""
    pod_id = pod["id"]
    with lock:
        rt = pod_runtimes.get(pod_id)
        if rt is None:
            rt = PodRuntime(pod_id, pod.get("max_concurrent", 1))
            pod_runtimes[pod_id] = rt
        rt.closed = False
        rt.max_concurrent = max(1, int(pod.get("max_concurrent") or 1))
        missing = rt.max_concurrent - rt.workers
        rt.workers += max(0, missing)
    for _ in range(max(0, missing)):
        threading.Thread(target=pod_worker_loop, args=(pod_id,), daemon=True).start()
    return rt


def sync_runtimes():
    """레지스트리에 있는 파드마다 런타임을 준비하고, 사라진 파드의 런타임은 닫는다."""
    pods = pod_registry.list_pods()
    for pod in pods:
        ensure_runtime(pod)
    alive = {p["id"] for p in pods}
    with lock:
        gone = [pid for pid in pod_runtimes if pid not in alive]
        for pid in gone:
            pod_runtimes[pid].closed = True
            del pod_runtimes[pid]


def _sync_assets_quietly(force: bool = False):
    # 색인은 부가 기능이라 실패해도 갤러리/작업 흐름을 막지 않는다.
    try:
        assets_index.sync(force=force)
    except Exception:
        logging.exception("결과물 색인(assets) 동기화에 실패했어요")


def _sync_key_mapper(item: dict, pod_id: str | None) -> str | None:
    """원격에서 받은 항목의 로컬 경로. nightshift 작업 폴더(job_id)는 그 작업의 주인의 파드에서만 받고, 그 밖의
    것(ComfyUI에서 직접 돌린 결과)은 파드 주인별 폴더(u<id>/)로 나눠 담아 회원끼리 섞이지도 덮어쓰지도 않게 한다."""
    pod = pod_registry.get_pod(pod_id) if pod_id else None
    owner = pod.get("owner_id") if pod else None
    subfolder = item.get("subfolder") or ""
    with lock:
        job = jobs.get(subfolder) if subfolder else None
    if job is not None:
        return item["key"] if job.get("owner_id") == owner else None   # 남의 작업 폴더에는 쓰지 않는다
    return f"u{owner}/{item['key']}" if owner is not None else item["key"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    comfy_outputs.set_key_mapper(_sync_key_mapper)
    load_state()
    admin = auth.ensure_admin()
    if admin is not None:
        pod_registry.load()
        pod_registry.assign_orphans(admin["id"])
        with lock:
            for job in jobs.values():
                if job.get("owner_id") is None:
                    job["owner_id"] = admin["id"]
        save_state()
    recent_workflows_store.load()
    recent_csvs_store.load()
    load_danbooru_state()
    load_lora_triggers()
    pod_registry.load()
    prune_thumb_cache()  # 축소본 캐시가 상한을 넘었으면 오래된 것부터 정리(14-output-images)
    # 재시작 전에 running/queued 상태로 남아있던 기록은 재실행되지 않으므로 상태만 정리.
    # 파드 연결을 기다리던 중이었다는 표시(waiting_for_comfy)도 함께 지운다 — 그
    # 대기는 워커 스레드 안에서만 살아 있는 상태라 재시작하면 남아 있을 이유가 없다.
    # pod_id가 없는 옛 작업 기록은 기본 파드 것으로 본다(다중 파드 이전에 만들어진 것).
    # 실행 중이던 작업은 중단으로 표시하지만, 아직 시작 안 하고 큐에서 기다리던 작업은 살려 둔다 — 파드가 살아나기를
    # 기다리는 게 이제 대기 큐의 정상 동작이라, 서버 재시작이 그 줄을 없애면 안 된다. 어느 파드 큐(메모리)에
    # 들어가 있던 것은 그 큐가 사라졌으니 대기 큐로 되돌려 스케줄러가 다시 배정하게 한다.
    try:
        default_id = pod_registry.default_pod()["id"]
    except Exception:
        default_id = None   # 파드가 하나도 없다
    with lock:
        for job in jobs.values():
            if job["status"] == "running":
                job["status"] = "interrupted"
            elif job["status"] == "queued" and not job.get("deleted"):
                job["pod_id"] = None
                job["auto_assigned"] = False
            elif "pod_id" not in job:
                job["pod_id"] = default_id
            set_comfy_wait_flag(job, False)
    save_state()
    sync_runtimes()
    threading.Thread(target=scheduler_loop, daemon=True, name="scheduler").start()
    poke_scheduler()
    if RUNPOD_AUTO_OFF_CHECK_SEC > 0 and runpod_api.RUNPOD_API_KEY:
        threading.Thread(target=_auto_power_off_loop, daemon=True, name="runpod-auto-off").start()   # NS-8
        if RUNPOD_BOOT_TIMEOUT_MIN > 0:
            threading.Thread(target=_boot_watch_loop, daemon=True, name="runpod-boot-watch").start()   # NS-18
    if RUNPOD_SYNC_INTERVAL_SEC > 0:
        threading.Thread(target=_runpod_sync_loop, daemon=True, name="runpod-sync").start()
    elif RUNPOD_SESSION_LOG_SEC > 0 and runpod_api.RUNPOD_API_KEY:
        threading.Thread(target=_log_runpod_sessions_quietly, daemon=True).start()   # 켤 때 한 번 — 놓친 기록을 바로 채운다
        threading.Thread(target=_runpod_session_log_loop, daemon=True, name="runpod-session-log").start()
    # 결과물 색인(assets)을 디스크와 맞춘다 — 파일이 많으면 시간이 걸릴 수 있으니
    # 서버가 뜨는 걸 붙잡지 않게 뒤에서 돌린다.
    threading.Thread(target=_sync_assets_quietly, kwargs={"force": True}, daemon=True).start()
    yield


app = FastAPI(title="RunPod Job Queue", lifespan=lifespan)

