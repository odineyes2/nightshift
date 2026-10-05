# RunPod 파드 만들기 요청과 부팅 스크립트 검사(NS-18). 실행: python tests/test_runpod_create.py
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import model_download  # noqa: E402
import runpod_api  # noqa: E402

seen = []
runpod_api._rest = lambda method, path, timeout=0, body=None: (seen.append(body), ({"id": "x"}, None))[1]
pod, err = runpod_api.create_pod("t", "image", entrypoint=model_download.BOOTSTRAP_ENTRYPOINT)
assert err is None and pod["id"] == "x", err
assert seen[0]["gpu"]["allowedCudaVersions"] == ["13.0"], seen[0]
assert seen[0]["args"] == {"entrypoint": model_download.BOOTSTRAP_ENTRYPOINT} and "mounts" not in seen[0], seen[0]

script = model_download.BOOTSTRAP_ENTRYPOINT[2]
assert "--max-time 60" in script and "timeout 120 bash" in script and script.endswith("exec /start.sh")
if shutil.which("bash"):
    r = subprocess.run(["bash", "-n", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
print("ok")
