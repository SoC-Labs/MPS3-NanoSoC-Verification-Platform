"""FakeShell's opt-in Linux-harness behaviour, and the pyverify client halves of it.

Linux harness plan §10 ("Harness Manager interface"): the double gains a
``linux`` profile, a ``hung`` mode, the UDP 6899 ``identify`` responder, the
TOFU ``authorized_keys`` claim and a stage0 ``rescue`` mode -- all OPT-IN, with
the default untouched (test_api_stability.py pins the default).
"""
from __future__ import annotations

import json
import socket
import time

import pytest

from pyverify.client import (IMPL_BARE_METAL, IMPL_LINUX, ShellClient, ShellProtocolError)
from pyverify.identify import IdentifyError, identify
from pyverify.testing.fakeshell import (LINUX_OMITTED_DIAG_KEYS, LINUX_PROFILE_FEATURES,
                                        STATS_KEYS, FakeShell, TftpError, tftp_put)


class _Scripted:
    def __init__(self, *lines):
        self._lines = list(lines)
        self.sent = []

    def send_line(self, payload):
        self.sent.append(json.loads(payload))

    def recv_line(self):
        return self._lines.pop(0).encode()

    def close(self):
        pass


def _client(*lines):
    return ShellClient("x", transport=_Scripted(*lines))


# --------------------------------------------------------------------------- #
# version.impl parsing (client)
# --------------------------------------------------------------------------- #

BARE = ('{"ok":true,"harness":"1.0.0","ver32":"0x01000000","sha":"abcd1234","dirty":0,'
        '"lmb_kb":1024,"features":["clcd"],"usr_access":null,"skew":null}')


def test_impl_absent_is_bare_metal():
    v = _client(BARE).version()
    assert v.impl == IMPL_BARE_METAL and v.is_bare_metal and not v.is_linux
    assert v.extra == {}


def test_impl_linux_and_128k_lmb():
    v = _client(BARE.replace('"lmb_kb":1024', '"lmb_kb":128')[:-1] + ',"impl":"linux"}').version()
    assert v.impl == IMPL_LINUX and v.is_linux and v.lmb_kb == 128


def test_impl_null_or_empty_reads_bare_metal():
    for tail in (',"impl":null}', ',"impl":""}'):
        assert _client(BARE[:-1] + tail).version().impl == IMPL_BARE_METAL


def test_impl_non_string_is_a_protocol_error():
    with pytest.raises(ShellProtocolError):
        _client(BARE[:-1] + ',"impl":7}').version()


def test_unknown_version_keys_are_kept_not_dropped_and_do_not_raise():
    v = _client(BARE[:-1] + ',"impl":"linux","image":{"kernel":"6.18.7"},"new_thing":1}').version()
    assert v.extra == {"image": {"kernel": "6.18.7"}, "new_thing": 1}
    # extra does not take part in equality (a newer server stays == to an older one)
    assert v == _client(BARE[:-1] + ',"impl":"linux"}').version()


# --------------------------------------------------------------------------- #
# The linux profile over real sockets
# --------------------------------------------------------------------------- #

@pytest.fixture
def linux():
    f = FakeShell.ephemeral(profile="linux").start()
    try:
        yield f
    finally:
        f.stop()


def test_linux_version_line(linux):
    with ShellClient(linux.host, linux.control_port, timeout=5.0) as c:
        v = c.version()
    assert v.impl == "linux" and v.lmb_kb == 128
    assert v.features == LINUX_PROFILE_FEATURES
    raw = linux.handle_control({"op": "version"})
    assert list(raw)[-1] == "impl", "impl must be the LAST key (HARNESSD_CONTRACT §9.1)"


def test_linux_diag_omits_sourceless_keys_and_present_says_so(linux):
    with ShellClient(linux.host, linux.control_port, timeout=5.0) as c:
        d = c.diag()
    for k in LINUX_OMITTED_DIAG_KEYS:
        assert not d.has(k), k
        assert getattr(d, k) == 0      # compat: the field still reads 0
    assert d.has("icap_bytes") and d.has("svc_count")
    assert "ok" not in d.present


def test_bare_metal_diag_has_every_key():
    f = FakeShell()
    raw = f.handle_control({"op": "diag"})
    d = _client(json.dumps(raw)).diag()
    # 42 (v0.9.2) + diag.h v9's two D13 words (svc_us_6, usd_boot).
    assert len(d.present) == 44 and d.has("pbuf_free") and d.has("usd_boot")


def test_linux_stats_shape_order_and_os_up_ms(linux):
    raw = linux.handle_control({"op": "stats"})
    keys = [k for k in raw if k != "ok"]
    assert keys[:len(STATS_KEYS)] == list(STATS_KEYS)
    assert keys[-1] == "os_up_ms"
    with ShellClient(linux.host, linux.control_port, timeout=5.0) as c:
        s = c.stats()
    assert s.ok and s.os_up_ms is not None and s.os_up_ms >= s.up_ms
    assert s.has("os_up_ms") and not s.has("ok")


def test_bare_metal_stats_line_has_no_os_up_ms():
    line = ('{"ok":true,"up_ms":5,"sid":"0x1","rm":"0x0","rm_ok":true,"lock":false,'
            '"clk_sel":0,"mmcm":true,"clk_alive":true,"dut_rst":true,"rp_rst":true,'
            '"decpl":false,"link":true,"spd":100,"fdx":true,"mac":"0002f7ef441c",'
            '"swap":"idle","swap_ok":false,"swap_n":0,"icap":0,"rxdrop":0,"txerr":0,'
            '"swap_err":"","clr_ok":true,"dut_mhz":50,"svc_max_us":0,"svc_skipped":0}')
    s = _client(line).stats()
    assert s.os_up_ms is None and not s.has("os_up_ms")


def test_linux_log_touch_cal_reboot(linux):
    log = linux.handle_control({"op": "log", "off": 0})
    assert log["ok"] and bytes.fromhex(log["data"]).startswith(b"--- MPS3")
    assert linux.handle_control({"op": "touch_cal", "act": "get"})["shift"] == 12
    bad = dict(op="touch_cal", act="set", ax=0, bx=0, cx=0, ay=0, by=0, cy=0, shift=12)
    assert linux.handle_control(bad) == {"ok": False, "err": "singular"}
    time.sleep(0.6)                              # give up_ms something to fall from
    up0 = linux.up_ms()
    assert up0 >= 500
    linux.reboot_in_ms = 50
    r = linux.handle_control({"op": "reboot"})
    assert r == {"ok": True, "in_ms": 50}
    deadline = time.monotonic() + 10
    while linux.up_ms() >= up0:                  # up_ms restarts: the reboot witness
        assert time.monotonic() < deadline, "up_ms never restarted after reboot"
        time.sleep(0.02)


def test_reboot_without_watchdog_declines():
    f = FakeShell(profile="linux", has_watchdog=False)
    assert f.handle_control({"op": "reboot"}) == {"ok": False, "err": "no watchdog"}


# --------------------------------------------------------------------------- #
# Liveness: hung = accepts, never answers ("wedged"); dead = refused ("offline")
# --------------------------------------------------------------------------- #

def test_hung_accepts_but_never_replies():
    f = FakeShell.ephemeral(profile="linux", hung=True).start()
    try:
        s = socket.create_connection((f.host, f.control_port), timeout=2)   # handshake OK
        s.sendall(b'{"op":"ping"}\n')
        s.settimeout(0.5)
        with pytest.raises(socket.timeout):
            s.recv(64)                                                    # ...no reply
        # the hang clears -> the buffered request is served, like a resumed process
        f.hung = False
        s.settimeout(3)
        assert b'"shell_id"' in s.recv(256)
        s.close()
    finally:
        f.stop()


def test_dead_is_refused_and_revives_on_the_same_port():
    f = FakeShell.ephemeral(profile="linux").start()
    port = f.control_port
    f.stop()                                   # harnessd died: nothing listens
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection((f.host, port), timeout=2)
    f.start()                                  # respawned
    try:
        assert f.control_port == port
        with ShellClient(f.host, port, timeout=5.0) as c:
            assert c.ping().ok
    finally:
        f.stop()


# --------------------------------------------------------------------------- #
# identify (UDP 6899)
# --------------------------------------------------------------------------- #

def test_identify_linux_reply(linux):
    r = identify(linux.host, linux.identify_port, nonce="0123456789abcdef")
    raw = r.raw
    assert r.ok and r.nonce == "0123456789abcdef" and r.impl == "linux"
    # HARNESSD_CONTRACT §9.2's order, with mode/os_up_ms where HOST_CONTRACT says
    assert list(raw) == ["ok", "op", "v", "nonce", "board", "mode", "mac", "ip", "dhcp",
                         "shell_id", "rm_id", "harness", "proto", "impl", "up_ms",
                         "os_up_ms", "ssh", "label", "ports"]   # v0.16: label before ports
    assert raw["mode"] == "run" and raw["shell_id"] == "0x%08x" % linux.static_id
    assert r.ssh_claimed is False and r.host_key_sha256.startswith("SHA256:")
    assert r.key_sha256 == "" and list(raw["ssh"]) == ["claimed", "host_key_sha256",
                                                       "key_sha256"]   # HM_ANSWERS C1
    assert "os_up_ms" in raw and raw["os_up_ms"] >= raw["up_ms"]
    assert raw["label"] == "MPS3"                                  # the image default
    assert raw["ports"]["ctrl"] == linux.control_port
    assert len(json.dumps(raw, separators=(",", ":"))) <= 1200


def test_identify_names_the_claiming_key():
    """ssh.key_sha256 (HM_ANSWERS C1): the claim's FIRST key, in OpenSSH's form --
    the vector harnessd's test_sshfp.c holds against ssh-keygen."""
    key = (b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFqCh5Otm9u56BJwiyh9UueSbK3YzqHHtjhWyOJQ9X6J"
           b" me@pc\nssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGVub3RoZXIga2V5IHRoYXQgbmV2ZXIgd2lucyEhISE= x\n")
    f = FakeShell.ephemeral(profile="linux").start()
    try:
        assert identify(f.host, f.identify_port).key_sha256 == ""
        tftp_put(f.host, f.tftp_port, key, filename="authorized_keys")
        r = identify(f.host, f.identify_port)
        assert r.ssh_claimed is True
        assert r.key_sha256 == "SHA256:s6n1vtZIZ6d1cBnloLKFQw3sQPxxRouFIgZLEdo3gT0"
    finally:
        f.stop()


def test_identify_malformed_is_silent_and_rate_limited():
    f = FakeShell.ephemeral(profile="linux", identify_rate_per_s=3).start()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.4)
        s.sendto(b'{"op":"identify","v":1,"nonce":"zz"}', (f.host, f.identify_port))
        with pytest.raises(socket.timeout):
            s.recvfrom(2048)                    # malformed nonce: silent
        req = b'{"op":"identify","v":1,"nonce":"0011223344556677"}'
        for _ in range(8):
            s.sendto(req, (f.host, f.identify_port))
        got = 0
        s.settimeout(0.5)
        try:
            while True:
                s.recvfrom(2048)
                got += 1
        except socket.timeout:
            pass
        assert 1 <= got <= 4, got               # burst of 3, not 8
        s.close()
    finally:
        f.stop()


def test_identify_is_off_by_default_and_silent_when_hung():
    assert not FakeShell().identify_enabled
    f = FakeShell.ephemeral(profile="linux", hung=True).start()
    try:
        with pytest.raises(IdentifyError):
            identify(f.host, f.identify_port, timeout=0.3, retries=0)
    finally:
        f.stop()


# --------------------------------------------------------------------------- #
# TOFU first-key claim
# --------------------------------------------------------------------------- #

def test_tofu_claim_once_then_error_2(linux):
    tftp_put(linux.host, linux.tftp_port, b"ssh-ed25519 AAAA me@host\n", filename="authorized_keys")
    assert linux.ssh_claimed and linux.authorized_keys.startswith(b"ssh-ed25519")
    assert identify(linux.host, linux.identify_port).ssh_claimed is True
    with pytest.raises(TftpError) as ei:
        tftp_put(linux.host, linux.tftp_port, b"ssh-ed25519 BBBB evil\n", filename="authorized_keys")
    assert "error 2" in str(ei.value)
    assert b"evil" not in linux.authorized_keys


def test_tofu_is_not_in_the_bare_metal_default():
    f = FakeShell.ephemeral().start()
    try:
        with pytest.raises(TftpError):
            # routed to the bitstream path: no MPS3 header -> rejected
            tftp_put(f.host, f.tftp_port, b"ssh-ed25519 AAAA me\n" * 2, filename="authorized_keys")
        assert not f.ssh_claimed
    finally:
        f.stop()


# --------------------------------------------------------------------------- #
# stage0 rescue mode
# --------------------------------------------------------------------------- #

def test_rescue_mode_tftp_identify_and_no_6900():
    f = FakeShell.ephemeral(profile="linux", mode="rescue").start()
    try:
        assert f.console_ports == {}
        r = identify(f.host, f.identify_port)
        assert r.raw["mode"] == "rescue" and "impl" not in r.raw and "ssh" not in r.raw
        # STAGE0_CONTRACT §7's exact key order
        assert list(r.raw) == ["ok", "op", "v", "nonce", "board", "mode", "shell_id",
                               "ip", "mac", "reason", "ports"]
        assert r.raw["ports"] == {"tftp": f.tftp_port}
        tftp_put(f.host, f.tftp_port, b"\x5a" * 1500, filename="blob.img")
        assert f.rescue_pushes == [b"\x5a" * 1500]
        # nothing listens on a control port in rescue
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
        s.close()
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection((f.host, free), timeout=1)
    finally:
        f.stop()


def test_rescue_status_block_is_stage0_layout():
    from pyverify.mailbox import decode_stage0_status
    f = FakeShell(profile="linux", mode="rescue")
    import struct
    st = decode_stage0_status(struct.unpack("<64I", f.rescue_status_block()))
    assert st.valid and st.summary()["phase"] == "RESCUE"
    assert st.summary()["rescue_state"] == "LISTEN"


def test_rescue_hooks_a_bound_sink_and_status_fields_by_name():
    """BOARD-RUNNER's hooks (additive): a fake stage0 that REFUSES a blob, one that
    captures it itself, and status-block fields by name -- without wrapping
    _tftp_receive_file."""
    import struct
    from pyverify.mailbox import decode_stage0_status
    seen = []

    def refuse(data):
        seen.append(len(data))
        return False, "rejected: table CRC"

    f = FakeShell.ephemeral(profile="linux", mode="rescue", rescue_sink=refuse,
                            rescue_status={"boot_count": 7, "rescue_rejects": 2}).start()
    try:
        with pytest.raises(TftpError):
            tftp_put(f.host, f.tftp_port, b"\x5a" * 700, filename="blob.img")
        assert seen == [700] and f.rescue_pushes == []           # the default sink did not run
        st = decode_stage0_status(struct.unpack("<64I", f.rescue_status_block()))
        assert (st.fields["boot_count"], st.fields["rescue_rejects"]) == (7, 2)
        assert st.valid and st.summary()["phase"] == "RESCUE"    # the defaults still there
        f.rescue_status["boot_count"] = 8                        # mutable mid-test
        st = decode_stage0_status(struct.unpack("<64I", f.rescue_status_block(fails_a=1)))
        assert (st.fields["boot_count"], st.fields["fails_a"]) == (8, 1)
    finally:
        f.stop()

    class Capturing(FakeShell):                                  # a subclass override
        def _rescue_sink(self, data):
            self.rescue_pushes.append(b"seen:" + data[:4])
            return True, ""

    g = Capturing.ephemeral(profile="linux", mode="rescue").start()
    try:
        tftp_put(g.host, g.tftp_port, b"ABCDEFGH", filename="blob.img")
        assert g.rescue_pushes == [b"seen:ABCD"]
    finally:
        g.stop()
    with pytest.raises(ValueError, match="unknown stage0 status fields"):
        FakeShell(profile="linux", mode="rescue", rescue_status={"boot_cnt": 1})
    with pytest.raises(ValueError, match="unknown stage0 status fields"):
        FakeShell(profile="linux", mode="rescue").rescue_status_block(nope=1)


def test_linux_features_follow_harnessd_s_build_flags():
    """LINUX_PROFILE_FEATURES vs the flags harnessd's Makefile actually builds the
    product with: `windowed` must be listed iff MPS3_CFG_AGENT_WINDOWED is set
    (it is not, on purpose), and each flag-derived feature iff its flag. A deploy
    decides windowed-vs-plain from this list, so a drift here is a wrong push.
    The full list is also pinned against a host-built harnessd's `version`
    (harnessd/tests/test_fakeshell_parity.py)."""
    import re
    from pathlib import Path
    mk = Path(__file__).resolve().parents[3] / "src/linux_harness/sw/harnessd/Makefile"
    if not mk.exists():
        pytest.skip("not in the platform tree (no harnessd Makefile)")
    text = mk.read_text()
    base = re.search(r"^BASE_CFLAGS :=(.*?)\n\n", text, re.S | re.M).group(1)
    product = re.search(r"^PRODUCT_FLAGS :=(.*?)\n\n", text, re.S | re.M).group(1)
    flags = set(re.findall(r"-D(\w+(?:=\w+)?)", base + product))
    by_flag = {"MPS3_HAS_CLCD": "clcd", "MPS3_HAS_CLCD_KVM": "clcd_kvm",
               "MPS3_HAS_TOUCH": "touch", "MPS3_HWICAP_FIFO=1": "hwicap_fifo",
               "MPS3_HAS_DUT_EGRESS": "dut_egress", "MPS3_CFG_AGENT_WINDOWED": "windowed"}
    for flag, feat in by_flag.items():
        assert (flag in flags) == (feat in LINUX_PROFILE_FEATURES), (flag, feat, sorted(flags))
    assert "windowed" not in LINUX_PROFILE_FEATURES             # the drift this pins


def test_bad_profile_and_mode_are_refused():
    with pytest.raises(ValueError):
        FakeShell(profile="freertos")
    with pytest.raises(ValueError):
        FakeShell(mode="sleep")
    with pytest.raises(ValueError):
        FakeShell(profile="linux", omit_diag_keys=("not_a_key",))
