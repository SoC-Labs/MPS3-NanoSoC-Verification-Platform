#!/usr/bin/env python3
"""Tier-0 gate: every CSR block bench must elaborate the RTL at the SAME
parameterisation shell_bd.tcl instantiates it with.

CATCHES: bug #1 (the CSR address-decode escape). shell_bd.tcl instantiates all
six shell CSR blocks with C_S_AXI_ADDR_WIDTH=32; the RTL default is 12. Every
block bench elaborated at the default 12, so the parameter under test was never
the parameter that ships. On silicon every CSR write was ignored and every read
returned 0. This gate is the generalisation the post-mortem asked for: parse the
BD's CONFIG.* overrides, parse each bench's elaboration parameters, and fail on
any (module, param, shipped-value) the benches never exercise.

Pure stdlib, no simulator, no Vivado -- runs in Tier 0 (seconds, no tools).

    python3 check_bench_param_parity.py [--waivers FILE] [--repo ROOT]

Exit 0 iff every BD-shipped parameterisation of every CSR block is covered by at
least one bench (or explicitly waived). Exit 1 (loud) otherwise.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

# vlnv-name (as it appears in shell_bd.tcl `soclabs.org:user:<name>:1.0`) ->
# REPO-RELATIVE path to the top .sv. The name is also the Verilog top module,
# which is what a bench's `-pvalue+<module>.PARAM=V` / TOPLEVEL names.
#
# WHY THESE ARE REPO-RELATIVE (changed 2026-07-24): this table used to be
# (subdir, top) rooted at fpga/shell/ip, which made any CSR block living
# ANYWHERE ELSE structurally invisible to this gate -- it could not be waived,
# could not be reported, it simply did not exist. The two ethernet CSR blocks
# (VPHY/GENCHK) live in fpga/ethernet/, so the gate that exists to catch bug #1
# was blind to the two newest copies of bug #1. Repo-relative paths + the
# unregistered-cell check in main() close that hole permanently.
CSR_BLOCKS = {
    "dut_clkrst":  ("fpga", "shell", "ip", "clkrst", "dut_clkrst.sv"),
    "dfx_ctl":     ("fpga", "shell", "ip", "dfx_ctl", "dfx_ctl.sv"),
    "board_gpio":  ("fpga", "shell", "ip", "board_gpio", "board_gpio.sv"),
    "swd_bb":      ("fpga", "shell", "ip", "swd_bb", "swd_bb.sv"),
    # REGISTERED 2026-09-09. jtag_bb replaced swd_bb as the LIVE debug block in the
    # A6 SWD->JTAG cutover and ships at C_S_AXI_ADDR_WIDTH=32 (shell_bd.tcl:561,
    # JTAGBB @ 0x44A7_0000) -- but it was never added here, so this gate reported
    # silence about the one debug block that is actually on the board while happily
    # checking swd_bb, which the BD no longer instantiates at all. That is the
    # UNREGISTERED-CELL hole this table's own comment (below) was written about,
    # reopened by a rename. It only surfaced when the gate was folded into
    # `make check` / `make check-ci`; before that nothing ran it on change.
    "jtag_bb":     ("fpga", "shell", "ip", "jtag_bb", "jtag_bb.sv"),
    "telem":       ("fpga", "shell", "ip", "telem", "telem.sv"),
    "uart_bridge": ("fpga", "shell", "ip", "uart_bridge", "uart_bridge.sv"),
    "clcd":        ("fpga", "shell", "ip", "clcd", "clcd.sv"),
    "clcd_kvm":    ("fpga", "shell", "ip", "clcd_kvm", "clcd_kvm.sv"),
    # REGISTERED 2026-09-11 with the block itself. dut_egress is the DUT
    # Ethernet return path (DUTEGR @0x44B2_0000) and ships at
    # C_S_AXI_ADDR_WIDTH=32 like every other shell CSR block; tests/dut_egress's
    # `csr` arm elaborates it there. Registering it in the SAME change that adds
    # the BD cell is the whole point of the UNREGISTERED-CELL check below -- an
    # unregistered cell reads as silence, not as a complaint.
    "dut_egress":  ("fpga", "shell", "ip", "dut_egress", "dut_egress.sv"),
    # REGISTERED 2026-09-14 with the block itself, for the same reason
    # dut_egress was: an unregistered cell reads as SILENCE, not as a complaint.
    # usr_access_rd is USRACC @0x44B3_0000 (the fabric build-identity readback)
    # and ships at C_S_AXI_ADDR_WIDTH=32 like every other shell CSR block;
    # tests/usr_access_rd elaborates it there, so no waiver is needed. For this
    # block a mis-decoded register reads 0, and 0 is a PLAUSIBLE build identity
    # -- bug #1 here would not look broken, it would look like an answer.
    "usr_access_rd": ("fpga", "shell", "ip", "usr_access_rd", "usr_access_rd.sv"),
    # REGISTERED with the block itself (D13, 2026-09). usd_spi is USD
    # @0x44A4_0000, the user-microSD SPI master that replaces axi_quad_spi_0 on
    # that page, and ships at C_S_AXI_ADDR_WIDTH=32. tests/usd_spi's CFG=bd
    # elaborates it there (and at the shipped DEBOUNCE_CYCLES default), driving
    # 0x44A4_0000 + offset -- no waiver needed.
    "usd_spi":     ("fpga", "shell", "ip", "usd_spi", "usd_spi.sv"),
    # --- ethernet CSR blocks (fpga/ethernet, NOT fpga/shell/ip) -------------
    # Registered ahead of the BD landing them, deliberately: registration is
    # what makes their benches count, and what makes the unregistered-cell
    # check below stay quiet when the virtual-PHY subsystem is instantiated.
    "mdio_phy_model": ("fpga", "ethernet", "mdio_phy_model", "mdio_phy_model.sv"),
    "gen_checker":    ("fpga", "ethernet", "gen_checker", "gen_checker.sv"),
    "eth_mac_test_subsystem": ("fpga", "ethernet", "eth_mac_test_subsystem.sv"),
}

# NESTING LIMIT, stated so nobody mistakes this gate for more than it is:
# it compares TOP-LEVEL BD cells against benches. mdio_phy_model and
# gen_checker are not top-level cells -- they are instantiated INSIDE
# eth_mac_test_subsystem, which hardcodes .C_S_AXI_ADDR_WIDTH(12) on both. So
# no CONFIG.* override can reach them and this gate cannot police their width.
# What covers them is the forward-guard pair
# tests/csr_decode_width BLOCK={mdio_phy_model,gen_checker}, which elaborates
# at 32 and fails on an un-capped decode. If the subsystem ever gains a
# pass-through width parameter, this gate starts policing it automatically via
# the eth_mac_test_subsystem entry above.


def _repo_root(explicit: str | None) -> str:
    if explicit:
        return os.path.abspath(explicit)
    # scripts/harness_gates/<this> -> repo root is two levels up.
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def parse_bd_overrides(bd_tcl: str) -> dict[str, dict[str, str]]:
    """module -> {PARAM: value} for every CONFIG.<PARAM> {value} the BD applies
    to a soclabs CSR cell. Handles both `set_property CONFIG.X {V} $cell` and
    `set_property -dict [list CONFIG.X {V} CONFIG.Y {W}] $cell`."""
    text = open(bd_tcl).read()

    # cellvar -> module, from: set <cellvar> [create_bd_cell ... -vlnv soclabs.org:user:<mod>:1.0 <inst>]
    cellvar_to_mod: dict[str, str] = {}
    for m in re.finditer(
        r"set\s+(\w+)\s+\[create_bd_cell\b[^\]]*-vlnv\s+soclabs\.org:user:(\w+):[\d.]+", text
    ):
        cellvar_to_mod[m.group(1)] = m.group(2)

    overrides: dict[str, dict[str, str]] = {mod: {} for mod in CSR_BLOCKS}

    for line in text.splitlines():
        if "set_property" not in line or "CONFIG." not in line:
            continue
        cellm = re.search(r"\$(\w+)\s*\]?\s*$", line.strip())
        if not cellm:
            continue
        mod = cellvar_to_mod.get(cellm.group(1))
        if mod is None:
            continue
        for pm in re.finditer(r"CONFIG\.(\w+)\s*\{([^}]*)\}", line):
            overrides.setdefault(mod, {})[pm.group(1)] = pm.group(2).strip()
    return overrides


def parse_rtl_default(sv_path: str, param: str) -> str | None:
    """`parameter int PARAM = 12,` -> '12'."""
    if not os.path.isfile(sv_path):
        return None
    rx = re.compile(r"\bparameter\b[^;]*?\b" + re.escape(param) + r"\s*=\s*([0-9]+)")
    m = rx.search(open(sv_path).read())
    return m.group(1) if m else None


def parse_bench_coverage(tests_dir: str) -> tuple[dict, dict]:
    """Returns (default_elaborated, pvalue_cov).
    default_elaborated: module -> True if some bench has TOPLEVEL == module.
    pvalue_cov: module -> {PARAM: set(values)} from every `-pvalue+mod[./]P=V`.
    The `.` and `/` hierarchy separators are both legal VCS -pvalue syntax."""
    default_elaborated: dict[str, bool] = {}
    pvalue_cov: dict[str, dict[str, set]] = {}

    for name in sorted(os.listdir(tests_dir)):
        mk = os.path.join(tests_dir, name, "Makefile")
        if not os.path.isfile(mk):
            continue
        body = open(mk).read()
        tl = re.search(r"^\s*TOPLEVEL\s*[:?]?=\s*(\w+)", body, re.M)
        if tl and tl.group(1) in CSR_BLOCKS:
            default_elaborated[tl.group(1)] = True
        for pm in re.finditer(r"-pvalue\+(\w+)[./](\w+)=([0-9]+)", body):
            mod, param, val = pm.group(1), pm.group(2), pm.group(3)
            if mod in CSR_BLOCKS:
                pvalue_cov.setdefault(mod, {}).setdefault(param, set()).add(val)
    return default_elaborated, pvalue_cov


def load_waivers(path: str | None) -> set[tuple[str, str]]:
    waived: set[tuple[str, str]] = set()
    if not path or not os.path.isfile(path):
        return waived
    for line in open(path):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            waived.add((parts[0], parts[1]))
    return waived


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=None)
    ap.add_argument("--waivers", default=None)
    args = ap.parse_args()

    root = _repo_root(args.repo)
    bd_tcl = os.path.join(root, "fpga", "shell", "bd", "shell_bd.tcl")
    tests_dir = os.path.join(root, "tests")
    ip_dir = os.path.join(root, "fpga", "shell", "ip")

    if not os.path.isfile(bd_tcl):
        print("FAIL: cannot find %s" % bd_tcl, file=sys.stderr)
        return 1

    overrides = parse_bd_overrides(bd_tcl)
    default_elab, pvalue_cov = parse_bench_coverage(tests_dir)
    waived = load_waivers(args.waivers)

    violations: list[str] = []
    waived_hits: list[str] = []
    checked = 0

    # UNREGISTERED-CELL CHECK. The reason bug #1's two newest copies were
    # invisible is that this gate could only ever see what CSR_BLOCKS listed --
    # a block absent from the table produced silence, not a complaint. Any
    # soclabs.org:user cell the BD parameterises but the table does not know is
    # now a LOUD failure, so "we forgot to register it" can never again read as
    # "it passed".
    for mod, params in sorted(overrides.items()):
        if mod in CSR_BLOCKS or not params:
            continue
        violations.append(
            "%-11s (UNREGISTERED) BD parameterises this soclabs cell (%s) but it is "
            "absent from CSR_BLOCKS -- add it (repo-relative path to its top .sv) so "
            "its bench coverage is actually checked."
            % (mod, ",".join("CONFIG.%s" % p for p in sorted(params)))
        )

    for mod, relpath in CSR_BLOCKS.items():
        sv = os.path.join(root, *relpath)
        for param, shipped in sorted(overrides.get(mod, {}).items()):
            default = parse_rtl_default(sv, param)
            # Coverage = every pvalue value seen for this module.param, plus the
            # RTL default IF some bench elaborates this module without forcing it.
            cov = set(pvalue_cov.get(mod, {}).get(param, set()))
            if default is not None and default_elab.get(mod):
                cov.add(default)
            checked += 1
            if shipped in cov:
                continue
            msg = ("%-11s %-20s BD ships =%s  RTL default =%s  bench coverage ={%s}"
                   % (mod, param, shipped, default, ",".join(sorted(cov)) or "-"))
            if (mod, param) in waived:
                waived_hits.append(msg)
            else:
                violations.append(msg)

    print("== Tier-0 bench/BD parameter-parity gate (bug #1) ==")
    print("   BD: %s" % os.path.relpath(bd_tcl, root))
    print("   checked %d (module,param) shipped values across %d CSR blocks"
          % (checked, len(CSR_BLOCKS)))
    for m in waived_hits:
        print("   WAIVED  %s" % m)
    for m in violations:
        print("   VIOLATION  %s" % m)

    if violations:
        print("\nFAIL: %d CSR block parameterisation(s) SHIP without a bench that "
              "elaborates at the shipped value." % len(violations))
        print("      This is exactly bug #1's state: the thing under test is not the "
              "thing that ships.")
        print("      Fix: add `-pvalue+<module>.<PARAM>=<shipped>` coverage (see "
              "tests/csr_decode_width/), or waive with a tracking owner.")
        return 1

    print("\nOK: every BD-shipped CSR parameterisation is covered by a bench"
          + (" (%d waived)" % len(waived_hits) if waived_hits else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
