#!/usr/bin/env python3
"""
scripts/migrate_verify.py
---------------------------
홈서버(이관 "목적지" 머신)에서 실행한다. migrate_export.py가 만든 manifest.json과
지금 이 머신의 NIGHTSHIFT_DATA_DIR/NIGHTSHIFT_OUTPUT_DIR/NIGHTSHIFT_ASSETS_DIR
(및 pose/depth/lineart 개별 override)를 다시 스캔해 비교한다. 트리별로 파일
개수/용량 총합을 맞춰보고, 빠졌거나 크기가 다른 파일을 목록으로 보여준다.

사용법:
    python3 scripts/migrate_verify.py --manifest ./migration_bundle/manifest.json

환경변수는 migrate_export.py와 완전히 같다 — 반드시 nightshift 서버가 실제로 쓸
값과 같게 맞춰서(즉, .env에 설정한 값 그대로) 실행해야 의미가 있다.
"""

import argparse
import json
import os
import sys
from pathlib import Path

# migrate_export.py의 resolve_roots/scan_tree/human_bytes를 그대로 복사했다
# (스크립트 자기완결성 — templates/*.py와 같은 정책. 고치면 두 파일 다 같이
# 고쳐야 한다).

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


def compare_tree(key, expected, actual_root, max_report=20):
    actual = scan_tree(actual_root)
    expected_by_rel = {f["rel"]: f for f in expected["files"]}
    actual_by_rel = {f["rel"]: f for f in actual["files"]}

    missing = [rel for rel in expected_by_rel if rel not in actual_by_rel]
    size_mismatch = [
        rel for rel in expected_by_rel
        if rel in actual_by_rel and expected_by_rel[rel]["size"] != actual_by_rel[rel]["size"]
    ]
    extra = [rel for rel in actual_by_rel if rel not in expected_by_rel]

    ok = not missing and not size_mismatch
    print(
        f"[migrate_verify] {key}: {'OK' if ok else 'FAIL'} "
        f"(원본 {expected['file_count']}개/{human_bytes(expected['total_bytes'])} -> "
        f"현재 {actual['file_count']}개/{human_bytes(actual['total_bytes'])})"
    )
    if missing:
        print(f"  누락 {len(missing)}개 (최대 {max_report}개만 표시):")
        for rel in missing[:max_report]:
            print(f"    - {rel}")
    if size_mismatch:
        print(f"  크기 불일치 {len(size_mismatch)}개 (최대 {max_report}개만 표시):")
        for rel in size_mismatch[:max_report]:
            print(f"    - {rel} (원본 {expected_by_rel[rel]['size']}B, 현재 {actual_by_rel[rel]['size']}B)")
    if extra:
        print(f"  참고: 원본에 없던 파일이 {len(extra)}개 더 있습니다(이관 후 새로 생긴 것이면 정상).")
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", required=True, help="migrate_export.py가 만든 manifest.json 경로")
    parser.add_argument("--max-report", type=int, default=20)
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    roots = resolve_roots()

    print(f"[migrate_verify] 원본({manifest.get('source_host')}, {manifest.get('generated_at')}) 기준으로 확인합니다.")
    all_ok = True
    for key, expected in manifest["roots"].items():
        if key not in roots:
            continue
        ok = compare_tree(key, expected, roots[key], max_report=args.max_report)
        all_ok = all_ok and ok

    print()
    if all_ok:
        print("[migrate_verify] 전부 일치합니다 — 이관이 성공적으로 끝났습니다.")
    else:
        print("[migrate_verify] 일부 불일치가 있습니다 — 위 목록을 확인하고 rsync를 다시 돌려보세요.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
