# MCP 서버(mcp_server.py) 테스트 시방서

`mcp_server.py`(app.py의 REST API를 감싼 MCP 도구 14개)가 **실제 ComfyUI + GPU가
붙은 환경**(RunPod 파드)에서 정상 동작하는지 확인하기 위한 절차입니다.

이 문서에 있는 절차 중 TC-02~TC-04, TC-11, TC-12는 GPU 없는 환경에서도 이미
검증됐습니다(격리된 사본 + fastmcp in-process 클라이언트, 실제 `jobs_state.json`은
건드리지 않음). **TC-05~TC-10(실제 이미지 생성)과 TC-16~TC-18(claude.ai 커넥터
등록)은 GPU/ComfyUI/인터넷 노출이 필요해 이 저장소를 만든 세션에서는 검증하지
못했습니다** — 이 문서는 그 나머지를 실제 파드에서 확인하기 위한 것입니다.

## 0. 사전 준비

1. 파드에서 최신 코드 받기: `git pull` (`mcp_server.py`, `mcp_smoke_test.py`가 있는지 확인)
2. 의존성 설치: `pip install -r requirements.txt` (`fastmcp`, `httpx`가 새로 추가됨)
3. `.env` 확인/설정 (`.env.example` 참고):
   - `NIGHTSHIFT_API_KEY`를 쓰고 있다면 `JOB_QUEUE_API_KEY`에 **같은 값**을 넣을 것
   - `JOB_QUEUE_BASE_URL`은 같은 머신이면 비워둬도 됨(기본 `http://127.0.0.1:8000`)
   - `MCP_SERVER_PORT`는 비워두면 8001
4. `npm run restart` (이미 pm2로 떠 있었다면) 또는 `npm start` (처음이면) — `ecosystem.config.js`에
   `nightshift-mcp` 앱이 추가돼 있으므로 `pm2 status`에 `nightshift`와
   `nightshift-mcp` **둘 다** `online`으로 떠 있어야 합니다.
5. ComfyUI가 실제로 켜져 있고 체크포인트가 최소 1개는 설치돼 있는지 확인 (웹 UI 헤더의
   연결 상태 표시로 확인 가능).

## 1. 자동화 스모크 테스트 (TC-02, 03, 04, 11, 12 + 가능하면 05~10)

`mcp_smoke_test.py`가 아래 표의 케이스 대부분을 자동으로 실행하고 PASS/FAIL을
출력합니다. 파드에서 실행:

```bash
cd /workspace/nightshift
python3 mcp_smoke_test.py
```

- ComfyUI가 연결돼 있고 체크포인트가 설치돼 있으면 TC-05~10(실제 생성)까지 자동으로
  이어서 실행되고, 마지막에 생성된 이미지 하나를 실제로 받아와 봅니다(수십 초~수 분
  소요 — 워크플로우/GPU 성능에 따라 다름).
- 연결/에러 처리만 빠르게 확인하고 싶으면 `python3 mcp_smoke_test.py --skip-generation`.
- `mcp_server.py`를 실제로 띄운 상태에서 HTTP 경로(streamable-http, claude.ai
  커넥터와 같은 경로)까지 확인하려면: `python3 mcp_smoke_test.py --mcp-url http://127.0.0.1:8001/mcp`
- 체크포인트/프롬프트/해상도를 바꾸고 싶으면 `--checkpoint`, `--positive`, `--negative`,
  `--width`, `--height`, `--steps`, `--job-timeout`(기본 180초) 참고 (`--help`로 전체 옵션 확인).

마지막에 `N/M 통과` 요약과 함께, 실패가 있으면 종료 코드 1로 끝납니다.

## 2. 테스트 케이스 상세

| ID | 목적 | 절차 | 기대 결과 | 자동화 |
|---|---|---|---|---|
| TC-01 | MCP 서버 기동 | `pm2 status`로 `nightshift-mcp` 확인 | `online` 상태, 재시작 반복 없음 | 수동 |
| TC-02 | ComfyUI 연결 상태 | `comfy_status` 호출 | `{"connected": true, "url": "..."}` | ✅ 스크립트 |
| TC-03 | 템플릿 목록 | `list_templates` 호출 | 웹 UI "템플릿" 드롭다운과 **개수·옵션 스키마가 정확히 일치** | ✅ 스크립트(개수만) — 옵션 내용은 아래 2.1 수동 확인 |
| TC-04 | 모델 목록 | `list_models` 호출 | 실제 설치된 체크포인트/LoRA 이름이 비어 있지 않음 | ✅ 스크립트 |
| TC-05 | 워크플로우 빌드 | `build_workflow`(checkpoint=설치된 것, positive="a cat...") | `{"workflow": {...}}` 반환, 노드 7개 안팎 | ✅ 스크립트 |
| TC-06 | 잡 제출 | `submit_job`(template_id="seed_batch", 위 workflow, options={"seed_count":1}) | `status: "pending"`인 잡 객체 반환 | ✅ 스크립트 |
| TC-07 | 큐 시작 | `start_queue` | `{"running": true, "started": 1}` | ✅ 스크립트 |
| TC-08 | 완료까지 대기 | `wait_for_job`(job_id, timeout_seconds=180) | 최종 `status: "done"` (실패하면 로그로 원인 확인) | ✅ 스크립트 |
| TC-09 | 결과 이미지 목록 | `list_output_images(job_id=...)` | 이미지 1장 이상, `job_id` 필터가 정확히 걸러줌 | ✅ 스크립트 |
| TC-10 | 이미지 콘텐츠 | `get_output_image(name=...)` | `ImageContent`(mime_type이 `image/png` 등), 실제로 열어보면 방금 만든 이미지와 같음 | ✅ 스크립트(형식만) — 실물 확인은 2.2 참고 |
| TC-11a | 존재하지 않는 템플릿 | `submit_job(template_id="__no_such__", ...)` | 예외 대신 `{"error": true, "status_code": 400, ...}` | ✅ 스크립트 |
| TC-11b | csv 누락 | `requires_csv=true` 템플릿에 `csv` 없이 `submit_job` | `{"error": true, "detail": "이 템플릿은 csv가 필요합니다"}` | ✅ 스크립트 |
| TC-12 | 존재하지 않는 잡 | `get_job(job_id="__no_such__")` | `{"error": true, "status_code": 404, ...}` | ✅ 스크립트 |
| TC-13 | 타임아웃 처리 | `wait_for_job(job_id=방금 만든 잡, timeout_seconds=1)`을 잡이 끝나기 전에 호출 | 예외 없이 `{"error": true, "detail": "...초 안에 끝나지 않았어요...", "last_job": {...}}` 반환 | 수동 (아래 2.3) |
| TC-14 | 대용량 이미지 자동 축소 | 5MB 넘는 결과 이미지에 `get_output_image(thumbnail=false)` | 자동으로 축소본(`image/jpeg`)으로 대체돼 반환 (원본이 5MB 안 넘으면 스킵) | 수동, 조건부 |
| TC-15 | 큐 폭주 가드 | (리뷰로 이미 확인됨 — 실제로 100개씩 채울 필요 없음) `MAX_ACTIVE_JOBS`(기본 100) 이상 쌓이면 `submit_job`이 429 에러 반환 | 코드 레벨에서 이미 검증(별도 PR) | 생략 권장 |
| TC-16 | claude.ai 커넥터 등록 | claude.ai 설정 → 커넥터 추가 → URL에 `mcp_server.py`의 공개 주소(`https://<pod>-<MCP_SERVER_PORT>.proxy.runpod.net/mcp`) 입력 | 커넥터가 "연결됨" 상태로 뜸 | 수동 (아래 2.4) |
| TC-17 | 도구 노출 확인 | 커넥터 상세에서 도구 목록 확인 | 14개 도구 이름이 전부 보임 | 수동 |
| TC-18 | 실제 채팅에서 호출 | claude.ai 채팅에서 "list_templates 도구로 지금 쓸 수 있는 템플릿 보여줘" 요청 | Claude가 실제로 도구를 호출해 템플릿 목록을 답함 | 수동 |

### 2.1 TC-03 옵션 스키마 수동 대조

`list_templates` 결과의 각 템플릿 `options` 배열이 웹 UI의 "새 작업 추가" 폼에서
그 템플릿을 골랐을 때 보이는 입력 필드(이름/타입/선택지/기본값)와 정확히 같은지
눈으로 한 번 대조하세요. 특히 `pose_batch`/`depth_batch`/`lineart_batch`의
`secondary_kind`/`secondary_char_no`/`secondary_set`처럼 다른 옵션 값에 따라
선택지가 바뀌는 필드가 있는지 확인해두면, 나중에 LLM이 `submit_job`을 호출할 때
그 관계를 설명해줘야 합니다.

### 2.2 TC-10 실물 확인

스크립트는 `get_output_image`가 유효한 이미지 콘텐츠 블록을 반환하는지까지만
확인합니다. 실제로 그 이미지가 맞는 그림인지는:

```bash
python3 -c "
import asyncio, base64
from fastmcp import Client
import mcp_server

async def main():
    async with Client(mcp_server.mcp) as client:
        images = await client.call_tool('list_output_images', {})
        name = images.structured_content['images'][0]['name']
        img = await client.call_tool('get_output_image', {'name': name})
        data = base64.b64decode(img.content[0].data)
        open('/tmp/mcp_test_output.png', 'wb').write(data)
        print('저장됨: /tmp/mcp_test_output.png (', len(data), 'bytes )')

asyncio.run(main())
"
```
로 파일로 저장한 뒤 직접 열어서 확인하세요.

### 2.3 TC-13 타임아웃 수동 확인

```bash
python3 -c "
import asyncio
from fastmcp import Client
import mcp_server

async def main():
    async with Client(mcp_server.mcp) as client:
        # seed_count를 크게 줘서 오래 걸리게 만든 잡을 하나 제출/시작해둔 뒤
        job_id = '<방금 만든 job_id>'
        result = await client.call_tool('wait_for_job', {'job_id': job_id, 'timeout_seconds': 1, 'poll_interval_seconds': 1})
        print(result.structured_content)

asyncio.run(main())
"
```
`error: true`와 `last_job`이 함께 온전히 반환되고(예외로 죽지 않고), 잡 자체는
서버에서 계속 실행 중이어야 합니다(`wait_for_job`은 대기만 취소할 뿐 잡을 멈추지
않음).

### 2.4 TC-16 RunPod 포트 노출 주의사항

RunPod은 보통 파드를 만들 때 지정한 포트만 `https://<pod-id>-<port>.proxy.runpod.net`
형태로 공개 노출합니다. 지금까지 8000(app.py)만 노출돼 있었다면, **MCP_SERVER_PORT
(기본 8001)도 별도로 노출 설정을 추가해야** claude.ai가 그 URL에 닿을 수 있습니다
— RunPod 콘솔에서 해당 파드의 "Expose HTTP Ports"에 8001을 추가하세요. (같은
파드 안에서 app.py가 `mcp_server.py`를 호출하는 방향은 내부 통신이라 이 노출과
무관합니다 — `JOB_QUEUE_BASE_URL`이 내부 주소를 가리키므로 그대로 둬도 됩니다.)

## 3. 테스트 후 정리

테스트로 실제 잡/이미지가 생겼다면:
- `clear_completed_jobs` 도구(또는 웹 UI의 "완료 삭제")로 테스트 잡 정리
- 생성된 테스트 이미지가 불필요하면 웹 UI 갤러리에서 삭제

## 4. 결과 기록

| 항목 | 결과 (PASS/FAIL) | 비고 |
|---|---|---|
| TC-01 |  |  |
| TC-02 |  |  |
| TC-03 |  |  |
| TC-04 |  |  |
| TC-05 |  |  |
| TC-06 |  |  |
| TC-07 |  |  |
| TC-08 |  |  |
| TC-09 |  |  |
| TC-10 |  |  |
| TC-11a/b |  |  |
| TC-12 |  |  |
| TC-13 |  |  |
| TC-14 |  |  |
| TC-16 |  |  |
| TC-17 |  |  |
| TC-18 |  |  |
