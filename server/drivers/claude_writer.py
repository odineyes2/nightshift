"""
Claude 글쓰기 파드 드라이버 — Claude API(Anthropic)로 텍스트를 생성하는 워커.

## ComfyUI/셸과 다른 점

이 워커는 원격 엔드포인트가 없다 — nightshift 프로세스 자신이 Claude API를 호출한다.
그래서 url/health 개념이 "그 주소에 접속되는가"가 아니라 "API 키가 설정돼 있는가"로
바뀐다. 파드 레코드의 url 필드는 아무 의미가 없으므로 무엇을 넣든 무시하고 항상
빈 문자열로 저장한다 — 에러를 내는 대신 조용히 무시하는 편이 "주소를 왜 지웠지"보다
덜 헷갈린다.

## 결과물을 어떻게 보는가

이미지가 아니라 글이라 갤러리(썸네일 격자)로는 못 보여준다. 대신 템플릿
(templates/claude_write.py)이 결과를 NIGHTSHIFT_OUTPUT_DIR/<job_id>/output.md에
직접 저장하고, 화면은 파드 스코프의 "📝 결과" 탭(POD_SUBTABS.claude_writer)에서
GET /api/jobs/{id}/text-result로 그 파일을 읽어 보여준다. collect()가 항상 None인
이유도 같다 — 원격 GPU pod처럼 "가져와야 할" 산출물이 아니라, 이 프로세스 자신이
API를 호출해 이미 로컬에 써 둔 것이기 때문이다.

## 왜 옵트인이 필요한가

ANTHROPIC_API_KEY가 없으면 이 종류의 파드를 아예 등록할 수 없다(available()) —
어차피 키 없이는 아무것도 못 하므로, 등록 후에 매번 "연결 안 됨"으로 보이는 것보다
애초에 "파드 종류" 선택지에서 빼는 편이 덜 헷갈린다.

환경변수:
    ANTHROPIC_API_KEY  Claude API 키 (.env, 필수 — 없으면 이 종류를 등록할 수 없음)
"""

import os

from .base import PodDriver

ANTHROPIC_API_KEY = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()

# 모델을 고를 수 있게 해달라는 요청은 아직 없었다 — 나중에 필요해지면 파드 레코드의
# note 필드(셸 드라이버가 workdir을 담는 것과 같은 자리)에 모델 이름을 넣고 여기서
# 읽게 확장하면 된다(YAGNI — 지금은 하드코딩).
CLAUDE_MODEL = "claude-opus-5"


class ClaudeWriterDriver(PodDriver):
    kind = "claude_writer"
    label = "Claude 글쓰기"

    @staticmethod
    def available() -> bool:
        return bool(ANTHROPIC_API_KEY)

    @staticmethod
    def unavailable_reason() -> str:
        return "ANTHROPIC_API_KEY가 설정돼 있지 않아요. .env에 넣으면 이 종류의 파드를 추가할 수 있어요."

    @staticmethod
    def normalize_url(raw: str) -> str:
        """이 워커는 원격 주소가 없다 — 뭘 입력해도 조용히 무시한다."""
        return ""

    @staticmethod
    def resolve(pod: dict) -> tuple[str | None, str]:
        return "Claude API (Anthropic)", "api"

    @staticmethod
    def health(pod: dict, timeout: float | None = None) -> dict:
        ok = bool(ANTHROPIC_API_KEY)
        return {
            "ok": ok, "url": "Claude API (Anthropic)", "source": "api",
            "detail": "" if ok else "ANTHROPIC_API_KEY가 설정돼 있지 않아요.",
        }

    @staticmethod
    def capabilities(pod: dict, force: bool = False) -> tuple[str | None, dict | None]:
        """이 워커에는 "설치된 모델 목록" 같은 게 없다 — 지금 쓰는 모델 이름 정도가
        전부다. 워크플로우 호환성 검사는 이 종류의 파드에 해당하지 않는다."""
        health = ClaudeWriterDriver.health(pod)
        if not health["ok"]:
            return health["url"], None
        return health["url"], {"model": CLAUDE_MODEL}

    @staticmethod
    def job_env(pod: dict, url: str) -> dict:
        return {"ANTHROPIC_API_KEY": ANTHROPIC_API_KEY, "CLAUDE_MODEL": CLAUDE_MODEL}

    @staticmethod
    def collect(pod: dict, url: str, job_id: str) -> dict | None:
        return None

    @staticmethod
    def card(pod: dict) -> dict:
        """대시보드 카드에 실을 이 파드 고유 정보 — 지금 쓰는 모델 이름."""
        if not ANTHROPIC_API_KEY:
            return {}
        return {"model": CLAUDE_MODEL}


__all__ = ["ClaudeWriterDriver", "ANTHROPIC_API_KEY", "CLAUDE_MODEL"]
