"""명시적 작업 생성원에 해당하는 PNG를 원본 라이브러리에 한 번만 등록한다."""

import json
import logging
import re
from pathlib import Path

import db
import pose_library
import position_library

log = logging.getLogger(__name__)
# 색인은 주기적으로 반복되므로 같은 (작업, 사유) 진단은 프로세스마다 한 번만 남긴다.
_warned = set()


def _diagnose(job_id, reason):
    """작업 ID와 사유 코드만 남긴다(프롬프트·이미지·경로는 기록하지 않는다)."""
    if (job_id, reason) not in _warned:
        _warned.add((job_id, reason))
        log.warning("라이브러리 자동 등록 누락: job=%s reason=%s", job_id, reason)


def match_path(job, path):
    """(생성원, 사유 코드)를 돌려준다. 생성원이 없고 사유도 None이면 라이브러리 생성이 아닌 일반 작업이다.
    seed_batch 실행기의 전용 저장 이름만 받아 다른 결과를 추측하지 않는다."""
    mapping = job.get("library_output_origins") or {}
    context = job.get("library_generation_context") or []
    if not context:
        return None, ("missing_origin" if mapping else None)
    if not mapping.get("save_node_id"):
        return None, "no_save_node_mapping"
    match = re.fullmatch(re.escape(job["id"]) +
                        r"/seed_batch_([1-9][0-9]*)_seed([0-9]+)_([0-9]+)_\.png", path)
    if not match or int(match[2]) > 2**31 - 1 or int(match[3]) < 1:
        return None, "output_name_mismatch"
    if job.get("options", {}).get("seed_mode") == "sequential" and int(match[2]) != int(match[1]) - 1:
        return None, "output_name_mismatch"
    origin = mapping.get("executions", {}).get(match[1])
    if not origin:
        return None, "execution_unmapped"
    # 실행 매핑과 제출 시 검증된 선택 스냅샷을 함께 확인한다.
    if origin not in context or origin.get("source_kind") not in ("pose", "position"):
        return None, "origin_mismatch"
    return origin, None


def origin_for_path(job, path):
    return match_path(job, path)[0]


def _record(conn, path, origin, status, reason="", image_id=None):
    conn.execute(
        "INSERT INTO library_generated_outputs(path, source_kind, article_id, image_id, status, reason, updated_at)"
        " VALUES(?,?,?,?,?,?,?) ON CONFLICT(path, source_kind, article_id) DO UPDATE SET"
        " image_id=excluded.image_id, status=excluded.status, reason=excluded.reason, updated_at=excluded.updated_at"
        " WHERE library_generated_outputs.status != 'registered'",
        (path, origin["source_kind"], origin["article_id"], image_id, status, reason, db.now_iso()))


def _register(base, path):
    created_files = []
    origin = None
    try:
        with db.connect() as conn:
            # 검사·복사·이미지 행·이력 기록을 하나의 쓰기 트랜잭션으로 직렬화한다.
            conn.execute("UPDATE library_generated_outputs SET updated_at=updated_at WHERE 0")
            asset = conn.execute("SELECT * FROM assets WHERE path=? AND kind='image' AND deleted_at IS NULL",
                                 (path,)).fetchone()
            if not asset or not asset["job_id"]:
                return
            row = conn.execute("SELECT owner_id, data_json, deleted_at FROM jobs WHERE id=?",
                               (asset["job_id"],)).fetchone()
            if not row or row["deleted_at"]:
                return
            job = json.loads(row["data_json"])
            job["id"] = asset["job_id"]
            origin, reason = match_path(job, path)
            if origin is None:
                if reason:
                    _diagnose(job["id"], reason)
                return
            key = (path, origin["source_kind"], origin["article_id"])
            previous = conn.execute("SELECT status, reason FROM library_generated_outputs"
                                    " WHERE path=? AND source_kind=? AND article_id=?", key).fetchone()
            if previous and (previous["status"] == "registered" or
                             previous["reason"] in ("source_deleted", "source_image_deleted")):
                return  # 사용자가 등록 이미지를 지운 경우도 완료 이력을 유지한다.
            kind = origin["source_kind"]
            table, images, column, lib = (("poses", "pose_images", "pose_id", pose_library) if kind == "pose"
                                          else ("positions", "position_images", "position_id", position_library))
            article = conn.execute(f"SELECT owner_id FROM {table} WHERE id=?", (origin["article_id"],)).fetchone()
            user = conn.execute("SELECT role, status FROM users WHERE id=?", (row["owner_id"],)).fetchone()
            reason = None
            if not article:
                reason = "source_deleted"
            elif (article["owner_id"] != origin.get("owner_id") or asset["owner_id"] != row["owner_id"]
                  or not user or user["status"] != "active"
                  or (user["role"] != "admin" and str(article["owner_id"]) != str(row["owner_id"]))):
                reason = "permission_mismatch"
            elif not conn.execute(f"SELECT id FROM {images} WHERE id=? AND {column}=?",
                                  (origin["source_image_id"], origin["article_id"])).fetchone():
                reason = "source_image_deleted"
            if reason:
                _record(conn, path, origin, "skipped", reason)
                _diagnose(job["id"], reason)
                return
            source = (base / path).resolve()
            if not source.is_relative_to(base.resolve()):
                raise ValueError("출력 폴더 밖의 이미지")
            checked = pose_library._check_images([(source.read_bytes(), "")])
            try:
                lib._save_images(conn, origin["article_id"], checked, created_files)
                image_id = conn.execute(f"SELECT id FROM {images} WHERE {column}=? ORDER BY position DESC, id DESC LIMIT 1",
                                        (origin["article_id"],)).fetchone()[0]
                _record(conn, path, origin, "registered", image_id=image_id)
            except Exception:
                # 쓰기 잠금을 풀기 전에 정리해 다른 업로드가 같은 이미지 번호를 재사용해도 안전하다.
                _cleanup(created_files)
                created_files.clear()
                raise
    except Exception as exc:
        # DB 롤백과 함께 이번 시도에서 만든 파일만 지운다. 실패 이력은 다음 색인에서 재시도한다.
        _cleanup(created_files)
        if origin is not None:
            # 실패는 재시도되므로 반복될 때마다 남긴다.
            log.warning("라이브러리 자동 등록 실패: job=%s reason=copy_failed:%s", job["id"], type(exc).__name__)
            try:
                with db.connect() as conn:
                    _record(conn, path, origin, "retry", type(exc).__name__)
            except Exception:
                log.exception("라이브러리 실패 이력 기록 불가: %s", path)
        else:
            log.warning("라이브러리 결과 확인 실패: %s (%s)", path, type(exc).__name__)


def _cleanup(files):
    for file in files:
        try:
            file.unlink(missing_ok=True)
        except OSError:
            log.warning("라이브러리 임시 파일 정리 실패: %s", file)


def sync(base: Path, found: dict):
    """갤러리 색인 연결이 닫힌 다음 실행한다. 부분 완료 작업도 현재 있는 PNG부터 등록한다."""
    for path, (_, kind) in found.items():
        if kind == "image":
            _register(base, path)
