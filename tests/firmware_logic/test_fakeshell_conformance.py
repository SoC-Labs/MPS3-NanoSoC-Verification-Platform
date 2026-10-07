"""tests/firmware_logic/test_fakeshell_conformance.py — the host-side
double vs the real firmware, response-shape conformance.

The host stack runs end-to-end against ``pyverify.testing.fakeshell.FakeShell``
(the stdlib reference server) in scores of green tests. Those tests prove the
*client* interoperates with the *double* — but nothing proved the double still
answers like the **real firmware**. That gap is exactly where I31 hid: the
fakeshell drifted from ``firmware/common/net_proto.c`` on the control-channel
failure shape and every host test stayed green.

This suite closes it. It sends **one identical request list** through BOTH:

- the **real firmware**: ``firmware/test/bin/ctrl_echo`` — the same stdin/stdout
  harness ``test_json_golden.py`` drives (``net_proto.c`` decode →
  ``coordinator.c`` dispatch/handlers → ``net_proto.c`` per-op encode); and
- the **fakeshell**: ``FakeShell.handle_control`` behind a tiny in-process
  transport that reproduces the socket ``_ControlHandler``'s per-line decode
  (so the compared bytes are exactly what a real client would see from it),
  with **no sockets and no threads** — ``handle_control`` only touches instance
  state.

For every verb (ping/reset/set_clk/swap/link/commit/telemetry/macgen/display/
diag/version/dutrx/usd) **and its
failure shapes** (bad args, unknown op, unknown enum, malformed line) it asserts
the two responses **conform**:

- identical **key set** — the I31-class check: a stray failure field such as the
  fakeshell's old ``set_clk`` ``"locked":false`` changes the key set and fails
  here;
- identical **``ok``** verdict and identical **JSON type** per key (a bool-vs-int
  slip is caught);
- identical **value** for every key *except* an explicit per-case
  ``allow_value_delta`` — the only permitted differences are values that
  legitimately depend on the (independently-configured) scenario: the GENCHK
  counters (the firmware reads fixed seeds; the fakeshell models traffic) and
  the human ``err`` diagnostic string (two independently-authored messages).

Where a verb's whole response is scenario-matched (the fakeshell is configured
to mirror ``ctrl_echo``'s fixed scenario), the case also asserts the raw
response lines are **byte-identical**, pinning key order and number formatting.

Import bootstrap mirrors ``test_json_golden.py``. The ``ctrl_echo`` build /
transport plumbing is a self-contained re-implementation of that file's pattern
(kept independent on purpose — the two files must not import each other).

ONE SUITE, SEVERAL IMPLEMENTATIONS (Linux harness, 2026-09-23). The case table
runs against every server implementation in ``_IMPLS``, each compared with a
FakeShell configured as ITS reference:

- ``ctrl_echo`` — the bare-metal firmware path (stdin/stdout), always; the
  reference is the default (bare-metal) FakeShell.
- ``fakeshell-socket`` — a FakeShell ``profile="linux"`` served over REAL
  sockets, always. It is the transport self-test: it proves the TCP line
  framing, the swap-then-push flow over the TFTP port and the per-test process
  isolation that the harnessd run depends on, with no harnessd needed.
- ``harnessd`` — ``mps3-harnessd``'s ``host-echo`` build (HARNESSD_CONTRACT.md
  §8) spawned per test on real sockets with ``--port-base`` and
  ``--scenario ctrl-echo``; the reference is FakeShell ``profile="linux"`` with
  ctrl_echo's feature set. Enabled by ``MPS3_HARNESSD_BIN=<path>``; REQUIRED
  (a missing binary FAILS, never skips) when ``MPS3_CONFORMANCE_REQUIRE``
  names it -- which is how ``make check-linux`` runs this file.

Where an implementation legitimately differs (harnessd's diag counters come
from the kernel, not lwIP), the difference is an explicit, commented entry in
``_IMPL_VALUE_DELTA`` -- the key set and every key's JSON type are still
compared, and byte-identity is dropped only for that case.

THE USER microSD (net-protocol.md v0.13, D13). ctrl_echo's and host-echo's
scenario has NO CARD in the user-microSD slot (and the fabric HAS ``usd_spi``),
and the ``usd`` feature is compiled in. Every ``usd`` / ``commit`` case below
is therefore FULLY DETERMINED BY THE CONTRACT -- the no-card status line is
quoted in it verbatim, and the refusals are contract error NAMES -- so they are
compared byte-for-byte, the ``err`` value included (no ``_ERR`` delta: a name is
not a free-form diagnostic). Where the contract is silent on which of two
refusals wins, no case depends on it: a well-formed ``commit`` naming the
shell's own ``static_id`` and the live ``rm_id`` with no card can only be
``no card``. The card-present rules (format a/b/c, the wipe, every commit
refusal, the park) are pinned on FakeShell's side by
``host/pyverify/tests/test_usd.py`` and on the firmware's by its own tests.
"""
from __future__ import annotations

import json
import os
import random
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FW_TEST_DIR = _REPO_ROOT / "firmware" / "test"
_CTRL_ECHO = _FW_TEST_DIR / "bin" / "ctrl_echo"

_PYVERIFY_DIR = str(_REPO_ROOT / "host" / "pyverify")
if _PYVERIFY_DIR not in sys.path:
    sys.path.insert(0, _PYVERIFY_DIR)

from pyverify.testing._pusher import (  # noqa: E402  (path bootstrap first)
    BitstreamKind,
    frame_bitstream,
)
from pyverify.testing.fakeshell import FakeShell  # noqa: E402
from pyverify.testing.swap_model import STATIC_ID  # noqa: E402

_MAKE = shutil.which("make")
_CC = shutil.which("gcc") or shutil.which("cc")

pytestmark = [
    pytest.mark.skipif(not _FW_TEST_DIR.is_dir(), reason="firmware/test/ not present"),
    pytest.mark.skipif(_MAKE is None, reason="no `make` on PATH -- cannot build ctrl_echo"),
    pytest.mark.skipif(_CC is None, reason="no gcc/cc on PATH -- firmware/test/Makefile needs one"),
]

# --------------------------------------------------------------------------- #
# ctrl_echo's fixed scenario (ctrl_echo.c header comment). The fakeshell is
# configured to mirror it so success responses can be compared byte-for-byte.
# --------------------------------------------------------------------------- #
_ECHO_STATIC_ID = 0xA1B2C3D4         # == swap_model.STATIC_ID (asserted below)
_ECHO_SWAP_RM_ID = 1                 # a pushed pair carries rm_id 1 and verifies
# No _ECHO_TELEM_MV/_MA any more: net-protocol.md v0.6 removed mv/ma from the
# protocol (no power sensor exists on this platform), and ctrl_echo.c no longer
# seeds TELEM. telemetry's reply is a *failure* on both servers now -- and it is
# still compared BYTE-FOR-BYTE below, because the failure is fully determined
# (fixed diagnostic, fixed key order) rather than scenario-dependent.
# ctrl_echo's clkrst preset table (matches firmware/clkrst/clkrst.c placeholder).
_CLK_PRESETS = ("25mhz", "50mhz", "100mhz")
# ctrl_echo's diag counters: all zero. Its mock register file and module fakes
# publish nothing into the diag mailbox, so `diag` is a fully determined line on
# the firmware side -- which is what makes it comparable byte-for-byte.
_ECHO_DIAG_COUNTERS = (
    "rx_recover", "rx_dumps", "rx_drops", "icap_bytes", "got", "expect",
    "rcv_wnd", "rcv_ann_wnd", "rx_queued", "pbuf_free", "grants_sent",
    "grant_fails", "sndbuf", "snd_wnd",
    # v0.9: + the 11 JTAG-only counters, appended in diag.h X-macro order.
    "tx_frames_sent", "tx_status_drained", "tx_fifo_full_drops", "tx_errors",
    "tx_space_stalls", "tx_iface_errors", "tx_last_status", "icap_sr_last",
    "icap_eos_status", "ovlstore_phase", "ovlstore_detail",
    # v0.9.1: + the four STMPE811 panel-probe words (diag.h v7).
    "touch_regs", "touch_adc_x", "touch_adc_y", "touch_verdict",
    # v0.9.2: the superloop service telemetry (diag.h v8), appended in X-macro
    # order -- the per-pass/per-service worst-case microseconds, the
    # budget-overrun counters and the sick-service skip mask
    # (firmware/common/service.h).
    "svc_count", "pass_max_us", "svc_max_us", "svc_max_ix", "svc_overruns",
    "svc_skips", "svc_skipped",
    "svc_us_0", "svc_us_1", "svc_us_2", "svc_us_3", "svc_us_4", "svc_us_5",
    # diag.h v9 (D13): service 12 ("usd")'s worst case. The other v9 word,
    # `usd_boot`, is NOT a counter and is deliberately absent from this seed: it
    # is the power-on load LATCH, which FakeShell derives from the usd scenario
    # (no card -> decided "none" -> 0xB0070004), exactly as the firmware
    # publishes overlay_store_diag_word(). The diag case compares it byte-for-
    # byte, and test_usd_boot_latch_agrees_with_usd_boot cross-checks it.
    "svc_us_6",
)

assert STATIC_ID == _ECHO_STATIC_ID, "swap_model.STATIC_ID drifted from ctrl_echo.c"


# --------------------------------------------------------------------------- #
# Transports
# --------------------------------------------------------------------------- #


class _CtrlEchoTransport:
    """Line transport over the ctrl_echo subprocess (mirrors
    test_json_golden.py's CtrlEchoTransport — deliberately re-implemented so the
    two test modules stay independent). select()-guarded so a wedged C process
    fails in bounded time instead of hanging pytest."""

    def __init__(self, proc: subprocess.Popen, timeout: float = 10.0):
        self._proc = proc
        self._timeout = timeout

    def send_line(self, payload: bytes) -> None:
        self._proc.stdin.write(payload + b"\n")
        self._proc.stdin.flush()

    def recv_line(self) -> bytes:
        ready, _, _ = select.select([self._proc.stdout], [], [], self._timeout)
        if not ready:
            raise TimeoutError(
                f"no response line from ctrl_echo within {self._timeout}s "
                f"(rc={self._proc.poll()!r})"
            )
        line = self._proc.stdout.readline()
        if not line:
            raise ConnectionError("ctrl_echo closed stdout (crashed?)")
        return line.rstrip(b"\n")


class _FakeShellTransport:
    """In-process line transport over ``FakeShell.handle_control``.

    Reproduces the socket ``_ControlHandler.handle()``'s per-line decode
    verbatim (malformed line -> the same ``{"ok":false,"err":"malformed request
    line: ..."}``; otherwise dispatch) and the same compact JSON encoding, so
    the bytes returned here are exactly what a client reading the fake shell's
    control socket would see — minus the socket."""

    def __init__(self, fake: FakeShell):
        self._fake = fake
        self._pending: bytes | None = None

    def send_line(self, payload: bytes) -> None:
        try:
            request = json.loads(payload.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
        except (ValueError, UnicodeDecodeError) as exc:
            response = {"ok": False, "err": f"malformed request line: {exc}"}
        else:
            response = self._fake.handle_control(request)
        self._pending = json.dumps(response, separators=(",", ":")).encode("ascii")

    def recv_line(self) -> bytes:
        assert self._pending is not None, "recv_line() before send_line()"
        line, self._pending = self._pending, None
        return line


# --------------------------------------------------------------------------- #
# Fixtures — a FRESH firmware process and a FRESH scenario-matched fakeshell per
# test, so verb state (commit A/B flip, the one-shot staged swap pair, macgen
# counters) is isolated exactly like test_json_golden.py's per-test processes.
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def ctrl_echo_bin() -> Path:
    """Builds bin/ctrl_echo via the firmware Makefile's own recipe (single
    source of truth; same rationale as test_json_golden.py)."""
    result = subprocess.run(
        ["make", "bin/ctrl_echo"],
        cwd=str(_FW_TEST_DIR),
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"building ctrl_echo failed:\n{result.stdout}\n{result.stderr}"
    )
    assert _CTRL_ECHO.exists()
    return _CTRL_ECHO


# --------------------------------------------------------------------------- #
# Implementations under test
# --------------------------------------------------------------------------- #

#: The harnessd host twin (HARNESSD_CONTRACT.md §8 target ``host-echo``).
_HARNESSD_BIN = os.environ.get("MPS3_HARNESSD_BIN", "").strip()
#: argv template after the binary; ``{base}`` is the port offset (every logical
#: port P binds base+P). The contract's §7 table calls the flag ``--port-base``;
#: the binary's ``--help`` says ``--port-offset`` -- the binary is what runs.
_HARNESSD_ARGS = os.environ.get(
    "MPS3_HARNESSD_ARGS",
    "--loopback --port-offset {base} --scenario ctrl-echo --wdog off --quiet "
    # v0.16: no identity run file, no card-backed /persist -- fixed, whatever host
    "--identity /nonexistent/mps3-identity --persist-state /nonexistent/persist.state",
)
#: Implementations that MUST run (comma list). make check-linux sets
#: "harnessd": a missing binary is then a FAILURE of this suite, not a skip.
_REQUIRED = {x.strip() for x in os.environ.get("MPS3_CONFORMANCE_REQUIRE", "").split(",")
             if x.strip()}

_IMPLS = ["ctrl_echo", "fakeshell-socket"]
if _HARNESSD_BIN or "harnessd" in _REQUIRED:
    _IMPLS.append("harnessd")

#: ctrl_echo's feature set. The ``host-echo`` harnessd build is compiled with
#: ctrl_echo's flags (HARNESSD_CONTRACT §8), so it reports the same flag-derived
#: array (plus the provider-derived bits: _LINUX_ECHO_FEATURES below).
#: v0.13: + ``usd`` (bit 13): both answer the ``usd`` verb (below), and an
#: engine that answers it advertises it -- clients test the NAME before use.
_ECHO_FEATURES = ("dut_egress", "jtag_server", "xvc_dbgbr", "stats", "log", "reboot", "usd")
#: ...and what a LINUX engine built with those flags reports: the bits that follow
#: a linked provider rather than a -D flag (HM_ANSWERS 2026-09-26) are appended --
#: the host-echo harnessd links slot_linux.c like every harnessd build, so it
#: reports "slot" and "xvc_lock"; ctrl_echo (bare metal) never does.
_LINUX_ECHO_FEATURES = _ECHO_FEATURES + ("slot", "xvc_lock",
                                         # v0.16 engine name: the board identity
                                         "identity")
#: The identity harnessd's host twin reports (v0.16): no /run/mps3/identity (the
#: image defaults), the ctrl-echo scenario's VALID stage0 block whose identity words
#: are 0 (a pre-IDENT stage0), and no card-backed /persist.
_LINUX_ECHO_IDENTITY = {"stage0": {"label": None, "ip": None, "mac": None}, "persist": False}

#: Per-implementation VALUE deltas on top of each case's own (key set and JSON
#: types are still compared). Every entry says why.
_IMPL_VALUE_DELTA = {
    "harnessd": {
        # HARNESSD_CONTRACT §5.4: the net counters harnessd CAN fill come from
        # the kernel (sysfs of the host's interface in a host build), and its
        # service table really runs, so its timing words move. (The keys it
        # cannot fill are OMITTED, and the linux reference omits them too.)
        "diag": frozenset({
            "tx_frames_sent", "tx_errors", "rx_drops",
            "svc_count", "pass_max_us", "svc_max_us", "svc_max_ix", "svc_overruns",
            "svc_skips", "svc_skipped",
            "svc_us_0", "svc_us_1", "svc_us_2", "svc_us_3", "svc_us_4", "svc_us_5",
            # diag.h v9: service 12's worst case, the same class as the six above.
            # (`usd_boot`, the power-on latch, is NOT a timing word: compared.)
            "svc_us_6",
        }),
    },
}


def _scenario_kwargs(profile: str, features) -> dict:
    """ctrl_echo's fixed scenario (the reference fixture below explains each
    value), for either engine profile."""
    return dict(
        static_id=_ECHO_STATIC_ID,
        boot_rm_id=0,
        clk_presets=_CLK_PRESETS,
        clk_locked=True,
        telemetry_lockup=False,
        boot_active_slot="A",
        macgen_frames_per_call=8,
        has_clcd_kvm=False,
        features=features,
        diag_counters={k: 0 for k in _ECHO_DIAG_COUNTERS},
        profile=profile,
        # ctrl_echo and harnessd's host builds (--wdog off) have no watchdog:
        # `reboot` answers "no watchdog" on all of them.
        has_watchdog=False,
        identity=_LINUX_ECHO_IDENTITY if "identity" in features else None,
    )


class _SocketTransport:
    """A real TCP 6900 line transport, plus the one thing a socket server
    needs that the stdin/stdout harness fakes internally: a swap PARKS until
    its clearing + partial are pushed (the armed-push rule), so
    :meth:`push_pair` pushes them over TFTP -- the plain, non-windowed path --
    whose in-band verdict (ERROR vs final ACK) says whether the shell was
    awaiting that bitstream yet, and retries until it is."""

    def __init__(self, host: str, ctrl_port: int, tftp_port: int, timeout: float = 10.0):
        self._sock = socket.create_connection((host, ctrl_port), timeout=timeout)
        self._sock.settimeout(timeout)
        self._buf = b""
        self.host, self.tftp_port = host, tftp_port

    def send_line(self, payload: bytes) -> None:
        self._sock.sendall(payload + b"\n")

    def recv_line(self) -> bytes:
        while b"\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("server closed the control channel")
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return line

    #: A type-1 NOP config word, and the "RMID" marker harnessd's MOCK fabric
    #: (src/linux_harness/sw/harnessd/hal_mock.c) watches the ICAP stream for:
    #: the word after it becomes DFXCTL.RM_ID, which is what the swap's VERIFY
    #: step reads back. ctrl_echo pokes that register instead; a socket server
    #: over a mock fabric needs the partial to SAY which RM it is. FakeShell
    #: ignores payload content, so the same bytes serve every implementation.
    _NOP = 0x20000000
    _RMID_MARKER = 0x524D4944

    def push_pair(self, rm_id: int = _ECHO_SWAP_RM_ID, deadline_s: float = 10.0) -> None:
        import struct
        from pyverify.pusher import PushError, tftp_put
        clearing = struct.pack(">16I", *([self._NOP] * 16))
        partial = struct.pack(">32I", *([self._NOP] * 8 + [self._RMID_MARKER, rm_id]
                                        + [self._NOP] * 22))
        for payload, kind in ((clearing, BitstreamKind.CLEARING),
                              (partial, BitstreamKind.PARTIAL)):
            frame = frame_bitstream(payload, kind=kind, rm_slot=0,
                                    static_id=_ECHO_STATIC_ID, rm_id=rm_id)
            end = time.monotonic() + deadline_s
            while True:
                try:
                    tftp_put(frame, self.host, self.tftp_port,
                             filename=f"{kind.name.lower()}.bin", timeout_s=2.0, retries=2)
                    break
                except PushError:
                    if time.monotonic() > end:
                        raise
                    time.sleep(0.1)    # the swap has not armed this kind yet

    def close(self) -> None:
        self._sock.close()


def _free_port_base() -> int:
    """A base such that every logical harness port P is free at base+P."""
    logical = (69, 2542, 6899, 6900, 6910, 6921, 6930, 6931, 6932)
    for _ in range(200):
        base = random.randrange(20000, 50000)
        socks = []
        try:
            for p in logical:
                for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
                    s_ = socket.socket(socket.AF_INET, kind)
                    socks.append(s_)
                    s_.bind(("127.0.0.1", base + p))
            return base
        except OSError:
            continue
        finally:
            for s_ in socks:
                s_.close()
    raise RuntimeError("no free port base for mps3-harnessd")


@pytest.fixture(params=_IMPLS)
def impl(request) -> str:
    return request.param


@pytest.fixture
def firmware(request, impl):
    """The server under test, as a line transport. A FRESH process (or fake)
    per test, so verb state (commit A/B flip, staged pairs, macgen counters)
    is isolated exactly as before."""
    if impl == "ctrl_echo":
        proc = subprocess.Popen(
            [str(request.getfixturevalue("ctrl_echo_bin"))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, cwd=str(_FW_TEST_DIR),
        )
        try:
            yield _CtrlEchoTransport(proc)
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.kill()
                proc.wait()
        return

    if impl == "fakeshell-socket":
        fake = FakeShell.ephemeral(**_scenario_kwargs("linux", _LINUX_ECHO_FEATURES)).start()
        tx = _SocketTransport(fake.host, fake.control_port, fake.tftp_port)
        try:
            yield tx
        finally:
            tx.close()
            fake.stop()
        return

    assert impl == "harnessd"
    assert _HARNESSD_BIN, (
        "harnessd conformance is REQUIRED (MPS3_CONFORMANCE_REQUIRE) but "
        "MPS3_HARNESSD_BIN is not set -- build it with "
        "`make -C src/linux_harness/sw/harnessd host-echo` (make check-linux does)")
    binary = Path(_HARNESSD_BIN)
    assert binary.is_file() and os.access(str(binary), os.X_OK), (
        f"MPS3_HARNESSD_BIN={binary} is not an executable file")
    base = _free_port_base()
    argv = [str(binary)] + shlex.split(_HARNESSD_ARGS.format(base=base))
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        ctrl = base + 6900
        end = time.monotonic() + 15.0
        while True:
            if proc.poll() is not None:
                out = proc.stdout.read().decode(errors="replace")
                pytest.fail(f"mps3-harnessd exited rc={proc.returncode} before listening:\n{out}")
            try:
                socket.create_connection(("127.0.0.1", ctrl), timeout=0.5).close()
                break
            except OSError:
                if time.monotonic() > end:
                    pytest.fail(f"mps3-harnessd never listened on {ctrl} ({' '.join(argv)})")
                time.sleep(0.1)
        time.sleep(0.05)     # let it adopt, then drop, the readiness probe
        tx = _SocketTransport("127.0.0.1", ctrl, base + 69)
        _settle_power_on_decision(tx)
        try:
            yield tx
        finally:
            tx.close()
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
            proc.wait()


def _settle_power_on_decision(tx, deadline_s: float = 5.0) -> None:
    """A freshly started engine takes its once-per-configuration power-on
    decision a moment AFTER the network is up (the firmware waits a 100 ms
    card-detect grace before it may conclude "no card"), and until then
    `usd.boot` and diag `usd_boot` legitimately read "pending". The reference
    has decided at construction. So wait, on the test's own connection and with
    the read-only `usd` verb, until the engine has decided -- bounded, and a
    no-op on an engine that does not answer `usd` at all (its cases then fail on
    their own terms)."""
    end = time.monotonic() + deadline_s
    while True:
        tx.send_line(b'{"op":"usd"}')
        reply = json.loads(tx.recv_line())
        if not reply.get("ok") or reply.get("boot") != "pending":
            return
        if time.monotonic() > end:
            return            # still pending: the usd cases will say so
        time.sleep(0.05)


def _stage_swap_pair(fake: FakeShell, *, rm_id: int = _ECHO_SWAP_RM_ID) -> None:
    """Stage a validated {clearing, partial} pair through the REAL config-agent
    path (the pusher's real framing + ConfigAgentModel), so a single ``swap``
    request succeeds with the given rm_id — the fakeshell counterpart of
    ctrl_echo.c's pump_swap_to_completion() arming its pair."""
    for payload, kind in (
        (b"\xC1" * 64, BitstreamKind.CLEARING),
        (b"\xB2" * 128, BitstreamKind.PARTIAL),
    ):
        frame = frame_bitstream(
            payload, kind=kind, rm_slot=0, static_id=_ECHO_STATIC_ID, rm_id=rm_id,
        )
        status, header = fake.config_agent.validate_header(frame[:24])
        assert status.name == "OK", status
        assert fake.config_agent.finish_payload(header, frame[24:]).name == "OK"
    assert fake.pair_ready


@pytest.fixture
def fakeshell(impl):
    """The REFERENCE for ``impl``: a FakeShell scenario-matched to ctrl_echo.c,
    NOT started (handle_control is pure instance logic), with an rm_id=1 pair
    pre-staged so the single ``swap`` case verifies. The bare-metal profile
    for ctrl_echo; the linux profile (impl "linux", 128 KiB LMB, ctrl_echo's
    features) for the socket implementations."""
    if impl != "ctrl_echo":
        fake = FakeShell(**_scenario_kwargs("linux", _LINUX_ECHO_FEATURES))
        _stage_swap_pair(fake)
        return _FakeShellTransport(fake)
    fake = FakeShell(
        static_id=_ECHO_STATIC_ID,
        boot_rm_id=0,
        clk_presets=_CLK_PRESETS,
        clk_locked=True,
        telemetry_lockup=False,      # ctrl_echo's dut_lockup pin reads 0
        boot_active_slot="A",       # first commit lands slot 'B', like ctrl_echo
        macgen_frames_per_call=8,
        # --- match ctrl_echo's BUILD, not just its scenario ------------------ #
        # These three model COMPILE-TIME facts about the firmware binary on the
        # other side of the comparison, and getting them wrong makes a
        # byte-identical assertion impossible for no good reason:
        #   has_clcd_kvm=False -- ctrl_echo is built WITHOUT -DMPS3_HAS_CLCD_KVM
        #     (the Makefile builds test_display_dispatch with it; ctrl_echo
        #     deliberately without), so `display` takes the OFF-build path on
        #     both sides and both answer "clcd_kvm not present".
        #   features=() + the default 0.0.0/ver32 0/sha "unknown"/not-dirty --
        #     ctrl_echo links only ../platform/mps3_version_weak.c and is built
        #     with none of the feature -D flags, so it reports the honest
        #     "not provisioned" identity and an EMPTY feature array. That makes
        #     `version` fully determined and comparable byte-for-byte, instead
        #     of moving with whatever commit the tree sits on.
        has_clcd_kvm=False,
        # v0.11: ctrl_echo (built with -DMPS3_HAS_DUT_EGRESS) also reports the
        # always-compiled-in services, appended in bit order; v0.13 appends usd.
        features=_ECHO_FEATURES,
        # diag: ctrl_echo's mocks publish nothing, so every counter reads 0 --
        # which is also the fakeshell default. Stated for the record.
        diag_counters={k: 0 for k in _ECHO_DIAG_COUNTERS},
        # v0.13: no card in the user microSD (the FakeShell default, stated):
        # usd_hw=True, usd_card=None -> state "none", boot "none".
        usd_hw=True,
        usd_card=None,
    )
    _stage_swap_pair(fake)
    return _FakeShellTransport(fake)


# --------------------------------------------------------------------------- #
# Conformance comparison
# --------------------------------------------------------------------------- #


def _assert_conforms(fw_line: bytes, fake_line: bytes, *, allow_value_delta=frozenset()):
    """The core assertion: the two responses must have the same key set, the
    same ``ok`` verdict, the same JSON type per key, and the same value for
    every key NOT listed in ``allow_value_delta``."""
    fw = json.loads(fw_line)
    fake = json.loads(fake_line)
    assert isinstance(fw, dict) and isinstance(fake, dict), (fw_line, fake_line)

    assert set(fw) == set(fake), (
        f"response key-set diverged (I31-class):\n"
        f"  firmware  keys={sorted(fw)}  line={fw_line!r}\n"
        f"  fakeshell keys={sorted(fake)}  line={fake_line!r}"
    )
    assert "ok" in fw, f"firmware response missing 'ok': {fw_line!r}"
    assert fw["ok"] == fake["ok"], (
        f"ok verdict diverged: firmware {fw['ok']!r} vs fakeshell {fake['ok']!r} "
        f"({fw_line!r} vs {fake_line!r})"
    )
    for key in fw:
        # bool is a subtype of int; `type(...) is type(...)` keeps them distinct
        # so a true/false-vs-integer slip on any key is caught.
        assert type(fw[key]) is type(fake[key]), (
            f"key {key!r} JSON type diverged: firmware "
            f"{type(fw[key]).__name__} vs fakeshell {type(fake[key]).__name__}"
        )
    for key in fw:
        if key in allow_value_delta:
            continue
        assert fw[key] == fake[key], (
            f"key {key!r} value diverged outside the allowed-delta list "
            f"{sorted(allow_value_delta)}: firmware {fw[key]!r} vs fakeshell "
            f"{fake[key]!r}"
        )


# Case table: (id, request_bytes, allow_value_delta, byte_identical).
#   allow_value_delta — keys whose *value* may differ (never the key's presence
#     or type): the scenario-dependent counters and the human err string.
#   byte_identical    — when the whole response is scenario-matched, also assert
#     the raw lines are identical (pins key order + number formatting).
_EMPTY: frozenset = frozenset()
_ERR: frozenset = frozenset({"err"})
_COUNTERS: frozenset = frozenset({"tx", "rx", "err"})

_CASES = [
    # -- successes, fully scenario-matched (byte-identical) ------------------ #
    ("ping",               b'{"op":"ping"}',                                   _EMPTY, True),
    ("reset_ok",           b'{"op":"reset","target":"dut"}',                   _EMPTY, True),
    ("set_clk_ok",         b'{"op":"set_clk","preset":"25mhz"}',               _EMPTY, True),
    ("link_ok",            b'{"op":"link","event":"down"}',                    _EMPTY, True),
    ("swap_ok",            b'{"op":"swap","rm":"nanosoc","src":"tftp"}',       _EMPTY, True),
    # -- telemetry: ALWAYS a failure (v0.6 -- no power sensor), and the one
    #    declared carve-out to the uniform failure shape (its failure line
    #    carries "lockup"). NOT in the _ERR class: its diagnostic is
    #    contract-fixed ("no power sensor"), not a free-form message, so both
    #    servers must emit the SAME string -- hence _EMPTY (no value delta
    #    allowed) + byte-identical, the strictest row in this table. -------- #
    ("telemetry",          b'{"op":"telemetry"}',                             _EMPTY, True),
    # -- diag: the verb the fakeshell did not have until v0.8. The firmware had
    #    shipped it for weeks and this suite could not see the divergence
    #    because no case sent it -- a conformance suite only conforms what it
    #    asks about. All 14 counters read 0 on both sides (ctrl_echo publishes
    #    nothing into the mailbox), so this is byte-identical: it pins the key
    #    ORDER of the longest response line in the protocol as well. --------- #
    ("diag",               b'{"op":"diag"}',                                  _EMPTY, True),
    # -- version (v0.8): both sides report the "not provisioned" identity and an
    #    EMPTY feature array (see the fixture's build-matching note), so the
    #    whole line -- including the JSON array, the only one in this protocol
    #    -- is byte-identical. -------------------------------------------- #
    ("version",            b'{"op":"version"}',                               _EMPTY, True),
    # -- dutrx (v0.10): the DUT-egress read. ctrl_echo is built WITH
    #    -DMPS3_HAS_DUT_EGRESS and leaves the DUTEGR page all-zero, so the reply
    #    is the EMPTY-FIFO line -- the one dutrx reply that is fully determined
    #    on both sides, which makes all thirteen keys, their order and their
    #    types comparable byte-for-byte. That matters more here than for most
    #    verbs: this is the protocol's only response with a variable-length
    #    payload, and a key set agreed only on the empty case is still the key
    #    set. The OFF-build decline ("dut_egress not present") is pinned
    #    firmware-side by test_coordinator_dispatch.c and fake-side by
    #    test_dutrx_off_build_declines below. ------------------------------- #
    ("dutrx",              b'{"op":"dutrx"}',                                 _EMPTY, True),
    # -- display on a build with NO KVM slave: the OFF-build decline. ctrl_echo
    #    is built without -DMPS3_HAS_CLCD_KVM and the fake is configured
    #    has_clcd_kvm=False, so both emit the same contract-fixed diagnostic
    #    ("clcd_kvm not present") -- hence _EMPTY, not _ERR. The ON-build
    #    success shape is covered by firmware/test/test_display_dispatch.c and
    #    host/pyverify/tests/test_display.py on their respective sides. ----- #
    ("display_off_build",  b'{"op":"display","owner":"query"}',               _EMPTY, True),
    # -- macgen success: same shape, but counters legitimately differ -------- #
    ("macgen_ok",          b'{"op":"macgen","gen":true,"chk":true,"inject":"bad_fcs"}', _COUNTERS, False),
    ("macgen_ok_none",     b'{"op":"macgen","gen":true,"chk":true,"inject":"none"}',    _COUNTERS, False),
    # -- failures: uniform {"ok":false,"err":<str>}; err string may differ --- #
    ("reset_bad_target",   b'{"op":"reset","target":"mainframe"}',            _ERR, False),
    ("set_clk_bad_preset", b'{"op":"set_clk","preset":"13mhz"}',              _ERR, False),
    ("set_clk_missing",    b'{"op":"set_clk"}',                               _ERR, False),
    ("link_bad_event",     b'{"op":"link","event":"sideways"}',               _ERR, False),
    ("macgen_bad_inject",  b'{"op":"macgen","gen":true,"chk":true,"inject":"smash"}', _ERR, False),
    ("macgen_missing_gen", b'{"op":"macgen","chk":true,"inject":"none"}',     _ERR, False),
    ("swap_missing_rm",    b'{"op":"swap","src":"tftp"}',                     _ERR, False),
    ("unknown_op",         b'{"op":"selfdestruct"}',                          _ERR, False),
    ("empty_object",       b'{}',                                             _ERR, False),
    ("array_not_object",   b'[1,2,3]',                                        _ERR, False),
    ("not_json",           b'not json',                                       _ERR, False),
    # -- v0.13 (D13): the user microSD. The scenario has NO CARD (module
    #    docstring). Every line below is fixed by the contract -- the status
    #    line is quoted in it, and the refusals are error NAMES -- so every
    #    case is _EMPTY + byte-identical: an engine that answers "no sd card"
    #    (the store's C string) instead of "no card" fails here. -------------- #
    ("usd_status_no_card", b'{"op":"usd"}',                                   _EMPTY, True),
    ("usd_format_no_card", b'{"op":"usd","action":"format","confirm":"erase"}', _EMPTY, True),
    ("usd_wipe_no_card",   b'{"op":"usd","action":"format","confirm":"erase-all"}', _EMPTY, True),
    ("usd_format_no_confirm", b'{"op":"usd","action":"format"}',             _EMPTY, True),
    ("usd_format_bad_confirm", b'{"op":"usd","action":"format","confirm":"yes"}', _EMPTY, True),
    ("usd_clear_no_card",  b'{"op":"usd","action":"clear"}',                  _EMPTY, True),
    ("usd_rescan_no_card", b'{"op":"usd","action":"rescan"}',                 _EMPTY, True),
    ("usd_bad_action",     b'{"op":"usd","action":"explode"}',                _EMPTY, True),
    # -- v0.13 `commit` REPLACED: the v0.11 form (rm only) is `bad args`; so is
    #    a src other than "tcp" and a malformed hex id. A WELL-FORMED commit of
    #    what is running (the shell's static_id, the live greybox rm_id 0) with
    #    no card is `no card`, answered at once -- before any park. ---------- #
    ("commit_v011_bad_args", b'{"op":"commit","rm":"nanosoc"}',               _EMPTY, True),
    ("commit_src_tftp",
     b'{"op":"commit","rm":"led","src":"tftp","rm_id":"0x00000000","static_id":"0xa1b2c3d4",'
     b'"clear_len":64,"clear_crc":"0x00000000","part_len":128,"part_crc":"0x00000000"}',
     _EMPTY, True),
    ("commit_bad_hex",
     b'{"op":"commit","rm":"led","src":"tcp","rm_id":"0x100000000","static_id":"0xa1b2c3d4",'
     b'"clear_len":64,"clear_crc":"0x00000000","part_len":128,"part_crc":"0x00000000"}',
     _EMPTY, True),
    ("commit_no_card",
     b'{"op":"commit","rm":"led","src":"tcp","rm_id":"0x00000000","static_id":"0xa1b2c3d4",'
     b'"clear_len":64,"clear_crc":"0x00000000","part_len":128,"part_crc":"0x00000000"}',
     _EMPTY, True),
    # -- v0.16: the board identity. Bare metal (ctrl_echo) declines both verbs with
    #    the weak provider's fixed text + code; harnessd's host twin answers from the
    #    scenario above (defaults, a valid pre-IDENT stage0 block, no /persist), and
    #    refuses a set with no_persist -- every line fixed, so byte-identical. ---- #
    ("identity",           b'{"op":"identity"}',                              _EMPTY, True),
    ("identity_set_no_persist", b'{"op":"identity_set","label":"MPS3-02"}',   _EMPTY, True),
    # -- v0.16 locate: every engine here is panel-less (ctrl_echo; harnessd's
    #    host-echo twin is built without CLCD), so all decline it identically. -- #
    ("locate_no_panel",    b'{"op":"locate","s":5,"who":"hm"}',               _EMPTY, True),
    # -- v0.17 presence + the panel: every engine here is panel-less, so all decline
    #    identically (the weak providers' text + code). The hello is HM's own, with
    #    its nested lease -- the ONE nested request, decoded by all three; the page
    #    change is declined too (unclaimed: the lock does not fire). ------------- #
    ("hello_no_panel",
     b'{"op":"hello","v":1,"sid":"a1b2c3d4","who":"alice@lab-pc01","app":"hm/0.1.0",'
     b'"name":"mps3-01","role":"holder","lease":{"by":"alice","left":4332,"q":1},"ttl":90}',
     _EMPTY, True),
    ("panel_no_panel",     b'{"op":"panel"}',                                 _EMPTY, True),
    ("panel_frame_no_panel", b'{"op":"panel","frame":"a"}',                   _EMPTY, True),
    ("panel_page_no_panel", b'{"op":"panel","page":"apps"}',                  _EMPTY, True),
]

#: The v0.13 cases whose reply is a refusal: its `err` must be a contract NAME.
_USD_REFUSAL_CASES = [c for c in _CASES
                      if c[0].startswith(("usd_", "commit_")) and c[0] != "usd_status_no_card"]


@pytest.mark.parametrize("case_id,request_bytes,allow_delta,byte_identical",
                         _CASES, ids=[c[0] for c in _CASES])
def test_response_conforms(impl, firmware, fakeshell, case_id, request_bytes, allow_delta,
                           byte_identical):
    """Firmware and fakeshell must return conforming responses for the same
    request line — this is the automated guard that would have caught I31."""
    firmware.send_line(request_bytes)
    if case_id == "swap_ok" and isinstance(firmware, _SocketTransport):
        # A real socket server parks the swap until the pair is pushed.
        firmware.push_pair()
    fw_line = firmware.recv_line()
    op = None
    try:
        op = json.loads(request_bytes).get("op")
    except (ValueError, AttributeError):
        pass
    extra = _IMPL_VALUE_DELTA.get(impl, {}).get(op, frozenset())
    if extra:
        allow_delta = frozenset(allow_delta) | extra
        byte_identical = False

    fakeshell.send_line(request_bytes)
    fake_line = fakeshell.recv_line()

    _assert_conforms(fw_line, fake_line, allow_value_delta=allow_delta)
    if byte_identical:
        assert fw_line == fake_line, (
            f"[{case_id}] scenario-matched response not byte-identical:\n"
            f"  firmware  {fw_line!r}\n  fakeshell {fake_line!r}"
        )


def test_all_verbs_covered():
    """Guard against a verb being added to the protocol without a conformance
    case: every op in the fakeshell's dispatch set must appear in _CASES."""
    covered = set()
    for _id, req, _delta, _bi in _CASES:
        try:
            op = json.loads(req).get("op")
        except (ValueError, AttributeError):
            continue
        if isinstance(op, str):
            covered.add(op)
    # The FULL v0.8 verb set. Kept as a literal (not derived from the fake's
    # dispatch tuple) on purpose: deriving it would let a verb be dropped from
    # BOTH the server and this guard in one edit and stay green. `diag` sat
    # outside this set for weeks, which is exactly how the fakeshell went that
    # long without implementing it.
    expected = {
        "ping", "reset", "set_clk", "swap", "link", "commit", "telemetry",
        "macgen", "display", "diag", "version", "dutrx",
        "usd",   # v0.13
        "identity", "identity_set", "locate",   # v0.16
        "hello", "panel",                       # v0.17
    }
    assert expected <= covered, f"verbs missing a conformance case: {expected - covered}"


def test_fakeshell_failure_shape_is_uniform_two_keys():
    """Belt-and-braces on the I31 fix itself: every failure case's fakeshell
    reply is exactly {"ok":false,"err":<str>} — no per-verb steady-state field
    (set_clk's locked, swap's verified/rm_id) leaks onto a failure line. The
    firmware side of this invariant is already pinned by test_response_conforms'
    key-set check against ctrl_echo."""
    tx = _FakeShellTransport(FakeShell(static_id=_ECHO_STATIC_ID, clk_presets=_CLK_PRESETS))
    failure_cases = [(req, _id) for _id, req, allow_delta, _bi in _CASES if allow_delta is _ERR]
    assert failure_cases, "expected at least one failure case in _CASES"
    for req, case_id in failure_cases:
        tx.send_line(req)
        resp = json.loads(tx.recv_line())
        assert resp["ok"] is False, (case_id, resp)
        assert set(resp) == {"ok", "err"}, (
            f"[{case_id}] failure reply is not the uniform 2-key shape: {resp}"
        )
        assert isinstance(resp["err"], str) and resp["err"]


def test_usd_boot_latch_agrees_with_usd_boot(impl, firmware):
    """diag.h v9: the mailbox word `usd_boot` IS the power-on load latch
    ([31:16] 0xB007, [15:8] reason, [3:0] decision). Whatever an engine
    publishes there must render as exactly the `boot` its own `usd` verb
    reports -- a JTAG read of the mailbox and the network verb are two views of
    ONE decision. Asked of each implementation on its own connection, so no
    reference is involved: an engine that says "none" over 6900 while its
    mailbox says 0 ("not decided yet") fails here."""
    from pyverify.client import usd_boot_text
    firmware.send_line(b'{"op":"usd"}')
    usd = json.loads(firmware.recv_line())
    firmware.send_line(b'{"op":"diag"}')
    diag = json.loads(firmware.recv_line())
    assert usd.get("ok") is True, usd
    assert "usd_boot" in diag, f"{impl}: diag has no usd_boot word (diag.h v9): {sorted(diag)}"
    word = diag["usd_boot"]
    try:
        rendered = usd_boot_text(word)
    except ValueError as exc:
        pytest.fail(f"{impl}: diag usd_boot 0x{word:08x} is not a latch word: {exc}")
    assert rendered == usd["boot"], (
        f"{impl}: diag usd_boot 0x{word:08x} renders as {rendered!r}, but `usd` "
        f"reports boot {usd['boot']!r}")


@pytest.mark.parametrize("profile", ["bare-metal", "linux"])
def test_usd_and_commit_refusals_are_contract_names(profile):
    """net-protocol.md v0.13: `usd` / `commit` errors are NAMES from one closed
    table, never errno numbers and never the store's internal C strings. The
    reference's side of the v0.13 cases, on both engine profiles; the byte-
    identical comparisons above hold every implementation to the same line."""
    from pyverify.client import USD_ERRORS
    tx = _FakeShellTransport(FakeShell(**_scenario_kwargs(profile, _ECHO_FEATURES)))
    assert _USD_REFUSAL_CASES, "expected v0.13 refusal cases in _CASES"
    for case_id, req, _delta, _bi in _USD_REFUSAL_CASES:
        tx.send_line(req)
        resp = json.loads(tx.recv_line())
        assert resp["ok"] is False and set(resp) == {"ok", "err"}, (case_id, resp)
        assert resp["err"] in USD_ERRORS, (case_id, resp)


def test_telemetry_is_the_only_carve_out_to_the_failure_shape():
    """net-protocol.md v0.6 declares telemetry the SINGLE exception to the
    uniform failure shape: its failure line (its only line) additionally
    carries "lockup". Pin the carve-out at exactly one verb and exactly one
    extra key, so it cannot quietly become a precedent.

    This is the fakeshell side; test_response_conforms['telemetry'] pins the
    firmware side (key set + bytes) against ctrl_echo."""
    tx = _FakeShellTransport(FakeShell(static_id=_ECHO_STATIC_ID, telemetry_lockup=True))
    tx.send_line(b'{"op":"telemetry"}')
    resp = json.loads(tx.recv_line())
    assert resp["ok"] is False                     # no success shape exists
    assert set(resp) == {"ok", "err", "lockup"}    # the 2-key rule + lockup, no more
    assert resp["err"] == "no power sensor"        # contract-fixed diagnostic
    assert resp["lockup"] is True                  # the raw pin, reported not editorialised
    assert "mv" not in resp and "ma" not in resp   # absent, NOT zeroed

    # Every OTHER verb's failure line stays at the uniform 2 keys — covered
    # exhaustively by test_fakeshell_failure_shape_is_uniform_two_keys above.


# --------------------------------------------------------------------------- #
# The multi-implementation plumbing itself
# --------------------------------------------------------------------------- #


def test_required_implementations_are_known_and_enabled():
    """MPS3_CONFORMANCE_REQUIRE can only name implementations this file has,
    and every one it names is in the run -- so `make check-linux` can never
    pass with the harnessd half quietly absent."""
    unknown = _REQUIRED - {"ctrl_echo", "fakeshell-socket", "harnessd"}
    assert not unknown, f"MPS3_CONFORMANCE_REQUIRE names unknown implementation(s): {unknown}"
    assert _REQUIRED <= set(_IMPLS), (_REQUIRED, _IMPLS)
    if "harnessd" in _REQUIRED:
        assert _HARNESSD_BIN, "harnessd is REQUIRED but MPS3_HARNESSD_BIN is unset"


def test_socket_transport_catches_a_mutant_server():
    """NEGATIVE CONTROL for the socket path: a server that drops `impl` from
    `version` (i.e. a harnessd whose mps3_proto_impl() seam did not link) must
    FAIL the comparison against the linux reference. Without this, a transport
    bug that compared a line with itself would read as conformance."""
    mutant = FakeShell.ephemeral(**_scenario_kwargs("linux", _LINUX_ECHO_FEATURES))
    mutant.impl = None
    mutant.start()
    tx = _SocketTransport(mutant.host, mutant.control_port, mutant.tftp_port)
    try:
        tx.send_line(b'{"op":"version"}')
        line = tx.recv_line()
    finally:
        tx.close()
        mutant.stop()
    ref = _FakeShellTransport(FakeShell(**_scenario_kwargs("linux", _LINUX_ECHO_FEATURES)))
    ref.send_line(b'{"op":"version"}')
    with pytest.raises(AssertionError, match="key-set diverged"):
        _assert_conforms(line, ref.recv_line())


_SOCKET_IMPLS = [i for i in _IMPLS if i != "ctrl_echo"]

#: v0.11 verbs the bare-metal FakeShell predates: compared on the socket
#: implementations against the LINUX reference, key set + JSON types (values
#: such as up_ms move by construction).
_V011_REQUESTS = [
    ("stats", b'{"op":"stats"}'),
    ("log", b'{"op":"log","off":0}'),
    ("reboot", b'{"op":"reboot"}'),
]


@pytest.mark.parametrize("impl", _SOCKET_IMPLS)
@pytest.mark.parametrize("name,request_bytes", _V011_REQUESTS, ids=[r[0] for r in _V011_REQUESTS])
def test_v011_verbs_match_the_linux_reference(impl, firmware, name, request_bytes):
    fw = firmware
    fw.send_line(request_bytes)
    fw_line = fw.recv_line()
    ref = FakeShell(**_scenario_kwargs("linux", _LINUX_ECHO_FEATURES))
    ref_tx = _FakeShellTransport(ref)
    ref_tx.send_line(request_bytes)
    ref_line = ref_tx.recv_line()
    got, want = json.loads(fw_line), json.loads(ref_line)
    assert list(got) == list(want), (
        f"{name}: key set/ORDER diverged from the linux reference\n"
        f"  {impl}: {list(got)}\n  reference: {list(want)}")
    for k in want:
        assert type(got[k]) is type(want[k]), (name, k, got[k], want[k])
