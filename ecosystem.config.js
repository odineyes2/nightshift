// pm2 프로세스 정의 — nightshift 서버(app.py)를 pm2로 백그라운드 실행/관리한다.
// `npm start`(= `pm2 start ecosystem.config.js`)로 시작하면:
//   - 터미널을 계속 붙잡고 있지 않아도 서버가 백그라운드에서 계속 돈다
//   - `npm run status` / `pm2 status`로 살아있는지 한눈에 확인
//   - `npm run logs` / `pm2 logs nightshift`로 로그만 깔끔하게 tail
//   - app.py나 templates/, static/ 안의 파일을 고치면 uvicorn --reload가 감지해서
//     자동으로 다시 로드한다(코드 변경 시 서버를 직접 껐다 켤 필요 없음)
//
// 서버 프로세스 자체는 여전히 uvicorn이 띄운다 — pm2는 그 프로세스를 감독만 한다
// (죽으면 자동 재시작, 상태/로그 조회를 깔끔하게 제공).

const { execSync } = require("child_process");

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

module.exports = {
  apps: [
    {
      name: "nightshift",
      script: resolvePython3(),
      args: "-m uvicorn app:app --host 0.0.0.0 --port 8000 --reload",
      interpreter: "none",
      cwd: __dirname,
      env: {
        PYTHONUNBUFFERED: "1", // print() 출력이 버퍼링 없이 바로 pm2 로그에 찍히게 함
      },
      autorestart: true,
      max_restarts: 10,
      restart_delay: 2000,
      // 파일 변경 감지는 uvicorn --reload가 이미 하므로 pm2 자체의 watch는 끈다
      // (둘 다 켜두면 재시작이 중복으로 겹칠 수 있음).
      watch: false,
    },
  ],
};
