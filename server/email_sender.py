"""
결과 이미지 이메일 발송 — send_images_email.py(주피터랩에서 배치 완료 후 수동으로
실행하던 스크립트)를 nightshift 웹 프론트엔드에서 바로 실행할 수 있도록 옮긴 모듈.

SMTP 계정 정보(보내는 메일 계정/비밀번호/받는 메일 계정)는 매 요청마다 프론트엔드에서
입력받아 그 자리에서 발송에만 쓰고 어디에도 저장하지 않는다 — jobs_state.json은 git으로
버전 관리되므로, 비밀번호를 job 데이터의 일부로 저장하면 커밋에 그대로 남는다.

환경변수:
    NIGHTSHIFT_OUTPUT_DIR  이미지가 쌓이는 폴더 (기본 /workspace/output, output_images.py 참고)
    NIGHTSHIFT_SMTP_HOST   SMTP 서버 주소 (기본 smtp.gmail.com)
    NIGHTSHIFT_SMTP_PORT   SMTP 포트 (기본 587)
"""

import os
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from output_images import OUTPUT_DIR, OutputFolderError, list_output_images

SMTP_HOST = os.environ.get("NIGHTSHIFT_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("NIGHTSHIFT_SMTP_PORT", "587"))


class EmailSendError(Exception):
    """발송을 진행할 수 없는 상황(설정 오류, 인증 실패 등)을 사용자가 읽을 메시지와 함께 전달."""


def find_image_files(search_dir: str) -> list[Path]:
    """list_output_images와 같지만, 이미지가 하나도 없으면 에러로 취급한다
    (이메일 발송/zip 다운로드처럼 '뭔가 있어야' 의미 있는 동작에서 사용)."""
    try:
        files = list_output_images(search_dir)
    except OutputFolderError as e:
        raise EmailSendError(str(e)) from e
    if not files:
        raise EmailSendError(f"{search_dir}에서 이미지 파일을 찾지 못했습니다.")
    return files


def batch_files(files: list[Path], max_bytes: int) -> list[list[Path]]:
    """용량 한도를 넘지 않는 선에서 파일들을 묶음(batch)으로 나눈다."""
    batches: list[list[Path]] = []
    current: list[Path] = []
    current_size = 0

    for f in files:
        size = f.stat().st_size
        if size > max_bytes:
            # 한 장이 단독으로 한도를 넘으면 별도 배치로 그냥 보낸다.
            if current:
                batches.append(current)
                current, current_size = [], 0
            batches.append([f])
            continue

        if current and current_size + size > max_bytes:
            batches.append(current)
            current, current_size = [], 0

        current.append(f)
        current_size += size

    if current:
        batches.append(current)

    return batches


def send_email_with_attachments(smtp_user: str, smtp_password: str, to_addr: str, subject: str, body: str, attachment_paths: list[Path]):
    msg = MIMEMultipart()
    msg["From"] = smtp_user
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    for path in attachment_paths:
        with open(path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{path.name}"')
        msg.attach(part)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)


def send_output_images(smtp_user: str, smtp_password: str, to_email: str, max_mb: int = 20, search_dir: str | None = None) -> dict:
    """search_dir(기본 OUTPUT_DIR)의 이미지들을 모아 한 통당 max_mb 이내로 묶어서
    필요한 만큼 여러 통으로 발송하고, 결과 요약을 반환한다."""
    search_dir = search_dir or OUTPUT_DIR
    files = find_image_files(search_dir)
    max_bytes = max_mb * 1024 * 1024
    batches = batch_files(files, max_bytes)
    total_batches = len(batches)

    sent = []
    for i, batch in enumerate(batches, start=1):
        subject = f"[이미지 전송 {i}/{total_batches}] {len(batch)}개 파일"
        body = f"이미지 {len(batch)}개를 첨부합니다. ({i}/{total_batches}통)\n\n" + "\n".join(p.name for p in batch)
        try:
            send_email_with_attachments(smtp_user, smtp_password, to_email, subject, body, batch)
        except smtplib.SMTPAuthenticationError as e:
            raise EmailSendError(
                f"{i}/{total_batches}통째에서 SMTP 로그인에 실패했습니다. "
                "보내는 메일 계정과 비밀번호(Gmail이면 앱 비밀번호)를 확인하세요."
            ) from e
        except OSError as e:
            raise EmailSendError(f"{i}/{total_batches}통째 발송 중 오류가 발생했습니다: {e}") from e
        sent.append({
            "batch": i,
            "files": [p.name for p in batch],
            "size_mb": round(sum(p.stat().st_size for p in batch) / (1024 * 1024), 1),
        })

    return {
        "total_files": len(files),
        "total_batches": total_batches,
        "to_email": to_email,
        "batches": sent,
    }
