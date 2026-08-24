# nightshift

미리 등록된 스크립트 템플릿(또는 일회성 커스텀 `.py`)에 워크플로우 JSON을 붙여 큐에 쌓아두면,
워커가 순서대로 하나씩 실행해주는 간단한 잡 큐 서버입니다.
GPU 인스턴스(RunPod 등)에서 반복되는 실행 로직(ComfyUI 배치 등)을 템플릿으로 등록해두고,
웹 UI에서 템플릿 선택 + 파일 첨부만으로 작업을 큐에 넣고 진행 상황과 로그를 확인하고 싶을 때 쓰도록 만들어졌습니다.

## 주요 기능

- `templates/manifest.json`에 등록된 스크립트 템플릿을 웹 UI 드롭다운에서 선택해 큐에 등록
  (매번 `.py`를 업로드할 필요 없이, 정해진 실행 로직을 재사용)
- 템플릿마다 워크플로우(`.json`, 항상 필수)와 CSV(템플릿이 요구하는 경우에만 필수)를 첨부
- 일회성 스크립트를 실험하고 싶을 때는 "커스텀 py 업로드"를 선택해 기존처럼 `.py`를 직접 첨부 가능
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
2. "새 작업 추가"에서 **템플릿**을 드롭다운으로 선택합니다.
   - 템플릿을 선택하면 스크립트는 이미 정해져 있으므로 별도로 `.py`를 올릴 필요가 없습니다.
   - 드롭다운 맨 아래의 **"커스텀 py 업로드"**를 선택하면 일회성 스크립트를 실험할 수 있는 `.py` 첨부 슬롯이 나타납니다.
3. **워크플로우(.json)**는 어떤 경우든 항상 필수로 첨부해야 합니다.
4. **CSV**는 선택한 템플릿의 `requires_csv`가 `true`일 때만 첨부 슬롯이 나타나며, 그 경우 필수입니다. (커스텀 py 업로드에는 CSV 슬롯이 없습니다.)
5. 각 슬롯은 클릭하거나 파일을 끌어다 놓아 선택할 수 있습니다. 모두 채운 뒤 "큐에 추가"를 누르면 작업이 등록됩니다.
6. "작업 목록" 테이블에서 각 작업의 스크립트 파일명, 템플릿 이름(커스텀이면 "커스텀"), 워크플로우 파일명, 상태(대기중/실행중/완료/실패/중단됨), 대기 등록/시작/종료 시각을 확인합니다.
7. 목록의 행을 클릭하면 해당 작업의 실행 로그(stdout/stderr)를 펼쳐볼 수 있습니다.
8. 실행 중이 아닌 작업은 "삭제" 버튼으로 이력에서 제거할 수 있습니다.

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

현재 등록된 템플릿:

- **`csv_batch`** (`templates/csv_batch.py`) — CSV의 (제목, 프롬프트) x 시드 조합을 ComfyUI workflow API에 반복 제출하는 예시 구현입니다. 실제 ComfyUI 워크플로우의 노드 제목/구조에 맞춰 `PROMPT_NODE_TITLE`/`SEED_NODE_TITLE`/`SAVE_NODE_TITLE` 등 환경변수나 노드 매칭 로직을 조정해야 할 수 있습니다. 자세한 사용법은 스크립트 상단 docstring을 참고하세요.

새 템플릿을 추가하려면 `templates/`에 스크립트를 넣고 `manifest.json`에 항목을 추가하면 됩니다(서버 재시작 불필요 — `/api/templates`가 매 요청마다 파일을 다시 읽습니다).

### 워크플로우 JSON / CSV와 스크립트 연동

작업이 실행될 때, 서버는 첨부된 파일의 **절대 경로**를 환경변수로 담아 스크립트 프로세스에 전달합니다.

- `WORKFLOW_PATH` — 첨부된 워크플로우(`.json`)의 절대 경로 (항상 전달됨)
- `CSV_PATH` — 첨부된 CSV의 절대 경로 (CSV가 첨부된 작업에서만 전달됨)

```python
import os, json

workflow_path = os.environ.get("WORKFLOW_PATH")
if workflow_path:
    with open(workflow_path) as f:
        workflow = json.load(f)

csv_path = os.environ.get("CSV_PATH")  # requires_csv 템플릿에서만 존재
```

## API

| Method | Path | 설명 |
|---|---|---|
| `GET` | `/api/templates` | `templates/manifest.json`의 내용을 그대로 반환 |
| `POST` | `/api/upload` | 작업을 큐에 등록 (multipart form). 필드: `template_id`(필수 — 템플릿 id 또는 커스텀 업로드를 뜻하는 `__custom__`), `workflow`(필수, `.json`), `file`(`template_id=__custom__`일 때만 필수, `.py`), `csv`(선택한 템플릿의 `requires_csv`가 `true`일 때만 필수, `.csv`) |
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
│   ├── manifest.json         # 등록된 스크립트 템플릿 목록
│   └── csv_batch.py          # 템플릿 스크립트 (필요에 따라 계속 추가)
├── jobs/                      # 업로드된 워크플로우/CSV/커스텀 스크립트가 저장되는 곳 (자동 생성)
├── logs/                      # 작업별 실행 로그 (자동 생성)
└── jobs_state.json            # 작업 이력 저장 파일 (자동 생성)
```

- 템플릿 기반 작업은 `templates/{script_filename}`을 직접 실행하고, 업로드된 워크플로우/CSV만 `jobs/{job_id}_{원본파일명}` 형태로 저장됩니다.
- 커스텀 py 업로드 작업은 스크립트/워크플로우/CSV 모두 `jobs/{job_id}_{원본파일명}` 형태로 저장됩니다.
- 각 작업의 로그는 `logs/{job_id}.log`에 저장됩니다.
- 워커는 단일 스레드로 동작하므로 스크립트는 항상 큐에 들어온 순서대로 **하나씩** 실행됩니다(동시 실행 없음).
- 서버가 재시작되면 이전에 `queued`/`running` 상태였던 작업은 `interrupted`로 표시되며, 자동으로 재실행되지 않습니다.

## 주의사항

- 템플릿 스크립트든 커스텀 업로드 `.py`든 모두 서버 프로세스 권한으로 그대로 실행됩니다. `templates/`에는 신뢰할 수 있는 스크립트만 등록하고, 커스텀 업로드로 신뢰할 수 없는 코드를 실행하지 않도록 주의하세요.
- 현재 인증/권한 제어가 없으므로, 외부에 노출할 경우 접근을 제한하는 별도 조치(리버스 프록시 인증, 방화벽 등)가 필요합니다.
