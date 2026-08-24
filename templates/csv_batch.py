"""
CSV 배치 템플릿 — ComfyUI workflow API에 CSV의 (제목, 프롬프트) x 시드 조합을 반복 제출한다.

주의: 이 스크립트는 실제 comfy_batch_template.py를 그대로 옮긴 것이 아니라,
매니페스트에 적힌 설명("CSV 배치 (제목/프롬프트 x 시드 반복)")을 바탕으로
best-effort로 새로 작성한 예시 구현이다. 워크플로우 JSON의 노드 제목/구조는
ComfyUI에서 어떻게 워크플로우를 구성했는지에 따라 다르므로, 아래
PROMPT_NODE_TITLE / SEED_NODE_TITLE / SAVE_NODE_TITLE 매칭 로직이 실제
워크플로우와 맞지 않으면 조정이 필요하다.

환경변수:
    WORKFLOW_PATH   (필수) ComfyUI API 형식 workflow json 경로 (nightshift가 주입)
    CSV_PATH        (필수) title,prompt[,seed] 컬럼을 가진 csv 경로 (nightshift가 주입)
    COMFY_URL       ComfyUI 서버 주소 (기본 http://127.0.0.1:8188)
    SEEDS           csv 행에 seed 컬럼이 없을 때 사용할 콤마구분 시드 목록 (기본 "0")
    PROMPT_NODE_TITLE  프롬프트를 주입할 노드의 _meta.title 부분일치 (기본 "Prompt")
    SEED_NODE_TITLE    시드를 주입할 노드의 _meta.title 부분일치 (기본 "KSampler")
    SAVE_NODE_TITLE    파일명 접두사를 주입할 노드의 _meta.title 부분일치 (기본 "Save")
    POLL_INTERVAL_SEC  히스토리 폴링 간격 초 (기본 2)
    POLL_TIMEOUT_SEC   개별 작업 완료 대기 제한 초 (기본 600)
"""

import copy
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid


def env(name, default=None):
    return os.environ.get(name, default)


def load_workflow(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def find_node(workflow, title_substring=None, class_types=()):
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


def sanitize_prefix(text, fallback):
    text = (text or fallback or "batch").strip()
    text = re.sub(r"[^\w\-가-힣 ]+", "_", text)
    return text[:80] or fallback


def apply_prompt(workflow, prompt_text):
    node_id, node = find_node(
        workflow,
        title_substring=env("PROMPT_NODE_TITLE", "Prompt"),
        class_types=("CLIPTextEncode",),
    )
    if node is None:
        print("[csv_batch] 경고: 프롬프트를 넣을 노드를 찾지 못했습니다 (CLIPTextEncode 없음)", file=sys.stderr)
        return
    node.setdefault("inputs", {})["text"] = prompt_text


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


def run_once(base_workflow, comfy_url, title, prompt_text, seed, index):
    workflow = copy.deepcopy(base_workflow)
    apply_prompt(workflow, prompt_text)
    apply_seed(workflow, seed)
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

    default_seeds = [s.strip() for s in env("SEEDS", "0").split(",") if s.strip()]
    comfy_url = env("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")

    index = 0
    for row in rows:
        title = (row.get("title") or row.get("name") or "").strip()
        prompt_text = (row.get("prompt") or "").strip()
        if not prompt_text:
            print(f"[csv_batch] 건너뜀 (prompt 없음): {row}")
            continue

        row_seed = (row.get("seed") or "").strip()
        seeds = [row_seed] if row_seed else default_seeds
        for seed in seeds:
            index += 1
            run_once(base_workflow, comfy_url, title, prompt_text, seed, index)

    print(f"[csv_batch] 총 {index}건 완료")


if __name__ == "__main__":
    main()
