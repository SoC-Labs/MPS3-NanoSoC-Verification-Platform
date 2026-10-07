#!/usr/bin/env python3
"""stage0_board.py -- validate one board's baked identity and print its -D flags.

    stage0_board.py --label MPS3-02 [--ip 192.0.2.101] [--mac 02:00:00:00:02:FE]
      -> -DS0_LABEL_LO=0x3353504Du -DS0_LABEL_HI=0x0032302Du
         [-DS0_IP=0xC0A80B65u] [-DMPS3_MAC0=0x02 ... -DMPS3_MAC5=0xFE]

The stage0 Makefile runs this for S0_LABEL (always) and S0_IP / S0_MAC (only when
set, directly or by a boards/<name>.mk through S0_BOARD=<name>; a bake may pass the
IP and MAC as -DS0_IP / -DMPS3_MACn in S0_EXTRA_DEFS instead). stage0 publishes the
result at every entry in the status block (stage0_status.h "THE BOARD
IDENTITY"). The rules are the Linux
resolver's (mps3-identity, identity_core.c) so a value stage0 publishes is always
one Linux accepts:
  ip     a dotted quad, a usable host address (not 0.x, 127.x, multicast/reserved,
         and not .0/.255: Linux takes a stage0 address as a /24)
  mac    12 hex digits, bare or ':'/'-' separated; unicast (byte 0 bit 0 clear), not
         all zero
  label  1..8 characters of [A-Z0-9-] (the CLCD row-0 field; 8 = two status words)
Any violation: a message on stderr, exit 2, nothing on stdout (the Makefile
turns that into $(error)). Stdlib only.
"""
import argparse
import re
import sys

LABEL_MAX = 8


def parse_ip(s):
    m = re.fullmatch(r"(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})", s or "")
    if not m:
        raise ValueError("S0_IP %r is not a dotted quad" % s)
    o = [int(x) for x in m.groups()]
    if any(x > 255 for x in o):
        raise ValueError("S0_IP %r has an octet > 255" % s)
    if o[0] == 0 or o[0] == 127 or o[0] >= 224 or o[3] in (0, 255):
        raise ValueError("S0_IP %r is not a usable host address (Linux reads it as a /24)" % s)
    return (o[0] << 24) | (o[1] << 16) | (o[2] << 8) | o[3]


def parse_mac(s):
    h = re.sub(r"[:-]", "", s or "")
    if not re.fullmatch(r"[0-9A-Fa-f]{12}", h) or \
            (len(s) != 12 and not re.fullmatch(r"[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}", s)):
        raise ValueError("S0_MAC %r is not 12 hex digits (bare, or ':'/'-' separated)" % s)
    b = bytes.fromhex(h)
    if b[0] & 1:
        raise ValueError("S0_MAC %r is a multicast address (byte 0 bit 0 set)" % s)
    if not any(b):
        raise ValueError("S0_MAC %r is all zero" % s)
    return b


def parse_label(s):
    if not re.fullmatch(r"[A-Z0-9-]{1,%d}" % LABEL_MAX, s or ""):
        raise ValueError("S0_LABEL %r must be 1..%d characters of [A-Z0-9-]" % (s, LABEL_MAX))
    raw = s.encode("ascii").ljust(LABEL_MAX, b"\0")
    return int.from_bytes(raw[:4], "little"), int.from_bytes(raw[4:], "little")


def defs(ip, mac, label):
    lo, hi = parse_label(label)
    out = ["-DS0_LABEL_LO=0x%08Xu" % lo, "-DS0_LABEL_HI=0x%08Xu" % hi]
    if ip:
        out += ["-DS0_IP=0x%08Xu" % parse_ip(ip)]
    if mac:
        out += ["-DMPS3_MAC%d=0x%02X" % (i, v) for i, v in enumerate(parse_mac(mac))]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--label", required=True)
    ap.add_argument("--ip", default="")
    ap.add_argument("--mac", default="")
    a = ap.parse_args(argv)
    try:
        print(" ".join(defs(a.ip, a.mac, a.label)))
    except ValueError as e:
        print("stage0_board.py: %s" % e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
