#!/usr/bin/env python3
"""Push a DUT partial bitstream to the MPS3 shell over the wire.

**This is now a thin wrapper over pyverify.** It used to carry its own copy of
the 24-byte push header, the raw-TCP socket work, the swap-before-payload
ordering and the manifest reader — four things ``pyverify.pusher`` /
``pyverify.swap`` / ``pyverify.fielded`` already own, and a second copy of a
wire format is a second thing to get wrong. The CLI, the stdout lines
(``before:`` / ``swap response:`` / ``after:``) and the exit codes are
UNCHANGED, because ``scripts/harness_gates/swap_check.py`` and
``scripts/harness_regression.sh`` read them.

    scripts/mps3_push.py --prod fielded/0x3F1A560F --rm regdemo_b
    scripts/mps3_push.py --ping

    # explicit, no manifest
    scripts/mps3_push.py --static-id 0xCAAE8C30 --rm-id 0xB2 \
        --clearing a_clear.bin --partial a.bin --rm-name regdemo_b

PERSIST (net-protocol.md v0.13, D13). This is a HUMAN tool, so it keeps the
library default: after a verified swap the pair is committed to the user
microSD and the board reloads it at power-on (skipped silently with no card).
``--no-persist`` opts out. AUTOMATION MUST OPT OUT -- a regression, proof or
sweep campaign must never rewrite the board's boot default -- and does it with
the environment instead of the flag: ``MPS3_PUSH_PERSIST=0``. An older staged
copy of this script (a mint stage made before D13) has no ``--no-persist`` and
would reject it, but ignores the variable -- and an older pyverify never
persists in the first place -- so one command line is safe against every
stage. The persist outcome goes to STDERR; stdout's lines are unchanged.

There is NO default --prod. The old one (and swap_check.py's) named a build
directory that had gone dead two mints earlier and does not exist in a fresh
clone; ``pyverify.fielded`` resolves the fielded shell's artefact dir from
``docs/FIELDED_SHELL.md`` instead, and ``--prod`` overrides it.

Run it from a host that can reach the board (currently only the fpgahub host;
the board sits on the hub's board-side interface).

TAKE THE BOARD LEASE FIRST -- `pyverify lease acquire` (or
scripts/mps3_board.sh, which now execs it) and docs/internal/MPS3_BOARD_LEASE.md.
A concurrent xsdb `stop` or a second stream will corrupt the transfer.

THINGS LEARNED THE HARD WAY -- all of them now live in the library, and are
named here so the reasons do not get lost with the code:
  * The shell closes the 6910 socket as soon as it finishes validating. A RST on
    the drain is NORMAL, not an error.  (pyverify.pusher)
  * NEVER abort a client mid-transfer. Until the AWAIT_* idle timeout landed
    (f5825db), a dead client parked the shell forever and recovery needed a JTAG
    bitstream reload. Timeouts are deliberately generous.
  * A swap request must be in flight on 6900 BEFORE the payloads are pushed:
    config_agent's begin_payload() fails closed unless the FSM has already
    asserted DECOUPLE + rp_reset.  (pyverify.swap.SwapOrchestrator.deploy)
  * The clearing must be pushed before the partial (the shell enforces this
    ordering and RSTs a partial that arrives first).  (pyverify.swap)
"""
import argparse
import json
import os
import sys
from pathlib import Path

# --- find pyverify ---------------------------------------------------------
# This file is STAGED ON ITS OWN to the hub by scripts/harness_regression.sh
# (the repo is not shared there), so "just import it" is not enough: look in
# the repo, then beside the staged copy, then in $PYVERIFY_DIR. If none of
# those has it, say exactly that rather than dying in an ImportError -- a
# staging gap must not read as a board fault.
_HERE = Path(__file__).resolve().parent
for _cand in (
    os.environ.get("PYVERIFY_DIR"),
    _HERE.parent / "host" / "pyverify",     # in-repo
    _HERE,                                  # staged: pyverify/ sits beside us
):
    if _cand and (Path(_cand) / "pyverify" / "__init__.py").is_file():
        sys.path.insert(0, str(_cand))
        break
try:
    from pyverify.client import ShellClient
    from pyverify.fielded import FieldedError, overlay_from_files, overlay_from_prod_dir
    from pyverify.overlay import OverlayValidationError
    from pyverify.pusher import BitstreamPusher, PushError
    from pyverify.swap import SwapError, SwapOrchestrator
except ImportError as exc:  # pragma: no cover - staging gap, not a board fault
    sys.exit(
        "mps3_push.py: cannot import pyverify (%s).\n"
        "  This script is a thin wrapper over it. Set PYVERIFY_DIR=<repo>/host/"
        "pyverify, or stage that directory beside this file (see "
        "scripts/harness_regression.sh's staging step)." % exc
    )

BOARD_DEFAULT = os.environ.get("MPS3_BOARD_HOST", "192.168.10.101")
# net-protocol.md's port map. Overridable so the wrapper can be exercised
# end-to-end against a FakeShell on ephemeral loopback ports -- the same seam
# pyverify.cli's --control-port/--tcp-push-port offer.
PORT_CTRL = int(os.environ.get("MPS3_PUSH_CTRL_PORT", "6900"))
PORT_STREAM = int(os.environ.get("MPS3_PUSH_STREAM_PORT", "6910"))


def _env_says_no_persist() -> bool:
    """``MPS3_PUSH_PERSIST=0`` (or no/false/off): the automation opt-out."""
    return os.environ.get("MPS3_PUSH_PERSIST", "").strip().lower() in ("0", "no", "false", "off")


def _deploy(orchestrator, overlay, persist):
    """deploy() with ``persist`` passed only when the staged pyverify knows it:
    an older pyverify has no persist step at all, which is what persist=False
    asks for."""
    import inspect
    if "persist" in inspect.signature(orchestrator.deploy).parameters:
        return orchestrator.deploy(overlay, src="tcp", persist=persist)
    return orchestrator.deploy(overlay, src="tcp")


def _report_persist(result, persist):
    p = getattr(result, "persist", None)
    if not persist:
        print("persist: off (--no-persist / MPS3_PUSH_PERSIST=0)", file=sys.stderr)
    elif p is not None:
        print("persist: %s" % json.dumps({"status": p.status, "slot": p.slot,
                                          "reason": p.reason, "err": p.err}),
              file=sys.stderr)
        if p.status == "failed" or getattr(p, "warning", False):
            print("mps3_push: WARNING: the swap stands, but it was not persisted to the "
                  "user microSD: %s" % (p.err or p.reason), file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--board", default=BOARD_DEFAULT)
    ap.add_argument("--timeout", type=float, default=900.0,
                    help="generous on purpose: never abort mid-transfer")
    ap.add_argument("--ping", action="store_true")
    ap.add_argument("--prod", help="DFX prod dir holding static_id.txt + overlay_inputs.txt "
                                   "(default: the fielded shell's, via pyverify.fielded)")
    ap.add_argument("--rm", help="RM name (e.g. regdemo_b) -- resolved via --prod")
    ap.add_argument("--static-id", type=lambda v: int(v, 0))
    ap.add_argument("--rm-id", type=lambda v: int(v, 0))
    ap.add_argument("--rm-name")
    ap.add_argument("--clearing")
    ap.add_argument("--partial")
    ap.add_argument("--windowed", action="store_true",
                    help="lock-step window-grant handshake -- REQUIRED against a "
                         "real FIFO-mode shell (net-protocol.md v0.5)")
    ap.add_argument("--no-persist", action="store_true", dest="no_persist",
                    help="do not commit the pair to the user microSD after the swap "
                         "(default: persist, net-protocol.md v0.13). Automation sets "
                         "MPS3_PUSH_PERSIST=0 instead (see the module docstring)")
    a = ap.parse_args()
    persist = not (a.no_persist or _env_says_no_persist())

    if a.ping:
        with ShellClient(a.board, port=PORT_CTRL, timeout=10.0) as client:
            resp = client.ping()
        print(json.dumps({"ok": resp.ok, "shell_id": resp.shell_id, "rm_id": resp.rm_id}))
        return 0

    try:
        if a.rm:
            prod = a.prod
            if not prod:
                from pyverify.fielded import load as load_fielded
                prod = load_fielded().require_prod_dir()
            overlay = overlay_from_prod_dir(Path(prod), a.rm)
        else:
            missing = [n for n in ("static_id", "rm_id", "rm_name", "clearing", "partial")
                       if getattr(a, n) is None]
            if missing:
                ap.error("need --rm (with or without --prod), or all of: --"
                         + ", --".join(missing))
            overlay = overlay_from_files(
                static_id=a.static_id, rm_id=a.rm_id, rm_name=a.rm_name,
                clearing=a.clearing, partial=a.partial,
            )
    except FieldedError as exc:
        print("mps3_push: %s" % exc, file=sys.stderr)
        return 1

    m = overlay.manifest
    client = ShellClient(a.board, port=PORT_CTRL, timeout=a.timeout)
    pusher = BitstreamPusher(host=a.board, transport="tcp", tcp_port=PORT_STREAM,
                             windowed=a.windowed)

    with ShellClient(a.board, port=PORT_CTRL, timeout=10.0) as probe:
        before = probe.ping()
    print("before: %s" % json.dumps(
        {"ok": before.ok, "shell_id": before.shell_id, "rm_id": before.rm_id}), flush=True)
    print("swap -> %s (rm_id 0x%08x, static_id 0x%08x)" % (m.rm_name, m.rm_id, m.static_id),
          flush=True)

    try:
        with client:
            result = _deploy(SwapOrchestrator(client, pusher), overlay, persist)
    except (SwapError, OverlayValidationError, PushError, OSError) as exc:
        print("swap response: <swap failed: %r>" % (exc,), flush=True)
        return 1
    _report_persist(result, persist)

    print("swap response: %s" % json.dumps(
        {"ok": True, "verified": result.verified, "rm_id": result.rm_id}), flush=True)
    with ShellClient(a.board, port=PORT_CTRL, timeout=10.0) as probe:
        after = probe.ping()
    print("after:  %s" % json.dumps(
        {"ok": after.ok, "shell_id": after.shell_id, "rm_id": after.rm_id}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
