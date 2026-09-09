#!/usr/bin/env python3
"""
send_images_email.py
---------------------
/workspace/output 폴더의 이미지 파일들을 자동으로 찾아
용량 한도 안에서 최대한 묶어 이메일로 발송하는 스크립트 (RunPod nightshift 작업용).
이미지 1장이 1~2MB 수준이라고 가정하고, 한 메일에 여러 장을 첨부해서 보내고
전체 용량이 한도를 넘으면 자동으로 여러 통으로 나눠 보낸다.

사용 전:
  아래 CONFIG 섹션의 SMTP_USER / SMTP_PASSWORD / TO_EMAIL 을 채워 넣을 것.
  (Gmail 기준: 2단계 인증 켠 뒤 https://myaccount.google.com/apppasswords 에서
   앱 비밀번호 16자리 발급해서 사용)

사용법:
  python send_images_email.py
  # SEARCH_DIR(/workspace/output)의 이미지들을 모아서
  # 한 통당 MAX_MB 이내로 묶어 필요한 만큼 여러 통으로 발송

  python send_images_email.py --max-mb 15   # 메일 1통당 용량 한도 조정
"""

import argparse
import os
import smtplib
import sys
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from pathlib import Path

# ===================== CONFIG (직접 채워 넣기) =====================
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = "odineyes2@gmail.com"        # 보내는 Gmail 주소
SMTP_PASSWORD = "-"  # Gmail 앱 비밀번호 (16자리, 공백 없이)
TO_EMAIL = "odineyes@naver.com"        # 받는 사람 이메일 주소

SEARCH_DIR = "/workspace/output"          # 이미지가 생기는 폴더
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
# ====================================================================


def find_image_files() -> list[Path]:
    if not Path(SEARCH_DIR).exists():
        print(f"오류: 폴더를 찾을 수 없습니다 - {SEARCH_DIR}")
        sys.exit(1)

    files = [
        p for p in sorted(Path(SEARCH_DIR).iterdir())
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    ]
    if not files:
        print(f"오류: {SEARCH_DIR} 에서 이미지 파일을 찾지 못했습니다.")
        sys.exit(1)
    return files


def batch_files(files: list[Path], max_bytes: int) -> list[list[Path]]:
    """용량 한도를 넘지 않는 선에서 파일들을 묶음(batch)으로 나눈다."""
    batches: list[list[Path]] = []
    current: list[Path] = []
    current_size = 0

    for f in files:
        size = f.stat().st_size
        if size > max_bytes:
            # 한 장이 단독으로 한도를 넘으면 별도 배치로 그냥 보낸다 (경고만 출력)
            print(f"  경고: {f.name} ({size/1024/1024:.1f} MB)이 한도({max_bytes/1024/1024:.0f} MB)를 초과합니다. 단독 발송합니다.")
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


def send_email_with_attachments(to_addr: str, subject: str, body: str, attachment_paths: list[Path]):
    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
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

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)


def main():
    parser = argparse.ArgumentParser(description="이미지 폴더를 묶어서 이메일로 발송")
    parser.add_argument("--to", default=TO_EMAIL, help="받는 사람 이메일 주소 (기본: CONFIG의 TO_EMAIL)")
    parser.add_argument("--max-mb", type=int, default=20, help="메일 1통당 최대 첨부 용량(MB), 기본 20")
    args = parser.parse_args()

    if "your_email" in SMTP_USER or "your_app_password" in SMTP_PASSWORD:
        print("오류: 스크립트 상단 CONFIG의 SMTP_USER / SMTP_PASSWORD를 먼저 채워 넣으세요.")
        sys.exit(1)

    files = find_image_files()
    total_mb = sum(f.stat().st_size for f in files) / (1024 * 1024)
    print(f"이미지 {len(files)}개 발견, 총 {total_mb:.1f} MB")

    max_bytes = args.max_mb * 1024 * 1024
    batches = batch_files(files, max_bytes)
    total_batches = len(batches)
    print(f"{total_batches}통으로 나눠 발송합니다.")

    for i, batch in enumerate(batches, start=1):
        names = ", ".join(p.name for p in batch)
        batch_mb = sum(p.stat().st_size for p in batch) / (1024 * 1024)
        print(f"  [{i}/{total_batches}] {len(batch)}개 파일 ({batch_mb:.1f} MB) 전송 중...")
        body = f"이미지 {len(batch)}개를 첨부합니다. ({i}/{total_batches}통)\n\n" + "\n".join(p.name for p in batch)
        send_email_with_attachments(
            args.to,
            subject=f"[이미지 전송 {i}/{total_batches}] {len(batch)}개 파일",
            body=body,
            attachment_paths=batch,
        )

    print("전송 완료.")


if __name__ == "__main__":
    main()
