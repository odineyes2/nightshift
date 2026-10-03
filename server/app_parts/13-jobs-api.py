# app.py가 app_parts/*.py를 번호 순서대로 한 네임스페이스에서 실행한다 — 앞 파일의 이름을 import 없이 쓴다.
@app.post("/api/queue/start")
def start_queue(request: Request, project_id: str | None = None):
    # 자동 실행 모드를 켠다 — 지금 대기 중인 작업을 전부 큐에 넣는 것은 물론,
    # 켜져 있는 동안 POST /api/upload로 새로 추가되는 작업도 계속 이어서 큐에
    # 들어간다. "⏸ 정지"를 누르기 전까지는 계속 켜져 있다.
    # 파드가 여러 개면 **사용 중인 파드 전부**를 켠다(화면의 "▶ 시작" 버튼 하나가
    # 전체를 켜는 것과 같다). 하나만 켜고 끄려면 /api/pods/{id}/queue/start를 쓴다.
    user = me(request)
    wanted_project = None
    if project_id is not None:
        wanted_project = "unassigned" if project_id == "unassigned" else parse_project_id(project_id)
    pod_ids = [p["id"] for p in pod_registry.list_pods(user["id"]) if p.get("enabled")]   # 내 파드만
    started = start_pods(pod_ids) if project_id is None else 0
    # 파드를 안 정한 채 쌓아 둔 작업은 대기 큐로 보낸다 — 스케줄러가 필요한 모델을 갖춘 파드가 살아 있을 때 배정한다.
    with lock:
        sent = [j for j in jobs.values() if j["status"] == "pending" and not j.get("deleted")
                and not j.get("pod_id") and j.get("owner_id") == user["id"]
                and (wanted_project is None
                     or j.get("project_id") == (None if wanted_project == "unassigned" else wanted_project))]
        for job in sent:
            job["status"] = "queued"
            job["waiting_reason"] = "워커를 찾는 중이에요"
    if sent:
        save_state()
    poke_scheduler()
    return {"running": True, "started": started + len(sent), "pods": pod_ids}


@app.post("/api/queue/stop")
def stop_queue(request: Request, project_id: str | None = None):
    # 자동 실행 모드를 끈다 — 이후 새로 추가되는 작업은 다시 "▶ 시작"을 누르기
    # 전까지 pending 상태로 대기 목록에만 쌓인다. 이미 큐에 들어가 있지만 아직 안 돈
    # 작업(queued)은 그대로 대기 상태로 남고(다음 "▶ 시작" 때 이어서 돎), 지금
    # 실행 중인(running) 작업들은 즉시 종료 요청한다 — 완전히 죽을 때까지 몇 초
    # 걸릴 수 있으니 job 상태가 "interrupted"로 바뀌는 건 GET /api/jobs로 잠시 후
    # 확인해야 한다.
    user = me(request)
    wanted_project = None
    if project_id is not None:
        wanted_project = "unassigned" if project_id == "unassigned" else parse_project_id(project_id)
    my_pod_ids = [p["id"] for p in pod_registry.list_pods(user["id"])] if project_id is None else []   # 내 파드만 멈춘다
    stopped: list[str] = []
    with lock:
        for pid in my_pod_ids:
            rt = pod_runtimes.get(pid)
            if rt is not None:
                rt.auto_run = False
        # 파드를 기다리던 작업은 대기 목록(pending)으로 되돌려 배정이 멈추게 한다 — 다음 "▶ 시작" 때 다시 대기 큐로 간다.
        for job in jobs.values():
            if (job["status"] == "queued" and not job.get("pod_id") and not job.get("deleted")
                    and job.get("owner_id") == user["id"]
                    and (wanted_project is None
                         or job.get("project_id") == (None if wanted_project == "unassigned" else wanted_project))):
                job["status"] = "pending"
                job["waiting_reason"] = None
    save_state()
    for pid in my_pod_ids:
        stopped.extend(stop_pod_jobs(pid))
    return {"running": False, "stopped_job_ids": stopped,
            "stopped_job_id": stopped[0] if stopped else None}


@app.post("/api/pods/{pod_id}/queue/start")
def start_pod_queue(pod_id: str, request: Request):
    """이 파드만 켠다 — 파드가 여러 대일 때 한 대씩 굴리기 위한 것."""
    pod = pod_or_404(me(request), pod_id)
    if not pod.get("enabled"):
        raise HTTPException(400, f"'{pod['name']}' 워커는 지금 사용 안 함 상태예요.")
    ensure_runtime(pod)
    return {"pod_id": pod_id, "running": True, "started": start_pods([pod_id])}


@app.post("/api/pods/{pod_id}/queue/stop")
def stop_pod_queue(pod_id: str, request: Request):
    """이 파드만 멈춘다. 다른 파드는 계속 돈다."""
    pod_or_404(me(request), pod_id)
    with lock:
        rt = pod_runtimes.get(pod_id)
        if rt is not None:
            rt.auto_run = False
    return {"pod_id": pod_id, "running": False, "stopped_job_ids": stop_pod_jobs(pod_id)}


@app.post("/api/jobs/clear-completed")
def clear_completed_jobs(request: Request, pod_id: str | None = None, project_id: str | None = None,
                         done_only: bool = False):
    # 다 끝난 작업(성공/실패/중단)을 목록에서 한꺼번에 치우고 싶을 때 쓴다 —
    # delete_job()과 같은 소프트 삭제다. 보관 한도를 넘는 오래된 설정은 영구 정리된다.
    # done_only는 완료 칸만 청소한다. 생략한 기존 호출은 실패/중단도 정리한다.
    # pending/queued/running은 여기서 건드리지
    # 않는다 — 아직 시작 안 했거나 진행 중인 작업까지 같이 지우면 안 되므로.
    #
    # pod_id를 주면 그 파드의 작업만 치운다. 화면의 작업 목록이 파드 하나 것만
    # 보여주므로("#pod/{id}/jobs"), 거기 있는 "완료 삭제"가 화면에 보이지도 않는
    # 다른 파드의 작업까지 지워버리면 안 된다. 프로젝트 화면의 "완료 삭제"도 같은 이유로
    # project_id를 주면 그 프로젝트 것만("unassigned"면 미분류) 치운다.
    scope = job_scope(me(request))
    wanted_project = None
    if project_id is not None:
        wanted_project = "unassigned" if project_id == "unassigned" else parse_project_id(project_id)
    with lock:
        completed = [j for j in jobs.values()
                     if j["status"] in (("done",) if done_only else ("done", "failed", "interrupted")) and not j.get("deleted")
                     and owned(j, scope)
                     and (pod_id is None or j.get("pod_id") == pod_id)
                     and (wanted_project is None
                          or j.get("project_id") == (None if wanted_project == "unassigned" else wanted_project))]
        now = now_iso()
        for job in completed:
            job["deleted"] = True
            job["deleted_at"] = now
        prune_deleted_jobs()
    save_state()
    return {"cleared": len(completed)}


@app.get("/api/jobs")
def list_jobs(request: Request, project_id: str | None = None):
    # project_id를 주면 그 프로젝트 것만("unassigned"면 미분류) — 안 주면 전부(내 것 전부, admin은 모두의 것).
    user = me(request)
    scope = job_scope(user)
    wanted = None
    if project_id is not None:
        wanted = "unassigned" if project_id == "unassigned" else parse_project_id(project_id)
    names = auth.usernames() if scope is None else {}
    visible_pod_ids = {p["id"] for p in visible_pods_for(user)}
    with lock:
        ordered = sorted(
            (j for j in jobs.values() if not j.get("deleted") and owned(j, scope)
             and (wanted is None or j.get("project_id") == (None if wanted == "unassigned" else wanted))),
            key=lambda j: j["queued_at"],
            reverse=True,
        )
        if scope is None:   # 관리자에게는 누구 작업인지 알려 준다
            ordered = [{**j, "owner_name": names.get(j.get("owner_id"))} for j in ordered]
        # 화면의 "▶ 시작/⏸ 정지" 버튼 하나는 "하나라도 돌고 있으면 켜진 것"으로 본다.
        my_runtimes = {pid: rt for pid, rt in pod_runtimes.items() if pid in visible_pod_ids}
        waiting_count = sum(1 for j in jobs.values() if j["status"] == "queued" and not j.get("pod_id")
                            and not j.get("deleted") and owned(j, scope))
        running = any(rt.auto_run for rt in my_runtimes.values()) or waiting_count > 0
        pending_count = sum(rt.queue.qsize() for rt in my_runtimes.values()) + waiting_count
        per_pod = {
            pid: {"running": rt.auto_run, "pending_count": rt.queue.qsize(),
                  "running_jobs": list(rt.running)}
            for pid, rt in my_runtimes.items()
        }
    return {"jobs": ordered, "pending_count": pending_count, "running": running,
            "pods": per_pod}


@app.get("/api/jobs/deleted")
def list_deleted_jobs(request: Request):
    # "작업 목록"에서 삭제한 작업들 — 상단의 "삭제된 작업 설정 불러오기" 드롭다운을
    # 채우는 용도. 소프트 삭제이므로 워크플로우/CSV는 prune_deleted_jobs()가 지우기
    # 전까지 GET /api/jobs/{id}/workflow·csv로 계속 읽을 수 있다.
    scope = job_scope(me(request))
    with lock:
        ordered = sorted(
            (j for j in jobs.values() if j.get("deleted") and owned(j, scope)),
            key=lambda j: j.get("deleted_at") or "",
            reverse=True,
        )
    return {"jobs": ordered, "retention": DELETED_JOBS_RETENTION}


@app.get("/api/jobs/{job_id}/log")
def job_log(job_id: str, request: Request, tail: int = 200):
    job_or_404(me(request), job_id)
    log_path = LOGS_DIR / f"{job_id}.log"
    if not log_path.exists():
        return {"log": ""}
    lines = log_path.read_text(errors="replace").splitlines()
    return {"log": "\n".join(lines[-tail:])}


@app.get("/api/jobs/{job_id}/text-result")
def job_text_result(job_id: str, request: Request):
    """이미지가 아니라 글을 만드는 워커(claude_writer)의 결과 — 템플릿이 직접
    NIGHTSHIFT_OUTPUT_DIR/<job_id>/output.md에 써 둔 것을 그대로 읽어 돌려준다.
    파드 화면의 "📝 결과" 탭이 이걸 부른다. 파일이 없으면(아직 실행 전/실패)
    빈 문자열 — 로그(job_log)를 보라고 굳이 에러를 내지 않는다."""
    job_or_404(me(request), job_id)
    path = Path(OUTPUT_DIR) / job_id / "output.md"
    if not path.is_file():
        return {"text": ""}
    return {"text": path.read_text(encoding="utf-8", errors="replace")}


@app.put("/api/jobs/{job_id}/progress")
async def update_job_progress(job_id: str, request: Request):
    # 실행 중인 템플릿 스크립트가 자기 진행 상황(전체/완료 이미지 수)을 스스로 보고하는
    # 용도. 매 이미지마다 호출될 수 있어 디스크 쓰기(save_state)는 하지 않고 메모리만 갱신한다
    # — 서버가 재시작되면 어차피 그 작업은 interrupted 처리되어 진행률의 의미가 없어진다.
    # 작업 스크립트(내부 토큰) 아니면 관리자만 — 진행률을 남이 바꿀 이유가 없다.
    if not request.state.internal:
        admin_only(request)
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")

    total = data.get("total")
    done = data.get("done")
    if not isinstance(total, int) or not isinstance(done, int) or total < 0 or done < 0:
        raise HTTPException(400, "total/done은 0 이상의 정수여야 해요.")

    with lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "없는 작업이에요.")
        job["progress"] = {"total": total, "done": done}

    return {"ok": True}


def read_job_attachment(job_id: str, field: str, missing_msg: str, user: dict | None = None) -> str:
    if user is not None:
        job_or_404(user, job_id)
    with lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "없는 작업이에요.")
        filename = job.get(field)

    if not filename:
        raise HTTPException(404, missing_msg)
    path = JOBS_DIR / filename
    if not path.exists():
        raise HTTPException(404, "파일을 찾을 수 없어요.")
    return path.read_text(encoding="utf-8")


async def write_job_attachment(job_id: str, field: str, request: Request, validate, missing_msg: str):
    job_or_404(me(request), job_id)
    body = await request.body()
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "유효한 UTF-8 텍스트가 아니에요.")
    validate(text)

    with lock:
        job = jobs.get(job_id)
        if not job or job.get("deleted"):
            raise HTTPException(404, "없는 작업이에요.")
        if job["status"] != "pending":
            raise HTTPException(400, "대기 중인 작업만 수정할 수 있어요.")
        filename = job.get(field)
        if not filename:
            raise HTTPException(404, missing_msg)
        (JOBS_DIR / filename).write_text(text, encoding="utf-8")


def validate_json_text(text: str):
    try:
        json.loads(text)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"유효한 JSON이 아니에요: {e}")


def validate_csv_text(text: str):
    rows = [row for row in csv.reader(io.StringIO(text)) if row]
    if not rows:
        raise HTTPException(400, "CSV 내용이 비어 있어요.")
    header_len = len(rows[0])
    for i, row in enumerate(rows[1:], start=2):
        if len(row) != header_len:
            raise HTTPException(400, f"{i}번째 줄의 열 개수가 헤더와 맞지 않아요 (헤더 {header_len}개, 이 줄 {len(row)}개).")


@app.get("/api/jobs/{job_id}/workflow")
def get_job_workflow(job_id: str, request: Request):
    text = read_job_attachment(job_id, "workflow_filename", "워크플로우 파일이 없어요.", me(request))
    return Response(content=text, media_type="application/json")


@app.put("/api/jobs/{job_id}/workflow")
async def update_job_workflow(job_id: str, request: Request):
    await write_job_attachment(job_id, "workflow_filename", request, validate_json_text, "워크플로우 파일이 없어요.")
    return {"ok": True}


@app.get("/api/jobs/{job_id}/video-workflow")
def get_job_video_workflow(job_id: str, request: Request):
    text = read_job_attachment(job_id, "video_workflow_filename", "영상 생성 워크플로우 파일이 없어요.", me(request))
    return Response(content=text, media_type="application/json")


@app.put("/api/jobs/{job_id}/video-workflow")
async def update_job_video_workflow(job_id: str, request: Request):
    await write_job_attachment(job_id, "video_workflow_filename", request, validate_json_text, "영상 생성 워크플로우 파일이 없어요.")
    return {"ok": True}


@app.get("/api/jobs/{job_id}/csv")
def get_job_csv(job_id: str, request: Request):
    text = read_job_attachment(job_id, "csv_filename", "CSV 파일이 없어요.", me(request))
    return Response(content=text, media_type="text/csv")


@app.put("/api/jobs/{job_id}/csv")
async def update_job_csv(job_id: str, request: Request):
    await write_job_attachment(job_id, "csv_filename", request, validate_csv_text, "CSV 파일이 없어요.")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str, request: Request):
    job_or_404(me(request), job_id)
    # 서버가 재시작되면서 queued/running이던 작업이 interrupted로 남았을 때, 처음부터
    # 새로 등록할 필요 없이 그 자리에서 다시 큐에 올린다. 워크플로우/CSV/옵션은 이미
    # JOBS_DIR에 남아있는 원래 값을 그대로 재사용한다. auto_run(▶ 시작/⏸ 정지) 상태와
    # 무관하게 이 버튼은 항상 즉시 큐에 넣는다 — 사용자가 특정 작업을 콕 집어 "지금
    # 다시 시도"하는 명시적 동작이기 때문이다.
    with lock:
        job = jobs.get(job_id)
        if not job or job.get("deleted"):
            raise HTTPException(404, "없는 작업이에요.")
        if job["status"] != "interrupted":
            raise HTTPException(400, "서버 재시작으로 중단된 작업만 재시작할 수 있어요.")
        job["status"] = "queued"
        job["queued_at"] = now_iso()
        job["started_at"] = None
        job["finished_at"] = None
        job["returncode"] = None
        job["progress"] = None
    save_state()
    dispatch_job(job_id)
    return jobs[job_id]


@app.post("/api/jobs/{job_id}/start")
def start_job(job_id: str, request: Request):
    # Job List 행의 "시작" — auto_run(전체 큐 자동 실행)과 무관하게 이 작업 하나만 콕
    # 집어 지금 돌린다. retry_job과 거의 같지만 pending/failed까지 넓힌 버전이다.
    # pod_id가 이미 있으면(정지됨/실패 — 예전에 어느 파드에서 돌았는지 앎) retry와
    # 똑같이 dispatch_job으로 그 파드 큐에 바로 넣는다. pod_id가 없으면(pending, 한
    # 번도 배정된 적 없음) dispatch_job으로 바로 넣지 않는다 — 그러면 스케줄러의
    # 모델/노드 적합성 검사를 건너뛰고 아무 기본 파드에나 꽂힌다. 대신
    # POST /api/queue/start와 같은 방식(queued + poke_scheduler)으로 보내 스케줄러가
    # 맞는 파드를 골라 배정하게 한다.
    job_or_404(me(request), job_id)
    with lock:
        job = jobs.get(job_id)
        if not job or job.get("deleted"):
            raise HTTPException(404, "없는 작업이에요.")
        if job["status"] not in ("pending", "interrupted", "failed"):
            raise HTTPException(400, "대기/정지/실패 상태의 작업만 시작할 수 있어요.")
        had_pod = bool(job.get("pod_id"))
        job["status"] = "queued"
        job["queued_at"] = now_iso()
        job["started_at"] = None
        job["finished_at"] = None
        job["returncode"] = None
        job["progress"] = None
        if not had_pod:
            job["waiting_reason"] = "워커를 찾는 중이에요"
    save_state()
    if had_pod:
        dispatch_job(job_id)
    else:
        poke_scheduler()
    return jobs[job_id]


@app.post("/api/jobs/{job_id}/pause")
def pause_job(job_id: str, request: Request):
    # 작업 카드의 ⏸ — 대기 큐에서 기다리는(queued) 작업 하나를 다시 일시정지(pending)로 돌린다.
    # 빈 파드가 생겨도 시작되지 않는다. 이미 파드 큐에 들어가 있어도 워커가 꺼낼 때 상태를 보고
    # 버리므로(wait_for_pod) 따로 빼낼 필요가 없다. 스케줄러가 골라 준 파드는 풀어서 다시 ▶할 때
    # 그 시점에 갖춰진 파드를 새로 찾게 하고, 사람이 고정한 파드(pinned_pod_id)는 그대로 둔다.
    job_or_404(me(request), job_id)
    with lock:
        job = jobs.get(job_id)
        if not job or job.get("deleted"):
            raise HTTPException(404, "없는 작업이에요.")
        if job["status"] != "queued":
            raise HTTPException(400, "대기 중인 작업만 일시정지할 수 있어요(실행 중이면 정지를 쓰세요).")
        job["status"] = "pending"
        job["waiting_reason"] = None
        job["missing_models"] = None
        if job.get("pod_id") and not job.get("pinned_pod_id"):
            job["pod_id"] = None
    save_state()
    return jobs[job_id]


def _registry_entry_for(name: str) -> dict | None:
    """없는 모델 이름(ComfyUI가 쓰는 하위 폴더 포함 경로)으로 등록부 항목을 찾는다 — 정확히 같은 이름이
    먼저, 없으면 파일 이름(마지막 경로 조각)이 같은 항목."""
    entries = model_registry.list_entries()
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    exact = [e for e in entries if e["filename"] == name]
    loose = [e for e in entries if e["filename"].replace("\\", "/").rsplit("/", 1)[-1] == base]
    for e in exact + loose:
        if e.get("download_url"):
            return e
    return (exact + loose or [None])[0]


def _watch_model_downloads(pod: dict, job_id: str | None, names: list[str]) -> None:
    """"없는 모델 받기"로 시작한 다운로드가 끝날 때까지 지켜보다가, 끝날 때마다 파드의 설치 목록 캐시를
    비우고 스케줄러를 깨운다 — 그래야 사람이 모델 탭을 안 열어도 다 받는 즉시 작업이 시작된다.
    job_id가 없으면(워커 모델 탭의 "모두 받기") 작업 카드 표시는 건드리지 않는다."""
    pending = set(names)
    errors: list[str] = []
    deadline = time.monotonic() + 6 * 3600
    while pending and time.monotonic() < deadline:
        time.sleep(10)
        try:
            result = model_download.call_node(pod, "GET", "/nightshift/dl/status")
        except Exception:
            continue
        finished = False
        seen_files = set()
        for item in result.get("downloads", []):   # 다운로더는 최근 것부터 준다
            fname = str(item.get("filename") or "")
            match = next((n for n in pending if n == fname or n.endswith("/" + fname) or fname.endswith("/" + n)), None)
            # 같은 파일의 옛 기록(예: 전에 실패한 받기)은 보지 않는다 — 다시 받는 중인데 옛 실패로 판정해
            # "받기 실패 → 없는 모델 받기" 버튼이 다시 뜨던 문제(NS-3).
            if not match or match in seen_files:
                continue
            seen_files.add(match)
            if item.get("status") in ("done", "error", "failed", "cancelled"):
                pending.discard(match)
                finished = True
                if item.get("status") != "done":
                    errors.append(f"{match}: {item.get('error') or item.get('status')}")
                    _note_model_event(job_id, f"받기 실패: {match} — {item.get('error') or item.get('status')}")
                else:
                    _note_model_event(job_id, f"받음: {match}")
        if finished:
            ComfyUIDriver.invalidate_capabilities(pod["id"])
            poke_scheduler()
    with lock:
        job = jobs.get(job_id) if job_id else None
        if job is not None:
            job["fetching_models"] = None
            # 실패한 받기는 이유를 남겨 카드에 보인다(없으면 받는 중 표시만 사라지고 원래 대기 이유로
            # 돌아가 "받았는데 왜 또 없다고 하지?"가 된다). 시간 안에 안 끝난 것도 알린다.
            if pending:
                errors.append(f"{', '.join(sorted(pending))}: 6시간 안에 끝나지 않았어요")
            job["fetch_error"] = " · ".join(errors) or None
            _append_model_event(job, "다 받았어요 — 설치 목록을 다시 확인해요" if not errors else "받기가 끝났지만 실패가 있어요")
            if not errors and job.get("status") == "queued" and not job.get("pod_id"):
                # 다 받았으면 옛 "없는 것 — …"을 지운다 — 스케줄러가 새 설치 목록으로 다시 볼 때까지(최대 몇 초)
                # 카드에 옛 이유가 다시 보여 "받았는데 또 없다고?"처럼 깜빡였다. 아직 모자라면 스케줄러가 다시 적는다.
                job["missing_models"] = None
                job["waiting_reason"] = "모델을 다 받았어요 — 곧 시작해요"
    save_state()
    ComfyUIDriver.invalidate_capabilities(pod["id"])
    poke_scheduler()


async def _start_model_downloads(user: dict, pod: dict, names: list[str]) -> tuple[list, list, list]:
    """등록부의 받을 주소로 그 워커에 모델 받기를 시작한다 — (시작한 것, 주소가 없는 것, 실패한 것).
    작업 카드의 "없는 모델 받기"와 워커 모델 탭의 "모두 받기"가 함께 쓴다."""
    try:
        token = (auth.get_secrets(user["id"]) or {}).get("civitai_token") or None
    except Exception:
        token = None   # 주인 없는 옛 작업 등 — 토큰 없이 받는다(공개 모델은 그대로 받힌다)
    started, no_url, failed = [], [], []
    for name in names:
        if name.startswith("노드 "):
            failed.append({"name": name, "detail": "커스텀 노드는 여기서 받을 수 없어요 — 파드에 직접 설치하세요."})
            continue
        entry = _registry_entry_for(name)
        if not entry or not entry.get("download_url"):
            no_url.append(name)
            continue
        body = {"url": entry["download_url"], "folder": entry["kind"], "filename": name, "overwrite": False,
                "headers": model_download.auth_header_for(entry["download_url"], token)}
        try:
            await asyncio.to_thread(model_download.call_node, pod, "POST", "/nightshift/dl/start", body)
            started.append(name)
        except model_download.DownloadError as e:
            if e.status == 409 and "받는 중" in str(e):
                # 다른 작업(또는 자동 설치)이 먼저 받고 있다 — 이 작업도 "받는 중"으로 따라간다. 전에는 시작도 실패도
                # 아닌 채로 빠져서, 받는 동안 카드에 "없는 모델 + 받기 버튼"이 떠 있었다(NS-3).
                started.append(name)
                continue
            if e.status == 409 and "이미" in str(e):
                # 파일이 이미 있다 — 받을 것 없이 설치 목록만 다시 읽으면 된다.
                ComfyUIDriver.invalidate_capabilities(pod["id"])
                poke_scheduler()
                continue
            failed.append({"name": name, "detail": str(e)})
    return started, no_url, failed


def _append_model_event(job: dict, text: str) -> None:
    """작업의 모델 받기 기록 한 줄(최근 30개) — 작업 상세에 보인다. lock을 잡은 채로 부른다."""
    events = list(job.get("model_events") or [])[-29:]
    events.append({"at": now_iso(), "text": text})
    job["model_events"] = events


def _note_model_event(job_id: str | None, text: str) -> None:
    if not job_id:
        return
    with lock:
        job = jobs.get(job_id)
        if job is not None:
            _append_model_event(job, text)


def _describe_fetch(prefix: str, started, no_url, failed) -> list[str]:
    lines = []
    if started:
        lines.append(f"{prefix}: {', '.join(started)}")
    if no_url:
        lines.append(f"주소가 없어 못 받음(모델 탭에서 다운로드 주소 등록): {', '.join(no_url)}")
    for f in failed:
        lines.append(f"받기 실패: {f['name']} — {f['detail']}")
    return lines


def _maybe_auto_fetch(job_id: str, pod: dict, missing: list[str]) -> None:
    """워커의 "필요한 모델 자동 설치"가 켜져 있을 때 스케줄러가 부른다 — 이 작업이 이 워커에서 모자란 모델을
    사람이 "없는 모델 받기"를 누른 것처럼 받는다. 같은 모델은 작업마다 한 번만 시도하고(auto_fetch_tried),
    받는 중이거나 지난 받기가 실패했으면(fetch_error — 사람이 "없는 모델 받기"를 누르면 풀린다) 손대지 않는다."""
    names = [n for n in missing if not n.startswith("노드 ")]
    with lock:
        job = jobs.get(job_id)
        if job is None or job.get("fetching_models") or job.get("fetch_error"):
            return
        tried = set(job.get("auto_fetch_tried") or [])
        todo = [n for n in names if n not in tried]
        if not todo:
            return
        job["auto_fetch_tried"] = sorted(tried | set(todo))
        owner_id = job.get("owner_id")
    threading.Thread(target=_auto_fetch_run, args=(job_id, pod, todo, owner_id), daemon=True).start()


def _auto_fetch_run(job_id: str, pod: dict, names: list[str], owner_id) -> None:
    started, no_url, failed = asyncio.run(_start_model_downloads({"id": owner_id}, pod, names))
    with lock:
        job = jobs.get(job_id)
        if job is not None:
            for line in _describe_fetch("자동 설치 시작", started, no_url, failed) or ["자동 설치 — 이미 있어서 받을 것이 없어요"]:
                _append_model_event(job, line)
            if started:
                job["fetching_models"] = {"pod_id": pod["id"], "names": started, "started_at": now_iso(), "auto": True}
            if failed:
                job["fetch_error"] = "자동 설치 실패 — " + " · ".join(f"{f['name']}: {f['detail']}" for f in failed)
    save_state()
    if started:
        _watch_model_downloads(pod, job_id, started)


def _needed_models_on_pod(user: dict, pod: dict) -> list[dict] | None:
    """내(관리자는 전체) 대기 작업 — 아직 워커가 안 정해진 queued·pending — 이 이 워커에서 돌려면 없는 것들.
    [{name, kind, download_url, node, jobs: [{id, label}]}] 이름순. 워커가 설치 목록을 안 주면 None."""
    scope = auth.owner_scope(user)
    with lock:
        waiting = [dict(j) for j in jobs.values()
                   if j["status"] in ("queued", "pending") and not j.get("pod_id") and not j.get("deleted")
                   and (scope is None or j.get("owner_id") == scope)]
    needed: dict[str, dict] = {}
    for job in sorted(waiting, key=lambda j: j.get("queued_at") or ""):
        missing = job_missing_on_pod(job, pod)
        if missing is None:
            return None
        for name in missing:
            if name not in needed:
                node = name.startswith("노드 ")
                entry = None if node else _registry_entry_for(name)
                needed[name] = {"name": name, "node": node, "kind": (entry or {}).get("kind"),
                                "download_url": (entry or {}).get("download_url") or None, "jobs": []}
            needed[name]["jobs"].append({"id": job["id"], "label": job.get("template_label") or job.get("template_id")})
    return sorted(needed.values(), key=lambda x: x["name"].lower())


@app.get("/api/pods/{pod_id}/models")
async def pod_models_api(pod_id: str, request: Request, refresh: bool = False):
    """워커 모델 탭 — 그 워커에 설치된 모델(종류별)과, 대기 작업에 필요한데 그 워커에 없는 것.
    사용이 꺼진 워커도 연결되면 보여 준다(설치 확인은 작업 배정과 무관하므로)."""
    user = me(request)
    pod = _download_pod(user, pod_id)
    try:
        _, info = await asyncio.to_thread(fetch_comfy_object_info, refresh, pod)
    except Exception:
        info = None
    if info is None:
        return {"connected": False, "models": {}, "needed": []}
    models = {k: combo_choices(info, *src) for k, src in MODEL_LIST_SOURCES.items()}
    needed = await asyncio.to_thread(_needed_models_on_pod, user, pod)
    return {"connected": True, "models": models, "needed": needed or []}


@app.post("/api/pods/{pod_id}/models/fetch-needed")
async def fetch_needed_models(pod_id: str, request: Request):
    """워커 모델 탭의 "모두 받기" — 대기 작업에 필요한데 없는 모델 중 받을 주소가 있는 것을 전부 받는다.
    다 받으면 작업 카드의 "없는 모델 받기"처럼 캐시를 비우고 스케줄러를 깨워 저절로 시작되게 한다."""
    user = me(request)
    pod = _download_pod(user, pod_id)
    needed = await asyncio.to_thread(_needed_models_on_pod, user, pod)
    if needed is None:
        raise HTTPException(409, "이 워커의 파드에 연결하지 못해 무엇이 없는지 알 수 없어요.")
    names = [n["name"] for n in needed if not n["node"]]
    if not names:
        raise HTTPException(400, "받을 모델이 없어요 — 대기 작업에 필요한 모델이 이미 다 있어요.")
    started, no_url, failed = await _start_model_downloads(user, pod, names)
    if started:
        threading.Thread(target=_watch_model_downloads, args=(pod, None, started), daemon=True).start()
    return {"pod_id": pod["id"], "started": started, "no_url": no_url, "failed": failed}


@app.post("/api/jobs/{job_id}/fetch-missing")
async def fetch_missing_models(job_id: str, request: Request):
    """카드의 "없는 모델 받기" — 스케줄러가 적어 둔 missing_models(그 파드에 없는 모델)를 등록부의 다운로드
    주소로 그 파드에 받는다. 주소가 없는 모델은 받지 않고 알려 준다. 다 받으면 작업은 저절로 시작된다."""
    user = me(request)
    job_or_404(user, job_id)
    with lock:
        job = jobs.get(job_id)
        info = dict(job.get("missing_models") or {}) if job else {}
    if not info.get("names"):
        raise HTTPException(400, "받을 모델이 없어요(이미 갖춰졌거나 아직 확인 전이에요).")
    pod = _download_pod(user, info["pod_id"])
    started, no_url, failed = await _start_model_downloads(user, pod, info["names"])
    with lock:
        job = jobs.get(job_id)
        if job is not None:
            for line in _describe_fetch("받기 시작(없는 모델 받기)", started, no_url, failed) or ["없는 모델 받기 — 이미 있어서 받을 것이 없어요"]:
                _append_model_event(job, line)
    if started:
        with lock:
            job = jobs.get(job_id)
            if job is not None:
                job["fetching_models"] = {"pod_id": pod["id"], "names": started, "started_at": now_iso()}
                job["fetch_error"] = None
        save_state()
        threading.Thread(target=_watch_model_downloads, args=(pod, job_id, started), daemon=True).start()
    return {"pod_id": pod["id"], "started": started, "no_url": no_url, "failed": failed}


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str, request: Request):
    # Job List 행의 "정지" — 그 파드의 auto_run이나 같은 파드에서 같이 도는 다른 작업은
    # 안 건드리고, 이 작업 하나의 서브프로세스만 종료 요청한다(stop_pod_jobs와 같은
    # terminate → 5초 뒤 감시 스레드로 kill 방식을 이 작업 하나에만 좁혀 적용).
    job_or_404(me(request), job_id)
    with lock:
        job = jobs.get(job_id)
        if not job or job.get("deleted"):
            raise HTTPException(404, "없는 작업이에요.")
        if job["status"] != "running":
            raise HTTPException(400, "진행 중인 작업만 정지할 수 있어요.")
        pod_id = job.get("pod_id")
        rt = pod_runtimes.get(pod_id) if pod_id else None
        proc = rt.running.get(job_id) if rt else None
        if proc is not None:
            job["_stop_requested"] = True
    if proc is None:
        raise HTTPException(409, "지금 실행 중인 프로세스를 찾지 못했어요(막 끝났을 수 있어요) — 잠시 후 다시 확인해 주세요.")
    proc.terminate()

    def _kill_if_still_alive(p=proc):
        time.sleep(5)
        if p.poll() is None:
            p.kill()

    threading.Thread(target=_kill_if_still_alive, daemon=True).start()
    return {"ok": True, "job_id": job_id}


@app.post("/api/jobs/{job_id}/move")
async def move_job(job_id: str, request: Request):
    """작업을 다른 파드로 옮긴다.

    파드마다 큐가 따로 도는 구조라, 파드 하나가 죽으면 그 큐만 멈춘다(옆 파드가 놀아도
    자동으로 안 넘어간다). 그때 손으로 풀 수 있는 탈출구다.

    이미 큐에 들어간(queued) 작업도 옮길 수 있다 — 파이썬 큐에서 꺼내 빼는 건 불가능하지만,
    워커가 작업을 꺼낼 때 "이 작업이 아직 내 것인가"를 확인하고 아니면 원래 주인에게
    다시 배차하기 때문이다(pod_worker_loop/wait_for_pod). 실행 중인 작업은 옮길 수 없다."""
    user = me(request)
    source_job = job_or_404(user, job_id)
    data = await read_json_object(request, allow_empty=False)
    target_id = (data.get("pod_id") or "").strip()
    if not target_id:
        # 파드 지정을 풀어 "갖춘 파드가 있으면 아무 파드나"로 되돌린다(대기 큐/대기 목록의 작업만).
        with lock:
            job = jobs.get(job_id)
            if not job or job.get("deleted"):
                raise HTTPException(404, "없는 작업이에요.")
            if job["status"] not in ("pending", "queued"):
                raise HTTPException(400, "대기 중인 작업만 워커 지정을 풀 수 있어요.")
            job["pinned_pod_id"] = None
            job["pod_id"] = None
            job["auto_assigned"] = False
            set_comfy_wait_flag(job, False)
            if job["status"] == "queued":
                job["waiting_reason"] = "워커를 찾는 중이에요"
        save_state()
        poke_scheduler()
        return jobs[job_id]
    target = pod_registry.get_pod(target_id)
    # 작업은 그 작업의 주인의 파드로만 옮길 수 있다.
    if target is None or target.get("owner_id") != source_job.get("owner_id"):
        raise HTTPException(404, "없는 워커예요.")
    if not target.get("enabled"):
        raise HTTPException(400, f"'{target['name']}' 워커는 지금 사용 안 함 상태예요.")

    with lock:
        job = jobs.get(job_id)
        if not job or job.get("deleted"):
            raise HTTPException(404, "없는 작업이에요.")
        if job["status"] == "running":
            raise HTTPException(400, "실행 중인 작업은 옮길 수 없어요. 먼저 멈춰주세요.")
        if job["status"] not in ("pending", "queued", "interrupted"):
            raise HTTPException(400, "대기 중이거나 중단된 작업만 옮길 수 있어요.")
        if job["status"] == "queued" and not job.get("pod_id"):
            # 대기 큐에서 파드를 기다리는 작업 — 그 파드로 고정만 하고, 배정은 스케줄러가 갖춰졌는지 보고 한다.
            job["pinned_pod_id"] = target_id
            job["waiting_reason"] = "워커를 찾는 중이에요"
            pinned_only = True
        else:
            pinned_only = False
            if job.get("pod_id") == target_id:
                return job
        if pinned_only:
            pass
        else:
            job["pinned_pod_id"] = target_id
    if pinned_only:
        save_state()
        poke_scheduler()
        return jobs[job_id]
    with lock:
        job = jobs[job_id]
        job["pod_id"] = target_id
        wf_names = [job.get("workflow_filename"), job.get("video_workflow_filename")]
        job_template_id = job.get("template_id")
        requeue = job["status"] == "queued"
        if requeue:
            # 옛 파드의 큐에 남은 항목은 그 파드 워커가 꺼낼 때 버려진다(소유권 확인).
            set_comfy_wait_flag(job, False)
    if target.get("kind") == pod_registry.DEFAULT_KIND:
        blobs = []
        for label, name in zip(("워크플로우", "영상 워크플로우"), wf_names):
            if name:
                try:
                    blobs.append((label, (JOBS_DIR / name).read_bytes()))
                except OSError:
                    pass
            elif label == "영상 워크플로우":
                blobs.append((label, default_video_workflow_bytes(load_templates_map().get(job_template_id) or {})))
        try:
            new_preflight = await asyncio.wait_for(
                asyncio.to_thread(compute_preflight, target, user, blobs), timeout=10) if blobs else None
        except Exception:
            new_preflight = None
        with lock:
            if jobs.get(job_id):
                jobs[job_id]["preflight"] = new_preflight
    save_state()
    if requeue:
        dispatch_job(job_id, target_id)
    return jobs[job_id]


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, request: Request):
    job_or_404(me(request), job_id)
    with lock:
        job = jobs.get(job_id)
        if not job or job.get("deleted"):
            raise HTTPException(404, "없는 작업이에요.")
        waiting_for_pod = job["status"] == "queued" and not job.get("pod_id")   # 아직 어느 파드 큐에도 안 들어갔다
        if job["status"] in ("running", "queued") and not waiting_for_pod:
            raise HTTPException(400, "실행 중이거나 이미 시작된 작업은 지울 수 없어요.")
        # 실제로 지우지 않고 소프트 삭제만 한다 — "작업 목록"에서는 사라지지만,
        # 상단의 "삭제된 작업 설정 불러오기" 드롭다운에서는 (보관 기간 안이면)
        # 계속 고를 수 있다. prune_deleted_jobs()가 오래된 것부터 완전히 정리한다.
        job["deleted"] = True
        job["deleted_at"] = now_iso()
        prune_deleted_jobs()
    save_state()
    return {"ok": True}


