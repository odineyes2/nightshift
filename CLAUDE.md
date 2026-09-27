# nightshift — Claude 작업 규칙

이 파일은 세션마다 자동으로 읽힌다. 짧게 유지할 것.

## 토큰 절약 (가장 중요)

- **큰 파일은 통째로 읽지 않는다.** `grep -n`으로 위치를 찾고 필요한 줄만 `Read`(offset/limit)로 읽는다.
  - 화면은 `static/index.html`(마크업) + `static/css/*` + `static/js/*`로 나뉘어 있다. 아래 "화면 파일 지도"로 바로 해당 파일을 연다.
  - `server/app.py` 약 6,400줄 — FastAPI 라우트·미들웨어·스케줄러.
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

## 서버 구조 (server/)

- `app.py` 본체 · `auth.py` 회원/세션 · `pod_registry.py` 파드 목록(`data/pods.json`) · `drivers/` 파드 종류별 드라이버(comfyui/shell/claude_writer)
- `runpod_api.py` RunPod REST 호출 · `runpod_sync.py` RunPod 파드 자동 등록
- `mcp_server.py` app.py API를 감싼 MCP 서버(claude.ai 커넥터용). app.py에는 `NIGHTSHIFT_MCP_KEY`로 인증
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

JS 문법만 확인할 때는 인라인 `<script>`를 뽑아 `node --check`로 검사한다(브라우저를 띄우는 것보다 훨씬 싸다).

## 스타일

- 코드 주석과 문서는 한국어. 주변 코드의 주석 밀도·말투(“~한다”)를 따른다.
- 화면 문구는 존댓말(“~해요”).
