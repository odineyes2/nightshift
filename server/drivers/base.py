"""
파드 드라이버의 공통 인터페이스.

## 왜 드라이버인가

nightshift의 작업 실행 구조는 사실 ComfyUI를 거의 모른다 — 작업은 "스크립트 + 입력 파일 +
옵션"이고, 워커가 그걸 서브프로세스로 돌리고, 스크립트가 진행 상황을 HTTP로 보고하고,
결과물이 job_id 폴더에 쌓인다. ComfyUI에 묶여 있는 건 딱 네 가지다: 주소를 어떻게 찾는지,
능력(설치된 모델/노드)을 어떻게 조회하는지, 작업에 어떤 환경변수를 실어 보내는지, 결과물을
어떻게 회수하는지.

그 네 가지를 드라이버로 뽑아내면, 이미지가 아닌 다른 일을 하는 워커(글 쓰기, 지도 그리기,
임의 스크립트 실행 등)를 같은 목록·같은 큐·같은 화면에 나란히 붙일 수 있다. 카드까지
드라이버가 그리게 두는 이유도 같다 — 워커 종류마다 보여줄 게 다르기 때문이다.

## 지금 범위 (P0)

작업을 실제로 실행하는 부분(서브프로세스 띄우고 로그 받고 중단 요청 처리)은 아직 app.py의
worker_loop에 있다. 그 부분은 워커 종류와 무관한 "일반 실행기"라서 드라이버로 내리는 대신,
드라이버는 job_env()로 "이 파드에 작업을 보낼 때 필요한 환경변수"만 기여한다. 파드별 큐로
쪼개는 단계(P1)에서 worker_loop을 다시 쓸 때, 완전히 다른 방식으로 작업을 실행하는 워커를
위해 run() 훅을 열 예정이다(multipod_plan.md 참고).
"""


class DriverError(Exception):
    """드라이버가 파드를 다루지 못할 때 (호출부가 4xx/5xx로 바꿔 쓴다)."""


class PodDriver:
    """파드 종류 하나를 다루는 방법. 인스턴스가 아니라 모듈 수준 함수 모음처럼 쓴다
    (드라이버는 상태를 갖지 않고, 파드 레코드를 매번 인자로 받는다)."""

    kind = "base"
    label = "알 수 없음"

    # ---- 등록 가능 여부 -------------------------------------------------------
    @staticmethod
    def available() -> bool:
        """이 종류의 파드를 지금 등록해도 되는가. 기본은 항상 가능. 셸 파드처럼
        위험해서 명시적으로 켜야 하는 종류가 이걸 덮어쓴다."""
        return True

    @staticmethod
    def unavailable_reason() -> str:
        return "이 종류의 파드는 지금 쓸 수 없어요."

    # ---- 주소 ---------------------------------------------------------------
    @staticmethod
    def normalize_url(raw: str) -> str:
        """주소를 저장 형태로 다듬는다. 무엇이 올바른 주소인지는 워커 종류마다 다르므로
        (HTTP 엔드포인트일 수도, ssh 대상일 수도 있다) 드라이버가 정한다. 빈 값은
        "기본값에 맡긴다"는 뜻이라 어느 종류든 허용한다. 틀리면 ValueError."""
        import urllib.parse
        url = (raw or "").strip().rstrip("/")
        if not url:
            return ""
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("http:// 또는 https:// 로 시작하는 주소여야 해요.")
        return url

    @staticmethod
    def resolve(pod: dict) -> tuple[str | None, str]:
        """(지금 쓸 주소, 출처). 출처는 화면에 "이 주소가 어디서 왔는지"를 보여주는 용도."""
        raise NotImplementedError

    # ---- 상태 ---------------------------------------------------------------
    @staticmethod
    def health(pod: dict, timeout: float | None = None) -> dict:
        """{"ok": bool, "url": str|None, "source": str, "detail": str}."""
        raise NotImplementedError

    # ---- 능력 ---------------------------------------------------------------
    @staticmethod
    def capabilities(pod: dict, force: bool = False) -> tuple[str | None, dict | None]:
        """(주소, 능력 정보). 이 파드가 무엇을 할 수 있는지 — ComfyUI라면 설치된
        노드/모델 목록이다. 조회가 비싸면 드라이버가 알아서 캐싱한다."""
        raise NotImplementedError

    # ---- 작업 실행 ----------------------------------------------------------
    @staticmethod
    def job_env(pod: dict, url: str) -> dict:
        """이 파드로 작업을 보낼 때 스크립트에 실어줄 환경변수."""
        return {}

    # ---- 결과물 -------------------------------------------------------------
    @staticmethod
    def collect(pod: dict, url: str, job_id: str) -> dict | None:
        """작업이 끝난 뒤 산출물을 회수한다. 회수할 게 없는 워커면 None."""
        return None

    # ---- 대시보드 -----------------------------------------------------------
    @staticmethod
    def card(pod: dict) -> dict:
        """대시보드 카드에 실을 이 파드 고유의 요약 정보. 공통 정보(이름/상태/큐 길이)는
        호출부가 채우므로, 여기서는 워커 종류에만 있는 것을 담는다."""
        return {}
