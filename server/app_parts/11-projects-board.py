# app.py가 app_parts/*.py를 번호 순서대로 한 네임스페이스에서 실행한다 — 앞 파일의 이름을 import 없이 쓴다.
# ---- 프로젝트 ------------------------------------------------------------------------
# 프로젝트는 파드와 무관하게 작업(job)과 결과물(asset)을 묶는다. project_id가 없는 것은
# "미분류". 프로젝트를 지워도 그 안의 작업/결과물은 지워지지 않고 미분류로 돌아온다.

def _clean_project_fields(body: dict, creating: bool) -> dict:
    fields: dict = {}
    if "name" in body or creating:
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(400, "프로젝트 이름이 필요해요.")
        fields["name"] = name.strip()
    if "description" in body:
        if not isinstance(body["description"], str):
            raise HTTPException(400, "description은 문자열이어야 해요.")
        fields["description"] = body["description"]
    if "defaults" in body:
        if not isinstance(body["defaults"], dict):
            raise HTTPException(400, "defaults는 JSON 객체여야 해요.")
        fields["defaults"] = body["defaults"]
    if "archived" in body:
        fields["archived"] = bool(body["archived"])
    if "is_mature" in body:
        fields["is_mature"] = bool(body["is_mature"])
    if "cover_asset_id" in body:
        cover = body["cover_asset_id"]
        if cover is not None and (not isinstance(cover, int) or isinstance(cover, bool)):
            raise HTTPException(400, "cover_asset_id는 숫자 또는 null이어야 해요.")
        fields["cover_asset_id"] = cover
    return fields


def project_or_404(user: dict, project_id: int) -> dict:
    project = project_store.get_project(project_id)
    if project is None or not auth.can_access(user, project.get("owner_id")):
        raise HTTPException(404, "없는 프로젝트예요.")
    return project


@app.get("/api/projects")
def list_projects_api(request: Request, include_archived: bool = False):
    user = me(request)
    scope = job_scope(user)
    projects = project_store.list_projects(include_archived, owner_id=scope)
    if scope is None:   # 관리자에게는 누구 프로젝트인지 알려 준다
        names = auth.usernames()
        projects = [{**p, "owner_name": names.get(p.get("owner_id"))} for p in projects]
    return {"projects": projects, "unassigned": project_store.unassigned_summary(owner_id=scope)}


@app.post("/api/projects")
async def create_project_api(request: Request):
    user = me(request)
    body = await read_json_object(request, allow_empty=False)
    fields = _clean_project_fields(body, creating=True)
    return project_store.create_project(fields["name"], fields.get("description", ""), fields.get("defaults"),
                                        owner_id=user["id"], is_mature=fields.get("is_mature", False))


@app.get("/api/projects/{project_id}")
def get_project_api(project_id: int, request: Request):
    return project_or_404(me(request), project_id)


@app.patch("/api/projects/{project_id}")
async def update_project_api(project_id: int, request: Request):
    project_or_404(me(request), project_id)
    body = await read_json_object(request, allow_empty=False)
    fields = _clean_project_fields(body, creating=False)
    if fields.get("cover_asset_id") is not None and not project_store.cover_candidate_ok(fields["cover_asset_id"], project_id):
        raise HTTPException(400, "이 프로젝트에 들어 있는 이미지만 대표 이미지로 고를 수 있어요.")
    project = project_store.update_project(project_id, fields)
    if project is None:
        raise HTTPException(404, "없는 프로젝트예요.")
    return project


@app.delete("/api/projects/{project_id}")
def delete_project_api(project_id: int, request: Request):
    project_or_404(me(request), project_id)
    if not project_store.delete_project(project_id):
        raise HTTPException(404, "없는 프로젝트예요.")
    # DB에서는 FK가 알아서 미분류로 돌렸지만 메모리 jobs dict는 그대로라 같이 맞춘다.
    with lock:
        for job in jobs.values():
            if job.get("project_id") == project_id:
                job["project_id"] = None
    save_state()
    return {"ok": True}


BOARD_NODE_KINDS = ("image", "video", "text", "job", "frame", "gen")   # frame = 묶음 틀(제목은 text), gen = 생성 카드
BOARD_ACTIVE_JOB_STATUSES = ("pending", "queued", "running")   # 화면이 작업 카드를 계속 새로 받는 상태


def _board_node_or_404(project_id: int, node_id: int) -> None:
    """그 노드가 이 프로젝트 것인지 확인 — project_or_404가 이미 프로젝트 소유권을
    확인했으므로, 여기서는 node_id가 그 project_id 밑에 실제로 있는지만 본다."""
    if board_store.node_project_id(node_id) != project_id:
        raise HTTPException(404, "없는 카드예요.")


# ---- 보드 생성 카드의 프리셋(회원별) ----
# 새 작업 폼에서 평소처럼 설정한 뒤 "보드 프리셋으로 저장"으로 만든다. 보드의 생성 카드는 이걸 꺼내
# 입력 이미지 칸(입구)을 선으로 채우고, 꺼내 둔 옵션만 고쳐 실행한다.
BOARD_PRESET_EXPOSABLE = ("textarea", "number", "number_optional", "select")   # 카드에서 바로 고칠 수 있는 옵션 타입
BOARD_PRESET_SLOT_TYPES = ("input_image", "input_image_optional")               # 보드에서 선으로 채우는 입구
BOARD_PRESET_MAX_WORKFLOW_BYTES = 5 * 1024 * 1024
BOARD_PRESET_MAX_EXPOSED = 12


def _board_preset_payload(body: dict) -> dict:
    """프리셋 저장 요청을 템플릿 기준으로 검증해 저장할 값으로 만든다(새로 만들기·통째로 다시 저장 공용)."""
    name = str(body.get("name") or "").strip()
    if not name or len(name) > 60:
        raise HTTPException(400, "프리셋 이름은 1~60자로 적어 주세요.")
    template = resolve_template(body.get("template_id"))
    if template.get("requires_csv"):
        raise HTTPException(400, "CSV로 여러 줄을 도는 템플릿은 보드 프리셋으로 쓸 수 없어요.")
    workflow = body.get("workflow")
    if template.get("requires_workflow", True):
        if not isinstance(workflow, dict):
            raise HTTPException(400, "이 템플릿은 워크플로우(JSON 객체)가 필요해요.")
    elif workflow is not None and not isinstance(workflow, dict):
        raise HTTPException(400, "workflow는 JSON 객체여야 해요.")
    video_workflow = body.get("video_workflow") if template.get("optional_video_workflow") else None
    if video_workflow is not None and not isinstance(video_workflow, dict):
        raise HTTPException(400, "video_workflow는 JSON 객체여야 해요.")
    for wf in (workflow, video_workflow):
        if wf is not None and len(json.dumps(wf)) > BOARD_PRESET_MAX_WORKFLOW_BYTES:
            raise HTTPException(400, "워크플로우가 너무 커요.")
    template_opts = {o["name"]: o for o in template.get("options", [])}
    raw_options = body.get("options") or {}
    if not isinstance(raw_options, dict):
        raise HTTPException(400, "options는 JSON 객체여야 해요.")
    # 템플릿에 있는 옵션만, 입구(입력 이미지 칸)는 빼고 — 입구는 보드에서 선으로 채운다.
    options = {k: str(v) for k, v in raw_options.items()
               if k in template_opts and template_opts[k].get("type") not in BOARD_PRESET_SLOT_TYPES and v is not None}
    raw_exposed = body.get("exposed") or []
    if not isinstance(raw_exposed, list):
        raise HTTPException(400, "exposed는 옵션 이름 목록이어야 해요.")
    exposed = []
    for name_ in raw_exposed:
        opt = template_opts.get(name_)
        if opt is None or opt.get("type") not in BOARD_PRESET_EXPOSABLE:
            raise HTTPException(400, f"카드에 꺼낼 수 없는 옵션이에요: {name_}")
        if name_ not in exposed:
            exposed.append(name_)
    if len(exposed) > BOARD_PRESET_MAX_EXPOSED:
        raise HTTPException(400, f"카드에 꺼낼 옵션은 {BOARD_PRESET_MAX_EXPOSED}개까지예요.")
    lora_trigger = str(body.get("lora_trigger") or "").strip()
    if len(lora_trigger) > 200:
        raise HTTPException(400, "LoRA 트리거가 너무 길어요.")
    # 카드에 만들 입구 — 새 작업 창이 워크플로우에 맞춰 보여 주던 입력 이미지 칸들(마법사가 칸 수를
    # 정하는 템플릿 때문). 안 주면 None = 템플릿이 선언한 칸 전부.
    slots = body.get("slots")
    if slots is not None:
        if not isinstance(slots, list):
            raise HTTPException(400, "slots는 입구 이름 목록이어야 해요.")
        slot_names = [o["name"] for o in template.get("options", []) if o.get("type") in BOARD_PRESET_SLOT_TYPES]
        bad = [x for x in slots if x not in slot_names]
        if bad:
            raise HTTPException(400, f"템플릿에 없는 입구예요: {', '.join(map(str, bad))}")
        slots = [x for x in slot_names if x in slots]   # 템플릿 순서대로
    return {"name": name, "template_id": template["id"], "workflow": workflow, "video_workflow": video_workflow,
            "options": options, "exposed": exposed, "slots": slots, "lora_trigger": lora_trigger}


def _board_preset_with_label(preset: dict) -> dict:
    template = load_templates_map().get(preset["template_id"]) or {}
    return {**preset, "template_label": template.get("label") or preset["template_id"],
            "template_missing": not template}


@app.get("/api/board-presets")
def list_board_presets_api(request: Request):
    return {"presets": [_board_preset_with_label(p) for p in board_presets.list_presets(me(request)["id"])]}


@app.get("/api/board-presets/{preset_id}")
def get_board_preset_api(preset_id: int, request: Request):
    preset = board_presets.get_preset(me(request)["id"], preset_id)
    if preset is None:
        raise HTTPException(404, "없는 프리셋이에요.")
    return _board_preset_with_label(preset)


@app.post("/api/board-presets")
async def create_board_preset_api(request: Request):
    user = me(request)
    body = await read_json_object(request, allow_empty=False)
    return _board_preset_with_label(board_presets.create_preset(user["id"], _board_preset_payload(body)))


@app.patch("/api/board-presets/{preset_id}")
async def update_board_preset_api(preset_id: int, request: Request):
    """이름만 주면 이름 바꾸기, template_id까지 주면 내용 전체를 다시 저장(같은 이름으로 덮어쓰기)."""
    user = me(request)
    body = await read_json_object(request, allow_empty=False)
    if set(body) <= {"name"}:
        name = str(body.get("name") or "").strip()
        if not name or len(name) > 60:
            raise HTTPException(400, "프리셋 이름은 1~60자로 적어 주세요.")
        fields = {"name": name}
    else:
        fields = _board_preset_payload(body)
    preset = board_presets.update_preset(user["id"], preset_id, fields)
    if preset is None:
        raise HTTPException(404, "없는 프리셋이에요.")
    return _board_preset_with_label(preset)


@app.delete("/api/board-presets/{preset_id}")
def delete_board_preset_api(preset_id: int, request: Request):
    if not board_presets.delete_preset(me(request)["id"], preset_id):
        raise HTTPException(404, "없는 프리셋이에요.")
    return {"ok": True}


@app.get("/api/projects/{project_id}/board")
def get_board_api(project_id: int, request: Request):
    project_or_404(me(request), project_id)
    return board_store.list_board(project_id)


@app.post("/api/projects/{project_id}/board/nodes")
async def create_board_node_api(project_id: int, request: Request):
    user = me(request)
    project_or_404(user, project_id)
    body = await read_json_object(request, allow_empty=False)
    kind = body.get("kind")
    if kind not in BOARD_NODE_KINDS:
        raise HTTPException(400, f"kind는 {'/'.join(BOARD_NODE_KINDS)} 중 하나여야 해요.")
    asset_path = _board_asset_path_or_error(user, kind, body.get("asset_path"))
    job_id = None
    if kind == "job":
        job_id = str(body.get("job_id") or "").strip()
        if not job_id:
            raise HTTPException(400, "작업 카드는 job_id가 필요해요.")
        job_or_404(user, job_id)   # 자기(admin은 전부) 작업만 — 남의 작업 id를 짐작해 끌어오지 못하게
    data = None
    if kind == "gen":
        try:
            preset_id = int(body.get("preset_id"))
        except (TypeError, ValueError):
            raise HTTPException(400, "생성 카드는 preset_id가 필요해요.")
        preset = board_presets.get_preset(user["id"], preset_id)
        if preset is None:
            raise HTTPException(404, "없는 프리셋이에요.")
        data = _board_gen_data_from_preset(preset)
    try:
        x = float(body.get("x", 0))
        y = float(body.get("y", 0))
        width = float(body["width"]) if body.get("width") is not None else None
        height = float(body["height"]) if body.get("height") is not None else None
    except (TypeError, ValueError):
        raise HTTPException(400, "x/y/width/height는 숫자여야 해요.")
    text = str(body.get("text") or "")
    if kind == "gen":
        # 카드 크기는 입구·꺼낸 옵션 수에 따라 정한다 — 화면은 만들기 전에는 그 수를 모른다.
        width = width or 300
        height = height or _board_gen_height(data)
    return board_store.create_node(project_id, kind, asset_path, text, x, y, width, height, job_id=job_id, data=data)


def _board_gen_height(data: dict) -> float:
    """생성 카드 기본 높이 — 머리 + 입구 줄 + 꺼낸 옵션 칸(글칸은 더 크게) + 아래 실행 줄. index.html의 .board-gen-* 크기와 맞춘다."""
    fields = sum(96 if f.get("type") == "textarea" else 60 for f in data.get("fields", []))
    return max(140, 52 + 30 * len(data.get("slots", [])) + fields + 14 + 46)   # 46 = 아래 실행 줄


def _board_gen_data_from_preset(preset: dict) -> dict:
    """프리셋을 생성 카드 안에 복사할 내용으로 만든다. 입구(slots)와 꺼낸 옵션(fields)의 정의도
    템플릿에서 떠 둔다 — 카드가 템플릿 파일이나 프리셋 없이도 스스로 그려지고 실행되게."""
    template = load_templates_map().get(preset["template_id"])
    if template is None:
        raise HTTPException(400, "프리셋의 템플릿이 이 서버에 없어요.")
    opts = {o["name"]: o for o in template.get("options", [])}
    keep = preset.get("slots")   # None이면(예전 프리셋) 선언된 입구 전부
    slots = [{"name": o["name"], "label": o.get("label") or o["name"], "required": o.get("type") == "input_image"}
             for o in template.get("options", [])
             if o.get("type") in BOARD_PRESET_SLOT_TYPES and (keep is None or o["name"] in keep)]
    fields = []
    for name in preset.get("exposed") or []:
        o = opts.get(name)
        if o is None or o.get("type") not in BOARD_PRESET_EXPOSABLE:
            continue
        f = {"name": name, "label": o.get("label") or name, "type": o["type"]}
        for key in ("choices", "placeholder", "min", "max", "step"):
            if key in o:
                f[key] = o[key]
        fields.append(f)
    values = {f["name"]: str(preset["options"].get(f["name"], opts[f["name"]].get("default", "")) or "")
              for f in fields}
    return {
        "preset_id": preset["id"], "preset_name": preset["name"],
        "template_id": template["id"], "template_label": template.get("label") or template["id"],
        "workflow": preset.get("workflow"), "video_workflow": preset.get("video_workflow"),
        "options": preset.get("options") or {}, "lora_trigger": preset.get("lora_trigger") or "",
        "slots": slots, "fields": fields, "values": values, "runs": [],
    }


def _board_gen_data_or_error(data) -> dict:
    """되살리기로 들어온 생성 카드 내용 확인 — 카드를 지우기 전에 받아 둔 그대로여야 한다. 모양만
    확인하고(실행할 때 create_job이 다시 템플릿 기준으로 검증한다), 모르는 템플릿·CSV 템플릿은 거부."""
    if not isinstance(data, dict):
        raise HTTPException(400, "생성 카드 내용이 없어요.")
    template = load_templates_map().get(data.get("template_id"))
    if template is None or template.get("requires_csv"):
        raise HTTPException(400, "생성 카드의 템플릿을 쓸 수 없어요.")
    for key, typ in (("slots", list), ("fields", list), ("values", dict), ("options", dict), ("runs", list)):
        if not isinstance(data.get(key), typ):
            raise HTTPException(400, f"생성 카드 내용 형식이 맞지 않아요({key}).")
    for key in ("workflow", "video_workflow"):
        if data.get(key) is not None and not isinstance(data[key], dict):
            raise HTTPException(400, f"생성 카드 내용 형식이 맞지 않아요({key}).")
    if len(json.dumps(data)) > BOARD_PRESET_MAX_WORKFLOW_BYTES * 2:
        raise HTTPException(400, "생성 카드 내용이 너무 커요.")
    return data


def _board_asset_path_or_error(user, kind: str, raw) -> str | None:
    """이미지/영상 카드가 가리킬 결과물 경로를 확인한다(텍스트 카드는 None).
    자기 소유의(관리자는 전부) 결과물만 카드로 놓을 수 있다 — 다른 회원의 파일
    경로를 짐작해 끌어오지 못하게 한다. 꼭 "이" 프로젝트 소속일 필요는 없다(다른
    프로젝트의 이미지를 참고 삼아 가져오는 것도 자연스러운 쓰임). 되살리기(restore)도
    같은 확인을 거친다 — 되살리기 요청에 남의 경로를 끼워 넣지 못하게."""
    if kind not in ("image", "video"):
        return None
    asset_path = (raw or "").strip()
    if not asset_path:
        raise HTTPException(400, "이미지/영상 카드는 asset_path가 필요해요.")
    _sync_assets_quietly()
    try:
        asset_meta.get_detail(asset_path, owner_id=auth.owner_scope(user))
    except asset_meta.AssetNotFound:
        raise HTTPException(404, "결과물을 찾을 수 없어요.")
    return asset_path


@app.patch("/api/projects/{project_id}/board/nodes/{node_id}")
async def update_board_node_api(project_id: int, node_id: int, request: Request):
    project_or_404(me(request), project_id)
    _board_node_or_404(project_id, node_id)
    body = await read_json_object(request, allow_empty=False)
    fields = {}
    try:
        for key in ("x", "y", "width", "height"):
            if key in body:
                fields[key] = float(body[key])
    except (TypeError, ValueError):
        raise HTTPException(400, "x/y/width/height는 숫자여야 해요.")
    if "text" in body:
        fields["text"] = str(body["text"] or "")
    if "values" in body:
        # 생성 카드의 꺼낸 옵션 값 — 카드에 꺼내 둔 옵션만, 문자열로. 나머지 내용(프리셋 사본·실행 기록)은 그대로.
        node = board_store.get_node(project_id, node_id)
        if node is None or node["kind"] != "gen" or not isinstance(node.get("data"), dict):
            raise HTTPException(400, "생성 카드가 아니에요.")
        if not isinstance(body["values"], dict):
            raise HTTPException(400, "values는 JSON 객체여야 해요.")
        allowed = {f["name"] for f in node["data"].get("fields", [])}
        unknown = [k for k in body["values"] if k not in allowed]
        if unknown:
            raise HTTPException(400, f"카드에 꺼내지 않은 옵션이에요: {', '.join(unknown)}")
        data = dict(node["data"])
        data["values"] = {**data.get("values", {}), **{k: "" if v is None else str(v) for k, v in body["values"].items()}}
        fields["data"] = data
    node = board_store.update_node(node_id, fields)
    if node is None:
        raise HTTPException(404, "없는 카드예요.")
    return node


@app.delete("/api/projects/{project_id}/board/nodes/{node_id}")
def delete_board_node_api(project_id: int, node_id: int, request: Request):
    project_or_404(me(request), project_id)
    _board_node_or_404(project_id, node_id)
    if not board_store.delete_node(node_id):
        raise HTTPException(404, "없는 카드예요.")
    # 이 카드에 붙은 연결선은 DB가 같이 지운다(board_edges의 ON DELETE CASCADE).
    return {"ok": True}


def _board_ids(raw, what: str) -> list:
    if not isinstance(raw, list) or not raw or len(raw) > 1000:
        raise HTTPException(400, f"{what}는 1~1000개짜리 목록이어야 해요.")
    try:
        return [int(v) for v in raw]
    except (TypeError, ValueError):
        raise HTTPException(400, f"{what}는 숫자 목록이어야 해요.")


@app.patch("/api/projects/{project_id}/board/nodes")
async def move_board_nodes_api(project_id: int, request: Request):
    """여러 카드를 한 번에 옮긴다 — `{"nodes": [{"id", "x", "y"}, ...]}`. 여러 장 선택해 끌었을
    때와 되돌리기에서 쓴다. 한 장이라도 이 프로젝트 카드가 아니면 아무것도 안 바꾼다."""
    project_or_404(me(request), project_id)
    body = await read_json_object(request, allow_empty=False)
    items = body.get("nodes")
    if not isinstance(items, list) or not items or len(items) > 1000:
        raise HTTPException(400, "nodes는 1~1000개짜리 목록이어야 해요.")
    try:
        moves = [(int(it["id"]), float(it["x"]), float(it["y"])) for it in items]
    except (TypeError, ValueError, KeyError):
        raise HTTPException(400, "nodes의 각 항목에는 숫자 id/x/y가 있어야 해요.")
    try:
        board_store.move_nodes(project_id, moves)
    except board_store.BoardNotFound:
        raise HTTPException(404, "없는 카드가 섞여 있어요.")
    return {"ok": True}


@app.post("/api/projects/{project_id}/board/nodes/delete")
async def delete_board_nodes_api(project_id: int, request: Request):
    """여러 카드를 한 번에 지운다 — `{"ids": [...]}`(DELETE는 본문을 못 믿어서 POST). 붙은 선도 같이."""
    project_or_404(me(request), project_id)
    body = await read_json_object(request, allow_empty=False)
    ids = _board_ids(body.get("ids"), "ids")
    try:
        board_store.delete_nodes(project_id, ids)
    except board_store.BoardNotFound:
        raise HTTPException(404, "없는 카드가 섞여 있어요.")
    return {"ok": True}


@app.post("/api/projects/{project_id}/board/restore")
async def restore_board_api(project_id: int, request: Request):
    """지운 카드·선을 되살린다(되돌리기) — `{"nodes": [...], "edges": [...]}`. 각 카드는 지우기
    전에 받아 둔 카드 그대로(id 포함), 선은 `{id, from_node_id, to_node_id}`. 원래 id를 되도록
    그대로 쓰고, 못 쓰면 `id_map`(옛 id → 새 id)으로 알려 준다. 선은 보낸 순서대로 돌려준다."""
    user = me(request)
    project_or_404(user, project_id)
    body = await read_json_object(request, allow_empty=False)
    raw_nodes = body.get("nodes") or []
    raw_edges = body.get("edges") or []
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list) or not (raw_nodes or raw_edges)             or len(raw_nodes) > 1000 or len(raw_edges) > 5000:
        raise HTTPException(400, "nodes/edges 목록이 필요해요.")
    nodes = []
    try:
        for n in raw_nodes:
            kind = n.get("kind")
            if kind not in BOARD_NODE_KINDS:
                raise HTTPException(400, f"kind는 {'/'.join(BOARD_NODE_KINDS)} 중 하나여야 해요.")
            nodes.append({
                "id": int(n["id"]) if n.get("id") is not None else None,
                "kind": kind,
                "asset_path": _board_asset_path_or_error(user, kind, n.get("asset_path")),
                "job_id": _board_restore_job_id(user, kind, n.get("job_id")),
                "data": _board_gen_data_or_error(n.get("data")) if kind == "gen" else None,
                "text": str(n.get("text") or ""),
                "x": float(n["x"]), "y": float(n["y"]),
                "width": float(n.get("width") or 220), "height": float(n.get("height") or 220),
                "z_index": int(n.get("z_index") or 0),
                "created_at": str(n["created_at"]) if n.get("created_at") else None,
            })
        edges = [{"id": int(e["id"]) if e.get("id") is not None else None,
                  "from_node_id": int(e["from_node_id"]), "to_node_id": int(e["to_node_id"]),
                  "to_slot": str(e["to_slot"]) if e.get("to_slot") else None,
                  "created_at": str(e["created_at"]) if e.get("created_at") else None} for e in raw_edges]
    except (TypeError, ValueError, KeyError, AttributeError):
        raise HTTPException(400, "되살릴 카드/선 형식이 맞지 않아요.")
    try:
        return board_store.restore(project_id, nodes, edges)
    except board_store.BoardNotFound:
        raise HTTPException(404, "선의 양 끝 카드가 이 보드에 없어요.")


def _board_restore_job_id(user, kind: str, raw) -> str | None:
    """되살릴 작업 카드의 job_id. 그 사이 작업이 아예 사라졌으면 카드는 "지워진 작업"으로
    살린다(None). 남아 있는데 남의 작업이면 404 — 되살리기 요청에 남의 작업을 끼워 넣지 못하게."""
    if kind != "job" or not raw:
        return None
    job_id = str(raw)
    with lock:
        exists = job_id in jobs
    if not exists:
        return None
    job_or_404(user, job_id)
    return job_id


@app.get("/api/projects/{project_id}/board/jobs")
def board_jobs_api(project_id: int, request: Request):
    """보드의 작업 카드마다 지금 상태 — `{"cards": {node_id: {...}}}`. 화면이 작업 카드를 그릴 때와,
    대기·실행 중인 작업이 있는 동안 몇 초마다 부른다. 카드마다 status/template_label/prompt/progress/
    시각/파드와 최근 결과물 4개(results: [{path, kind, nsfw}])를 준다. 작업이 지워졌거나(휴지통 포함)
    볼 수 없으면 missing=true."""
    user = me(request)
    project_or_404(user, project_id)
    cards = board_store.job_nodes(project_id)
    # 생성 카드는 마지막 실행 작업을 같은 모양으로 보여 준다(run_count는 실행 횟수). 아직 보드에 안 펼친
    # 결과 수(pending_spread)도 같이 줘서, 화면이 있으면 펼치기(POST .../spread)를 부르게 한다.
    gen_runs = board_store.gen_runs(project_id)
    run_counts = {node_id: len(runs) for node_id, runs in gen_runs}
    cards = cards + [(node_id, runs[-1].get("job_id")) for node_id, runs in gen_runs]
    if cards:
        _sync_assets_quietly()   # 방금 끝난 작업의 결과물이 색인에 올라오게(자체적으로 간격을 둔다)
    pending_spread = {node_id: _board_gen_unspread_count(runs) for node_id, runs in gen_runs}
    results = board_store.job_results(job_id for _, job_id in cards)
    out = {}
    with lock:
        for node_id, job_id in cards:
            job = jobs.get(job_id) if job_id else None
            if job is None or job.get("deleted") or not auth.can_access(user, job.get("owner_id")):
                out[node_id] = {"job_id": job_id, "missing": True}
                continue
            prompt = (job.get("options") or {}).get("main_prompt") or ""
            out[node_id] = {
                "job_id": job_id, "missing": False,
                "status": job.get("status"),
                "active": job.get("status") in BOARD_ACTIVE_JOB_STATUSES,
                "template_label": job.get("template_label") or job.get("template_id"),
                "prompt": str(prompt)[:300],
                "progress": job.get("progress"),
                "queued_at": job.get("queued_at"), "started_at": job.get("started_at"),
                "finished_at": job.get("finished_at"),
                "pod_name": job.get("pod_name"),
                "waiting_reason": job.get("waiting_reason"),
                "results": results.get(job_id, []),
            }
            if node_id in run_counts:
                out[node_id]["run_count"] = run_counts[node_id]
                # 끝난 직후에는 파드에서 결과가 늦게 넘어올 수 있어 잠깐 더 지켜본다.
                out[node_id]["settling"] = _finished_within(job.get("finished_at"), BOARD_GEN_SETTLE_SEC)
        for node_id, n in pending_spread.items():
            out.setdefault(node_id, {"missing": True})["pending_spread"] = n
    return {"cards": out}


BOARD_GEN_SETTLE_SEC = 120      # 작업이 끝난 뒤 결과가 늦게 넘어오는지 더 지켜보는 시간
BOARD_GEN_SPREAD_RUNS = 10      # 결과를 펼칠 때 볼 최근 실행 수
BOARD_GEN_SPREAD_COLS = 4       # 펼친 결과 카드의 한 줄 개수
BOARD_GEN_SPREAD_SIZE = 160     # 펼친 결과 카드 크기
_board_spread_lock = threading.Lock()   # 같은 카드의 펼치기가 겹쳐 카드가 두 번 생기지 않게


def _finished_within(finished_at: str | None, seconds: int) -> bool:
    if not finished_at:
        return False
    try:
        done = datetime.fromisoformat(finished_at)
    except ValueError:
        return False
    if done.tzinfo is None:
        done = done.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - done).total_seconds() < seconds


def _board_gen_unspread_count(runs: list) -> int:
    """최근 실행들의 결과 중 아직 보드에 펼치지 않은 수."""
    n = 0
    for run in (runs or [])[-BOARD_GEN_SPREAD_RUNS:]:
        done = set(run.get("spread") or [])
        n += sum(1 for a in board_store.job_assets(run.get("job_id")) if a["path"] not in done)
    return n


@app.post("/api/projects/{project_id}/board/nodes/{node_id}/spread")
def spread_board_gen_api(project_id: int, node_id: int, request: Request):
    """생성 카드의 결과 펼치기 — 최근 실행들의 결과 중 아직 안 펼친 것을 이미지/영상 카드로 만들어 카드
    오른쪽에 격자로 놓고 생성 카드에서 선으로 잇는다. 펼친 결과는 실행 기록(runs[].spread)에 적어 두어
    두 번 펼치지 않는다 — 펼친 카드를 지워도 다시 생기지 않고, 작업을 지워도 펼친 카드는 남는다."""
    user = me(request)
    project_or_404(user, project_id)
    _sync_assets_quietly()
    with _board_spread_lock:
        node = board_store.get_node(project_id, node_id)
        if node is None:
            raise HTTPException(404, "없는 카드예요.")
        data = node.get("data")
        if node["kind"] != "gen" or not isinstance(data, dict):
            raise HTTPException(400, "생성 카드가 아니에요.")
        runs = data.get("runs") or []
        placed = sum(len(r.get("spread") or []) for r in runs)   # 지금까지 펼친 수 — 다음 칸 자리
        size, gap = BOARD_GEN_SPREAD_SIZE, 20
        new_nodes, new_edges = [], []
        for run in runs[-BOARD_GEN_SPREAD_RUNS:]:
            done = run.setdefault("spread", [])
            for asset in board_store.job_assets(run.get("job_id")):
                if asset["path"] in done:
                    continue
                try:   # 자기(admin은 전부) 결과물만 — 작업이 내 것이면 결과도 내 것이지만 한 번 더 확인
                    asset_meta.get_detail(asset["path"], owner_id=auth.owner_scope(user))
                except asset_meta.AssetNotFound:
                    continue
                col, row = placed % BOARD_GEN_SPREAD_COLS, placed // BOARD_GEN_SPREAD_COLS
                x = node["x"] + node["width"] + 80 + col * (size + gap)
                y = node["y"] + row * (size + gap)
                card = board_store.create_node(project_id, asset["kind"], asset["path"], "", x, y, size, size)
                new_nodes.append(card)
                new_edges.append(board_store.create_edge(project_id, node_id, card["id"]))
                done.append(asset["path"])
                placed += 1
        if new_nodes:
            # 실행 기록만 고친다 — 펼치는 사이 바뀐 꺼낸 옵션 값 등은 다시 읽은 것을 그대로 둔다.
            fresh = board_store.get_node(project_id, node_id) or node
            fresh_data = dict(fresh.get("data") or {})
            spread_by_job = {r.get("job_id"): r.get("spread") for r in runs}
            fresh_data["runs"] = [{**r, "spread": spread_by_job.get(r.get("job_id"), r.get("spread") or [])}
                                  for r in fresh_data.get("runs") or []]
            node = board_store.update_node(node_id, {"data": fresh_data})
    return {"nodes": new_nodes, "edges": new_edges, "node": node}


BOARD_GEN_MAX_RUNS = 50   # 카드에 남기는 실행 기록 수


def _prepend_lora_trigger(text: str, trigger: str) -> str:
    """index.html의 prependLoraTrigger와 같은 규칙 — 이미 들어 있으면 그대로, 비었으면 트리거만."""
    t = (trigger or "").strip()
    v = text or ""
    if not t:
        return v
    if not v.strip():
        return t
    if t.lower() in v.lower():
        return v
    return f"{t}, {v}"


async def _board_gen_prepare(user: dict, project_id: int, node_id: int) -> dict:
    """생성 카드를 실행할 재료를 모은다 — 입구에 이어진 이미지 카드를 입력 이미지 풀로 가져와 그 칸의
    값으로 넣고, 고정 값 + 꺼낸 옵션 값 + LoRA 트리거를 합친다. 필수 입구가 비었으면 400.
    실행(run)과 "자세히"(prepare, 새 작업 폼 채우기)가 같이 쓴다."""
    node = board_store.get_node(project_id, node_id)
    if node is None:
        raise HTTPException(404, "없는 카드예요.")
    data = node.get("data")
    if node["kind"] != "gen" or not isinstance(data, dict):
        raise HTTPException(400, "생성 카드가 아니에요.")
    template = load_templates_map().get(data.get("template_id"))
    if template is None:
        raise HTTPException(400, "이 카드의 템플릿이 이 서버에 없어요.")
    slots = {sl["name"]: sl for sl in data.get("slots", [])}
    filled = {}
    for from_id, slot in board_store.slot_edges(project_id, node_id):
        src = board_store.get_node(project_id, from_id)
        if slot in slots and src and src["kind"] == "image" and src.get("asset_path"):
            filled[slot] = src["asset_path"]
    missing = [sl["label"] for sl in data.get("slots", []) if sl.get("required") and sl["name"] not in filled]
    if missing:
        raise HTTPException(400, f"비어 있는 필수 입구가 있어요: {', '.join(missing)}")
    options = {**(data.get("options") or {}), **(data.get("values") or {})}
    for slot, path in filled.items():
        _board_asset_path_or_error(user, "image", path)   # 자기(admin은 전부) 결과물만
        try:
            options[slot] = await _import_output_image_to_input_pool(path)
        except (OSError, InputAssetError) as e:
            raise HTTPException(400, f"입구 이미지를 가져오지 못했어요: {e}")
    trigger = data.get("lora_trigger") or ""
    if trigger and any(o["name"] == "main_prompt" for o in template.get("options", [])):
        options["main_prompt"] = _prepend_lora_trigger(options.get("main_prompt", ""), trigger)
    return {"node": node, "template": template, "options": {k: "" if v is None else str(v) for k, v in options.items()},
            "workflow": data.get("workflow"), "video_workflow": data.get("video_workflow"),
            "label": data.get("preset_name") or template.get("label") or template["id"]}


@app.post("/api/projects/{project_id}/board/nodes/{node_id}/prepare")
async def prepare_board_gen_api(project_id: int, node_id: int, request: Request):
    """생성 카드의 "자세히" — 새 작업 폼을 이 카드 값으로 채울 재료(입구 이미지는 입력 이미지 풀로 가져온 이름)."""
    user = me(request)
    project_or_404(user, project_id)
    prep = await _board_gen_prepare(user, project_id, node_id)
    return {"template_id": prep["template"]["id"], "options": prep["options"], "workflow": prep["workflow"],
            "video_workflow": prep["video_workflow"], "label": prep["label"]}


@app.post("/api/projects/{project_id}/board/nodes/{node_id}/run")
async def run_board_gen_api(project_id: int, node_id: int, request: Request):
    """생성 카드 실행 — 재료를 모아 이 보드의 프로젝트로 작업을 만든다. 파드는 정하지 않아 바로 대기 큐로
    가고, 스케줄러가 필요한 모델을 갖춘 파드를 찾아 시작한다(새 작업 폼의 "추가"와 달리 일시정지하지 않음).
    실행 기록(작업 id)은 카드에 쌓인다."""
    user = me(request)
    project_or_404(user, project_id)
    prep = await _board_gen_prepare(user, project_id, node_id)
    wf, vwf = prep["workflow"], prep["video_workflow"]
    job = await create_job(prep["template"], json.dumps(wf).encode("utf-8") if wf is not None else None,
                           "board_gen_workflow.json" if wf is not None else None, None, None, prep["options"], None,
                           json.dumps(vwf).encode("utf-8") if vwf is not None else None,
                           "board_gen_video_workflow.json" if vwf is not None else None,
                           project_id, user=user)
    # 실행하는 사이 카드가 바뀌었을 수 있으니(값 저장 등) 다시 읽어서 실행 기록만 더한다.
    node = board_store.get_node(project_id, node_id) or prep["node"]
    data = dict(node.get("data") or {})
    data["runs"] = ((data.get("runs") or []) + [{"job_id": job["id"], "at": now_iso(), "spread": []}])[-BOARD_GEN_MAX_RUNS:]
    node = board_store.update_node(node_id, {"data": data})
    return {"job": {"id": job["id"], "status": job.get("status")}, "node": node}


@app.post("/api/projects/{project_id}/board/edges")
async def create_board_edge_api(project_id: int, request: Request):
    project_or_404(me(request), project_id)
    body = await read_json_object(request, allow_empty=False)
    try:
        from_id = int(body.get("from_node_id"))
        to_id = int(body.get("to_node_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "from_node_id/to_node_id는 숫자여야 해요.")
    if from_id == to_id:
        raise HTTPException(400, "카드를 자기 자신과 이을 수는 없어요.")
    # 양 끝이 둘 다 이 프로젝트 카드여야 한다 — 다른 프로젝트 카드 id를 짐작해 잇지 못하게.
    owners = board_store.node_project_ids((from_id, to_id))
    if owners.get(from_id) != project_id or owners.get(to_id) != project_id:
        raise HTTPException(404, "없는 카드예요.")
    to_slot = body.get("to_slot")
    if to_slot:
        # 생성 카드의 입구로 — 받는 쪽은 그 입구가 있는 생성 카드, 보내는 쪽은 이미지 카드여야 한다.
        target = board_store.get_node(project_id, to_id)
        source = board_store.get_node(project_id, from_id)
        slots = {sl["name"] for sl in ((target or {}).get("data") or {}).get("slots", [])} if target and target["kind"] == "gen" else set()
        if str(to_slot) not in slots:
            raise HTTPException(400, "그 생성 카드에 없는 입구예요.")
        if not source or source["kind"] != "image":
            raise HTTPException(400, "입구에는 이미지 카드만 이을 수 있어요.")
        edge, removed = board_store.create_slot_edge(project_id, from_id, to_id, str(to_slot))
        return {**edge, "replaced": removed}
    return {**board_store.create_edge(project_id, from_id, to_id), "replaced": []}


@app.delete("/api/projects/{project_id}/board/edges/{edge_id}")
def delete_board_edge_api(project_id: int, edge_id: int, request: Request):
    project_or_404(me(request), project_id)
    if not board_store.delete_edge(project_id, edge_id):
        raise HTTPException(404, "없는 연결선이에요.")
    return {"ok": True}


