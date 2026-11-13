"""Tailscale 기기 전용 DNS 응답기 — nightshift.lomebrote.com을 이 PC의 Tailscale 주소로 답한다(pm2 "tailnet-dns").

왜: 한국에서 nightshift.lomebrote.com에 붙으면 Cloudflare 미국 서부를 돌아와 이미지·영상이 느리다. Tailscale이 켜진
기기만 집 PC로 바로 오게 하려고, Tailscale 관리 화면의 DNS에서 이 도메인만(split DNS) 이 응답기로 보내게 한다.
그 기기의 브라우저는 같은 주소로 Tailscale 주소의 Caddy(tailnet.Caddyfile)에 붙는다. 다른 사람은 그대로 Cloudflare다.

- 이 PC의 Tailscale IPv4 주소(UDP 53)에만 묶는다 — 다른 네트워크에서는 닿지 않는다.
- 답하는 이름은 TAILNET_DNS_NAMES(쉼표, 기본 nightshift.lomebrote.com) 뿐이고 A 레코드만 준다. Caddy가 IPv4에만
  묶여 있으니 AAAA는 "없음"으로 답해 IPv6로 붙으려다 실패하지 않게 한다. 그 밖의 이름은 NXDOMAIN.
- 외부 라이브러리 없이 DNS 메시지를 직접 만든다(질문 하나짜리 단순 질의만 다룬다).
"""

import os
import socket
import struct
import subprocess
import sys
import time

NAMES = {n.strip().lower().rstrip(".") for n in os.environ.get("TAILNET_DNS_NAMES", "nightshift.lomebrote.com").split(",") if n.strip()}
TTL = 60
TYPE_A, TYPE_AAAA, CLASS_IN = 1, 28, 1


def tailscale_ipv4() -> str:
    explicit = os.environ.get("TAILNET_IP", "").strip()
    if explicit:
        return explicit
    exe = os.environ.get("TAILSCALE_EXE", r"C:\Program Files\Tailscale\tailscale.exe")
    out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=10)
    ip = out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else ""
    if not ip:
        raise RuntimeError("Tailscale IPv4 주소를 못 읽었어요(Tailscale이 꺼져 있거나 로그아웃 상태)")
    return ip


def parse_question(data: bytes) -> tuple[int, int, str, int, int, int] | None:
    """(id, flags, 이름, 종류, 클래스, 질문 끝 위치) — 질문이 하나가 아니거나 형식이 이상하면 None."""
    if len(data) < 12:
        return None
    qid, flags, qdcount = struct.unpack(">HHH", data[:6])
    if qdcount != 1 or flags & 0x8000:   # 질문이 하나인 질의만
        return None
    labels, pos = [], 12
    while True:
        if pos >= len(data):
            return None
        length = data[pos]
        if length == 0:
            pos += 1
            break
        if length & 0xC0 or pos + 1 + length > len(data):   # 질문에 압축 포인터는 안 온다
            return None
        labels.append(data[pos + 1:pos + 1 + length].decode("ascii", "replace"))
        pos += 1 + length
    if pos + 4 > len(data):
        return None
    qtype, qclass = struct.unpack(">HH", data[pos:pos + 4])
    return qid, flags, ".".join(labels).lower(), qtype, qclass, pos + 4


def answer(data: bytes, ip: str) -> bytes | None:
    q = parse_question(data)
    if q is None:
        return None
    qid, flags, name, qtype, qclass, end = q
    question = data[12:end]
    rd = flags & 0x0100
    known = name in NAMES
    rcode = 0 if known else 3   # NXDOMAIN
    answers = b""
    if known and qtype == TYPE_A and qclass == CLASS_IN:
        # 0xC00C = 질문의 이름(헤더 바로 뒤 12바이트 위치)을 가리키는 압축 포인터
        answers = struct.pack(">HHHIH", 0xC00C, TYPE_A, CLASS_IN, TTL, 4) + socket.inet_aton(ip)
    # QR=1, AA=1, RD는 질의 그대로, RA=0
    resp_flags = 0x8000 | 0x0400 | rd | rcode
    header = struct.pack(">HHHHHH", qid, resp_flags, 1, 1 if answers else 0, 0, 0)
    return header + question + answers


def main() -> None:
    ip = tailscale_ipv4()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((ip, 53))
    print(f"[tailnet-dns] {ip}:53 에서 {sorted(NAMES)} → {ip}", flush=True)
    while True:
        try:
            data, addr = sock.recvfrom(512)
        except ConnectionResetError:
            continue   # Windows는 앞서 보낸 응답의 ICMP 거절을 다음 recvfrom에서 이 예외로 알린다
        resp = answer(data, ip)
        if resp is not None:
            sock.sendto(resp, addr)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:   # pm2가 다시 띄운다 — Tailscale이 부팅 직후 아직 안 떴을 때 등
        print(f"[tailnet-dns] 시작 실패: {e}", file=sys.stderr, flush=True)
        time.sleep(5)
        sys.exit(1)
