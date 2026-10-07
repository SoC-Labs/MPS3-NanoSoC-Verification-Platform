#!/usr/bin/env python3
"""Tier-0 gate: static lint of shell_bd.tcl CONFIG values that pass
validate_bd_design but fail LATER (at IP generation / synthesis) OR are silent
correctness/boundary hazards on silicon. Reads shell_bd.tcl WITHOUT opening
Vivado. Pure stdlib, seconds, no tools.

    python3 check_bd_config_lint.py [--repo ROOT]

CHECKS
------
1. bug #3 — dfx_decoupler DECOUPLED_VALUE must be a hex STRING ("0x0"). A bare
   integer `0` passes `set_property` AND `validate_bd_design`, then fails ~45 s
   into the build at IP generation with "isn't a valid hexidecimal value".
   validate_bd_design is not a sufficient gate; this static check is.

2. QSPI safe-idle clamp (boundary freeze 2026-07-15, commits 9a259f9/541a513).
   The +14-pin Flash/QSPI XiP crossing added four RP-drive decoupler interfaces
   (qspi_sclk/csn/io_o/io_oe, IDs 15-18). This is the ONE decoupler group with a
   MIXED clamp: qspi_csn MUST clamp to 0x1 (SST26 chip DESELECTED) so a swap or a
   decoupled RP can never leave the external flash asserted; sclk/io_o/io_oe MUST
   clamp to 0x0. A qspi_csn clamped to 0x0 would drive the flash chip-select
   active during a partial reconfiguration — a real silicon hazard invisible to
   validate_bd_design.

3. No shell SPI master on the SST26 (D13). axi_quad_spi_0 was removed when
   usd_spi_0 took the 0x44A4_0000 page; no axi_quad_spi cell may come back.
   (Was: axi_quad_spi_0 mode lockstep, while that cell still existed.)

4. SPI_0 relinquishment (541a513). No live Tcl may export/connect a SPI_0
   external interface — see the long comment at the check.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys


# Expected safe-idle clamp for the QSPI XiP crossing (boundary freeze
# 2026-07-15, shell_bd.tcl SECTION 4 dfx_decoupler_boundary IDs 15-18).
QSPI_SAFE_IDLE = {
    "qspi_sclk": "0x0",
    "qspi_csn":  "0x1",   # chip DESELECTED while decoupled — the load-bearing one
    "qspi_io_o": "0x0",
    "qspi_io_oe": "0x0",
}


def _strip_tcl_comment(line: str) -> str:
    """Drop a Tcl comment tail. `#` never appears inside the live BD commands or
    the 0x-hex DECOUPLED_VALUEs we scan, so cutting at the first `#` cleanly
    removes both whole-line `#` comments and `;#` inline comments."""
    return line.split("#", 1)[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None)
    args = ap.parse_args()
    root = args.repo or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    bd = os.path.join(root, "fpga", "shell", "bd", "shell_bd.tcl")
    if not os.path.isfile(bd):
        print("FAIL: cannot find %s" % bd, file=sys.stderr)
        return 1
    text = open(bd).read()

    violations: list[str] = []

    # --- Check 1: DECOUPLED_VALUE hex-string lockstep (bug #3) ----------------
    # Only REAL dict entries, not the prose in the big schema comment: an actual
    # entry is `... MANAGEMENT <mgmt> DECOUPLED_VALUE <v> }`. The value must be a
    # 0x-hex string. Accepts 0x0, 0x1, 0xdeadbeef; rejects bare 0, 255, etc.
    entry_rx = re.compile(r"MANAGEMENT\s+\w+\s+DECOUPLED_VALUE\s+(\S+?)\s*\}")
    entries = entry_rx.findall(text)
    for val in entries:
        if not re.fullmatch(r"0[xX][0-9a-fA-F]+", val):
            violations.append(
                "DECOUPLED_VALUE '%s' is not a 0x-hex string -- passes "
                "validate_bd_design, FAILS at IP generation (bug #3)." % val)

    # --- Check 2: QSPI safe-idle clamp semantics (9a259f9 / 541a513) ----------
    # Parse each decoupler INTF entry as `<name> { ID <n> ... DECOUPLED_VALUE
    # <v> }` and enforce the MIXED clamp for the four QSPI signals. The generic
    # hex check above only validates FORMAT; this validates the load-bearing
    # VALUE. qspi_csn=0x0 would assert the SST26 chip-select during a swap.
    intf_rx = re.compile(
        r"^\s*(\w+)\s*\{\s*ID\s+\d+\b.*?DECOUPLED_VALUE\s+(0[xX][0-9a-fA-F]+)",
        re.M)
    intf_vals = {name: val for name, val in intf_rx.findall(text)}
    qspi_checked = 0
    for sig, want in QSPI_SAFE_IDLE.items():
        got = intf_vals.get(sig)
        if got is None:
            violations.append(
                "QSPI decoupler interface '%s' is MISSING from "
                "dfx_decoupler_boundary -- the +14-pin QSPI XiP crossing "
                "(boundary freeze 2026-07-15, commits 9a259f9/541a513) declares "
                "qspi_sclk/csn/io_o/io_oe as decoupler IDs 15-18. Its absence "
                "means the boundary regressed." % sig)
            continue
        qspi_checked += 1
        if int(got, 16) != int(want, 16):
            violations.append(
                "QSPI safe-idle clamp: '%s' DECOUPLED_VALUE is %s, expected %s. "
                "qspi_csn must clamp to 0x1 (SST26 DESELECTED while the RP is "
                "decoupled); sclk/io_o/io_oe must clamp to 0x0. A wrong clamp "
                "here asserts the external flash during a partial swap -- a "
                "silicon hazard validate_bd_design cannot see." % (sig, got, want))

    # --- Check 3: no axi_quad_spi cell at all (D13) ---------------------------
    # History: post-541a513 (QSPI boundary freeze, D16) the SST26VF064B pads
    # belong to the RP's own QSPI controller, and axi_quad_spi_0 survived only
    # as a pad-less AXI-Lite window @0x44A40000 (OVLSTORE); this check used to
    # hold it in standard SPI mode. D13 (docs/planning/HANDOVER_USD_OVERLAY_STORE.md)
    # REMOVED the cell: the overlay store moved onto the USER microSD, and
    # usd_spi_0 owns the 0x44A4_0000 page. A re-added axi_quad_spi would be a
    # second SPI master with nothing legitimate to drive -- the SST26 is the
    # RP's, and the user microSD is usd_spi_0's -- and it would silently take
    # the page back in the generated regmap. So: no LIVE create_bd_cell of
    # axi_quad_spi may appear (comments naming it are fine) -- in shell_bd.tcl
    # OR in any file it sources (the CPU seam's cpu_mb/cpu_mbv.tcl, the gated
    # touch_iic_add.tcl, ...): since the ONE-BD restructure a cell created in a
    # CPU file is as much a part of the static as one created in shell_bd.tcl.
    spi_checked = 0
    qspi_live = []
    bd_dir = os.path.dirname(bd)
    for path in sorted(glob.glob(os.path.join(bd_dir, "*.tcl"))):
        rel = os.path.relpath(path, root)
        with open(path) as fh:
            for i, line in enumerate(fh.read().splitlines(), 1):
                code = _strip_tcl_comment(line)
                if "create_bd_cell" in code:
                    spi_checked += 1
                    if "axi_quad_spi" in code:
                        qspi_live.append("%s:%d: %s" % (rel, i, line.strip()))
    if qspi_live:
        violations.append(
            "the shell BD instantiates an axi_quad_spi again, but D13 removed it: "
            "the SST26 belongs to the RP (D16) and 0x44A4_0000 is usd_spi_0's "
            "(the user microSD). A shell SPI master here has nothing legitimate to "
            "drive:\n     " + "\n     ".join(qspi_live))
    if spi_checked == 0:
        violations.append("found no create_bd_cell lines in fpga/shell/bd/*.tcl -- the "
                          "axi_quad_spi absence check did not run (rename? refactor?)")

    # --- Check 4: SPI_0 external interface stays RELINQUISHED (541a513) -------
    # 541a513 removed axi_quad_spi_0's SPI_0 external interface: "the RP now owns
    # the SST26 pads for XiP ... axi_quad_spi_0's SPI_0 master interface is left
    # intentionally unconnected". Re-exporting SPI_0 (create_bd_intf_port,
    # make_bd_intf_pins_external, or a connect_bd_intf_net on any <cell>/SPI_0)
    # would resurrect the wrapper's orphan SPI IOBUFs that would FIGHT the RP for
    # the physical flash pads -- two drivers on one net, a contention that only
    # shows up on the board. So: no LIVE (non-comment) reference to SPI_0 may
    # appear in shell_bd.tcl. (The `# ... SPI_0 ...` explanatory comments are
    # fine; only live Tcl is scanned.) Still meaningful after D13 removed the
    # cell: it is the net under Check 3.
    spi0_live = []
    for i, line in enumerate(text.splitlines(), 1):
        code = _strip_tcl_comment(line)
        if "SPI_0" in code:
            spi0_live.append("line %d: %s" % (i, line.strip()))
    if spi0_live:
        violations.append(
            "axi_quad_spi_0's SPI_0 external interface was RELINQUISHED in "
            "541a513 (RP owns the SST26 pads), but LIVE Tcl still references "
            "SPI_0 -- re-exporting it makes the wrapper's orphan SPI IOBUFs "
            "fight the RP for the flash pads on silicon:\n     "
            + "\n     ".join(spi0_live))

    print("== Tier-0 shell_bd.tcl CONFIG lint ==")
    print("   [1] scanned %d DECOUPLED_VALUE dict entries (hex-string, bug #3)" % len(entries))
    print("   [2] checked %d/%d QSPI safe-idle clamps (9a259f9/541a513)"
          % (qspi_checked, len(QSPI_SAFE_IDLE)))
    print("   [3] scanned %d create_bd_cell line(s) in fpga/shell/bd/*.tcl: no axi_quad_spi may exist (D13)" % spi_checked)
    print("   [4] scanned for live SPI_0 references (must be relinquished, 541a513)")
    for v in violations:
        print("   VIOLATION  %s" % v)
    if violations:
        print("\nFAIL: %d CONFIG value(s) will pass validate_bd_design and then "
              "fail at IP generation or on silicon." % len(violations))
        return 1
    print("\nOK: all DECOUPLED_VALUE entries are 0x-hex strings")
    print("OK: QSPI safe-idle clamp is qspi_csn=0x1 / sclk,io_o,io_oe=0x0 (SST26 deselected while decoupled)")
    print("OK: no axi_quad_spi cell -- 0x44A4_0000 is usd_spi_0's (D13), the SST26 is the RP's (D16)")
    print("OK: no live SPI_0 external interface -- pads are RP-owned (541a513)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
