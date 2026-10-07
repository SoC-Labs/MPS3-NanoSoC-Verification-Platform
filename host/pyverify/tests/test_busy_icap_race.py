"""The busy-ICAP race: swapping AWAY from a DAP RM on the Linux harness.

Silicon, B1 v4 2026-09-25 (docs/evidence/2026-09-linux-b1-v4/b1_h_xvc.txt):
``pyverify deploy --overlay dbg_demo`` with nanosoc resident failed
``TFTP DATA block 1: server aborted: TFTP ERROR 0: rejected``. The swap FSM first
streams the OUTGOING RM's cached clearing into the ICAP (SWAP_STREAM_CLEARING;
nanosoc's is 167,308 B, seconds on the MicroBlaze V); the host pushes the
incoming clearing (it lands in RAM) and then the partial straight away. The
partial's header arrives while the stream is still running, so the ICAP-direct
sink cannot arm (swap_fsm.c ``icap_direct_begin`` needs SWAP_AWAIT_PARTIAL). On
6910 config_agent PARKS it (RECV_PENDING_ICAP_BEGIN: header read, window left
closed, armed once the FSM gets there); on TFTP there is no window to hold, so
it is REJECTED. harnessd advertises no ``windowed``, so pyverify auto-picked
TFTP -- and hit it.

FakeShell models that window (``clearing_stream_bytes_per_s``), and these tests
pin the fix: the Linux harness is pushed over 6910 (auto), a forced TFTP push to
it re-pushes a partial refused at its header, and bare metal is unchanged.
"""
from __future__ import annotations

import json
import time
import zlib
from pathlib import Path

import pytest

from pyverify import cli
from pyverify.client import ShellClient
from pyverify.overlay import Overlay
from pyverify.pusher import (
    DEFAULT_PUSH_TIMEOUT_S, LINUX_PUSH_TIMEOUT_S, LINUX_TFTP_PARTIAL_RETRY_S,
    BitstreamKind, BitstreamPusher, PushError, PushResult, choose_push,
)
from pyverify.swap import SwapOrchestrator
from pyverify.testing.fakeshell import FakeShell
from pyverify.testing.swap_model import STATIC_ID

#: nanosoc's clearing on the fielded static (B1 v4 console: "persist: resident
#: RM 0x01000001 clearing (167308 B) saved").
NANOSOC_CLEARING_BYTES = 167_308
#: The busy window these tests open: long enough that no loopback push of a
#: small clearing can outlast it, short enough to keep the suite quick.
STREAM_S = 1.5

WINDOWED_FEATURES = ("clcd", "clcd_kvm", "touch", "hwicap_fifo", "windowed")


def make_overlay(root: Path, name: str, rm_id: int, *, clearing_len: int = 128,
                 partial_len: int = 384, static_id: int = STATIC_ID) -> Path:
    """A synthetic overlay in gen_manifest.py's shape."""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    clr = (b"\xC1\x0E\xA2\x00" * (clearing_len // 4 + 1))[:clearing_len // 4 * 4]
    prt = (bytes([rm_id & 0xFF, 0x75, 0x00, 0x0D]) * (partial_len // 4 + 1))[:partial_len // 4 * 4]
    (d / (name + "_clear.bin")).write_bytes(clr)
    (d / (name + ".bin")).write_bytes(prt)
    man = {"schema": 1, "static_id": "0x%08X" % static_id, "rm_id": "0x%08x" % rm_id,
           "rm_name": name,
           "clearing": {"file": name + "_clear.bin", "len": len(clr),
                        "crc32": "0x%08x" % (zlib.crc32(clr) & 0xFFFFFFFF)},
           "partial": {"file": name + ".bin", "len": len(prt),
                       "crc32": "0x%08x" % (zlib.crc32(prt) & 0xFFFFFFFF)},
           "built": "2026-09-25", "vivado": "2026.1"}
    (d / "manifest.json").write_text(json.dumps(man, indent=2) + "\n")
    return d


def dap_resident_shell(profile: str = "linux", **kw) -> FakeShell:
    """A shell with a DAP-RM-sized clearing resident (the boot seed stands in for
    nanosoc's) and the busy-ICAP window on: the next swap streams it for STREAM_S."""
    kw.setdefault("greybox_clearing_len_words", NANOSOC_CLEARING_BYTES // 4)
    kw.setdefault("clearing_stream_bytes_per_s", NANOSOC_CLEARING_BYTES / STREAM_S)
    return FakeShell.ephemeral(profile=profile, **kw).start()


@pytest.fixture
def shells():
    started = []

    def make(*a, **kw):
        f = dap_resident_shell(*a, **kw)
        started.append(f)
        return f

    yield make
    for f in started:
        f.stop()


def _deploy(fake: FakeShell, pusher: BitstreamPusher, ovl: Path, src: str):
    with ShellClient(fake.host, port=fake.control_port, timeout=30.0) as c:
        return SwapOrchestrator(c, pusher).deploy(Overlay.load(ovl), src=src, persist=False)


# --------------------------------------------------------------------------- #
# The fake models the silicon race (so the tests below can fail)
# --------------------------------------------------------------------------- #


def test_the_fake_rejects_a_tftp_partial_inside_the_busy_window(shells, tmp_path):
    """The silicon failure, reproduced: a plain TFTP deploy away from a DAP RM
    dies at the partial's header with TFTP ERROR 0 "rejected"."""
    fake = shells()
    ovl = make_overlay(tmp_path, "dbg_demo", 0x01000009)
    pusher = BitstreamPusher(host=fake.host, transport="tftp", tftp_port=fake.tftp_port)
    with pytest.raises(PushError, match=r"DATA block 1: server aborted: TFTP ERROR 0: rejected"):
        _deploy(fake, pusher, ovl, "tftp")
    assert fake.icap_busy_rejects == 1
    assert fake.accepted_pushes[0].info.kind is BitstreamKind.CLEARING   # the clearing got in


def test_the_fake_parks_a_6910_partial_until_the_stream_is_done(shells, tmp_path):
    fake = shells()
    ovl = make_overlay(tmp_path, "dbg_demo", 0x01000009)
    pusher = BitstreamPusher(host=fake.host, transport="tcp", tcp_port=fake.raw_tcp_port,
                             timeout_s=LINUX_PUSH_TIMEOUT_S)
    t0 = time.monotonic()
    res = _deploy(fake, pusher, ovl, "tcp")
    assert res.verified and int(res.rm_id, 16) == 0x01000009
    assert fake.icap_defer_parks == 1 and fake.icap_busy_rejects == 0
    assert time.monotonic() - t0 >= STREAM_S * 0.9          # it really waited out the stream


def test_the_busy_window_is_opt_in(tmp_path):
    """No rate = an instant stream: every existing scenario is untouched."""
    fake = FakeShell.ephemeral(profile="linux",
                               greybox_clearing_len_words=NANOSOC_CLEARING_BYTES // 4).start()
    try:
        ovl = make_overlay(tmp_path, "dbg_demo", 0x01000009)
        pusher = BitstreamPusher(host=fake.host, transport="tftp", tftp_port=fake.tftp_port)
        assert _deploy(fake, pusher, ovl, "tftp").verified
        assert fake.icap_busy_rejects == fake.icap_defer_parks == 0
    finally:
        fake.stop()


# --------------------------------------------------------------------------- #
# The fix: pyverify deploy (CLI) against the Linux harness
# --------------------------------------------------------------------------- #


def _cli_deploy(fake: FakeShell, ovl: Path, *extra: str):
    return cli.main(["deploy", "--host", fake.host, "--overlay", str(ovl), "--no-persist",
                     "--control-port", str(fake.control_port),
                     "--tftp-port", str(fake.tftp_port),
                     "--tcp-push-port", str(fake.raw_tcp_port), *extra])


def test_cli_deploy_away_from_a_dap_rm_on_linux_succeeds(shells, tmp_path, capsys):
    """THE B1 v4 BLOCKER. Before the fix auto picked TFTP here and exited 3
    ("rejected"); now auto picks 6910, the shell parks the partial, rc 0."""
    fake = shells()
    ovl = make_overlay(tmp_path, "dbg_demo", 0x01000009)
    rc = _cli_deploy(fake, ovl)
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert json.loads(out.strip().splitlines()[-1])["verified"] is True
    assert "deploy: transport=tcp src=tcp windowed=False (version.impl = linux" in err
    assert fake.icap_defer_parks == 1 and fake.icap_busy_rejects == 0
    assert all(e.transport == "tcp" for e in fake.accepted_pushes)


def test_cli_forced_tftp_to_linux_re_pushes_the_refused_partial(shells, tmp_path, capsys):
    """A caller that forces TFTP onto the Linux harness (every flag explicit, as
    the soak's --transport tftp does) still gets through: the shell is asked what
    it is, and a partial refused at its header is re-pushed once the stream ends."""
    fake = shells()
    ovl = make_overlay(tmp_path, "dbg_demo", 0x01000009)
    rc = _cli_deploy(fake, ovl, "--pusher-transport", "tftp", "--src", "tftp", "--no-windowed")
    out, err = capsys.readouterr()
    assert rc == 0, err
    assert "(explicit flags; impl=linux over tftp" in err
    assert fake.icap_busy_rejects >= 1                       # it really hit the race
    assert json.loads(out.strip().splitlines()[-1])["verified"] is True


def test_the_silicon_sequence_led_dbg_demo_nanosoc_dbg_demo(tmp_path, capsys):
    """B1 v4's swaps in order, with the real clearing sizes: led -> dbg_demo ->
    nanosoc (all passed on silicon) -> dbg_demo (the failure). Every one must pass."""
    rate = NANOSOC_CLEARING_BYTES / STREAM_S
    fake = FakeShell.ephemeral(profile="linux", clearing_stream_bytes_per_s=rate).start()
    try:
        seq = [("led", 0x0100001E, 64_604), ("dbg_demo", 0x01000009, 80_872),
               ("nanosoc", 0x01000001, NANOSOC_CLEARING_BYTES), ("dbg_demo", 0x01000009, 80_872)]
        for i, (name, rm_id, clr) in enumerate(seq):
            ovl = make_overlay(tmp_path / str(i), name, rm_id, clearing_len=clr, partial_len=8192)
            rc = _cli_deploy(fake, ovl)
            _, err = capsys.readouterr()
            assert rc == 0, (name, err)
            assert fake.current_rm_id == rm_id
        assert fake.icap_busy_rejects == 0
        assert fake.icap_defer_parks >= 1                    # at least the swap away from nanosoc
    finally:
        fake.stop()


# --------------------------------------------------------------------------- #
# The rule (pyverify.pusher.choose_push) -- one rule for the CLI and Mps3Board
# --------------------------------------------------------------------------- #


class _Ver:
    def __init__(self, features=(), impl="bare-metal", ok=True):
        self.ok, self.features, self.impl, self.err = ok, tuple(features), impl, ""


class _Client:
    def __init__(self, ver=None, exc=None):
        self.ver, self.exc, self.calls = ver, exc, 0

    def version(self):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.ver


def test_choose_push_linux_is_tcp_plain_with_a_long_inactivity_limit():
    c = choose_push(_Client(_Ver(("clcd", "hwicap_fifo", "usd"), impl="linux")))
    assert (c.transport, c.src, c.windowed) == ("tcp", "tcp", False)
    assert c.timeout_s == LINUX_PUSH_TIMEOUT_S >= 30 and c.partial_retry_s == 0.0
    assert c.impl == "linux" and c.why.startswith("version.impl = linux")
    p = c.pusher("h")
    assert (p.transport, p.windowed, p.timeout_s) == ("tcp", False, LINUX_PUSH_TIMEOUT_S)


@pytest.mark.parametrize("features,impl,want", [
    (WINDOWED_FEATURES, "bare-metal", ("tcp", "tcp", True)),
    (("clcd",), "bare-metal", ("tftp", "tftp", False)),
    (WINDOWED_FEATURES, "linux", ("tcp", "tcp", True)),    # 'windowed' still wins
])
def test_choose_push_bare_metal_is_unchanged(features, impl, want):
    c = choose_push(_Client(_Ver(features, impl=impl)))
    assert c.as_tuple() == want
    if impl != "linux":
        assert c.timeout_s == DEFAULT_PUSH_TIMEOUT_S and c.partial_retry_s == 0.0


def test_choose_push_old_shell_keeps_the_tftp_default():
    c = choose_push(_Client(exc=OSError("unknown op 'version'")))
    assert c.as_tuple() == ("tftp", "tftp", False) and c.partial_retry_s == 0.0
    assert "version unavailable" in c.why


def test_choose_push_explicit_tcp_does_not_ask():
    cl = _Client(exc=AssertionError("must not probe"))
    assert choose_push(cl, transport="tcp", src="tcp", windowed=False).as_tuple() == \
        ("tcp", "tcp", False)
    assert cl.calls == 0


@pytest.mark.parametrize("impl,retry", [("linux", LINUX_TFTP_PARTIAL_RETRY_S),
                                        ("bare-metal", 0.0)])
def test_choose_push_explicit_tftp_asks_whether_the_guard_is_needed(impl, retry):
    cl = _Client(_Ver(("clcd",), impl=impl))
    c = choose_push(cl, transport="tftp", src="tftp", windowed=False)
    assert c.as_tuple() == ("tftp", "tftp", False) and c.why.startswith("explicit flags")
    assert c.partial_retry_s == retry and c.timeout_s == DEFAULT_PUSH_TIMEOUT_S
    assert cl.calls == 1


def test_mps3board_auto_on_linux_is_tcp_with_the_long_limit(shells):
    from pyverify.board import Mps3Board
    fake = shells()
    with Mps3Board(fake.host, control_port=fake.control_port) as board:
        p = board._auto_pusher()
        assert (p.transport, p.windowed, p.timeout_s, board._auto_src) == \
            ("tcp", False, LINUX_PUSH_TIMEOUT_S, "tcp")


def test_cli_passes_the_choice_and_push_timeout_override_to_the_pusher(shells, monkeypatch, tmp_path):
    seen = {}

    class Stop(Exception):
        pass

    def fake_build(host, transport, **kw):
        seen.update(transport=transport, **kw)
        raise Stop()

    monkeypatch.setattr(cli, "_build_pusher", fake_build)
    monkeypatch.setattr(cli, "_resolve_deploy_overlay", lambda args: object())
    fake = shells()
    with pytest.raises(Stop):
        cli.main(["deploy", "--host", fake.host, "--control-port", str(fake.control_port),
                  "--overlay", str(tmp_path)])
    assert (seen["transport"], seen["windowed"], seen["timeout_s"]) == \
        ("tcp", False, LINUX_PUSH_TIMEOUT_S)
    with pytest.raises(Stop):
        cli.main(["deploy", "--host", fake.host, "--control-port", str(fake.control_port),
                  "--overlay", str(tmp_path), "--push-timeout", "7"])
    assert seen["timeout_s"] == 7.0


# --------------------------------------------------------------------------- #
# The TFTP re-push itself
# --------------------------------------------------------------------------- #


class _Scripted(BitstreamPusher):
    """A pusher whose sends follow a script: an exception to raise, or None = ok."""

    def __init__(self, script, **kw):
        super().__init__(host="h", **kw)
        self.script, self.sends = list(script), []

    def _send(self, frame, *, kind):
        self.sends.append(kind)
        step = self.script.pop(0) if self.script else None
        if step is not None:
            raise step
        return PushResult(ok=True, kind=kind, bytes_sent=len(frame), message="ok")


REFUSED = PushError("TFTP DATA block 1: server aborted: TFTP ERROR 0: rejected")


def _ovl(tmp_path):
    return Overlay.load(make_overlay(tmp_path, "dbg_demo", 0x01000009))


def test_repush_retries_only_a_header_refusal_of_the_partial(tmp_path):
    p = _Scripted([None, REFUSED, REFUSED, None], transport="tftp",
                  partial_retry_s=5.0, partial_retry_gap_s=0.01)
    clr, par = p.push_pair(_ovl(tmp_path))
    assert p.partial_retries == 2 and "after 2 re-push(es)" in par.message
    assert p.sends == [BitstreamKind.CLEARING] + [BitstreamKind.PARTIAL] * 3


@pytest.mark.parametrize("exc", [
    PushError("TFTP DATA block 7: server aborted: TFTP ERROR 0: rejected"),   # mid-transfer
    PushError("TFTP DATA block 1: no ACK(1) from h:69 after 5 attempt(s) (2.0s each)"),
    PushError("TFTP WRQ 'partial.bin': server aborted: TFTP ERROR 0: transfer already in progress"),
])
def test_repush_never_retries_anything_else(tmp_path, exc):
    p = _Scripted([None, exc], transport="tftp", partial_retry_s=5.0, partial_retry_gap_s=0.01)
    with pytest.raises(PushError):
        p.push_pair(_ovl(tmp_path))
    assert p.partial_retries == 0


def test_repush_is_off_by_default_and_bounded(tmp_path):
    p = _Scripted([None, REFUSED], transport="tftp")                  # bare metal: off
    with pytest.raises(PushError, match="rejected$"):
        p.push_pair(_ovl(tmp_path))
    p = _Scripted([None] + [REFUSED] * 1000, transport="tftp",
                  partial_retry_s=0.2, partial_retry_gap_s=0.05)
    with pytest.raises(PushError, match=r"re-push\(es\).*never took the partial"):
        p.push_pair(_ovl(tmp_path))
    assert 1 <= p.partial_retries <= 10
