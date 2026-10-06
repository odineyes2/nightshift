"""Library > Pose(NS-39) — 포즈 레퍼런스 게시물 저장소.

게시물은 `poses` 표(DB v19)에, 이미지는 여러 장(NS-40, `pose_images` 표 v20)으로 `data/library/poses/` 아래
`<게시물id>_<이미지id>.<확장자>` 원본과 `…_thumb.webp` 축소본으로 둔다(NS-39에서 만든 첫 장은 `<id>.<확장자>` 그대로). 회원마다 자기 것만 보고, admin은 모두 본다(owner_id).
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
MAX_IMAGES = 20   # 한 번에 넣는 장 수 상한(요청 크기 상한 미들웨어가 없어 장 수·장당 크기로만 막는다)
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


def _image(row) -> dict:
    d = dict(row)
    base = f"/api/library/poses/{d['pose_id']}/images/{d['id']}"
    d["image_url"], d["thumb_url"] = f"{base}/image", f"{base}/thumb"
    return d


def _with_images(conn, rows) -> list[dict]:
    """게시물마다 images(position 순)·image_count와 대표(첫 장) 주소를 붙인다."""
    poses = [dict(r) for r in rows]
    by_id = {p["id"]: p for p in poses}
    for p in poses:
        p["images"] = []
    if by_id:
        marks = ",".join("?" * len(by_id))
        for r in conn.execute(f"SELECT * FROM pose_images WHERE pose_id IN ({marks}) ORDER BY pose_id, position, id",
                              list(by_id)):
            by_id[r["pose_id"]]["images"].append(_image(r))
    for p in poses:
        p["image_count"] = len(p["images"])
        p["image_url"] = f"/api/library/poses/{p['id']}/image"
        p["thumb_url"] = f"/api/library/poses/{p['id']}/thumb"
        p["source_url"] = p["images"][0]["source_url"] if p["images"] else ""
    return poses


def _check_images(images: list) -> list:
    """(바이트, 원본 주소) 목록을 모두 먼저 검사한다 — 한 장이라도 아니면 아무것도 저장하지 않는다."""
    if not images:
        raise PoseError("이미지를 올리거나 이미지 주소를 적어주세요.")
    if len(images) > MAX_IMAGES:
        raise PoseError(f"이미지는 한 번에 {MAX_IMAGES}장까지 넣을 수 있어요.")
    return [(data, (src or "").strip(), _open_image(data)) for data, src in images]


def _save_images(conn, pose_id: int, checked: list) -> str:
    """검사한 이미지를 이어지는 position으로 저장하고 첫 장의 파일 이름을 돌려준다."""
    pos = conn.execute("SELECT COALESCE(MAX(position) + 1, 0) FROM pose_images WHERE pose_id=?",
                       (pose_id,)).fetchone()[0]
    first = ""
    for data, src, img in checked:
        image_id = conn.execute(
            "INSERT INTO pose_images(pose_id, image_file, thumb_file, source_url, position, created_at)"
            " VALUES (?, '', '', ?, ?, ?)", (pose_id, src, pos, db.now_iso())).lastrowid
        filename, thumb_name = f"{pose_id}_{image_id}.{EXTS[img.format]}", f"{pose_id}_{image_id}_thumb.webp"
        (_dir() / filename).write_bytes(data)
        thumb = ImageOps.exif_transpose(img).convert("RGB")
        thumb.thumbnail((THUMB_SIZE, THUMB_SIZE))
        thumb.save(_dir() / thumb_name, "WEBP", quality=85)
        conn.execute("UPDATE pose_images SET image_file=?, thumb_file=? WHERE id=?", (filename, thumb_name, image_id))
        first = first or filename
        pos += 1
    return first


def add_pose(owner_id, name: str, description: str, danbooru_prompt: str, images: list) -> dict:
    """images: [(바이트, 원본 주소)] 1~MAX_IMAGES장."""
    name = (name or "").strip()
    if not name:
        raise PoseError("Pose 명칭을 적어주세요.")
    checked = _check_images(images)
    with db.connect() as conn:
        pose_id = conn.execute(
            "INSERT INTO poses(name, description, danbooru_prompt, image_file, source_url, owner_id, created_at)"
            " VALUES (?, ?, ?, '', ?, ?, ?)",
            (name, (description or "").strip(), (danbooru_prompt or "").strip(), checked[0][1],
             owner_id, db.now_iso())).lastrowid
        # 옛 코드로 되돌려도 첫 장이 보이게 poses.image_file에도 남긴다.
        conn.execute("UPDATE poses SET image_file=? WHERE id=?", (_save_images(conn, pose_id, checked), pose_id))
    return get_pose(pose_id)


def add_images(pose_id: int, images: list) -> dict:
    """기존 게시물에 이미지를 덧붙인다(position은 이어서)."""
    checked = _check_images(images)
    with db.connect() as conn:
        _save_images(conn, pose_id, checked)
    return get_pose(pose_id)


def list_poses(owner_scope) -> list[dict]:
    """owner_scope가 None이면(admin) 전부, 아니면 그 회원 것만 — 최신순."""
    with db.connect() as conn:
        if owner_scope is None:
            rows = conn.execute("SELECT * FROM poses ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute("SELECT * FROM poses WHERE owner_id=? ORDER BY id DESC", (owner_scope,)).fetchall()
        return _with_images(conn, rows)


def get_pose(pose_id: int) -> dict | None:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM poses WHERE id=?", (pose_id,)).fetchall()
        return _with_images(conn, rows)[0] if rows else None


def get_pose_image(pose: dict, image_id: int | None) -> dict | None:
    """게시물의 이미지 하나 — image_id가 None이면 첫 장(옛 /image·/thumb 주소용)."""
    images = pose["images"] if image_id is None else [i for i in pose["images"] if i["id"] == image_id]
    return images[0] if images else None


def image_path(image: dict, thumb: bool) -> Path:
    return _dir() / (image["thumb_file"] if thumb else image["image_file"])
