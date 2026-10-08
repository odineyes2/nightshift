"""Library > Lighting(NS-60-1) — Pose와 같은 구조의 게시물 저장소(표 `lightings`·`lighting_images`, DB v24).

파일은 `data/library/lightings/` 아래 `<게시물id>_<이미지id>.<확장자>` 원본과 `…_thumb.webp` 축소본으로 둔다.
이미지 검사는 pose_library를 재사용하고, 입력 이미지 복사·다른 라이브러리 이동은 제공하지 않는다.
회원마다 자기 것만 보고, admin은 모두 본다(owner_id).
"""

from pathlib import Path

from PIL import ImageOps

import db
from data_paths import data_dir
from pose_library import EXTS, THUMB_SIZE, PoseError, _check_images


def _dir() -> Path:
    return data_dir("library/lightings")


def _with_images(conn, rows) -> list[dict]:
    """게시물마다 images(position 순)·image_count와 대표(첫 장) 주소를 붙인다."""
    items = [dict(r) for r in rows]
    by_id = {p["id"]: p for p in items}
    for p in items:
        p["images"] = []
    if by_id:
        marks = ",".join("?" * len(by_id))
        for r in conn.execute(f"SELECT * FROM lighting_images WHERE lighting_id IN ({marks})"
                              " ORDER BY lighting_id, position, id", list(by_id)):
            d = dict(r)
            base = f"/api/library/lightings/{d['lighting_id']}/images/{d['id']}"
            d["image_url"], d["thumb_url"] = f"{base}/image", f"{base}/thumb"
            by_id[d["lighting_id"]]["images"].append(d)
    for p in items:
        p["image_count"] = len(p["images"])
        first = p["images"][0] if p["images"] else {}
        p["image_url"], p["thumb_url"] = first.get("image_url", ""), first.get("thumb_url", "")
        p["source_url"] = first.get("source_url", "")
    return items


def _save_images(conn, lighting_id: int, checked: list, created_files=None) -> None:
    """검사한 이미지를 이어지는 position으로 저장한다."""
    pos = conn.execute("SELECT COALESCE(MAX(position) + 1, 0) FROM lighting_images WHERE lighting_id=?",
                       (lighting_id,)).fetchone()[0]
    for data, src, img in checked:
        image_id = conn.execute(
            "INSERT INTO lighting_images(lighting_id, image_file, thumb_file, source_url, position, created_at)"
            " VALUES (?, '', '', ?, ?, ?)", (lighting_id, src, pos, db.now_iso())).lastrowid
        filename, thumb_name = f"{lighting_id}_{image_id}.{EXTS[img.format]}", f"{lighting_id}_{image_id}_thumb.webp"
        if created_files is not None:
            created_files.extend([_dir() / filename, _dir() / thumb_name])
        (_dir() / filename).write_bytes(data)
        thumb = ImageOps.exif_transpose(img).convert("RGB")
        thumb.thumbnail((THUMB_SIZE, THUMB_SIZE))
        thumb.save(_dir() / thumb_name, "WEBP", quality=85)
        conn.execute("UPDATE lighting_images SET image_file=?, thumb_file=? WHERE id=?",
                     (filename, thumb_name, image_id))
        pos += 1


def _name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise PoseError("Lighting 명칭을 적어주세요.")
    return name


def add_lighting(owner_id, name: str, description: str, danbooru_prompt: str, images: list) -> dict:
    """images: [(바이트, 원본 주소)] 1~MAX_IMAGES장."""
    name = _name(name)
    checked = _check_images(images)
    with db.connect() as conn:
        lighting_id = conn.execute(
            "INSERT INTO lightings(name, description, danbooru_prompt, owner_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, (description or "").strip(), (danbooru_prompt or "").strip(), owner_id, db.now_iso())).lastrowid
        _save_images(conn, lighting_id, checked)
    return get_lighting(lighting_id)


def add_images(lighting_id: int, images: list) -> dict:
    """기존 게시물에 이미지를 덧붙인다(position은 이어서)."""
    checked = _check_images(images)
    with db.connect() as conn:
        _save_images(conn, lighting_id, checked)
    return get_lighting(lighting_id)


def update_lighting(lighting_id: int, fields: dict) -> dict:
    """명칭·설명·danbooru prompt 중 온 것만 바꾼다(명칭은 비울 수 없다)."""
    sets = {k: (str(fields[k] or "")).strip() for k in ("name", "description", "danbooru_prompt") if k in fields}
    if "name" in sets:
        sets["name"] = _name(sets["name"])
    if sets:
        with db.connect() as conn:
            conn.execute(f"UPDATE lightings SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                         [*sets.values(), lighting_id])
    return get_lighting(lighting_id)


def _unlink(image: dict) -> None:
    for f in (image["image_file"], image["thumb_file"]):
        if f:
            (_dir() / f).unlink(missing_ok=True)


def delete_lighting(lighting: dict) -> None:
    """게시물 행(이미지 행은 CASCADE)과 파일을 모두 지운다."""
    with db.connect() as conn:
        conn.execute("DELETE FROM lightings WHERE id=?", (lighting["id"],))
    for image in lighting["images"]:
        _unlink(image)


def delete_image(lighting: dict, image_id: int) -> dict:
    """이미지 한 장을 지운다 — 마지막 한 장은 지우지 못한다(게시물을 지운다)."""
    image = get_image(lighting, image_id)
    if not image:
        raise LookupError(image_id)
    if len(lighting["images"]) <= 1:
        raise PoseError("마지막 이미지는 지울 수 없어요. 게시물을 지워주세요.")
    with db.connect() as conn:
        conn.execute("DELETE FROM lighting_images WHERE id=?", (image_id,))
    _unlink(image)
    return get_lighting(lighting["id"])


def list_lightings(owner_scope) -> list[dict]:
    """owner_scope가 None이면(admin) 전부, 아니면 그 회원 것만 — 최신순."""
    with db.connect() as conn:
        if owner_scope is None:
            rows = conn.execute("SELECT * FROM lightings ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute("SELECT * FROM lightings WHERE owner_id=? ORDER BY id DESC",
                                (owner_scope,)).fetchall()
        return _with_images(conn, rows)


def get_lighting(lighting_id: int) -> dict | None:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM lightings WHERE id=?", (lighting_id,)).fetchall()
        return _with_images(conn, rows)[0] if rows else None


def get_image(lighting: dict, image_id: int | None) -> dict | None:
    """게시물의 이미지 하나 — image_id가 None이면 첫 장."""
    images = lighting["images"] if image_id is None else [i for i in lighting["images"] if i["id"] == image_id]
    return images[0] if images else None


def image_path(image: dict, thumb: bool) -> Path:
    return _dir() / (image["thumb_file"] if thumb else image["image_file"])

