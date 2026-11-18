"""LoRA 사용 권한 — 회원이 학습한 인물 LoRA는 그 회원만 쓰고, 공개 LoRA는 누구나 쓴다.

lora_access 표(db.py v34)에 줄이 없는 LoRA는 공개다(관리자가 등록한 모델, 예전 LoRA). 학습으로 생긴 LoRA는 주인과
공개 여부를 적는다: 연동 앱(charchat)의 운영진 전속 배우는 public, 회원이 만든 배우는 private.
관리자는 언제나 쓸 수 있다. 서비스 연동 계정이 관리자 대신 보낸 요청은 role이 user여도 관리자 id라 주인 비교로 통과한다.
지키는 곳: /api/generate·/api/generate/refine의 loras(generate_models.clean_loras), 워커 자동 설치(13-jobs-api).
"""

import auth
import db

VISIBILITIES = ("public", "private")


def lookup(filename: str):
    with db.read() as conn:
        return conn.execute("SELECT owner_id, visibility FROM lora_access WHERE filename=?", (filename,)).fetchone()


def can_use(filename: str, user_id) -> bool:
    row = lookup(filename)
    if row is None or row["visibility"] == "public":
        return True
    if user_id is not None and row["owner_id"] == user_id:
        return True
    return user_id is not None and auth.is_admin(auth.get_user(user_id))


def set_access(filename: str, owner_id, visibility: str) -> None:
    if visibility not in VISIBILITIES:
        raise ValueError("공개 여부는 public 또는 private이어야 해요.")
    with db.connect() as conn:
        conn.execute("INSERT INTO lora_access(filename, owner_id, visibility, created_at) VALUES(?,?,?,?)"
                     " ON CONFLICT(filename) DO UPDATE SET owner_id=excluded.owner_id, visibility=excluded.visibility",
                     (filename, owner_id, visibility, db.now_iso()))
