"""``python -m socket_harness`` — the board-free front-end for the unified
MPS3 nanoSoC endpoint harness.

This is the single command-line replacement for the scattered host scripts the
harness grew organically:

    * ``host/console/socat_consoles.sh`` / ``host/console/ser2net.yaml``
        -> ``emit {socat|ser2net}`` (regenerated from the ONE registry) and
           ``console <uart>`` (a pure-Python pty/tcp bridge, no socat install).
    * ``scripts/mps3_diag.tcl``  ->  ``reg <block.reg> …`` (the xsdb CSR/mailbox
           path, with a board-free ``--dry-run`` that prints the exact argv/tcl).
    * ``scripts/mps3_console.py --self-test``  ->  ``selftest`` (the aggregate
           board-free proof: pty loopback, retry schedule, register codec, xsdb
           parse, session reassembly, harness/pyverify interop).

Exit-code discipline (copied from :mod:`pyverify.cli` — every failure is a
clean one-line stderr message, never a traceback):

    0  ok
    1  the endpoint declined the verb, or ``selftest`` failed a proof
    2  usage error (bad args / unknown block/reg/field)
    3  the endpoint is unreachable (control channel / xsdb / board down)

Board-free discipline: ``endpoints``, ``emit`` and ``reg --dry-run`` and
``selftest`` touch no board and open no real socket. ``console``, ``probe`` and
a non-dry ``reg`` are the only verbs that talk to a live shell; they are wired
here but never exercised against hardware by the test-suite.
"""
from __future__ import annotations

import argparse
import json
import sys

from .console_bridge import PtyEndpoint, TcpListenEndpoint, ser2net_yaml, socat_argv
from .endpoints import (
    DEFAULT_HUB_URL,
    DEFAULT_SHELL_HOST,
    REGISTRY,
    by_name,
    console_specs,
)
from .harness import ProbeResult, SocketHarness
from .registers import BLOCKS, RegisterAccess

# status string -> process exit code (the honest-liveness contract shared with
# SocketHarness.probe / pyverify.edge.status_fpga: never fabricate an "ok").
_STATUS_EXIT = {"ok": 0, "declined": 1, "unreachable": 3}


# --------------------------------------------------------------------------- #
# small pure helpers (unit-testable, no I/O)
# --------------------------------------------------------------------------- #
def _fmt_hex(value: object) -> str:
    """Render an int as canonical upper-case ``0x…`` (leaving non-ints as str)."""
    if isinstance(value, bool):  # bool is an int subclass — keep it readable
        return "1" if value else "0"
    if isinstance(value, int):
        return f"0x{value:X}"
    return str(value)


def _parse_kv(pairs: list[str]) -> dict[str, int]:
    """Parse ``name=value`` field assignments into a dict of ints.

    Values accept any Python integer literal (``1``, ``0x10``, ``0b11``) via
    ``int(v, 0)``. Raises ``ValueError`` (-> CLI usage exit 2) on a malformed
    pair so the caller never sees a traceback.
    """
    out: dict[str, int] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"bad field assignment {pair!r}; expected name=value")
        key, _, raw = pair.partition("=")
        key = key.strip()
        if not key:
            raise ValueError(f"bad field assignment {pair!r}; empty field name")
        try:
            out[key] = int(raw.strip(), 0)
        except ValueError:
            raise ValueError(
                f"bad value for {key!r}: {raw!r} (want an integer, e.g. 1 or 0x10)"
            ) from None
    return out


def _render_preview(preview: object) -> str:
    """Flatten an xsdb dry-run preview (argv list or tcl string) to one line."""
    if isinstance(preview, (list, tuple)):
        return " ".join(str(tok) for tok in preview)
    return str(preview)


# --------------------------------------------------------------------------- #
# endpoints
# --------------------------------------------------------------------------- #
def _endpoint_dict(spec) -> dict:
    return {
        "name": spec.name,
        "family": spec.family.name,
        "framing": spec.framing.name,
        "proto": spec.proto,
        "port": spec.default_port,
        "csr_base": (None if spec.csr_base is None else f"0x{spec.csr_base:08X}"),
        "gated_by": spec.gated_by,
        "survives_swap": spec.survives_swap,
    }


def _cmd_endpoints(args: argparse.Namespace) -> int:
    if args.json:
        print(json.dumps([_endpoint_dict(s) for s in REGISTRY], indent=2))
        return 0

    header = ("name", "family", "proto", "port", "csr_base", "gated_by", "swap")
    rows = []
    for spec in REGISTRY:
        rows.append(
            (
                spec.name,
                spec.family.name,
                spec.proto,
                "-" if spec.default_port is None else str(spec.default_port),
                "-" if spec.csr_base is None else f"0x{spec.csr_base:08X}",
                spec.gated_by,
                "yes" if spec.survives_swap else "no",
            )
        )
    widths = [max(len(header[i]), *(len(r[i]) for r in rows)) for i in range(len(header))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*header))
    print(fmt.format(*("-" * w for w in widths)))
    for row in rows:
        print(fmt.format(*row))
    return 0


# --------------------------------------------------------------------------- #
# emit  (regenerate host/console/* from the single registry)
# --------------------------------------------------------------------------- #
def _cmd_emit(args: argparse.Namespace) -> int:
    if args.what == "ser2net":
        print(ser2net_yaml(host=args.host))
        return 0
    # socat: one bridge invocation per console stream (uart0/uart1/swo).
    for spec in console_specs():
        print(" ".join(socat_argv(spec, args.host)))
    return 0


# --------------------------------------------------------------------------- #
# console  (the pure-Python socat_consoles.sh replacement)
# --------------------------------------------------------------------------- #
def _cmd_console(args: argparse.Namespace) -> int:
    if args.pty is not None and args.listen is not None:
        print("console: --pty and --listen are mutually exclusive", file=sys.stderr)
        return 2

    if args.listen is not None:
        local = TcpListenEndpoint(port=args.listen)
    else:
        # default: a pts symlinked under /tmp/mps3-console/<name> (matches the
        # socat/ser2net accepter path so downstream tools find it either way).
        link = args.pty if args.pty is not None else f"/tmp/mps3-console/{args.stream}"
        local = PtyEndpoint(link)

    harness = SocketHarness(host=args.host)
    bridge = harness.bridge(args.stream, local)
    try:
        bridge.run_forever()
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        print(
            f"console: cannot bridge {args.stream} to {args.host}: {exc}",
            file=sys.stderr,
        )
        return 3
    return 0


# --------------------------------------------------------------------------- #
# reg  (CSR / mailbox ops — the mps3_diag.tcl port)
# --------------------------------------------------------------------------- #
def _cmd_reg(args: argparse.Namespace) -> int:
    target = args.target
    if "." not in target:
        print(
            f"reg: target must be block.reg (e.g. dfxctl.RM_STATUS), got {target!r}",
            file=sys.stderr,
        )
        return 2
    block, _, reg = target.partition(".")
    block = block.lower()

    if block not in BLOCKS:
        print(
            f"reg: unknown block {block!r}; known: {', '.join(sorted(BLOCKS))}",
            file=sys.stderr,
        )
        return 2
    rb = BLOCKS[block]
    try:
        rb.reg(reg)  # validates the register name (ValueError if absent)
    except ValueError as exc:
        print(f"reg: {exc}", file=sys.stderr)
        return 2

    fields: dict[str, int] = {}
    if args.op == "write":
        try:
            fields = _parse_kv(args.assignments)
            rb.encode(reg, **fields)  # validate field names up-front (usage error)
        except ValueError as exc:
            print(f"reg: {exc}", file=sys.stderr)
            return 2
    elif args.assignments:
        print("reg: 'read' takes no field assignments", file=sys.stderr)
        return 2

    addr = rb.addr(reg)

    # ---- board-free preview -------------------------------------------------
    if args.dry_run:
        from .xsdb import XsdbConfig, XsdbRegisterEndpoint

        cfg = XsdbConfig(hub_url=args.hub_url) if args.hub_url else XsdbConfig()
        backend = XsdbRegisterEndpoint(cfg, dry_run=True)
        if args.op == "write":
            val = rb.encode(reg, **fields)
            preview = backend.write_word(addr, val)
            head = f"# {block}.{reg} write 0x{addr:08X} <- {_fmt_hex(val)}"
        else:
            preview = backend.read_word(addr)
            head = f"# {block}.{reg} read 0x{addr:08X}"
        print(head)
        print(_render_preview(preview))
        return 0

    # ---- live xsdb path -----------------------------------------------------
    harness = SocketHarness(host=args.host, hub_url=args.hub_url or DEFAULT_HUB_URL)
    access = harness.registers()
    try:
        if args.op == "write":
            access.write_reg(block, reg, **fields)
            print(json.dumps({"ok": True, "wrote": f"{block}.{reg}", "fields": fields}))
        else:
            decoded = access.read_reg(block, reg)
            print(json.dumps({"reg": f"{block}.{reg}", "addr": f"0x{addr:08X}",
                              "fields": decoded}))
    except Exception as exc:  # xsdb TargetNotFound / OSError / subprocess failure
        # A live register op that fails means the JTAG/hub path did not answer;
        # that is "unreachable" (3), reported as a clean one-liner, not a trace.
        print(f"reg: {block}.{reg}: xsdb path unreachable: {exc}", file=sys.stderr)
        return 3
    return 0


# --------------------------------------------------------------------------- #
# probe  (honest one-line liveness)
# --------------------------------------------------------------------------- #
def _cmd_probe(args: argparse.Namespace) -> int:
    try:
        by_name(args.name)  # usage-validate the endpoint name (exit 2 if unknown)
    except ValueError as exc:
        print(f"probe: {exc}", file=sys.stderr)
        return 2

    harness = SocketHarness(host=args.host)
    result: ProbeResult = harness.probe(args.name)

    parts = [f"{result.name}: {result.status}"]
    if result.detail:
        parts.append(result.detail)
    if result.value is not None:
        parts.append(f"({_fmt_hex(result.value)})")
    print(" ".join(parts))
    return _STATUS_EXIT.get(result.status, 3)


# --------------------------------------------------------------------------- #
# selftest  (aggregate board-free proofs — the mps3_console --self-test role)
# --------------------------------------------------------------------------- #
def _st_pty_loopback() -> None:
    """A local pty round-trip with a baked-in negative control (no board)."""
    import os
    import select

    from .loopback import pty_loopback

    def _drain(fd: int, deadline: float) -> bytes:
        import time

        out = b""
        while time.monotonic() < deadline:
            r, _, _ = select.select([fd], [], [], 0.1)
            if not r:
                continue
            try:
                chunk = os.read(fd, 4096)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                break
            if chunk:
                out += chunk
                if b"\n" in out:
                    break
        return out

    import time

    ctrl, stop, thread = pty_loopback()
    try:
        os.write(ctrl, b"hello\r")
        got = _drain(ctrl, time.monotonic() + 3.0)
        assert b"WORLD_42" in got, f"round trip: missing marker in {got!r}"
        # negative control: junk must NOT elicit the success marker.
        os.write(ctrl, b"goodbye\r")
        junk = _drain(ctrl, time.monotonic() + 3.0)
        assert b"unknown" in junk and b"WORLD_42" not in junk, (
            f"negative control leaked marker: {junk!r}"
        )
    finally:
        stop.set()
        try:
            os.close(ctrl)
        except OSError:
            pass
        thread.join(timeout=1.0)


def _st_retry_schedule() -> None:
    """Deterministic backoff schedule + sleeps actually happen."""
    from .loopback import FakeClock
    from .retry import ConnectFailed, RetryPolicy, connect_with_retry

    policy = RetryPolicy(attempts=5, base=0.1, factor=2.0, cap=5.0)
    want = [0.1, 0.2, 0.4, 0.8]
    got = policy.backoffs()
    assert len(got) == len(want) and all(abs(a - b) < 1e-9 for a, b in zip(got, want)), (
        f"backoff schedule {got} != {want}"
    )

    clock = FakeClock()
    state = {"n": 0}

    def open_once():
        state["n"] += 1
        if state["n"] < 3:
            raise OSError("connection refused")
        return "SOCK"

    retries: list[int] = []
    value = connect_with_retry(open_once, policy, clock, on_retry=lambda a, _e: retries.append(a))
    assert value == "SOCK", "connect_with_retry lost the successful value"
    assert len(retries) == 2, f"expected 2 retries, got {len(retries)}"
    # 2 consumed backoffs (0.1 + 0.2) must have elapsed on the virtual clock.
    assert abs(clock.monotonic() - 0.3) < 1e-6, (
        f"backoff sleeps not honoured: elapsed {clock.monotonic()}"
    )

    def always_fail():
        raise OSError("nope")

    try:
        connect_with_retry(always_fail, policy, FakeClock())
    except ConnectFailed:
        pass
    else:
        raise AssertionError("all-fail did not raise ConnectFailed")


def _st_register_codec() -> None:
    """Pure decode/encode + the read_safe dump-page exclusion property."""
    dfx = BLOCKS["dfxctl"]
    assert dfx.decode("RM_STATUS", 0b101) == {
        "rm_id_valid": 1,
        "dut_lockup": 0,
        "dut_eth_irq": 1,
    }, "DFXCTL.RM_STATUS decode wrong (RM_STATUS[2]=dut_eth_irq)"
    assert BLOCKS["vphy"].encode("LINK_EVENT", force_down=1, pulse=1) == 0b11, "VPHY encode"
    assert BLOCKS["genchk"].encode("INJECT", giant=1) == 0b100, "GENCHK.INJECT encode"

    class _Rec:
        def __init__(self):
            self.reads: list[int] = []
        def read_word(self, addr: int) -> int:
            self.reads.append(addr)
            return 0
        def write_word(self, addr: int, val: int) -> None:  # pragma: no cover
            pass
        def read_block(self, addr: int, n: int) -> list[int]:
            return [self.read_word(addr + 4 * i) for i in range(n)]

    rec = _Rec()
    RegisterAccess(rec).dump_page("dfxctl")
    popped = dfx.base + 0x20  # CONSOLE_POP: read_safe=False, must be skipped
    assert popped not in rec.reads, "dump_page read the destructive CONSOLE_POP (0x20)"


def _st_xsdb_parse() -> None:
    from .xsdb import parse_mrd_block, parse_mrd_value

    assert parse_mrd_value("00000001") == 1, "bare mrd -value parse"
    assert parse_mrd_value("44A10010:   00000001") == 1, "labelled mrd parse"
    assert parse_mrd_block("00000001 00000002", 2) == [1, 2], "mrd block parse order"


def _st_session_reassembly() -> None:
    from .endpoints import by_name as _by_name
    from .loopback import fake_opener
    from .session import LineSession, NotConnected

    # NotConnected guard: recv before connect() must raise, not touch a socket.
    guard = LineSession(_by_name("control").resolve("127.0.0.1"))
    try:
        guard.recv_line()
    except NotConnected:
        pass
    else:
        raise AssertionError("recv_line before connect did not raise NotConnected")

    # A single line split across two recv chunks reassembles into one line.
    opener = fake_opener([b"par", b"tial\n"])
    sess = LineSession(_by_name("control").resolve("127.0.0.1"), opener=opener)
    sess.connect()
    try:
        line = sess.recv_line()
        assert line == b"partial", f"recv_line reassembly wrong: {line!r}"
    finally:
        sess.close()


def _st_harness_interop() -> None:
    """The keystone: LineChannel === pyverify Transport, so shell() just works."""
    from .loopback import fake_opener

    opener = fake_opener([b'{"ok": true, "shell_id": "0x0", "rm_id": "0x1"}\n'])
    harness = SocketHarness(host="127.0.0.1", opener=opener)
    shell = harness.shell()
    shell.connect()
    try:
        resp = shell.ping()
    finally:
        shell.close()
    assert resp.ok and resp.rm_id == "0x1", f"pyverify ping over LineSession failed: {resp}"


_SELFTESTS = (
    ("pty loopback", _st_pty_loopback),
    ("retry schedule", _st_retry_schedule),
    ("register codec", _st_register_codec),
    ("xsdb parse", _st_xsdb_parse),
    ("session reassembly", _st_session_reassembly),
    ("harness/pyverify interop", _st_harness_interop),
)


def _cmd_selftest(args: argparse.Namespace) -> int:
    print("socket_harness selftest (board-free, no real socket):")
    failures: list[str] = []
    for name, fn in _SELFTESTS:
        try:
            fn()
        except Exception as exc:  # a proof failing is data, never a traceback
            failures.append(f"{name}: {exc}")
            print(f"  [FAIL] {name}: {exc}")
        else:
            print(f"  [PASS] {name}")
    print()
    if failures:
        print(f"SELFTEST FAILED ({len(failures)}/{len(_SELFTESTS)}):", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print(f"SELFTEST PASSED ({len(_SELFTESTS)}/{len(_SELFTESTS)}) — harness verified board-free")
    return 0


# --------------------------------------------------------------------------- #
# argument parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="socket_harness", description=__doc__)
    sub = parser.add_subparsers(dest="verb", required=True)

    p_ep = sub.add_parser("endpoints", help="print the frozen endpoint registry")
    p_ep.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    p_ep.set_defaults(func=_cmd_endpoints)

    p_emit = sub.add_parser(
        "emit", help="regenerate a console config (ser2net.yaml / socat) from the registry"
    )
    p_emit.add_argument("what", choices=("ser2net", "socat"))
    p_emit.add_argument("--host", default=DEFAULT_SHELL_HOST, help="shell host/IP")
    p_emit.set_defaults(func=_cmd_emit)

    p_con = sub.add_parser(
        "console", help="bridge a shell console TCP port to a local pty/tcp (pure Python)"
    )
    p_con.add_argument("stream", choices=("uart0", "uart1", "swo"))
    p_con.add_argument("--pty", default=None, help="pty symlink path (default /tmp/mps3-console/<name>)")
    p_con.add_argument("--listen", type=int, default=None, help="listen on this local TCP port instead")
    p_con.add_argument("--host", default=DEFAULT_SHELL_HOST, help="shell host/IP")
    p_con.set_defaults(func=_cmd_console)

    p_reg = sub.add_parser("reg", help="read/write a CSR block.reg via xsdb (shell-regmap.md)")
    p_reg.add_argument("target", help="block.reg, e.g. dfxctl.RM_STATUS")
    p_reg.add_argument("op", nargs="?", default="read", choices=("read", "write"))
    p_reg.add_argument("assignments", nargs="*", help="field=value pairs (write only)")
    p_reg.add_argument("--dry-run", action="store_true", dest="dry_run",
                       help="print the xsdb argv/tcl without running a subprocess")
    p_reg.add_argument("--hub-url", default=None, dest="hub_url",
                       help=f"xsdb hw_server URL (default {DEFAULT_HUB_URL})")
    p_reg.add_argument("--host", default=DEFAULT_SHELL_HOST, help="shell host/IP")
    p_reg.set_defaults(func=_cmd_reg)

    p_probe = sub.add_parser("probe", help="one-line honest liveness for an endpoint")
    p_probe.add_argument("name", help="endpoint name (see `endpoints`)")
    p_probe.add_argument("--host", default=DEFAULT_SHELL_HOST, help="shell host/IP")
    p_probe.set_defaults(func=_cmd_probe)

    p_self = sub.add_parser("selftest", help="run the board-free proof suite and exit 0/1")
    p_self.set_defaults(func=_cmd_selftest)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)  # argparse exits 2 on a usage error
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
    except Exception as exc:  # last-resort net: a clean one-liner, never a trace
        print(f"socket_harness {getattr(args, 'verb', '')}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
