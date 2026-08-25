"""
CSV 배치 템플릿 — ComfyUI workflow API에 CSV의 (프롬프트 여러 개, 시드, 해상도, 배치수)
조합을 반복 제출한다.

주의: 이 스크립트는 실제 comfy_batch_template.py를 그대로 옮긴 것이 아니라,
매니페스트에 적힌 설명("CSV 배치 (제목/프롬프트 x 시드 반복)")을 바탕으로
best-effort로 새로 작성한 예시 구현이다. 워크플로우 JSON의 노드 제목/구조는
ComfyUI에서 어떻게 워크플로우를 구성했는지에 따라 다르므로, 아래 매칭 로직이
실제 워크플로우와 맞지 않으면 조정이 필요하다.

프롬프트 노드 매칭:
    많은 워크플로우가 프롬프트 하나를 여러 CLIPTextEncode 노드로 나눠서
    (예: 트리거워드 / 본문 / 퀄리티 태그 / 네거티브) 구성한 뒤 다운스트림에서
    합치는 방식을 쓴다. 이 스크립트는 CSV에 아래 컬럼이 있으면, 그 컬럼 이름과
    "_meta.title"에 그 이름이 포함된 CLIPTextEncode 노드를 찾아 값을 주입한다.
    컬럼이 없거나 값이 비어 있으면 그 필드는 건드리지 않는다.

        trigger_prompt, main_prompt, quality_prompt, negative_prompt, prompt

    (예: 워크플로우에 "main_prompt", "negative_prompt"라는 제목의 CLIPTextEncode
    노드가 있다면, CSV에 같은 이름의 컬럼을 두면 그대로 매칭된다.) 이름이 다르면
    워크플로우의 실제 노드 제목에 맞춰 위 목록이나 아래 PROMPT_FIELD_TITLES를
    조정한다. 프롬프트 노드가 여러 개일 수 있으므로, 이름이 일치하는 노드를
    못 찾으면 다른 CLIPTextEncode로 대체하지 않고 건너뛴다(엉뚱한 노드를 덮어쓰는
    사고를 막기 위함).

환경변수:
    WORKFLOW_PATH   (필수) ComfyUI API 형식 workflow json 경로 (nightshift가 주입)
    CSV_PATH        (필수) 아래 CSV 컬럼을 가진 csv 경로 (nightshift가 주입)
    SEEDS_PER_CASE  (필수) csv 행에 seed 컬럼이 없을 때, 케이스(행)당 반복할 시드 개수
                    (nightshift가 템플릿 옵션 "seeds_per_case"로 주입)
    COMFY_URL       ComfyUI 서버 주소 (기본 http://127.0.0.1:8188)
    SEED_NODE_TITLE    시드를 주입할 노드의 _meta.title 부분일치 (기본 "KSampler")
    LATENT_NODE_TITLE  해상도/배치수를 주입할 노드의 _meta.title 부분일치 (기본 "latent")
    SAVE_NODE_TITLE    파일명 접두사를 주입할 노드의 _meta.title 부분일치 (기본 "Save")
    POLL_INTERVAL_SEC  히스토리 폴링 간격 초 (기본 2)
    POLL_TIMEOUT_SEC   개별 작업 완료 대기 제한 초 (기본 600)

CSV 컬럼:
    title           결과 파일명 접두사로 쓰일 제목 (선택, name도 허용)
    trigger_prompt   트리거워드 프롬프트 (선택)
    main_prompt      본문 프롬프트 (prompt와 동일하게 취급, 둘 중 하나는 있어야 함)
    quality_prompt   퀄리티 태그 프롬프트 (선택)
    negative_prompt  네거티브 프롬프트 (선택)
    prompt           main_prompt가 없을 때 쓰이는 대체 컬럼 (하위 호환용, 선택)
    seed             지정하면 그 시드 하나만 사용, 비어 있으면 SEEDS_PER_CASE개의
                     랜덤 시드로 반복 (선택)
    batch_no         한 번에 생성할 이미지 수 (EmptyLatentImage류 노드의
                     batch_size에 주입, 선택)
    resolution       해상도. "1024x1024"처럼 WxH 형식이거나 RESOLUTION_PRESETS에
                     정의된 이름("square"/"portrait"/"landscape") 중 하나 (선택)
"""

import copy
import csv
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

# CSV 컬럼 이름 = 매칭할 CLIPTextEncode 노드의 _meta.title 부분 문자열.
# 워크플로우의 실제 노드 제목이 다르면 이 목록을 맞춰서 조정한다.
PROMPT_FIELD_TITLES = {
    "trigger_prompt": "trigger_prompt",
    "main_prompt": "main_prompt",
    "quality_prompt": "quality_prompt",
    "negative_prompt": "negative_prompt",
    "prompt": "prompt",
}

RESOLUTION_PRESETS = {
    "square": (1024, 1024),
    "portrait": (832, 1216),
    "landscape": (1216, 832),
}


def env(name, default=None):
    return os.environ.get(name, default)


def load_workflow(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def find_node(workflow, title_substring=None, class_types=(), allow_class_fallback=True):
    title_substring = (title_substring or "").lower()
    fallback = None
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        meta_title = str(node.get("_meta", {}).get("title", "")).lower()
        class_type = node.get("class_type", "")
        if title_substring and title_substring in meta_title:
            return node_id, node
        if allow_class_fallback and class_types and class_type in class_types and fallback is None:
            fallback = (node_id, node)
    return fallback if fallback else (None, None)


def sanitize_prefix(text, fallback):
    text = (text or fallback or "batch").strip()
    text = re.sub(r"[^\w\-가-힣 ]+", "_", text)
    return text[:80] or fallback


def apply_prompts(workflow, row):
    applied = False
    for field, title_substring in PROMPT_FIELD_TITLES.items():
        value = (row.get(field) or "").strip()
        if not value:
            continue
        # 프롬프트 노드가 여러 개(트리거/본문/퀄리티/네거티브)일 수 있으므로
        # 제목이 정확히 일치하지 않으면 다른 CLIPTextEncode로 대체하지 않는다.
        node_id, node = find_node(
            workflow,
            title_substring=title_substring,
            class_types=("CLIPTextEncode",),
            allow_class_fallback=False,
        )
        if node is None:
            print(
                f"[csv_batch] 경고: '{field}' 값을 넣을 노드를 찾지 못했습니다 "
                f"(제목에 '{title_substring}'가 포함된 CLIPTextEncode 없음)",
                file=sys.stderr,
            )
            continue
        node.setdefault("inputs", {})["text"] = value
        applied = True
    if not applied:
        print("[csv_batch] 경고: 이 행에 프롬프트 컬럼 값이 하나도 없습니다.", file=sys.stderr)


def apply_seed(workflow, seed):
    node_id, node = find_node(
        workflow,
        title_substring=env("SEED_NODE_TITLE", "KSampler"),
        class_types=("KSampler", "KSamplerAdvanced"),
    )
    if node is None:
        print("[csv_batch] 경고: 시드를 넣을 노드를 찾지 못했습니다 (KSampler 없음)", file=sys.stderr)
        return
    try:
        node.setdefault("inputs", {})["seed"] = int(seed)
    except (TypeError, ValueError):
        print(f"[csv_batch] 경고: seed 값 '{seed}'을 정수로 변환하지 못했습니다", file=sys.stderr)


def apply_batch_size(workflow, batch_no):
    batch_no = (batch_no or "").strip()
    if not batch_no:
        return
    try:
        batch_size = int(batch_no)
    except ValueError:
        print(f"[csv_batch] 경고: batch_no 값 '{batch_no}'을 정수로 변환하지 못했습니다", file=sys.stderr)
        return
    node_id, node = find_node(
        workflow,
        title_substring=env("LATENT_NODE_TITLE", "latent"),
        class_types=("EmptyLatentImage",),
    )
    if node is None:
        print("[csv_batch] 경고: batch_no를 넣을 노드를 찾지 못했습니다 (EmptyLatentImage 없음)", file=sys.stderr)
        return
    node.setdefault("inputs", {})["batch_size"] = batch_size


def apply_resolution(workflow, resolution):
    resolution = (resolution or "").strip()
    if not resolution:
        return

    preset = RESOLUTION_PRESETS.get(resolution.lower())
    if preset:
        width, height = preset
    else:
        match = re.match(r"^(\d+)\s*[xX]\s*(\d+)$", resolution)
        if not match:
            print(
                f"[csv_batch] 경고: resolution 값 '{resolution}'을 해석하지 못했습니다 "
                f"(WxH 형식이거나 {list(RESOLUTION_PRESETS)} 중 하나여야 함)",
                file=sys.stderr,
            )
            return
        width, height = int(match.group(1)), int(match.group(2))

    node_id, node = find_node(
        workflow,
        title_substring=env("LATENT_NODE_TITLE", "latent"),
        class_types=("EmptyLatentImage",),
    )
    if node is None:
        print("[csv_batch] 경고: resolution을 넣을 노드를 찾지 못했습니다 (EmptyLatentImage 없음)", file=sys.stderr)
        return
    inputs = node.setdefault("inputs", {})
    inputs["width"] = width
    inputs["height"] = height


def apply_filename_prefix(workflow, title, index):
    node_id, node = find_node(
        workflow,
        title_substring=env("SAVE_NODE_TITLE", "Save"),
        class_types=("SaveImage", "SaveImageWebsocket"),
    )
    if node is None:
        return
    node.setdefault("inputs", {})["filename_prefix"] = sanitize_prefix(title, f"batch_{index}")


def queue_prompt(comfy_url, workflow):
    payload = json.dumps({"prompt": workflow, "client_id": str(uuid.uuid4())}).encode("utf-8")
    req = urllib.request.Request(
        f"{comfy_url}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "error" in data:
        raise RuntimeError(f"ComfyUI가 프롬프트를 거부했습니다: {data['error']}")
    return data["prompt_id"]


def wait_for_completion(comfy_url, prompt_id):
    interval = float(env("POLL_INTERVAL_SEC", "2"))
    timeout = float(env("POLL_TIMEOUT_SEC", "600"))
    deadline = time.time() + timeout
    while time.time() < deadline:
        req = urllib.request.Request(f"{comfy_url}/history/{prompt_id}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                history = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise RuntimeError(f"ComfyUI 히스토리 조회 실패: {e}") from e
        if prompt_id in history:
            return history[prompt_id]
        time.sleep(interval)
    raise TimeoutError(f"prompt_id={prompt_id} 완료 대기 시간({timeout}s) 초과")


def run_once(base_workflow, comfy_url, row, title, seed, index):
    workflow = copy.deepcopy(base_workflow)
    apply_prompts(workflow, row)
    apply_seed(workflow, seed)
    apply_batch_size(workflow, row.get("batch_no"))
    apply_resolution(workflow, row.get("resolution"))
    apply_filename_prefix(workflow, title, index)

    prompt_id = queue_prompt(comfy_url, workflow)
    print(f"[csv_batch] [{index}] title={title!r} seed={seed} 큐 등록 (prompt_id={prompt_id})")
    wait_for_completion(comfy_url, prompt_id)
    print(f"[csv_batch] [{index}] 완료")


def main():
    workflow_path = env("WORKFLOW_PATH")
    csv_path = env("CSV_PATH")
    if not workflow_path or not csv_path:
        print("[csv_batch] WORKFLOW_PATH와 CSV_PATH 환경변수가 모두 필요합니다.", file=sys.stderr)
        sys.exit(1)

    base_workflow = load_workflow(workflow_path)
    rows = load_rows(csv_path)
    if not rows:
        print("[csv_batch] CSV에 처리할 행이 없습니다.")
        return

    seeds_per_case = int(env("SEEDS_PER_CASE", "10"))
    comfy_url = env("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")

    index = 0
    for row in rows:
        title = (row.get("title") or row.get("name") or "").strip()
        main_prompt = (row.get("main_prompt") or row.get("prompt") or "").strip()
        if not main_prompt:
            print(f"[csv_batch] 건너뜀 (main_prompt/prompt 없음): {row}")
            continue

        row_seed = (row.get("seed") or "").strip()
        if row_seed:
            seeds = [row_seed]
        else:
            seeds = [random.randint(0, 2**31 - 1) for _ in range(seeds_per_case)]
        for seed in seeds:
            index += 1
            run_once(base_workflow, comfy_url, row, title, seed, index)

    print(f"[csv_batch] 총 {index}건 완료")


if __name__ == "__main__":
    main()
