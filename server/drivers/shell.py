"""
셸 파드 드라이버 — 등록한 머신에서 임의의 명령을 돌리는 워커.

## 무엇을 증명하는가

이 드라이버의 첫 목적은 "ComfyUI가 아닌 워커가 정말로 이 구조에 들어오는가"를 실물로
확인하는 것이다(multipod_plan.md의 P3). 셸 파드는 ComfyUI와 거의 모든 것이 다르다 —
HTTP 엔드포인트가 없고, 설치된 모델 같은 것도 없고, 결과물을 회수할 필요도 없다(로컬에
바로 쓰므로). 그런데도 같은 레지스트리에 등록되고, 같은 큐/워커 구조로 돌고, 같은
대시보드에 나란히 뜬다. 그게 되면 나중에 글 쓰는 워커든 지도 그리는 워커든 같은 자리에
들어온다.

부수적으로 실제 쓸모도 있다 — 후처리 배치, 백업, 파일 정리 같은 GPU가 필요 없는 잡일.

## 대상(url 필드)

    (비어 있음)          nightshift가 도는 이 머신에서 실행
    ssh://user@host      그 머신에 ssh로 붙어 실행

실제 ssh 호출은 템플릿(templates/shell_command.py)이 한다. 이 저장소의 관례대로 워커와
실제로 대화하는 쪽은 템플릿 스크립트이고, 드라이버는 "어디로 보낼지"를 환경변수로 넘겨줄
뿐이다(job_env). 덕분에 app.py의 실행기는 한 줄도 바뀌지 않는다.

## 왜 옵트인이 필요한가

셸 파드는 정의상 **임의 명령 실행**이다. nightshift는 NIGHTSHIFT_API_KEY가 비어 있으면
인증 없이 뜨므로(기본값), 이 드라이버가 항상 켜져 있으면 "그 포트에 닿는 누구나 이 머신에서
아무 명령이나 돌릴 수 있는" 상태가 된다. 그래서 환경변수로 명시적으로 켜야만 등록할 수 있게
했다:

    NIGHTSHIFT_ENABLE_SHELL_PODS=1

켤 생각이라면 NIGHTSHIFT_API_KEY도 같이 설정하는 것을 강하게 권한다.

환경변수:
    NIGHTSHIFT_ENABLE_SHELL_PODS  1/true/yes면 셸 파드를 쓸 수 있다 (기본 꺼짐)
    NIGHTSHIFT_SHELL_WORKDIR      명령을 실행할 기본 작업 폴더 (기본: nightshift 폴더)
"""

import os
import shutil
import subprocess
import urllib.parse
from pathlib import Path

from .base import PodDriver

# 로컬 실행의 기본 작업 폴더 — "이 저장소 폴더"가 합리적인 기본값이다.
# 이 파일은 server/drivers/ 아래에 있으므로 두 단계 위가 저장소 루트다.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

DEFAULT_WORKDIR = os.environ.get("NIGHTSHIFT_SHELL_WORKDIR") or str(REPO_ROOT)
# ssh가 비밀번호를 물어보며 멈춰 있으면 워커 스레드가 통째로 잠긴다 — BatchMode로
# 물어보지 않게 하고, 짧은 타임아웃을 건다.
SSH_CHECK_TIMEOUT = 8


def shell_pods_enabled() -> bool:
    return (os.environ.get("NIGHTSHIFT_ENABLE_SHELL_PODS") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def parse_target(pod: dict) -> tuple[str | None, str]:
    """(ssh 대상, 사람이 읽을 표시). url이 비어 있으면 이 머신에서 실행한다."""
    raw = (pod.get("url") or "").strip()
    if not raw:
        return None, "이 머신 (로컬)"
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme != "ssh" or not parsed.netloc:
        # normalize_url이 ssh://만 통과시키므로 보통 여기 오지 않는다 — 손으로 고친
        # pods.json 같은 경우를 대비해, 알 수 없는 주소는 로컬 실행으로 떨어뜨리지 않고
        # 그대로 표시만 해서 사용자가 알아채게 한다.
        return None, raw
    return parsed.netloc, f"ssh://{parsed.netloc}"


def workdir(pod: dict) -> str:
    return (pod.get("note") or "").strip() or DEFAULT_WORKDIR


class ShellDriver(PodDriver):
    kind = "shell"
    label = "셸(스크립트)"

    @staticmethod
    def available() -> bool:
        return shell_pods_enabled()

    @staticmethod
    def unavailable_reason() -> str:
        return ("셸 파드는 임의 명령을 실행하므로 기본으로 꺼져 있어요. "
                "쓰려면 NIGHTSHIFT_ENABLE_SHELL_PODS=1로 켜고, "
                "NIGHTSHIFT_API_KEY도 함께 설정하세요.")

    @staticmethod
    def normalize_url(raw: str) -> str:
        """빈 값은 "이 머신에서 실행", 아니면 ssh://user@host."""
        url = (raw or "").strip().rstrip("/")
        if not url:
            return ""
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "ssh" or not parsed.netloc:
            raise ValueError("비워두면 이 머신에서 실행하고, 다른 머신이면 ssh://user@host 형태로 적어주세요.")
        return url

    @staticmethod
    def resolve(pod: dict) -> tuple[str | None, str]:
        target, label = parse_target(pod)
        return (pod.get("url") or "") or None, "ssh" if target else "local"

    @staticmethod
    def health(pod: dict, timeout: float | None = None) -> dict:
        target, label = parse_target(pod)
        if not shell_pods_enabled():
            return {"ok": False, "url": label, "source": "local",
                    "detail": "셸 파드가 꺼져 있어요 (NIGHTSHIFT_ENABLE_SHELL_PODS=1로 켭니다)."}
        if target is None:
            # 로컬은 "작업 폴더가 실제로 있는가"가 곧 살아 있는지 여부다.
            ok = Path(workdir(pod)).is_dir()
            return {"ok": ok, "url": label, "source": "local",
                    "detail": "" if ok else f"작업 폴더를 찾을 수 없어요: {workdir(pod)}"}
        if shutil.which("ssh") is None:
            return {"ok": False, "url": label, "source": "ssh", "detail": "이 서버에 ssh가 없어요."}
        try:
            proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o",
                 f"ConnectTimeout={int(timeout or SSH_CHECK_TIMEOUT)}", target, "true"],
                capture_output=True, timeout=(timeout or SSH_CHECK_TIMEOUT) + 2,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            return {"ok": False, "url": label, "source": "ssh", "detail": f"ssh 접속 실패: {e}"}
        ok = proc.returncode == 0
        detail = "" if ok else (proc.stderr.decode("utf-8", "replace").strip()[:200] or "ssh 접속 실패")
        return {"ok": ok, "url": label, "source": "ssh", "detail": detail}

    @staticmethod
    def capabilities(pod: dict, force: bool = False) -> tuple[str | None, dict | None]:
        """셸 워커에는 "설치된 모델 목록" 같은 게 없다. 대신 화면이 알아야 할 최소한
        (어디서 도는지, 어느 폴더에서 도는지)만 돌려준다 — 워크플로우 호환성 검사는
        이 종류의 파드에 해당하지 않는다."""
        target, label = parse_target(pod)
        health = ShellDriver.health(pod)
        if not health["ok"]:
            return label, None
        return label, {"target": target or "local", "workdir": workdir(pod)}

    @staticmethod
    def job_env(pod: dict, url: str) -> dict:
        """템플릿(shell_command.py)이 실제로 ssh를 걸거나 로컬에서 돌리는 데 필요한 값."""
        target, _label = parse_target(pod)
        return {"SHELL_TARGET": target or "", "SHELL_WORKDIR": workdir(pod)}

    @staticmethod
    def collect(pod: dict, url: str, job_id: str) -> dict | None:
        """회수할 게 없다. 로컬 실행이면 산출물이 이미 로컬에 있고, ssh 실행이면 무엇을
        어디로 가져올지는 사용자가 명령 안에서 정한다(scp/rsync 등) — 드라이버가 추측할
        수 있는 규칙이 없다."""
        return None

    @staticmethod
    def card(pod: dict) -> dict:
        """대시보드 카드에 실을 셸 파드 고유 정보 — ComfyUI의 VRAM 자리에 들어간다."""
        target, label = parse_target(pod)
        data = {"target": label, "workdir": workdir(pod)}
        if target is None:
            try:
                usage = shutil.disk_usage(workdir(pod))
                data["disk_free"] = usage.free
                data["disk_total"] = usage.total
            except OSError:
                pass
        return data
