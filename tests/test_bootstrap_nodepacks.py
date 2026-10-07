"""파드 시작 스크립트의 노드팩 설치 검사(NS-34) — python tests/test_bootstrap_nodepacks.py"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

tmp = tempfile.mkdtemp()
os.environ.update(NIGHTSHIFT_DATA_DIR=tmp, NIGHTSHIFT_OUTPUT_DIR=tmp + "/out", NIGHTSHIFT_ASSETS_DIR=tmp + "/a",
                  NIGHTSHIFT_ADMIN_USER="admin", NIGHTSHIFT_ADMIN_PASSWORD="Test-Passw0rd-xyz!")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import app
import db
import model_download as md
import model_registry as reg

# 노드팩이 없으면 노드팩 조각이 없다
assert "nightshift_nodepack" not in md.install_script("p1")

reg.upsert("custom_nodes", "ComfyUI-Impact-Pack", {"download_url": "https://github.com/ltdrdata/ComfyUI-Impact-Pack"})
reg.upsert("custom_nodes", "ComfyUI-Impact-Subpack", {"download_url": "https://github.com/ltdrdata/ComfyUI-Impact-Subpack"})
reg.upsert("loras", "x.safetensors", {"download_url": "https://example.com/x.safetensors"})
# 등록부 검증을 거치지 않고 DB에 들어간 이상한 값도 스크립트에는 들어가지 않는다(github 이외·셸 문자)
with db.connect() as conn:
    for name, url in (("Evil", "https://gitlab.com/a/b"), ("Evil2", "https://github.com/a/b;rm -rf /"),
                      ("../up", "https://github.com/a/b"), ("Quote", "https://github.com/a/b'c")):
        conn.execute("INSERT INTO models(kind, filename, base_model, notes, tags_json, trigger_keyword, page_url, "
                     "download_url, updated_at) VALUES('custom_nodes',?,'','','[]','','',?,?)", (name, url, db.now_iso()))

script = md.install_script("p1")
assert "nightshift_nodepack ComfyUI-Impact-Pack https://github.com/ltdrdata/ComfyUI-Impact-Pack\n" in script
assert "nightshift_nodepack ComfyUI-Impact-Subpack https://github.com/ltdrdata/ComfyUI-Impact-Subpack\n" in script
assert script.count("\nnightshift_nodepack ") == 2, script
for bad in ("gitlab", "rm -rf /", "Evil", "../up", "b'c", "example.com"):
    assert bad not in script, bad
assert "git clone --depth 1" in script and '[ ! -d "$dir" ]' in script and ".nightshift_pip_ok" in script
assert '"$COMFY_DIR"/.venv*/bin/python' in script and "PY=python3" in script and '"$PY" -m pip install -r' in script

# 설치 상한 < 부팅 감시
entry = md.BOOTSTRAP_ENTRYPOINT[2]
assert f"timeout {md.BOOTSTRAP_TIMEOUT_SEC} bash" in entry
assert 2 * md.BOOTSTRAP_TIMEOUT_SEC + 60 < app.RUNPOD_BOOT_TIMEOUT_MIN * 60, app.RUNPOD_BOOT_TIMEOUT_MIN

# 실제로 돌려 본다 — 가짜 git·python으로: 처음엔 clone+pip, 두 번째엔 둘 다 건너뛴다
bash = shutil.which("bash")
if bash:
    root = Path(tempfile.mkdtemp())
    comfy, fake, log = root / "ComfyUI", root / "bin", root / "log.txt"
    (comfy / "custom_nodes").mkdir(parents=True)
    fake.mkdir()
    (fake / "git").write_text('#!/bin/bash\necho "git $*" >> "$NS_LOG"\nmkdir -p "${@: -1}"\n'
                              'echo numpy > "${@: -1}/requirements.txt"\n', newline="\n")
    (fake / "python3").write_text('#!/bin/bash\necho "pip $*" >> "$NS_LOG"\n', newline="\n")
    sh = root / "s.sh"
    sh.write_text(script, newline="\n", encoding="utf-8")
    # COMFY_DIR는 root 기준 상대 경로 — 절대 경로면 mkdir -p가 C:/Users/<이름>까지 거슬러 확인하다 Codex 샌드박스에서 거절된다.
    env = dict(os.environ, COMFY_DIR="ComfyUI", NS_LOG=log.as_posix())
    # Codex Windows 샌드박스는 PATH를 새로 짜서 Git Bash의 /usr/bin(mkdir·chmod·cygpath)이 빠진다 — 앞에 붙여 둔다.
    cmd = f'export PATH="/usr/bin:$PATH"; export PATH="$(cygpath -u "{fake.as_posix()}" 2>/dev/null || echo "{fake.as_posix()}"):$PATH"; ' \
          f'chmod +x "{fake.as_posix()}"/*; bash "{sh.as_posix()}"'
    for _ in range(2):
        r = subprocess.run([bash, "-c", cmd], capture_output=True, text=True, env=env, encoding="utf-8", cwd=root)
        assert r.returncode == 0, r.stdout + r.stderr
    lines = log.read_text().splitlines()
    assert len([x for x in lines if x.startswith("git clone --depth 1 ")]) == 2, lines
    assert len([x for x in lines if re.match(r"pip -m pip install -r .*requirements\.txt$", x)]) == 2, lines
    assert len(lines) == 4, lines
    assert (comfy / "custom_nodes" / "ComfyUI-Impact-Pack" / ".nightshift_pip_ok").exists()
    assert not list((comfy / "custom_nodes").glob("*.nstmp"))
print("ok")
