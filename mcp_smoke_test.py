#!/usr/bin/env python3
"""
mcp_server.py가 실제 nightshift(app.py) + ComfyUI에 대고 정상 동작하는지
확인하는 자동화 스모크 테스트. mcp_test_plan.md의 TC-02~TC-12를 그대로
코드로 옮긴 것 — 자세한 절차/기대 결과 설명은 그 문서를 같이 보세요.

사용법:
    # mcp_server.py를 따로 안 띄우고, 이 스크립트가 도구 함수를 직접 호출
    # (app.py에만 붙으면 됨 — 기본은 http://127.0.0.1:8000, 필요하면
    # JOB_QUEUE_BASE_URL/JOB_QUEUE_API_KEY 환경변수로 바꾸세요)
    python3 mcp_smoke_test.py

    # mcp_server.py를 실제로 streamable-http로 띄워둔 상태에서, 그 URL로
    # 접속해(= claude.ai 커넥터와 똑같은 경로) 검증하고 싶으면:
    python3 mcp_smoke_test.py --mcp-url http://127.0.0.1:8001/mcp

옵션:
    --mcp-url URL       위 설명대로. 생략하면 mcp_server.py를 in-process로 임포트.
    --skip-generation   실제 이미지 생성(build_workflow→submit_job→...→
                         get_output_image)은 건너뛰고 연결/에러 처리 테스트만 실행
                         (ComfyUI가 꺼져 있거나 GPU 시간을 아끼고 싶을 때)
    --job-timeout N     wait_for_job에 줄 timeout_seconds (기본 180)
    --checkpoint NAME   테스트에 쓸 체크포인트 파일명 (생략하면 list_models가
                        돌려주는 첫 번째 설치 체크포인트를 자동으로 씀)
"""
import argparse
import asyncio
import os
import sys

from fastmcp import Client

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def unwrap(structured):
    # 반환 타입이 dict 하나가 아닌 도구(list_templates 등)는 fastmcp가
    # {"result": ...}로 한 번 더 감싸서 내려준다 — 그 경우만 풀어준다.
    if isinstance(structured, dict) and list(structured.keys()) == ["result"]:
        return structured["result"]
    return structured


async def call(client: Client, name: str, **kwargs):
    result = await client.call_tool(name, kwargs)
    return unwrap(result.structured_content)


async def run(client: Client, args) -> None:
    status = await call(client, "comfy_status")
    record("TC-02 comfy_status 응답 형태", isinstance(status, dict) and "connected" in status, str(status))
    comfy_connected = bool(status.get("connected")) if isinstance(status, dict) else False
    if not comfy_connected:
        print("!! ComfyUI가 연결돼 있지 않습니다 — 실제 생성 플로우(TC-05~10)는 실패할 수밖에 없습니다.")

    templates = await call(client, "list_templates")
    templates_ok = isinstance(templates, list) and len(templates) > 0
    record("TC-03 list_templates", templates_ok, f"{len(templates)}개 템플릿" if templates_ok else str(templates))
    template_ids = {t["id"] for t in templates} if templates_ok else set()

    models = await call(client, "list_models")
    checkpoints = (models.get("models") or {}).get("checkpoints") or [] if isinstance(models, dict) else []
    record("TC-04 list_models", isinstance(models, dict) and "models" in models, f"체크포인트 {len(checkpoints)}개")

    bad = await call(client, "submit_job", template_id="__no_such_template__", workflow={"a": 1}, options={})
    record("TC-11a 존재하지 않는 template_id 에러", isinstance(bad, dict) and bad.get("error") is True, str(bad))

    csv_template = next((t for t in templates if t.get("requires_csv")), None) if templates_ok else None
    if csv_template:
        bad_csv = await call(
            client, "submit_job", template_id=csv_template["id"], workflow={"a": 1}, options={}
        )
        record("TC-11b csv 누락 에러", isinstance(bad_csv, dict) and bad_csv.get("error") is True, str(bad_csv))
    else:
        print("(csv가 필요한 템플릿을 찾지 못해 TC-11b는 건너뜀)")

    missing_job = await call(client, "get_job", job_id="__does_not_exist__")
    record(
        "TC-12 존재하지 않는 job_id 에러",
        isinstance(missing_job, dict) and missing_job.get("error") is True,
        str(missing_job),
    )

    if args.skip_generation:
        print("\n--skip-generation 지정됨 — 실제 이미지 생성 플로우(TC-05~10)는 건너뜁니다.")
        return
    if not comfy_connected:
        record("TC-05~10 생성 플로우", False, "ComfyUI 연결 안 됨 — 건너뜀")
        return
    checkpoint = args.checkpoint or (checkpoints[0] if checkpoints else None)
    if not checkpoint:
        record("TC-05~10 생성 플로우", False, "설치된 체크포인트를 찾지 못함(--checkpoint로 직접 지정 가능) — 건너뜀")
        return
    if "seed_batch" not in template_ids:
        record("TC-05~10 생성 플로우", False, "seed_batch 템플릿을 찾지 못함 — 건너뜀")
        return

    wf_result = await call(
        client,
        "build_workflow",
        checkpoint=checkpoint,
        positive=args.positive,
        negative=args.negative,
        width=args.width,
        height=args.height,
        steps=args.steps,
    )
    wf_ok = isinstance(wf_result, dict) and "workflow" in wf_result
    record(
        "TC-05 build_workflow",
        wf_ok,
        f"노드 {len(wf_result['workflow'])}개" if wf_ok else str(wf_result)[:200],
    )
    if not wf_ok:
        return
    workflow = wf_result["workflow"]

    job = await call(
        client,
        "submit_job",
        template_id="seed_batch",
        workflow=workflow,
        options={"seed_count": 1, "seed_mode": "random"},
    )
    job_ok = isinstance(job, dict) and job.get("status") == "pending"
    record("TC-06 submit_job", job_ok, str(job)[:200])
    if not job_ok:
        return
    job_id = job["id"]

    started = await call(client, "start_queue")
    record("TC-07 start_queue", isinstance(started, dict) and started.get("running") is True, str(started))

    final_job = await call(
        client, "wait_for_job", job_id=job_id, poll_interval_seconds=3, timeout_seconds=args.job_timeout
    )
    done_ok = isinstance(final_job, dict) and final_job.get("status") == "done"
    record(
        "TC-08 wait_for_job (done까지)",
        done_ok,
        f"status={final_job.get('status') if isinstance(final_job, dict) else final_job}",
    )
    if not done_ok:
        print(f"   -> 로그 확인: GET /api/jobs/{job_id}/log 를 직접 열어보세요.")
        return

    images = await call(client, "list_output_images", job_id=job_id)
    image_list = images.get("images") if isinstance(images, dict) else None
    images_ok = bool(image_list)
    record("TC-09 list_output_images(job_id=...)", images_ok, f"{len(image_list) if image_list else 0}장")
    if not images_ok:
        return

    name = image_list[0]["name"]
    img_result = await client.call_tool("get_output_image", {"name": name})
    content = img_result.content[0] if img_result.content else None
    image_ok = content is not None and getattr(content, "type", None) == "image"
    record("TC-10 get_output_image", image_ok, f"name={name}, mime={getattr(content, 'mime_type', None)}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mcp-url", help="mcp_server.py가 streamable-http로 떠 있는 URL (예: http://127.0.0.1:8001/mcp)")
    parser.add_argument("--skip-generation", action="store_true")
    parser.add_argument("--job-timeout", type=float, default=180)
    parser.add_argument("--checkpoint", default=None)
    parser.add_argument("--positive", default="a cat sitting on a windowsill, masterpiece, best quality")
    parser.add_argument("--negative", default="lowres, bad anatomy, worst quality")
    parser.add_argument("--width", type=int, default=832)
    parser.add_argument("--height", type=int, default=1216)
    parser.add_argument("--steps", type=int, default=20)
    args = parser.parse_args()

    if args.mcp_url:
        client_cm = Client(args.mcp_url)
    else:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import mcp_server

        client_cm = Client(mcp_server.mcp)

    async with client_cm as client:
        await run(client, args)

    print("\n=== 요약 ===")
    for name, ok, _ in results:
        print(f"{'✓' if ok else '✗'} {name}")
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} 통과")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
