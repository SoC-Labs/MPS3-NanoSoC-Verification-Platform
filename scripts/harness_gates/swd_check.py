#!/usr/bin/env python3
"""Tier-3 debug-channel gate (opt-in): OpenOCD JTAG first-light against the resident DUT.

Runs ``openocd -f host/openocd/nanosoc_mps3_jtag.cfg`` in ``remote_bitbang`` mode
against the shell's firmware ``jtag_server`` on **TCP 6921**, and asserts the DUT's
JTAG TAP answers with IDCODE ``0x6ba00477`` -- proving the shell's jtag_server +
bitbang engine and the DUT's CoreSight SWJ-DP (JTAG mode) are alive over Ethernet.
This is the board-side confirmation that the swap-then-debug channel actually joins.

RETARGETED 2026-09-09 -- READ THIS BEFORE "FIXING" IT BACK.
  This gate used to run ``swd_remote_bitbang.cfg`` against **6920** (``swd_server``)
  and assert ``SWD DPIDR 0x0bb11477``. That port is DORMANT on the fielded
  ``0xA8C1C535`` shell: the A6 SWD->JTAG cutover moved the live debug path to
  ``jtag_server``/6921 (remote_bitbang, TAP 0x6ba00477), and it is the JTAG path --
  not SWD -- that is silicon-proven (an M0 halt with S_HALT=1). So the gate was
  aimed at a port nothing answers; it would have failed for a reason unrelated to
  the thing it tests, which is worse than not running, because it reads as a real
  regression. 6920/SWD is intentionally NOT a fallback here: a gate with two paths
  passes on the wrong one.

  ``nanosoc_mps3_jtag.cfg`` defaults ``TRANSPORT_MODE`` to ``rbb`` (its own header
  states rbb is the proven path and xvc is not); this gate sets it explicitly
  anyway, BEFORE the ``-f``, because the cfg picks its adapter driver at source
  time -- an override that lands after the ``-f`` is read too late and silently
  leaves the xvc front-end selected.

Like swap_check.py this is an ssh ORCHESTRATOR (openocd needs the repo cfg files
and the dataplane, both only on the hub), not a stdin-fed snippet.

PRECONDITION -- READ THIS:
  * A nanoSoC swap must already have reached DONE. The debug channel is GATED
    until a swap completes; a failed/mid swap drops the connection (swap_fsm
    15c05df). harness_regression sequences ``swap_check --rm nanosoc`` immediately
    before this.
  * Opt-in (MPS3_RUN_SWD=1): openocd is slower than the socket gates, and nanoSoC
    is left RESIDENT -- its clearing (~155 KB) overflows the swap arena and routes
    to QSPI, so nanoSoC's swap-AWAY fails closed (clearing_fit_waivers.txt). Run
    this LAST; recover to another RM via a JTAG full-shell reload, not a swap.

ON-BOARD STATUS: the retarget is proven only as far as the ARGV it builds
(``--dry-run`` + tests/integration/test_swd_check_argv.py). It has NOT been run
against the board; that run is pending a lease.

USAGE
    swd_check.py --dry-run                       # print the exact argv, run nothing
    swd_check.py --via-hub <hub-host>            # from your box, through the hub
    swd_check.py                                 # already on the hub

Exit 0 iff the expected TAP IDCODE is seen; non-zero otherwise.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys

REPO_DEFAULT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BOARD_DEFAULT = "192.168.10.101"
#: fw jtag_server, remote_bitbang JTAG (docs/contracts/net-protocol.md, MPS3_PORT_JTAG).
JTAG_PORT_DEFAULT = 6921
#: The DUT's JTAG-DP TAP, hardware-verified on this silicon. NOT the SWD DPIDR
#: 0x0bb11477 -- different protocol, different register, different value.
IDCODE_DEFAULT = "0x6ba00477"
CFG_NAME = "nanosoc_mps3_jtag.cfg"
SSH_OPTS = ["-o", "ControlPath=none", "-o", "BatchMode=yes"]

#: OpenOCD prints e.g.
#:   Info : JTAG tap: nanosoc.cpu tap/device found: 0x6ba00477 (mfg: 0x23b ...)
#: and, on a mismatch against `-expected-id`:
#:   Error: JTAG tap: nanosoc.cpu  UNEXPECTED: 0x00000000 ...
_IDCODE_RX = re.compile(r"tap/device found:\s*(0x[0-9a-fA-F]+)")


def build_cmd(args) -> list:
    # --cfg-dir holds nanosoc_mps3_jtag.cfg. Local default = the repo copy; via
    # the hub it is the STAGED copy the harness scp'd there (the repo is NOT
    # shared to the hub -- the same fix applied to swap_check). The cfg is
    # self-contained (no relative `source`), so an absolute -f path is enough.
    cfg = "%s/%s" % (args.cfg_dir.rstrip("/"), CFG_NAME)
    oocd = [
        args.openocd,
        # ORDER IS LOAD-BEARING: these three must precede the -f. The cfg's
        # `switch -- $TRANSPORT_MODE` runs while it is being sourced.
        "-c", "set TRANSPORT_MODE rbb",
        "-c", "set RBB_HOST %s" % args.board,
        "-c", "set RBB_PORT %d" % args.port,
        "-f", cfg,
        "-c", "init", "-c", "shutdown",
    ]
    if args.via_hub:
        remote = " ".join(shlex.quote(a) for a in oocd)
        return ["ssh"] + SSH_OPTS + [args.via_hub, remote]
    return oocd


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=REPO_DEFAULT,
                    help="(legacy) repo root; superseded by --cfg-dir")
    ap.add_argument("--cfg-dir", default="host/openocd",
                    help="dir holding %s; via-hub = the staged dir" % CFG_NAME)
    ap.add_argument("--board", default=BOARD_DEFAULT)
    ap.add_argument("--port", type=int, default=JTAG_PORT_DEFAULT,
                    help="fw jtag_server remote_bitbang port (default %d)"
                         % JTAG_PORT_DEFAULT)
    ap.add_argument("--via-hub", default="",
                    help="fpgahub host to ssh through; empty => run locally (on-hub)")
    ap.add_argument("--expect-idcode", default=IDCODE_DEFAULT,
                    help="required TAP IDCODE; empty => accept any tap/device found line")
    ap.add_argument("--openocd", default="openocd")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the exact argv that would be exec'd and exit 0 -- "
                         "the board-free half of this gate, and the only part "
                         "testable without a lease")
    a = ap.parse_args(argv)

    cmd = build_cmd(a)
    cwd = a.repo if not a.via_hub else None
    where = "hub %s" % a.via_hub if a.via_hub else "local (on-hub)"
    print("== Tier-3 JTAG debug gate: openocd TAP IDCODE on %s:%d  [%s] =="
          % (a.board, a.port, where))
    print("   expecting IDCODE %s (JTAG-DP; NOT the SWD DPIDR 0x0bb11477)"
          % (a.expect_idcode or "<any>"))
    print("   $ " + " ".join(shlex.quote(c) for c in cmd), flush=True)

    if a.dry_run:
        print("\nDRY RUN: nothing was executed. Run without --dry-run, on a held "
              "board lease, after a nanoSoC swap has reached DONE.")
        return 0

    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=a.timeout)
    except subprocess.TimeoutExpired:
        print("\nFAIL: openocd did not finish within %.0fs (%d hung?)."
              % (a.timeout, a.port))
        return 1
    except OSError as e:
        print("\nFAIL: could not launch openocd: %r" % e)
        return 1

    # OpenOCD writes its Info: lines to stderr; scan both streams.
    combined = (proc.stdout or "") + (proc.stderr or "")
    for line in combined.splitlines():
        low = line.lower()
        if "tap" in low or "idcode" in low or "error" in low or "cortex" in low:
            print("   | " + line.rstrip())

    m = _IDCODE_RX.search(combined)
    if not m:
        print("\nFAIL: no `tap/device found` line -- the JTAG chain did not come up.")
        print("      Checks, in order: is a nanoSoC swap resident and DONE (the debug")
        print("      channel is gated until a swap completes)? is jtag_server listening")
        print("      on %d? did the shell firmware ship WITH jtag_server?" % a.port)
        return 1
    seen = m.group(1)
    if a.expect_idcode and seen.lower() != a.expect_idcode.lower():
        print("\nFAIL: TAP IDCODE %s != expected %s (wrong TAP / unexpected DUT)."
              % (seen, a.expect_idcode))
        return 1
    print("\nOK: JTAG TAP IDCODE %s on hardware (jtag_server + bitbang engine + DUT "
          "SWJ-DP alive)." % seen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
