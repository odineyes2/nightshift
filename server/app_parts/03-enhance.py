# app.py가 app_parts/*.py를 번호 순서대로 한 네임스페이스에서 실행한다 — 앞 파일의 이름을 import 없이 쓴다.
# ---- 프롬프트 개선(Text Enhance) ----------------------------------------------
# templates/*.py의 find_node()/primitive_value_field()와 같은 알고리즘의 사본이다
# (그쪽은 배치 작업을 큐에 올려 python3 서브프로세스로 실행하는 것과 달리, 이건
# HTTP 요청 하나 처리하는 동안 서버가 직접 ComfyUI에 동기적으로 물어보고 기다리는
# 별개의 경로라 템플릿 스크립트를 그대로 재사용할 수 없다).
_ENHANCER_PRIMITIVE_VALUE_FIELDS = {
    "CLIPTextEncode": "text",
    "PrimitiveStringMultiline": "value",
    "PrimitiveString": "value",
}


def _enhancer_find_node(workflow: dict, title_substring: str | None = None, class_types: tuple = ()):
    title_substring = (title_substring or "").lower()
    fallback = None
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        meta_title = str(node.get("_meta", {}).get("title", "")).lower()
        class_type = node.get("class_type", "")
        if title_substring and title_substring in meta_title:
            return node_id, node
        if class_types and class_type in class_types and fallback is None:
            fallback = (node_id, node)
    return fallback if fallback else (None, None)


def _enhancer_primitive_value_field(node: dict) -> str | None:
    field = _ENHANCER_PRIMITIVE_VALUE_FIELDS.get(node.get("class_type", ""))
    if field:
        return field
    inputs = node.get("inputs", {})
    if "text" in inputs:
        return "text"
    if "value" in inputs:
        return "value"
    return None


def _enhancer_extract_text(history_entry: dict, node_id: str) -> str | None:
    # PreviewAny처럼 OUTPUT_NODE=True인 커스텀 노드는 보통 {"text": ["..."]}
    # 형태로 history의 outputs에 결과를 남기지만, 커스텀 노드마다 키 이름이
    # 다를 수 있어(예: "string", "value") 키 이름은 보지 않고 문자열 리스트인
    # 첫 값을 그대로 쓴다.
    outputs = (history_entry or {}).get("outputs", {}) or {}
    node_output = outputs.get(node_id)
    if not isinstance(node_output, dict):
        return None
    for value in node_output.values():
        if isinstance(value, list) and value and isinstance(value[0], str):
            return value[0]
    return None


ENHANCER_MODEL = "Huihui-qwen3vl_4b_fp8_scaled.safetensors"   # prompt_enhancer.json의 CLIPLoader가 쓰는 텍스트 인코더


def enhancer_model_missing(pod: dict | None = None) -> bool:
    """그 파드의 텍스트 인코더 목록에 ENHANCER_MODEL이 없으면 True. 파드에 못 물어보면(꺼짐 등) 판단하지 않고 False."""
    try:
        _, info = fetch_comfy_object_info(False, pod)
    except Exception:
        return False
    if info is None:
        return False
    return ENHANCER_MODEL not in combo_choices(info, *MODEL_LIST_SOURCES["text_encoders"])


def _enhance_prompt_sync(user_prompt: str, mode: str = "natural") -> str:
    """ComfyUI에 prompt_enhancer.json 워크플로우를 제출하고 완료될 때까지 동기적으로
    기다린 뒤 개선된 프롬프트 문자열을 돌려준다. mode가 "danbooru"면 자연어로 다듬은
    결과를 다시 Danbooru 태그 목록으로 변환하는 경로를 태운다(isDanbooru_sys? 스위치
    노드). urllib(블로킹 I/O)를 쓰므로 반드시 asyncio.to_thread로 감싸서 호출해야 한다."""
    if not ENHANCER_WORKFLOW_PATH.exists():
        raise HTTPException(500, "프롬프트 개선용 워크플로우(prompt_enhancer.json)가 서버에 없어요.")
    try:
        workflow = json.loads(ENHANCER_WORKFLOW_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"프롬프트 개선용 워크플로우가 올바른 JSON이 아니에요: {e}")

    input_node_id, input_node = _enhancer_find_node(workflow, title_substring=ENHANCER_INPUT_NODE_TITLE)
    input_field = _enhancer_primitive_value_field(input_node) if input_node is not None else None
    if input_field is None:
        raise HTTPException(
            500,
            f"프롬프트 개선용 워크플로우에서 입력 노드를 찾지 못했어요 "
            f"(제목에 '{ENHANCER_INPUT_NODE_TITLE}'가 포함된 텍스트 노드 없음).",
        )
    input_node.setdefault("inputs", {})[input_field] = user_prompt

    # 모드를 고를 스위치 노드가 없는(예전 워크플로우로 되돌린) 경우에도 전체 기능이
    # 깨지지 않도록, 못 찾으면 조용히 건너뛰고 워크플로우에 이미 설정된 기본 경로를
    # 그대로 쓴다.
    mode_node_id, mode_node = _enhancer_find_node(
        workflow,
        title_substring=ENHANCER_MODE_NODE_TITLE,
        class_types=("ComfySwitchNode",),
    )
    if mode_node is not None:
        mode_node.setdefault("inputs", {})["switch"] = (mode == "danbooru")

    output_node_id, output_node = _enhancer_find_node(
        workflow,
        title_substring=ENHANCER_OUTPUT_NODE_TITLE,
        class_types=("PreviewAny",),
    )
    if output_node is None:
        raise HTTPException(500, "프롬프트 개선용 워크플로우에서 결과를 읽어올 출력 노드를 찾지 못했어요.")

    comfy_url, connected = resolve_comfy_url()
    if not connected:
        raise HTTPException(503, "ComfyUI 서버에 연결할 수 없어요.")

    payload = json.dumps({"prompt": workflow, "client_id": str(uuid.uuid4())}).encode("utf-8")
    req = urllib.request.Request(
        f"{comfy_url}/prompt",
        data=payload,
        # User-Agent가 필요한 이유는 drivers/comfyui.py의 COMFY_USER_AGENT 주석 참고
        # — Cloudflare가 앞단에 있는 RunPod pod는 기본 urllib UA를 403으로 막는다.
        headers={"Content-Type": "application/json", "User-Agent": COMFY_USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            submit_data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise HTTPException(502, f"ComfyUI에 프롬프트를 제출하지 못했어요: {e}")
    if "error" in submit_data:
        raise HTTPException(502, f"ComfyUI가 프롬프트를 거부했어요: {submit_data['error']}")
    prompt_id = submit_data["prompt_id"]

    deadline = time.time() + ENHANCE_TIMEOUT_SEC
    while time.time() < deadline:
        try:
            hist_req = urllib.request.Request(
                f"{comfy_url}/history/{prompt_id}", headers={"User-Agent": COMFY_USER_AGENT})
            with urllib.request.urlopen(hist_req, timeout=30) as resp:
                history = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise HTTPException(502, f"ComfyUI 히스토리 조회에 실패했어요: {e}")
        if prompt_id in history:
            text = _enhancer_extract_text(history[prompt_id], output_node_id)
            if text is None:
                raise HTTPException(500, "프롬프트 개선 결과를 읽지 못했어요 (출력 노드에 텍스트가 없음).")
            return text.strip()
        time.sleep(ENHANCE_POLL_INTERVAL_SEC)
    raise HTTPException(504, f"프롬프트 개선이 {int(ENHANCE_TIMEOUT_SEC)}초 안에 끝나지 않았어요.")


def stop_pod_jobs(pod_id: str) -> list[str]:
    """그 파드에서 지금 돌고 있는 서브프로세스들에 종료를 요청한다("⏸ 정지").
    종료를 요청한 job_id 목록을 반환한다. SIGTERM을 무시하고 계속 살아있는 경우를
    대비해, 잠시 후에도 안 죽어있으면 강제 종료(kill)하는 감시 스레드를 띄운다."""
    with lock:
        rt = pod_runtimes.get(pod_id)
        if rt is None:
            return []
        targets = list(rt.running.items())
        for job_id, _proc in targets:
            job = jobs.get(job_id)
            if job is not None:
                job["_stop_requested"] = True

    for _job_id, proc in targets:
        proc.terminate()

        def _kill_if_still_alive(p=proc):
            time.sleep(5)
            if p.poll() is None:
                p.kill()

        threading.Thread(target=_kill_if_still_alive, daemon=True).start()
    return [job_id for job_id, _ in targets]


def stop_all_jobs() -> list[str]:
    with lock:
        pod_ids = list(pod_runtimes)
    stopped = []
    for pod_id in pod_ids:
        stopped.extend(stop_pod_jobs(pod_id))
    return stopped


def set_comfy_wait_flag(job: dict, waiting: bool) -> bool:
    """job의 "ComfyUI 연결 대기 중" 표시를 갱신하고, 값이 실제로 바뀌었는지 돌려준다
    (바뀔 때만 save_state()를 부르면 되므로 — 15초마다 상태 파일을 새로 쓸 이유가 없다).
    lock을 쥔 채 호출해야 한다."""
    if waiting:
        if job.get("waiting_for_comfy"):
            return False
        job["waiting_for_comfy"] = True
        job["waiting_since"] = now_iso()
        return True
    if not job.get("waiting_for_comfy"):
        return False
    job.pop("waiting_for_comfy", None)
    job.pop("waiting_since", None)
    return True


def wait_for_pod(pod_id: str, job_id: str) -> tuple[str, str | None]:
    """파드에 연결될 때까지 붙잡고 있다가, 연결되면 ("run", 주소)를 돌려준다.

    예전에는 큐에서 꺼낸 작업을 곧바로 실행 상태로 바꾼 뒤 연결을 확인하고, 안 되면
    그 자리에서 failed 처리했다. 워커가 같은 머신에서 항상 같이 떠 있던 시절에는
    그게 맞았지만, GPU pod가 따로 있는 구성에서는 pod가 잠깐 꺼져 있는 동안 대기 중인
    작업이 순식간에 전멸한다 — 큐가 한 번에 한 개씩 돌기 때문에 수십 개가 몇 초 만에
    차례로 실패한다. 그래서 이제는 실패시키지 않고 "queued"인 채로 기다린다.

    기다리기를 그만둬야 하는 경우:
      ("skip", None)   그 사이 작업이 삭제됐다 → 그냥 건너뛴다.
      ("moved", None)  그 사이 작업이 다른 파드로 옮겨졌다 → 그 파드 큐로 넘긴다.
      ("pending", None) "⏸ 정지"로 이 파드의 auto_run이 꺼졌다 → 작업을 "pending"으로
                       되돌린다. 다음 "▶ 시작" 때 이어서 돌고, 무한정 기다리는 상태에서
                       빠져나오는 탈출구이기도 하다(대기 중인 작업은 status가 "queued"라
                       그대로는 삭제할 수 없다).
    """
    waited = False
    while True:
        pod = pod_registry.get_pod(pod_id)
        if pod is None:
            return "moved", None      # 파드가 지워졌다 — 다시 배차한다
        health = driver_for(pod).health(pod)
        url, connected = health["url"], health["ok"]

        with lock:
            job = jobs.get(job_id)
            gone = job is None or job.get("deleted")
            moved = not gone and job.get("pod_id") != pod_id
            if job is not None and (connected or gone or moved):
                set_comfy_wait_flag(job, False)
        if gone:
            if waited:
                save_state()
            return "skip", None
        if moved:
            if waited:
                save_state()
            return "moved", None
        if connected:
            if waited:
                save_state()
            return "run", url

        with lock:
            rt = pod_runtimes.get(pod_id)
            if job.get("auto_assigned") and not (rt and rt.closed):
                pod_label = pod.get("name") or pod_id
                job["auto_assigned"] = False
                job["pod_id"] = None
                job["status"] = "queued"
                job["waiting_reason"] = f"'{pod_label}': 배정된 뒤 연결이 끊겼어요"
                set_comfy_wait_flag(job, False)
                reassigned = True
            else:
                reassigned = False
        if reassigned:
            save_state()
            poke_scheduler()
            return "moved", None
        with lock:
            rt = pod_runtimes.get(pod_id)
            keep_waiting = bool(rt and rt.auto_run and not rt.closed)
            if keep_waiting:
                changed = set_comfy_wait_flag(job, True)
            else:
                set_comfy_wait_flag(job, False)
                job["status"] = "pending"
                job["started_at"] = None
                changed = True
        if changed:
            save_state()
        if not keep_waiting:
            # 아무 설명 없이 대기 목록으로 되돌아가면 사용자가 이유를 알 길이 없으므로
            # 로그에 한 줄 남긴다(작업 행을 펼치면 그대로 보인다).
            where = url or "자동 탐지 실패"
            (LOGS_DIR / f"{job_id}.log").write_text(
                f"'{pod['name']}' 워커의 파드에 연결할 수 없어 대기 목록으로 되돌렸어요 ({where}).\n"
                "서버가 켜진 걸 확인한 뒤 ▶ 시작을 누르면 이어서 실행됩니다.\n",
                encoding="utf-8",
            )
            return "pending", None
        waited = True

        # COMFY_WAIT_RETRY_SEC를 통째로 자면 그동안 "⏸ 정지"에 반응하지 못하므로
        # 잘게 쪼개 자면서 중간에 빠져나올 조건을 확인한다.
        slept = 0.0
        while slept < COMFY_WAIT_RETRY_SEC:
            time.sleep(COMFY_WAIT_TICK_SEC)
            slept += COMFY_WAIT_TICK_SEC
            with lock:
                job = jobs.get(job_id)
                rt = pod_runtimes.get(pod_id)
                if (rt is None or not rt.auto_run or rt.closed
                        or job is None or job.get("deleted")
                        or job.get("pod_id") != pod_id):
                    break


def pull_job_outputs(pod: dict, job_id: str, comfy_url: str, log_path: Path):
    """작업 하나가 끝난 뒤 그 작업(job_id 하위 폴더)의 결과 이미지만 끌어온다.

    설정이 꺼져 있으면 아무것도 안 한다. 실패해도 작업 상태에는 영향을 주지 않는다 —
    이미지는 원격에 그대로 남아 있고 갤러리 탭에서 수동으로 다시 가져올 수 있으므로,
    이미 끝난 작업을 실패로 뒤집을 이유가 없다. 대신 무슨 일이 있었는지 그 작업의
    로그 끝에 덧붙여, 이미지가 안 보일 때 이유를 찾을 수 있게 한다."""
    try:
        result = driver_for(pod).collect(pod, comfy_url, job_id)
    except OutputSyncError as e:
        note = f"[출력 동기화] 실패: {e}"
    else:
        if result is None:
            return
        note = (
            f"[출력 동기화] {len(result['downloaded'])}장 가져옴 "
            f"(확인 {result['checked']}장, 이미 있음 {result['skipped_existing']}장, "
            f"기록됨 {result['skipped_known']}장)"
        )
        if result["errors"]:
            note += f" — 실패 {len(result['errors'])}건: {'; '.join(result['errors'][:3])}"
    try:
        with open(log_path, "a", encoding="utf-8") as logf:
            logf.write(f"\n{note}\n")
    except OSError:
        pass


