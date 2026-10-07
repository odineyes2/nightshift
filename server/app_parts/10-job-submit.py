# app.py가 app_parts/*.py를 번호 순서대로 한 네임스페이스에서 실행한다 — 앞 파일의 이름을 import 없이 쓴다.
# 템플릿 id -> 그 템플릿의 "주(main) 참조 종류". 값이 있는 템플릿만 "주 참조 필수"
# 템플릿이다(업로드 시점에 LoadImage 노드 존재를 강제 검증) — csv_batch처럼 참조
# 이미지가 아예 없는 템플릿은 여기 없다. 보조 참조(secondary_kind 옵션)는 있으면
# 좋고 없어도 그만인 선택 기능이라 이 딕셔너리와 무관하게 항상 best-effort다
# (노드가 없으면 실행 스크립트가 경고만 남기고 계속 진행 — MAIN_PROMPT 노드를
# 못 찾았을 때와 같은 관용).
TEMPLATE_PRIMARY_KIND = {
    "depth_batch": "depth",
    "depth_csv_batch": "depth",
    "lineart_batch": "lineart",
    "lineart_csv_batch": "lineart",
}
REF_NODE_REQUIRED_TEMPLATES = set(TEMPLATE_PRIMARY_KIND)

# 주 참조 종류별로 LoadImage 노드 제목 매칭에 쓰는 환경변수 이름 — 템플릿
# 스크립트가 쓰는 것과 정확히 같은 이름이어야 업로드 시점 검증과 실행 시점 동작이
# 일치한다(templates/*_batch.py의 환경변수 문서 참고).
REF_KIND_NODE_TITLE_ENV = {"pose": "POSE_NODE_TITLE", "depth": "DEPTH_NODE_TITLE", "lineart": "LINEART_NODE_TITLE"}
REF_KIND_LABELS = {"pose": "포즈", "depth": "depth", "lineart": "lineart"}

# CSV 템플릿에서 주 참조를 지정하는 컬럼 이름 — 종류 이름을 그대로 컬럼명으로 쓴다.
# (OpenPose는 pose_batch 대신 시드·CSV 템플릿의 pose_image로 옮겼다 — NS-41)
CSV_PRIMARY_REF_COLUMN = {"depth_csv_batch": "depth", "lineart_csv_batch": "lineart"}


def find_ref_load_image_node(workflow: dict, title_substring: str):
    """templates/*_batch.py의 apply_ref_image()류가 실행 시점에 실제로 어떤
    노드를 골라 참조 이미지를 주입할지 업로드 시점에 미리 예측한다 — 각
    스크립트의 find_node()와 정확히 같은 알고리즘이다: 제목에 title_substring이
    포함된 노드가 있으면 class_type과 무관하게 그 노드를 최우선으로 고르고(그래서
    "Load Checkpoint"처럼 우연히 제목에 "Load"가 들어간 LoadImage가 아닌 노드가
    먼저 골라질 수 있다 — 이 경우도 아래에서 "참조 노드 없음"으로 취급해야 함),
    없으면 다른 노드의 입력에 실제로 연결된 LoadImage 노드를 우선으로 고른다."""
    title_substring = (title_substring or "").lower()
    connected = set()
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        for value in node.get("inputs", {}).values():
            if isinstance(value, list) and len(value) == 2:
                connected.add(str(value[0]))
    fallback = None
    fallback_connected = None
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        meta_title = str(node.get("_meta", {}).get("title", "")).lower()
        class_type = node.get("class_type", "")
        if title_substring and title_substring in meta_title:
            return node
        if class_type == "LoadImage":
            if node_id in connected:
                if fallback_connected is None:
                    fallback_connected = node
            elif fallback is None:
                fallback = node
    return fallback_connected or fallback


def validate_workflow_has_ref_node(workflow_bytes: bytes, kind: str):
    # *_batch/*_csv_batch는 주 참조 이미지를 LoadImage 노드에 주입해야 ControlNet이
    # 실제로 동작한다. 이 노드가 없는 워크플로우를 잘못 올리면, 실행 스크립트는
    # stderr에 경고만 남기고 참조 없이 이미지 생성을 계속 진행한다 — 큐에 올리기
    # 전에 미리 걸러서 그런 사고를 막는다. (이 검사는 "주 참조"에만 적용된다 —
    # 보조 참조는 있으면 좋고 없어도 그만인 선택 기능이라 노드가 없어도 업로드를
    # 막지 않고 실행 시점에 경고만 남긴다.)
    try:
        workflow = json.loads(workflow_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise HTTPException(400, f"워크플로우가 올바른 JSON이 아니에요: {e}")
    if not isinstance(workflow, dict):
        raise HTTPException(400, "워크플로우가 올바른 ComfyUI API 형식(JSON 객체)이 아니에요.")

    title_substring = os.environ.get(REF_KIND_NODE_TITLE_ENV[kind], "Load")
    node = find_ref_load_image_node(workflow, title_substring)
    label = REF_KIND_LABELS[kind]
    if node is None or node.get("class_type") != "LoadImage":
        raise HTTPException(
            400,
            f"이 워크플로우에는 {label} 이미지를 넣을 LoadImage 노드가 없어요. "
            f"(제목에 '{title_substring}'가 포함된 노드가 있다면 LoadImage가 아니고, "
            "그런 노드가 아예 없다면 다른 LoadImage 노드도 찾지 못했어요.) 참조 "
            "없이 이미지가 생성되는 사고를 막기 위해 업로드를 거부했어요 — 워크플로우에 "
            "LoadImage 노드를 추가하거나 제목을 확인한 뒤 다시 업로드하세요.",
        )


# input_image_batch/input_image_csv_batch(img2img)와 ipadapter_batch/
# ipadapter_csv_batch(IPAdapter 프리셋) 전용 — pose/depth/lineart처럼
# ref_assets.py의 kind 계층을 쓰지 않으므로(input_assets.py의 평평한 목록)
# TEMPLATE_PRIMARY_KIND와는 별개로 다룬다. 템플릿 id별로 "어떤 제목의 LoadImage
# 노드에 주입하는지"/"CSV의 어느 컬럼을 읽는지"만 다르고 검증 로직은 완전히
# 같아서 하나의 함수 쌍을 재사용한다.
FLAT_IMAGE_TEMPLATES = {
    "input_image_batch": {"node_title_env": "INPUT_IMAGE_NODE_TITLE", "default_title": "input_image", "column": "input_image", "label": "입력 이미지"},
    "input_image_csv_batch": {"node_title_env": "INPUT_IMAGE_NODE_TITLE", "default_title": "input_image", "column": "input_image", "label": "입력 이미지"},
    "ipadapter_batch": {"node_title_env": "IPADAPTER_NODE_TITLE", "default_title": "ipadapter_ref", "column": "ipadapter_ref", "label": "IPAdapter 참조 이미지"},
    "ipadapter_csv_batch": {"node_title_env": "IPADAPTER_NODE_TITLE", "default_title": "ipadapter_ref", "column": "ipadapter_ref", "label": "IPAdapter 참조 이미지"},
}


def validate_workflow_has_flat_image_node(workflow_bytes: bytes, node_title_env: str, default_title: str, label: str):
    try:
        workflow = json.loads(workflow_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise HTTPException(400, f"워크플로우가 올바른 JSON이 아니에요: {e}")
    if not isinstance(workflow, dict):
        raise HTTPException(400, "워크플로우가 올바른 ComfyUI API 형식(JSON 객체)이 아니에요.")

    title_substring = os.environ.get(node_title_env, default_title)
    node = find_ref_load_image_node(workflow, title_substring)
    if node is None or node.get("class_type") != "LoadImage":
        raise HTTPException(
            400,
            f"이 워크플로우에는 {label}를 넣을 LoadImage 노드가 없어요. "
            f"(제목에 '{title_substring}'가 포함된 노드가 있다면 LoadImage가 아니고, "
            "그런 노드가 아예 없다면 다른 LoadImage 노드도 찾지 못했어요.) 이미지 없이 "
            f"실행되는 사고를 막기 위해 업로드를 거부했어요 — 워크플로우 빌더로 만들거나, "
            f"직접 만든 워크플로우라면 LoadImage 노드의 제목을 '{title_substring}'로 맞춰서 "
            "다시 업로드하세요.",
        )


def validate_flat_image_csv_rows(csv_bytes: bytes, column: str):
    # *_csv_batch 공용 검증과 같은 패턴(validate_ref_csv_rows) — CSV 전체를 미리
    # 훑어 그 컬럼 값이 실제로 존재하는 파일인지 확인한다.
    try:
        text = csv_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "CSV가 유효한 UTF-8 텍스트가 아니에요.")

    rows = list(csv.DictReader(io.StringIO(text)))
    errors = []
    for line_no, row in enumerate(rows, start=2):  # 헤더가 1번 줄
        value = (row.get(column) or "").strip()
        if not value:
            continue
        try:
            resolve_input_image(value)
        except InputAssetError as e:
            errors.append(f"{line_no}번째 줄({column}='{value}'): {e}")

    if errors:
        raise HTTPException(400, f"CSV의 {column} 컬럼을 확인하세요.\n" + "\n".join(errors))


def validate_pose_image(workflow_bytes: bytes, csv_bytes, option_value):
    # OpenPose ControlNet(DWPreprocessor)이 든 워크플로우는 포즈 이미지가 꼭 있어야
    # 한다 — 없으면 ControlNet이 빈 그림을 따르는 작업이 그대로 돈다. 시드 반복은
    # pose_image 옵션, CSV는 모든 행의 pose_image 열 또는 옵션 기본값이 있어야 한다.
    try:
        workflow = json.loads(workflow_bytes)
    except ValueError:
        return
    if not isinstance(workflow, dict) or not any(
        isinstance(n, dict) and n.get("class_type") == "DWPreprocessor" for n in workflow.values()
    ):
        return
    default = str(option_value or "").strip()
    if default:
        try:
            resolve_input_image(default)
        except InputAssetError as e:
            raise HTTPException(400, f"포즈 이미지를 확인하세요: {e}")
    if csv_bytes is not None:
        validate_flat_image_csv_rows(csv_bytes, "pose_image")
        if not default:
            rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
            missing = [str(i) for i, row in enumerate(rows, start=2) if not (row.get("pose_image") or "").strip()]
            if missing:
                raise HTTPException(
                    400,
                    "OpenPose 워크플로우는 포즈 이미지가 필요해요 — 포즈 이미지 옵션을 고르거나 "
                    f"CSV pose_image 열을 채워 주세요 ({', '.join(missing[:10])}번째 줄).",
                )
    elif not default:
        raise HTTPException(400, "OpenPose 워크플로우는 포즈 이미지가 필요해요 — 포즈 이미지를 골라 주세요.")


_recent_user_stores: dict[tuple[str, int], "RecentFileStore"] = {}


def recent_store(kind: str, user: dict) -> "RecentFileStore":
    """"최근 워크플로우/CSV" 저장소 — 관리자는 예전 것 그대로, 일반 회원은 자기 것을 따로 갖는다."""
    base = recent_workflows_store if kind == "workflows" else recent_csvs_store
    if auth.is_admin(user):
        return base
    key = (kind, user["id"])
    with lock:
        store = _recent_user_stores.get(key)
        if store is None:
            folder = base.dir_path / "users" / str(user["id"])
            folder.mkdir(parents=True, exist_ok=True)
            store = RecentFileStore(folder, base.state_path.with_name(f"{base.state_path.stem}_u{user['id']}.json"),
                                    base.retention)
            store.load()
            _recent_user_stores[key] = store
    return store


@app.get("/api/recent-workflows")
def list_recent_workflows(request: Request):
    # "새 작업 추가"의 워크플로우 슬롯 옆 "최근 워크플로우" 버튼이 호출한다.
    store = recent_store("workflows", me(request))
    return {"workflows": store.list_meta(), "retention": store.retention}


@app.get("/api/recent-workflows/{workflow_id}")
def get_recent_workflow(workflow_id: str, request: Request):
    store = recent_store("workflows", me(request))
    entry = store.get(workflow_id)
    if entry is None:
        raise HTTPException(404, "해당 워크플로우를 찾을 수 없어요.")
    path = store.dir_path / entry["stored_filename"]
    if not path.exists():
        raise HTTPException(404, "워크플로우 파일이 서버에 없어요.")
    return Response(content=path.read_text(encoding="utf-8"), media_type="application/json")


@app.delete("/api/recent-workflows/{workflow_id}")
def delete_recent_workflow(workflow_id: str, request: Request):
    if not recent_store("workflows", me(request)).delete(workflow_id):
        raise HTTPException(404, "해당 워크플로우를 찾을 수 없어요.")
    return {"ok": True}


@app.get("/api/recent-csvs")
def list_recent_csvs(request: Request):
    # "새 작업 추가"의 CSV 슬롯 옆 "최근 CSV" 버튼이 호출한다.
    store = recent_store("csvs", me(request))
    return {"csvs": store.list_meta(), "retention": store.retention}


@app.get("/api/recent-csvs/{csv_id}")
def get_recent_csv(csv_id: str, request: Request):
    store = recent_store("csvs", me(request))
    entry = store.get(csv_id)
    if entry is None:
        raise HTTPException(404, "해당 CSV를 찾을 수 없어요.")
    path = store.dir_path / entry["stored_filename"]
    if not path.exists():
        raise HTTPException(404, "CSV 파일이 서버에 없어요.")
    return Response(content=path.read_text(encoding="utf-8"), media_type="text/csv")


@app.delete("/api/recent-csvs/{csv_id}")
def delete_recent_csv(csv_id: str, request: Request):
    if not recent_store("csvs", me(request)).delete(csv_id):
        raise HTTPException(404, "해당 CSV를 찾을 수 없어요.")
    return {"ok": True}


def resolve_template(template_id) -> dict:
    if not isinstance(template_id, str) or not template_id:
        raise HTTPException(400, "template_id는 필수예요.")
    template = load_templates_map().get(template_id)
    if template is None:
        raise HTTPException(400, "존재하지 않는 템플릿이에요.")
    return template


async def create_job(
    template: dict,
    workflow_bytes: bytes,
    workflow_filename: str,
    csv_bytes: bytes | None,
    csv_filename: str | None,
    raw_options: dict,
    pod_id: str | None = None,
    video_workflow_bytes: bytes | None = None,
    video_workflow_filename: str | None = None,
    project_id: int | None = None,
    user: dict | None = None,
    start_paused: bool = False,
) -> dict:
    # POST /api/upload(사람이 브라우저에서 파일 첨부)와 POST /api/jobs(LLM 등
    # 프로그램이 JSON으로 호출)가 공유하는 실제 잡 생성 로직 — 두 경로 모두
    # 워크플로우/CSV를 이미 bytes로, 옵션을 이미 {name: 원본 문자열} 형태로
    # 만들어서 넘겨준다. 그 앞단(멀티파트 폼 파싱 vs JSON 파싱)만 다르다.
    if user is None:
        raise HTTPException(401, "로그인이 필요해요.")
    # 파드는 정해도 되고 안 정해도 된다. 안 정하면 작업이 파드 없이 대기 큐에 들어가고, 스케줄러가 필요한 모델을 갖춘
    # 파드가 살아 있을 때 배정한다(파드가 하나도 없어도 작업을 만들어 둘 수 있다). 작업은 그 작업을 만든 회원의
    # 파드에서만 돈다.
    pod = None
    if pod_id:
        pod = pod_registry.get_pod(pod_id)
        if pod is None or pod.get("owner_id") != user["id"]:
            raise HTTPException(400, "없는 워커예요.")
        if not pod.get("enabled"):
            raise HTTPException(400, f"'{pod['name']}' 워커는 지금 사용 안 함 상태예요.")

    # 프로젝트는 파드와 무관하게 작업을 묶는다. 없으면 "미분류"(project_id=None). 자기 프로젝트만 쓸 수 있다.
    if project_id is not None and not project_store.project_exists(project_id, owner_id=user["id"]):
        raise HTTPException(400, "없는 프로젝트예요.")

    # 템플릿이 특정 워커 종류 전용이면(셸 명령은 셸 파드에서만 뜻이 있다) 여기서 막는다.
    # 안 막으면 ComfyUI 파드에 셸 작업이 들어가 조용히 엉뚱하게 돈다.
    allowed_kinds = template.get("pod_kinds")
    if pod is not None and allowed_kinds and pod["kind"] not in allowed_kinds:
        raise HTTPException(
            400,
            f"'{template['label']}' 템플릿은 {', '.join(allowed_kinds)} 종류의 워커에서만 쓸 수 있어요 "
            f"(고른 워커 '{pod['name']}'는 {pod['kind']}).",
        )
    # 반대 방향도 막는다 — ComfyUI용 템플릿(대부분)은 셸 파드로 보낼 수 없다.
    if pod is not None and not allowed_kinds and pod["kind"] != pod_registry.DEFAULT_KIND:
        raise HTTPException(
            400,
            f"'{template['label']}' 템플릿은 {pod_registry.DEFAULT_KIND} 워커용이에요 "
            f"(고른 워커 '{pod['name']}'는 {pod['kind']}).",
        )

    with lock:
        active_count = sum(
            1 for j in jobs.values()
            if j["status"] in ("pending", "queued", "running") and not j.get("deleted")
            and j.get("owner_id") == user["id"]
        )
    if active_count >= MAX_ACTIVE_JOBS:
        raise HTTPException(
            429,
            f"대기/실행 중인 작업이 이미 {MAX_ACTIVE_JOBS}개예요 — 너무 많이 쌓였어요. "
            "완료된 작업을 정리하거나 잠시 후 다시 시도하세요.",
        )

    template_id = template["id"]
    requires_csv = bool(template.get("requires_csv"))
    if requires_csv and not csv_bytes:
        raise HTTPException(400, "이 템플릿은 csv가 필요해요.")

    primary_kind = TEMPLATE_PRIMARY_KIND.get(template_id)
    primary_csv_column = CSV_PRIMARY_REF_COLUMN.get(template_id)
    if primary_csv_column and csv_bytes is not None:
        validate_ref_csv_rows(csv_bytes, primary_kind, primary_csv_column)

        # 보조 참조(CSV 템플릿 전용) — secondary_kind가 "none"이 아니면 CSV의
        # secondary_ref/secondary_char_no 컬럼도 미리 검증한다. 아직 옵션을
        # coerce하기 전이라 원본 값을 직접 읽는다(정식 검증은 select 타입
        # 처리에서 한 번 더 함 — 여기서는 "CSV 컬럼 검사가 필요한지" 판단용으로만
        # 가볍게 읽는다).
        secondary_kind_raw = raw_options.get("secondary_kind")
        if secondary_kind_raw in REF_KINDS:
            validate_ref_csv_rows(csv_bytes, secondary_kind_raw, "secondary_ref", "secondary_char_no")

    if primary_kind and workflow_bytes is not None:
        needs_ref_node = True
        if primary_csv_column:
            needs_ref_node = csv_bytes is not None and csv_has_ref_value(csv_bytes, primary_csv_column)
        if needs_ref_node:
            validate_workflow_has_ref_node(workflow_bytes, primary_kind)

    flat_image_spec = FLAT_IMAGE_TEMPLATES.get(template_id)
    if flat_image_spec and workflow_bytes is not None:
        if csv_bytes is not None:
            validate_flat_image_csv_rows(csv_bytes, flat_image_spec["column"])
        validate_workflow_has_flat_image_node(
            workflow_bytes, flat_image_spec["node_title_env"], flat_image_spec["default_title"], flat_image_spec["label"],
        )

    if workflow_bytes is not None:
        validate_pose_image(workflow_bytes, csv_bytes, raw_options.get("pose_image"))

    # comfy_model 옵션(체크포인트/LoRA 드롭다운)을 쓰는 템플릿이면 설치 목록으로
    # 값을 검증해야 한다. coerce_option은 동기 함수라, 여기서 미리 스레드로 받아
    # 캐시를 채워둔다 — 안 그러면 그 안의 ComfyUI 조회가 이벤트 루프를 막는다.
    # 못 받아오면(꺼져 있음 등) coerce_option이 검증을 건너뛴다.
    if pod is not None and any(o.get("type") == "comfy_model" for o in template.get("options", [])):
        try:
            await asyncio.to_thread(fetch_comfy_object_info, False, pod)
        except Exception:
            pass

    options = {}
    for option in template.get("options", []):
        raw = raw_options.get(option["name"])
        # 파드를 안 정했으면 설치 목록으로 검증할 기준이 없다 — 값은 그대로 받고, 스케줄러가 배정할 때 그 파드에 있는지 본다.
        options[option["name"]] = coerce_option(option, raw, options, pod if pod is not None else NO_POD)

    job_id = str(uuid.uuid4())[:8]

    # 파드 스냅샷용 GPU/시간당 비용 — 대시보드가 이미 주기적으로 조회해 캐시해둔 값을
    # 쓰므로 보통 네트워크를 안 탄다. 캐시가 비어 있어도 작업 추가를 오래 붙잡지 않도록
    # 짧게만 기다리고, 못 얻으면 그냥 비워둔다(비용 집계에서 "모름"으로 남는다).
    pod_gpu = pod_cost_per_hr = None
    try:
        if pod is None:
            raise RuntimeError("워커 없음")
        info = await asyncio.wait_for(
            asyncio.to_thread(runpod_api.get_runpod_info, pod.get("url") or ""), timeout=3)
        if info:
            pod_gpu = info.get("gpu_type")
            cost = info.get("cost_per_hr")
            pod_cost_per_hr = float(cost) if isinstance(cost, (int, float)) else None
    except Exception:
        pass

    # 워크플로우가 필요 없는 템플릿도 있다(셸 명령처럼 ComfyUI를 아예 안 쓰는 것들).
    workflow_dest_name = None
    if workflow_bytes is not None:
        workflow_dest_name = f"{job_id}_{workflow_filename}"
        (JOBS_DIR / workflow_dest_name).write_bytes(workflow_bytes)
        recent_store("workflows", user).record(workflow_filename, workflow_bytes)

    csv_dest_name = None
    csv_original_name = None
    if requires_csv:
        csv_dest_name = f"{job_id}_{csv_filename}"
        (JOBS_DIR / csv_dest_name).write_bytes(csv_bytes)
        csv_original_name = csv_filename
        recent_store("csvs", user).record(csv_filename, csv_bytes)

    # img2video 복합 템플릿의 "영상 생성 워크플로우"는 완전히 선택 — 안 올리면
    # 템플릿 스크립트가 nightshift 내장 기본값(wan22_i2v.json/wan22_flf2v.json)을
    # 그대로 쓴다. 다른 템플릿들은 애초에 이 값을 보내지 않으므로 항상 None.
    video_workflow_dest_name = None
    if video_workflow_bytes is not None:
        video_workflow_dest_name = f"{job_id}_video_{video_workflow_filename}"
        (JOBS_DIR / video_workflow_dest_name).write_bytes(video_workflow_bytes)
        recent_store("workflows", user).record(video_workflow_filename, video_workflow_bytes)

    preflight = None
    if pod is not None and pod["kind"] == pod_registry.DEFAULT_KIND and workflow_bytes is not None:
        blobs = [("워크플로우", workflow_bytes),
                 ("영상 워크플로우", video_workflow_bytes if video_workflow_bytes is not None
                  else default_video_workflow_bytes(template))]
        try:
            preflight = await asyncio.wait_for(asyncio.to_thread(compute_preflight, pod, user, blobs), timeout=10)
        except Exception:
            preflight = None

    with lock:
        jobs[job_id] = {
            "id": job_id,
            "template_id": template_id,
            "template_label": template["label"],
            "script_filename": template["script_filename"],
            "options": options,
            "workflow_filename": workflow_dest_name,
            "workflow_original_name": workflow_filename if workflow_dest_name else None,
            "video_workflow_filename": video_workflow_dest_name,
            "video_workflow_original_name": video_workflow_filename if video_workflow_dest_name else None,
            "csv_filename": csv_dest_name,
            "csv_original_name": csv_original_name,
            "status": "pending",
            "queued_at": now_iso(),
            "started_at": None,
            "finished_at": None,
            "returncode": None,
            "progress": None,
            "deleted": False,
            "deleted_at": None,
            "pod_id": pod["id"] if pod is not None else None,
            # 파드를 정해서 만든 작업은 그 파드에서만 돈다(스케줄러도 다른 파드로 보내지 않는다).
            "pinned_pod_id": pod["id"] if pod is not None else None,
            "waiting_reason": None,
            "project_id": project_id,
            "owner_id": user["id"],
            "preflight": preflight,
            # 파드는 일시적이라(지워지고 다시 안 쓴다) 나중에 "어디서 돌았나/얼마 들었나"를
            # 볼 수 있게 이 시점의 정보를 job에 찍어둔다. 파드를 안 정했으면 배정될 때 채운다.
            "pod_name": pod.get("name") if pod is not None else None,
            "pod_kind": pod.get("kind") if pod is not None else None,
            "pod_gpu": pod_gpu,
            "pod_cost_per_hr": pod_cost_per_hr,
        }
        # start_paused(브라우저의 "New job" 모달이 항상 켠다)면 pod_id/auto_run과
        # 무관하게 "pending"(일시정지) 그대로 둔다 — 대기 칸에 카드만 쌓아두고,
        # 사람이 카드의 ▶ 시작을 직접 눌러야 돈다. LLM/curl이 쓰는 POST /api/jobs는
        # 이 인자를 안 넘기므로(기본 False) 예전처럼 바로 큐에 들어가는 게 그대로다.
        if start_paused:
            auto_queued = False
        elif pod is None:
            # 파드 없이 만든 작업은 곧장 대기 큐로 간다("보내기") — 스케줄러가 갖춰진 파드를 찾는다.
            jobs[job_id]["status"] = "queued"
            jobs[job_id]["waiting_reason"] = "워커를 찾는 중이에요"
            auto_queued = False
        else:
            # 자동 실행 모드("▶ 시작"이 켜져 있는 동안)면 대기 목록에 머무르지 않고
            # 바로 그 파드의 실행 큐에 넣는다 — 그래야 켜놓은 동안 새로 추가하는 작업이
            # 계속 이어서 처리된다. 자동 실행은 파드마다 따로 켜고 끈다.
            rt = pod_runtimes.get(pod["id"])
            auto_queued = bool(rt and rt.auto_run)
            if auto_queued:
                jobs[job_id]["status"] = "queued"
    save_state()
    if start_paused:
        pass
    elif pod is None:
        poke_scheduler()
    elif auto_queued:
        dispatch_job(job_id, pod["id"])
    return jobs[job_id]


@app.post("/api/upload")
async def upload(request: Request):
    user = me(request)
    form = await request.form()

    template = resolve_template(form.get("template_id"))

    # 워크플로우가 필요 없는 템플릿(셸 명령 등)은 첨부를 요구하지 않는다.
    needs_workflow = template.get("requires_workflow", True)
    workflow = form.get("workflow")
    if needs_workflow and (not isinstance(workflow, UploadFile) or not workflow.filename
                           or not workflow.filename.endswith(".json")):
        raise HTTPException(400, "워크플로우는 json 파일만 업로드할 수 있어요.")

    requires_csv = bool(template.get("requires_csv"))
    csv_file = form.get("csv")
    if requires_csv and (not isinstance(csv_file, UploadFile) or not csv_file.filename or not csv_file.filename.endswith(".csv")):
        raise HTTPException(400, "이 템플릿은 csv 파일이 필요해요.")

    # 영상 생성 워크플로우는 완전히 선택(img2video 복합 템플릿에서만 의미가 있고,
    # 안 올리면 템플릿이 내장 기본값을 씀) — 있으면 .json인지만 확인한다.
    video_workflow = form.get("video_workflow")
    has_video_workflow = isinstance(video_workflow, UploadFile) and bool(video_workflow.filename)
    if has_video_workflow and not video_workflow.filename.endswith(".json"):
        raise HTTPException(400, "영상 생성 워크플로우는 json 파일만 업로드할 수 있어요.")

    # UploadFile은 한 번만 읽을 수 있으므로, 검증에도 쓰고 저장에도 쓸 수 있게
    # 여기서 미리 한 번만 읽어둔다.
    workflow_bytes = await workflow.read() if needs_workflow else None
    csv_bytes = await csv_file.read() if requires_csv else None
    video_workflow_bytes = await video_workflow.read() if has_video_workflow else None

    raw_options = {}
    for option in template.get("options", []):
        value = form.get(option["name"])
        raw_options[option["name"]] = value if isinstance(value, str) else None

    pod_id = form.get("pod_id")
    return await create_job(
        template,
        workflow_bytes,
        workflow.filename if needs_workflow else None,
        csv_bytes,
        csv_file.filename if requires_csv else None,
        raw_options,
        pod_id if isinstance(pod_id, str) and pod_id.strip() else None,
        video_workflow_bytes,
        video_workflow.filename if has_video_workflow else None,
        parse_project_id(form.get("project_id")),
        user=user,
        start_paused=form.get("start_paused") == "1",
    )


def parse_project_id(value) -> int | None:
    """요청에서 온 project_id(숫자/숫자 문자열/빈 값)를 int 또는 None(미분류)으로."""
    if value is None or value == "" or value == "null":
        return None
    if isinstance(value, bool):
        raise HTTPException(400, "project_id는 숫자여야 해요.")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, "project_id는 숫자여야 해요.")


@app.post("/api/jobs")
async def create_job_from_json(request: Request):
    # /api/upload과 완전히 같은 파이프라인(create_job)을 파일 첨부 없이 JSON
    # 바디로 쓸 수 있게 한 것. 사람은 브라우저에서 파일을 고르지만, curl이나
    # LLM처럼 프로그램으로 호출하는 쪽엔 multipart/form-data보다 JSON이 훨씬
    # 다루기 쉽다 — 워크플로우는 POST /api/build-workflow로 만든 걸 그대로
    # 넣거나 직접 준 JSON을 쓰면 되고, CSV는 원문 문자열 그대로 준다(csv_batch.
    # sample.csv 같은 형식). 큐에 실제로 등록된다는 점은 /api/upload와 동일하다
    # — 미리보기가 필요하면 POST /api/validate-workflow를 먼저 불러볼 것.
    user = me(request)
    try:
        body = json.loads(await request.body())
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "유효한 JSON이 아니에요.")
    if not isinstance(body, dict):
        raise HTTPException(400, "요청 본문이 JSON 객체여야 해요.")

    template = resolve_template(body.get("template_id"))

    needs_workflow = template.get("requires_workflow", True)
    workflow = body.get("workflow")
    workflow_filename = None
    workflow_bytes = None
    if needs_workflow or workflow is not None:
        if not isinstance(workflow, dict):
            raise HTTPException(400, "workflow는 JSON 객체(워크플로우 자체)여야 해요.")
        workflow_filename = body.get("workflow_filename") or "workflow.json"
        if not isinstance(workflow_filename, str) or not workflow_filename.endswith(".json"):
            raise HTTPException(400, "workflow_filename은 .json으로 끝나야 해요.")
        workflow_bytes = json.dumps(workflow).encode("utf-8")

    csv_text = body.get("csv")
    csv_bytes = None
    csv_filename = None
    if csv_text is not None:
        if not isinstance(csv_text, str):
            raise HTTPException(400, "csv는 CSV 원문을 담은 문자열이어야 해요.")
        csv_filename = body.get("csv_filename") or "data.csv"
        if not isinstance(csv_filename, str) or not csv_filename.endswith(".csv"):
            raise HTTPException(400, "csv_filename은 .csv로 끝나야 해요.")
        csv_bytes = csv_text.encode("utf-8")

    raw_options_in = body.get("options") or {}
    if not isinstance(raw_options_in, dict):
        raise HTTPException(400, "options는 JSON 객체여야 해요.")
    raw_options = {k: (None if v is None else str(v)) for k, v in raw_options_in.items()}

    # 영상 생성 워크플로우는 완전히 선택(img2video 복합 템플릿 전용) — 안 주면
    # 템플릿이 내장 기본값을 쓴다.
    video_workflow = body.get("video_workflow")
    video_workflow_filename = None
    video_workflow_bytes = None
    if video_workflow is not None:
        if not isinstance(video_workflow, dict):
            raise HTTPException(400, "video_workflow는 JSON 객체(워크플로우 자체)여야 해요.")
        video_workflow_filename = body.get("video_workflow_filename") or "video_workflow.json"
        if not isinstance(video_workflow_filename, str) or not video_workflow_filename.endswith(".json"):
            raise HTTPException(400, "video_workflow_filename은 .json으로 끝나야 해요.")
        video_workflow_bytes = json.dumps(video_workflow).encode("utf-8")

    pod_id = body.get("pod_id")
    return await create_job(template, workflow_bytes, workflow_filename, csv_bytes, csv_filename,
                            raw_options, pod_id if isinstance(pod_id, str) and pod_id.strip() else None,
                            video_workflow_bytes, video_workflow_filename,
                            parse_project_id(body.get("project_id")), user=user)


def start_pods(pod_ids: list[str]) -> int:
    """그 파드들의 자동 실행 모드를 켜고, 그 파드로 배정된 대기 작업을 전부 큐에 넣는다.

    배정된 파드가 **사라졌거나 꺼져 있는** 작업만 켜는 파드 중 첫 번째로 되돌린다 —
    큐에 못 들어가 영영 안 도는 작업이 생기면 안 되므로. 반대로 배정된 파드가 멀쩡히
    살아 있으면 그건 남의 몫이라 손대지 않는다. 예전에는 "파드를 하나만 켤 때"를
    "다른 파드는 안 쓴다는 뜻"으로 읽고 전부 끌어왔는데, 작업 화면이 파드 안으로
    들어가면서 그 화면의 ▶ 시작이 늘 파드 하나만 켜게 됐다 — 그 규칙이 남아 있으면
    파드 A의 작업 화면에서 시작을 눌렀을 뿐인데 파드 B의 대기 작업이 A로 끌려온다.
    """
    targets = set(pod_ids)
    if not targets:
        return 0
    pod_owner = {pid: (pod_registry.get_pod(pid) or {}).get("owner_id") for pid in pod_ids}
    templates = load_templates_map()
    with lock:
        for pod_id in targets:
            rt = pod_runtimes.get(pod_id)
            if rt is not None:
                rt.auto_run = True
        # deleted도 함께 확인해야 한다 — delete_job()은 소프트 삭제라 status를
        # "pending"으로 그대로 둔 채 deleted=True만 표시하므로, 이 필터가 없으면
        # 삭제된(그래서 화면에는 안 보이는) 작업이 여기서 다시 주워져 실행 큐에
        # 들어가는 사고가 난다.
        pending = sorted(
            (j for j in jobs.values() if j["status"] == "pending" and not j.get("deleted")),
            key=lambda j: j["queued_at"],
        )
        started = []
        for job in pending:
            assigned = job.get("pod_id")
            if not assigned:
                continue   # 파드를 안 정한 작업은 스케줄러가 갖춰진 파드를 찾아 배정한다(start_queue가 대기 큐로 보낸다)
            if assigned not in targets:
                owner = pod_registry.get_pod(assigned) if assigned else None
                if owner is not None and owner.get("enabled"):
                    continue   # 그 파드가 살아 있다 — 그쪽을 켤 때 돈다
                # 갈 곳이 없어진 작업. 같은 주인의 파드로만, 그리고 되돌릴 파드의 종류가 맞을 때만 옮긴다 —
                # 남의 파드로 옮기면 안 되고, 셸 작업을 ComfyUI 파드에 넣으면(그 반대도) 조용히 엉뚱하게 돈다.
                mine = [pid for pid in pod_ids if pod_owner.get(pid) == job.get("owner_id")]
                if not mine:
                    continue
                fallback = mine[0]
                fallback_kind = (pod_registry.get_pod(fallback) or {}).get("kind")
                allowed = (templates.get(job["template_id"], {}).get("pod_kinds")
                           or [pod_registry.DEFAULT_KIND])
                if fallback_kind not in allowed:
                    continue
                assigned = fallback
                job["pod_id"] = assigned
            job["status"] = "queued"
            started.append((job["id"], assigned))
    save_state()
    for job_id, pod_id in started:
        dispatch_job(job_id, pod_id)
    return len(started)


