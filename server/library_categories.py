"""사용자 정의 Library 카테고리·아티클·이미지 저장소와 파일 정리를 맡는다."""

import sqlite3
from pathlib import Path

from PIL import ImageOps

import db
from data_paths import data_dir
from pose_library import EXTS, MAX_IMAGES, PoseError, THUMB_SIZE, _check_images, apply_cover, cover_value


def _dir() -> Path:
    return data_dir("library/articles")


def _name(value, label="명칭") -> str:
    name = str(value or "").strip()
    if not name or len(name) > 100:
        raise PoseError(f"{label}은 1~100자로 적어주세요.")
    return name


def _category(conn, category_id):
    row = conn.execute("SELECT * FROM library_categories WHERE id=?", (category_id,)).fetchone()
    return dict(row) if row else None


def list_categories(scope):
    with db.connect() as conn:
        sql = "SELECT c.*, (SELECT COUNT(*) FROM library_articles a WHERE a.category_id=c.id) article_count FROM library_categories c"
        if scope is None:
            rows = conn.execute(sql + " ORDER BY c.id").fetchall()
        else:
            rows = conn.execute(sql + " WHERE c.owner_id=? ORDER BY c.id", (scope,)).fetchall()
        return [dict(row) for row in rows]


def get_category(category_id):
    with db.connect() as conn:
        return _category(conn, category_id)


def create_category(owner_id, name):
    name = _name(name, "카테고리 명칭")
    try:
        with db.connect() as conn:
            category_id = conn.execute(
                "INSERT INTO library_categories(owner_id,name,created_at) VALUES(?,?,?)",
                (owner_id, name, db.now_iso())).lastrowid
    except sqlite3.IntegrityError:
        raise PoseError("같은 이름의 카테고리가 이미 있어요.")
    return get_category(category_id)


def rename_category(category_id, name):
    name = _name(name, "카테고리 명칭")
    try:
        with db.connect() as conn:
            conn.execute("UPDATE library_categories SET name=? WHERE id=?", (name, category_id))
    except sqlite3.IntegrityError:
        raise PoseError("같은 이름의 카테고리가 이미 있어요.")
    return get_category(category_id)


def _with_images(conn, rows):
    items = [dict(row) for row in rows]
    by_id = {item["id"]: item for item in items}
    for item in items:
        item["images"] = []
    if by_id:
        marks = ",".join("?" * len(by_id))
        for row in conn.execute(
            f"SELECT * FROM library_article_images WHERE article_id IN ({marks}) ORDER BY article_id, position, id",
            list(by_id)):
            image = dict(row)
            base = f"/api/library/categories/{by_id[image['article_id']]['category_id']}/articles/{image['article_id']}/images/{image['id']}"
            image["image_url"], image["thumb_url"] = base + "/image", base + "/thumb"
            by_id[image["article_id"]]["images"].append(image)
    for item in items:
        item["image_count"] = len(item["images"])
        apply_cover(item)
        item["source_url"] = item["images"][0]["source_url"] if item["images"] else ""
    return items


def list_articles(category_id):
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM library_articles WHERE category_id=? ORDER BY id DESC", (category_id,))
        return _with_images(conn, rows)


def get_article(article_id):
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM library_articles WHERE id=?", (article_id,))
        items = _with_images(conn, rows)
        return items[0] if items else None


def _save_images(conn, article_id, checked, created):
    pos = conn.execute("SELECT COALESCE(MAX(position)+1,0) FROM library_article_images WHERE article_id=?",
                       (article_id,)).fetchone()[0]
    for data, src, image in checked:
        image_id = conn.execute(
            "INSERT INTO library_article_images(article_id,image_file,thumb_file,source_url,position,created_at) VALUES(?,'','',?,?,?)",
            (article_id, src, pos, db.now_iso())).lastrowid
        filename = f"{article_id}_{image_id}.{EXTS[image.format]}"
        thumb_name = f"{article_id}_{image_id}_thumb.webp"
        original, thumb_path = _dir() / filename, _dir() / thumb_name
        created.extend((original, thumb_path))
        original.write_bytes(data)
        thumb = ImageOps.exif_transpose(image).convert("RGB")
        thumb.thumbnail((THUMB_SIZE, THUMB_SIZE))
        thumb.save(thumb_path, "WEBP", quality=85)
        conn.execute("UPDATE library_article_images SET image_file=?,thumb_file=? WHERE id=?",
                     (filename, thumb_name, image_id))
        pos += 1


def _clean(paths):
    for path in paths:
        path.unlink(missing_ok=True)


def create_article(category_id, owner_id, name, description, danbooru_prompt, images):
    name = _name(name, "아티클 명칭")
    checked = _check_images(images)
    created = []
    try:
        with db.connect() as conn:
            article_id = conn.execute(
                "INSERT INTO library_articles(category_id,owner_id,name,description,danbooru_prompt,created_at) VALUES(?,?,?,?,?,?)",
                (category_id, owner_id, name, str(description or "").strip(),
                 str(danbooru_prompt or "").strip(), db.now_iso())).lastrowid
            _save_images(conn, article_id, checked, created)
    except Exception:
        _clean(created)
        raise
    return get_article(article_id)


def add_images(article_id, images):
    checked = _check_images(images)
    article = get_article(article_id)
    if len(article["images"]) + len(checked) > MAX_IMAGES:
        raise PoseError(f"아티클에는 이미지를 {MAX_IMAGES}장까지 넣을 수 있어요.")
    created = []
    try:
        with db.connect() as conn:
            _save_images(conn, article_id, checked, created)
    except Exception:
        _clean(created)
        raise
    return get_article(article_id)


def update_article(article_id, fields):
    values = {key: str(fields[key] or "").strip() for key in ("name", "description", "danbooru_prompt") if key in fields}
    if "name" in values:
        values["name"] = _name(values["name"], "아티클 명칭")
    if "cover_image_id" in fields:
        values["cover_image_id"] = cover_value(get_article(article_id), fields["cover_image_id"])
    if values:
        with db.connect() as conn:
            conn.execute(f"UPDATE library_articles SET {', '.join(key+'=?' for key in values)} WHERE id=?",
                         [*values.values(), article_id])
    return get_article(article_id)


def _image_paths(image):
    return [_dir() / image[key] for key in ("image_file", "thumb_file") if image[key]]


def delete_image(article, image_id):
    image = next((i for i in article["images"] if i["id"] == image_id), None)
    if image is None:
        raise LookupError(image_id)
    if len(article["images"]) <= 1:
        raise PoseError("마지막 이미지는 지울 수 없어요. 아티클을 지워주세요.")
    with db.connect() as conn:
        conn.execute("DELETE FROM library_article_images WHERE id=?", (image_id,))
        conn.execute("UPDATE library_articles SET cover_image_id=NULL WHERE id=? AND cover_image_id=?",
                     (article["id"], image_id))
    _clean(_image_paths(image))
    return get_article(article["id"])


def delete_article(article):
    with db.connect() as conn:
        conn.execute("DELETE FROM library_articles WHERE id=?", (article["id"],))
    for image in article["images"]:
        _clean(_image_paths(image))


def delete_category(category):
    articles = list_articles(category["id"])
    with db.connect() as conn:
        conn.execute("DELETE FROM library_categories WHERE id=?", (category["id"],))
    for article in articles:
        for image in article["images"]:
            _clean(_image_paths(image))
    return {"ok": True, "article_count": len(articles),
            "image_count": sum(len(article["images"]) for article in articles)}


def image_path(image, thumb=False):
    return _dir() / image["thumb_file" if thumb else "image_file"]
