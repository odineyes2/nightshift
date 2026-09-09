"""
Claude 글쓰기 배치 — Claude API(claude_writer 드라이버)로 프롬프트 하나를 보내 글을
생성한다.

다른 템플릿들이 ComfyUI에 프롬프트를 밀어 넣는 것과 같은 자리에 있지만, 상대가
ComfyUI가 아니라 Claude API다. nightshift 입장에서는 "스크립트를 서브프로세스로
돌리고 진행 상황을 보고받고 산출물을 job_id 폴더에서 찾는다"는 점이 완전히 같아서,
큐/워커/대시보드가 그대로 동작한다.

무엇을 하나:
    PROMPT을 Claude API(공식 anthropic SDK)로 한 번 보내고, 응답 텍스트를
    NIGHTSHIFT_OUTPUT_DIR/<JOB_ID>/output.md로 저장한다. 결과는 작업 로그(표준
    출력)에도 그대로 찍히고, 파드 화면의 "📝 결과" 탭에서도 볼 수 있다
    (GET /api/jobs/{id}/text-result가 이 파일을 읽는다).

환경변수 (nightshift가 주입한다):
    PROMPT             모델에 보낼 프롬프트 (필수)
    MODEL              쓸 모델 (작업 화면의 "모델" 선택칸, manifest.json)
    MAX_TOKENS         응답 최대 토큰 수 (기본 16000)
    CLAUDE_MODEL       MODEL이 없을 때 쓰는 기본값 (드라이버가 주입, 기본 claude-opus-5)
    ANTHROPIC_API_KEY  Claude API 키 (드라이버가 주입)
    NIGHTSHIFT_OUTPUT_DIR  산출물이 쌓이는 폴더 (기본 /workspace/output)
    JOB_ID             nightshift가 주입하는 이 작업의 id (진행 상황 보고/출력 폴더용)
    NIGHTSHIFT_URL     nightshift 자신의 주소 (진행 상황 보고용, 기본 http://127.0.0.1:8000)
    NIGHTSHIFT_API_KEY 설정돼 있으면 진행 상황 보고에 X-API-Key로 실어 보냄 (선택)
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

import anthropic


def env(name, default=None):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def report_progress(job_id, nightshift_url, total, done):
    if not job_id or not nightshift_url:
        return
    try:
        payload = json.dumps({"total": total, "done": done}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        # shell_command.py와 같은 이유 — NIGHTSHIFT_API_KEY를 켜둔 배포에서도
        # 진행률 보고가 401로 조용히 실패하지 않게 같이 실어 보낸다.
        api_key = os.environ.get("NIGHTSHIFT_API_KEY", "").strip()
        if api_key:
            headers["X-API-Key"] = api_key
        req = urllib.request.Request(
            f"{nightshift_url}/api/jobs/{job_id}/progress",
            data=payload,
            headers=headers,
            method="PUT",
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"[claude_write] 경고: 진행 상황 보고 실패: {e}", file=sys.stderr)


def main():
    prompt = env("PROMPT")
    if not prompt:
        print("[claude_write] PROMPT 환경변수가 필요합니다.", file=sys.stderr)
        sys.exit(1)

    api_key = env("ANTHROPIC_API_KEY")
    if not api_key:
        print("[claude_write] ANTHROPIC_API_KEY가 설정돼 있지 않습니다.", file=sys.stderr)
        sys.exit(1)

    # MODEL은 작업 화면의 "모델" 선택칸(manifest.json)에서 오고, CLAUDE_MODEL은
    # 드라이버가 주는 기본값이다 — 둘 다 없을 때만 코드 기본값(claude-opus-5)을 쓴다.
    model = env("MODEL") or env("CLAUDE_MODEL", "claude-opus-5")
    try:
        max_tokens = int(env("MAX_TOKENS", "16000"))
    except ValueError:
        print("[claude_write] MAX_TOKENS는 정수여야 합니다.", file=sys.stderr)
        sys.exit(1)

    job_id = env("JOB_ID")
    nightshift_url = env("NIGHTSHIFT_URL", "http://127.0.0.1:8000")
    output_dir = env("NIGHTSHIFT_OUTPUT_DIR", "/workspace/output")
    out_dir = Path(output_dir) / job_id if job_id else Path(output_dir)

    print(f"[claude_write] {model}에 프롬프트 전송 (max_tokens={max_tokens})")
    report_progress(job_id, nightshift_url, 1, 0)

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError as e:
        print(f"[claude_write] 인증 실패 — API 키를 확인하세요: {e}", file=sys.stderr)
        sys.exit(1)
    except anthropic.RateLimitError as e:
        print(f"[claude_write] 요청 한도 초과: {e}", file=sys.stderr)
        sys.exit(1)
    except anthropic.APIStatusError as e:
        print(f"[claude_write] API 오류(HTTP {e.status_code}): {e.message}", file=sys.stderr)
        sys.exit(1)
    except anthropic.APIConnectionError as e:
        print(f"[claude_write] 연결 실패: {e}", file=sys.stderr)
        sys.exit(1)

    if response.stop_reason == "refusal":
        detail = getattr(response.stop_details, "category", None) if response.stop_details else None
        print(f"[claude_write] 모델이 이 요청을 거절했습니다 (category={detail}).", file=sys.stderr)
        report_progress(job_id, nightshift_url, 1, 1)
        sys.exit(1)

    text = "".join(block.text for block in response.content if block.type == "text")
    print(text)

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "output.md").write_text(text, encoding="utf-8")
    except OSError as e:
        print(f"[claude_write] 경고: 결과 파일을 남기지 못했어요: {e}", file=sys.stderr)

    report_progress(job_id, nightshift_url, 1, 1)
    print(f"[claude_write] 완료 ({len(text)}자, stop_reason={response.stop_reason})")


if __name__ == "__main__":
    main()
