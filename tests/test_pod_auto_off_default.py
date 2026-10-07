"""워커 유휴 자동 끄기 기본값(NS-47) — 값이 없으면 켜고, 명시적으로 끈 false는 그대로 둔다."""
import os
import sys
import tempfile
from pathlib import Path

tmp = tempfile.mkdtemp()
os.environ["NIGHTSHIFT_DATA_DIR"] = tmp
os.environ["NIGHTSHIFT_PODS_FILE"] = os.path.join(tmp, "pods.json")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

import pod_registry  # noqa: E402

pod_registry.load()

assert pod_registry.normalize_pod({"name": "a", "url": "https://a.example"})["auto_power_off"] is True
assert pod_registry.normalize_pod({"name": "a", "url": "https://a.example", "auto_power_off": False})["auto_power_off"] is False

on = pod_registry.create_pod({"name": "on", "url": "https://on.example"})
assert on["auto_power_off"] is True

off = pod_registry.create_pod({"name": "off", "url": "https://off.example", "auto_power_off": False})
updated = pod_registry.update_pod(off["id"], {"note": "다른 필드만"})
assert updated["auto_power_off"] is False and updated["note"] == "다른 필드만"

# 저장한 뒤 다시 읽어도 false가 남는다.
pod_registry.load()
assert pod_registry.get_pod(off["id"])["auto_power_off"] is False
assert pod_registry.get_pod(on["id"])["auto_power_off"] is True

print("ok")
