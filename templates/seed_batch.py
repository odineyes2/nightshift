"""
시드 반복 템플릿 — 워크플로우 하나를 시드만 바꿔가며 seed_count번 반복 실행한다.

csv_batch.py와 같은 ComfyUI 연동 방식(워크플로우 노드 찾기/제출/폴링)을 쓰되,
CSV 입력과 프롬프트 주입 없이 시드만 바꾸는 가장 단순한 형태다. 워크플로우 JSON의
노드 제목/구조가 SEED_NODE_TITLE / SAVE_NODE_TITLE 매칭과 맞지 않으면 조정이 필요하다.

환경변수:
    WORKFLOW_PATH   (필수) ComfyUI API 형식 workflow json 경로 (nightshift가 주입)
    SEED_COUNT      (필수) 반복할 시드 개수 (nightshift가 템플릿 옵션 "seed_count"로 주입)
    COMFY_URL       ComfyUI 서버 주소 (기본 http://127.0.0.1:8188)
    SEED_NODE_TITLE    시드를 주입할 노드의 _meta.title 부분일치 (기본 "KSampler")
    SAVE_NODE_TITLE    파일명 접두사를 주입할 노드의 _meta.title 부분일치 (기본 "Save")
    POLL_INTERVAL_SEC  히스토리 폴링 간격 초 (기본 2)
    POLL_TIMEOUT_SEC   개별 작업 완료 대기 제한 초 (기본 600)
"""

import copy
import json
import os
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


def apply_seed(workflow, seed):
    node_id, node = find_node(
        workflow,
        title_substring=env("SEED_NODE_TITLE", "KSampler"),
        class_types=("KSampler", "KSamplerAdvanced"),
    )
    if node is None:
        print("[seed_batch] 경고: 시드를 넣을 노드를 찾지 못했습니다 (KSampler 없음)", file=sys.stderr)
        return
    node.setdefault("inputs", {})["seed"] = seed


def apply_filename_prefix(workflow, index):
    node_id, node = find_node(
        workflow,
        title_substring=env("SAVE_NODE_TITLE", "Save"),
        class_types=("SaveImage", "SaveImageWebsocket"),
    )
    if node is None:
        return
    node.setdefault("inputs", {})["filename_prefix"] = f"seed_batch_{index}"


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


def run_once(base_workflow, comfy_url, seed, index):
    workflow = copy.deepcopy(base_workflow)
    apply_seed(workflow, seed)
    apply_filename_prefix(workflow, index)

    prompt_id = queue_prompt(comfy_url, workflow)
    print(f"[seed_batch] [{index}] seed={seed} 큐 등록 (prompt_id={prompt_id})")
    wait_for_completion(comfy_url, prompt_id)
    print(f"[seed_batch] [{index}] 완료")


def main():
    workflow_path = env("WORKFLOW_PATH")
    seed_count_raw = env("SEED_COUNT")
    if not workflow_path or not seed_count_raw:
        print("[seed_batch] WORKFLOW_PATH와 SEED_COUNT 환경변수가 모두 필요합니다.", file=sys.stderr)
        sys.exit(1)

    seed_count = int(seed_count_raw)
    base_workflow = load_workflow(workflow_path)
    comfy_url = env("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")

    for index in range(1, seed_count + 1):
        run_once(base_workflow, comfy_url, index - 1, index)

    print(f"[seed_batch] 총 {seed_count}건 완료")


if __name__ == "__main__":
    main()
