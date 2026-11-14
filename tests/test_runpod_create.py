# RunPod 파드 만들기 요청과 부팅 스크립트 검사(NS-18). 실행: python tests/test_runpod_create.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import model_download  # noqa: E402
import runpod_api  # noqa: E402

seen = []
runpod_api._rest = lambda method, path, timeout=0, body=None, key="": (seen.append(body), ({"id": "x"}, None))[1]
pod, err = runpod_api.create_pod("t", "image", entrypoint=model_download.BOOTSTRAP_ENTRYPOINT, key="k")
assert err is None and pod["id"] == "x", err
assert seen[0]["gpu"]["minCudaVersion"] == "13.0" and "allowedCudaVersions" not in seen[0]["gpu"], seen[0]
# v2는 최상위 entrypoint 배열 — args는 문자열(CMD)이라 객체를 넣으면 422(2026-10-07)
assert seen[0]["entrypoint"] == model_download.BOOTSTRAP_ENTRYPOINT and "args" not in seen[0] and "mounts" not in seen[0], seen[0]

# 대체 GPU는 기존 후보 뒤에서만 시도한다.
gpus = runpod_api.RUNPOD_TIERS["image"]["gpus"]
assert gpus[4:] == ["NVIDIA GeForce RTX 3090", "NVIDIA RTX A5000", "NVIDIA GeForce RTX 4090", "NVIDIA L4"]
no_stock = "RunPod가 거절했어요(HTTP 400): There are no longer any instances available with the requested specifications."
seen.clear()
runpod_api._rest = lambda method, path, timeout=0, body=None, key="": (seen.append(body), (None, no_stock))[1]
pod, err = runpod_api.create_pod("t", "image", key="k")
assert pod is None and f"{4 * len(gpus)}곳" in err and "모두 재고 없음" in err, err
assert no_stock not in err and len(seen) == 4 * len(gpus), (err, len(seen))
assert [body["gpu"]["id"] for body in seen[:len(gpus)]] == gpus

seen.clear()
def succeed_on_extra_gpu(method, path, timeout=0, body=None, key=""):
    seen.append(body)
    return ({"id": "later", "gpu": {"id": gpus[4]}}, None) if len(seen) == 5 else (None, no_stock)

runpod_api._rest = succeed_on_extra_gpu
pod, err = runpod_api.create_pod("t", "image", key="k")
assert err is None and pod["id"] == "later" and pod["gpu"] == gpus[4] and len(seen) == 5, (pod, err)

# 재고 외 400은 요약하지 않고, 422는 즉시 중단하며 원문을 보인다.
invalid = "RunPod가 거절했어요(HTTP 422): Request validation failed. gpu.id is invalid"
seen.clear()
runpod_api._rest = lambda method, path, timeout=0, body=None, key="": (seen.append(body), (None, invalid))[1]
pod, err = runpod_api.create_pod("t", "image", key="k")
assert pod is None and invalid in err and len(seen) == 1, (err, seen)

seen.clear()
runpod_api._rest = lambda method, path, timeout=0, body=None, key="": (seen.append(body), (None, no_stock if len(seen) == 1 else "RunPod가 거절했어요(HTTP 400): invalid GPU"))[1]
pod, err = runpod_api.create_pod("t", "image", key="k")
assert pod is None and "invalid GPU" in err and "모두 재고 없음" not in err, err

script = model_download.BOOTSTRAP_ENTRYPOINT[2]
assert "--max-time 60" in script and f"timeout {model_download.BOOTSTRAP_TIMEOUT_SEC} bash" in script and script.endswith("exec /start.sh")
if shutil.which("bash"):
    r = subprocess.run(["bash", "-n", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
print("ok")
