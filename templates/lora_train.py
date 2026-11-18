"""
인물 LoRA 학습 템플릿 — 캐릭터 스튜디오의 배우 LoRA를 워커(ComfyUI 파드)에서 학습하고 결과를 홈서버에 보관한다.

nightshift POST /api/generate/train이 학습 그림을 입력 이미지 폴더에 저장하고, 학습 정보(TRAIN_MANIFEST, JSON 파일)와
함께 이 템플릿으로 작업을 만든다. 이 스크립트는
  1. 파드의 nightshift 노드가 학습을 지원하는지(v7 이상) 확인하고,
  2. 학습 그림을 ComfyUI /upload/image로 input/nightshift_train/<이름>/ 아래에 올린 뒤,
  3. 노드의 POST /nightshift/train/start로 학습을 시작해 GET /nightshift/train/status로 진행을 보고하고,
  4. 끝나면 /view로 결과 LoRA를 받아 NIGHTSHIFT_HOSTED_DIR(홈서버 보관 폴더)에 저장한다.
등록부(model_registry)에 적는 일은 작업이 성공으로 끝난 뒤 nightshift 서버가 한다(DB는 서버 프로세스만 연다).

환경변수: COMFY_URL, NIGHTSHIFT_POD_TOKEN, TRAIN_MANIFEST, NIGHTSHIFT_HOSTED_DIR, NIGHTSHIFT_INPUT_IMAGES_DIR
(없으면 NIGHTSHIFT_ASSETS_DIR/input), JOB_ID·NIGHTSHIFT_URL·NIGHTSHIFT_API_KEY(진행 보고).
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
MIN_NODE_VERSION = 7
POLL_SEC = 15


def env(name, default=""):
    return os.environ.get(name, default).strip()


def fail(message):
    print(f"[lora_train] 실패: {message}", flush=True)
    sys.exit(1)


def node(method, path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": USER_AGENT, "X-Nightshift-Token": env("NIGHTSHIFT_POD_TOKEN")}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(env("COMFY_URL").rstrip("/") + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error")
        except Exception:
            detail = None
        raise RuntimeError(detail or f"파드가 HTTP {e.code}을 돌려줬어요.") from None


def upload(path, subfolder):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in (("subfolder", subfolder), ("overwrite", "true"), ("type", "input")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{path.name}"\r\n'
                 f"Content-Type: application/octet-stream\r\n\r\n".encode() + path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(env("COMFY_URL").rstrip("/") + "/upload/image", data=b"".join(parts), method="POST",
                                 headers={"User-Agent": USER_AGENT, "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        info = json.loads(resp.read().decode("utf-8"))
    return f"{info.get('subfolder') or subfolder}/{info['name']}"


def report(done, total):
    job, base = env("JOB_ID"), env("NIGHTSHIFT_URL")
    if not job or not base or not total:
        return
    headers = {"Content-Type": "application/json"}
    if env("NIGHTSHIFT_API_KEY"):
        headers["X-API-Key"] = env("NIGHTSHIFT_API_KEY")
    try:
        req = urllib.request.Request(f"{base}/api/jobs/{job}/progress", data=json.dumps({"total": total, "done": done}).encode(),
                                     headers=headers, method="PUT")
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"[lora_train] 경고: 진행 보고 실패: {e}", file=sys.stderr)


def input_dir():
    override = env("NIGHTSHIFT_INPUT_IMAGES_DIR")
    return Path(override) if override else Path(env("NIGHTSHIFT_ASSETS_DIR", "/workspace/dataset/assets")) / "input"


def main():
    for name in ("COMFY_URL", "NIGHTSHIFT_POD_TOKEN", "TRAIN_MANIFEST", "NIGHTSHIFT_HOSTED_DIR"):
        if not env(name):
            fail(f"{name}이 없어요.")
    spec = json.loads(Path(env("TRAIN_MANIFEST")).read_text(encoding="utf-8"))
    name = spec["name"]
    try:
        version = node("GET", "/nightshift/dl/ping").get("version") or 0
    except RuntimeError as e:
        fail(f"파드의 nightshift 노드에 닿지 못했어요: {e}")
    if version < MIN_NODE_VERSION:
        fail(f"파드의 nightshift 노드가 v{version}이라 학습을 못 해요(v{MIN_NODE_VERSION} 이상 — 워커를 껐다 켜면 새로 깔려요).")

    folder, files = input_dir(), []
    subfolder = f"nightshift_train/{name}"
    for index, item in enumerate(spec["images"], start=1):
        path = folder / item["file"]
        if not path.is_file():
            fail(f"학습 그림이 없어요: {item['file']}")
        files.append({"file": upload(path, subfolder), "caption": item.get("caption", "")})
        print(f"[lora_train] 올림 {index}/{len(spec['images'])}", flush=True)

    body = {"name": name, "trigger": spec["trigger"], "checkpoint": spec["checkpoint"], "images": files,
            **{key: spec[key] for key in ("repeats", "epochs", "dim", "alpha", "lr", "seed", "one_face") if key in spec}}
    try:
        node("POST", "/nightshift/train/start", body)
    except RuntimeError as e:
        fail(f"학습을 시작하지 못했어요: {e}")
    print("[lora_train] 학습 시작", flush=True)

    last_phase, misses = None, 0
    while True:
        time.sleep(POLL_SEC)
        try:
            status = node("GET", "/nightshift/train/status")
            misses = 0
        except RuntimeError as e:
            misses += 1
            if misses >= 20:
                fail(f"파드에서 학습 상태를 받지 못했어요: {e}")
            continue
        if status.get("name") != name:
            fail("파드에서 다른 학습이 돌고 있어요.")
        if status.get("phase") != last_phase:
            last_phase = status.get("phase")
            print(f"[lora_train] 단계: {last_phase} (쓰는 그림 {status.get('kept')}장, 뺀 그림 {len(status.get('dropped') or [])}장, "
                  f"얼굴 거르기 {status.get('face_filter') or '?'})", flush=True)
        if status.get("total"):
            report(status.get("step") or 0, status["total"])
        if status.get("status") == "error":
            print(status.get("log") or "", flush=True)
            fail(status.get("error") or "학습이 실패했어요.")
        if status.get("status") == "done":
            break

    output = status["output"]
    query = urllib.parse.urlencode({"filename": output["name"], "subfolder": output["subfolder"], "type": "output"})
    req = urllib.request.Request(env("COMFY_URL").rstrip("/") + "/view?" + query, headers={"User-Agent": USER_AGENT})
    target_dir = Path(env("NIGHTSHIFT_HOSTED_DIR"))
    target_dir.mkdir(parents=True, exist_ok=True)
    part = target_dir / (output["name"] + ".part")
    with urllib.request.urlopen(req, timeout=600) as resp, open(part, "wb") as f:
        while chunk := resp.read(1024 * 1024):
            f.write(chunk)
    if part.stat().st_size != output["size"]:
        part.unlink(missing_ok=True)
        fail("결과 LoRA를 끝까지 받지 못했어요.")
    os.replace(part, target_dir / output["name"])
    print(f"[lora_train] 완료: {output['name']} ({output['size']} bytes), 쓴 그림 {status.get('kept')}장", flush=True)


if __name__ == "__main__":
    main()
