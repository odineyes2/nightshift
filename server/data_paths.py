"""
런타임에 생기는 것들이 사는 곳 — 생성물을 소스 트리 밖으로 뺀다.

## 왜 필요한가

작업 기록·로그·최근 파일·파드 목록처럼 **돌면서 생기는 것**이 저장소 루트에 소스와
나란히 쌓여 있었다. 그 결과 셋이 문제였다:

1. 루트만 봐서는 무엇이 코드고 무엇이 생성물인지 알 수 없다(루트 항목의 절반이
   생성물이었다).
2. `jobs_state.json`이 커밋돼 있어서, 작업을 한 번만 돌려도 워킹 트리가 더러워지고
   홈서버에서 `git pull`을 할 때 걸린다.
3. `lora_triggers.json` 같은 파일은 추적도 `.gitignore`도 안 돼 있어서 운영 중인
   머신에서 `git status`를 치면 정체불명의 untracked 파일이 계속 떴다.

전부 `data/` 아래로 모으고 그 폴더를 통째로 무시하면 셋 다 사라진다.

## 무엇이 여기 들어가고 무엇이 안 들어가는가

들어가는 것은 **앱이 만들어내는 것**뿐이다. `templates/`(작업 스크립트), `static/`,
`prompt_enhancer.json`처럼 사람이 쓰고 커밋하는 것은 저장소에 그대로 남는다.
결과 이미지(`NIGHTSHIFT_OUTPUT_DIR`)도 여기 두지 않는다 — 보통 저장소 밖(원격 pod의
네트워크 볼륨 등)에 있고, 이미 환경변수로 따로 정하게 돼 있다.

## 이관

옛 위치(저장소 루트)에 있고 `data/`에 없으면 첫 import 때 한 번 옮긴다. 이미 돌고
있는 홈서버나 pod가 설정과 기록을 잃지 않게 하기 위한 것으로, `pod_registry`가
`comfy_endpoint.json` → `pods.json`을 옮겨 담는 것과 같은 방식이다.

환경변수:
    NIGHTSHIFT_DATA_DIR   런타임 데이터 폴더 (기본 <저장소>/data)
"""

import os
import shutil
from pathlib import Path

# 이 파일은 server/ 안에 있지만, data/는 저장소 루트에 둔다 — server/ 자체가
# 코드 트리이고 data/는 그와 나란한 별개 산출물 트리이기 때문이다(server/ 안에
# 중첩시키면 "코드와 생성물을 가른다"는 애초 목적이 흐려진다).
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("NIGHTSHIFT_DATA_DIR") or (REPO_ROOT / "data"))

# 옛 위치(저장소 루트)에서 data/로 옮겨올 이름들. 여기 적힌 것이 곧 "이 앱이 만들어
# 내는 것 전부"이기도 하다 — 새로 저장소를 하나 늘릴 때 여기에도 이름을 추가한다.
MIGRATED_NAMES = (
    "jobs",
    "logs",
    "recent_workflows",
    "recent_csvs",
    "workflow_presets",
    "jobs_state.json",
    "recent_workflows_state.json",
    "recent_csvs_state.json",
    "pods.json",
    "comfy_endpoint.json",
    "comfy_output_sync.json",
    "lora_triggers.json",
    "base_model_families.json",
    "danbooru_tag_edits.json",
    "danbooru_history.json",
)


def _migrate_legacy():
    """옛 위치에 있는 것을 data/로 옮긴다. 이미 data/에 있으면 건드리지 않는다 —
    양쪽에 다 있으면 새 위치가 최신이라고 본다(이관은 한 번만 일어나므로)."""
    for name in MIGRATED_NAMES:
        old = REPO_ROOT / name
        new = DATA_DIR / name
        if not old.exists() or new.exists():
            continue
        try:
            shutil.move(str(old), str(new))
        except OSError:
            # 옮기지 못해도 앱은 떠야 한다 — 새 위치에서 빈 상태로 시작할 뿐이다.
            # (권한 문제나 다른 프로세스가 잡고 있는 경우)
            pass


DATA_DIR.mkdir(parents=True, exist_ok=True)
_migrate_legacy()


def data_path(name: str) -> Path:
    """data/ 아래의 경로 하나. 이름은 MIGRATED_NAMES에 있는 것과 같아야 이관이 된다."""
    return DATA_DIR / name


def data_dir(name: str) -> Path:
    """data/ 아래의 폴더 하나 — 없으면 만들어서 돌려준다."""
    path = DATA_DIR / name
    path.mkdir(parents=True, exist_ok=True)
    return path
