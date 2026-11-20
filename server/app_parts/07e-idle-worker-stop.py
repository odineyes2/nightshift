# 회원 세션으로 자기 유휴 파드만 종료한다. 유료 공용 파드의 전원 권한은 노출하지 않는다.
_IDLE_JOB_STATES = ("pending", "queued", "running")


def _idle_worker_owned(user, worker_id):
    pod = pod_registry.get_pod(worker_id)
    if (not pod or pod.get("owner_id") != user["id"] or pod.get("kind") != pod_registry.DEFAULT_KIND
            or pod.get("runpod_tier") not in ("image", "video")):
        raise HTTPException(403, "자기 RunPod 워커만 종료할 수 있어요.")
    return pod


def _idle_worker_busy(user_id, pod):
    """jobs 잠금 안에서 호출한다. 대기·실행 작업과 런타임 큐·정비를 모두 확인한다."""
    for job in jobs.values():
        if job.get("deleted") or job.get("status") not in _IDLE_JOB_STATES:
            continue
        if job.get("owner_id") == user_id or job.get("pod_id") == pod["id"]:
            return True
        if not job.get("pod_id") and pod.get("owner_id") in service_tokens.pod_owner_ids_for(job):
            return True
    rt = pod_runtimes.get(pod["id"])
    return bool(rt and (rt.running or rt.queue.qsize() or rt.maintenance))


@app.get("/api/auth/runpod-stop-status")
def auth_runpod_stop_status(request: Request):
    user = me(request)
    if paid_plan.plan(user["id"])["paid"]:
        return {"mode": "shared", "workers": []}
    workers = []
    for tier in ("image", "video"):
        for pod in _member_workers(user["id"], tier):
            starting = pod.get("note") == "runpod:creating"
            on = bool(pod.get("enabled") or pod.get("url")) and not starting
            with lock:
                busy = _idle_worker_busy(user["id"], pod) or pod["id"] in idle_stopping_pods
            with _runpod_power_locks_guard:
                power = _runpod_power_locks.get(pod["id"])
                powering = bool(power and power.locked())
            workers.append({"id": pod["id"], "name": pod.get("name") or ("이미지 파드" if tier == "image" else "영상 파드"),
                            "tier": tier, "on": on, "starting": starting,
                            "can_stop": on and not starting and not busy and not powering
                                        and bool(runpod_api.extract_pod_id(pod.get("url") or ""))
                                        and bool(runpod_api.key_for_owner(user["id"]))})
    return {"mode": "own", "workers": workers}


def _stop_idle_worker(user, worker_id):
    # 개인 자동 기동과 같은 잠금으로 종료 중 재기동을 막는다.
    if not _member_auto_lock.acquire(blocking=False):
        raise HTTPException(409, "워커 전원을 처리 중이에요. 잠시 뒤 다시 확인해 주세요.")
    power = None
    marked = False
    try:
        if paid_plan.plan(user["id"])["paid"]:
            raise HTTPException(403, "유료 공용 파드는 직접 종료할 수 없어요.")
        pod = _idle_worker_owned(user, worker_id)
        power = _try_power_lock(worker_id)
        if power is None:
            raise HTTPException(409, "워커 전원을 처리 중이에요.")
        with lock:
            pod = _idle_worker_owned(user, worker_id)
            if pod.get("note") == "runpod:creating" or _idle_worker_busy(user["id"], pod):
                raise HTTPException(409, "대기하거나 실행 중인 작업이 있어요.")
            if not runpod_api.extract_pod_id(pod.get("url") or "") or not runpod_api.key_for_owner(user["id"]):
                raise HTTPException(409, "종료할 수 있는 RunPod 파드가 없어요.")
            idle_stopping_pods.add(worker_id)
            marked = True
            pod_registry.update_pod(worker_id, {"enabled": False})
        # 외부 호출 동안 jobs 잠금은 풀어 다른 워커 작업을 막지 않는다.
        error = _power_off_runpod_worker(pod)
        if error:
            with lock:
                current = pod_registry.get_pod(worker_id)
                if current and current.get("url") == pod.get("url"):
                    pod_registry.update_pod(worker_id, {"enabled": pod.get("enabled", False)})
            raise HTTPException(503, "파드를 종료하지 못했어요. 상태를 다시 확인해 주세요.")
        return {"ok": True, "worker_id": worker_id, "stopped": True}
    finally:
        if marked:
            with lock:
                idle_stopping_pods.discard(worker_id)
        if power is not None:
            power.release()
        _member_auto_lock.release()


@app.post("/api/auth/runpod-stop-idle")
async def auth_runpod_stop_idle(request: Request):
    user = me(request)
    body = await read_json_object(request, allow_empty=False)
    worker_id = body.get("worker_id")
    if set(body) != {"worker_id"} or not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 200:
        raise HTTPException(422, "종료할 워커를 골라 주세요.")
    return await asyncio.to_thread(_stop_idle_worker, user, worker_id.strip())
