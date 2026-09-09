"""
원격 ComfyUI가 만든 결과 이미지를 HTTP로 끌어와 로컬 출력 폴더(NIGHTSHIFT_OUTPUT_DIR)에
채워 넣는다. nightshift를 홈서버에 상시 띄워두고 ComfyUI만 원격 GPU pod에서 돌리는
구성을 위한 모듈이다.

## 왜 이것만 있으면 되는가

갤러리/zip 다운로드/이메일 발송/자동 회전은 전부 output_images.py의 OUTPUT_DIR를
로컬 디스크로 읽는다. 예전처럼 ComfyUI가 같은 머신에 있으면 OUTPUT_DIR가 곧 ComfyUI의
출력 폴더라 그냥 맞아떨어졌지만, ComfyUI가 원격이면 그 폴더가 텅 비어 있게 된다.

그런데 템플릿 스크립트(templates/*.py의 apply_filename_prefix)가 filename_prefix를
"<JOB_ID>/..."로 바꿔 두기 때문에, ComfyUI는 결과를 job_id 이름의 하위 폴더에 저장하고
/history 응답의 subfolder에도 그 job_id를 그대로 실어 준다. 즉 원격에서 받아온 파일을
로컬 OUTPUT_DIR/<job_id>/<파일명>에 그대로 떨궈 주기만 하면, 갤러리의 "작업별 보기"부터
zip/이메일까지 전부 한 줄도 안 고치고 그대로 동작한다. 이 모듈이 하는 일이 딱 그것이다.

## 지운 이미지가 되살아나지 않게 하기

한 번 받아온 파일은 comfy_output_sync.json에 기록해 둔다. 사용자가 갤러리에서 이미지를
지운 뒤 다시 동기화해도, 원격 ComfyUI의 히스토리에는 그 이미지가 그대로 남아 있으므로
기록이 없으면 매번 되살아난다. "이미 한 번 받아온 것"은 로컬에 지금 있든 없든 다시
받지 않는다 — 삭제는 사용자의 의사 표시이기 때문이다. 정말 다시 받고 싶을 때를 위한
탈출구가 force=True(그리고 forget_downloaded())다.

환경변수:
    NIGHTSHIFT_OUTPUT_SYNC_FILE  동기화 기록 파일 경로 (기본 <저장소>/data/comfy_output_sync.json)
    NIGHTSHIFT_SYNC_MAX_ITEMS    /history에서 훑어볼 최근 프롬프트 개수 (기본 500)
    NIGHTSHIFT_SYNC_TIMEOUT_SEC  ComfyUI HTTP 요청 타임아웃 (기본 60)
"""

import json
import os
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from data_paths import data_path
from output_images import IMAGE_EXTENSIONS, OUTPUT_DIR

# drivers/comfyui.py의 COMFY_USER_AGENT와 값이 같다(왜 필요한지는 그쪽 주석 참고 —
# Cloudflare가 앞단인 RunPod pod가 파이썬 urllib 기본 UA를 403으로 막는다). 여기서
# 다시 import하지 않고 값을 그대로 복사해 둔 이유는 drivers.comfyui가 이 모듈을
# import하므로(순환 import가 된다).
COMFY_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

SYNC_STATE_FILE = Path(
    os.environ.get("NIGHTSHIFT_OUTPUT_SYNC_FILE")
    or data_path("comfy_output_sync.json")
)
# ComfyUI의 /history는 최근 것부터 max_items개를 돌려준다. 한 작업이 이미지 수만큼
# 프롬프트를 만들기 때문에(seed_count=100이면 프롬프트도 100개) 넉넉해야 한다.
SYNC_MAX_ITEMS = int(os.environ.get("NIGHTSHIFT_SYNC_MAX_ITEMS", "500"))
SYNC_TIMEOUT_SEC = float(os.environ.get("NIGHTSHIFT_SYNC_TIMEOUT_SEC", "60"))

# 동기화는 워커 스레드(작업 끝난 직후 자동)와 API 요청(사용자가 버튼을 누름) 양쪽에서
# 시작될 수 있다. 같은 파일을 동시에 받아 쓰지 않도록 통째로 직렬화한다 — 동기화는
# 몇 초짜리 I/O 작업이라 겹치면 잠깐 기다리는 편이 낫다.
_sync_lock = threading.Lock()


class OutputSyncError(Exception):
    """원격 ComfyUI에서 목록을 못 가져왔을 때(연결 실패, 응답 형식 이상 등)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_sync_state() -> dict:
    """{"downloaded": {"<subfolder>/<파일명>": 받은 시각}, "last_sync": ...} 형태.
    파일이 없거나 깨져 있으면 빈 상태로 시작한다 — 기록이 없으면 최악의 경우 한 번
    더 받아올 뿐이라, 여기서 예외를 던져 동기화 자체를 막을 이유가 없다."""
    try:
        with open(SYNC_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    downloaded = data.get("downloaded")
    if not isinstance(downloaded, dict):
        downloaded = {}
    return {"downloaded": downloaded, "last_sync": data.get("last_sync")}


def save_sync_state(state: dict):
    tmp = SYNC_STATE_FILE.with_suffix(SYNC_STATE_FILE.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, SYNC_STATE_FILE)


def sync_state_summary() -> dict:
    state = load_sync_state()
    return {"last_sync": state["last_sync"], "known": len(state["downloaded"])}


def forget_downloaded(subfolder: str | None = None) -> int:
    """동기화 기록을 지운다(subfolder를 주면 그 작업 것만). 지운 개수를 반환.
    "지운 이미지를 다시 받고 싶다"는 명시적인 의사 표시에만 쓴다."""
    with _sync_lock:
        state = load_sync_state()
        before = len(state["downloaded"])
        if subfolder:
            prefix = f"{subfolder}/"
            state["downloaded"] = {
                k: v for k, v in state["downloaded"].items() if not k.startswith(prefix)
            }
        else:
            state["downloaded"] = {}
        save_sync_state(state)
        return before - len(state["downloaded"])


def _http_get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": COMFY_USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def remote_output_items(
    comfy_url: str,
    max_items: int = SYNC_MAX_ITEMS,
    timeout: float = SYNC_TIMEOUT_SEC,
) -> list[dict]:
    """원격 ComfyUI의 /history를 훑어 결과 이미지 목록을 뽑는다.

    ComfyUI에는 "출력 폴더를 나열하는" API가 없다 — 대신 /history가 프롬프트마다
    어떤 파일을 저장했는지(filename/subfolder/type) 그대로 알려주므로 그걸 목록으로 쓴다.
    같은 파일이 여러 프롬프트에 걸쳐 중복으로 나올 수 있어 키 기준으로 한 번만 남긴다."""
    url = f"{comfy_url.rstrip('/')}/history?max_items={int(max_items)}"
    try:
        raw = _http_get(url, timeout)
        history = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise OutputSyncError(f"ComfyUI 히스토리 조회 실패: {e}") from e
    if not isinstance(history, dict):
        raise OutputSyncError("ComfyUI 히스토리 응답 형식이 예상과 달라요.")

    items: dict[str, dict] = {}
    for entry in history.values():
        if not isinstance(entry, dict):
            continue
        outputs = entry.get("outputs")
        if not isinstance(outputs, dict):
            continue
        for node_output in outputs.values():
            if not isinstance(node_output, dict):
                continue
            for image in node_output.get("images") or []:
                if not isinstance(image, dict):
                    continue
                # type이 "output"이 아닌 것(temp 미리보기, 업로드해 둔 input 이미지)은
                # 결과물이 아니므로 건너뛴다.
                if (image.get("type") or "output") != "output":
                    continue
                filename = image.get("filename")
                if not filename:
                    continue
                subfolder = image.get("subfolder") or ""
                key = f"{subfolder}/{filename}" if subfolder else filename
                items.setdefault(key, {"filename": filename, "subfolder": subfolder, "key": key})
    return list(items.values())


def _safe_dest(base: Path, key: str) -> Path | None:
    """key("<subfolder>/<파일명>")를 로컬 경로로 바꾸되, 원격 서버가 준 문자열이라는
    전제로 검증한다 — ".."나 절대 경로로 출력 폴더 밖에 파일을 쓰게 두면 안 된다.
    이미지 확장자가 아닌 것도 여기서 걸러낸다(갤러리가 어차피 못 읽는다)."""
    if not key or key.startswith("/") or "\\" in key:
        return None
    parts = Path(key).parts
    if any(part in ("..", "") for part in parts):
        return None
    dest = (base / key).resolve()
    if not dest.is_relative_to(base.resolve()):
        return None
    if dest.suffix.lower() not in IMAGE_EXTENSIONS:
        return None
    return dest


def _download(comfy_url: str, item: dict, dest: Path, timeout: float):
    query = urllib.parse.urlencode({
        "filename": item["filename"],
        "subfolder": item["subfolder"],
        "type": "output",
    })
    data = _http_get(f"{comfy_url.rstrip('/')}/view?{query}", timeout)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 받는 도중에 갤러리가 반쯤 쓰인 파일을 읽지 않도록 임시 이름으로 받고 바꿔 끼운다.
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, dest)


def sync_outputs(
    comfy_url: str,
    output_dir: str | None = None,
    only_subfolder: str | None = None,
    force: bool = False,
    max_items: int = SYNC_MAX_ITEMS,
    timeout: float = SYNC_TIMEOUT_SEC,
) -> dict:
    """원격 ComfyUI의 결과 이미지를 로컬 출력 폴더로 가져온다.

    only_subfolder를 주면 그 작업(job_id) 것만 받는다 — 작업이 끝난 직후 자동으로
    부를 때 쓴다. force=True면 "이미 받았다"는 기록을 무시하고, 로컬에 없는 파일을
    다시 받는다(로컬에 이미 있는 파일은 어느 경우에도 건드리지 않는다).

    반환: {"checked", "downloaded": [...], "skipped_known", "skipped_existing",
           "skipped_invalid", "errors": [...], "last_sync"}
    """
    base = Path(output_dir or OUTPUT_DIR)
    items = remote_output_items(comfy_url, max_items=max_items, timeout=timeout)
    if only_subfolder:
        items = [i for i in items if i["subfolder"] == only_subfolder]

    downloaded_now: list[str] = []
    errors: list[str] = []
    skipped_known = skipped_existing = skipped_invalid = 0

    with _sync_lock:
        state = load_sync_state()
        known = state["downloaded"]

        for item in items:
            key = item["key"]
            if not force and key in known:
                skipped_known += 1
                continue
            dest = _safe_dest(base, key)
            if dest is None:
                skipped_invalid += 1
                continue
            if dest.exists():
                # 같은 머신에서 ComfyUI가 돌던 시절에 이미 쌓인 파일이거나, 기록만
                # 날아간 경우. 받을 필요는 없지만 기록은 남겨 둬야 나중에 지웠을 때
                # 되살아나지 않는다.
                known.setdefault(key, _now_iso())
                skipped_existing += 1
                continue
            try:
                _download(comfy_url, item, dest, timeout)
            except Exception as e:
                errors.append(f"{key}: {e}")
                continue
            known[key] = _now_iso()
            downloaded_now.append(key)

        # 받은 게 없어도 last_sync는 갱신한다 — "언제 확인했는지"가 화면에 필요하다.
        state["last_sync"] = _now_iso()
        save_sync_state(state)

    return {
        "checked": len(items),
        "downloaded": downloaded_now,
        "skipped_known": skipped_known,
        "skipped_existing": skipped_existing,
        "skipped_invalid": skipped_invalid,
        "errors": errors,
        "last_sync": state["last_sync"],
    }
