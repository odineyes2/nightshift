// pm2 프로세스 정의 — nightshift 서버(server/app.py)를 pm2로 백그라운드 실행/관리한다.
// `npm start`(= `pm2 start ecosystem.config.js`)로 시작하면:
//   - 터미널을 계속 붙잡고 있지 않아도 서버가 백그라운드에서 계속 돈다
//   - `npm run status` / `pm2 status`로 살아있는지 한눈에 확인
//   - `npm run logs` / `pm2 logs nightshift`로 로그만 깔끔하게 tail
//   - server/ 안의 파이썬 파일이나 templates/, static/ 안의 파일을 고치면
//     uvicorn --reload가 감지해서 자동으로 다시 로드한다(코드 변경 시 서버를
//     직접 껐다 켤 필요 없음)
//
// 서버 프로세스 자체는 여전히 uvicorn이 띄운다 — pm2는 그 프로세스를 감독만 한다
// (죽으면 자동 재시작, 상태/로그 조회를 깔끔하게 제공).

const { execSync } = require("child_process");
const fs = require("fs");
const path = require("path");

// notify_ntfy.sh는 `source .env`로 .env를 읽어들이는데, pm2가 앱을 스폰할 때는
// 그 셸을 거치지 않아서 같은 방식이 통하지 않는다. .env 포맷이 단순한 KEY=VALUE
// 라인뿐이라 dotenv 패키지를 새로 추가하는 대신 여기서 직접 파싱해 env에 얹는다.
// (예: NIGHTSHIFT_API_KEY — 없으면 그냥 건너뛰고 기존처럼 인증 없이 뜬다.)
function loadDotEnv() {
  const envPath = path.join(__dirname, ".env");
  const result = {};
  if (!fs.existsSync(envPath)) return result;
  for (const line of fs.readFileSync(envPath, "utf8").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq === -1) continue;
    const key = trimmed.slice(0, eq).trim();
    let value = trimmed.slice(eq + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    result[key] = value;
  }
  return result;
}

// RunPod 등에서는 conda/venv가 ~/.bashrc 안에서만 활성화되는 경우가 흔한데, pm2가
// 자식 프로세스를 스폰할 때는 로그인 셸이 아니라서 그 활성화가 적용되지 않는다.
// 그러면 "python3"이 PATH에서 (uvicorn이 설치된 conda/venv 쪽이 아니라) 시스템
// /usr/bin/python3로 풀려서 "No module named uvicorn"이 나는 경우가 생긴다.
// `npm start`를 실행한 바로 그 셸에서 `which python3`가 가리키는 인터프리터를
// 미리 확인해두면, uvicorn이 실제로 설치돼 있는(=지금 `python3 app.py`가 정상
// 동작하는) 인터프리터와 항상 같은 것을 pm2가 쓰게 된다.
function resolvePython3() {
  try {
    const resolved = execSync("which python3", { encoding: "utf8" }).trim();
    if (resolved) return resolved;
  } catch (e) {
    // which가 없거나 python3을 못 찾으면 아래에서 그냥 "python3"으로 폴백한다.
  }
  return "python3";
}

// 서버 파이썬 모듈이 사는 곳. app.py/mcp_server.py 둘 다 여기서 cwd로 띄운다 —
// 형제 모듈 import(from data_paths import ...  등)가 상대 경로가 아니라 cwd
// 기준으로 풀리므로, 패키지 프리픽스 없이 그대로 두려면 여기서 작업 디렉터리를
// 맞춰줘야 한다.
const SERVER_DIR = path.join(__dirname, "server");

// app.py가 뜰 포트. .env의 NIGHTSHIFT_PORT를 그대로 쓴다 — 예전에는 여기 8000이
// 박혀 있고 .env에도 같은 값을 따로 적어야 했는데, 둘이 어긋나면 (a) 접속 포트와
// (b) 템플릿이 진행 상황을 보고하는 주소(app.py의 SELF_URL)가 서로 달라져서
// 진행률 바가 조용히 안 움직인다. 한 곳에서만 정하게 한다.
const dotEnv = loadDotEnv();
const nightshiftPort = (dotEnv.NIGHTSHIFT_PORT || "8000").trim() || "8000";

module.exports = {
  apps: [
    {
      name: "nightshift",
      script: resolvePython3(),
      args: `-m uvicorn app:app --host 0.0.0.0 --port ${nightshiftPort} --reload`,
      interpreter: "none",
      cwd: SERVER_DIR,
      env: {
        PYTHONUNBUFFERED: "1", // print() 출력이 버퍼링 없이 바로 pm2 로그에 찍히게 함
        ...dotEnv, // .env의 NIGHTSHIFT_API_KEY/NIGHTSHIFT_PORT 등을 app.py 프로세스로 전달
      },
      autorestart: true,
      max_restarts: 10,
      restart_delay: 2000,
      // 파일 변경 감지는 uvicorn --reload가 이미 하므로 pm2 자체의 watch는 끈다
      // (둘 다 켜두면 재시작이 중복으로 겹칠 수 있음).
      watch: false,
    },
    {
      // nightshift(app.py)의 REST API를 MCP 도구로 감싸는 별도 프로세스
      // (mcp_server.py) — app.py를 뜯어고치지 않고 그 위에 얹는 얇은 레이어라
      // 별도 포트로 따로 띄운다. app.py처럼 uvicorn --reload가 있는 구조가
      // 아니라서(fastmcp가 내부적으로 서버를 띄움), 코드 변경 감지는 여기서
      // pm2 자체의 watch로 대신한다.
      name: "nightshift-mcp",
      script: resolvePython3(),
      args: "mcp_server.py",
      interpreter: "none",
      cwd: SERVER_DIR,
      env: {
        PYTHONUNBUFFERED: "1",
        ...dotEnv, // .env의 JOB_QUEUE_BASE_URL/JOB_QUEUE_API_KEY/MCP_SERVER_PORT 전달
      },
      autorestart: true,
      max_restarts: 10,
      restart_delay: 2000,
      watch: ["mcp_server.py"],
    },
  ],
};
