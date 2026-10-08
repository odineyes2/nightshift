"""백틱 빠른 실행창(NS-52)의 워크플로우 분류·옵션 조립 — 서버에 의존하지 않는 순수 함수.

최근 워크플로우에는 JSON만 있으므로 노드 모양으로 어느 템플릿에 넣을지 정한다.
- 이미지 입력 노드 = LoadImage 중 DWPreprocessor로만 들어가는 것(OpenPose 포즈 칸)을 뺀 것.
- 영상: WanImageToVideo → wan22_i2v_batch(첨부 1), WanFirstLastFrameToVideo → wan22_flf2v_batch(첨부 2),
  MiniMaxH3ImageToVideo → minimax_h3_i2v_batch(LoadImage가 있으면 i2v, 없으면 t2v), 그 밖의 영상은 미지원.
- 이미지: 이미지 입력 노드가 있으면 i2i(input_image_batch), 없으면 t2i(seed_batch).
장수·나머지 옵션은 템플릿 기본값을 쓴다(옵션을 비워 보낸다).
"""

UNSUPPORTED_MESSAGE = "빠른 실행에서는 아직 못 써요"


class QuickRunError(ValueError):
    pass


def _nodes(wf):
    return {k: n for k, n in wf.items() if isinstance(n, dict)} if isinstance(wf, dict) else {}


def _consumers(nodes, node_id):
    """node_id의 출력을 입력으로 받는 노드들의 class_type 목록."""
    out = []
    for n in nodes.values():
        for v in (n.get("inputs") or {}).values():
            if isinstance(v, list) and v and str(v[0]) == str(node_id):
                out.append(n.get("class_type"))
    return out


def _title(node):
    return str((node.get("_meta") or {}).get("title") or "")


def classify_workflow(wf) -> dict:
    nodes = _nodes(wf)
    if not nodes:
        return {"kind": None, "template_id": None, "image_slots": [], "openpose": False,
                "unsupported": "워크플로우가 올바른 ComfyUI API 형식이 아니에요"}
    classes = {n.get("class_type") for n in nodes.values()}
    openpose = "DWPreprocessor" in classes
    loads = [(k, n) for k, n in nodes.items() if n.get("class_type") == "LoadImage"]
    image_inputs = []
    for k, n in loads:
        used_by = _consumers(nodes, k)
        if used_by and all(c == "DWPreprocessor" for c in used_by):
            continue  # 포즈 칸 — 첨부가 아니라 라이브러리 포즈가 들어간다
        image_inputs.append((k, n))

    def result(kind, template_id, slots, unsupported=None):
        return {"kind": kind, "template_id": template_id, "image_slots": slots,
                "openpose": openpose, "unsupported": unsupported}

    if "WanFirstLastFrameToVideo" in classes:
        return result("i2v", "wan22_flf2v_batch", ["start_image", "end_image"])
    if "WanImageToVideo" in classes:
        return result("i2v", "wan22_i2v_batch", ["start_image"])
    if "MiniMaxH3ImageToVideo" in classes:
        titles = {_title(n) for _, n in image_inputs}
        slots = [s for s in ("first_frame_image", "last_frame_image") if s in titles]
        if not slots and image_inputs:
            slots = ["first_frame_image"]
        return result("i2v" if slots else "t2v", "minimax_h3_i2v_batch", slots)
    if any("video" in str(c).lower() for c in classes):
        return result("t2v" if not image_inputs else "i2v", None, [], UNSUPPORTED_MESSAGE)
    if image_inputs:
        return result("i2i", "input_image_batch", ["input_image"])
    return result("t2i", "seed_batch", [])


def append_tags(prompt: str, tags: str) -> str:
    """NS-51 태그 자동 붙이기와 같은 규칙 — 이미 들어 있지 않으면 ", "로 끝에 붙인다."""
    prompt, tags = (prompt or "").strip(), (tags or "").strip()
    if not tags or tags in prompt:
        return prompt
    return ", ".join(p for p in (prompt, tags) if p)


def build_quick_options(cls: dict, prompt, images=None, library=None) -> dict:
    """분류 결과와 입력으로 create_job의 raw_options를 만든다.
    돌려주는 값: {"options", "ignored_images", "pose_skipped"}. 잘못된 입력은 QuickRunError."""
    if cls.get("unsupported") or not cls.get("template_id"):
        raise QuickRunError(cls.get("unsupported") or UNSUPPORTED_MESSAGE)
    prompt = str(prompt or "").strip()
    if not prompt:
        raise QuickRunError("프롬프트를 입력해 주세요.")
    images = [str(i).strip() for i in (images or []) if str(i or "").strip()]
    slots = cls["image_slots"]
    if len(images) < len(slots):
        raise QuickRunError(f"이 프리셋은 이미지 {len(slots)}장이 필요해요 — 첨부해 주세요.")
    options = dict(zip(slots, images))
    ignored = images[len(slots):]

    pose_skipped = False
    if library:
        tags = str(library.get("tags") or "")
        if library.get("mode") == "openpose":
            image = str(library.get("image") or "").strip()
            if not cls["openpose"]:
                pose_skipped, tags = True, ""  # NS-51처럼 포즈·태그 모두 적용하지 않는다
            elif not image:
                raise QuickRunError("Openpose CN에 쓸 포즈 이미지가 없어요.")
            else:
                options["pose_image"] = image
        prompt = append_tags(prompt, tags)

    prompt_key = "user_prompt" if cls["kind"] in ("t2v", "i2v") else "main_prompt"
    options[prompt_key] = prompt
    return {"options": options, "ignored_images": ignored, "pose_skipped": pose_skipped}
