# nightshift — Claude 작업 규칙

이 파일은 세션마다 자동으로 읽힌다. 짧게 유지할 것.

## 토큰 절약 (가장 중요)

- **큰 파일은 통째로 읽지 않는다.** `grep -n`으로 위치를 찾고 필요한 줄만 `Read`(offset/limit)로 읽는다.
  - 화면은 `static/index.html`(마크업) + `static/css/*` + `static/js/*`로 나뉘어 있다. 아래 "화면 파일 지도"로 바로 해당 파일을 연다.
  - 서버 본체는 `server/app_parts/*.py`로 나뉘어 있다. 아래 "서버 파일 지도"로 바로 해당 파일을 연다.
  - `README.md` 약 1,300줄 — 사용자 문서. 고칠 때도 해당 절만 찾아서 고친다.
- **파일을 통째로 다시 쓰지 않는다.** 항상 부분 수정(Edit)만 한다.
- 같은 파일을 반복해서 다시 읽지 않는다. 이미 읽은 범위는 기억해서 쓴다.
- 탐색은 좁게 시작한다. "어디서 X를 하나"는 `grep -n "키워드"` 한 번으로 먼저 찾는다.

## 화면 파일 지도 (static/)

- `index.html` (~1,400줄): 마크업만. `<head>`의 작은 인라인 스크립트 2개, SVG 아이콘 스프라이트(`<symbol id="i-이름">`, 사용은 `<svg class="ico"><use href="#i-이름"/></svg>`), 탭 패널·모달.
- JS/CSS는 기능별 파일로 나뉘어 있고, **서버가 번호 순서대로 이어 붙여 `/js/bundle.js`, `/css/bundle.css` 한 덩어리로 보낸다**(`app.py`의 `_bundle`). 그래서 파일끼리 전역 변수·함수를 자유롭게 공유한다. 빌드 과정 없음.
  - 새 파일을 추가하면 번호로 순서를 정한다. 파일 이름을 바꿔도 index.html은 고칠 필요 없다.
- `js/` (~500~1,100줄씩)
  - `01-core-jobs` 공용 유틸(`escapeHtml`, `fmt*`)·파드 목록 `fetchPods`·작업 목록 `fetchJobs`·작업 카드·작업 상세 `openJobDetailModal`
  - `02-job-move-newjob-form` 작업 파드 이동·로그, 새 작업 폼(템플릿 옵션 `populateSelectOptions`, 에셋 `fetchAssets`)
  - `03-newjob-refs-inputs` 참조 슬롯·입력 이미지/영상/오디오 컨트롤
  - `04-newjob-tools` 최근 워크플로우 선택·CSV 편집기·ComfyUI 상태
  - `05-models` 모델 탭(등록부·설치 현황·LoRA 트리거 `rebuildLoraTriggersFromRegistry`)
  - `06-wizard` 마법사(계열·유형·LoRA·배치 모달, 워크플로우 검사)
  - `07-account-db` 테마·NSFW·로그인·회원 관리·DB 탭
  - `08-pods` 파드 카드 `renderPodCard`·RunPod 켜기/끄기 `runpodPower`·파드 편집 `openPodEditModal`
  - `09-routing-results` 탭 전환 `showTab`·해시 라우팅 `applyHashRoute`·파드 스코프 바 `renderPodBar`/`updatePodBarStatus`·결과 목록
  - `10-gallery-meta` 갤러리 필터·즐겨찾기/NSFW/별점/태그 일괄 변경
  - `11-projects` 프로젝트·홈 대시보드 `renderHome`·프로젝트 바
  - `12-board` 프로젝트 보드(캔버스·선택·작업 카드) / `13-board-gen` 보드 생성 카드
  - `14-projects-form` 프로젝트 생성/이동 모달
  - `15-gallery` 갤러리 툴바·삭제/다운로드/회전·라이트박스 `openLightbox`
  - `16-video` 영상 갤러리·영상 편집(`ve*`)·공유/OpenCut
  - `17-danbooru` Danbooru 탭 / `18-init` Danbooru 모달 나머지 + 페이지 초기화·폴링 시작(반드시 마지막)
- `css/`: `01-base`(`:root` 색 토큰, 다크 모드 `html[data-theme="dark"]`, 공통 레이아웃) · `02-meta` · `03-projects-components`(프로젝트·공용 컴포넌트) · `04-pods` · `05-jobs` · `06-models-db` · `07-video` · `08-danbooru` · `09-csv-editor`
- 함수가 어느 파일인지 모르면 `grep -n "function 이름" static/js/*.js` 한 번으로 찾는다.

## 서버 파일 지도 (server/)

- `app.py`는 로더뿐이다: `app_parts/NN-이름.py`를 번호 순서대로 **한 네임스페이스에서 exec**한다. 한 파일이던 때와 동작이 같다(라우트 등록 순서, `jobs`·`lock` 같은 전역을 모두 공유).
  - 그래서 파트 파일에는 import가 없어도 앞 파일의 이름을 그대로 쓴다. 새 import는 `01-config`에 추가한다.
  - 파트 파일을 따로 `import`하거나 실행하지 않는다. 새 파일은 번호로 순서를 정한다(라우트는 먼저 등록된 것이 이긴다 — 정적 파일 마운트는 반드시 마지막).
- `app_parts/` (~100~730줄씩)
  - `01-config` import·경로·환경변수 상수·최근 파일 저장소·Danbooru/LoRA 트리거 저장·`jobs`/`lock`
  - `02-pod-runtime-comfy` 상태 저장/복원 `save_state`·템플릿 목록·ComfyUI 연결 확인·`PodRuntime`·object_info/모델 목록 조회
  - `03-enhance` 프롬프트 개선(Text Enhance)·작업 정지·ComfyUI 대기
  - `04-scheduler-app` 대기 큐 스케줄러 `schedule_once`/`job_missing_on_pod`·`dispatch_job`·워커·RunPod 동기화·`lifespan`·`app = FastAPI(...)`
  - `05-auth` 인증 미들웨어 `authenticate_request`·MCP 키·`me`/`admin_only`·파드 접근 범위·`/api/auth/*`
  - `06-admin-comfy-status` 회원 관리 `/api/admin/*`·ComfyUI 상태/주소
  - `07-pods` 파드 API `/api/pods*`·파드 카드·RunPod 켜기/끄기·모델 사용 현황
  - `08-models-inputs` 모델 받기 `/api/models*`·입력 이미지/영상/오디오
  - `09-workflows` 모델 계열·워크플로우 유형/프리셋·`build-workflow`/`validate-workflow`·`enhance-prompt`·에셋
  - `10-job-submit` 참조 노드 검사·최근 워크플로우/CSV·작업 생성 `create_job`(`/api/upload`, `POST /api/jobs`)
  - `11-projects-board` 프로젝트·보드 프리셋·보드(카드·선·생성 카드 실행)
  - `12-output-assets` 작업의 프로젝트 이동·결과물 메타(`/api/output-assets*`)·태그
  - `13-jobs-api` 큐 시작/정지·작업 목록/로그/진행 보고·start/pause/fetch-missing/stop/move/삭제
  - `14-output-images` 메일 보내기·결과 이미지 목록/썸네일/회전/다운로드/삭제·ComfyUI 결과 동기화
  - `15-videos-share` 결과 영상·영상 편집·OpenCut 공유
  - `16-danbooru-static` Danbooru 태그/기록·`/js/bundle.js`·`/css/bundle.css`·정적 파일 마운트
- 함수가 어느 파일인지 모르면 `grep -n "def 이름\|\"/api/경로" server/app_parts/*.py` 한 번으로 찾는다.
- 그 밖의 모듈: `auth.py` 회원/세션 · `pod_registry.py` 파드 목록(`data/pods.json`) · `drivers/` 파드 종류별 드라이버(comfyui/shell/claude_writer) · `runpod_api.py` RunPod REST 호출 · `runpod_sync.py` RunPod 파드 자동 등록 · `mcp_server.py` app.py API를 감싼 MCP 서버(claude.ai 커넥터용, app.py에는 `NIGHTSHIFT_MCP_KEY`로 인증)
- `templates/` 작업 실행 스크립트 + `manifest.json`

## git 규칙

- 홈서버(Windows, `~/Projects/nightshift`)는 pm2로 운영 중이며, 커밋 안 된 로컬 작업이 있을 수 있다.
  **`git reset` / `checkout --` / `stash` / `rebase` / 강제 push로 남의 변경을 되돌리지 않는다.**
- pull 전에 `git status --short`를 확인하고, 변경이 있으면 먼저 커밋하거나 사용자에게 묻는다.
- GitHub push와 main 병합은 사용자에게 먼저 묻는다.

## 비밀값

- `.env` 내용을 출력하지 않는다(`grep -c`처럼 값이 안 보이는 방법만 쓴다).
- 비밀번호를 명령어 인자나 채팅으로 다루지 않는다. 사용자가 에디터로 직접 넣게 안내한다.
- admin 비밀번호는 JupyterLab(홈서버 셸) 게이트의 열쇠이기도 하다. `.env`에 넣지 않는다.

## 로컬 테스트

실제 데이터를 건드리지 않게 임시 폴더로 띄운다.

```
cd server
NIGHTSHIFT_DATA_DIR=/tmp/ns/data NIGHTSHIFT_OUTPUT_DIR=/tmp/ns/out NIGHTSHIFT_ASSETS_DIR=/tmp/ns/assets \
NIGHTSHIFT_ADMIN_USER=admin NIGHTSHIFT_ADMIN_PASSWORD='Test-Passw0rd-xyz!' \
python3 -m uvicorn app:app --port 8765
```

JS 문법만 확인할 때는 고친 파일을 `node --check static/js/파일.js`로 검사한다(브라우저를 띄우는 것보다 훨씬 싸다).

## 스타일

- 코드 주석과 문서는 한국어. 주변 코드의 주석 밀도·말투(“~한다”)를 따른다.
- 화면 문구는 존댓말(“~해요”).

## graphify (코드 지식 그래프)

- `graphify-out/graph.json`이 있으면 코드 질문은 `graphify query "<질문>"`부터 쓴다(관계는 `graphify path "A" "B"`, 개념은 `graphify explain "X"`). `graphify-out/GRAPH_REPORT.md`는 전체 구조를 볼 때만 읽는다.
- `app_parts/`·`static/js/`는 import 없이 이름을 공유해서 그래프에 파일 사이 연결이 빠질 수 있다 — 그래프에서 못 찾으면 위 파일 지도와 `grep`으로 확인한다.
- 코드를 고친 뒤 `graphify update .`(AST만, API 비용 없음)로 그래프를 갱신한다. `graphify-out/`은 생성물이라 커밋하지 않는다.
