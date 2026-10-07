"""test_mps3_debug.py -- the on-board GDB server launcher (mps3-debug, contract
"mps3-debug/1") on the build host: the REAL launcher (src/mps3_debug.c, host gcc)
supervising fake_openocd.py, against a fake jtag_server on a loopback port (single
client, like harnessd's 6921: a second connection is reset) and a fake identify
responder (UDP, the harness's 6899). No board, no Buildroot, seconds.

    python3 -m pytest -q src/linux_harness/sw/br2_external/package/mps3-debug/tests

Every refusal the contract names has a case: busy (a live 6921 holder, found in
/proc/net/tcp; and a 6921 that resets OpenOCD), no_openocd (12), no_dap (13), no_cfg
(14: an unknown rm_id, and a silent identify), plus up/already/status/down, a swap
(target_lost), the idle timeout, a crash, the lock, and the argv order.
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
SRC = PKG / "src" / "mps3_debug.c"
REPO = PKG.parents[4]
CFG_DIR = REPO / "host" / "openocd"
IDCODE = 0x6BA00477

pytestmark = pytest.mark.skipif(shutil.which(os.environ.get("CC", "gcc")) is None,
                                reason="no host C compiler")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="session")
def launcher(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("bin") / "mps3-debug"
    cc = os.environ.get("CC", "gcc")
    subprocess.run([cc, "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
                    "-DMPS3_DEBUG_VERSION=\"1.0.0\"", "-o", str(out), str(SRC)], check=True)
    return out


class FakeJtag:
    """harnessd's 6921 in miniature: ONE client; a newcomer while one is live is reset
    (SO_LINGER 0 -> RST, the 'Connection reset by peer' OpenOCD logs); 'R' -> the next
    IDCODE bit, LSB first. mode="reset_all" resets every connection."""

    def __init__(self, mode: str = "normal"):
        self.port = free_port()
        self.mode = mode
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", self.port))
        self.srv.listen(4)
        self.client = None
        self.stop = False
        self.t = threading.Thread(target=self.run, daemon=True)
        self.t.start()

    @staticmethod
    def _reset(c):
        c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        c.close()

    def run(self):
        import select
        bit = 0
        while not self.stop:
            rl = [self.srv] + ([self.client] if self.client else [])
            r, _, _ = select.select(rl, [], [], 0.05)
            for s in r:
                if s is self.srv:
                    c, _ = self.srv.accept()
                    if self.mode == "reset_all" or self.client is not None:
                        time.sleep(0.05)
                        self._reset(c)
                    else:
                        self.client, bit = c, 0
                else:
                    try:
                        data = s.recv(4096)
                    except OSError:
                        data = b""
                    if not data:
                        s.close()
                        self.client = None
                        continue
                    out = b""
                    for ch in data:
                        if ch == ord("R"):
                            out += b"1" if (IDCODE >> (bit % 32)) & 1 else b"0"
                            bit += 1
                    if out:
                        s.sendall(out)

    def close(self):
        self.stop = True
        self.t.join(timeout=2)
        self.srv.close()


class FakeIdentify:
    """The harness's UDP identify: echoes the nonce, reports self.rm_id; silent=True
    answers nothing (a harness that is not running)."""

    def __init__(self, rm_id: int = 0x01000001, silent: bool = False):
        self.rm_id, self.silent, self.stop = rm_id, silent, False
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.settimeout(0.05)
        self.requests = 0
        self.t = threading.Thread(target=self.run, daemon=True)
        self.t.start()

    def run(self):
        while not self.stop:
            try:
                data, peer = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            self.requests += 1
            if self.silent:
                continue
            req = json.loads(data)
            assert req["op"] == "identify" and req["v"] == 1
            rep = {"ok": True, "op": "identify", "v": 1, "nonce": req["nonce"], "board": "mps3",
                   "mac": "0200000002fe", "ip": "192.168.10.102", "dhcp": False,
                   "shell_id": "0x44ee76d5", "rm_id": "0x%08x" % self.rm_id, "harness": "1.0.0",
                   "proto": "0.16", "mode": "run", "impl": "linux", "up_ms": 1,
                   "ports": {"ctrl": 6900, "jtag": 6921}}
            self.sock.sendto(json.dumps(rep, separators=(",", ":")).encode(), peer)

    def close(self):
        self.stop = True
        self.t.join(timeout=2)
        self.sock.close()


class Rig:
    def __init__(self, tmp: Path, launcher: Path, jtag: FakeJtag, ident: FakeIdentify, **env):
        self.tmp, self.launcher, self.jtag, self.ident = tmp, launcher, jtag, ident
        self.share = tmp / "share"
        self.share.mkdir()
        for f in list(CFG_DIR.glob("*.cfg")) + list(CFG_DIR.glob("*.tcl")):
            shutil.copy(f, self.share)
        shutil.copy(PKG / "designs.conf", self.share / "designs.conf")
        (self.share / "VERSION").write_text("0.12.0+mps3 (test)\n")
        self.run_dir = tmp / "run"
        self.argv_file = tmp / "openocd.argv"
        self.gdb = free_port()
        # cpu1 is gdb+1: make sure that is free too
        while True:
            try:
                s = socket.socket()
                s.bind(("127.0.0.1", self.gdb + 1))
                s.close()
                break
            except OSError:
                self.gdb = free_port()
        self.env = dict(os.environ)
        self.env.update({
            "MPS3_DEBUG_OPENOCD": str(HERE / "fake_openocd.py"),
            "MPS3_DEBUG_SHARE": str(self.share), "MPS3_DEBUG_RUN": str(self.run_dir),
            "MPS3_DEBUG_USER": "", "MPS3_DEBUG_IDENTIFY_PORT": str(ident.port),
            "MPS3_DEBUG_RBB_PORT": str(jtag.port), "MPS3_DEBUG_GDB_PORT": str(self.gdb),
            "MPS3_DEBUG_TELNET_PORT": str(free_port()), "MPS3_DEBUG_TCL_PORT": str(free_port()),
            "MPS3_DEBUG_WAIT_S": "20", "MPS3_DEBUG_SWAP_POLL_MS": "200",
            "MPS3_DEBUG_LOCK_WAIT_MS": "500", "FAKE_OCD_ARGV": str(self.argv_file),
        })
        self.env.update({k: str(v) for k, v in env.items()})

    def __call__(self, *args, expect_rc=None):
        p = subprocess.run([str(self.launcher), *args], env=self.env, capture_output=True,
                           text=True, timeout=60)
        lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
        assert len(lines) == 1, f"one JSON object on stdout, got {p.stdout!r} / {p.stderr!r}"
        j = json.loads(lines[0])
        if expect_rc is not None:
            assert p.returncode == expect_rc, (p.returncode, j)
        return p.returncode, j

    def wait_state(self, want, timeout=10.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            _, j = self("status")
            if j["state"] == want:
                return j
            time.sleep(0.2)
        raise AssertionError(f"state never became {want}: {j}")


@pytest.fixture
def rig(tmp_path, launcher):
    rigs = []

    def make(rm_id=0x01000001, silent=False, jtag_mode="normal", **env):
        j, i = FakeJtag(jtag_mode), FakeIdentify(rm_id, silent)
        d = tmp_path / f"r{len(rigs)}"
        d.mkdir()
        r = Rig(d, launcher, j, i, **env)
        rigs.append(r)
        return r

    yield make
    for r in rigs:
        subprocess.run([str(r.launcher), "down"], env=r.env, capture_output=True, timeout=30)
        r.jtag.close()
        r.ident.close()


def test_version_names_the_contract_and_the_designs(rig):
    r = rig()
    rc, j = r("version", "--json", expect_rc=0)
    assert j["schema"] == "mps3-debug/1" and j["openocd"]["adapter"] == "remote_bitbang"
    assert j["openocd"]["version"] == "0.12.0+mps3 (test)" and j["openocd"]["present"] is True
    names = {d["name"]: d for d in j["designs"]}
    assert names["nanosoc"]["dap"] and names["nanosoc_multicore"]["cores"] == 2
    assert names["led"]["dap"] is False and j["designs_bad_lines"] == 0
    assert j["bind"] == "127.0.0.1"


def test_up_auto_status_already_down(rig):
    r = rig(rm_id=0x01000001)
    rc, j = r("up", "--rm", "auto", "--json", expect_rc=0)
    assert j["schema"] == "mps3-debug/1" and j["state"] == "up" and j["already"] is False
    assert j["design"] == "nanosoc" and j["rm_id"] == "0x01000001"
    assert j["cfg"] == ["nanosoc_mps3_jtag.cfg", "nanosoc_ops.tcl"]
    assert j["tap"] == {"idcode": "0x6ba00477"}
    assert j["cores"] == [{"name": "cpu0", "gdb_port": r.gdb}]
    assert j["bind"] == "127.0.0.1" and j["busy"] is None and j["error"] is None
    assert j["started_at"].endswith("Z") and isinstance(j["pid"], int)
    assert j["openocd"] == {"version": "0.12.0+mps3 (test)", "adapter": "remote_bitbang"}
    # bound to loopback only: the GDB port answers on 127.0.0.1
    socket.create_connection(("127.0.0.1", r.gdb), timeout=2).close()
    pid = j["pid"]
    # idempotent
    rc, j2 = r("up", expect_rc=0)
    assert j2["already"] is True and j2["pid"] == pid and j2["state"] == "up"
    rc, st = r("status", expect_rc=0)
    assert st["state"] == "up" and st["pid"] == pid
    rc, d = r("down", expect_rc=0)
    assert d["state"] == "down" and d["reason"] == "closed" and d["pid"] is None and d["cores"] == []
    time.sleep(0.3)
    assert not os.path.exists(f"/proc/{pid}") or \
        open(f"/proc/{pid}/stat").read().split()[2] == "Z"
    rc, st = r("status", expect_rc=0)
    assert st["state"] == "down"


def test_the_argv_is_hm_s_proven_order(rig):
    r = rig()
    r("up", expect_rc=0)
    argv = r.argv_file.read_text().splitlines()
    assert argv[0:2] == ["-s", str(r.share)]
    cs = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
    fs = [argv[i + 1] for i, a in enumerate(argv) if a == "-f"]
    assert cs[0] == "set RBB_HOST 127.0.0.1" and cs[1] == f"set RBB_PORT {r.jtag.port}"
    assert cs[2] == "set TRANSPORT_MODE rbb"
    assert fs == ["nanosoc_mps3_jtag.cfg", "nanosoc_ops.tcl"]
    assert cs[3] == "nanosoc.cpu0 configure -event gdb-attach nanosoc_halt_examine"
    assert cs[-1].startswith(f"gdb_port {r.gdb}; ") and cs[-1].endswith("; bindto 127.0.0.1")
    # the probe sets precede every -f; the ports come last
    first_f = argv.index("-f")
    assert all(argv.index(c) < first_f for c in cs[:3]) and argv.index(cs[-1]) == len(argv) - 1


def test_multicore_by_name_has_two_gdb_ports(rig):
    r = rig(rm_id=0x01000001)
    rc, j = r("up", "--rm", "nanosoc_multicore", expect_rc=0)
    assert j["design"] == "nanosoc_multicore"
    assert j["cores"] == [{"name": "cpu0", "gdb_port": r.gdb}, {"name": "cpu1", "gdb_port": r.gdb + 1}]
    argv = r.argv_file.read_text()
    assert "nanosoc.cpu1 configure -event gdb-attach nanosoc_halt_examine1" in argv


def test_no_openocd_is_12(rig):
    r = rig(MPS3_DEBUG_OPENOCD="/nonexistent/openocd")
    rc, j = r("up", expect_rc=12)
    assert j["error"]["code"] == "no_openocd" and j["state"] == "down"


def test_a_design_without_a_dap_is_13(rig):
    r = rig(rm_id=0x0100001E)                       # led
    rc, j = r("up", expect_rc=13)
    assert j["error"]["code"] == "no_dap" and j["design"] == "led" and j["rm_id"] == "0x0100001e"


def test_an_unknown_rm_id_is_14_with_the_hint(rig):
    r = rig(rm_id=0x01000042)
    rc, j = r("up", expect_rc=14)
    assert j["error"]["code"] == "no_cfg" and "--rm" in j["error"]["hint"]
    assert j["rm_id"] == "0x01000042"


def test_a_silent_identify_is_14_and_rm_name_still_works(rig):
    r = rig(silent=True)
    rc, j = r("up", expect_rc=14)
    assert j["error"]["code"] == "no_cfg" and "--rm NAME" in j["error"]["hint"]
    assert "6899" in j["error"]["message"] or "identify" in j["error"]["message"]
    rc, j = r("up", "--rm", "nanosoc", expect_rc=0)
    assert j["state"] == "up" and j["rm_id"] is None


def test_an_unknown_name_is_14(rig):
    r = rig()
    rc, j = r("up", "--rm", "nosuchdesign", expect_rc=14)
    assert j["error"]["code"] == "no_cfg"


def test_a_live_6921_client_makes_it_busy_with_the_peer(rig):
    r = rig()
    holder = socket.create_connection(("127.0.0.1", r.jtag.port), timeout=2)
    time.sleep(0.2)
    try:
        rc, j = r("up", expect_rc=4)
        assert j["error"]["code"] == "busy"
        assert j["busy"]["by"] == "6921 remote_bitbang"
        assert j["busy"]["peer"] == "127.0.0.1:%d" % holder.getsockname()[1]
        assert j["state"] == "down"
    finally:
        holder.close()


def test_a_6921_that_resets_openocd_is_busy_with_the_log_tail(rig):
    r = rig(jtag_mode="reset_all")
    rc, j = r("up", expect_rc=4)
    assert j["state"] == "failed" and j["error"]["code"] == "busy"
    assert j["busy"]["by"] == "6921 remote_bitbang"
    assert any("remote_bitbang_fill_buf" in ln for ln in j["log_tail"]), j["log_tail"]
    assert 0 < len(j["log_tail"]) <= 20


def test_a_swap_stops_the_session_as_target_lost(rig):
    r = rig(rm_id=0x01000001)
    rc, j = r("up", expect_rc=0)
    pid = j["pid"]
    r.ident.rm_id = 0x01000005                     # the partition now holds nanosoc_upy
    st = r.wait_state("down", timeout=10)
    # the contract (HM, 1 Oct): reason target_lost, error.code openocd_exit, "swap:" first
    assert st["reason"] == "target_lost" and st["error"]["code"] == "openocd_exit"
    assert st["error"]["message"].startswith("swap: ")
    assert "0x01000001 -> 0x01000005" in st["error"]["message"]
    time.sleep(0.3)
    assert not os.path.exists(f"/proc/{pid}") or open(f"/proc/{pid}/stat").read().split()[2] == "Z"
    # and a fresh up follows the new design
    rc, j = r("up", expect_rc=0)
    assert j["design"] == "nanosoc_upy" and j["rm_id"] == "0x01000005"


def test_idle_closes_only_without_a_gdb_client(rig):
    r = rig(MPS3_DEBUG_IDLE_S=2)
    rc, j = r("up", expect_rc=0)
    g = socket.create_connection(("127.0.0.1", r.gdb), timeout=2)
    time.sleep(3.5)
    rc, st = r("status")
    assert st["state"] == "up", "a GDB client keeps the session"
    g.close()
    st = r.wait_state("down", timeout=8)
    assert st["reason"] == "idle" and st["error"] is None


def test_an_openocd_crash_is_failed_with_the_log(rig, tmp_path):
    flag = tmp_path / "crash.flag"
    r = rig(FAKE_OCD_CRASH=str(flag))
    rc, j = r("up", expect_rc=0)
    flag.write_text("x")
    st = r.wait_state("failed", timeout=8)
    assert st["error"]["code"] == "openocd_exit" and st["reason"] == "openocd_exit"
    assert any("fake crash" in ln for ln in st["log_tail"])
    # a fresh up recovers
    flag.unlink()
    rc, j = r("up", expect_rc=0)
    assert j["state"] == "up"


def test_one_instance_the_lock(rig):
    r = rig()
    r.run_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(r.run_dir / "lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        rc, j = r("up", expect_rc=4)
        assert j["error"]["code"] == "busy" and j["busy"]["by"] == "mps3-debug"
    finally:
        os.close(fd)
    rc, j = r("up", expect_rc=0)


def test_usage_is_2_and_bad_names_are_refused(rig):
    r = rig()
    p = subprocess.run([str(r.launcher), "up", "--rm", "Bad;Name"], env=r.env,
                       capture_output=True, text=True)
    assert p.returncode == 2
    p = subprocess.run([str(r.launcher), "frobnicate"], env=r.env, capture_output=True, text=True)
    assert p.returncode == 2
