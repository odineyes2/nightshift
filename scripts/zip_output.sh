#!/usr/bin/env bash
# zip_output.sh — /workspace/output 폴더의 이미지를 zip으로 압축
#
# 사용법:
#   bash zip_output.sh
#   bash zip_output.sh /workspace/output my_images   # 폴더/파일명 직접 지정
#
# 옵션 (환경변수로 조정 가능):
#   SOURCE_DIR   압축할 폴더 (기본: /workspace/output)
#   OUT_NAME     결과 zip 파일 이름, 확장자 제외 (기본: output_YYYYMMDD_HHMMSS)
#   SPLIT_SIZE   지정 시 이 크기 단위로 분할 압축 (예: 1g, 500m). 미지정 시 분할 안 함.

set -euo pipefail

SOURCE_DIR="${1:-${SOURCE_DIR:-/workspace/output}}"
OUT_NAME="${2:-${OUT_NAME:-output_$(date +%Y%m%d_%H%M%S)}}"
SPLIT_SIZE="${SPLIT_SIZE:-}"

if [ ! -d "$SOURCE_DIR" ]; then
  echo "오류: 폴더가 존재하지 않습니다: $SOURCE_DIR" >&2
  exit 1
fi

DEST_DIR="$(dirname "$SOURCE_DIR")"
ZIP_PATH="${DEST_DIR}/${OUT_NAME}.zip"

FILE_COUNT=$(find "$SOURCE_DIR" -maxdepth 1 -type f | wc -l)
if [ "$FILE_COUNT" -eq 0 ]; then
  echo "경고: ${SOURCE_DIR} 안에 압축할 파일이 없습니다." >&2
  exit 1
fi

echo "압축 대상: $SOURCE_DIR (파일 ${FILE_COUNT}개)"
echo "결과 파일: $ZIP_PATH"

# zip 명령이 없으면 설치 (Debian/Ubuntu 계열 클라우드 이미지 기준)
if ! command -v zip >/dev/null 2>&1; then
  echo "zip 명령이 없어 설치를 시도합니다..."
  apt-get update -qq && apt-get install -y -qq zip
fi

cd "$SOURCE_DIR"

if [ -n "$SPLIT_SIZE" ]; then
  # 용량이 커서 분할이 필요한 경우: output.zip, output.z01, output.z02 ...
  zip -r -s "$SPLIT_SIZE" "$ZIP_PATH" . -x "*.zip"
  echo "완료: 분할 압축 (${SPLIT_SIZE} 단위) → ${ZIP_PATH%.zip}.z* 파일들"
else
  zip -r "$ZIP_PATH" . -x "*.zip"
  echo "완료: $ZIP_PATH"
fi

du -sh "$ZIP_PATH" 2>/dev/null || true
