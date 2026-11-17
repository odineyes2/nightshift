"""홈서버에 보관하는 모델 파일(지금은 인물 LoRA)과, 워커(파드)가 그 파일을 받아 갈 주소를 만든다.

파드는 끌 때 지워지므로 직접 학습한 LoRA는 받을 인터넷 주소가 없다. 파일을 data/hosted_models/<종류>/ 아래에 두고,
작업이 그 모델을 찾으면 자동 설치(13-jobs-api._start_model_downloads)가 여기서 만든 주소로 파드에 받게 한다.
주소에는 파일마다 다른 열쇠가 들어 있어 로그인 없이 그 파일 하나만 받을 수 있다(05-auth HOSTED_MODEL_PATH_RE).
"""

import hashlib
import hmac
import os
import re
import urllib.parse
from pathlib import Path

import model_download
from data_paths import data_dir

KINDS = ("loras",)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.safetensors$")
MAX_BYTES = 512 * 1024 * 1024


class HostedModelError(ValueError):
    pass


def _check(kind: str, filename: str) -> None:
    if kind not in KINDS:
        raise HostedModelError("보관할 수 있는 종류가 아니에요.")
    if not isinstance(filename, str) or not NAME_RE.match(filename) or ".." in filename:
        raise HostedModelError("파일 이름은 글자·숫자·밑줄·점·하이픈과 .safetensors로 써 주세요.")


def path(kind: str, filename: str) -> Path:
    _check(kind, filename)
    return data_dir("hosted_models") / kind / filename


def exists(kind: str, filename: str) -> bool:
    try:
        return path(kind, filename).is_file()
    except HostedModelError:
        return False


def save(kind: str, filename: str, data: bytes) -> Path:
    target = path(kind, filename)
    if not data or len(data) > MAX_BYTES:
        raise HostedModelError("파일이 비었거나 너무 커요(512MB까지).")
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(".part")
    part.write_bytes(data)
    os.replace(part, target)
    return target


def token(kind: str, filename: str) -> str:
    """파일마다 다른 열쇠 — 서버 비밀값으로 계산하므로 저장하지 않는다."""
    return hmac.new(model_download._secret().encode(), f"hosted:{kind}/{filename}".encode(), hashlib.sha256).hexdigest()[:40]


def url(base: str, kind: str, filename: str) -> str:
    return f"{base.rstrip('/')}/api/hosted-models/{kind}/{token(kind, filename)}/{urllib.parse.quote(filename)}"
