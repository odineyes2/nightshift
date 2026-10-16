# nightshift — 에이전트 작업 규칙 (Claude·Codex 공통)

이 파일은 세션마다 자동으로 읽힌다. 짧게 유지할 것.
모든 프로젝트 공통 규칙(사람의 결정, git, 비밀값, 말투·디자인)은 `~/Projects/dev/harness/COMMON.md` — dev가 에이전트 프롬프트와 MCP 안내문에 넣는다. 여기에는 nightshift에만 해당하는 것만 둔다.

## 토큰 절약 (가장 중요)

- **큰 파일은 통째로 읽지 않는다.** `grep -n`으로 위치를 찾고 필요한 줄만 `Read`(offset/limit)로 읽는다.
  - 화면은 `static/index.html`(마크업) + `static/css/*` + `static/js/*`, 서버 본체는 `server/app_parts/*.py`로 나뉘어 있다. 자동 파일 지도로 바로 해당 파일을 연다(아래 "핵심 모듈 동작").
  - `README.md` 약 1,300줄 — 사용자 문서. 고칠 때도 해당 절만 찾아서 고친다.
- **파일을 통째로 다시 쓰지 않는다.** 항상 부분 수정(Edit)만 한다.
- 같은 파일을 반복해서 다시 읽지 않는다. 이미 읽은 범위는 기억해서 쓴다.
- 탐색은 좁게 시작한다. "어디서 X를 하나"는 `grep -n "키워드"` 한 번으로 먼저 찾는다.

## 핵심 모듈 동작

파일마다 무엇이 있는지는 **자동 파일 지도**가 맡는다(각 파일 첫 줄 설명을 모은 것). dev 에이전트는 프롬프트로 받고, 대화형 세션은
`python ~/Projects/dev/server/filemap.py .`로 본다. 새 파일을 만들면 첫 줄에 그 파일의 역할을 적는다. 여기는 파일 하나만 봐서는 안 보이는 동작만 적는다.

### 화면 (static/)
- `index.html`은 마크업만: `<head>`의 작은 인라인 스크립트 2개, SVG 아이콘 스프라이트(`<symbol id="i-이름">`, 쓸 때 `<svg class="ico"><use href="#i-이름"/></svg>`), 탭 패널·모달.
- JS·CSS는 기능별 파일인데 **서버가 번호 순서대로 이어 붙여 `/js/bundle.js`, `/css/bundle.css` 한 덩어리로 보낸다**(`server/app_parts/16-danbooru-static.py`의 `_bundle`). 그래서 파일끼리 전역 변수·함수를 import 없이 공유하고, 빌드 과정이 없다.
  - 새 파일은 번호로 순서를 정한다(사이에 넣을 땐 `08b`·`17c`처럼). 파일 이름을 바꿔도 index.html은 고칠 필요 없다.
  - `js/18-init.js`는 페이지 초기화·폴링 시작이라 반드시 마지막이다.
  - 색은 `css/01-base.css`의 `:root` 토큰과 다크 모드 `html[data-theme="dark"]`만 쓴다.
- 함수가 어느 파일인지 모르면 `grep -n "function 이름" static/js/*.js` 한 번으로 찾는다.

### 서버 (server/)
- `app.py`는 로더뿐이다: `app_parts/NN-이름.py`를 번호 순서대로 **한 네임스페이스에서 exec**한다. 한 파일이던 때와 동작이 같다(라우트 등록 순서, `jobs`·`lock` 같은 전역을 모두 공유).
  - 그래서 파트 파일에는 import가 없어도 앞 파일의 이름을 그대로 쓴다. 새 import는 `01-config`에 추가한다.
  - 파트 파일을 따로 `import`하거나 실행하지 않는다. 라우트는 먼저 등록된 것이 이기니 정적 파일 마운트(`16-danbooru-static`)는 반드시 마지막. 새 파일은 번호로 순서를 정한다(`15b`~`15d`처럼 끼워 넣기).
- Library(Pose·Position·Lighting, `app_parts/15b`~`15d`)는 같은 게시물 구조이고 회원마다 자기 것만, admin은 전부 본다. 화면은 `js/17c-library.js` 하나가 셋을 같이 그린다.
- 함수·라우트가 어느 파일인지 모르면 `grep -n "def 이름\|\"/api/경로" server/app_parts/*.py` 한 번으로 찾는다.
- `mcp_server.py`는 app.py API를 감싼 MCP 서버(claude.ai 커넥터용)이고 app.py에는 `NIGHTSHIFT_MCP_KEY`로 인증한다. `drivers/`는 파드 종류별 드라이버(comfyui·shell·claude_writer), `templates/`는 작업 실행 스크립트와 `manifest.json`.

## git 규칙

- 홈서버(Windows, `~/Projects/nightshift`)는 pm2로 운영 중이며, 커밋 안 된 로컬 작업이 있을 수 있다.
- pull 전에 `git status --short`를 확인하고, 변경이 있으면 먼저 커밋하거나 사용자에게 묻는다.
- main 병합도 사용자에게 먼저 묻는다(dev 오케스트레이터의 자동 병합은 예외).

## 비밀값

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

새 API 라우트는 핸들러 함수를 직접 부르지 말고 `TestClient(app)`로 로그인·라우팅을 거치는 검사를 하나 둔다(예: `tests/test_lighting_library.py`의 `LightingHTTPTests`) — 함수 호출만으로는 라우트 등록·인증·회원별 범위가 안 잡힌다(NS-60).
서버(`server/`) 변경은 `--reload`가 없어 재시작해야 반영된다. dev 이슈로 병합되면 dev 오케스트레이터가 실행 중 작업이 없을 때 자동 재시작한다(`/api/jobs/busy`·`/api/health`).
