# nightshift

미리 등록된 스크립트 템플릿을 선택하고, 그 템플릿이 필요로 하는 옵션 값/워크플로우 JSON/CSV만
채워서 큐에 쌓아두면 워커가 순서대로 하나씩 실행해주는 잡 큐 서버입니다.
GPU 인스턴스(RunPod 등)에서 반복되는 실행 로직(ComfyUI 배치 등)을 템플릿으로 등록해두고,
웹 UI에서 템플릿 선택 + 옵션 입력 + 파일 첨부만으로 작업을 큐에 넣고 진행 상황과 로그를 확인하고 싶을 때 쓰도록 만들어졌습니다.

## 주요 기능

- `templates/manifest.json`에 등록된 스크립트 템플릿을 웹 UI 드롭다운에서 선택해 큐에 등록
  (매번 `.py`를 업로드할 필요 없이, 정해진 실행 로직을 재사용)
- 템플릿마다 정의된 옵션(숫자/텍스트)을 선택 즉시 입력 폼으로 보여주고, 기본값을 미리 채워줌
- 템플릿마다 워크플로우(`.json`, 항상 필수)와 CSV(템플릿이 요구하는 경우에만 필수)를 첨부
- 업로드된 순서대로 워커 스레드가 하나씩 `python3`로 실행 (동시 실행 없음)
- 작업 상태 추적: `queued` → `running` → `done` / `failed` (서버 재시작 시 `interrupted`)
- 작업별 실행 로그(stdout/stderr)를 실시간에 가깝게 조회 (2초 폴링)
- 완료/실패한 작업 삭제
- 서버가 재시작되어도 `jobs_state.json`에 저장된 이력은 유지됨 (단, 큐에 남아있던 대기 작업은 재실행되지 않음)

## 기술 스택

- Python 3 / [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/)
- 순수 HTML/CSS/JS로 작성된 프론트엔드 (`static/index.html`, 별도 빌드 과정 없음), 라이트 테마의 모니터링 대시보드 스타일
- 백그라운드 스레드 + `queue.Queue` 기반의 단일 워커 실행 모델

## 요구 사항

- Python 3.9 이상 권장 (`dict[str, dict]` 등 최신 타입 힌트 문법 사용)

## 설치

```bash
git clone https://github.com/odineyes2/nightshift.git
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
2. "새 작업 추가"에서 **템플릿**을 드롭다운으로 선택합니다. 스크립트는 템플릿에 이미 정해져 있으므로 `.py`를 올릴 필요가 없습니다.
3. 템플릿을 선택하면 그 템플릿에 정의된 **옵션 입력 필드**가 바로 아래 나타나며, 기본값이 미리 채워집니다. 필요하면 값을 바꿉니다.
4. **워크플로우(.json)**는 어떤 템플릿이든 항상 필수로 첨부해야 합니다.
5. **CSV**는 선택한 템플릿의 `requires_csv`가 `true`일 때만 첨부 슬롯이 나타나며, 그 경우 필수입니다.
6. 각 슬롯은 클릭하거나 파일을 끌어다 놓아 선택할 수 있습니다. 모두 채운 뒤 "큐에 추가"를 누르면 작업이 등록됩니다.
7. "작업 목록" 테이블에서 각 작업의 템플릿 이름, 실행에 사용된 옵션 값(예: `seed_count=20`), 워크플로우 파일명, 상태(대기중/실행중/완료/실패/중단됨), 대기 등록/시작/종료 시각을 확인합니다.
8. 목록의 행을 클릭하면 해당 작업의 실행 로그(stdout/stderr)를 펼쳐볼 수 있습니다.
9. 실행 중이 아닌 작업은 "삭제" 버튼으로 이력에서 제거할 수 있습니다.

화면은 2초마다 자동으로 갱신되며, 로그를 펼쳐둔 상태에서도 2초마다 갱신됩니다.

### 스크립트 템플릿 등록하기 (`templates/`)

반복해서 쓰는 실행 로직은 `templates/` 아래에 스크립트 파일로 두고 `templates/manifest.json`에 등록하면
웹 UI 드롭다운에 자동으로 노출됩니다. `manifest.json`은 템플릿 객체의 배열이며, 각 항목은 다음 필드를 가집니다.

| 필드 | 설명 |
|---|---|
| `id` | 템플릿 고유 id (예: `"csv_batch"`) |
| `label` | 드롭다운에 표시될 사람이 읽는 이름 |
| `script_filename` | `templates/` 안에 있는 실제 실행 파일명 |
| `requires_csv` | `true`면 이 템플릿 선택 시 CSV 첨부가 필수, `false`면 CSV 슬롯이 아예 나타나지 않음 |
| `options` | 이 템플릿이 받는 옵션 목록 (아래 참고). 없으면 `[]` 또는 생략 |

`options`의 각 항목은 다음 필드를 가집니다.

| 필드 | 설명 |
|---|---|
| `name` | 옵션 이름. 폼 필드명이자, 대문자로 변환되어 스크립트에 환경변수로 전달됨 (예: `seed_count` → `SEED_COUNT`) |
| `label` | 입력 필드 위에 표시될 사람이 읽는 이름 |
| `type` | `"number"` 또는 `"text"` (현재 지원하는 타입은 이 두 가지) |
| `default` | 입력 필드에 미리 채워지는 기본값. 폼에서 값이 비어 있으면 서버가 이 기본값으로 대체함 |

현재 등록된 템플릿:

- **`seed_batch`** (`templates/seed_batch.py`) — 워크플로우 하나를 `seed_count`번만큼 시드만 바꿔가며 반복 실행하는 가장 단순한 형태입니다.
- **`csv_batch`** (`templates/csv_batch.py`) — CSV의 (제목, 프롬프트) 행마다 `seeds_per_case`개의 시드로 반복 제출하는 예시 구현입니다 (CSV 행에 `seed` 컬럼이 있으면 그 값 하나만 사용).

두 템플릿 모두 ComfyUI workflow API를 호출하는 best-effort 구현입니다. 실제 ComfyUI 워크플로우의 노드 제목/구조에 맞춰 `PROMPT_NODE_TITLE`/`SEED_NODE_TITLE`/`SAVE_NODE_TITLE` 등 환경변수나 노드 매칭 로직을 조정해야 할 수 있습니다. 자세한 사용법은 각 스크립트 상단 docstring을 참고하세요.

새 템플릿을 추가하려면 `templates/`에 스크립트를 넣고 `manifest.json`에 항목을 추가하면 됩니다(서버 재시작 불필요 — `/api/templates`가 매 요청마다 파일을 다시 읽습니다).

### 워크플로우 JSON / CSV / 옵션과 스크립트 연동

작업이 실행될 때, 서버는 다음을 환경변수로 담아 스크립트 프로세스에 전달합니다.

- `WORKFLOW_PATH` — 첨부된 워크플로우(`.json`)의 **절대 경로** (항상 전달됨)
- `CSV_PATH` — 첨부된 CSV의 **절대 경로** (`requires_csv`가 `true`인 템플릿에서만 전달됨)
- 템플릿의 `options`에 정의된 각 옵션 — `name`을 대문자로 바꾼 이름의 환경변수로 전달 (예: `seed_count` 옵션에 `20`을 입력하면 `SEED_COUNT=20`)

```python
import os, json

workflow_path = os.environ.get("WORKFLOW_PATH")
if workflow_path:
    with open(workflow_path) as f:
        workflow = json.load(f)

csv_path = os.environ.get("CSV_PATH")     # requires_csv 템플릿에서만 존재
seed_count = int(os.environ.get("SEED_COUNT", "10"))
```

## API

| Method | Path | 설명 |
|---|---|---|
| `GET` | `/api/templates` | `templates/manifest.json`의 내용을 그대로 반환 |
| `POST` | `/api/upload` | 작업을 큐에 등록 (multipart form). 필드: `template_id`(필수 — 등록된 템플릿 id), `workflow`(필수, `.json`), `csv`(선택한 템플릿의 `requires_csv`가 `true`일 때만 필수, `.csv`), 그리고 템플릿의 `options`마다 하나씩 `name=값` 필드 (예: `seed_count=20`; 비어 있거나 생략하면 해당 옵션의 `default`가 사용됨) |
| `GET` | `/api/jobs` | 전체 작업 목록과 대기 중인 작업 수 조회 |
| `GET` | `/api/jobs/{job_id}/log?tail=200` | 특정 작업의 로그 조회 (기본 마지막 200줄) |
| `DELETE` | `/api/jobs/{job_id}` | 완료/실패/중단된 작업 삭제 (실행 중인 작업은 삭제 불가) |

## 동작 방식 / 디렉터리 구조

```
nightshift/
├── app.py                    # FastAPI 서버 + 큐 워커
├── requirements.txt
├── static/
│   └── index.html            # 프론트엔드 (단일 HTML 파일)
├── templates/
│   ├── manifest.json         # 등록된 스크립트 템플릿 목록 (옵션 스키마 포함)
│   ├── seed_batch.py         # 템플릿 스크립트
│   └── csv_batch.py          # 템플릿 스크립트 (필요에 따라 계속 추가)
├── jobs/                      # 업로드된 워크플로우/CSV가 저장되는 곳 (자동 생성)
├── logs/                      # 작업별 실행 로그 (자동 생성)
└── jobs_state.json            # 작업 이력 저장 파일 (자동 생성)
```

- 모든 작업은 `templates/{script_filename}`을 직접 실행하고, 업로드된 워크플로우/CSV만 `jobs/{job_id}_{원본파일명}` 형태로 저장됩니다.
- 각 작업의 로그는 `logs/{job_id}.log`에 저장됩니다.
- 워커는 단일 스레드로 동작하므로 스크립트는 항상 큐에 들어온 순서대로 **하나씩** 실행됩니다(동시 실행 없음).
- 서버가 재시작되면 이전에 `queued`/`running` 상태였던 작업은 `interrupted`로 표시되며, 자동으로 재실행되지 않습니다.

## 주의사항

- 템플릿 스크립트는 서버 프로세스 권한으로 그대로 실행됩니다. `templates/`에는 신뢰할 수 있는 스크립트만 등록하세요.
- 현재 인증/권한 제어가 없으므로, 외부에 노출할 경우 접근을 제한하는 별도 조치(리버스 프록시 인증, 방화벽 등)가 필요합니다.
