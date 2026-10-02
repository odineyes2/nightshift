"""축소본 디스크 캐시(NS-23-1) 검사 — 적중·회전 무효화·크기 단계·키우지 않음·삭제 정리. 실행: python tests/test_thumbnail_cache.py"""
import asyncio
import io
import os
import sys
import tempfile
from pathlib import Path

tmp = tempfile.mkdtemp()
for k in ("DATA", "OUTPUT", "ASSETS"):
    os.environ[f"NIGHTSHIFT_{k}_DIR"] = os.path.join(tmp, k.lower())
os.environ.setdefault("NIGHTSHIFT_ADMIN_USER", "admin")
os.environ.setdefault("NIGHTSHIFT_ADMIN_PASSWORD", "Test-Passw0rd-xyz!")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
os.chdir(Path(__file__).resolve().parent.parent / "server")

import app as A  # noqa: E402
from PIL import Image  # noqa: E402

A.auth.current_user.set({"id": 1, "role": A.auth.ROLE_ADMIN})
out = Path(A.OUTPUT_DIR)
(out / "job1").mkdir(parents=True, exist_ok=True)
src = out / "job1" / "big.png"
Image.new("RGB", (4000, 6000), (200, 30, 30)).save(src)


class Req:
    headers = {}


def thumb(size, v=""):
    r = A.get_output_image_thumbnail("job1/big.png", Req(), size=size, fit="inside", v=v)
    body = r.body if hasattr(r, "body") else Path(r.path).read_bytes()
    return r, body


def cache_files():
    return sorted(A.THUMB_CACHE_DIR.glob("*.jpg"))


# (a) 첫 요청에서 캐시 파일이 생기고, 두 번째는 디코딩 없이 같은 바이트
r1, b1 = thumb(400)
assert len(cache_files()) == 1, cache_files()
real_open = A.Image.open
A.Image.open = lambda *a, **k: (_ for _ in ()).throw(AssertionError("디코딩하면 안 됨"))
r2, b2 = thumb(400)
A.Image.open = real_open
assert isinstance(r2, A.FileResponse) and b1 == b2
assert r1.headers["cache-control"] == "private, max-age=86400"
r3, _ = thumb(400, v="123")
assert "immutable" in r3.headers["cache-control"]

# (b) 회전하면 새 키로 다시 만들어지고 옛 키 파일은 사라진다
old = cache_files()
st = src.stat()
asyncio.run(A.rotate_output_image("job1/big.png"))
os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))  # 같은 초 안에 돌려도 mtime이 바뀌게
_, b3 = thumb(400)
assert cache_files() != old and len(cache_files()) == 1
assert Image.open(io.BytesIO(b3)).size == (400, 267), Image.open(io.BytesIO(b3)).size

# (c) size=1000은 1280 단계로 올라간다
_, b = thumb(1000)
assert max(Image.open(io.BytesIO(b)).size) == 1280
assert A.thumb_size_step(60) == 80 and A.thumb_size_step(9999) == 2048

# (d) 원본보다 큰 요청은 원본 크기를 넘지 않는다
small = out / "job1" / "small.png"
Image.new("RGB", (300, 200)).save(small)
r = A.get_output_image_thumbnail("job1/small.png", Req(), size=2048)
assert Image.open(io.BytesIO(r.body)).size == (300, 200)

# (e) 이미지를 지우면 그 이미지의 캐시 파일도 지워진다
assert len(cache_files()) == 3
A.delete_output_image("job1/big.png")
assert len(cache_files()) == 1  # small.png 것만 남음

# 정리: 상한을 넘으면 오래된 것부터 지운다
A.prune_thumb_cache(0)
assert cache_files() == []

print("ok")
