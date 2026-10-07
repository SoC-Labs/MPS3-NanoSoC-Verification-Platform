#!/usr/bin/env python3
"""list_benches.py — single source of truth mapping each per-block cocotb
bench to the thing that gates its readiness (see dut_presence.py).
Used by tests/Makefile so the block->RTL-file mapping isn't duplicated in
shell; also runnable standalone for a human-readable summary.

Usage:
    python3 list_benches.py            # print "READY|SKIP <name> <rtl_dir> [why]"
    python3 list_benches.py --summary  # add a trailing ready=N skip=N count line

WHAT READINESS USED TO MEAN, AND WHY IT WAS A TAUTOLOGY FOR FOUR BENCHES
-----------------------------------------------------------------------
Readiness was `rtl_ready(dir, files)`: every named file exists under `dir` and
none still carries the Phase-0 stub marker. That is exactly right for a bench
whose DUT is a block IN THIS REPOSITORY -- the file is absent until the RTL
lands, so the answer can change.

It is MEANINGLESS for the four benches whose DUT is OUTSIDE this repository.
Their entries named the bench's own committed harness (`tb_top.sv`,
`uart2_rx_harness.sv`, `qspi_xip_harness.sv`), a file that is checked in next to
the entry itself and can never be missing. READY was not a measurement; it was
`True` spelled at length.

It cost six weeks. `tests/jtag_dap_bringup` could not elaborate from 2026-07-30
(the multicore tree moved its SoC-400 wrappers into a shared tech block, so six
paths in its flist resolved to nothing) and reported READY here every single day
of it, while a stale committed `sim_build/simv` and a `results.xml` saying 8/8
sat in its directory (`8b9a2fe`, and that bench's README, "Repaired 2026-09-11").
Nothing in this file could have said otherwise: `tb_top.sv` was present, as it
always is.

WHAT IT MEANS NOW
-----------------
Readiness is `rtl_ready(...)` AND every EXTERNAL requirement the bench declares
actually resolves. A requirement is written as a path in the bench's OWN
Makefile variables, e.g.

    "$(SOCLABS_CORESIGHT_SOC400_TECH_DIR)/flist/coresight_soc400_swjdap.flist"

and is expanded by reading that Makefile's own assignments -- so the constants
are not copied here, and a bench that repoints its own variable repoints this
check with it. That one requirement is the precise thing whose absence caused
the six weeks: the tech block's filelist.

RESOLUTION IS FROM THE ENVIRONMENT, NOT FROM tools.env, and that is deliberate.
`tools.env` is an untracked, site-local *make* fragment with make's own include
and override precedence; re-implementing those rules in Python would put a
second, drifting copy of the answer in the tree, which is the failure mode this
repo keeps rediscovering. So this file reports what the ENVIRONMENT provides,
names the missing variable in the skip reason, and tests/Makefile exports
tools.env before consulting it -- one resolution mechanism, with make doing the
make part. Run standalone in a bare shell you will therefore see

    SKIP jtag_dap_bringup <dir> -- NANOSOC_MULTICORE_HOME is not set ...

which is the honest answer for that shell, and the answer a fresh clone gets.

A SKIP IS NOT A FAILURE. It never was, and this change does not make it one: a
bench whose external tree is absent is a bench that cannot run here, and saying
so is the entire job. What changed is that it can now be said at all.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dut_presence import rtl_ready  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_TESTS_ROOT = os.path.dirname(_HERE)
_REPO_ROOT = os.path.dirname(_TESTS_ROOT)


def _p(*parts):
    return os.path.join(_REPO_ROOT, *parts)


# ---------------------------------------------------------------------------
# Reading a bench Makefile's own variables, so no default is copied in here.
# ---------------------------------------------------------------------------
#: `NAME ?= value`, `NAME := value`, `NAME = value`. Nothing else is parsed:
#: conditionals, `+=` and function calls are out of scope on purpose, because a
#: half-implemented make would be worse than an explicit one.
_ASSIGN_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(\?=|:=|=)\s*(.*?)\s*$")
_VARREF_RE = re.compile(r"\$[({]([A-Za-z_][A-Za-z0-9_]*)[)}]")


def _makefile_vars(makefile: str) -> dict:
    """{name: (flavour, raw value)} for the simple assignments in `makefile`."""
    out: dict = {}
    try:
        with open(makefile, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("\t"):
                    continue  # a recipe line, not a variable
                m = _ASSIGN_RE.match(line)
                if m:
                    out[m.group(1)] = (m.group(2), m.group(3))
    except OSError:
        pass
    return out


def _expand(text: str, mvars: dict, depth: int = 0):
    """Expand $(VAR)/${VAR} against the environment, then the Makefile.

    Follows make's precedence for the two flavours that matter here: a `?=`
    default yields to an environment value, a `:=`/`=` assignment does not.
    Returns (expanded_text, unset_variable_names).
    """
    unset = []
    if depth > 8:
        return text, unset

    def one(m):
        name = m.group(1)
        flavour, raw = mvars.get(name, ("?=", ""))
        if flavour == "?=":
            val = os.environ.get(name)
            if val is None:
                val = raw
        else:
            val = raw
        val, deeper = _expand(val, mvars, depth + 1)
        unset.extend(deeper)
        if not val:
            unset.append(name)
        return val

    return _VARREF_RE.sub(one, text), unset


def external_gap(bench_dir: str, requirements) -> str | None:
    """None if every external requirement resolves; else why it does not."""
    if not requirements:
        return None
    mvars = _makefile_vars(os.path.join(bench_dir, "Makefile"))
    for req in requirements:
        path, unset = _expand(req, mvars)
        if unset:
            names = ", ".join(sorted(set(unset)))
            return ("%s is not set (and has no default) -- set it in the "
                    "environment, or in the repo-root tools.env for the make "
                    "targets that read it; see tools.env.example" % names)
        if not os.path.exists(path):
            return "%s does not exist (needed by %s)" % (path, req)
    return None


# name -> (rtl_dir, [filenames that must all be present + de-stubbed])
#      or (rtl_dir, [filenames], [external requirements])
#
# The third element is for the benches whose DUT is NOT in this repository. Each
# requirement is a path written in the BENCH'S OWN Makefile variables and
# expanded by reading that Makefile -- see external_gap() and the module
# docstring. Pick the one artefact whose absence is the real failure, not a
# directory that happens to exist.
BENCHES = {
    # Harness sanity bench (W-SIM, I24): its DUT lives inside the bench dir
    # itself and never carries a stub marker, so rtl_ready() reports READY
    # unconditionally -- by design. If sim_smoke is red, fix the simulator/
    # cocotb environment (repo-root set_env.sh) before any real bench.
    "sim_smoke":      (_p("tests", "sim_smoke"),                 ["counter.sv"]),
    "mdio_phy_model": (_p("fpga", "ethernet", "mdio_phy_model"), ["mdio_phy_model.sv"]),
    "gen_checker":    (_p("fpga", "ethernet", "gen_checker"),    ["gen_checker.sv"]),
    "rmii_phy_if":    (_p("fpga", "ethernet", "rmii_phy_if"),    ["rmii_phy_if.sv"]),
    "bridge":         (_p("fpga", "ethernet", "bridge"),         ["eth_bridge_3port.sv"]),
    # W-RTL-ETH addition: bench elaborates the MAC *paired* with rmii_phy_if
    # (bench-local tb_link_partner_pair.sv wrapper). This entry can only gate
    # on the block's own dir per the schema; the test file itself ALSO
    # skip-gates on rmii_phy_if.sv being de-stubbed (see its NO_RTL).
    "link_partner_mac": (_p("fpga", "ethernet", "link_partner_mac"),
                         ["link_partner_mac.sv"]),
    # W-ETH-SS: the §8 integration subsystem. The RTL file lives directly under
    # fpga/ethernet/ (not a per-block subdir); this entry gates on it being
    # present + de-stubbed, and the test file's own NO_RTL additionally
    # requires all five wired blocks (same belt-and-braces as link_partner_mac).
    "eth_mac_subsystem": (_p("fpga", "ethernet"), ["eth_mac_test_subsystem.sv"]),
    # The DUT's Ethernet RETURN path (DUTEGR). Gates on the capture block's own
    # RTL; the bench ALSO elaborates the whole §8 subsystem around it (its TB
    # mirrors shell_bd.tcl SECTION 5), which the eth_mac_subsystem entry above
    # already gates.
    "dut_egress":     (_p("fpga", "shell", "ip", "dut_egress"),
                       ["dut_egress.sv", "dutegr_cfifo.sv"]),
    "dfx_ctl":        (_p("fpga", "shell", "ip", "dfx_ctl"),     ["dfx_ctl.sv"]),
    # The USRACC fabric-identity readback CSR (IDENTITY-REG). Internal bench
    # at the shipped 32-bit decode width with a plusarg-driven USR_ACCESSE2
    # model; no external tree, so it is READY the moment its RTL exists.
    "usr_access_rd":   (_p("fpga", "shell", "ip", "usr_access_rd"), ["usr_access_rd.sv"]),
    # USD @0x44A4_0000 (D13): the user-microSD SPI master. Its default make goal
    # (what `make check` / `make -C tests` run) regresses CFG=bdfast
    # (C_S_AXI_ADDR_WIDTH=32, 64-cycle debounce) and CFG=w12. The full CFG=bd
    # (width 32 + the shipped 10 ms debounce, ~5 min) is `make -C tests/usd_spi
    # premint`, run by fpga/dfx/Makefile's mint-preflight before any static build.
    "usd_spi":         (_p("fpga", "shell", "ip", "usd_spi"), ["usd_spi.sv"]),
    # The SoCScope trace plane as an RM. The wrapper is in this repo; the
    # INSTRUMENT is not -- fpga/dfx/rms/rm_socscope/filelist.tcl and the bench
    # Makefile both read it from $SOCSCOPE_HOME -- so readiness keys on the
    # wrapper AND on that checkout, and a machine without it must SKIP naming
    # the variable rather than fail to elaborate. Five elaborations per run;
    # see tests/rm_socscope/Makefile.
    "rm_socscope":    (_p("fpga", "dfx", "rms", "rm_socscope"),  ["rm_socscope.sv"],
                       ["$(SOCSCOPE_HOME)/hw/rtl/socscope_trace_top.f",
                        "$(SOCSCOPE_HOME)/hw/rtl/socscope_selftest.f"]),
    "clkrst":         (_p("fpga", "shell", "ip", "clkrst"),      ["dut_clkrst.sv"]),
    # v0.1 addition (I4 GPIO passthrough): no RTL exists yet at all, so
    # rtl_ready() (file-must-exist-and-not-be-a-stub) correctly reports SKIP
    # -- see tests/board_gpio/dut_notes.md.
    "board_gpio":     (_p("fpga", "shell", "ip", "board_gpio"),  ["board_gpio.sv"]),
    # W-RTL-NEWIP additions (I5/I7/I9, real RTL from day one — no stub
    # marker, so these report READY on landing). uart_bridge gates on ALL
    # THREE of its files: the top `include`s the other two, so a missing
    # helper would be a compile error, not a clean skip.
    "uart_bridge":    (_p("fpga", "shell", "ip", "uart_bridge"),
                       ["uart_bridge.sv", "uartbr_async_fifo.sv", "swo_uart_rx.sv"]),
    # csr_decode_width is BLOCK-parameterised (BLOCK ?= uart_bridge, then
    # dfx_ctl/clkrst/... each select their own RTL_DIR). The schema is one dir
    # per entry, so gate on the DEFAULT block's RTL; the other blocks are
    # selected explicitly and already gated by their own entries above.
    "csr_decode_width": (_p("fpga", "shell", "ip", "uart_bridge"),
                         ["uart_bridge.sv"]),
    # uart_echo_integration co-simulates the rm_uart_echo RM against the real
    # uart_bridge across the CDC. Gate on the RM (the thing that can be absent);
    # uart_bridge.sv is already gated by its own entry.
    "uart_echo_integration": (_p("fpga", "dfx", "rms", "rm_uart_echo"),
                              ["rm_uart_echo.sv"]),
    "swd_bb":         (_p("fpga", "shell", "ip", "swd_bb"),      ["swd_bb.sv"]),
    "telem":          (_p("fpga", "shell", "ip", "telem"),       ["telem.sv"]),
    # CLCD 8080 bus master (I-CLCD, shell-regmap.md v0.4 @ 0x44AC_0000). Real
    # RTL from day one (no stub marker) -> READY once clcd.sv lands. RESERVED /
    # not-yet-instantiated in the shell, but the block + bench are unit-proven
    # now; the port/XDC/BD change is a Phase-D static-batch rider.
    "clcd":           (_p("fpga", "shell", "ip", "clcd"),        ["clcd.sv"]),
    # CLCD KVM — panel arbiter between the harness (clcd_0) and a DUT-side
    # accelerator, hardware USER_nPB[1] toggle, two-sided safe-switch gate, DFX
    # forced-revert (clcd_kvm/README.md v1.0 @ 0x44AD_0000). Real RTL from day
    # one (no stub marker) -> READY once clcd_kvm.sv lands. RESERVED / not-yet-
    # instantiated in the shell (Wave-4 static-batch rider), but the block +
    # bench are unit-proven now.
    "clcd_kvm":       (_p("fpga", "shell", "ip", "clcd_kvm"),    ["clcd_kvm.sv"]),
    # DUT-side display accelerator — `ahb_clcd` inside `nanosoc_exp_socket`, the
    # reference block filling nanosoc's `exp_*` hole at 0x6000_0000 (the socket
    # contract is fpga/rp/nanosoc_exp/README.md v1.0). AHB-Lite slave (not AXI),
    # verified with the SAME HX8347-D panel model as clcd via an active-high ->
    # active-low shim, and W2-B's AhbLiteMaster BFM. Gates on all THREE contract
    # files: the socket (frozen port list), the reference accelerator, and the
    # shared FIFO+8080 core lifted from the shell's clcd.sv — a missing helper
    # would be a compile error, not a clean skip. SKIP until W2-D lands the RTL.
    "nanosoc_lcd":    (_p("fpga", "rp", "nanosoc_exp"),
                       # clcd_core.sv is a compile-time dep from fpga/shell/ip/clcd
                       # (resolved via the bench Makefile -y/-I path), NOT a file in
                       # this dir -- listing it here wrongly SKIPped the bench.
                       ["nanosoc_exp_socket.sv", "ahb_clcd.sv"]),
    # A5 verification-confidence pass (NEW leaf benches for real sequential
    # RTL that had ZERO verification): the nanosoc RP's UART<->AXIS console
    # shim, and the eth-ss AHB constant-programmer bring-up FSM. Both are real
    # from day one (no stub marker) -> READY on landing.
    "uart_axis_shim": (_p("fpga", "rp", "nanosoc"),              ["uart_axis_shim.sv"]),
    "eth_ss_bringup": (_p("fpga", "rp", "eth_ss"),               ["eth_ss_bringup.sv"]),
    # First RECONFIGURABLE-MODULE bench (the DFX-flow RMs under fpga/dfx/rms/
    # had lint-only coverage before this). rm_uart_echo drives the console
    # AXI-Stream boundary — banner + echo + backpressure. Single file, real
    # from day one (no stub marker) -> READY on landing.
    "rm_uart_echo":   (_p("fpga", "dfx", "rms", "rm_uart_echo"),  ["rm_uart_echo.sv"]),
    # M1 console-RX gate. Proves a host byte can actually reach the DUT's
    # console receiver (the real Arm cmsdk_apb_uart as UART2) through the real
    # nanosoc pin mux. This found a hard RTL bug: nanosoc_ss_systemctrl left
    # the pin mux's p1_in pad-input port unconnected, so uart2_rxd was a
    # loopback of the SoC's own GPIO drive and the host->DUT path was
    # physically absent. See tests/uart2_rx_path/README.md; `make falsify`
    # re-runs the bench against pre-fix RTL to keep the gate honest.
    #
    # Like sim_smoke, the toplevel lives IN the bench dir (uart2_rx_harness.sv)
    # -- the DUT hierarchy comes from the nanosoc_m0_soc tree, which is outside
    # this repo (resolved via NANOSOC_SOC_DIR in the bench Makefile), so
    # readiness keys on the harness rather than on an in-repo RTL file.
    # EXTERNAL: the DUT is nanosoc_ss_systemctrl + nanosoc_pin_mux out of the
    # nanosoc_m0_soc tree. PINMUX is the file the bench exists to indict (the
    # unconnected p1_in), and SYSCTRL is what instantiates it; both are already
    # overridable in the bench Makefile for `make falsify`, so gating on them
    # gates on exactly what will be compiled.
    "uart2_rx_path":  (_p("tests", "uart2_rx_path"),             ["uart2_rx_harness.sv"],
                       ["$(PINMUX)", "$(SYSCTRL)"]),
    # M2 gate. Proves memory-mapped XiP reads work through the qspi_flash_ahb
    # wrapper (the module the SoC instantiates) against the real SST26VF064B
    # VIP -- i.e. that the CPU could execute code in place from flash. Also
    # pins the CG092 cache (measured at the pads: cold line fill = 168 SCLK,
    # cached re-read = 0 nCS) and the fact that qspi_mem's placement at
    # 0x7000_0000 is what makes CPU fetches cacheable at all under the ARMv6-M
    # default memory map. `make falsify` reverses the QSPI_IO_i lanes to prove
    # the bench can fail. Toplevel is the bench-local harness; the DUT RTL
    # comes from nanosoc_m0_soc + the ahb_qspi IP repo, both outside this repo.
    # EXTERNAL: qspi_flash_ahb (the module the SoC instantiates) from the SoC
    # tree, plus the ahb_qspi IP repo the wrapper wraps and the Arm cell library
    # the CG092 cache comes from.
    "qspi_xip":       (_p("tests", "qspi_xip"),                  ["qspi_xip_harness.sv"],
                       ["$(QSPI_WRAPPER)", "$(AHB_QSPI_DIR)", "$(ARM_IP)"]),
    # P2 SIM GATE (docs/planning/SOC400_BASELINE_INTEGRATION.md). Drives the
    # SERIAL JTAG pins of the CoreSight SoC-400 SWJ-DP and halts a real
    # Cortex-M0 to DHCSR S_HALT -- the one segment nothing else proves
    # (host-JTAG -> cxdapswjdp serial decode -> DAP -> M0). Like uart2_rx_path
    # / qspi_xip, the toplevel lives in the bench dir but the DUT hierarchy
    # (SoC-400 wrappers + Cortex-M0-QS IP) comes from the multicore tree +
    # ARM_IP_LIBRARY_PATH, outside this repo (NANOSOC_MULTICORE_HOME in the
    # bench Makefile), so readiness keys on the harness, not an in-repo file.
    # EXTERNAL, and this is the entry the whole change exists for. The
    # requirement is the SHARED TECH BLOCK'S OWN FILELIST -- precisely the thing
    # that moved in the multicore tree and left this bench unable to elaborate
    # for six weeks while it read READY here (8b9a2fe). NANOSOC_MULTICORE_HOME
    # has NO default in the bench Makefile, deliberately, so in a bare shell
    # this resolves to nothing and reports SKIP with the variable named.
    "jtag_dap_bringup": (_p("tests", "jtag_dap_bringup"),        ["tb_top.sv"],
                        ["$(SOCLABS_CORESIGHT_SOC400_TECH_DIR)/flist/"
                         "coresight_soc400_swjdap.flist",
                         "$(SOCLABS_SLCOREM0_TECH_DIR)/flist/slcorem0_qs.flist",
                         "$(NANOSOC_MULTICORE_HOME)/coresight_soc400/flist/"
                         "coresight_soc400_swjdp.flist",
                         "$(ARM_IP_LIBRARY_PATH)"]),
    # The two-TAP chain bench (23814a9). It was never listed here or in
    # tests/Makefile's BENCH_DIRS at all -- so it was invisible to `make list`,
    # never run by `make all`, and its sim_build/ was never swept by `make
    # clean`: exactly the housekeeping gap that Makefile's own comment records
    # for eth_mac_subsystem and three others. Its DUT is the REAL RM shim from
    # this repo plus the same external SoC-400 collateral as the bench above.
    "jtag_chain":     (_p("fpga", "rp", "nanosoc_iice"),
                       ["rp_nanosoc_iice_shim.sv"],
                       ["$(SOCLABS_CORESIGHT_SOC400_TECH_DIR)/flist/"
                        "coresight_soc400_swjdap.flist",
                        "$(SOCLABS_SLCOREM0_TECH_DIR)/flist/slcorem0_qs.flist",
                        "$(NANOSOC_MULTICORE_HOME)/coresight_soc400/flist/"
                        "coresight_soc400_swjdp.flist",
                        "$(ARM_IP_LIBRARY_PATH)"]),
}

#: Where each bench's Makefile lives, when it is not the RTL directory. The
#: external requirements are read from THAT Makefile, not from the RTL dir.
BENCH_MAKEFILE_DIR = {name: _p("tests", name) for name in BENCHES}


def _entry(name: str):
    """(rtl_dir, files, external requirements) for `name`."""
    e = BENCHES[name]
    return (e[0], e[1], e[2] if len(e) > 2 else ())


def skip_reason(name: str):
    """None if the bench can run here; else the one sentence saying why not."""
    rtl_dir, files, requires = _entry(name)
    if not rtl_ready(rtl_dir, files):
        return "%s: RTL not landed or still a Phase-0 stub" % rtl_dir
    return external_gap(BENCH_MAKEFILE_DIR[name], requires)


def is_ready(name: str) -> bool:
    return skip_reason(name) is None


def main(argv):
    ready_count = 0
    skip_count = 0
    for name in sorted(BENCHES):
        rtl_dir, _files, _req = _entry(name)
        why = skip_reason(name)
        ready_count += int(why is None)
        skip_count += int(why is not None)
        # FORMAT IS LOAD-BEARING: tests/Makefile reads `status name rtldir` and
        # awk-matches on $2, so the reason goes LAST, after the directory.
        tail = "" if why is None else "  -- %s" % why
        print(f"{'READY' if why is None else 'SKIP'} {name} {rtl_dir}{tail}")
    if "--summary" in argv:
        print(f"SUMMARY ready={ready_count} skip={skip_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
