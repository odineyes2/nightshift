#!/usr/bin/env bash
# RunPod 같은 pod는 보통 네트워크 볼륨을 /workspace에만 마운트한다. 이 저장소
# (/workspace/nightshift)는 거기 있어서 pod를 재시작해도 남아있지만, pip로 설치한
# 패키지나 apt로 설치한 Node.js는 컨테이너 시스템 경로(/usr 등)에 들어가서
# 네트워크 볼륨 바깥이다 — 그래서 pod가 재시작될 때마다 컨테이너가 기본 이미지로
# 초기화되면서 매번 같이 사라진다.
#
# 이 스크립트는 그걸 확인해서 없는 것만 골라 다시 설치한 뒤, pm2로 서버까지
# 띄운다. 이미 다 설치돼 있으면(같은 세션에서 다시 실행한 경우) 대부분 빠르게
# 지나간다 — pod를 새로 시작할 때마다 이 스크립트 하나만 실행하면 된다.
#
# 사용법:
#   cd /workspace/nightshift && ./bootstrap.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "[bootstrap] 1/3 Python 의존성 설치 확인 (requirements.txt)..."
python3 -m pip install -q -r requirements.txt

echo "[bootstrap] 2/3 Node.js 확인..."
if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
  echo "[bootstrap]   이미 설치돼 있음 ($(node -v)) — 건너뜀"
else
  echo "[bootstrap]   Node.js가 없어 새로 설치합니다 (NodeSource, Node 20 LTS)..."
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y nodejs
fi

echo "[bootstrap] 3/3 pm2 설치 확인 + 서버 시작..."
npm install
npx pm2 delete nightshift >/dev/null 2>&1 || true
npm start
