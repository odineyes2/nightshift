# app.py가 app_parts/*.py를 번호 순서대로 한 네임스페이스에서 실행한다 — 앞 파일의 이름을 import 없이 쓴다.
# ---- 파드별 실행 상태 ---------------------------------------------------------
# 예전에는 큐도 자동 실행 플래그도 "지금 돌고 있는 서브프로세스"도 전부 모듈 전역이었다
# — 워커가 하나뿐이었기 때문이다. 이제는 파드마다 하나씩 갖는다. 작업에는 pod_id가
# 붙고, 그 파드의 큐에만 들어간다(파드별 독립 큐 — multipod_plan.md 참고).
class PodRuntime:
    """파드 하나의 실행 상태. 필드는 전부 전역 lock 아래에서 읽고 쓴다."""

    def __init__(self, pod_id: str, max_concurrent: int = 1):
        self.pod_id = pod_id
        self.queue: "queue.Queue[str]" = queue.Queue()
        # 자동 실행 모드 — 켜져 있는 동안에는 이 파드로 새로 추가되는 작업이 pending에
        # 머무르지 않고 바로 큐에 들어간다("▶ 시작"/"⏸ 정지"). 서버가 재시작되면 큐에
        # 남아있던 작업이 interrupted로 표시되는 것과 같은 이유로 초기화된다(재시작 후
        # 저절로 다시 돌기 시작하면 안 되므로) — 그래서 디스크에 저장하지 않는다.
        self.auto_run = False
        # 지금 이 파드에서 돌고 있는 작업 {job_id: 서브프로세스}. "⏸ 정지"가 이걸
        # 종료시킨다. max_concurrent가 1이면 최대 한 개다.
        self.running: dict[str, subprocess.Popen] = {}
        self.max_concurrent = max(1, int(max_concurrent or 1))
        self.workers = 0        # 실제로 띄운 워커 스레드 수
        self.closed = False     # 파드가 지워지면 True — 스레드가 스스로 끝난다


pod_runtimes: dict[str, PodRuntime] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_state():
    # 큐 자체는 프로세스가 죽으면 사라지지만, 이력 조회는 재시작 후에도 가능하게 기록만 남긴다.
    # 작업 기록은 SQLite(db.py)에 산다 — 메모리 jobs dict를 통째로 맞추되 바뀐 것만 쓴다.
    # DB가 잠깐 안 써져도 워커 스레드가 죽으면 안 되므로 로그만 남기고 넘어간다(바뀐 것은
    # 저장에 성공할 때까지 계속 "바뀐 것"으로 남아서 다음 save_state()가 다시 쓴다).
    with lock:
        try:
            db.save_jobs(jobs)
        except Exception:
            logging.exception("작업 기록을 DB에 저장하지 못했어요")


def load_state():
    # 예전 jobs_state.json이 있으면 한 번만 DB로 가져오고(원본은 .migrated로 보존).
    db.init()
    db.import_legacy_jobs(STATE_FILE)
    jobs.update(db.load_jobs())


def prune_deleted_jobs():
    # 소프트 삭제된 작업 중 DELETED_JOBS_RETENTION개를 넘는 오래된 것들은 이제
    # 완전히 정리한다(레코드 + 워크플로우/CSV 파일). lock을 쥔 채로 호출해야 한다.
    deleted = sorted(
        (job for job in jobs.values() if job.get("deleted")),
        key=lambda job: job.get("deleted_at") or "",
        reverse=True,
    )
    for job in deleted[DELETED_JOBS_RETENTION:]:
        for field in ("workflow_filename", "video_workflow_filename", "csv_filename"):
            filename = job.get(field)
            if filename:
                (JOBS_DIR / filename).unlink(missing_ok=True)
        del jobs[job["id"]]


def load_templates_list() -> list[dict]:
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_templates_map() -> dict[str, dict]:
    return {t["id"]: t for t in load_templates_list()}


# 아래 세 함수는 "기본 파드"에 대한 얇은 껍데기다. 실제 로직은 전부 드라이버에 있고,
# 이 이름들을 남겨 두는 이유는 이 파일의 열두 곳이 이미 이 이름으로 부르고 있기 때문이다
# (파드를 골라 쓰는 것은 파드별 큐를 만드는 P1에서 한꺼번에 정리한다).
def check_comfy_url(url: str, timeout: float = COMFY_CHECK_TIMEOUT) -> bool:
    return check_url(url, timeout)


def configured_comfy_url() -> tuple[str | None, str]:
    """명시적으로 지정된 ComfyUI 주소와 그 출처("setting"/"env"). 어느 쪽에도
    지정돼 있지 않으면 (None, "auto") — 이때만 자동 탐지로 넘어간다."""
    return ComfyUIDriver.configured(pod_registry.default_pod())


def resolve_comfy_url() -> tuple[str | None, bool]:
    """(지금 쓸 주소, 연결 가능 여부) — 요청한 회원의 기본 파드 기준(로그인 없는 내부 호출은 서버 기본 파드)."""
    user = auth.current_user.get()
    pod = default_pod_for_user(user) if user else pod_registry.default_pod()
    if pod is None:
        return None, False
    health = driver_for(pod).health(pod)
    return health["url"], health["ok"]


# ---- ComfyUI 노드/모델 목록 조회 ----------------------------------------------
# ComfyUI의 /object_info는 그 서버에 설치된 모든 노드 타입과, 파일을 고르는 입력
# (체크포인트/LoRA/VAE 등)이 실제로 고를 수 있는 선택지까지 통째로 돌려준다.
# 워크플로우 JSON은 결국 "노드 이름 + 입력값"일 뿐이라, 이 목록만 있으면 업로드된
# 워크플로우가 이 서버에서 돌아갈 수 있는지 미리 검사할 수 있다.
# 커스텀 노드가 많이 깔린 서버에서는 응답이 수 MB까지 커지고 모델 폴더를 훑느라
# 느리기도 해서 짧게 캐싱한다 — 모델을 새로 설치하는 일은 드물고, 필요하면
# refresh=true로 강제로 다시 받아온다.
# ComfyUI에 "설치된 모델 목록" 전용 API는 없어서, 각 종류를 대표하는 로더 노드의
# 입력 선택지를 그대로 읽어 쓴다(그 노드가 없는 서버면 빈 목록).
MODEL_LIST_SOURCES = {
    "checkpoints": ("CheckpointLoaderSimple", "ckpt_name"),
    "diffusion_models": ("UNETLoader", "unet_name"),
    "loras": ("LoraLoader", "lora_name"),
    "vae": ("VAELoader", "vae_name"),
    "text_encoders": ("CLIPLoader", "clip_name"),
    "controlnet": ("ControlNetLoader", "control_net_name"),
    "upscale_models": ("UpscaleModelLoader", "model_name"),
    "clip_vision": ("CLIPVisionLoader", "clip_name"),
    "ultralytics": ("UltralyticsDetectorProvider", "model_name"),
}
# 노드팩이 들여오는 노드 → 노드팩 이름. 이 노드가 없으면 대기 사유를 "노드팩 없음 (이름)"으로 적는다 —
# 노드팩은 파드 시작 때만 설치하므로(ComfyUI 재시작 필요) 작업 도중 받지 않는다.
NODE_PACKS = {
    "FaceDetailer": "ComfyUI-Impact-Pack",
    "UltralyticsDetectorProvider": "ComfyUI-Impact-Subpack",
}


def missing_node_label(class_type: str) -> str:
    pack = NODE_PACKS.get(class_type)
    return f"노드팩 없음 ({pack})" if pack else f"노드 {class_type}"


def is_node_problem(name: str) -> bool:
    """대기 사유 항목이 모델 파일이 아니라 노드·노드팩이면 True — 받기 대상이 아니다."""
    return name.startswith(("노드 ", "노드팩 없음 "))


def object_info_pod(pod_id: str | None) -> dict:
    """object_info를 물어볼 파드를 고른다.

    화면이 파드 안으로 들어간 뒤로("#pod/{id}/builder", "#pod/{id}/lora") "이 파드에
    무엇이 설치돼 있나"를 묻는 것이 정상이라, 부르는 쪽이 파드를 지정한다. 지정이
    없거나 ComfyUI 파드가 아니면 기본 파드로 떨어진다 — 셸 파드처럼 노드 목록이라는
    개념 자체가 없는 워커에 물어봐야 의미가 없기 때문이다."""
    if pod_id == "auto":   # 파드를 정하지 않은 작업 구상 — 어느 파드의 목록도 아니다
        return NO_POD
    user = auth.current_user.get()
    if user is None:   # 로그인 없이 부르는 내부 경로(서버 시작 등)
        if pod_id:
            pod = pod_registry.get_pod(pod_id)
            if pod is not None and pod.get("kind") == pod_registry.DEFAULT_KIND:
                return pod
        return pod_registry.default_pod()
    if pod_id:
        pod = pod_registry.get_pod(pod_id)
        if pod is not None and auth.can_access(user, pod.get("owner_id")) and pod.get("kind") == pod_registry.DEFAULT_KIND:
            return pod
    return default_pod_for_user(user) or NO_POD


def fetch_comfy_object_info(force: bool = False, pod: dict | None = None) -> tuple[str | None, dict | None]:
    """(주소, object_info) — 그 파드가 안 떠 있으면 (주소, None). 캐싱은 드라이버가
    파드별로 한다(파드마다 설치된 노드/모델이 다를 수 있으므로).
    pod를 주지 않으면 기본 파드를 본다."""
    if pod is not None and pod.get("_none"):
        return None, None   # 쓸 파드가 없다 — 서버 자신의 ComfyUI로 떨어지지 않게 한다
    pod = pod or pod_registry.default_pod()
    return driver_for(pod).capabilities(pod, force)


def combo_spec(object_info: dict, class_type: str, field: str) -> list[str] | None:
    """그 노드의 그 입력이 목록에서 고르는 입력(COMBO)이면 선택지 목록(**비어 있을 수 있다** — 새로 만든 파드의
    UNet 목록처럼 "설치된 게 하나도 없음"), 목록형이 아니거나 노드가 없으면 None. ComfyUI는 COMBO를
    [["선택지1", ...], {옵션들}] 또는 새 형식 ["COMBO", {"options": [...]}]로 주고, 그냥 문자열/숫자 입력은
    ["STRING", {...}]처럼 타입 이름을 준다."""
    spec = (object_info.get(class_type) or {}).get("input", {})
    for section in ("required", "optional"):
        entry = (spec.get(section) or {}).get(field)
        if not isinstance(entry, list) or not entry:
            continue
        if isinstance(entry[0], list):
            return [str(v) for v in entry[0]]
        if entry[0] == "COMBO" and len(entry) > 1 and isinstance(entry[1], dict) and isinstance(entry[1].get("options"), list):
            return [str(v) for v in entry[1]["options"]]
    return None


def combo_choices(object_info: dict, class_type: str, field: str) -> list[str]:
    """그 노드의 그 입력이 고를 수 있는 선택지 목록 — 목록형이 아니면 빈 리스트(비어 있는 목록과 구분이 필요하면 combo_spec)."""
    return combo_spec(object_info, class_type, field) or []


def workflow_missing(object_info: dict, workflow: dict) -> tuple[list[str], list[dict], int]:
    """워크플로우가 이 ComfyUI(object_info)에서 돌 수 있는지 — (없는 노드, 없는 모델/설정값, 검사한 노드 수).
    링크나 숫자 입력은 검사 대상이 아니고, 목록에서 고르는 입력(체크포인트/LoRA/샘플러 이름 등)만
    실제 선택지와 대조한다. 노드 자체가 없으면 그 노드의 입력값은 검사할 기준이 없어 건너뛴다."""
    missing_nodes: list[str] = []
    missing_values: list[dict] = []
    checked = 0
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        if not isinstance(class_type, str) or not class_type:
            continue
        checked += 1
        if class_type not in object_info:
            if class_type not in missing_nodes:
                missing_nodes.append(class_type)
            continue
        for field, value in (node.get("inputs") or {}).items():
            if not isinstance(value, str):
                continue
            # 빈 목록도 "없음"이다 — 새로 만든 파드는 UNet 같은 목록이 통째로 비어 있어서, 빈 목록을 "모름"으로
            # 넘기면 없는 모델이 안 잡혀 작업이 ComfyUI에서 400으로 튕겼다(2026-09-29 실제 파드 검사).
            choices = combo_spec(object_info, class_type, field)
            if choices is not None and value not in choices:
                missing_values.append({
                    "node_id": str(node_id),
                    "class_type": class_type,
                    "field": field,
                    "value": value,
                })
    return missing_nodes, missing_values, checked


def annotate_available_on(missing_values: list[dict], user: dict, exclude_pod_id: str | None) -> None:
    """없는 모델마다 "내 다른 파드 중 어디에 있나"를 available_on으로 붙인다(제자리 수정).
    파드가 꺼져 있거나 목록을 못 받으면 그 파드는 조용히 뺀다 — 어디까지나 안내다."""
    if not missing_values:
        return
    candidates = [p for p in visible_pods_for(user)
                  if p.get("kind") == pod_registry.DEFAULT_KIND and p.get("enabled") and p["id"] != exclude_pod_id]

    def load(pod):
        try:
            _, info = fetch_comfy_object_info(False, pod)
            return pod, info
        except Exception:
            return pod, None

    infos = []
    if candidates:
        with ThreadPoolExecutor(max_workers=min(6, len(candidates))) as ex:
            infos = [(pod, info) for pod, info in ex.map(load, candidates) if info]
    reverse_kinds = {src: kind for kind, src in MODEL_LIST_SOURCES.items()}
    for item in missing_values:
        # 등록부(파드와 무관한 기준 데이터)에 받을 주소가 적혀 있으면 함께 알려 준다.
        kind = reverse_kinds.get((item["class_type"], item["field"]))
        entry = model_registry.get_entry(kind, item["value"]) if kind else None
        if entry and (entry["download_url"] or entry["page_url"]):
            item["kind"] = kind
            item["registry"] = {"download_url": entry["download_url"], "page_url": entry["page_url"]}
        item["available_on"] = [
            {"id": pod["id"], "name": pod.get("name") or pod["id"]}
            for pod, info in infos
            if item["value"] in combo_choices(info, item["class_type"], item["field"])
        ]


def default_video_workflow_bytes(template: dict) -> bytes | None:
    """영상 워크플로우를 안 올렸을 때 템플릿 스크립트가 쓰는 내장 기본값(있으면)."""
    if not template.get("optional_video_workflow"):
        return None
    name = "wan22_flf2v" if "flf2v" in str(template.get("script_filename") or "") else "wan22_i2v"
    path = VIDEO_WORKFLOWS_DIR / f"{name}.json"
    try:
        return path.read_bytes()
    except OSError:
        return None


def compute_preflight(pod: dict, user: dict, blobs: list[tuple[str, bytes | None]]) -> dict | None:
    """작업이 실제로 갈 파드에 필요한 노드/모델이 있는지 미리 본다. 파드가 꺼져 있어 확인을 못 하면 None
    (이 앱은 ComfyUI가 꺼진 동안 큐에 쌓아두는 게 정상이라 막지 않고, 실행 직전 워커가 다시 확인한다).
    결과는 안내용이라 작업 등록을 막지는 않는다."""
    _, object_info = fetch_comfy_object_info(False, pod)
    if object_info is None:
        return None
    missing_nodes: list[str] = []
    missing_values: list[dict] = []
    for label, data in blobs:
        if not data:
            continue
        try:
            workflow = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(workflow, dict):
            continue
        nodes, values, _ = workflow_missing(object_info, workflow)
        for n in nodes:
            if n not in missing_nodes:
                missing_nodes.append(n)
        for v in values:
            v["workflow"] = label
            missing_values.append(v)
    annotate_available_on(missing_values, user, pod.get("id"))
    return {
        "pod_id": pod.get("id"),
        "checked_at": now_iso(),
        "missing_nodes": missing_nodes,
        "missing_values": missing_values,
    }


