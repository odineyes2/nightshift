# nightshift — Claude 작업 규칙

이 파일은 세션마다 자동으로 읽힌다. 짧게 유지할 것.

## 토큰 절약 (가장 중요)

- **큰 파일은 통째로 읽지 않는다.** `grep -n`으로 위치를 찾고 필요한 줄만 `Read`(offset/limit)로 읽는다.
  - `static/index.html` 약 13,000줄 / 670KB — 화면 전체(CSS·마크업·JS)가 한 파일이다.
  - `server/app.py` 약 5,600줄 — FastAPI 라우트·미들웨어·스케줄러.
  - `README.md` 약 1,300줄 — 사용자 문서. 고칠 때도 해당 절만 찾아서 고친다.
- **파일을 통째로 다시 쓰지 않는다.** 항상 부분 수정(Edit)만 한다. 특히 `index.html`.
- 같은 파일을 반복해서 다시 읽지 않는다. 이미 읽은 범위는 기억해서 쓴다.
- 탐색은 좁게 시작한다. "어디서 X를 하나"는 `grep -n "키워드"` 한 번으로 먼저 찾는다.

## index.html 길찾기

- 1~2670행: `<style>` CSS (`:root` 색 토큰, 다크 모드는 `html[data-theme="dark"]`)
- 2700행대: SVG 아이콘 스프라이트(`<symbol id="i-이름">`, 사용은 `<svg class="ico"><use href="#i-이름"/></svg>`)
- 2735~3940행: `<body>` 마크업(탭 패널, 모달)
- 3940행~끝: 메인 `<script>`
- 자주 찾는 곳: 파드 카드 `renderPodCard`, 파드 스코프 바 `renderPodBar`/`updatePodBarStatus`, 탭 전환 `showTab`, 작업 목록 `fetchJobs`
- 줄 번호는 바뀌니 위 이름으로 `grep -n` 해서 찾는다.

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
