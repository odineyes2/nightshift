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
// (예: NIGHTSHIFT_ADMIN_PASSWORD — admin 계정을 처음 만들 때 필요하다.)
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
// `npm start`를 실행한 바로 그 셸에서 `which python3`(윈도우는 `where python`)가
// 가리키는 인터프리터를 미리 확인해두면, uvicorn이 실제로 설치돼 있는(=지금
// `python3 app.py`/`python app.py`가 정상 동작하는) 인터프리터와 항상 같은 것을
// pm2가 쓰게 된다. 윈도우엔 `python3` 실행 파일 자체가 보통 없고(App Execution
// Alias 스텁만 있어 "Python was not found..." 오류가 남) `which` 명령도 없으므로,
// 플랫폼별로 후보를 다르게 시도한다.
function resolvePython3() {
  try {
    const isWindows = process.platform === "win32";
    const cmd = isWindows ? "where.exe python" : "which python3";
    const resolved = execSync(cmd, { encoding: "utf8" }).split(/\r?\n/)[0].trim();
    if (resolved) return resolved;
  } catch (e) {
    // which/where가 없거나 python을 못 찾으면 아래에서 폴백한다.
  }
  return process.platform === "win32" ? "python" : "python3";
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
      args: `-m uvicorn app:app --host 0.0.0.0 --port ${nightshiftPort}`,
      interpreter: "none",
      cwd: SERVER_DIR,
      env: {
        PYTHONUNBUFFERED: "1", // print() 출력이 버퍼링 없이 바로 pm2 로그에 찍히게 함
        ...dotEnv, // .env의 NIGHTSHIFT_ADMIN_*/NIGHTSHIFT_PORT 등을 app.py 프로세스로 전달
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
        ...dotEnv, // .env의 JOB_QUEUE_BASE_URL/NIGHTSHIFT_MCP_KEY/MCP_SERVER_PORT 등 전달
      },
      autorestart: true,
      max_restarts: 10,
      restart_delay: 2000,
      watch: ["mcp_server.py"],
    },
    {
      // Cloudflare Tunnel(jupyter.lomebrote.com)이 예전엔 이 프로세스를 바로
      // 가리켜서, 로그인 화면 없이 노트북(임의 코드 실행)이 그대로 노출돼 있었다.
      // 이제 127.0.0.1에만 묶어서 아래 jupyter-gate를 거치지 않고는 이 머신 밖
      // 어디서도 닿을 수 없게 한다. JUPYTER_TOKEN은 게이트가 모든 프록시 요청에
      // 자동으로 실어 보내는 내부 전용 값이라, 사람이 이 토큰을 보거나 입력할
      // 일은 없다(잊어버려도 되는 값 — 실제 로그인은 nightshift 관리자 계정으로 한다).
      name: "jupyterlab",
      script: "jupyter",
      args: "lab --no-browser --ip=127.0.0.1 --port=18888",
      interpreter: "none",
      cwd: "C:\\Users\\Simon Lomebrote\\Projects",
      env: {
        PYTHONUNBUFFERED: "1",
        JUPYTER_TOKEN: dotEnv.JUPYTER_INTERNAL_TOKEN || "",
      },
      autorestart: true,
      max_restarts: 10,
      restart_delay: 2000,
      watch: false,
    },
    {
      // 위 jupyterlab 앞을 지키는 로그인 게이트 (server/jupyter_gate.py 참고).
      // Cloudflare Tunnel이 실제로 가리키는 포트(8888)를 이제 이 프로세스가 받는다.
      name: "jupyter-gate",
      script: resolvePython3(),
      args: "jupyter_gate.py",
      interpreter: "none",
      cwd: SERVER_DIR,
      env: {
        PYTHONUNBUFFERED: "1",
        JUPYTER_INTERNAL_TOKEN: dotEnv.JUPYTER_INTERNAL_TOKEN || "",
        JUPYTER_GATE_PORT: "8888",
        JUPYTER_UPSTREAM_PORT: "18888",
        NIGHTSHIFT_INTERNAL_URL: `http://127.0.0.1:${nightshiftPort}`,
      },
      autorestart: true,
      max_restarts: 10,
      restart_delay: 2000,
      watch: false,
    },
    {
      // OpenCut(classic 포크) — 별도 저장소(../opencut, docs/opencut/ 참고)를 빌드해 둔 것을 띄운다.
      // 내부 전용 포트(3101)의 127.0.0.1로만 묶는다 — 앞의 opencut-gate를 거치지 않고는 이 머신
      // 밖 어디서도 닿을 수 없다(로그인 없이 노출되지 않게).
      name: "opencut",
      script: "node_modules/next/dist/bin/next",
      args: "start -p 3101 -H 127.0.0.1",
      interpreter: "node",
      cwd: "C:/Users/Simon Lomebrote/Projects/opencut/apps/web",
      env: { NODE_ENV: "production" },
      autorestart: true,
      max_restarts: 10,
      restart_delay: 2000,
      watch: false,
    },
    {
      // 위 opencut 앞을 지키는 로그인 게이트(server/opencut_gate.py 참고) — nightshift 계정을
      // 그대로 쓴다(별도 회원 시스템 없음). Cloudflare Tunnel이 opencut.lomebrote.com을 이제
      // 이 포트(3100)로 연결한다(예전엔 opencut을 직접 가리켰다).
      name: "opencut-gate",
      script: resolvePython3(),
      args: "opencut_gate.py",
      interpreter: "none",
      cwd: SERVER_DIR,
      env: {
        PYTHONUNBUFFERED: "1",
        OPENCUT_GATE_PORT: "3100",
        OPENCUT_UPSTREAM_PORT: "3101",
        NIGHTSHIFT_INTERNAL_URL: `http://127.0.0.1:${nightshiftPort}`,
      },
      autorestart: true,
      max_restarts: 10,
      restart_delay: 2000,
      watch: false,
    },
  ],
};
