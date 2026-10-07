"""Library Pose↔Position 게시물 이동(NS-46) — 대상 쪽에 같은 게시물을 만들고 원본을 지운다.

명칭·설명·danbooru prompt·owner와 이미지(순서·원본 주소)를 그대로 옮긴다. 장 수 상한(MAX_IMAGES)은
업로드용이라 걸지 않는다. 대상 저장 중 실패하면 DB는 되돌리고 새로 쓴 파일을 지우며 원본은 그대로 둔다.
새 게시물 id는 원본과 달라진다.
"""

import db
import pose_library
import position_library
from pose_library import _open_image


def _checked(images: list, image_path) -> list:
    """원본 이미지 파일을 (바이트, 원본 주소, 검사한 이미지)로 읽는다 — 하나라도 못 읽으면 아무것도 옮기지 않는다."""
    out = []
    for image in images:
        data = image_path(image, False).read_bytes()
        out.append((data, image["source_url"] or "", _open_image(data)))
    if not out:
        raise pose_library.PoseError("옮길 이미지가 없어요.")
    return out


def _cleanup(target_dir, new_id) -> None:
    if new_id is not None:
        for f in target_dir.glob(f"{new_id}_*"):
            f.unlink(missing_ok=True)


def move_pose_to_position(pose: dict) -> dict:
    checked = _checked(pose["images"], pose_library.image_path)
    new_id = None
    try:
        with db.connect() as conn:
            new_id = conn.execute(
                "INSERT INTO positions(name, description, danbooru_prompt, owner_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (pose["name"], pose["description"] or "", pose["danbooru_prompt"] or "", pose["owner_id"],
                 db.now_iso())).lastrowid
            position_library._save_images(conn, new_id, checked)
    except Exception:
        _cleanup(position_library._dir(), new_id)
        raise
    pose_library.delete_pose(pose)
    return position_library.get_position(new_id)


def move_position_to_pose(position: dict) -> dict:
    checked = _checked(position["images"], position_library.image_path)
    new_id = None
    try:
        with db.connect() as conn:
            new_id = conn.execute(
                "INSERT INTO poses(name, description, danbooru_prompt, image_file, source_url, owner_id, created_at)"
                " VALUES (?, ?, ?, '', ?, ?, ?)",
                (position["name"], position["description"] or "", position["danbooru_prompt"] or "", checked[0][1],
                 position["owner_id"], db.now_iso())).lastrowid
            # 옛 코드용 첫 장 칸(poses.image_file)도 채운다 — add_pose와 같다.
            conn.execute("UPDATE poses SET image_file=? WHERE id=?",
                         (pose_library._save_images(conn, new_id, checked), new_id))
    except Exception:
        _cleanup(pose_library._dir(), new_id)
        raise
    position_library.delete_position(position)
    return pose_library.get_pose(new_id)
