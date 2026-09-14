#!/usr/bin/env python3
"""
scripts/migrate_export.py
---------------------------
RunPod 등 지금 nightshift가 도는 곳(이관할 "원본" 머신)에서 실행한다. 홈서버로
옮겨야 할 세 트리를 다룬다:

    NIGHTSHIFT_DATA_DIR    (기본 <저장소>/data)         — 작업기록/로그/설정 등 작은 상태
    NIGHTSHIFT_OUTPUT_DIR  (기본 /workspace/output)     — 결과 이미지/영상 (보통 제일 큼)
    NIGHTSHIFT_ASSETS_DIR  (기본 /workspace/dataset/assets) — 입력 이미지 + pose/depth/lineart 참조

이 스크립트가 실제로 대량 파일을 복사하지는 않는다 — 결과 이미지/영상, 참조
이미지처럼 큰 트리는 rsync가 훨씬 안정적으로(재개 가능, 변경분만 전송) 잘 하는
일이라, 이 스크립트는 그 자리에서 쓸 정확한 rsync 명령만 만들어 화면과 파일로
남긴다. 대신 다음 두 가지를 한다:

    1. data/ 트리(보통 수십MB 이하)는 크기가 작으므로 여기서 직접 tar.gz로
       통째로 묶는다 — 홈서버로는 scp 한 줄이면 충분하다.
    2. 세 트리 전부를 스캔해 "무엇이 있었는지"(상대경로/크기) manifest.json을
       만든다 — 홈서버에서 옮긴 뒤 migrate_verify.py로 빠짐/손상 여부를 확인하는
       데 쓴다.

사용법:
    python3 scripts/migrate_export.py --out ./migration_bundle

만들어지는 것(--out 폴더 안):
    manifest.json         세 트리의 파일 목록/크기 스냅샷 (migrate_verify.py가 씀)
    data.tar.gz            data/ 전체 압축본
    rsync_commands.sh      결과 이미지/영상, 참조 이미지를 옮기는 rsync 명령 모음
                           (호스트 정보는 <POD_SSH_HOST> 등으로 자리만 잡아둠 —
                           RunPod 콘솔의 "Connect" > SSH 정보로 바꿔서 홈서버에서
                           실행해야 함)

환경변수 (nightshift 서버와 완전히 같은 이름/기본값 — server/data_paths.py,
server/output_images.py, server/ref_assets.py 참고. 이 스크립트는 서버 모듈을
import하지 않고 그대로 복사했다 — scripts/의 다른 유틸리티들과 같은 정책):
    NIGHTSHIFT_DATA_DIR, NIGHTSHIFT_OUTPUT_DIR, NIGHTSHIFT_ASSETS_DIR
    NIGHTSHIFT_POSES_DIR / NIGHTSHIFT_DEPTH_DIR / NIGHTSHIFT_LINEART_DIR
        (개별 지정 시 ASSETS_DIR/pose 등 대신 사용 — ref_assets.py와 동일한 규칙)
"""

import argparse
import json
import os
import socket
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def env_path(name, default):
    value = os.environ.get(name)
    return Path(value) if value else Path(default)


def resolve_roots():
    data_dir = env_path("NIGHTSHIFT_DATA_DIR", REPO_ROOT / "data")
    output_dir = env_path("NIGHTSHIFT_OUTPUT_DIR", "/workspace/output")
    assets_dir = env_path("NIGHTSHIFT_ASSETS_DIR", "/workspace/dataset/assets")
    return {
        "data": data_dir,
        "output": output_dir,
        "assets_input": assets_dir / "input",
        "assets_pose": env_path("NIGHTSHIFT_POSES_DIR", assets_dir / "pose"),
        "assets_depth": env_path("NIGHTSHIFT_DEPTH_DIR", assets_dir / "depth"),
        "assets_lineart": env_path("NIGHTSHIFT_LINEART_DIR", assets_dir / "lineart"),
    }


def scan_tree(root: Path):
    if not root.exists():
        return {"path": str(root), "exists": False, "file_count": 0, "total_bytes": 0, "files": []}
    files = []
    total_bytes = 0
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        try:
            stat = p.stat()
        except OSError:
            continue
        rel = str(p.relative_to(root))
        files.append({"rel": rel, "size": stat.st_size, "mtime": int(stat.st_mtime)})
        total_bytes += stat.st_size
    return {"path": str(root), "exists": True, "file_count": len(files), "total_bytes": total_bytes, "files": files}


def human_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def make_data_tarball(data_dir: Path, dest: Path):
    if not data_dir.exists():
        print(f"[migrate_export] 경고: data 폴더가 없습니다: {data_dir}", file=sys.stderr)
        return None
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(data_dir, arcname="data")
    return dest


def write_rsync_commands(roots, dest: Path):
    lines = [
        "#!/bin/sh",
        "# 홈서버에서 실행하세요 (RunPod pod -> 홈서버로 '당겨오는' 방향).",
        "# <POD_SSH_HOST>/<POD_SSH_PORT>는 RunPod 콘솔의 'Connect' > SSH 정보로 바꿔 넣으세요.",
        "# 목적지 경로(오른쪽)는 홈서버에서 실제 쓸 NIGHTSHIFT_OUTPUT_DIR/NIGHTSHIFT_ASSETS_DIR",
        "# 값과 맞춰서 바꾸세요 (.env에 설정한 값과 반드시 일치해야 갤러리가 이어집니다).",
        "# 목적지 폴더가 없으면 rsync가 알아서 만들지 않으니 미리 mkdir -p 해두세요.",
        "set -e",
        "",
    ]
    for key in ("output", "assets_input", "assets_pose", "assets_depth", "assets_lineart"):
        root = roots[key]
        lines.append(f"mkdir -p {root}")
        lines.append(
            f'rsync -avz --progress -e "ssh -p <POD_SSH_PORT>" '
            f'root@<POD_SSH_HOST>:{root}/ {root}/'
        )
        lines.append("")
    dest.write_text("\n".join(lines), encoding="utf-8")
    dest.chmod(0o755)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="./migration_bundle", help="산출물을 담을 폴더 (기본 ./migration_bundle)")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    roots = resolve_roots()

    print("[migrate_export] 대상 폴더:")
    for key, path in roots.items():
        print(f"  {key:14s} {path}")

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_host": socket.gethostname(),
        "roots": {},
    }
    for key, path in roots.items():
        info = scan_tree(path)
        manifest["roots"][key] = info
        status = "없음" if not info["exists"] else f"{info['file_count']}개, {human_bytes(info['total_bytes'])}"
        print(f"[migrate_export] {key}: {status}")

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[migrate_export] manifest 저장: {manifest_path}")

    tarball_path = out_dir / "data.tar.gz"
    if make_data_tarball(roots["data"], tarball_path):
        print(f"[migrate_export] data/ 압축 완료: {tarball_path} ({human_bytes(tarball_path.stat().st_size)})")

    rsync_script_path = out_dir / "rsync_commands.sh"
    write_rsync_commands(roots, rsync_script_path)
    print(f"[migrate_export] rsync 명령 스크립트 생성: {rsync_script_path} (홈서버에서 값 채운 뒤 실행)")

    print()
    print("[migrate_export] 다음 순서로 진행하세요:")
    print(f"  1. {manifest_path}와 {rsync_script_path}(data.tar.gz도)를 홈서버로 옮깁니다 (scp 등).")
    print(f"  2. {rsync_script_path.name}을 열어 <POD_SSH_HOST>/<POD_SSH_PORT>와 목적지 경로를 채운 뒤 홈서버에서 실행합니다.")
    print("  3. 홈서버에서 data.tar.gz를 NIGHTSHIFT_DATA_DIR의 부모 폴더에 풉니다:")
    print("       tar xzf data.tar.gz -C <NIGHTSHIFT_DATA_DIR의 부모 폴더>")
    print("  4. scripts/migrate_verify.py --manifest manifest.json 로 개수/용량이 일치하는지 확인하세요.")


if __name__ == "__main__":
    main()
