"""Library > Pose(NS-39) — 포즈 레퍼런스 게시물 저장소.

게시물은 `poses` 표(DB v19)에, 이미지는 `data/library/poses/` 아래 `<id>.<확장자>` 원본과
`<id>_thumb.webp` 축소본으로 둔다. 회원마다 자기 것만 보고, admin은 모두 본다(owner_id).
URL로 넣으면 서버가 받아 저장한다 — 원본 사이트가 지워져도 썸네일이 깨지지 않게.
"""

import io
import urllib.request
from pathlib import Path

from PIL import Image, ImageOps

import db
from data_paths import data_dir
from pod_registry import PodError, assert_public_url

MAX_BYTES = 20 * 1024 * 1024
FETCH_TIMEOUT = 15
THUMB_SIZE = 512
EXTS = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif", "BMP": "bmp"}


class PoseError(Exception):
    pass


def _dir() -> Path:
    return data_dir("library/poses")


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    """리다이렉트로 내부 주소에 닿지 않게 옮겨 갈 주소도 다시 검사한다."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _assert_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _assert_public(url: str) -> None:
    try:
        assert_public_url(url)
    except PodError:
        raise PoseError("인터넷에서 열리는 http(s) 이미지 주소만 쓸 수 있어요.")


def fetch_image(url: str) -> bytes:
    """URL의 이미지를 받아 바이트로 돌려준다(사설·루프백 주소 거절, 15초, 20MB 상한)."""
    url = (url or "").strip()
    _assert_public(url)
    # ponytail: 검사와 접속 사이 DNS가 바뀌는 경우(rebinding)는 막지 못한다 — 필요하면 검사한 IP로 직접 접속한다.
    opener = urllib.request.build_opener(_CheckedRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": "nightshift-library/1"})
    try:
        with opener.open(req, timeout=FETCH_TIMEOUT) as resp:
            data = resp.read(MAX_BYTES + 1)
    except PoseError:
        raise
    except Exception as e:
        raise PoseError(f"이미지를 받지 못했어요: {e}")
    if len(data) > MAX_BYTES:
        raise PoseError("이미지가 20MB보다 커요.")
    return data


def _open_image(data: bytes) -> Image.Image:
    if not data:
        raise PoseError("이미지를 넣어주세요.")
    if len(data) > MAX_BYTES:
        raise PoseError("이미지가 20MB보다 커요.")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise PoseError("이미지 파일이 아니에요.")
    if img.format not in EXTS:
        raise PoseError("PNG·JPEG·WEBP·GIF·BMP 이미지만 넣을 수 있어요.")
    return img


def _row(row) -> dict:
    d = dict(row)
    d["image_url"] = f"/api/library/poses/{d['id']}/image"
    d["thumb_url"] = f"/api/library/poses/{d['id']}/thumb"
    return d


def add_pose(owner_id, name: str, description: str, danbooru_prompt: str,
             image: bytes, source_url: str = "") -> dict:
    name = (name or "").strip()
    if not name:
        raise PoseError("Pose 명칭을 적어주세요.")
    img = _open_image(image)
    ext = EXTS[img.format]
    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO poses(name, description, danbooru_prompt, image_file, source_url, owner_id, created_at)"
            " VALUES (?, ?, ?, '', ?, ?, ?)",
            (name, (description or "").strip(), (danbooru_prompt or "").strip(), (source_url or "").strip(),
             owner_id, db.now_iso()))
        pose_id = cur.lastrowid
        filename = f"{pose_id}.{ext}"
        (_dir() / filename).write_bytes(image)
        thumb = ImageOps.exif_transpose(img).convert("RGB")
        thumb.thumbnail((THUMB_SIZE, THUMB_SIZE))
        thumb.save(_dir() / f"{pose_id}_thumb.webp", "WEBP", quality=85)
        conn.execute("UPDATE poses SET image_file=? WHERE id=?", (filename, pose_id))
        row = conn.execute("SELECT * FROM poses WHERE id=?", (pose_id,)).fetchone()
    return _row(row)


def list_poses(owner_scope) -> list[dict]:
    """owner_scope가 None이면(admin) 전부, 아니면 그 회원 것만 — 최신순."""
    with db.connect() as conn:
        if owner_scope is None:
            rows = conn.execute("SELECT * FROM poses ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute("SELECT * FROM poses WHERE owner_id=? ORDER BY id DESC", (owner_scope,)).fetchall()
    return [_row(r) for r in rows]


def get_pose(pose_id: int) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM poses WHERE id=?", (pose_id,)).fetchone()
    return _row(row) if row else None


def image_path(pose: dict, thumb: bool) -> Path:
    return _dir() / (f"{pose['id']}_thumb.webp" if thumb else pose["image_file"])
