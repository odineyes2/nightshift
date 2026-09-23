"""
RunPod에서 지금 살아 있는 ComfyUI pod를 nightshift 파드 목록에 자동으로 맞춘다.

## 왜 필요한가

RunPod pod는 필요할 때만 켜고 끄는(시간당 과금) 자원이라, 켤 때마다 화면에 로그인해서
주소를 손으로 등록하는 게 번거롭다. Claude가 RunPod 커넥터로 pod를 켠 뒤 이 모듈의
`sync_runpod_pods()`를 MCP로 부르면(server/mcp_server.py의 sync_runpod_pods 도구),
사람 로그인 없이도 그 pod가 nightshift 파드 목록에 자동으로 나타난다.

## 매칭 규칙

nightshift 파드의 url이 RunPod 프록시 주소(https://{POD_ID}-{PORT}.proxy.runpod.net/)면
그 안에 박힌 POD_ID로 RunPod의 실제 pod와 짝짓는다(runpod_api.extract_pod_id) — 문자열
그대로 비교하면 끝의 "/" 유무 같은 표기 차이로 어긋날 수 있어서다. 이 자동 동기화가
만든 파드에는 tags=["runpod","auto"]를 붙여 두고, 그 태그가 있는 파드만 "사람이 손대지
않은 것"으로 보고 자동으로 켜고 끈다 — 사람이 화면에서 일부러 끈 파드(auto 태그 없음)는
절대 건드리지 않고, 실행 중인 작업이 있는 파드도 건드리지 않고, **삭제는 절대 하지
않는다**(enabled만 내린다). url이 빈 파드(기본 파드 등)는 extract_pod_id가 None을
돌려주므로 매칭 대상에서 자연히 빠진다.

## 동시성

pod_registry._lock(재진입 가능한 RLock)을 "매칭 확인 → 생성/수정" 전체에 걸쳐 잡고
있다가, 그 안에서 pod_registry.create_pod/update_pod를 부른다(그 함수들도 내부에서
같은 락을 다시 잡지만 RLock이라 데드락이 없다) — 그래야 동시에 두 번 불려도 같은
RunPod pod가 중복으로 등록되지 않는다.

## ensure_runtime_fn / job_has_running_fn

둘 다 server/app.py에만 있는 개념(파드 워커 스레드, 진행 중인 작업)이라 이 모듈이
app.py를 직접 import하면 순환 import가 된다. 그래서 호출부(app.py)가 자기 함수를
그대로 넘겨준다 — 이 모듈은 "그 파드 id로 뭘 하는 함수"라는 것만 알면 된다.
"""

import logging
import os

import pod_registry
import runpod_api
import runpod_sessions

log = logging.getLogger("uvicorn.error")


def sync_runpod_pods(owner_id: int, dry_run: bool, ensure_runtime_fn, job_has_running_fn) -> dict:
    """RunPod에서 RUNNING이고 ComfyUI 포트(RUNPOD_SYNC_COMFY_PORT, 기본 8188)가 열린
    pod를 찾아 등록/되살리고, 더는 RUNNING이 아닌 auto 파드는(실행 중인 작업이 없을 때만)
    끈다. dry_run이면 아무것도 바꾸지 않고 무엇을 했을지만 담아 돌려준다."""
    comfy_port = (os.environ.get("RUNPOD_SYNC_COMFY_PORT") or "8188").strip() or "8188"
    result: dict = {"dry_run": dry_run, "runpod_ok": True, "error": None,
                     "added": [], "reenabled": [], "disabled": [], "unchanged": [], "skipped": []}

    pods, error = runpod_api.list_runpod_pods_verbose()
    if error:
        result["runpod_ok"] = False
        result["error"] = error
        return result

    running_by_id = {
        p["id"]: p for p in pods
        if p["status"] == "RUNNING" and f"{comfy_port}/http" in p["ports"]
    }

    with pod_registry._lock:
        existing = pod_registry.list_pods(pod_registry.ALL)
        existing_by_runpod_id = {}
        for p in existing:
            if p.get("kind") != pod_registry.DEFAULT_KIND:
                continue
            rpid = runpod_api.extract_pod_id(p.get("url") or "")
            if rpid:
                existing_by_runpod_id[rpid] = p

        for rpid, rp in running_by_id.items():
            url = f"https://{rpid}-{comfy_port}.proxy.runpod.net"
            existing_pod = existing_by_runpod_id.get(rpid)
            if existing_pod is None:
                item = {"id": None, "name": rp["name"] or None, "url": url, "runpod_pod_id": rpid}
                if not dry_run:
                    created = pod_registry.create_pod({
                        "kind": pod_registry.DEFAULT_KIND, "url": url,
                        "name": rp["name"], "tags": ["runpod", "auto"],
                        "note": f"runpod:{rpid}", "owner_id": owner_id,
                    })
                    item["id"] = created["id"]
                    item["name"] = created["name"]
                    ensure_runtime_fn(created)
                result["added"].append(item)
            elif existing_pod.get("enabled"):
                result["unchanged"].append({"id": existing_pod["id"], "runpod_pod_id": rpid})
            elif "auto" in (existing_pod.get("tags") or []):
                if not dry_run:
                    pod_registry.update_pod(existing_pod["id"], {"enabled": True})
                result["reenabled"].append({"id": existing_pod["id"], "runpod_pod_id": rpid})
            else:
                result["skipped"].append({"runpod_pod_id": rpid,
                                           "reason": "사람이 화면에서 꺼둔 파드예요(auto 태그 없음)."})

        for rpid, pod in existing_by_runpod_id.items():
            if rpid in running_by_id or not pod.get("enabled") or "auto" not in (pod.get("tags") or []):
                continue
            if job_has_running_fn(pod["id"]):
                result["skipped"].append({"runpod_pod_id": rpid, "reason": "이 파드에 실행 중인 작업이 있어요."})
                continue
            if not dry_run:
                pod_registry.update_pod(pod["id"], {"enabled": False})
            result["disabled"].append({"id": pod["id"], "runpod_pod_id": rpid})

    # 사용 내역(DB 탭) 로깅은 nightshift 파드 등록(위, ComfyUI 포트가 열린 것만)과는
    # 무관하게 RunPod 계정에 있는 pod 전부를 대상으로 한다 — "얼마나 썼나" 기록이
    # 목적이라 ComfyUI 포트 유무와 상관없이 완전하게 남겨야 한다. dry_run이면(아무것도
    # 안 바꾸는 게 정의) 이것도 건너뛴다.
    if not dry_run:
        for p in pods:
            gpu_type = runpod_api.get_gpu_type_cached(p["id"]) or ""
            runpod_sessions.sync_pod_session(
                p["id"], p["name"], gpu_type, p.get("cost_per_hr"), p["status"] == "RUNNING",
                p.get("last_started_at"), p.get("last_status_change"))

    log.info("runpod 동기화%s: added=%d reenabled=%d disabled=%d unchanged=%d skipped=%d",
              " (dry_run)" if dry_run else "", len(result["added"]), len(result["reenabled"]),
              len(result["disabled"]), len(result["unchanged"]), len(result["skipped"]))
    return result


__all__ = ["sync_runpod_pods"]
