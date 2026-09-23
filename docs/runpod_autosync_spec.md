# 요구사항 명세서 — RunPod 파드 자동 인식·등록 (runpod-autosync)

> 작성: 2026-09-23 · 대상: 이 저장소(nightshift)에서 작업할 Claude Code 세션
> 이 문서만 읽고 작업을 시작할 수 있도록 배경·현재 코드 상태·요구사항·완료 기준을 한 곳에 모았다.

---

## 0. 한 줄 요약

**사람이 로그인하거나 화면을 조작하지 않아도, Claude(MCP 클라이언트)가 "RunPod에서 지금 살아 있는 ComfyUI 파드"를 찾아 nightshift 파드 목록에 자동으로 등록할 수 있게 만든다.**

---

## 1. 배경 — 왜 필요한가

- 사용자는 RunPod에서 ComfyUI pod를 필요할 때만 켜고 끈다(시간당 과금). 켤 때마다 nightshift 화면에 로그인해서 파드 주소를 손으로 넣는 것이 번거롭다.
- `server/mcp_server.py`를 만든 목적은 **Claude가 로그인 절차 없이 nightshift를 조작하게 하는 것**이었다. 하지만 지금 MCP 도구는 잡 큐용뿐이라 파드를 다룰 수 없다.
- 2026-09-23 세션에서 Claude가 Runpod 커넥터로 pod를 켠 뒤 nightshift에 등록하려 했다. MCP에 파드 도구가 없고 MCP가 Claude에 연결돼 있지도 않아서, 브라우저로 웹 UI에 들어가려다 로그인 화면에 막혔다. → 이 문서가 그 공백을 메운다.

### 목표 흐름 (완성 후)

```
사용자: "파드 켜고 nightshift에 붙여줘"
Claude: Runpod 커넥터로 pod start
     → nightshift MCP `sync_runpod_pods` 호출
     → nightshift가 RunPod API로 RUNNING pod를 찾아 자동 등록
     → Claude: "추가됨: <이름> (https://<id>-8188.proxy.runpod.net)"
```

---

## 2. 현재 코드 상태 (작업 전 사실관계)

| 파일 | 현재 역할 | 이번 작업과의 관계 |
|---|---|---|
| `server/pod_registry.py` | `data/pods.json` 파드 레지스트리. `create_pod`/`update_pod`/`list_pods`/`default_pod`, 랜덤 이름 생성, `assert_public_url` | 자동 등록은 **여기 함수를 그대로 사용**한다(형식 검증 재사용) |
| `server/runpod_api.py` | `RUNPOD_API_KEY`로 **단일 pod 메타데이터만** 조회(`GET /v1/pods/{id}`). `extract_pod_id(url)`는 프록시 주소에서 pod id를 뽑음 | **목록 조회 함수 추가**가 필요 |
| `server/drivers/comfyui.py` | ComfyUI 드라이버. 브라우저 UA로 Cloudflare 403 우회. `check_url()`는 `/system_stats`의 **HTTP 200 여부만** 확인 | 헬스체크 보강 필요(§5 참고) |
| `server/app.py` | FastAPI. `POST /api/pods`(`create_pod_api`)는 로그인 필수. 등록 후 `ensure_runtime(pod)`로 큐/워커 스레드를 준비 | 동기화 엔드포인트 추가 |
| `server/mcp_server.py` | app.py REST를 감싼 MCP(도구 14개, 잡 큐 전용). `.env`의 `JOB_QUEUE_USER`/`JOB_QUEUE_PASSWORD`로 **스스로 로그인**. `0.0.0.0:8001`, streamable-http, **MCP 엔드포인트 자체 인증 없음** | 파드 도구 추가 + 인증 추가 |
| `ecosystem.config.js` | pm2: `nightshift`(uvicorn :8000), `nightshift-mcp`(:8001, `watch: ["mcp_server.py"]`), jupyter/opencut 게이트 | 필요 시 env 전달만 |
| `data/pods.json` | 지금은 "기본 파드"(`ded2824f`, url 빈 값, owner_id 1) 1개뿐 | 기본 파드는 **건드리지 않는다** |

**실행 환경:** Windows PC(`nucbox-m6ultra`), pm2로 구동. Cloudflare Tunnel을 이미 쓰고 있다(`jupyter.lomebrote.com` → jupyter-gate). `~/.cloudflared` 존재.

### 실측 정보 (2026-09-23)

- RunPod pod 예시: id `ffa84a6leuumux`, 이미지 `runpod/comfyui:1.4.7-cuda13.0`, 포트 `8080/http`(File Browser), `8188/http`(ComfyUI), `8888/http`(Jupyter), `22/tcp`. GPU RTX PRO 4500 Blackwell(VRAM 32GB). 상태값은 `RUNNING` / `EXITED`.
- ComfyUI 주소: `https://ffa84a6leuumux-8188.proxy.runpod.net`. `/system_stats`는 JSON으로 정상 응답했다(ComfyUI 0.30.0, `--enable-cors-header`).
- **pod가 RUNNING이 된 직후 약 1분 동안은 ComfyUI가 아직 부팅 중이다.** 이때 프록시는 "Waiting for service to respond — RunPod"라는 **HTML 페이지**를 돌려준다.
- `runpod_api.py` docstring 기록: REST `GET /v1/pods/{id}`의 `machine`은 빈 객체이고, GPU 모델명은 GraphQL로만 받을 수 있다.

---

## 3. 범위

### 포함
- F1. RunPod pod 목록 조회
- F2. 동기화 로직(멱등)
- F3. REST 엔드포인트
- F4. MCP 파드 도구
- F5. MCP 인증 + 외부 노출
- F6. ComfyUI 헬스체크 보강

### 선택 (시간이 되면)
- F7. 주기적 자동 동기화(백그라운드)

### 제외
- nightshift가 RunPod pod를 **생성·시작·중지·삭제**하는 기능. 이 일은 Claude가 Runpod 커넥터로 따로 한다. nightshift는 "살아 있는 pod를 인식·등록"만 한다.
- 웹 UI 개편(동기화 버튼 하나 정도는 허용).

---

## 4. 기능 요구사항

### F1. RunPod pod 목록 조회 — `server/runpod_api.py`

- `list_runpod_pods() -> list[dict] | None`를 추가한다.
  - `GET https://rest.runpod.io/v1/pods`를 쓰고, 헤더는 기존 `_fetch_pod_raw`와 같게 맞춘다(Bearer, 브라우저 UA).
  - 각 항목을 정규화한다: `{id, name, status, ports: [str], image, cost_per_hr}`.
    - `status`의 원본 필드는 `desiredStatus`인 것으로 추정한다. **실제 응답을 한 번 찍어서 필드명을 확인하고 코드 주석에 남길 것.**
  - 키가 없거나 실패하면 `None`을 돌려준다. 다만 F3/F4에서는 실패 원인이 보여야 하므로, 이유를 담는 변형(`list_runpod_pods_verbose()` 또는 `(data, error)` 튜플)도 함께 제공한다.
  - 캐시하지 않는다. 동기화는 드물고, 항상 최신이어야 한다.

### F2. 동기화 로직 — 새 모듈 `server/runpod_sync.py`

`sync_runpod_pods(owner_id: int, dry_run: bool = False) -> dict`

1. **대상 선정:** 다음 조건을 모두 만족하는 RunPod pod.
   - `status == "RUNNING"`
   - 포트에 `f"{COMFY_PORT}/http"`가 있음. `COMFY_PORT`는 환경변수 `RUNPOD_SYNC_COMFY_PORT`로 정하고 기본값은 `8188`.
2. **주소:** `https://{pod_id}-{COMFY_PORT}.proxy.runpod.net`
3. **기존 파드와 매칭:** nightshift 파드 중 `runpod_api.extract_pod_id(pod["url"]) == pod_id`인 것이 있는지 본다. **URL 문자열 비교가 아니라 pod id로 비교한다**(끝의 `/` 유무 같은 표기 차이를 흡수하기 위해).
4. **처리 규칙:**

| 상황 | 동작 |
|---|---|
| 매칭 없음 | `pod_registry.create_pod({kind:"comfyui", url, name: <RunPod pod 이름>, tags:["runpod","auto"], note:"runpod:<pod_id>", owner_id})` → `ensure_runtime(pod)` |
| 매칭 있음 + enabled | 아무것도 안 함(`unchanged`) |
| 매칭 있음 + disabled + `auto` 태그 | `enabled=True`로 되돌림(`reenabled`) |
| 매칭 있음 + disabled + `auto` 태그 없음 | 사람이 일부러 끈 것이므로 건드리지 않음(`skipped`, 사유 기록) |
| `auto` 태그 파드인데 해당 RunPod pod가 RUNNING이 아니거나 목록에 없음 | 그 파드에 **실행 중인 잡이 없을 때만** `enabled=False`(`disabled`). 잡이 돌고 있으면 `skipped`. **절대 삭제하지 않는다.** |
| url이 빈 기본 파드 | 건드리지 않음 |

5. **이름:** RunPod가 붙인 이름(예: `energetic_gray_turtle-migration`)을 그대로 쓴다. 비어 있으면 `create_pod`의 랜덤 이름에 맡긴다.
6. **멱등성:** 같은 상태에서 두 번 연속 호출하면 두 번째는 변경 0건이어야 한다.
7. **동시성:** `pod_registry`의 `_lock` 범위 안에서 "매칭 → 생성"이 원자적이어야 한다. 동시에 두 번 호출돼도 중복 생성이 없어야 한다.
8. **dry_run:** 아무것도 바꾸지 않고, 실행했다면 무엇을 할지만 보고한다.
9. **반환 형식:**

```json
{
  "dry_run": false,
  "runpod_ok": true,
  "error": null,
  "added":      [{"id": "...", "name": "...", "url": "...", "runpod_pod_id": "..."}],
  "reenabled":  [...],
  "disabled":   [...],
  "unchanged":  [...],
  "skipped":    [{"runpod_pod_id": "...", "reason": "..."}]
}
```

- `RUNPOD_API_KEY`가 없으면 `runpod_ok:false`, `error:"RUNPOD_API_KEY가 설정되지 않았어요."`를 돌려준다. 예외를 던지지 않는다.

### F3. REST 엔드포인트 — `server/app.py`

- `POST /api/pods/sync-runpod` — 본문 `{"dry_run": bool}`(선택). **관리자만** 쓸 수 있고, 아니면 403. `owner_id`는 호출한 관리자 id.
- 이 동작은 "사람 대신 도는 자동화"라 소유자를 관리자로 고정한다. 셸 파드와 같은 이유로, 일반 회원에게는 열지 않는다.
- 파드 목록 캐시(`invalidate_comfy_status_cache` 등)를 알맞게 무효화한다.
- (선택) 파드 화면에 "RunPod에서 불러오기" 버튼 1개.

### F4. MCP 파드 도구 — `server/mcp_server.py`

기존 패턴을 그대로 따른다: `_client()`, `_error_from_response`, 예외 대신 `{"error": true, ...}` 반환.

| 도구 | 호출 | 설명(docstring에 쓸 내용) |
|---|---|---|
| `list_pods()` | `GET /api/pods` | nightshift에 등록된 파드 목록(id, name, url, enabled, tags, effective_url) |
| `sync_runpod_pods(dry_run: bool = False)` | `POST /api/pods/sync-runpod` | RunPod에서 RUNNING인 ComfyUI pod를 자동 등록/정리. **RunPod에서 pod를 켠 직후 이 도구를 부르면 된다**고 명시 |
| `add_pod(url: str, name: str = "", pull_outputs: bool = False)` | `POST /api/pods` | 수동 등록(RunPod가 아닌 주소 포함) |
| `set_pod_enabled(pod_id: str, enabled: bool)` | `PUT /api/pods/{id}` | 파드 켜기/끄기(nightshift 쪽 사용 여부만. RunPod 과금과는 무관하다고 명시) |
| `pod_health(pod_id: str)` | `POST /api/pods/{id}/test` | 연결 확인 |

- 도구 docstring은 Claude가 읽는 설명서다. **"nightshift 파드 = 주소 레코드이고, RunPod pod의 전원과는 별개"**라는 점을 분명히 적는다.
- `docs/mcp_test_plan.md`의 도구 개수(14개)와 표를 갱신한다.

### F5. MCP 인증 + 외부 노출

**문제:** MCP 서버가 관리자 계정으로 app.py에 로그인한 상태로 `0.0.0.0:8001`에 인증 없이 떠 있다. 터널로 공개하는 순간 주소를 아는 누구나 관리자 권한으로 nightshift를 조작할 수 있다.

요구사항:
1. **바인딩:** 기본 host를 `127.0.0.1`로 바꾼다(환경변수 `MCP_SERVER_HOST`로 덮어쓸 수 있게). 외부 접근은 터널로만 들어오게 한다. `jupyterlab`을 127.0.0.1에 묶은 것과 같은 이유다(`ecosystem.config.js` 주석 참고).
2. **토큰:** 환경변수 `MCP_AUTH_TOKEN`을 추가한다. 설정돼 있으면 다음 둘 중 하나를 만족하는 요청만 통과시킨다.
   - `Authorization: Bearer <token>` 헤더
   - URL 경로에 토큰 포함(`/mcp/<token>` 형태). claude.ai 커스텀 커넥터가 임의 헤더를 넣을 수 없는 경우를 위한 것이다.
   - **작업 전에 claude.ai 커스텀 커넥터가 현재 지원하는 인증 방식(OAuth / 헤더 / 없음)을 공식 문서에서 확인하고, 둘 중 맞는 방식을 기본으로 삼을 것.** 문서 URL을 코드 주석에 남긴다.
   - 비교는 `secrets.compare_digest`로 한다.
   - 토큰이 설정되지 않은 채 host가 `127.0.0.1`이 아니면, 시작할 때 경고 로그를 남긴다.
3. **`.env.example`에 추가:** `MCP_AUTH_TOKEN`, `MCP_SERVER_HOST`, `RUNPOD_SYNC_COMFY_PORT`(, F7의 `RUNPOD_SYNC_INTERVAL_SEC`). 기존 파일의 설명 스타일을 따른다(어느 파일이 읽는지, 비우면 어떻게 되는지).
4. **터널:** 사용자의 기존 Cloudflare Tunnel 설정에 MCP용 ingress(예: `mcp.lomebrote.com → http://127.0.0.1:8001`)를 추가하는 방법을 README에 적는다. 터널 설정 파일 자체를 고치는 것은 **사용자 확인 후에만** 한다(파일 위치가 저장소 밖일 수 있음).
5. **연결 안내:** claude.ai → 설정 → 커넥터 → 커스텀 커넥터 추가에 넣을 URL 형식을 README에 적는다.

### F6. ComfyUI 헬스체크 보강 — `server/drivers/comfyui.py`

- 지금 `check_url()`은 `/system_stats`가 **HTTP 200이기만 하면** 살아 있다고 본다. 부팅 중인 RunPod pod의 "Waiting for service to respond" HTML이 200으로 오면 **거짓 양성**이 된다. **실제 상태 코드를 확인할 것.**
- 수정: 응답 본문을 JSON으로 파싱하고 `devices` 또는 `system` 키가 있을 때만 `True`로 판단한다.
- 동기화로 방금 등록된 파드는 1분쯤 "응답 없음"으로 보이는 게 정상이다. 등록 자체는 막지 않는다.

### F7. (선택) 주기적 자동 동기화

- `RUNPOD_SYNC_INTERVAL_SEC`(기본 `0` = 끔). 0보다 크면 app.py 시작 시 백그라운드 스레드가 그 간격으로 `sync_runpod_pods(owner_id=auth.admin_id())`를 실행한다.
- RunPod API 오류는 로그만 남기고 루프를 계속 돈다.
- 켜두면 Claude 없이도 "RunPod에서 켜면 → 몇 분 안에 nightshift에 나타남"이 된다.

---

## 5. 비기능 요구사항

- **기존 동작 무변경:** 수동 파드, url이 빈 기본 파드, 셸/Claude 글쓰기 파드는 동기화 대상이 아니며 영향이 없어야 한다.
- **실패 격리:** RunPod API 장애가 대시보드·큐·기존 파드 동작을 깨면 안 된다(`runpod_api.py`의 기존 철학 유지).
- **로그:** 동기화 한 번마다 한 줄 요약(added/reenabled/disabled/skipped 개수)을 남긴다.
- **비밀값:** `RUNPOD_API_KEY`, `MCP_AUTH_TOKEN`, `JOB_QUEUE_PASSWORD`를 로그·응답·에러 메시지에 절대 싣지 않는다. 참고로 기존 `_fetch_gpu_display_name`은 API 키를 쿼리스트링에 싣는다. 에러 메시지에 URL이 섞여 나가지 않는지 확인한다.
- **스타일:** 기존 코드처럼 한국어 docstring·주석을 쓰고, "왜"를 설명한다. 사용자에게 보이는 에러 문구는 "~해요" 체로 쓴다.

---

## 6. 완료 기준 (Acceptance Criteria)

| # | 시나리오 | 기대 결과 |
|---|---|---|
| AC-1 | RunPod pod 1개 RUNNING(8188 노출), nightshift엔 기본 파드뿐. `sync_runpod_pods()` | `added` 1건. `pods.json`에 url `https://<id>-8188.proxy.runpod.net`, tags `["runpod","auto"]`인 파드가 생김. 기본 파드는 그대로 |
| AC-2 | AC-1 직후 다시 호출 | 변경 0건(`unchanged` 1건) |
| AC-3 | 같은 호출을 동시에 2번 | 파드 중복 없음 |
| AC-4 | RunPod pod를 stop(EXITED) 후 호출, 실행 중 잡 없음 | 해당 auto 파드 `enabled=false`(`disabled`), 삭제 안 됨 |
| AC-5 | 다시 start 후 호출 | `reenabled` 1건, 같은 파드 id 유지(새로 안 만듦) |
| AC-6 | 사람이 화면에서 끈 수동 파드(auto 태그 없음)가 RunPod RUNNING pod를 가리킴 | `skipped`, 그대로 꺼져 있음 |
| AC-7 | `dry_run=true` | `pods.json` 수정 시각 불변, 보고 내용은 AC-1과 동일 |
| AC-8 | `RUNPOD_API_KEY` 미설정 | 예외 없이 `runpod_ok:false` + 안내 문구 |
| AC-9 | 일반 회원 세션으로 `POST /api/pods/sync-runpod` | 403 |
| AC-10 | `MCP_AUTH_TOKEN` 설정 후 토큰 없이 MCP 호출 | 401, 도구 실행 안 됨 |
| AC-11 | 부팅 중 pod(Waiting 페이지)에 `pod_health` | `ok:false` (거짓 양성 없음) |
| AC-12 | claude.ai 커스텀 커넥터로 연결 후 "파드 동기화해줘" | Claude가 `sync_runpod_pods`를 호출하고 결과를 답함(사람 로그인 없음) |

## 7. 테스트

- 단위 테스트: RunPod 응답을 목(mock)으로 바꿔 AC-1~AC-9를 확인한다. `NIGHTSHIFT_PODS_FILE`을 임시 파일로 지정해 **실제 `data/pods.json`은 건드리지 않는다**.
- `server/mcp_smoke_test.py`에 `list_pods`, `sync_runpod_pods(dry_run=True)` 케이스를 추가한다.
- 실측: 실제 RunPod pod로 AC-1, AC-4, AC-5, AC-11을 한 번씩 확인한다. **RunPod pod를 켜고 끄는 건 과금이 걸린 일이라 사용자에게 먼저 물어볼 것.**

## 8. 작업 순서 제안

1. F1 → 실제 `GET /v1/pods` 응답을 찍어 필드명 확정
2. F6 (작고 독립적, 기존 버그 성격)
3. F2 + 단위 테스트
4. F3
5. F4 + 스모크 테스트
6. F5 (인증 → 바인딩 → `.env.example` → README)
7. (선택) F7
8. `pm2 restart nightshift nightshift-mcp` 후 실측, `docs/mcp_test_plan.md` 갱신

## 9. 사용자가 직접 해야 하는 일 (Claude Code가 대신할 수 없거나, 확인이 필요한 것)

- `.env`에 `RUNPOD_API_KEY`(RunPod 콘솔 → Settings → API Keys), `JOB_QUEUE_USER`/`JOB_QUEUE_PASSWORD`(관리자), `MCP_AUTH_TOKEN` 넣기
- Cloudflare Tunnel에 MCP 호스트 추가 승인
- claude.ai에서 커스텀 커넥터 등록
