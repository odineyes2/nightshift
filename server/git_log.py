"""DB 탭의 두 번째 표(업데이트 내역) — nightshift 저장소의 git 커밋 로그를 그대로
읽어서 보여준다.

따로 기록하는 동작이 없다: 커밋 메시지(제목+본문)가 이미 "무엇을 왜 바꿨는지"를
담고 있으므로, 매 요청마다 `git log`를 다시 읽을 뿐 아무것도 저장하지 않는다.
그래서 평소처럼 커밋만 하면(이 세션에서 계속 해온 대로 제목+이유를 담아) 자동으로
반영되고, Claude가 이 화면을 위해 따로 도구를 부르거나 기록을 남길 필요가 없다.

"버전" 칸은 이 저장소에 아직 git 태그/버전 번호가 없어서 커밋 짧은 해시로 채운다
(사용자 확인, 2026-09-24) — 나중에 실제 태그를 달면 있는 커밋에 한해 태그도 같이
보여준다(%d로 얻은 decorate 문자열에서 뽑음).
"""
import re
import subprocess

_SEP_FIELD = "\x1f"
_SEP_RECORD = "\x1e"
_TAG_RE = re.compile(r"tag:\s*([^,)]+)")
_TRAILER_LINE_RE = re.compile(r"^[A-Za-z][\w-]*:\s?\S")
_LOG_FORMAT = (
    f"%H{_SEP_FIELD}%h{_SEP_FIELD}%ad{_SEP_FIELD}%an{_SEP_FIELD}%s{_SEP_FIELD}%b{_SEP_FIELD}%d{_SEP_RECORD}"
)


def _strip_trailers(body):
    """맨 끝의 `Co-Authored-By: ...`류 git 트레일러 줄(들)을 지운다 — 매 커밋 끝에
    똑같이 반복돼 화면만 어지럽히고 "왜"를 설명하는 실제 내용은 아니라서다. 한글로
    시작하는 줄은 이 패턴에 안 걸리니 실제 설명을 잘못 지울 일은 없다."""
    lines = body.split("\n")
    end = len(lines)
    while end > 0 and (not lines[end - 1].strip() or _TRAILER_LINE_RE.match(lines[end - 1])):
        end -= 1
    return "\n".join(lines[:end]).strip()


def _run_git(repo_root, args, timeout=10):
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(repo_root), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def list_commits(repo_root, limit=300):
    """repo_root(nightshift 저장소 루트)의 git 커밋 로그를 최신순으로 최대 limit개
    돌려준다. git이 없거나 저장소가 아니면(예: git 없이 복사 배포한 경우) 빈 리스트."""
    log_out = _run_git(repo_root, ["log", f"--max-count={limit}", "--date=iso-strict",
                                    f"--pretty=format:{_LOG_FORMAT}"])
    if not log_out:
        return []

    records = [r for r in log_out.split(_SEP_RECORD) if r.strip("\n")]
    total_out = _run_git(repo_root, ["rev-list", "--count", "HEAD"])
    try:
        total = int(total_out.strip()) if total_out else len(records)
    except ValueError:
        total = len(records)

    commits = []
    for i, record in enumerate(records):
        parts = record.strip("\n").split(_SEP_FIELD)
        if len(parts) < 7:
            continue
        full_hash, short_hash, date, author, subject, body, decorate = parts[:7]
        commits.append({
            "seq": total - i,
            "hash": full_hash,
            "version": short_hash,
            "date": date,
            "author": author,
            "title": subject,
            "content": _strip_trailers(body),
            "tags": _TAG_RE.findall(decorate),
        })
    return commits


__all__ = ["list_commits"]
