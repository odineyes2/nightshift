# nightshift

`.py` 스크립트를 업로드하면 큐에 쌓아두고, 워커가 순서대로 하나씩 실행해주는 간단한 잡 큐 서버입니다.
GPU 인스턴스(RunPod 등)에서 여러 개의 학습/추론 스크립트를 순차 실행하고, 웹 UI로 진행 상황과 로그를 확인하고 싶을 때 쓰도록 만들어졌습니다.

## 주요 기능

- 브라우저에서 `.py` 파일을 드래그 앤 드롭(또는 클릭 업로드)하면 큐에 등록
- 업로드된 순서대로 워커 스레드가 하나씩 `python3`로 실행 (동시 실행 없음)
- 작업 상태 추적: `queued` → `running` → `done` / `failed` (서버 재시작 시 `interrupted`)
- 작업별 실행 로그(stdout/stderr)를 실시간에 가깝게 조회 (2초 폴링)
- 완료/실패한 작업 삭제
- 서버가 재시작되어도 `jobs_state.json`에 저장된 이력은 유지됨 (단, 큐에 남아있던 대기 작업은 재실행되지 않음)

## 기술 스택

- Python 3 / [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/)
- 순수 HTML/CSS/JS로 작성된 프론트엔드 (`static/index.html`, 별도 빌드 과정 없음)
- 백그라운드 스레드 + `queue.Queue` 기반의 단일 워커 실행 모델

## 요구 사항

- Python 3.9 이상 권장 (`dict[str, dict]` 등 최신 타입 힌트 문법 사용)

## 설치

```bash
git clone <this-repo-url>
cd nightshift
pip install -r requirements.txt
```

`requirements.txt`에 포함된 패키지:

- `fastapi`
- `uvicorn[standard]`
- `python-multipart` (파일 업로드 처리에 필요)

## 실행 방법

```bash
python3 app.py
```

기본적으로 `0.0.0.0:8000`에서 서버가 뜹니다. (RunPod 등에서 ComfyUI가 흔히 8188 포트를 쓰기 때문에 겹치지 않도록 8000번을 사용합니다.)

- 로컬에서 실행 중이라면: `http://localhost:8000`
- RunPod 등 원격 인스턴스라면: 해당 플랫폼에서 8000번 포트를 프록시로 노출한 뒤, 제공되는 URL로 접속

개발 중 자동 리로드가 필요하다면 uvicorn을 직접 실행할 수도 있습니다.

```bash
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

## 사용 방법

1. 브라우저에서 서버 주소(`http://<host>:8000`)에 접속합니다.
2. 화면 상단의 드롭존에 `.py` 파일을 끌어다 놓거나 클릭해서 파일을 선택합니다. 여러 개를 한 번에 올리면 올린 순서대로 큐에 등록되어 실행됩니다.
3. "작업 목록" 테이블에서 각 작업의 상태(대기중/실행중/완료/실패/중단됨), 대기 등록 시각, 시작/종료 시각을 확인합니다.
4. 목록의 행을 클릭하면 해당 작업의 실행 로그(stdout/stderr)를 펼쳐볼 수 있습니다.
5. 실행 중이 아닌 작업은 "삭제" 버튼으로 이력에서 제거할 수 있습니다.

화면은 2초마다 자동으로 갱신되며, 로그를 펼쳐둔 상태에서도 2초마다 갱신됩니다.

## API

| Method | Path | 설명 |
|---|---|---|
| `POST` | `/api/upload` | `.py` 파일을 업로드하여 큐에 등록 (multipart form, 필드명 `file`) |
| `GET` | `/api/jobs` | 전체 작업 목록과 대기 중인 작업 수 조회 |
| `GET` | `/api/jobs/{job_id}/log?tail=200` | 특정 작업의 로그 조회 (기본 마지막 200줄) |
| `DELETE` | `/api/jobs/{job_id}` | 완료/실패/중단된 작업 삭제 (실행 중인 작업은 삭제 불가) |

## 동작 방식 / 디렉터리 구조

```
nightshift/
├── app.py             # FastAPI 서버 + 큐 워커
├── requirements.txt
├── static/
│   └── index.html     # 프론트엔드 (단일 HTML 파일)
├── jobs/               # 업로드된 스크립트가 저장되는 곳 (자동 생성)
├── logs/               # 작업별 실행 로그 (자동 생성)
└── jobs_state.json     # 작업 이력 저장 파일 (자동 생성)
```

- 업로드된 파일은 `jobs/{job_id}_{원본파일명}` 형태로 저장됩니다.
- 각 작업의 로그는 `logs/{job_id}.log`에 저장됩니다.
- 워커는 단일 스레드로 동작하므로 스크립트는 항상 큐에 들어온 순서대로 **하나씩** 실행됩니다(동시 실행 없음).
- 서버가 재시작되면 이전에 `queued`/`running` 상태였던 작업은 `interrupted`로 표시되며, 자동으로 재실행되지 않습니다.

## 주의사항

- 업로드된 `.py` 파일은 서버 프로세스 권한으로 그대로 실행됩니다. 신뢰할 수 없는 코드를 업로드하지 않도록 주의하세요.
- 현재 인증/권한 제어가 없으므로, 외부에 노출할 경우 접근을 제한하는 별도 조치(리버스 프록시 인증, 방화벽 등)가 필요합니다.
