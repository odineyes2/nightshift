"""Library > Position(NS-43) — Pose와 같은 구조의 게시물 저장소(표 `positions`·`position_images`, DB v21).

파일은 `data/library/positions/` 아래 `<게시물id>_<이미지id>.<확장자>` 원본과 `…_thumb.webp` 축소본으로 둔다.
이미지 검사·URL 받기는 pose_library를 그대로 쓰고, Pose에 없는 수정·게시물 삭제·이미지 한 장 삭제를 더한다.
회원마다 자기 것만 보고, admin은 모두 본다(owner_id).
"""

from pathlib import Path

from PIL import ImageOps

import db
from data_paths import data_dir
from pose_library import EXTS, THUMB_SIZE, PoseError, _check_images, input_bytes


def _dir() -> Path:
    return data_dir("library/positions")


def _with_images(conn, rows) -> list[dict]:
    """게시물마다 images(position 순)·image_count와 대표(첫 장) 주소를 붙인다."""
    items = [dict(r) for r in rows]
    by_id = {p["id"]: p for p in items}
    for p in items:
        p["images"] = []
    if by_id:
        marks = ",".join("?" * len(by_id))
        for r in conn.execute(f"SELECT * FROM position_images WHERE position_id IN ({marks})"
                              " ORDER BY position_id, position, id", list(by_id)):
            d = dict(r)
            base = f"/api/library/positions/{d['position_id']}/images/{d['id']}"
            d["image_url"], d["thumb_url"] = f"{base}/image", f"{base}/thumb"
            by_id[d["position_id"]]["images"].append(d)
    for p in items:
        p["image_count"] = len(p["images"])
        first = p["images"][0] if p["images"] else {}
        p["image_url"], p["thumb_url"] = first.get("image_url", ""), first.get("thumb_url", "")
        p["source_url"] = first.get("source_url", "")
    return items


def _save_images(conn, position_id: int, checked: list, created_files=None) -> None:
    """검사한 이미지를 이어지는 position으로 저장한다."""
    pos = conn.execute("SELECT COALESCE(MAX(position) + 1, 0) FROM position_images WHERE position_id=?",
                       (position_id,)).fetchone()[0]
    for data, src, img in checked:
        image_id = conn.execute(
            "INSERT INTO position_images(position_id, image_file, thumb_file, source_url, position, created_at)"
            " VALUES (?, '', '', ?, ?, ?)", (position_id, src, pos, db.now_iso())).lastrowid
        filename, thumb_name = f"{position_id}_{image_id}.{EXTS[img.format]}", f"{position_id}_{image_id}_thumb.webp"
        if created_files is not None:
            created_files.extend([_dir() / filename, _dir() / thumb_name])
        (_dir() / filename).write_bytes(data)
        thumb = ImageOps.exif_transpose(img).convert("RGB")
        thumb.thumbnail((THUMB_SIZE, THUMB_SIZE))
        thumb.save(_dir() / thumb_name, "WEBP", quality=85)
        conn.execute("UPDATE position_images SET image_file=?, thumb_file=? WHERE id=?",
                     (filename, thumb_name, image_id))
        pos += 1


def _name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise PoseError("Position 명칭을 적어주세요.")
    return name


def add_position(owner_id, name: str, description: str, danbooru_prompt: str, images: list) -> dict:
    """images: [(바이트, 원본 주소)] 1~MAX_IMAGES장."""
    name = _name(name)
    checked = _check_images(images)
    with db.connect() as conn:
        position_id = conn.execute(
            "INSERT INTO positions(name, description, danbooru_prompt, owner_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, (description or "").strip(), (danbooru_prompt or "").strip(), owner_id, db.now_iso())).lastrowid
        _save_images(conn, position_id, checked)
    return get_position(position_id)


def add_images(position_id: int, images: list) -> dict:
    """기존 게시물에 이미지를 덧붙인다(position은 이어서)."""
    checked = _check_images(images)
    with db.connect() as conn:
        _save_images(conn, position_id, checked)
    return get_position(position_id)


def update_position(position_id: int, fields: dict) -> dict:
    """명칭·설명·danbooru prompt 중 온 것만 바꾼다(명칭은 비울 수 없다)."""
    sets = {k: (str(fields[k] or "")).strip() for k in ("name", "description", "danbooru_prompt") if k in fields}
    if "name" in sets:
        sets["name"] = _name(sets["name"])
    if sets:
        with db.connect() as conn:
            conn.execute(f"UPDATE positions SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                         [*sets.values(), position_id])
    return get_position(position_id)


def _unlink(image: dict) -> None:
    for f in (image["image_file"], image["thumb_file"]):
        if f:
            (_dir() / f).unlink(missing_ok=True)


def delete_position(position: dict) -> None:
    """게시물 행(이미지 행은 CASCADE)과 파일을 모두 지운다."""
    with db.connect() as conn:
        conn.execute("DELETE FROM positions WHERE id=?", (position["id"],))
    for image in position["images"]:
        _unlink(image)


def delete_image(position: dict, image_id: int) -> dict:
    """이미지 한 장을 지운다 — 마지막 한 장은 지우지 못한다(게시물을 지운다)."""
    image = get_image(position, image_id)
    if not image:
        raise LookupError(image_id)
    if len(position["images"]) <= 1:
        raise PoseError("마지막 이미지는 지울 수 없어요. 게시물을 지워주세요.")
    with db.connect() as conn:
        conn.execute("DELETE FROM position_images WHERE id=?", (image_id,))
    _unlink(image)
    return get_position(position["id"])


def list_positions(owner_scope) -> list[dict]:
    """owner_scope가 None이면(admin) 전부, 아니면 그 회원 것만 — 최신순."""
    with db.connect() as conn:
        if owner_scope is None:
            rows = conn.execute("SELECT * FROM positions ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute("SELECT * FROM positions WHERE owner_id=? ORDER BY id DESC",
                                (owner_scope,)).fetchall()
        return _with_images(conn, rows)


def get_position(position_id: int) -> dict | None:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchall()
        return _with_images(conn, rows)[0] if rows else None


def get_image(position: dict, image_id: int | None) -> dict | None:
    """게시물의 이미지 하나 — image_id가 None이면 첫 장."""
    images = position["images"] if image_id is None else [i for i in position["images"] if i["id"] == image_id]
    return images[0] if images else None


def image_path(image: dict, thumb: bool) -> Path:
    return _dir() / (image["thumb_file"] if thumb else image["image_file"])


def input_copy(position: dict, image: dict) -> tuple[str, bytes]:
    """입력 이미지 풀에 넣을 (파일 이름, 바이트) — Pose와 같은 번호의 게시물과 겹치지 않게 library_position_ 이름."""
    data, ext = input_bytes(image_path(image, False))
    return f"library_position_{position['id']}_{image['id']}{ext}", data
