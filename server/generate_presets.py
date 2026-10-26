"""관리자 생성 프리셋을 검증·저장하고 요청용 워크플로우로 조립한다."""

import json
import re

import db
import quick_run


class PresetError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _integer(value, label, lower, upper):
    if isinstance(value, bool):
        raise PresetError(f"{label} 값이 올바르지 않아요.")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise PresetError(f"{label} 값이 올바르지 않아요.") from None
    if str(value).strip() != str(number) or not lower <= number <= upper:
        raise PresetError(f"{label} 값은 {lower}~{upper}여야 해요.")
    return number


def _workflow(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            raise PresetError("워크플로우 JSON이 올바르지 않아요.") from None
    if not isinstance(value, dict) or quick_run.classify_workflow(value).get("template_id") != "seed_batch":
        raise PresetError("텍스트→이미지 워크플로우만 등록할 수 있어요.")
    samplers = [node for node in value.values() if isinstance(node, dict) and node.get("class_type") == "KSampler"]
    latents = [node for node in value.values() if isinstance(node, dict) and node.get("class_type") == "EmptyLatentImage"]
    if not samplers or not latents or not all(k in samplers[0].get("inputs", {}) for k in ("positive", "negative")):
        raise PresetError("KSampler와 EmptyLatentImage, 프롬프트 연결이 필요해요.")
    for key in ("positive", "negative"):
        link = samplers[0]["inputs"][key]
        node = value.get(str(link[0])) if isinstance(link, list) and link else None
        if not isinstance(node, dict) or node.get("class_type") != "CLIPTextEncode":
            raise PresetError("KSampler 프롬프트는 CLIPTextEncode에 연결되어야 해요.")
    if str(samplers[0]["inputs"]["positive"][0]) == str(samplers[0]["inputs"]["negative"][0]):
        raise PresetError("긍정·부정 프롬프트는 별도 노드여야 해요.")
    return value


def _public(row, include_workflow=False):
    result = {key: row[key] for key in ("name", "width", "height", "steps", "max_count", "enabled")}
    result["enabled"] = bool(result["enabled"])
    result["aspects"] = json.loads(row["aspects_json"])
    if include_workflow:
        result["user_ids"] = json.loads(row["user_ids_json"])
        result["workflow"] = json.loads(row["workflow_json"])
    return result


def list_presets(user_id=None):
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM generate_presets ORDER BY name").fetchall()
    if user_id is None:
        return [_public(row, True) for row in rows]
    return [_public(row) for row in rows if row["enabled"] and user_id in json.loads(row["user_ids_json"])]


def get(name):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM generate_presets WHERE name=?", (name,)).fetchone()
    return _public(row, True) if row else None


def save(body, existing_name=None):
    if not isinstance(body, dict):
        raise PresetError("프리셋을 입력해 주세요.")
    name = body.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9가-힣_-]{1,64}", name):
        raise PresetError("프리셋 이름은 1~64자의 글자·숫자·밑줄·하이픈이어야 해요.")
    workflow = _workflow(body.get("workflow"))
    width = _integer(body.get("width"), "너비", 64, 2048)
    height = _integer(body.get("height"), "높이", 64, 2048)
    steps = _integer(body.get("steps"), "단계", 1, 150)
    max_count = _integer(body.get("max_count", 1), "최대 장수", 1, 8)
    if width % 8 or height % 8:
        raise PresetError("크기는 8의 배수여야 해요.")
    aspects = body.get("aspects", {"default": [width, height]})
    if not isinstance(aspects, dict) or not aspects or len(aspects) > 12:
        raise PresetError("비율별 크기를 입력해 주세요.")
    clean_aspects = {}
    for label, size in aspects.items():
        if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,32}", label):
            raise PresetError("비율 이름이 올바르지 않아요.")
        if not isinstance(size, (list, tuple)) or len(size) != 2:
            raise PresetError("비율별 크기는 [너비, 높이]여야 해요.")
        w, h = (_integer(size[0], "너비", 64, 2048), _integer(size[1], "높이", 64, 2048))
        if w % 8 or h % 8:
            raise PresetError("크기는 8의 배수여야 해요.")
        clean_aspects[label] = [w, h]
    user_ids = body.get("user_ids", [])
    if not isinstance(user_ids, list) or len(user_ids) > 100 or any(isinstance(v, bool) or not isinstance(v, int) for v in user_ids):
        raise PresetError("허용 회원 목록이 올바르지 않아요.")
    user_ids = sorted(set(user_ids))
    if user_ids:
        with db.connect() as conn:
            rows = conn.execute(f"SELECT id FROM users WHERE id IN ({','.join('?' for _ in user_ids)}) AND role!='admin'", user_ids).fetchall()
        if len(rows) != len(user_ids):
            raise PresetError("일반 회원만 허용할 수 있어요.")
    enabled = body.get("enabled", True)
    if not isinstance(enabled, bool):
        raise PresetError("사용 여부가 올바르지 않아요.")
    values = (name, json.dumps(workflow, ensure_ascii=False), width, height, steps, max_count,
              json.dumps(clean_aspects), json.dumps(user_ids), int(enabled))
    with db.connect() as conn:
        if existing_name is None:
            if conn.execute("SELECT 1 FROM generate_presets WHERE name=?", (name,)).fetchone():
                raise PresetError("같은 이름의 프리셋이 있어요.", 409)
            conn.execute("INSERT INTO generate_presets VALUES(?,?,?,?,?,?,?,?,?)", values)
        else:
            if not conn.execute("SELECT 1 FROM generate_presets WHERE name=?", (existing_name,)).fetchone():
                raise PresetError("프리셋을 찾을 수 없어요.", 404)
            if name != existing_name and conn.execute("SELECT 1 FROM generate_presets WHERE name=?", (name,)).fetchone():
                raise PresetError("같은 이름의 프리셋이 있어요.", 409)
            conn.execute("UPDATE generate_presets SET name=?, workflow_json=?, width=?, height=?, steps=?, max_count=?, aspects_json=?, user_ids_json=?, enabled=? WHERE name=?", values + (existing_name,))
    return get(name)


def delete(name):
    with db.connect() as conn:
        cursor = conn.execute("DELETE FROM generate_presets WHERE name=?", (name,))
    if not cursor.rowcount:
        raise PresetError("프리셋을 찾을 수 없어요.", 404)
    return {"ok": True}


def build_workflow(preset, prompt, negative_prompt, aspect, seed):
    workflow = preset["workflow"]
    width, height = preset["aspects"][aspect]
    sampler = next(node for node in workflow.values() if isinstance(node, dict) and node.get("class_type") == "KSampler")
    for key, value in (("positive", prompt), ("negative", negative_prompt)):
        node = workflow[str(sampler["inputs"][key][0])]
        node["inputs"]["text"] = value
        node.setdefault("_meta", {})["title"] = "main_prompt" if key == "positive" else "negative_prompt"
    sampler["inputs"]["steps"] = preset["steps"]
    sampler["inputs"]["seed"] = seed
    for node in workflow.values():
        if isinstance(node, dict) and node.get("class_type") == "EmptyLatentImage":
            node["inputs"].update(width=width, height=height, batch_size=1)
    return workflow
