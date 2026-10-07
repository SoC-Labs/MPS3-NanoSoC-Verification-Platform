"""test_identity_tool.py -- the BOARD identity at boot (lane IDENT, 2026-09-28):
the resolver/CLI `mps3-identity` and its consumer S41mps3net, board-free.

  mps3-identity  (identity_core.h is the model; the host-uio build, the SAME
                  sources the rv32 image carries)
    - precedence, per field: each source ALONE (default / stage0 / override), MIXED,
      a CORRUPT override (ignored line by line, logged), the stage0 block INVALID
      or with its identity words ZERO (an older stage0), and an override on a
      NON-persistent /persist (netboot, mps3.persist=off) = absent;
    - the stage0 block read through the REAL UIO path (hal_uio.c over a fake sysfs
      + a plain file standing in for /dev/uioN, as test_hal_uio.c does);
    - set / clear / get: validation (rc 2, the field named), no persistent
      /persist (rc 3), the atomic write (no temp file left), `key=` drops a key.

  S41mps3net     run for real under /bin/sh against a fake sysfs and stub `ip` /
                 `arping` / `udhcpc` / `hostname`: the identity's MAC is set BEFORE
                 `ip link set eth0 up`, the static address is the identity's, the
                 hostname is set. NEGATIVE CONTROL: the same script with the
                 set_mac call moved after the link-up must FAIL the order check
                 (and it leaves eth0 down: set_mac takes the link down to change
                 the MAC; the stub `ip` refuses a MAC change on a running
                 interface, as some drivers do).
"""
from __future__ import annotations

import json
import os
import re
import stat
import struct
import subprocess
import time
from pathlib import Path
from typing import Optional

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
TOOL = os.environ.get("MPS3_IDENTITY_BIN", str(HERE.parent / "build" / "host-uio" / "mps3-identity"))
OVERLAY = REPO / "src" / "linux_harness" / "sw" / "br2_external" / "rootfs_overlay"
S41 = OVERLAY / "etc" / "init.d" / "S41mps3net"
NETCONF = OVERLAY / "etc" / "mps3" / "net.conf"

pytestmark = pytest.mark.skipif(not Path(TOOL).exists(), reason=f"{TOOL} not built (make host)")

S0_MAGIC = 0x54533053
OFF = {"ip_addr": 0xB0, "mac_lo": 0xB4, "mac_hi": 0xB8, "label_lo": 0xEC, "label_hi": 0xF0}


def s0_block(ip=0, mac=None, label=None, *, magic=S0_MAGIC, version=1) -> bytes:
    """A stage0 status block (stage0_status.h): 256 bytes, the identity words set."""
    b = bytearray(256)
    struct.pack_into("<III", b, 0, magic, version, 0x100)
    struct.pack_into("<I", b, 0xFC, S0_MAGIC)
    struct.pack_into("<I", b, OFF["ip_addr"], ip)
    if mac:
        m = bytes.fromhex(mac.replace(":", ""))
        b[OFF["mac_lo"]:OFF["mac_lo"] + 4] = m[:4]
        b[OFF["mac_hi"]:OFF["mac_hi"] + 2] = m[4:]
    if label:
        b[OFF["label_lo"]:OFF["label_lo"] + 8] = label.encode().ljust(8, b"\0")
    return bytes(b)


B2 = s0_block(0xC0A80B65, "02:00:00:00:02:fe", "MPS3-02")


class Env:
    def __init__(self, tmp: Path, persist=True):
        tmp.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp
        self.ovr = tmp / "persist" / "etc" / "mps3" / "identity"
        self.run = tmp / "run" / "identity"
        self.pst = tmp / "persist.state"
        self.pst.write_text("backing=card dev=/dev/mmcblk0p3 storage=ok\n" if persist
                            else "backing=tmpfs reason=cmdline storage=ok\n")
        self.s0 = tmp / "s0.bin"

    def tool(self, *args, s0: Optional[bytes] = None):
        if s0 is None:
            sf = ["--status-file", "none"]
        else:
            self.s0.write_bytes(s0)
            sf = ["--status-file", str(self.s0)]
        return subprocess.run([TOOL, "--override", str(self.ovr), "--run", str(self.run),
                               "--persist-state", str(self.pst), *sf, *args],
                              capture_output=True, text=True, timeout=20)

    def resolved(self) -> dict:
        return dict(re.findall(r"^(MPS3_\w+)=(.*)$", self.run.read_text(), re.M))


def test_each_source_alone(tmp_path):
    e = Env(tmp_path)
    r = e.tool("resolve")
    assert r.returncode == 0, r.stderr
    d = e.resolved()
    assert (d["MPS3_LABEL"], d["MPS3_HOSTNAME"], d["MPS3_IP"], d["MPS3_MAC"]) == \
        ("MPS3", "mps3", "192.168.10.101/24", "02:00:00:4d:50:53")
    assert (d["MPS3_LABEL_SRC"], d["MPS3_IP_SRC"], d["MPS3_MAC_SRC"], d["MPS3_HOSTNAME_SRC"]) == \
        ("default", "default", "default", "label")
    assert d["MPS3_STAGE0"] == "nowindow"

    assert e.tool("resolve", s0=B2).returncode == 0
    d = e.resolved()
    assert (d["MPS3_LABEL"], d["MPS3_HOSTNAME"], d["MPS3_IP"], d["MPS3_MAC"]) == \
        ("MPS3-02", "mps3-02", "192.168.11.101/24", "02:00:00:00:02:fe")
    assert {d["MPS3_LABEL_SRC"], d["MPS3_IP_SRC"], d["MPS3_MAC_SRC"]} == {"stage0"}

    e.ovr.parent.mkdir(parents=True)
    e.ovr.write_text("MPS3_LABEL=BENCH-1\nMPS3_HOSTNAME=bench1\nMPS3_IP=10.4.0.9/16\n"
                     "MPS3_MAC=02:de:ad:be:ef:01\n")
    assert e.tool("resolve", s0=B2).returncode == 0
    d = e.resolved()
    assert (d["MPS3_LABEL"], d["MPS3_HOSTNAME"], d["MPS3_IP"], d["MPS3_MAC"]) == \
        ("BENCH-1", "bench1", "10.4.0.9/16", "02:de:ad:be:ef:01")
    assert {d[k] for k in ("MPS3_LABEL_SRC", "MPS3_HOSTNAME_SRC", "MPS3_IP_SRC", "MPS3_MAC_SRC")} \
        == {"override"}


def test_mixed_per_field(tmp_path):
    e = Env(tmp_path)
    e.ovr.parent.mkdir(parents=True)
    e.ovr.write_text("MPS3_IP=192.168.50.7/24\n")
    assert e.tool("resolve", s0=s0_block(0, None, "LAB-3")).returncode == 0
    d = e.resolved()
    assert (d["MPS3_LABEL"], d["MPS3_LABEL_SRC"]) == ("LAB-3", "stage0")
    assert (d["MPS3_IP"], d["MPS3_IP_SRC"]) == ("192.168.50.7/24", "override")
    assert (d["MPS3_MAC"], d["MPS3_MAC_SRC"]) == ("02:00:00:4d:50:53", "default")
    assert (d["MPS3_HOSTNAME"], d["MPS3_HOSTNAME_SRC"]) == ("lab-3", "label")


def test_corrupt_override_is_ignored_with_a_log(tmp_path):
    e = Env(tmp_path)
    e.ovr.parent.mkdir(parents=True)
    e.ovr.write_bytes(b"\x7fELF\x00\x01garbage\nMPS3_MAC=ff:ff:ff:ff:ff:ff\n"
                      b"MPS3_IP=192.168.11.255/24\nMPS3_HOSTNAME=ok-host\n")
    r = e.tool("resolve", s0=B2)
    assert r.returncode == 0
    assert "line 1" in r.stderr and "MPS3_MAC" in r.stderr and "MPS3_IP" in r.stderr, r.stderr
    d = e.resolved()
    assert (d["MPS3_MAC"], d["MPS3_MAC_SRC"]) == ("02:00:00:00:02:fe", "stage0")
    assert (d["MPS3_IP"], d["MPS3_IP_SRC"]) == ("192.168.11.101/24", "stage0")
    assert (d["MPS3_HOSTNAME"], d["MPS3_HOSTNAME_SRC"]) == ("ok-host", "override")


@pytest.mark.parametrize("blk,state", [
    (s0_block(0xC0A80B65, "02:00:00:00:02:fe", "MPS3-02", magic=0x12345678), "invalid"),
    (s0_block(0, None, None), "valid"),                              # a pre-IDENT stage0
    (s0_block(0x7F000001, "01:00:5e:00:00:01", "lower"), "valid"),   # values the rules refuse
])
def test_stage0_invalid_or_zero_falls_back(tmp_path, blk, state):
    e = Env(tmp_path)
    r = e.tool("resolve", s0=blk)
    assert r.returncode == 0
    d = e.resolved()
    assert d["MPS3_STAGE0"] == state
    assert (d["MPS3_LABEL"], d["MPS3_IP"], d["MPS3_MAC"]) == \
        ("MPS3", "192.168.10.101/24", "02:00:00:4d:50:53")
    if b"lower" in blk:
        assert r.stderr.count("refused") == 3, r.stderr


def test_override_on_a_volatile_persist_is_absent(tmp_path):
    """netboot / mps3.persist=off: /persist is a tmpfs, so an override there is not
    this board's (and would not survive): it is not read, and `set` refuses."""
    e = Env(tmp_path, persist=False)
    e.ovr.parent.mkdir(parents=True)
    e.ovr.write_text("MPS3_LABEL=GHOST\n")
    assert e.tool("resolve", s0=B2).returncode == 0
    d = e.resolved()
    assert (d["MPS3_LABEL"], d["MPS3_LABEL_SRC"], d["MPS3_PERSIST"]) == ("MPS3-02", "stage0", "0")
    r = e.tool("set", "label=X")
    assert r.returncode == 3 and "persist" in r.stderr


def test_stage0_through_the_uio_window(tmp_path):
    """The rv32 image reads the block through hal_uio.c, exactly as harnessd does:
    a fake /sys/class/uio + a file as /dev/uio3 whose map0 is the LMB tail 0x1F000
    (the block at +0xE00)."""
    e = Env(tmp_path)
    sysfs, dev = tmp_path / "sys", tmp_path / "dev"
    m = sysfs / "uio3" / "maps" / "map0"
    m.mkdir(parents=True)
    (m / "addr").write_text("0x1f000\n")
    (m / "size").write_text("0x1000\n")
    dev.mkdir()
    page = bytearray(4096)
    page[0xE00:0xF00] = B2
    (dev / "uio3").write_bytes(bytes(page))
    base = [TOOL, "--override", str(e.ovr), "--run", str(e.run), "--persist-state", str(e.pst)]
    r = subprocess.run(base + ["--uio-sysfs", str(sysfs), "--uio-devdir", str(dev), "resolve"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    d = e.resolved()
    assert (d["MPS3_LABEL"], d["MPS3_LABEL_SRC"], d["MPS3_STAGE0"]) == ("MPS3-02", "stage0", "valid")
    # no UIO at all (QEMU -M virt): the defaults, and still a complete run file
    r = subprocess.run(base + ["--uio-sysfs", str(tmp_path / "none"), "resolve"],
                       capture_output=True, text=True)
    assert r.returncode == 0 and e.resolved()["MPS3_STAGE0"] == "nowindow"
    assert e.resolved()["MPS3_LABEL"] == "MPS3"


def test_set_clear_get(tmp_path):
    e = Env(tmp_path)
    assert e.tool("resolve", s0=B2).returncode == 0
    for bad, field in (("label=mps3", "label"), ("mac=01:00:00:00:00:00", "mac"),
                       ("ip=192.168.11.0/24", "ip"), ("ip=10.0.0.1/31", "ip"),
                       ("hostname=-bad", "hostname"), ("colour=red", "colour")):
        r = e.tool("set", "hostname=fine", bad, s0=B2)
        assert r.returncode == 2 and f"invalid {field}" in r.stderr, (bad, r.stderr)
        assert not e.ovr.exists(), "a refused set writes NOTHING (not even the valid key)"
    r = e.tool("set", "label=BENCH-2", "ip=192.168.11.7", s0=B2)
    assert r.returncode == 0, r.stderr
    assert '"label":"BENCH-2"' in r.stdout and '"ip":"192.168.11.7/24"' in r.stdout
    assert e.ovr.read_text().count("MPS3_") == 2
    assert [p.name for p in e.ovr.parent.iterdir()] == ["identity"], "no temp file left behind"
    r = e.tool("get", "--json", s0=B2)
    assert r.returncode == 0
    j = json.loads(r.stdout)
    assert j["label"] == "MPS3-02" and j["source"]["label"] == "stage0"      # this boot
    assert j["override"] == {"label": "BENCH-2", "ip": "192.168.11.7/24"}
    assert j["pending"] == {"label": "BENCH-2", "hostname": "bench-2", "ip": "192.168.11.7/24"}
    assert j["stage0"] == {"label": "MPS3-02", "ip": "192.168.11.101/24", "mac": "0200000002fe"}
    assert j["persist"] is True
    assert e.tool("set", "ip=", s0=B2).returncode == 0            # drop one key
    assert "MPS3_IP" not in e.ovr.read_text() and "MPS3_LABEL=BENCH-2" in e.ovr.read_text()
    assert e.tool("clear", s0=B2).returncode == 0 and not e.ovr.exists()
    assert e.tool("clear", s0=B2).returncode == 0, "clearing nothing is fine"


# --------------------------------------------------------------------------- #
# S41mps3net: the MAC before link up
# --------------------------------------------------------------------------- #

STUBS = {
    # models a driver that refuses a MAC change on a RUNNING interface (smsc911x
    # generation <= 1, and others): the negative control's second failure
    "ip": r'''#!/bin/sh
echo "ip $*" >> "$CALLS"
case "$*" in
  "link set dev eth0 address "*)
      [ "$(cat "$SYS/eth0/operstate")" = up ] && { echo "RTNETLINK answers: Device or resource busy" >&2; exit 2; }
      for a; do last=$a; done; echo "$last" > "$SYS/eth0/address" ;;
  "link set dev eth0 down") echo down > "$SYS/eth0/operstate" ;;
  "link set eth0 up") echo up > "$SYS/eth0/operstate"; echo 1 > "$SYS/eth0/carrier" ;;
  "-4 addr show dev eth0") cat "$SYS/eth0/addrs" 2>/dev/null ;;
  "addr add "*) echo "    inet $3 scope global eth0" >> "$SYS/eth0/addrs" ;;
esac
exit 0
''',
    "arping": '#!/bin/sh\necho "arping $*" >> "$CALLS"\nexit 0\n',
    "udhcpc": '#!/bin/sh\necho "udhcpc $*" >> "$CALLS"\nexit 0\n',
    "sysctl": '#!/bin/sh\nexit 0\n',
    "hostname": '#!/bin/sh\necho "hostname $*" >> "$CALLS"\nexit 0\n',
    "sleep": '#!/bin/sh\nexit 0\n',
}


def run_s41(tmp: Path, script_text: str, identity: Optional[str], cmdline="console=ttyUL0"):
    tmp.mkdir(parents=True, exist_ok=True)
    stubs = tmp / "stubs"
    stubs.mkdir()
    for name, body in STUBS.items():
        p = stubs / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    sysn = tmp / "sys"
    (sysn / "eth0").mkdir(parents=True)
    (sysn / "eth0" / "address").write_text("02:00:00:4d:50:53\n")   # the DTB's MAC
    (sysn / "eth0" / "carrier").write_text("0\n")
    (sysn / "eth0" / "operstate").write_text("down\n")
    run = tmp / "run"
    run.mkdir()
    if identity is not None:
        (run / "identity").write_text(identity)
    (tmp / "cmdline").write_text(cmdline + "\n")
    script = tmp / "S41mps3net"
    script.write_text(script_text)
    env = dict(os.environ, PATH=f"{stubs}:{os.environ['PATH']}", CALLS=str(tmp / "calls"),
               SYS=str(sysn), MPS3_T_NETCONF=str(NETCONF), MPS3_T_RUN=str(run),
               MPS3_T_SYSNET=str(sysn), MPS3_T_CMDLINE=str(tmp / "cmdline"))
    subprocess.run(["/bin/sh", str(script), "start"], env=env, check=True, timeout=20,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while time.time() < deadline:
        if (run / "net.state").exists() and "settled=1" in (run / "net.state").read_text():
            break
        time.sleep(0.05)
    calls = (tmp / "calls").read_text().splitlines() if (tmp / "calls").exists() else []
    state = dict(re.findall(r"^(\w+)=(.*)$", (run / "net.state").read_text(), re.M))
    return calls, state, (sysn / "eth0" / "address").read_text().strip()


def mac_before_up(calls, mac) -> bool:
    """THE ORDER CHECK: the identity's MAC went on eth0 before the link came up."""
    try:
        i_mac = calls.index(f"ip link set dev eth0 address {mac}")
        i_up = calls.index("ip link set eth0 up")
    except ValueError:
        return False
    return i_mac < i_up


def board2_identity(tmp: Path) -> str:
    e = Env(tmp / "id")
    assert e.tool("resolve", s0=B2).returncode == 0
    return e.run.read_text()


def test_s41_sets_the_mac_before_link_up(tmp_path):
    calls, state, addr = run_s41(tmp_path / "s41", S41.read_text(), board2_identity(tmp_path))
    assert mac_before_up(calls, "02:00:00:00:02:fe"), calls
    assert addr == "02:00:00:00:02:fe" and state["mac_set"] == "ok" and state["mac"] == addr
    assert "hostname mps3-02" in calls
    assert "ip addr add 192.168.11.101/24 dev eth0" in calls, calls
    assert state["static"] == "192.168.11.101/24" and state["static101"] == "added"
    assert not any("192.168.10.101" in c for c in calls), "the old hard-coded address is gone"
    assert state["dhcp"] == "0" and state["settled"] == "1", "DHCP-first and net.state kept"
    assert any(c.startswith("udhcpc ") and "-x hostname:" in c for c in calls)


def test_s41_negative_control_mac_after_link_up_fails(tmp_path):
    """The order check must be able to fail: move set_mac after the link-up."""
    text = S41.read_text()
    good = "\n    set_mac\n    ip link set $IF up\n"
    assert text.count(good) == 1
    bad = text.replace(good, "\n    ip link set $IF up\n    set_mac\n")
    calls, state, addr = run_s41(tmp_path / "s41", bad, board2_identity(tmp_path))
    assert not mac_before_up(calls, "02:00:00:00:02:fe"), "NEGATIVE CONTROL passed the order check"
    # and what it would cost: set_mac takes the link DOWN to change the MAC, so after
    # the link-up it bounces the link -- and leaves eth0 down (no one brings it back)
    assert calls.index("ip link set dev eth0 down") > calls.index("ip link set eth0 up")
    assert (tmp_path / "s41" / "sys" / "eth0" / "operstate").read_text().strip() == "down"


def test_s41_without_identity_and_in_dhcp_mode(tmp_path):
    calls, state, addr = run_s41(tmp_path / "a", S41.read_text(), None)
    assert state["mac_set"] == "none" and addr == "02:00:00:4d:50:53"
    assert "ip addr add 192.168.10.101/24 dev eth0" in calls, "no identity file: the image default"
    calls, state, addr = run_s41(tmp_path / "b", S41.read_text(), board2_identity(tmp_path / "b"),
                                 cmdline="console=ttyUL0 mps3.net=dhcp")
    assert state["static101"] == "off" and not any(c.startswith("ip addr add") for c in calls)
    assert mac_before_up(calls, "02:00:00:00:02:fe") and state["mac_set"] == "ok", \
        "the MAC is the board's in every mode"
