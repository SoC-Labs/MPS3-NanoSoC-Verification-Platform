"""test_harnessd_e2e.py — mps3-harnessd (host build, MOCK fabric) over REAL
sockets: the gate in HARNESSD_CONTRACT.md §8.

Every test starts its own harnessd with every contract port at a random offset
(--port-offset) on loopback, and a behavioural fabric in a file the test can
read and poke (harnessd_mock.py). Clients are the REAL ones where they exist:
pyverify's tftp_put / tcp_send / tcp_send_windowed for pushes, OpenOCD's
remote_bitbang byte protocol on 6921, the XVC 1.0 protocol on 2542.

Negative controls live next to their positives: a garbage stage0 block, an
image claim for another fabric, a malformed identify, a second TOFU claim, a
stopped harnessd whose kicks must STOP.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
REPO = HERE.parents[4]
sys.path.insert(0, str(REPO / "host" / "pyverify"))

from harnessd_mock import (  # noqa: E402
    CLKRST, HDR_MMCM_KHZ, HDR_MMCM_LOADS, HOST_FEATURES, LINUX_DIAG_OMITTED, MMCM_CFG2,
    MMCM_DRP, S0_ATT_CONFIRM, S0_CONFIRM_MAGIC, TAP_IDCODE, Ctl,
    Harnessd,
    clearing_payload, frame, partial_payload,
)
from pyverify import pusher  # noqa: E402

BIN = os.environ.get("HARNESSD_BIN", str(HERE.parent / "build" / "host" / "mps3-harnessd"))
pytestmark = pytest.mark.skipif(not Path(BIN).exists(), reason=f"{BIN} not built (make host)")

SID = 0x5A5A0001


@pytest.fixture
def hd(tmp_path):
    h = Harnessd(BIN, tmp_path, static_id=SID).start()
    yield h
    h.stop()


# --------------------------------------------------------------------------- #
# the control channel
# --------------------------------------------------------------------------- #

def test_every_verb_answers_with_the_linux_shape(hd):
    with hd.ctl() as c:
        ping = c.req({"op": "ping"})
        assert ping == {"ok": True, "shell_id": f"0x{SID:08x}", "rm_id": "0x00000000"}

        c.send({"op": "version"})
        raw = c.line()
        ver = json.loads(raw)
        assert ver["ok"] is True and ver["lmb_kb"] == 128
        assert ver["features"] == HOST_FEATURES          # PRODUCT=1 minus windowed
        assert raw.endswith(b',"impl":"linux"}')        # additive, LAST
        assert "id_skew" not in ver                      # identity consistent

        st = c.req({"op": "stats"})
        keys = list(st)
        assert keys[1] == "up_ms" and keys[-1] == "os_up_ms"
        assert st["sid"] == f"0x{SID:08x}"
        assert st["mmcm"] is True and st["clk_alive"] is True

        d = c.req({"op": "diag"})
        assert d["ok"] is True
        assert not (set(d) & LINUX_DIAG_OMITTED), "omitted diag keys leaked onto the line"
        assert d["svc_count"] == 13                     # + row 12 "usd" (D13)
        assert "svc_us_6" in d and "usd_boot" in d       # diag v9, filled on Linux too

        assert c.req({"op": "telemetry"}) == {"ok": False, "err": "no power sensor", "lockup": False}
        assert c.req({"op": "reset", "target": "dut"}) == {"ok": True}
        assert c.req({"op": "set_clk", "preset": "25mhz"}) == {"ok": True, "locked": True}
        assert c.req({"op": "link", "event": "down"}) == {"ok": True}
        mg = c.req({"op": "macgen", "gen": True, "chk": True, "inject": "none"})
        assert mg["ok"] is True and set(mg) == {"ok", "tx", "rx", "err"}
        # v0.13: the v0.11 commit form is bad args; no card is not an error
        assert c.req({"op": "commit", "rm": "x"}) == {"ok": False, "err": "bad args"}
        u = c.req({"op": "usd"})
        assert u["boot"] in ("pending", "none")          # decided after the 100 ms grace
        assert {k: v for k, v in u.items() if k != "boot"} == \
            {"ok": True, "present": False, "state": "none", "text": "none"}
        assert c.req({"op": "display", "owner": "query"})["ok"] is True   # KVM built in
        du = c.req({"op": "dutrx"})
        assert du["ok"] is True and du["n"] == 0
        tc = c.req({"op": "touch_cal", "act": "get"})
        assert tc["ok"] is True
        lg = c.req({"op": "log", "off": 0})
        assert lg["ok"] is True and b"shell up:" in bytes.fromhex(lg["data"]) or lg["more"]
        assert c.req({"op": "selfdestruct"}) == {"ok": False, "err": "unknown op"}


def test_log_verb_serves_the_console(hd):
    text = b""
    off = 0
    with hd.ctl() as c:
        for _ in range(64):
            r = c.req({"op": "log", "off": off})
            text += bytes.fromhex(r["data"])
            off = r["off"] + r["n"]
            if not r["more"]:
                break
    assert b"shell up: static_id 0x5a5a0001" in text


def test_single_client_refusal_is_accept_then_eof(hd):
    with hd.ctl() as c:
        c.req({"op": "ping"})
        s2 = socket.create_connection(("127.0.0.1", hd.port(6900)), timeout=3)
        s2.settimeout(3)
        t0 = time.time()
        try:
            data = s2.recv(16)
        except ConnectionResetError:
            data = b""
        assert data == b"" and time.time() - t0 < 2.0
        s2.close()
        assert c.req({"op": "ping"})["ok"] is True   # the first client is untouched


# --- reap before refuse (2026-09-28, net_if.h mps3_net_peer_closed) ----------
# Every single-client service accepts FIRST in its pass and reads the current
# client later, so a client that closed and at once reconnected used to meet its
# own old connection still registered -- and was refused (the soak's 6900 RST,
# Harness Manager's back-to-back requests). These use RAW sockets with NO retry:
# a refusal fails the test instead of being absorbed the way Ctl absorbs it.
# (The deterministic before/after proof, with a negative control, is
# firmware/test/test_ctrl_reap{,_noprobe}; these are the real-socket half.)

def _line_on_fresh_connection(port: int, line: bytes) -> bytes:
    """Connect, send one line, read one line, close. b"" = closed unanswered."""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    f = s.makefile("rb")
    try:
        s.sendall(line)
        return f.readline()
    except ConnectionResetError:
        return b""
    finally:
        f.close()
        s.close()


def test_6900_close_then_reconnect_is_answered_every_time(hd):
    for i in range(200):
        ln = _line_on_fresh_connection(hd.port(6900), b'{"op":"ping"}\n')
        assert ln, f"reconnect {i} was refused (closed unanswered)"
        assert json.loads(ln)["ok"] is True, ln
    # ...and through pyverify's real client, one connection per request, the way
    # Harness Manager drives it.
    from pyverify.client import ShellClient
    for i in range(50):
        with ShellClient("127.0.0.1", hd.port(6900), timeout=5) as sc:
            assert sc.ping().ok, i


def _push_pair(hd, rm_id: int) -> None:
    clr = frame(clearing_payload(), kind=0, static_id=SID, rm_id=rm_id)
    par = frame(partial_payload(rm_id), kind=1, static_id=SID, rm_id=rm_id)
    for data in (clr, par):
        pusher.tcp_send(data, "127.0.0.1", hd.port(6910))
        time.sleep(0.2)


def test_6900_parked_client_that_hangs_up_is_reaped(hd):
    """A client parked on a held verb is never read, so nothing but the probe can
    see it hang up. The newcomer is adopted and served while the verb is still in
    flight, the held verb still settles, and its answer goes to nobody."""
    rm = 0x0100001E
    s1 = socket.create_connection(("127.0.0.1", hd.port(6900)), timeout=5)
    s1.sendall(json.dumps({"op": "swap", "rm": "led", "src": "tcp"}).encode() + b"\n")
    time.sleep(0.2)                                   # parked: no push yet
    s1.close()
    s2 = socket.create_connection(("127.0.0.1", hd.port(6900)), timeout=5)
    f2 = s2.makefile("rb")
    try:
        s2.sendall(b'{"op":"ping"}\n')
        ln = f2.readline()
        assert ln, "the newcomer was refused behind a dead parked client"
        assert json.loads(ln) == {"ok": True, "shell_id": f"0x{SID:08x}",
                                  "rm_id": "0x00000000"}, ln
        _push_pair(hd, rm)                            # the held verb still settles...
        deadline = time.time() + 5
        while True:
            s2.sendall(b'{"op":"ping"}\n')
            r = json.loads(f2.readline())
            assert set(r) == {"ok", "shell_id", "rm_id"}, r   # ...never delivered here
            if r["rm_id"] == f"0x{rm:08x}" or time.time() > deadline:
                break
            time.sleep(0.05)
        assert r["rm_id"] == f"0x{rm:08x}", hd.log()
    finally:
        f2.close()
        s2.close()


def test_6900_parked_live_client_keeps_the_slot(hd):
    """Single-client semantics are unchanged: a parked client that is still
    connected wins the slot (the newcomer is accept-then-EOF) and gets its held
    answer."""
    rm = 0x0100001E
    with hd.ctl() as c:
        c.send({"op": "swap", "rm": "led", "src": "tcp"})
        time.sleep(0.2)
        s2 = socket.create_connection(("127.0.0.1", hd.port(6900)), timeout=3)
        try:
            assert s2.recv(16) == b""
        except ConnectionResetError:
            pass
        finally:
            s2.close()
        _push_pair(hd, rm)
        assert json.loads(c.line()) == {"ok": True, "rm_id": f"0x{rm:08x}", "verified": True}


def test_debug_and_console_ports_close_then_reconnect(hd):
    """The same reap on 2542 (XVC), 6921 (remote_bitbang) and 6930 (console)."""
    for i in range(50):
        s = socket.create_connection(("127.0.0.1", hd.port(2542)), timeout=5)
        try:
            s.sendall(b"getinfo:")
            info = s.recv(64)
        except ConnectionResetError:
            info = b""
        s.close()
        assert info.startswith(b"xvcServer_v1.0:"), f"2542 reconnect {i}: {info!r}"
    for i in range(50):
        s = socket.create_connection(("127.0.0.1", hd.port(6921)), timeout=5)
        try:
            s.sendall(b"R")
            bit = s.recv(1)
        except ConnectionResetError:
            bit = b""
        s.close()
        assert bit in (b"0", b"1"), f"6921 reconnect {i}: {bit!r}"
    for i in range(30):
        s = socket.create_connection(("127.0.0.1", hd.port(6930)), timeout=5)
        got = b""
        try:
            s.sendall(b"%c" % (0x41 + i % 26))       # the mock bridge loops it back
            deadline = time.time() + 3
            while not got and time.time() < deadline:
                got = s.recv(16)
                if not got:
                    break                            # EOF: refused
        except ConnectionResetError:
            got = b""
        s.close()
        assert got == b"%c" % (0x41 + i % 26), f"6930 reconnect {i}: {got!r}"


# --------------------------------------------------------------------------- #
# JTAG 6921 (OpenOCD remote_bitbang) and XVC 2542, against the mock TAP
# --------------------------------------------------------------------------- #

def _rbb(tck, tms, tdi):
    return bytes([ord("0") + (tck << 2 | tms << 1 | tdi)])


def test_jtag_remote_bitbang_reads_the_idcode(hd):
    s = socket.create_connection(("127.0.0.1", hd.port(6921)), timeout=5)
    out = b""
    def clk(tms, tdi=0, read=False):
        nonlocal out
        out += _rbb(0, tms, tdi)
        if read:
            out += b"R"
        out += _rbb(1, tms, tdi)
    for _ in range(5):
        clk(1)                    # Test-Logic-Reset (selects IDCODE)
    for tms in (0, 1, 0, 0):
        clk(tms)                  # RTI -> Select-DR -> Capture-DR -> Shift-DR
    for i in range(32):
        clk(1 if i == 31 else 0, read=True)
    out += _rbb(0, 1, 0)
    s.sendall(out)
    got = b""
    while len(got) < 32:
        chunk = s.recv(64)
        assert chunk, "6921 closed early"
        got += chunk
    s.close()
    bits = [1 if ch == ord("1") else 0 for ch in got[:32]]
    idcode = sum(b << i for i, b in enumerate(bits))
    assert idcode == TAP_IDCODE, hex(idcode)


def test_xvc_getinfo_and_an_idcode_shift(hd):
    s = socket.create_connection(("127.0.0.1", hd.port(2542)), timeout=5)
    s.sendall(b"getinfo:")
    info = s.recv(64)
    assert info.startswith(b"xvcServer_v1.0:") and info.endswith(b"\n")
    tms_bits = [1] * 5 + [0, 1, 0, 0] + [0] * 31 + [1]
    n = len(tms_bits)
    def vec(bits):
        v = bytearray((len(bits) + 7) // 8)
        for i, b in enumerate(bits):
            v[i // 8] |= b << (i % 8)
        return bytes(v)
    s.sendall(b"shift:" + struct.pack("<I", n) + vec(tms_bits) + vec([0] * n))
    want = (n + 7) // 8
    tdo = b""
    while len(tdo) < want:
        tdo += s.recv(want - len(tdo))
    s.close()
    bits = [(tdo[i // 8] >> (i % 8)) & 1 for i in range(n)]
    idcode = sum(bits[9 + i] << i for i in range(32))
    assert idcode == TAP_IDCODE, hex(idcode)


# --------------------------------------------------------------------------- #
# pushes + swaps: TFTP, plain 6910, windowed 6910 (both pyverify pusher modes)
# --------------------------------------------------------------------------- #

def _swap(hd, rm_id, clearing_via, partial_via):
    clr = frame(clearing_payload(), kind=0, static_id=SID, rm_id=rm_id)
    par = frame(partial_payload(rm_id), kind=1, static_id=SID, rm_id=rm_id)
    with hd.ctl() as c:
        c.send({"op": "swap", "rm": f"rm{rm_id}", "src": "tcp"})
        time.sleep(0.2)                                   # FSM -> AWAIT_INCOMING_CLEARING
        for data, via in ((clr, clearing_via), (par, partial_via)):
            if via == "tftp":
                pusher.tftp_put(data, "127.0.0.1", hd.port(69), timeout_s=2.0)
            elif via == "tcp":
                pusher.tcp_send(data, "127.0.0.1", hd.port(6910))
            else:
                pusher.tcp_send_windowed(data, "127.0.0.1", hd.port(6910))
            time.sleep(0.2)
        return json.loads(c.line())


@pytest.mark.parametrize("clr_via,par_via", [("tftp", "tcp"), ("tcp", "windowed"),
                                             ("windowed", "tftp")])
def test_swap_with_every_push_transport(hd, clr_via, par_via):
    reply = _swap(hd, 0x0100001E, clr_via, par_via)
    assert reply == {"ok": True, "rm_id": "0x0100001e", "verified": True}, hd.log()
    assert hd.req({"op": "ping"})["rm_id"] == "0x0100001e"
    assert hd.fabric.hdr(0x1C) > 0                        # words really reached HWICAP
    # the resident clearing was persisted for a restart (§6)
    assert (hd.dir / "state" / "resident.id").read_text().startswith("rm_id=0x0100001e")


def test_a_partial_outside_a_swap_is_rejected(hd):
    par = frame(partial_payload(7), kind=1, static_id=SID, rm_id=7)
    with pytest.raises(pusher.PushError):
        pusher.tftp_put(par, "127.0.0.1", hd.port(69), timeout_s=1.0, retries=1)


def test_a_push_for_another_static_is_rejected(hd):
    with hd.ctl() as c:
        c.send({"op": "swap", "rm": "x", "src": "tcp"})
        time.sleep(0.2)
        clr = frame(clearing_payload(), kind=0, static_id=SID ^ 1, rm_id=1)
        with pytest.raises(pusher.PushError):
            pusher.tftp_put(clr, "127.0.0.1", hd.port(69), timeout_s=1.0, retries=1)


# --------------------------------------------------------------------------- #
# the busy-ICAP race: swapping AWAY from a DAP RM (B1 v4 2026-09-25, finding h)
# --------------------------------------------------------------------------- #
# A swap first streams the OUTGOING RM's cached clearing into the ICAP; the host
# pushes the incoming clearing and then the partial with no pause. A big resident
# clearing (nanosoc: 167,308 B) outlasts the incoming clearing's push, so the
# partial's header lands before SWAP_AWAIT_PARTIAL: 6910 PARKS it
# (RECV_PENDING_ICAP_BEGIN), TFTP REJECTS it. Here the resident clearing is the
# arena's 1 MiB, streamed 256 words a loop pass, and the clients are pyverify's.

BIG_CLEARING_WORDS = 1_048_576 // 4


def _overlay(root: Path, name: str, rm_id: int, clr_words: int) -> Path:
    import zlib
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    clr, prt = clearing_payload(clr_words), partial_payload(rm_id, 4096)
    (d / "c.bin").write_bytes(clr)
    (d / "p.bin").write_bytes(prt)
    (d / "manifest.json").write_text(json.dumps({
        "schema": 1, "static_id": "0x%08X" % SID, "rm_id": "0x%08x" % rm_id, "rm_name": name,
        "clearing": {"file": "c.bin", "len": len(clr), "crc32": "0x%08x" % zlib.crc32(clr)},
        "partial": {"file": "p.bin", "len": len(prt), "crc32": "0x%08x" % zlib.crc32(prt)},
        "built": "2026-09-25", "vivado": "2026.1"}))
    return d


def _deploy(hd, ovl: Path, *extra: str, capsys=None) -> "tuple[int, str]":
    """`pyverify deploy`, retried ONLY when 6900 refused its connection outright.

    6900 is single-client and accepts BEFORE it reads the previous client's EOF
    (coordinator_net_poll), and after sending a HELD reply (a swap) it returns
    without reading at all -- so a deploy that connects straight after another
    client closed (this file's `with hd.ctl()` swap) is accept-then-closed:
    `version unavailable (... reset ...)`, then exit 3. A refused connection is
    never read, so nothing reached the shell and the retry repeats nothing
    (harnessd_mock.Ctl adopts its connections for the same reason). Any other
    failure is returned at once."""
    import contextlib
    import io
    from pyverify import cli
    argv = ["deploy", "--host", "127.0.0.1", "--overlay", str(ovl), "--no-persist",
            "--control-port", str(hd.port(6900)), "--tftp-port", str(hd.port(69)),
            "--tcp-push-port", str(hd.port(6910)), *extra]
    for _ in range(100):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = cli.main(argv)
        err = buf.getvalue()
        sys.stderr.write(err)                            # capsys still sees every attempt
        if not (rc == 3 and "version unavailable (" in err and "cannot reach shell control "
                "channel" in err):
            break
        time.sleep(0.05)
    return rc, (capsys.readouterr().err if capsys else "")


def test_deploy_away_from_a_dap_rm_parks_on_6910(hd, tmp_path, capsys):
    """pyverify deploy's AUTO choice against harnessd (impl linux) is 6910, and
    the real config_agent parks the early partial: every swap away verifies."""
    dap = _overlay(tmp_path, "dap", 0x01000001, BIG_CLEARING_WORDS)
    small = _overlay(tmp_path, "dbg", 0x01000009, 64)
    for _ in range(3):
        for ovl, rm_id in ((dap, "0x01000001"), (small, "0x01000009")):
            rc, err = _deploy(hd, ovl, capsys=capsys)
            assert rc == 0, err + hd.log()
            assert "transport=tcp src=tcp windowed=False (version.impl = linux" in err
            assert hd.req({"op": "ping"})["rm_id"] == rm_id


def test_tftp_partial_is_rejected_in_the_busy_window_and_a_re_push_lands(hd, tmp_path):
    """The silicon failure on the real code, and the TFTP re-push that survives
    it: a partial pushed straight after the clearing is refused at DATA block 1
    (TFTP ERROR 0 "rejected") while the 1 MiB resident clearing streams; the swap
    carries on, and the same partial pushed again once the FSM awaits it lands.
    Timing-based, so the refusal must show in at least one of three cycles."""
    dap = _overlay(tmp_path, "dap", 0x01000001, BIG_CLEARING_WORDS)
    rejected = 0
    for i in range(3):
        rc, _ = _deploy(hd, dap)                         # a DAP-sized clearing resident
        assert rc == 0, hd.log()
        rm_id = 0x01000009 + i
        clr = frame(clearing_payload(64), kind=0, static_id=SID, rm_id=rm_id)
        par = frame(partial_payload(rm_id, 4096), kind=1, static_id=SID, rm_id=rm_id)
        with hd.ctl() as c:
            c.send({"op": "swap", "rm": "dbg", "src": "tftp"})
            pusher.tftp_put(clr, "127.0.0.1", hd.port(69), timeout_s=2.0)
            try:
                pusher.tftp_put(par, "127.0.0.1", hd.port(69), timeout_s=2.0)
            except pusher.PushError as exc:
                assert "DATA block 1: server aborted: TFTP ERROR 0: rejected" in str(exc)
                rejected += 1
                time.sleep(0.5)                          # the stream ends; the FSM awaits
                pusher.tftp_put(par, "127.0.0.1", hd.port(69), timeout_s=2.0)
            assert json.loads(c.line()) == {"ok": True, "rm_id": "0x%08x" % rm_id,
                                            "verified": True}, hd.log()
    assert rejected >= 1, "the busy-ICAP race never showed: is the resident clearing streamed?"


# --------------------------------------------------------------------------- #
# identify (UDP 6899)
# --------------------------------------------------------------------------- #

def _identify(hd, payload: bytes, timeout=1.0):
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.settimeout(timeout)
    u.sendto(payload, ("127.0.0.1", hd.port(6899)))
    try:
        data, _ = u.recvfrom(4096)
        return data
    except socket.timeout:
        return None
    finally:
        u.close()


def test_identify_answers_to_the_sender(hd):
    data = _identify(hd, b'{"op":"identify","v":1,"nonce":"0011aabbccddeeff"}')
    assert data is not None and len(data) <= 1200
    r = json.loads(data)
    assert list(r)[:13] == ["ok", "op", "v", "nonce", "board", "mac", "ip", "dhcp",
                            "shell_id", "rm_id", "harness", "proto", "mode"]
    assert r["nonce"] == "0011aabbccddeeff" and r["mode"] == "run" and r["impl"] == "linux"
    assert r["shell_id"] == f"0x{SID:08x}" and "os_up_ms" in r and "unit" not in r
    assert r["ssh"] == {"claimed": False, "host_key_sha256": "", "key_sha256": ""}
    assert r["ports"]["ctrl"] == 6900 and r["ports"]["swo"] == 6932


@pytest.mark.parametrize("bad", [b"not json", b'{"op":"identify","v":2,"nonce":"00112233"}',
                                 b'{"op":"identify","v":1,"nonce":"0011"}',
                                 b'{"op":"identify","v":1,"nonce":"zz112233"}',
                                 b'{"op":"ping"}'])
def test_identify_malformed_is_silent(hd, bad):
    assert _identify(hd, bad, timeout=0.4) is None


def test_identify_is_rate_limited(hd):
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.settimeout(0.3)
    for i in range(40):
        u.sendto(b'{"op":"identify","v":1,"nonce":"%08x"}' % i, ("127.0.0.1", hd.port(6899)))
    got = 0
    try:
        while True:
            u.recvfrom(4096)
            got += 1
    except socket.timeout:
        pass
    u.close()
    assert 5 <= got <= 12, got


def test_identify_answers_while_6900_is_parked_in_a_swap(hd):
    with hd.ctl() as c:
        c.send({"op": "swap", "rm": "x", "src": "tcp"})   # parked: no push follows
        time.sleep(0.2)
        data = _identify(hd, b'{"op":"identify","v":1,"nonce":"deadbeef"}')
        assert data is not None and json.loads(data)["nonce"] == "deadbeef"


# --------------------------------------------------------------------------- #
# TOFU first-key claim (TFTP WRQ "authorized_keys")
# --------------------------------------------------------------------------- #

def test_tofu_claim_then_refusal(hd):
    key = b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPtestkeytestkeytestkeytestkeytestkeytest user@host\n"
    pusher.tftp_put(key, "127.0.0.1", hd.port(69), filename="authorized_keys")
    ak = hd.dir / "ssh" / "authorized_keys"
    assert ak.read_bytes() == key
    assert oct(ak.stat().st_mode & 0o777) == "0o600"
    r = json.loads(_identify(hd, b'{"op":"identify","v":1,"nonce":"01234567"}'))
    assert r["ssh"]["claimed"] is True
    with pytest.raises(pusher.PushError, match="2|access"):
        pusher.tftp_put(b"ssh-ed25519 AAAAother\n", "127.0.0.1", hd.port(69),
                        filename="authorized_keys", retries=1)
    assert ak.read_bytes() == key                         # untouched


# --------------------------------------------------------------------------- #
# SINGLE-PORT TFTP: every reply FROM :69, so a stateful firewall passes it
# --------------------------------------------------------------------------- #
# B1 (2026-09-24): the hub's INPUT firewall accepts only replies from the exact
# (ip, port) the client sent to. A server that answers from a fresh transfer ID
# (RFC 1350) is dropped there -- stage0 was, and config_agent's claim, slot and
# bitstream pushes would be too. A connected UDP socket is exactly that filter:
# the kernel drops every datagram from any other source. pyverify's tftp_put
# runs unmodified; only its socket is connected on its first send (the shim
# fw_stage0/test/test_tools.py calls FIREWALLED).

class _Stateful(socket.socket):
    def sendto(self, data, *a):
        if self.type == socket.SOCK_DGRAM and getattr(self, "_peer", None) is None:
            self._peer = a[-1]
            self.connect(self._peer)
        return super().sendto(data, *a)


@pytest.fixture
def firewalled(monkeypatch):
    monkeypatch.setattr(pusher.socket, "socket", _Stateful)


def test_every_tftp_reply_comes_from_port_69(hd):
    key = b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPsingleportsingleportsingleportsingleport u@h\n"
    dst = ("127.0.0.1", hd.port(69))
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.settimeout(2.0)
    try:
        u.sendto(b"\0\2authorized_keys\0octet\0", dst)
        pkt, src = u.recvfrom(600)
        assert (pkt, src) == (b"\0\4\0\0", dst), "ACK 0 must come FROM :69 (%r from %r)" % (pkt, src)
        u.sendto(b"\0\3\0\1" + key, dst)                     # DATA to :69, not to a TID
        pkt, src = u.recvfrom(600)
        assert (pkt, src) == (b"\0\4\0\1", dst), "final ACK must come FROM :69 (%r from %r)" % (pkt, src)
    finally:
        u.close()
    assert (hd.dir / "ssh" / "authorized_keys").read_bytes() == key


def test_tofu_claim_through_a_stateful_firewall(hd, firewalled):
    key = b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPfirewalledfirewalledfirewalledfirewall u@h\n"
    pusher.tftp_put(key, "127.0.0.1", hd.port(69), filename="authorized_keys",
                    timeout_s=1.0, retries=1)
    assert (hd.dir / "ssh" / "authorized_keys").read_bytes() == key
    r = json.loads(_identify(hd, b'{"op":"identify","v":1,"nonce":"0badf00d"}'))  # firewalled too
    assert r["ssh"]["claimed"] is True
    with pytest.raises(pusher.PushError, match="2|access"):                      # refusal still lands
        pusher.tftp_put(b"ssh-ed25519 AAAAother\n", "127.0.0.1", hd.port(69),
                        filename="authorized_keys", timeout_s=1.0, retries=1)


def test_tftp_swap_through_a_stateful_firewall(hd, firewalled):
    reply = _swap(hd, 0x0100002A, "tftp", "tftp")
    assert reply == {"ok": True, "rm_id": "0x0100002a", "verified": True}, hd.log()


def test_host_key_fingerprint_matches_ssh_keygen(tmp_path):
    kg = subprocess.run(["which", "ssh-keygen"], capture_output=True, text=True).stdout.strip()
    if not kg:
        pytest.skip("no ssh-keygen")
    key = tmp_path / "hostkey"
    subprocess.run([kg, "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    want = subprocess.run([kg, "-lf", str(key)], capture_output=True, text=True,
                          check=True).stdout.split()[1]
    (tmp_path / "hostkey.pub").unlink()                  # force the private-key parser
    h = Harnessd(BIN, tmp_path / "hd", static_id=SID)
    h.args[h.args.index("--host-key") + 1] = str(key)
    h.start()
    try:
        r = json.loads(_identify(h, b'{"op":"identify","v":1,"nonce":"89abcdef"}'))
        assert r["ssh"]["host_key_sha256"] == want
    finally:
        h.stop()


# --------------------------------------------------------------------------- #
# SAFETY: the fabric-bound identity (HARNESSD_CONTRACT §9.4)
# --------------------------------------------------------------------------- #

def _locked_swap_touches_no_icap(h):
    before = h.fabric.hdr(0x1C)
    with h.ctl() as c:
        c.send({"op": "swap", "rm": "x", "src": "tcp"})
        r = json.loads(c.line())
    assert r["ok"] is False
    assert r["err"] == "swap failed" or r["err"].startswith("identity lock:")
    assert h.fabric.hdr(0x1C) == before, "a locked swap wrote ICAP words"
    return r


def test_identity_match_is_consistent(hd):
    ver = hd.req({"op": "version"})
    assert "id_skew" not in ver


def test_identity_card_mismatch_locks_swaps(tmp_path):
    h = Harnessd(BIN, tmp_path, static_id=SID, s0=SID, claim=0x0BADCAFE).start()
    try:
        assert h.req({"op": "ping"})["shell_id"] == f"0x{SID:08x}"   # FABRIC, not card
        ver = h.req({"op": "version"})
        assert ver["id_skew"] == f"image 0x0badcafe != fabric 0x{SID:08x}"
        _locked_swap_touches_no_icap(h)
    finally:
        h.stop()


def test_identity_garbage_stage0_block_locks_swaps(tmp_path):
    h = Harnessd(BIN, tmp_path, static_id=SID, s0_garbage=True, claim=SID).start()
    try:
        assert h.req({"op": "ping"})["shell_id"] == "0x00000000"      # never the card's
        assert h.req({"op": "version"})["id_skew"] == "no valid stage0 status block"
        _locked_swap_touches_no_icap(h)
    finally:
        h.stop()


def test_identity_usr_access_skew_locks(tmp_path):
    h = Harnessd(BIN, tmp_path, static_id=SID, extra=["--mock-usr-access", "0x01020300"])
    (tmp_path / "version").write_text("harness=1.2.4\nver32=0x01020400\nsha=abcdef12\ndirty=0\n")
    h.start()
    try:
        ver = h.req({"op": "version"})
        assert ver["skew"] is True and ver["usr_access"] == "0x01020300"
        assert ver["id_skew"] == "usr_access 0x01020300 != image 0x01020400"
        assert ver["harness"] == "1.2.4" and ver["sha"] == "abcdef12"
    finally:
        h.stop()


# --------------------------------------------------------------------------- #
# restarts: boot start vs respawn, live resets kept, parked RP never released
# --------------------------------------------------------------------------- #

def test_respawn_keeps_the_rp_and_resyncs_rm_id(tmp_path):
    h = Harnessd(BIN, tmp_path, static_id=SID).start()
    assert _swap(h, 0x00000042, "tcp", "tcp")["verified"] is True
    rst_before = h.fabric.rd(0x44A00000, 0x00)            # CLKRST.RESET_CTRL
    assert rst_before & 0x2                               # rp_resetn released by the swap
    h.stop(signal.SIGKILL)                                # a crash
    assert "FIRST start" in h.log()

    h2 = Harnessd(BIN, tmp_path, static_id=SID, s0=SID)
    h2.start()
    try:
        log = h2.log().split("--- MPS3 shell services")[-1]
        assert "RESPAWN" in log
        assert "power-on decision was already taken in this FPGA configuration" in log
        assert "cached clearing restored" in log
        assert h2.req({"op": "ping"})["rm_id"] == "0x00000042"
        assert h2.fabric.rd(0x44A00000, 0x00) & 0x2 == rst_before & 0x2   # not re-asserted
        # and the NEXT swap clears with the restored RM's clearing, not the greybox's
        assert _swap(h2, 0x00000043, "tcp", "tcp")["verified"] is True
    finally:
        h2.stop()


def test_respawn_into_a_decoupled_rp_parks_it(tmp_path):
    h = Harnessd(BIN, tmp_path, static_id=SID).start()
    h.stop(signal.SIGKILL)
    h.fabric.wr(0x44A10000, 0x00, 1)                      # DFXCTL.DECOUPLE: died mid-swap
    h2 = Harnessd(BIN, tmp_path, static_id=SID, s0=SID).start()
    try:
        assert "PARKED SAFE" in h2.log()
        time.sleep(0.3)
        assert h2.fabric.rd(0x44A10000, 0x00) == 1        # never released blindly
        assert h2.req({"op": "stats"})["decpl"] is True
    finally:
        h2.stop()


# --------------------------------------------------------------------------- #
# the DUT clock across restarts (ILA-mint finding #12; clk_linux.c)
# --------------------------------------------------------------------------- #

def test_dut_mhz_survives_a_respawn(tmp_path):
    """set_clk 100mhz -> kill -9 -> init respawns harnessd -> stats reads 100.
    (The negative control -- the same sequence reading 50 on the RAM shadow -- is
    tests/test_dut_clk.c's bare-metal build: this binary cannot run the old code.)"""
    h = Harnessd(BIN, tmp_path, static_id=SID).start()
    assert h.req({"op": "stats"})["dut_mhz"] == 50
    assert h.req({"op": "set_clk", "preset": "100mhz"}) == {"ok": True, "locked": True}
    assert h.fabric.hdr(HDR_MMCM_KHZ) == 100000            # the MMCM runs 100
    loads = h.fabric.hdr(HDR_MMCM_LOADS)
    h.stop(signal.SIGKILL)

    h2 = Harnessd(BIN, tmp_path, static_id=SID, s0=SID).start()
    try:
        log = h2.log().split("--- MPS3 shell services")[-1]
        assert "RESPAWN" in log and "clock: respawn -- the MMCM is not touched" in log
        assert h2.req({"op": "stats"})["dut_mhz"] == 100
        assert h2.fabric.hdr(HDR_MMCM_LOADS) == loads       # nothing re-programmed it
        assert h2.fabric.hdr(HDR_MMCM_KHZ) == 100000
    finally:
        h2.stop()


def test_boot_start_after_a_fabric_reset_resyncs_the_mmcm(tmp_path):
    """A POR/WDOG reset returns clk_wiz_dut's register file to 50 MHz but not the
    MMCM's DRP preset. The next OS boot's harnessd re-LOADs the register file while
    the DUT is held, so dut_mhz reads 50 AND the MMCM runs 50."""
    h = Harnessd(BIN, tmp_path, static_id=SID).start()
    assert h.req({"op": "set_clk", "preset": "100mhz"})["ok"] is True
    h.stop(signal.SIGKILL)
    h.fabric.fabric_reset()                                # the watchdog fires
    assert h.fabric.hdr(HDR_MMCM_KHZ) == 100000             # ...the MMCM kept 100:
    assert h.fabric.rd(MMCM_DRP, MMCM_CFG2) == 20           # the register file says 50
    (tmp_path / "run.marker").unlink()                      # a new OS boot

    h2 = Harnessd(BIN, tmp_path, static_id=SID, s0=SID).start()
    try:
        log = h2.log().split("--- MPS3 shell services")[-1]
        assert "FIRST start" in log and "(50 MHz) re-LOADed into the MMCM, locked" in log
        assert h2.fabric.hdr(HDR_MMCM_KHZ) == 50000             # true again
        assert h2.req({"op": "stats"})["dut_mhz"] == 50
    finally:
        h2.stop()


def test_boot_start_with_a_running_dut_leaves_its_clock_alone(tmp_path):
    """A Linux-only reboot (no fabric reset): the DUT's reset is still released,
    so the MMCM is not re-LOADed under it; the register file already describes it."""
    h = Harnessd(BIN, tmp_path, static_id=SID).start()
    assert h.req({"op": "set_clk", "preset": "100mhz"})["ok"] is True
    assert h.req({"op": "reset", "target": "dut"}) == {"ok": True}   # dut_resetn released
    loads = h.fabric.hdr(HDR_MMCM_LOADS)
    h.stop(signal.SIGKILL)
    (tmp_path / "run.marker").unlink()

    h2 = Harnessd(BIN, tmp_path, static_id=SID, s0=SID).start()
    try:
        assert "resets RELEASED" in h2.log().split("--- MPS3 shell services")[-1]
        assert h2.fabric.hdr(HDR_MMCM_LOADS) == loads
        assert h2.fabric.hdr(HDR_MMCM_KHZ) == 100000
        assert h2.req({"op": "stats"})["dut_mhz"] == 100
    finally:
        h2.stop()


# --------------------------------------------------------------------------- #
# watchdog, reboot, stage0 CONFIRM
# --------------------------------------------------------------------------- #

def test_watchdog_kicks_only_while_the_loop_runs(hd):
    time.sleep(0.8)
    k0 = hd.fabric.hdr(0x20)
    time.sleep(0.8)
    k1 = hd.fabric.hdr(0x20)
    assert k1 > k0, "no kicks while healthy"
    hd.proc.send_signal(signal.SIGSTOP)                   # B1 item 7, on the host
    try:
        time.sleep(0.3)
        k2 = hd.fabric.hdr(0x20)
        time.sleep(0.8)
        assert hd.fabric.hdr(0x20) == k2, "a stopped harnessd still kicked"
    finally:
        hd.proc.send_signal(signal.SIGCONT)
    time.sleep(0.6)
    assert hd.fabric.hdr(0x20) > k2
    hd.stop()
    assert hd.fabric.rd(0x44B40000, 0x04) & 1 == 0        # clean stop disarms (EWDT2)


def test_reboot_verb_arms_and_the_process_goes(hd):
    r = hd.req({"op": "reboot"})
    assert r["ok"] is True and r["in_ms"] > 2000
    hd.proc.wait(timeout=10)                              # mock: exits at the fallback
    assert "watchdog armed, filesystems synced" in hd.log()


def test_stage0_boot_is_confirmed_when_healthy(hd):
    deadline = time.time() + 6
    while time.time() < deadline and hd.fabric.s0(S0_ATT_CONFIRM) == 0:
        time.sleep(0.2)
    assert hd.fabric.s0(S0_ATT_CONFIRM) == S0_CONFIRM_MAGIC


def test_stage0_boot_is_not_confirmed_outside_a_handoff(tmp_path):
    h = Harnessd(BIN, tmp_path, static_id=SID)
    h.fabric.s0_write_block(SID, phase=5)                 # stage0 is in RESCUE
    h.start()
    try:
        time.sleep(3.0)
        assert h.fabric.s0(S0_ATT_CONFIRM) == 0
        assert "not HANDOFF" in h.log()
    finally:
        h.stop()


def test_mailbox_is_mirrored_to_the_lmb_tail(hd):
    time.sleep(0.3)
    mb = hd.fabric.mailbox()
    assert mb[0] == 0xD1A6C0DE and mb[1] == 9             # magic, diag v9 (D13)
    assert mb[0x7C // 4] == 13                            # svc_count (+ "usd")
    assert mb[0xB4 // 4] == 0xB0070004                    # usd_boot: the latch, decided "none"


# --------------------------------------------------------------------------- #
# UART bridge 6930 (mock loopback) and the idle policy
# --------------------------------------------------------------------------- #

def test_uart_bridge_relays(hd):
    s = socket.create_connection(("127.0.0.1", hd.port(6930)), timeout=3)
    s.sendall(b"hello")
    got = b""
    deadline = time.time() + 3
    while len(got) < 5 and time.time() < deadline:
        got += s.recv(16)
    s.close()
    assert got == b"hello"


def _recv_for(s, seconds):
    s.settimeout(0.1)
    got = b""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            chunk = s.recv(256)
        except socket.timeout:
            continue
        if not chunk:
            break
        got += chunk
    return got


def test_uart_no_client_output_is_drained_not_backpressured(hd):
    """ILA-mint #16: with nobody on 6930 the DUT's console is drained and dropped
    (tready never held low), and the next client reads only NEW output."""
    hd.fabric.uart_inject(0, b"boot banner nobody reads\r\n")
    deadline = time.time() + 3
    while hd.fabric.uart_pending(0) and time.time() < deadline:
        time.sleep(0.02)
    assert hd.fabric.uart_pending(0) == 0
    assert "uart0 (6930): no client -- DUT output is drained and dropped" in hd.log()
    s = socket.create_connection(("127.0.0.1", hd.port(6930)), timeout=3)
    try:
        time.sleep(0.2)
        assert "26 byte(s) of DUT output were dropped" in hd.log()
        hd.fabric.uart_inject(0, b"fresh")
        assert _recv_for(s, 1.0) == b"fresh"
    finally:
        s.close()


def test_uart_swap_flushes_the_previous_rms_bytes(hd):
    """ILA-mint #16: bytes left in the bridge at the swap are the previous RM's;
    a client connected across the swap never reads them as the new RM's."""
    s = socket.create_connection(("127.0.0.1", hd.port(6930)), timeout=3)
    try:
        time.sleep(0.2)
        clr = frame(clearing_payload(), kind=0, static_id=SID, rm_id=0x44)
        par = frame(partial_payload(0x44), kind=1, static_id=SID, rm_id=0x44)
        with hd.ctl() as c:
            c.send({"op": "swap", "rm": "rm68", "src": "tcp"})
            time.sleep(0.2)                                   # gated, awaiting the clearing
            hd.fabric.uart_inject(0, b"rm_uart_echo rea")     # the silicon capture's 16 bytes
            time.sleep(0.2)
            assert hd.fabric.uart_pending(0) == 16             # gated: not relayed, not drained
            for data in (clr, par):
                pusher.tcp_send(data, "127.0.0.1", hd.port(6910))
                time.sleep(0.2)
            assert json.loads(c.line())["verified"] is True
        time.sleep(0.2)
        assert "uart0 (6930): swap ungated -- flushed 16 stale byte(s)" in hd.log()
        hd.fabric.uart_inject(0, b"nanosoc boot\r\n")         # the new RM, after reset dut
        assert _recv_for(s, 1.0) == b"nanosoc boot\r\n"
    finally:
        s.close()


def test_uart_input_pacing_flag(tmp_path):
    """#17 (optional, harnessd --uart-pace-ms): client->DUT bytes >= N ms apart."""
    h = Harnessd(BIN, tmp_path, static_id=SID, extra=["--uart-pace-ms", "50"]).start()
    s = socket.create_connection(("127.0.0.1", h.port(6930)), timeout=3)
    try:
        assert "input paced at 50 ms/byte" in h.log()
        t0 = time.time()
        s.sendall(b"abcdef")                                  # the mock loops TX back to RX
        got = b""
        while len(got) < 6 and time.time() - t0 < 5:
            got += _recv_for(s, 0.1)
        elapsed = time.time() - t0
    finally:
        s.close()
        h.stop()
    assert got == b"abcdef"
    assert elapsed >= 0.25, f"6 bytes at 50 ms/byte arrived in {elapsed:.3f} s"


def _cpu_s(pid):
    f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    return (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")


def test_idle_does_not_spin(hd, capsys):
    time.sleep(1.0)
    c0, t0 = _cpu_s(hd.proc.pid), time.time()
    time.sleep(5.0)
    c1, t1 = _cpu_s(hd.proc.pid), time.time()
    frac = (c1 - c0) / (t1 - t0)
    d = hd.req({"op": "diag"})
    with capsys.disabled():
        print(f"\n[harnessd idle] CPU {100 * frac:.2f}% of one core over {t1 - t0:.1f} s; "
              f"worst pass {d['pass_max_us']} us, worst service {d['svc_max_us']} us "
              f"(ix {d['svc_max_ix']})")
    assert frac < 0.05, f"idle harnessd burns {100 * frac:.1f}% CPU"


# --------------------------------------------------------------------------- #
# IMAGE / STAGE0 / HOST hand-offs (HARNESSD_CONTRACT §5.3, §7, §9)
# --------------------------------------------------------------------------- #

def test_confirm_waits_for_image_boot_health(tmp_path):
    h = Harnessd(BIN, tmp_path, static_id=SID)
    (tmp_path / "boot-health").write_text("healthy=0\nreasons=no-sshd\n")
    h.start()
    try:
        time.sleep(3.2)
        assert h.fabric.s0(S0_ATT_CONFIRM) == 0, "confirmed an unhealthy boot"
        assert "does not say healthy=1" in h.log()
        (tmp_path / "boot-health").write_text("healthy=1\n")
        deadline = time.time() + 4
        while time.time() < deadline and h.fabric.s0(S0_ATT_CONFIRM) == 0:
            time.sleep(0.2)
        assert h.fabric.s0(S0_ATT_CONFIRM) == S0_CONFIRM_MAGIC
    finally:
        h.stop()


def test_first_kick_is_early_when_stage0_armed_the_watchdog(tmp_path):
    h = Harnessd(BIN, tmp_path, static_id=SID)
    h.fabric.wr(0x44B40000, 0x04, 1)                       # TWCSR1.EWDT2 (stage0 did this)
    h.fabric.wr(0x44B40000, 0x00, 2)                       # TWCSR0.EWDT1
    k0 = h.fabric.hdr(0x20)
    h.start()
    try:
        log = h.log()
        first = log.index("wdog: FIRST KICK at os_up_ms=")
        assert "right after the register backend opened" in log[first:first + 200]
        assert first < log.index("coordinator") if "coordinator" in log else True
        assert first < log.index("shell up:"), "the first kick came after the slow init"
        assert h.fabric.hdr(0x20) > k0
    finally:
        h.stop()


def test_wdog_off_means_no_watchdog_in_the_mock(tmp_path):
    h = Harnessd(BIN, tmp_path, static_id=SID, extra=["--wdog", "off"]).start()
    try:
        assert h.req({"op": "reboot"}) == {"ok": False, "err": "no watchdog"}
        assert h.proc.poll() is None
    finally:
        h.stop()


def test_claim_runs_keys_sync_and_identify_uses_the_published_fingerprint(tmp_path):
    h = Harnessd(BIN, tmp_path, static_id=SID)
    sync = tmp_path / "keys-sync"
    sync.write_text(f"#!/bin/sh\ntouch {tmp_path}/synced\n")
    sync.chmod(0o755)
    fp = "SHA256:publishedpublishedpublishedpublishedpubli"
    (tmp_path / "host_key_sha256").write_text(fp + "\n")
    h.start()
    try:
        # a real key (test_sshfp.c's vector), so identify can name it (HM_ANSWERS C1)
        pusher.tftp_put(b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFqCh5Otm9u56BJwiyh9UueSbK3YzqHHtj"
                        b"hWyOJQ9X6J me@pc\n", "127.0.0.1", h.port(69), filename="authorized_keys")
        deadline = time.time() + 3
        while time.time() < deadline and not (tmp_path / "synced").exists():
            time.sleep(0.05)
        assert (tmp_path / "synced").exists(), "mps3-keys-sync was not run after the claim"
        r = json.loads(_identify(h, b'{"op":"identify","v":1,"nonce":"00aa11bb"}'))
        assert r["ssh"] == {"claimed": True, "host_key_sha256": fp,
                            "key_sha256": "SHA256:s6n1vtZIZ6d1cBnloLKFQw3sQPxxRouFIgZLEdo3gT0"}
    finally:
        h.stop()


UIO_BIN = os.environ.get("HARNESSD_UIO_BIN", str(HERE.parent / "build" / "host-uio" / "mps3-harnessd"))


@pytest.mark.skipif(not Path(UIO_BIN).exists(), reason="make host-uio")
@pytest.mark.parametrize("why", ["no-uio", "strict-missing", "flag"])
def test_no_fabric_idles_instead_of_crash_looping(tmp_path, why):
    sysfs = tmp_path / "sys"
    sysfs.mkdir()
    extra = ["--uio-sysfs", str(sysfs), "--uio-devdir", str(tmp_path)]
    if why == "strict-missing":            # one unrelated device: the required ones are missing
        d = sysfs / "uio0" / "maps" / "map0"
        d.mkdir(parents=True)
        (d / "addr").write_text("0x44b30000\n")
        (d / "size").write_text("0x10000\n")
        (tmp_path / "uio0").write_bytes(b"\0" * 0x10000)
    if why == "flag":
        extra.append("--no-hw")
    h = Harnessd(UIO_BIN, tmp_path / "hd", static_id=SID, extra=extra)
    # The image manifest must reach identify in NO-HW mode too (the same provider
    # as `version`); IMAGE's QEMU proof caught "0.0.0" here.
    (tmp_path / "hd" / "version").write_text("format=1\nimpl=linux\nharness=1.0.0\n"
                                             "ver32=0x01000000\nsha=a1f5b6e0\ndirty=0\n")
    h.args = [a for a in h.args if a not in ("--mock-fabric", str(h.fabric_path))]
    h.proc = subprocess.Popen(h.args, stdout=open(h.log_path, "ab"), stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 5
        while time.time() < deadline and "NO-HW MODE" not in h.log():
            time.sleep(0.05)
        assert "NO-HW MODE" in h.log(), h.log()
        time.sleep(1.5)
        assert h.proc.poll() is None, "harnessd exited: init would respawn it in a loop"
        assert h.req({"op": "ping"})["err"].startswith("no fabric:")
        for i in range(50):                  # reap before refuse holds in this loop too
            ln = _line_on_fresh_connection(h.port(6900), b'{"op":"ping"}\n')
            assert ln.startswith(b'{"ok":false,"err":"no fabric:'), (i, ln)
        r = json.loads(_identify(h, b'{"op":"identify","v":1,"nonce":"feedface"}'))
        assert r["mode"] == "nohw" and r["shell_id"] == "0x00000000"
        assert r["harness"] == "1.0.0", r            # the manifest, not "0.0.0"
        cpu = _cpu_s(h.proc.pid)
        time.sleep(2.0)
        assert _cpu_s(h.proc.pid) - cpu < 0.05, "no-hw idle is spinning"
    finally:
        h.stop()


# --------------------------------------------------------------------------- #
# D13: the overlay store on the user microSD, over the POSIX provider on a
# temp-file card (the whole-disk node the rv32 build opens as /dev/mmcblk0)
# --------------------------------------------------------------------------- #

RM_LED = 0x0100001E
CARD_MB = 64


def _card(path: Path) -> Path:
    """A blank card image: 64 MiB of zeros (sparse)."""
    with open(path, "wb") as f:
        f.truncate(CARD_MB * 1024 * 1024)
    return path


def _usd(h):
    return h.req({"op": "usd"})


def _wait_boot(h, want, timeout=10.0):
    deadline = time.time() + timeout
    st = None
    while time.time() < deadline:
        st = _usd(h)
        if st["boot"] != "pending" and st["state"] != "init":
            break
        time.sleep(0.1)
    assert st["boot"] == want, (st, h.log())
    return st


def _commit(h, rm_id, clr_payload, par_payload):
    clr = frame(clr_payload, kind=0, static_id=SID, rm_id=rm_id)
    par = frame(par_payload, kind=1, static_id=SID, rm_id=rm_id)
    import zlib
    req = {"op": "commit", "rm": "led", "src": "tcp", "rm_id": f"0x{rm_id:08x}",
           "static_id": f"0x{SID:08x}", "clear_len": len(clr_payload),
           "clear_crc": f"0x{zlib.crc32(clr_payload) & 0xFFFFFFFF:08x}",
           "part_len": len(par_payload),
           "part_crc": f"0x{zlib.crc32(par_payload) & 0xFFFFFFFF:08x}"}
    with h.ctl() as c:
        c.send(req)
        time.sleep(0.2)                                   # parked: no answer yet
        pusher.tcp_send(clr, "127.0.0.1", h.port(6910))
        pusher.tcp_send_windowed(par, "127.0.0.1", h.port(6910))
        return json.loads(c.line())


def test_usd_no_card_on_a_host_build_by_default(hd):
    # a host build never opens a disk unless told to (--usd-dev)
    _wait_boot(hd, "none")
    assert _usd(hd) == {"ok": True, "present": False, "state": "none", "text": "none",
                        "boot": "none"}
    assert "no card device configured" in hd.log()
    r = hd.req({"op": "usd", "action": "format", "confirm": "erase"})
    assert r == {"ok": False, "err": "no card"}


def test_harnessd_never_links_the_bare_metal_driver():
    # Under Linux the kernel owns usd_spi (0x44A4): usd.c / ovl_bdev_usd.c must
    # never be in the daemon, and nothing may map that page.
    syms = subprocess.run(["nm", BIN], capture_output=True, text=True, check=True).stdout
    for bad in (" usd_init", " usd_poll", " ovl_bdev_usd_bind"):
        assert bad not in syms, bad
    assert " ovl_bdev_posix_open" in syms


def test_usd_card_lifecycle_commit_and_power_on_load(tmp_path):
    card = _card(tmp_path / "card.img")
    h = Harnessd(BIN, tmp_path, static_id=SID, usd_dev=card).start()
    try:
        # a zeroed card is BLANK: foreign (read-only) until formatted; decided "none"
        st = _wait_boot(h, "none")
        assert st["present"] is True and st["state"] == "foreign" and st["card_mb"] == CARD_MB
        # the wipe is disabled under Linux: the card holds the running system
        assert h.req({"op": "usd", "action": "format", "confirm": "erase-all"}) == \
            {"ok": False, "err": "wipe disabled"}
        assert h.req({"op": "usd", "action": "format", "confirm": "erase"}) == \
            {"ok": True, "state": "empty"}
        img = card.read_bytes()
        assert img[510:512] == b"\x55\xAA" and img[446 + 3 * 16 + 4] == 0xDA   # entry 4
        # run the RM, then persist it
        assert _swap(h, RM_LED, "tcp", "tcp")["verified"] is True
        clr, par = clearing_payload(), partial_payload(RM_LED)
        wrong = dict(op="commit", rm="led", src="tcp", rm_id=f"0x{RM_LED ^ 1:08x}",
                     static_id=f"0x{SID:08x}", clear_len=len(clr), clear_crc="0x1",
                     part_len=len(par), part_crc="0x2")
        assert h.req(wrong) == {"ok": False, "err": "rm mismatch"}
        assert _commit(h, RM_LED, clr, par) == {"ok": True, "slot": "A"}, h.log()
        st = _usd(h)
        assert st["state"] == "valid" and st["text"] == "led [A]"
        assert st["default"] == {"rm_id": f"0x{RM_LED:08x}", "static_id": f"0x{SID:08x}",
                                 "slot": "A"}
        assert st["boot"] == "none"                     # this configuration's decision stands
        w0 = h.fabric.hdr(0x1C)
    finally:
        h.stop()

    # a RESPAWN (same fabric = same configuration): never re-decides, never loads
    h2 = Harnessd(BIN, tmp_path, static_id=SID, s0=SID, usd_dev=card).start()
    try:
        time.sleep(1.0)
        assert _usd(h2)["boot"] == "none"
        assert h2.fabric.hdr(0x1C) == w0                 # not one ICAP word
        assert "already taken in this FPGA configuration" in h2.log()
    finally:
        h2.stop()

    # a RECONFIGURATION (a fresh fabric: the LMB tail comes up zero): the default
    # loads at power-on through the swap FSM's "usd" source
    fresh = tmp_path / "reconf"
    h3 = Harnessd(BIN, fresh, static_id=SID, usd_dev=card).start()
    try:
        st = _wait_boot(h3, "loaded")
        assert st["state"] == "valid"
        assert h3.req({"op": "ping"})["rm_id"] == f"0x{RM_LED:08x}"
        assert h3.fabric.hdr(0x10) == RM_LED               # the mock fabric's loaded RM
        assert h3.fabric.mailbox()[0xB4 // 4] == 0xB0070002  # usd_boot: loaded
        # the loaded RM's clearing is resident (RAM) and persisted for a restart
        assert h3.req({"op": "stats"})["clr_ok"] is True
        assert (fresh / "state" / "resident.id").read_text().startswith(f"rm_id=0x{RM_LED:08x}")
        words = h3.fabric.hdr(0x1C)
    finally:
        h3.stop()

    # ...and a respawn of THAT configuration keeps "loaded" without reloading
    h4 = Harnessd(BIN, fresh, static_id=SID, s0=SID, usd_dev=card).start()
    try:
        time.sleep(1.0)
        assert _usd(h4)["boot"] == "loaded"
        assert h4.fabric.hdr(0x1C) == words
        assert h4.req({"op": "ping"})["rm_id"] == f"0x{RM_LED:08x}"
    finally:
        h4.stop()


def test_usd_pb1_held_at_the_boot_check_skips(tmp_path):
    card = _card(tmp_path / "card.img")
    h = Harnessd(BIN, tmp_path, static_id=SID, usd_dev=card).start()
    try:
        _wait_boot(h, "none")
        assert h.req({"op": "usd", "action": "format", "confirm": "erase"})["ok"] is True
        assert _swap(h, RM_LED, "tcp", "tcp")["verified"] is True
        assert _commit(h, RM_LED, clearing_payload(), partial_payload(RM_LED))["ok"] is True
    finally:
        h.stop()
    fresh = tmp_path / "reconf"
    h2 = Harnessd(BIN, fresh, static_id=SID, usd_dev=card)
    h2.fabric.wr(0x44AD0000, 0x04, 1 << 11)              # CLCDKVM.STATUS.PB_LEVEL: held
    h2.start()
    try:
        st = _wait_boot(h2, "skipped")
        assert st["text"] == "skipped"
        assert h2.fabric.hdr(0x1C) == 0                   # nothing streamed
        assert h2.req({"op": "ping"})["rm_id"] == "0x00000000"
    finally:
        h2.stop()


# --------------------------------------------------------------------------- #
# a FRESH FPGA configuration vs the resident cache (lane RESFIX, 2026-10-01)
#
# --state-dir is on the card (/persist/mps3), so the resident record outlives a
# reconfiguration (MCC REBOOT / power-on) while the fabric does not: board 1
# reported dbg_demo across two full reconfigurations, and after a keep-on-card
# MCC REBOOT it reported nanosoc while the card said failed:timeout. Here a NEW
# --mock-fabric file is a reconfiguration (the LMB tail, so the latch, comes up
# zero) and the SAME file is a respawn; the state dir is kept across both.
# --------------------------------------------------------------------------- #

RM_X = 0x00000042


def _swap_words(h, rm_id):
    """Swap to rm_id; returns the ICAP words it streamed (the outgoing clearing +
    the partial), so a test can tell WHICH clearing the swap used."""
    w0 = h.fabric.hdr(0x1C)
    assert _swap(h, rm_id, "tcp", "tcp")["verified"] is True, h.log()
    return h.fabric.hdr(0x1C) - w0


def _reconfigure(tmp_path):
    """An MCC REBOOT: a new OS boot (run marker gone) on a new fabric file."""
    (tmp_path / "run.marker").unlink()
    return "fabric.reconf"


def test_fresh_configuration_ignores_the_resident_cache(tmp_path):
    h = Harnessd(BIN, tmp_path, static_id=SID).start()
    try:
        from_grey = _swap_words(h, RM_X)            # greybox clearing + partial
        assert (tmp_path / "state" / "resident.id").read_text().startswith(f"rm_id=0x{RM_X:08x}")
    finally:
        h.stop()

    reconf = _reconfigure(tmp_path)
    h2 = Harnessd(BIN, tmp_path, static_id=SID, fabric_name=reconf).start()
    try:
        log = h2.log().split("--- MPS3 shell services")[-1]
        assert "a FRESH FPGA configuration" in log
        assert (f"resident: FRESH FPGA configuration -- the cached record (RM 0x{RM_X:08x}) "
                "predates it and is ignored; the greybox is resident") in log
        assert "(greybox) from a FRESH FPGA configuration; greybox clearing" in log
        assert "the last verified swap (cache)" not in log
        assert h2.req({"op": "ping"})["rm_id"] == "0x00000000"
        assert h2.req({"op": "stats"})["rm"] == "0x00000000"
        assert not (tmp_path / "state" / "resident.id").exists()   # invalidated (see below)
        h2.stop(signal.SIGKILL)

        # A respawn in THIS configuration before any swap is not fresh, and the
        # greybox's RP is still in reset (RM_STATUS not valid): an ignored-but-kept
        # record would come back here. It was invalidated, so it does not.
        h3 = Harnessd(BIN, tmp_path, static_id=SID, s0=SID, fabric_name=reconf).start()
        try:
            log = h3.log().split("--- MPS3 shell services")[-1]
            assert "already taken in this FPGA configuration" in log
            assert "FRESH FPGA configuration --" not in log
            assert "nothing recorded: the greybox assumed" in log
            assert h3.req({"op": "ping"})["rm_id"] == "0x00000000"
            # the next swap clears with the GREYBOX's clearing, not RM_X's
            assert _swap_words(h3, RM_X + 1) == from_grey
            assert (tmp_path / "state" / "resident.id").read_text() \
                .startswith(f"rm_id=0x{RM_X + 1:08x}")              # rewritten by the swap
        finally:
            h3.stop()
    finally:
        h2.stop()


def test_respawn_with_rm_status_invalid_still_trusts_the_cache(tmp_path):
    """Today's behaviour, kept: a WDOG reset clamps the RP (RM_STATUS reads not
    valid) but does not reconfigure -- the latch survives in BRAM, so the cached
    record, verified when it was written, is still the resident RM."""
    h = Harnessd(BIN, tmp_path, static_id=SID).start()
    try:
        from_grey = _swap_words(h, RM_X)
    finally:
        h.stop(signal.SIGKILL)
    h.fabric.fabric_reset()                               # the watchdog fires
    assert h.fabric.mailbox()[0xB4 // 4] >> 16 == 0xB007    # the latch: same configuration
    (tmp_path / "run.marker").unlink()                    # the OS boots again

    h2 = Harnessd(BIN, tmp_path, static_id=SID, s0=SID).start()
    try:
        log = h2.log().split("--- MPS3 shell services")[-1]
        assert "already taken in this FPGA configuration" in log
        assert "FRESH FPGA configuration --" not in log
        assert (f"resident: RM 0x{RM_X:08x} from the last verified swap (cache); "
                "its cached clearing restored") in log
        assert "PARKED SAFE" in log
        assert h2.req({"op": "ping"})["rm_id"] == f"0x{RM_X:08x}"
        # ...and the next swap clears with RM_X's clearing (64 words), not the greybox's (16)
        assert _swap_words(h2, RM_X + 1) == from_grey + 64 - 16
    finally:
        h2.stop()


def test_fresh_configuration_power_on_load_is_not_suppressed_by_a_stale_cache(tmp_path):
    card = _card(tmp_path / "card.img")
    h = Harnessd(BIN, tmp_path, static_id=SID, usd_dev=card).start()
    try:
        _wait_boot(h, "none")
        assert h.req({"op": "usd", "action": "format", "confirm": "erase"})["ok"] is True
        assert _swap(h, RM_LED, "tcp", "tcp")["verified"] is True
        assert _commit(h, RM_LED, clearing_payload(), partial_payload(RM_LED))["ok"] is True
        _swap_words(h, RM_X)                              # the LAST swap: the cache says RM_X
        assert (tmp_path / "state" / "resident.id").read_text().startswith(f"rm_id=0x{RM_X:08x}")
    finally:
        h.stop()

    h2 = Harnessd(BIN, tmp_path, static_id=SID, usd_dev=card,
                  fabric_name=_reconfigure(tmp_path)).start()
    try:
        st = _wait_boot(h2, "loaded")                     # NOT "none" ("not ours")
        assert st["state"] == "valid"
        assert h2.req({"op": "ping"})["rm_id"] == f"0x{RM_LED:08x}"
        assert h2.fabric.hdr(0x10) == RM_LED
        assert h2.fabric.mailbox()[0xB4 // 4] == 0xB0070002   # usd_boot: loaded
        assert f"the cached record (RM 0x{RM_X:08x}) predates it" in h2.log()
        assert (tmp_path / "state" / "resident.id").read_text().startswith(f"rm_id=0x{RM_LED:08x}")
    finally:
        h2.stop()
