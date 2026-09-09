"""
셸 명령 배치 — 셸 파드(drivers/shell.py)에서 임의의 명령을 돌린다.

이 저장소의 다른 템플릿들이 ComfyUI에 프롬프트를 밀어 넣는 것과 같은 자리에 있지만,
상대가 ComfyUI가 아니라 셸이다. nightshift 입장에서는 "스크립트를 서브프로세스로 돌리고
진행 상황을 보고받고 산출물을 job_id 폴더에서 찾는다"는 점이 완전히 같고, 그래서 큐/워커/
대시보드가 그대로 동작한다 — 그게 이 템플릿이 있는 이유다(multipod_plan.md의 P3).

무엇을 하나:
    COMMAND를 REPEAT번 실행한다. SHELL_TARGET이 있으면 그 머신에 ssh로 붙어서 돌리고,
    비어 있으면 이 머신에서 돌린다. 표준 출력/에러는 작업 로그에 그대로 흘려보내고,
    회차마다의 출력을 NIGHTSHIFT_OUTPUT_DIR/<JOB_ID>/shell_<n>.txt로도 남긴다 —
    작업이 끝난 뒤 무엇이 나왔는지 파일로 되짚어볼 수 있게.

주의:
    이 템플릿은 정의상 임의 명령을 실행한다. 셸 파드 자체가 NIGHTSHIFT_ENABLE_SHELL_PODS로
    막혀 있고(기본 꺼짐), 그 위에 NIGHTSHIFT_API_KEY까지 걸어두는 것을 강하게 권한다.

환경변수 (nightshift가 주입한다):
    COMMAND            실행할 명령 (필수)
    REPEAT             몇 번 반복할지 (기본 1)
    TIMEOUT_SEC        한 회차의 제한 시간 (기본 600)
    SHELL_TARGET       ssh 대상(user@host). 비어 있으면 이 머신에서 실행
    SHELL_WORKDIR      명령을 실행할 폴더
    NIGHTSHIFT_OUTPUT_DIR  산출물이 쌓이는 폴더 (기본 /workspace/output)
    JOB_ID             nightshift가 주입하는 이 작업의 id (진행 상황 보고용, 없으면 보고 생략)
    NIGHTSHIFT_URL     nightshift 자신의 주소 (진행 상황 보고용, 기본 http://127.0.0.1:8000)
    NIGHTSHIFT_API_KEY 설정돼 있으면 진행 상황 보고에 X-API-Key로 실어 보냄 (선택)
"""

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path


def env(name, default=None):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def report_progress(job_id, nightshift_url, total, done):
    if not job_id or not nightshift_url:
        return
    try:
        payload = json.dumps({"total": total, "done": done}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        # nightshift가 NIGHTSHIFT_API_KEY로 인증을 켜두면 /api/*가 전부 이 키를
        # 요구한다. 키 없이 보내면 401로 조용히 실패해서(경고만 찍고 계속 돈다)
        # 진행률 바가 영영 안 움직인다 — 셸 파드를 쓸 정도면 키를 켜뒀을 가능성이
        # 높으므로 여기서 같이 실어 보낸다.
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
        print(f"[shell_command] 경고: 진행 상황 보고 실패: {e}", file=sys.stderr)


def build_argv(command, target, workdir):
    """실행할 argv. 로컬이면 셸에 그대로 맡기고, 원격이면 ssh에 넘긴다.

    셸을 거치는 건 의도한 것이다 — 파이프나 리다이렉션이 있는 한 줄짜리 명령을 그대로
    쓸 수 있어야 이 템플릿이 쓸모가 있다. 임의 명령 실행이라는 점은 셸 파드 자체가
    옵트인이라는 것으로 감당한다(drivers/shell.py 참고)."""
    if target:
        remote = f"cd {workdir} && {command}" if workdir else command
        return ["ssh", "-o", "BatchMode=yes", target, remote]
    return ["bash", "-lc", command]


def run_once(command, target, workdir, timeout, out_dir, index):
    argv = build_argv(command, target, workdir)
    where = f"ssh {target}" if target else "로컬"
    print(f"[shell_command] [{index}] {where}에서 실행: {command}")

    cwd = workdir if (not target and workdir and Path(workdir).is_dir()) else None
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        print(f"[shell_command] [{index}] 제한 시간({timeout}s) 초과", file=sys.stderr)
        return 124, ""
    except OSError as e:
        print(f"[shell_command] [{index}] 실행할 수 없어요: {e}", file=sys.stderr)
        return 127, ""

    stdout = proc.stdout.decode("utf-8", "replace")
    stderr = proc.stderr.decode("utf-8", "replace")
    if stdout:
        print(stdout, end="" if stdout.endswith("\n") else "\n")
    if stderr:
        print(stderr, end="" if stderr.endswith("\n") else "\n", file=sys.stderr)

    # 로그는 흘러가 버리므로 회차별 출력을 파일로도 남긴다. 다른 템플릿이 결과 이미지를
    # 두는 자리(job_id 폴더)와 같은 곳이라, 나중에 갤러리가 이미지 아닌 산출물까지
    # 다루게 되면 그대로 보인다.
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"shell_{index}.txt").write_text(
            f"$ {command}\n[exit {proc.returncode}]\n\n{stdout}\n{stderr}", encoding="utf-8")
    except OSError as e:
        print(f"[shell_command] 경고: 출력 파일을 남기지 못했어요: {e}", file=sys.stderr)

    print(f"[shell_command] [{index}] 종료 코드 {proc.returncode}")
    return proc.returncode, stdout


def main():
    command = env("COMMAND")
    if not command:
        print("[shell_command] COMMAND 환경변수가 필요합니다.", file=sys.stderr)
        sys.exit(1)

    try:
        repeat = max(1, int(env("REPEAT", "1")))
        timeout = float(env("TIMEOUT_SEC", "600"))
    except ValueError:
        print("[shell_command] REPEAT/TIMEOUT_SEC는 숫자여야 합니다.", file=sys.stderr)
        sys.exit(1)

    target = env("SHELL_TARGET", "")
    workdir = env("SHELL_WORKDIR", "")
    job_id = env("JOB_ID")
    nightshift_url = env("NIGHTSHIFT_URL", "http://127.0.0.1:8000")
    output_dir = env("NIGHTSHIFT_OUTPUT_DIR", "/workspace/output")
    out_dir = Path(output_dir) / job_id if job_id else Path(output_dir)

    print(f"[shell_command] 총 {repeat}회 실행 예정")
    report_progress(job_id, nightshift_url, repeat, 0)

    failed = 0
    for index in range(1, repeat + 1):
        code, _stdout = run_once(command, target, workdir, timeout, out_dir, index)
        if code != 0:
            failed += 1
        report_progress(job_id, nightshift_url, repeat, index)

    print(f"[shell_command] 총 {repeat}회 완료 (실패 {failed}회)")
    # 한 번이라도 실패했으면 작업 전체를 실패로 표시한다 — 조용히 성공으로 넘어가면
    # 작업 목록만 보고는 문제를 못 알아챈다.
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
