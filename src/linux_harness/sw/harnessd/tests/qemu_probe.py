#!/usr/bin/env python3
"""qemu_probe.py — the wire checks qemu_smoke.sh runs against a harnessd inside
QEMU (through slirp hostfwd at --offset + port). Prints one PASS/FAIL line per
check, exits non-zero on any FAIL. Stdlib + pyverify's pusher only."""
from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[4] / "host" / "pyverify"))
from harnessd_mock import TAP_IDCODE, clearing_payload, frame, partial_payload  # noqa: E402
from pyverify import pusher  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    fails += 0 if cond else 1


def ctl(port):
    s = socket.create_connection(("127.0.0.1", port), timeout=30)
    return s, s.makefile("rb")


def req(port, obj):
    for _ in range(60):
        try:
            s, f = ctl(port)
            s.sendall(json.dumps(obj).encode() + b"\n")
            line = f.readline()
            f.close()
            s.close()
            if line:
                return json.loads(line)
        except OSError:
            pass
        time.sleep(0.3)
    raise ConnectionError("6900 never answered")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offset", type=int, required=True)
    ap.add_argument("--static-id", type=lambda v: int(v, 0), required=True)
    a = ap.parse_args()
    P = lambda p: a.offset + p  # noqa: E731
    sid = a.static_id

    ping = req(P(6900), {"op": "ping"})
    check("6900 ping", ping.get("shell_id") == f"0x{sid:08x}", json.dumps(ping))
    ver = req(P(6900), {"op": "version"})
    check("6900 version impl=linux lmb_kb=128", ver.get("impl") == "linux" and ver.get("lmb_kb") == 128,
          json.dumps(ver))
    st = req(P(6900), {"op": "stats"})
    check("6900 stats has os_up_ms", "os_up_ms" in st and st.get("link") is True, f"link={st.get('link')}")

    # 6921: OpenOCD remote_bitbang IDCODE read through the mock TAP
    s = socket.create_connection(("127.0.0.1", P(6921)), timeout=30)
    out = b""
    def clk(tms, read=False):
        nonlocal out
        out += bytes([ord("0") + (tms << 1)])
        if read:
            out += b"R"
        out += bytes([ord("0") + (4 | tms << 1)])
    for _ in range(5):
        clk(1)
    for tms in (0, 1, 0, 0):
        clk(tms)
    for i in range(32):
        clk(1 if i == 31 else 0, True)
    s.sendall(out)
    got = b""
    while len(got) < 32:
        got += s.recv(64)
    s.close()
    idc = sum((1 if c == ord("1") else 0) << i for i, c in enumerate(got[:32]))
    check("6921 remote_bitbang IDCODE", idc == TAP_IDCODE, hex(idc))

    # 2542: XVC getinfo
    s = socket.create_connection(("127.0.0.1", P(2542)), timeout=30)
    s.sendall(b"getinfo:")
    info = s.recv(64)
    s.close()
    check("2542 XVC getinfo", info.startswith(b"xvcServer_v1.0:"), repr(info))

    # 6910: a swap with a plain push of the pair
    s, f = ctl(P(6900))
    s.sendall(b'{"op":"swap","rm":"qemu","src":"tcp"}\n')
    time.sleep(1.0)
    pusher.tcp_send(frame(clearing_payload(64), kind=0, static_id=sid, rm_id=0x77),
                    "127.0.0.1", P(6910), timeout_s=30)
    time.sleep(0.5)
    pusher.tcp_send(frame(partial_payload(0x77, 4096), kind=1, static_id=sid, rm_id=0x77),
                    "127.0.0.1", P(6910), timeout_s=30)
    line = f.readline()
    f.close()          # the makefile holds the fd open too: close BOTH, or the
    s.close()          # single-client 6900 keeps refusing everyone after us
    check("6910 plain push -> swap verified", json.loads(line) == {"ok": True, "rm_id": "0x00000077",
                                                                   "verified": True}, line.decode().strip())

    # 6899: identify
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.settimeout(10)
    u.sendto(b'{"op":"identify","v":1,"nonce":"0badc0de0badc0de"}', ("127.0.0.1", P(6899)))
    try:
        r = json.loads(u.recv(2048))
        check("6899 identify", r.get("nonce") == "0badc0de0badc0de" and r.get("impl") == "linux",
              f"ip={r.get('ip')} rm_id={r.get('rm_id')}")
    except socket.timeout:
        check("6899 identify", False, "no reply")
    try:
        d = req(P(6900), {"op": "diag"})
        check("diag: the table runs (pass/service worst, us)", d.get("svc_count") == 12,
              f"pass_max_us={d.get('pass_max_us')} svc_max_us={d.get('svc_max_us')} "
              f"ix={d.get('svc_max_ix')}")
    except ConnectionError as exc:
        check("diag after the swap", False, str(exc))
    print("RESULT:", "PASS" if fails == 0 else f"{fails} FAIL")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
