"""
RunPod Job Queue — 등록된 스크립트 템플릿을 큐에 쌓아두면 워커가 순서대로 하나씩 실행한다.

실행:
    pip install -r requirements.txt
    python3 app.py
    (RunPod라면 8188 등 이미 쓰는 포트와 겹치지 않게 8000번을 열어둠)

    pm2로 백그라운드 실행 + 코드 변경 자동 반영 + 깔끔한 로그를 원하면
    `npm install && npm start`를 대신 쓴다 (README "실행 방법" 참고,
    설정은 ecosystem.config.js).

접속:
    브라우저에서 http://<pod-ip>:8000  (RunPod는 포트 8000을 프록시로 노출해야 함)

코드 위치:
    본문은 app_parts/NN-이름.py에 기능별로 나뉘어 있다(파일 지도는 CLAUDE.md).
    아래에서 번호 순서대로 이 모듈의 네임스페이스에서 실행하므로, 한 파일이던 때와
    동작이 같다 — 라우트 등록 순서, 전역 상태(jobs·lock 등)를 모든 파일이 그대로 공유한다.
    그래서 파일끼리는 import 없이 앞 파일의 이름을 쓴다. 오류 추적(traceback)에는
    실제 파일 이름과 줄 번호가 나온다.
"""

from pathlib import Path as _Path

for _part in sorted((_Path(__file__).parent / "app_parts").glob("*.py")):
    exec(compile(_part.read_text("utf-8"), str(_part), "exec"), globals())
del _part


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
