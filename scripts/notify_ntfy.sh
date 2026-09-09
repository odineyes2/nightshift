#!/usr/bin/env bash
# notify_ntfy.sh — pod가 뜬 뒤 웹앱 접속 주소(RunPod 프록시 URL)를 ntfy.sh로 폰에 push
#
# RunPod pod을 재시작할 때마다 프록시 주소(https://{POD_ID}-{PORT}.proxy.runpod.net/)가
# 바뀌므로, 서버가 완전히 뜬 걸 확인한 뒤 그 주소를 ntfy.sh 토픽으로 보내 폰(ntfy 앱)
# 알림을 탭해서 바로 접속할 수 있게 한다. bootstrap.sh가 서버를 띄우기 직전에 이 스크립트를
# 백그라운드(`./scripts/notify_ntfy.sh &`)로 실행한다 — 자체적으로 서버가 응답할 때까지 기다렸다가
# 보내므로 순서를 맞추려고 sleep을 넣거나 실행 순서를 바꿀 필요가 없다.
#
# 사용법 (보통은 bootstrap.sh가 대신 호출하므로 직접 실행할 일은 거의 없음, 저장소 루트에서):
#   ./scripts/notify_ntfy.sh
#
# 환경변수 (.env 파일이 있으면 먼저 읽어들인 뒤 적용):
#   NTFY_TOPIC                  (필수) ntfy.sh 토픽 이름 — 하드코딩하지 않고 반드시 이걸로 받는다
#   NIGHTSHIFT_PORT             웹앱이 떠 있는 포트 (기본 8000) — ecosystem.config.js의
#                                --port 값을 바꿨다면 여기도 같이 바꿔야 주소가 맞는다
#   NTFY_HEALTHCHECK_TIMEOUT_SEC 헬스체크 최대 대기 시간 초 (기본 30)
#   NTFY_HEALTHCHECK_INTERVAL_SEC 헬스체크 폴링 간격 초 (기본 2)
#   NTFY_LOG_FILE                로그를 남길 파일 (기본 ./data/logs/notify_ntfy.log)
#   RUNPOD_POD_ID                RunPod가 자동으로 주입 — 없으면 RunPod pod가 아니라고
#                                판단해 조용히 건너뛴다
#
# 테스트 방법 (수동 재전송으로 확인):
#   1) 웹앱이 이미 떠 있는 상태에서 그냥 다시 실행해보면 된다 — 헬스체크를 바로
#      통과하고 몇 초 안에 알림이 온다.
#        NTFY_TOPIC=내토픽 RUNPOD_POD_ID=테스트용아이디 ./scripts/notify_ntfy.sh
#   2) 헬스체크/타임아웃 로직만 따로 확인하려면 웹앱을 잠깐 내려둔 채로 실행 —
#      NTFY_HEALTHCHECK_TIMEOUT_SEC=5 등으로 짧게 줘서 타임아웃 경고 로그가
#      남는지, 그래도 알림이 가는지 확인한다.
#        NTFY_TOPIC=내토픽 NTFY_HEALTHCHECK_TIMEOUT_SEC=5 ./scripts/notify_ntfy.sh
#   3) curl 자체만 재전송해서 ntfy 쪽 확인만 하고 싶다면:
#        curl -s -H "Title: RunPod 웹앱 준비 완료" -d "웹앱 접속 주소: https://example.com/" \
#             "https://ntfy.sh/내토픽"
#   4) 로그는 ./data/logs/notify_ntfy.log(또는 NTFY_LOG_FILE로 지정한 경로)에 계속 쌓인다.

# 헬스체크/전송이 실패해도 웹앱 실행을 막으면 안 되므로(요구사항) -e는 일부러 안 쓴다 —
# 모든 실패 지점을 if/then으로 직접 처리하고 항상 exit 0으로 끝낸다.
set -uo pipefail

# 이 스크립트는 scripts/ 아래로 옮겨졌으므로, 저장소 루트(.env·data/가 있는 곳)로
# 가려면 한 단계 위로 올라가야 한다.
cd "$(dirname "$0")/.."

# .env가 있으면 여기서 채운다 — NTFY_TOPIC 같은 민감정보를 코드에 하드코딩하지 않기 위함.
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

PORT="${NIGHTSHIFT_PORT:-8000}"
HEALTHCHECK_TIMEOUT_SEC="${NTFY_HEALTHCHECK_TIMEOUT_SEC:-30}"
HEALTHCHECK_INTERVAL_SEC="${NTFY_HEALTHCHECK_INTERVAL_SEC:-2}"
# 앱이 만들어내는 것은 전부 data/ 아래에 모은다(data_paths.py 참고).
LOG_FILE="${NTFY_LOG_FILE:-$(pwd)/data/logs/notify_ntfy.log}"

mkdir -p "$(dirname "$LOG_FILE")"

log(){
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG_FILE"
}

if [ -z "${NTFY_TOPIC:-}" ]; then
  log "[notify_ntfy] NTFY_TOPIC 환경변수가 없어 알림을 건너뜁니다. (.env 파일 또는 환경변수로 설정하세요)"
  exit 0
fi

if [ -z "${RUNPOD_POD_ID:-}" ]; then
  log "[notify_ntfy] RUNPOD_POD_ID가 없습니다 (RunPod pod가 아니거나 아직 주입 전) — 알림을 건너뜁니다."
  exit 0
fi

URL="https://${RUNPOD_POD_ID}-${PORT}.proxy.runpod.net/"

log "[notify_ntfy] 웹앱(포트 ${PORT}) 준비 확인 중 (최대 ${HEALTHCHECK_TIMEOUT_SEC}초, ${HEALTHCHECK_INTERVAL_SEC}초 간격)..."

elapsed=0
ready=0
while [ "$elapsed" -lt "$HEALTHCHECK_TIMEOUT_SEC" ]; do
  if curl -fsS -o /dev/null --max-time 3 "http://127.0.0.1:${PORT}/" 2>>"$LOG_FILE"; then
    ready=1
    break
  fi
  sleep "$HEALTHCHECK_INTERVAL_SEC"
  elapsed=$((elapsed + HEALTHCHECK_INTERVAL_SEC))
done

if [ "$ready" -eq 1 ]; then
  log "[notify_ntfy] 웹앱 응답 확인 완료 (약 ${elapsed}초 소요). 알림을 보냅니다: ${URL}"
else
  log "[notify_ntfy] 경고: ${HEALTHCHECK_TIMEOUT_SEC}초 안에 웹앱 응답을 확인하지 못했습니다. 그래도 주소는 보냅니다 — 알림 도착 시점에는 아직 안 떠 있을 수 있습니다."
fi

send_ntfy(){
  curl -fsS -H "Title: RunPod 웹앱 준비 완료" \
       -d "웹앱 접속 주소: ${URL}" \
       "https://ntfy.sh/${NTFY_TOPIC}"
}

if send_ntfy >>"$LOG_FILE" 2>&1; then
  log "[notify_ntfy] ntfy 알림 전송 성공: ${URL}"
else
  log "[notify_ntfy] ntfy 전송 1차 실패 — 2초 후 한 번 더 시도합니다."
  sleep 2
  if send_ntfy >>"$LOG_FILE" 2>&1; then
    log "[notify_ntfy] ntfy 알림 전송 성공 (재시도): ${URL}"
  else
    log "[notify_ntfy] 에러: ntfy 알림 전송에 최종 실패했습니다 (네트워크 오류 등). 웹앱 실행에는 영향 없습니다."
  fi
fi

exit 0
