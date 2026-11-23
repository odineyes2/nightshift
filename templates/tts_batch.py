"""
낭독 음성 템플릿 — 연동 앱(charchat)이 맡긴 글 구간들을 워커(ComfyUI 파드)의 Qwen3-TTS로 읽혀 MP3로 받아 온다.

nightshift POST /api/generate/tts가 구간 목록(TTS_MANIFEST, JSON 파일)과 함께 이 템플릿으로 작업을 만든다. 이 스크립트는
  1. 파드의 nightshift 노드가 낭독을 지원하는지(v9 이상) 확인하고,
  2. 노드의 POST /nightshift/tts/start로 생성을 시작해 GET /nightshift/tts/status로 진행을 보고하고,
  3. 끝나면 /view로 구간별 MP3를 받아 NIGHTSHIFT_TTS_DIR(작업별 폴더)에 <id>.mp3와 result.json(길이·크기)으로 둔다.
연동 앱은 GET /api/generate/tts/<작업>/…으로 받아 간다(10f-generate-tts).

환경변수: COMFY_URL, NIGHTSHIFT_POD_TOKEN, TTS_MANIFEST, NIGHTSHIFT_TTS_DIR, JOB_ID·NIGHTSHIFT_URL·NIGHTSHIFT_API_KEY(진행 보고).
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
MIN_NODE_VERSION = 9
POLL_SEC = 10
MAX_CLIP_BYTES = 8 * 1024 * 1024


def env(name, default=""):
    return os.environ.get(name, default).strip()


def fail(message):
    print(f"[tts_batch] 실패: {message}", flush=True)
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
        print(f"[tts_batch] 경고: 진행 보고 실패: {e}", file=sys.stderr)


def download(output, target_dir):
    query = urllib.parse.urlencode({"filename": output["name"], "subfolder": output["subfolder"], "type": "output"})
    req = urllib.request.Request(env("COMFY_URL").rstrip("/") + "/view?" + query, headers={"User-Agent": USER_AGENT})
    part = target_dir / (output["id"] + ".mp3.part")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read(MAX_CLIP_BYTES + 1)
            if len(raw) != output["size"] or len(raw) > MAX_CLIP_BYTES:
                raise RuntimeError("크기가 맞지 않아요")
            part.write_bytes(raw)
            os.replace(part, target_dir / (output["id"] + ".mp3"))
            return
        except Exception as e:
            if attempt == 2:
                fail(f"구간 음성을 받지 못했어요({output['id'][:8]}): {e}")
            time.sleep(3)


def main():
    for name in ("COMFY_URL", "NIGHTSHIFT_POD_TOKEN", "TTS_MANIFEST", "NIGHTSHIFT_TTS_DIR"):
        if not env(name):
            fail(f"{name}이 없어요.")
    spec = json.loads(Path(env("TTS_MANIFEST")).read_text(encoding="utf-8"))
    try:
        version = node("GET", "/nightshift/dl/ping").get("version") or 0
    except RuntimeError as e:
        fail(f"파드의 nightshift 노드에 닿지 못했어요: {e}")
    if version < MIN_NODE_VERSION:
        fail(f"파드의 nightshift 노드가 v{version}이라 낭독을 못 해요(v{MIN_NODE_VERSION} 이상 — 워커를 껐다 켜면 새로 깔려요).")
    body = {key: spec[key] for key in ("name", "model", "speaker", "language", "items", "batch") if key in spec}
    try:
        node("POST", "/nightshift/tts/start", body, timeout=120)
    except RuntimeError as e:
        fail(f"낭독을 시작하지 못했어요: {e}")
    print(f"[tts_batch] 시작: 구간 {len(spec['items'])}개, 성우 {spec['speaker']}", flush=True)

    last_phase, misses, last_done = None, 0, -1
    while True:
        time.sleep(POLL_SEC)
        try:
            status = node("GET", "/nightshift/tts/status")
            misses = 0
        except RuntimeError as e:
            misses += 1
            if misses >= 30:
                fail(f"파드에서 낭독 상태를 받지 못했어요: {e}")
            continue
        if status.get("name") != spec["name"]:
            fail("파드에서 다른 낭독이 돌고 있어요.")
        if status.get("phase") != last_phase:
            last_phase = status.get("phase")
            print(f"[tts_batch] 단계: {last_phase}", flush=True)
        if status.get("total") and status.get("done") != last_done:
            last_done = status.get("done") or 0
            report(last_done, status["total"])
        if status.get("status") == "error":
            print(status.get("log") or "", flush=True)
            fail(status.get("error") or "낭독 음성을 만들지 못했어요.")
        if status.get("status") == "done":
            break

    target_dir = Path(env("NIGHTSHIFT_TTS_DIR"))
    target_dir.mkdir(parents=True, exist_ok=True)
    outputs = status.get("outputs") or []
    wanted = {item["id"] for item in spec["items"]}
    if {o.get("id") for o in outputs} != wanted:
        fail("파드가 돌려준 구간 목록이 맡긴 것과 달라요.")
    results = {}
    for index, output in enumerate(outputs, start=1):
        download(output, target_dir)
        results[output["id"]] = {"duration": output.get("duration"), "size": output["size"]}
        if index % 20 == 0:
            print(f"[tts_batch] 받음 {index}/{len(outputs)}", flush=True)
    (target_dir / "result.json").write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
    print(f"[tts_batch] 완료: 구간 {len(results)}개", flush=True)


if __name__ == "__main__":
    main()
