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
module.exports = {
  apps: [
    {
      name: "nightshift",
      script: "python3",
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
