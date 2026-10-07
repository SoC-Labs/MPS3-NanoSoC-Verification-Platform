"""tests/shell_cpu_seam/test_shell_cpu_seam.py

The CPU seam: ONE shell BD, two CPUs (fpga/shell/bd/cpu_mb.tcl, the fielded
classic MicroBlaze; cpu_mbv.tcl, MicroBlaze V + DDR4 for Linux). This is the
board-free, Vivado-free half of its gate. The Vivado half -- the SHELL_CPU=mb BD
dump IDENTICAL to the pre-seam baseline, with a flipped-CONFIG control that must
differ, and the MBV validate -- is run_seam_gate.sh beside this file; its
results are pinned in golden/ and consumed here.

WHAT IS GATED, AND THE CONTROL FOR EACH
--------------------------------------
1. THE KNOB. `soclabs_shell_cpu` (shell_bd.tcl's OWN proc, run under tclsh)
   accepts unset/""/mb/mbv and refuses everything else. The SHELL_REALPHY gate
   once read "no" as "yes" and built the non-default variant.
2. THE SEAM IS COMPLETE. shell_bd.tcl sources the CPU file at a fixed set of
   stages; every CPU file must handle every one of them and refuse an unknown
   one, so a stage added to the BD cannot be skipped by one CPU in silence.
   Control: a copy of cpu_mbv.tcl with a stage removed is caught.
3. NO CPU LEAKS INTO THE SHARED BD. The CPU-specific cells (the core, its debug
   module, its LMB, the DDR4 MIG) are created only in a CPU file. This is the
   property that makes July's drift impossible: the fork was a COPY of the
   shared blocks, and its copy lost the POR/WDOG wires to dfx_ctl.
4. THE LINUX CONTRACT IS PINNED TWICE. C_INTERRUPT_WAKEUP / C_USE_SSTC /
   C_USE_COUNTERS = 1 are set in cpu_mbv.tcl AND re-read after propagation by
   tools/shell_bd_guards.tcl. Control: a mutated copy is caught.
5. THE MBV PADS STAY OUT OF THE BARE-METAL BUILD. shell_top.sv preprocesses to
   no DDR4 port and the raw ETH_INT without MPS3_SHELL_CPU_MBV, and to all 16
   DDR4 ports and ~ETH_INT with it (control); constraints/mbv/ is never reached
   by the default constraints glob.
6. THE DECOUPLER CLAMP ASSERTS, FOR THE WIRING THE BD ACTUALLY HAS. The clamp
   wiring extracted from each variant's BD dump (golden/clamp_wiring_*.txt,
   refreshed by run_seam_gate.sh) is fed to clamp_bench/tb_clamp.sv, the real
   dfx_ctl.sv at the BD's width: both variants must PASS, and the July fork's
   wiring (POR and WDOG inputs dangling -> auto-tied 0) must FAIL.
7. THE DUMP COMPARATOR CAN FAIL: it refuses vacuous dumps, reports a one-line
   change, and its --expect differ mode fails on identical input.

Needs tclsh (items 1-2), verilator (5) and iverilog (6); each skips with its
reason when the tool is absent.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
BD_DIR = REPO / "fpga" / "shell" / "bd"
SHELL_BD = BD_DIR / "shell_bd.tcl"
CPU_MB = BD_DIR / "cpu_mb.tcl"
CPU_MBV = BD_DIR / "cpu_mbv.tcl"
GUARDS = REPO / "fpga" / "shell" / "tools" / "shell_bd_guards.tcl"
BUILD_SHELL = REPO / "fpga" / "shell" / "build_shell.tcl"
SHELL_TOP = REPO / "fpga" / "shell" / "shell_top.sv"
CONSTR = REPO / "fpga" / "shell" / "constraints"
DFX_CTL = REPO / "fpga" / "shell" / "ip" / "dfx_ctl" / "dfx_ctl.sv"
TB_CLAMP = HERE / "clamp_bench" / "tb_clamp.sv"
GOLDEN = HERE / "golden"

sys.path.insert(0, str(HERE))
import seam_dump  # noqa: E402

TCLSH = shutil.which("tclsh")
VERILATOR = shutil.which("verilator")
IVERILOG = shutil.which("iverilog")
VVP = shutil.which("vvp")

STAGES = ["core", "intc", "timer", "axi", "addr", "finish"]


def _run_tcl(script: str, env=None) -> subprocess.CompletedProcess:
    # As a FILE: tclsh reading stdin exits 0 after an uncaught error.
    with tempfile.NamedTemporaryFile("w", suffix=".tcl", delete=False) as fh:
        fh.write(script)
        path = fh.name
    try:
        return subprocess.run([TCLSH, path], env=env, capture_output=True, text=True)
    finally:
        os.unlink(path)


# =========================================================================== 1
def _cpu_gate(value):
    src = SHELL_BD.read_text()
    m = re.search(r"^proc soclabs_shell_cpu \{\} \{.*?^\}", src, re.S | re.M)
    assert m, "shell_bd.tcl no longer defines proc soclabs_shell_cpu"
    env = dict(os.environ)
    env.pop("SHELL_CPU", None)
    if value is not None:
        env["SHELL_CPU"] = value
    p = _run_tcl(m.group(0) + "\nputs [soclabs_shell_cpu]\n", env)
    return p.returncode, p.stdout.strip(), p.stderr


@pytest.mark.skipif(TCLSH is None, reason="tclsh not available")
@pytest.mark.parametrize("value,expect", [
    (None, "mb"), ("", "mb"), ("mb", "mb"), ("mbv", "mbv"), (" mbv ", "mbv")])
def test_cpu_knob_truth_table(value, expect):
    rc, out, err = _cpu_gate(value)
    assert rc == 0, err
    assert out == expect


@pytest.mark.skipif(TCLSH is None, reason="tclsh not available")
@pytest.mark.parametrize("value", ["MB", "MBV", "riscv", "1", "0", "yes", "mbv2", "linux"])
def test_cpu_knob_refuses_to_guess(value):
    rc, out, err = _cpu_gate(value)
    assert rc != 0, f"SHELL_CPU={value!r} was accepted as {out!r}"
    assert "not one of" in err


# =========================================================================== 2
def _sourced_stages(text: str) -> list[str]:
    """The cpu_stage values shell_bd.tcl sets before each `source $cpu_tcl`."""
    return re.findall(r"^\s*set cpu_stage (\w+)\s*\n\s*source \$cpu_tcl\s*$", text, re.M)


def test_shell_bd_sources_every_stage_once_in_order():
    got = _sourced_stages(SHELL_BD.read_text())
    assert got == STAGES, f"shell_bd.tcl seam points are {got}, expected {STAGES}"


def _stage_probe(cpu_file: Path, stage: str) -> subprocess.CompletedProcess:
    """Source a CPU file for one stage with every Vivado command stubbed: tells a
    handled stage (runs) from an unhandled one (the file's own error)."""
    stubs = "\n".join(
        f"proc {c} args {{return x}}" for c in (
            "create_bd_cell set_property connect_bd_intf_net connect_bd_net "
            "get_bd_intf_pins get_bd_pins assign_bd_address get_bd_addr_segs "
            "create_bd_port get_bd_ports get_property make_bd_intf_pins_external "
            "get_bd_intf_ports disconnect_bd_net get_bd_nets get_bd_cells "
            "get_ipdefs current_project").split())
    script = f"""{stubs}
proc version args {{return 2026.1}}
proc soclabs_ddr4_mig_create args {{return x}}
proc ::soclabs_mbv_assert_cfg args {{}}
set ::SHELL_BD_DIR {{{BD_DIR}}}
rename source ::tcl::source_orig
proc source {{f}} {{ if {{[string match *ddr4_mig.tcl $f]}} {{return}}; uplevel 1 [list ::tcl::source_orig $f] }}
foreach v {{shell_clk shell_mb_reset clk_wiz_shell axi_intc_0 axi_timer_0 axi_uartlite_0
           axi_interconnect_0 axi_timebase_wdt_0 telem_0 microblaze_0 microblaze_riscv_0
           proc_sys_reset_cpu proc_sys_reset_ddr cpu_mb_reset cpu_ic_arstn ddr_ui_clk
           ddr_calib shell_aresetn mdm_riscv_0 debug_bridge_0}} {{ set $v x }}
set cpu_stage {stage}
source {{{cpu_file}}}
puts HANDLED
"""
    return _run_tcl(script)


@pytest.mark.skipif(TCLSH is None, reason="tclsh not available")
@pytest.mark.parametrize("cpu_file", [CPU_MB, CPU_MBV], ids=["mb", "mbv"])
def test_every_cpu_file_handles_every_stage(cpu_file):
    for st in STAGES:
        p = _stage_probe(cpu_file, st)
        assert "HANDLED" in p.stdout, f"{cpu_file.name} stage {st}: {p.stderr[-400:]}"
    p = _stage_probe(cpu_file, "not_a_stage")
    assert p.returncode != 0 and "unknown cpu_stage" in p.stderr, (
        f"{cpu_file.name} accepted an unknown stage -- a new seam point could be skipped silently")


@pytest.mark.skipif(TCLSH is None, reason="tclsh not available")
def test_control_a_cpu_file_missing_a_stage_is_caught(tmp_path):
    src = CPU_MBV.read_text()
    mutated, n = re.subn(r"^timer \{.*?^\}\n", "", src, count=1, flags=re.S | re.M)
    assert n == 1, "control could not remove the timer branch -- bad control"
    bad = tmp_path / "cpu_mbv.tcl"
    bad.write_text(mutated)
    p = _stage_probe(bad, "timer")
    assert "HANDLED" not in p.stdout and "unknown cpu_stage" in p.stderr


# =========================================================================== 3
_CPU_ONLY_VLNVS = ("xilinx.com:ip:microblaze:", "xilinx.com:ip:microblaze_riscv:",
                   "xilinx.com:ip:mdm:", "xilinx.com:ip:mdm_riscv:",
                   "xilinx.com:ip:lmb_v10:", "xilinx.com:ip:lmb_bram_if_cntlr:",
                   "xilinx.com:ip:ddr4:", "xilinx.com:ip:smartconnect:",
                   "xilinx.com:ip:blk_mem_gen:")


def _code(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def test_no_cpu_cell_is_created_in_the_shared_bd():
    code = _code(SHELL_BD.read_text())
    leaks = [v for v in _CPU_ONLY_VLNVS if v in code]
    assert not leaks, f"shell_bd.tcl creates CPU-specific IP {leaks}; it belongs in cpu_*.tcl"
    assert not re.search(r"\bmicroblaze(_riscv)?_0\b", code), (
        "shell_bd.tcl names a CPU cell directly; route it through a cpu_stage")


def test_both_cpu_files_create_their_core_and_lmb():
    for f, core in ((CPU_MB, "microblaze:11.0 microblaze_0"),
                    (CPU_MBV, "microblaze_riscv:1.0 microblaze_riscv_0")):
        code = _code(f.read_text())
        assert core in code, f"{f.name} does not create {core}"
        for cell in ("ilmb_bram_if_cntlr", "dlmb_bram_if_cntlr", "local_ram"):
            assert re.search(rf"create_bd_cell [^\n]* {cell}\]", code), f"{f.name}: no {cell}"


# =========================================================================== 4
_LINUX_PINS = ("C_INTERRUPT_WAKEUP", "C_USE_SSTC", "C_USE_COUNTERS")


def _pinned_in_cpu_mbv(text: str) -> set[str]:
    return {k for k in _LINUX_PINS if re.search(rf"CONFIG\.{k}\s+\{{1\}}", _code(text))}


def _reasserted_in_guards(text: str) -> set[str]:
    return {k for k in _LINUX_PINS if re.search(rf"\b{k} 1\b", _code(text))}


def test_linux_contract_is_pinned_and_reasserted():
    assert _pinned_in_cpu_mbv(CPU_MBV.read_text()) == set(_LINUX_PINS)
    assert _reasserted_in_guards(GUARDS.read_text()) == set(_LINUX_PINS)


def test_control_dropping_interrupt_wakeup_is_caught():
    mutated = CPU_MBV.read_text().replace("CONFIG.C_INTERRUPT_WAKEUP {1}", "CONFIG.C_INTERRUPT_WAKEUP {0}")
    assert "C_INTERRUPT_WAKEUP" not in _pinned_in_cpu_mbv(mutated)


# ------------------------------------------------------------------ 4b SEAM-8
def _num_bs_master(text: str, cell_var: str) -> list[str]:
    return re.findall(r"CONFIG\.C_NUM_BS_MASTER\s+\{(\d+)\}[^\n]*\$%s" % cell_var, _code(text))


def test_seam8_mig_debug_hub_is_mbv_only():
    """The DDR4 MIG's XSDB calibration slave needs a debug hub, and with
    debug_bridge_0 in master mode Vivado will not insert one (DFX opt_design:
    Chipscope 16-335). [SEAM-8] adds a second BSCAN master and a static
    mode-1 hub -- in the MBV file only; the bare-metal bridge keeps ONE master."""
    bd, mb, mbv = SHELL_BD.read_text(), CPU_MB.read_text(), CPU_MBV.read_text()
    shared = re.findall(r"CONFIG\.C_NUM_BS_MASTER \{(\d+)\}", _code(bd))
    assert shared == ["1"], f"shell_bd.tcl must build debug_bridge_0 with ONE master, got {shared}"
    assert "C_NUM_BS_MASTER" not in _code(mb) and "mig_dbg_hub" not in _code(mb)
    assert _num_bs_master(mbv, "debug_bridge_0") == ["2"]
    finish = re.search(r"^finish \{\n(.*?)^\}", mbv, re.S | re.M).group(1)
    assert re.search(r"create_bd_cell [^\n]*debug_bridge:3\.0 mig_dbg_hub\]", finish)
    assert re.search(r"CONFIG\.C_DEBUG_MODE \{1\}", finish)
    assert re.search(r"m1_bscan\][^\n]*mig_dbg_hub/S_BSCAN", finish)
    # the hub runs on the shell clock, which exists whether or not DDR4 calibrates
    assert re.search(r"connect_bd_net \$shell_clk \[get_bd_pins \$mig_dbg_hub/clk\]", finish)


def test_control_seam8_in_the_shared_bd_is_caught():
    mutated = SHELL_BD.read_text().replace("CONFIG.C_NUM_BS_MASTER {1}", "CONFIG.C_NUM_BS_MASTER {2}")
    assert re.findall(r"CONFIG\.C_NUM_BS_MASTER \{(\d+)\}", _code(mutated)) != ["1"]


# =========================================================================== 5
_DDR_PORTS = ["c0_sys_clk_p", "c0_sys_clk_n", "c0_ddr4_act_n", "c0_ddr4_adr", "c0_ddr4_ba",
              "c0_ddr4_bg", "c0_ddr4_ck_c", "c0_ddr4_ck_t", "c0_ddr4_cke", "c0_ddr4_cs_n",
              "c0_ddr4_dm_n", "c0_ddr4_dq", "c0_ddr4_dqs_c", "c0_ddr4_dqs_t", "c0_ddr4_odt",
              "c0_ddr4_reset_n"]


def _pp(defines):
    args = [VERILATOR, "-E", "-sv"] + [f"+define+{d}" for d in defines] + [str(SHELL_TOP)]
    p = subprocess.run(args, capture_output=True, text=True, cwd=str(REPO))
    assert p.returncode == 0, p.stderr
    return p.stdout


@pytest.mark.skipif(VERILATOR is None, reason="verilator not available")
@pytest.mark.parametrize("others", [[], ["MPS3_SHELL_TOUCH"], ["MPS3_SHELL_TOUCH", "MPS3_SHELL_REALPHY"]])
def test_bare_metal_top_has_no_mbv_logic(others):
    out = _pp(others)
    assert not [p for p in _DDR_PORTS if p in out]
    assert "~ETH_INT" not in out and re.search(r"\.eth_irq\s*\(ETH_INT\)", out)


@pytest.mark.skipif(VERILATOR is None, reason="verilator not available")
@pytest.mark.parametrize("others", [[], ["MPS3_SHELL_TOUCH"], ["MPS3_SHELL_TOUCH", "MPS3_SHELL_REALPHY"]])
def test_control_the_mbv_define_brings_every_ddr_port_and_the_irq_inversion(others):
    out = _pp(["MPS3_SHELL_CPU_MBV"] + others)
    for p in _DDR_PORTS:
        # declared AND bound on u_shell: `.<p> (<p>)`
        assert re.search(rf"\.{p}\s*\(\s*{p}\s*\)", out), f"{p} not bound with MPS3_SHELL_CPU_MBV"
    assert re.search(r"\.eth_irq\s*\(\s*~ETH_INT\s*\)", out)


def test_mbv_constraints_are_outside_the_default_glob_and_added_only_under_mbv():
    mbv_dir = CONSTR / "mbv"
    assert (mbv_dir / "ddr4_pins.xdc").is_file()
    for x in CONSTR.glob("*.xdc"):   # what build_shell.tcl globs into EVERY build
        assert "c0_ddr4" not in x.read_text(), f"{x.name} constrains a DDR4 port in every build"
    src = BUILD_SHELL.read_text()
    m = re.search(r'^if \{ \$shell_cpu eq "mbv" \} \{\n(.*?)^\}', src, re.S | re.M)
    blocks = re.findall(r'^if \{ \$shell_cpu eq "mbv" \} \{\n(.*?)^\}', src, re.S | re.M)
    assert m and any('"constraints" "mbv"' in b for b in blocks), (
        "build_shell.tcl adds constraints/mbv outside an `if { $shell_cpu eq \"mbv\" }` block")
    outside = re.sub(r'^if \{ \$shell_cpu eq "mbv" \} \{\n.*?^\}', "", src, flags=re.S | re.M)
    assert '"constraints" "mbv"' not in outside


# =========================================================================== 6
def _golden_clamp(cpu):
    lines = (GOLDEN / f"clamp_wiring_{cpu}.txt").read_text().splitlines()
    assert lines[0] == f"cpu {cpu}"
    return lines


def _defines_for(lines):
    """What the dump says about dfx_ctl_0's two isolation resets -> bench defines."""
    wiring = dict(ln.split(" <- ", 1) for ln in lines[1:])
    d = []
    if "/sys_rst_n" not in wiring["/dfx_ctl_0/ext_por_n_i"].split():
        d.append("POR_UNDRIVEN")
    if "/axi_timebase_wdt_0/wdt_reset" not in wiring["/dfx_ctl_0/wdt_reset_i"].split():
        d.append("WDT_UNDRIVEN")
    return d


def _bench(defines, tmp_path):
    exe = tmp_path / "tb_clamp.vvp"
    cmd = [IVERILOG, "-g2012", "-o", str(exe)] + [f"-D{d}" for d in defines] + [
        str(TB_CLAMP), str(DFX_CTL)]
    c = subprocess.run(cmd, capture_output=True, text=True)
    assert c.returncode == 0, c.stderr
    r = subprocess.run([VVP, "-n", str(exe)], capture_output=True, text=True, timeout=120)
    return r.stdout


@pytest.mark.parametrize("cpu", ["mb", "mbv"])
def test_golden_clamp_wiring_is_complete(cpu):
    lines = _golden_clamp(cpu)
    assert seam_dump.check_clamp_lines(lines) == []


def test_control_a_dangling_por_is_reported():
    lines = _golden_clamp("mbv")
    mutated = [("/dfx_ctl_0/ext_por_n_i <- UNDRIVEN" if ln.startswith("/dfx_ctl_0/ext_por_n_i ") else ln)
               for ln in lines]
    assert any("ext_por_n_i" in b for b in seam_dump.check_clamp_lines(mutated))


@pytest.mark.skipif(IVERILOG is None or VVP is None, reason="iverilog not available")
@pytest.mark.parametrize("cpu", ["mb", "mbv"])
def test_clamp_asserts_with_the_wiring_the_bd_has(cpu, tmp_path):
    defines = _defines_for(_golden_clamp(cpu))
    assert defines == [], f"the {cpu} BD leaves {defines} -- the clamp cannot work"
    out = _bench(defines, tmp_path)
    assert "CLAMP_BENCH PASS" in out, out


@pytest.mark.skipif(IVERILOG is None or VVP is None, reason="iverilog not available")
def test_control_the_july_fork_wiring_cannot_clamp(tmp_path):
    """src/linux_harness/shell_linux_bd.tcl (static 0x2B082E1B): dfx_ctl_0's
    ext_por_n_i and wdt_reset_i were never connected, so IPI tied both to 0."""
    fork = ["cpu mbv", "/dfx_ctl_0/ext_por_n_i <- UNDRIVEN", "/dfx_ctl_0/wdt_reset_i <- UNDRIVEN"]
    defines = _defines_for(fork)
    assert defines == ["POR_UNDRIVEN", "WDT_UNDRIVEN"]
    out = _bench(defines, tmp_path)
    assert "CLAMP_BENCH FAIL DECOUPLE=1" in out, out


# =========================================================================== 7
def _fake_dump(tmp_path, name, tweak=None):
    lines = ["# soclabs BD dump v1", "DESIGN shell_bd", "CELL /microblaze_0 vlnv=x type=ip"]
    for i in range(70):
        lines.append(f"CELL /c{i} vlnv=x type=ip")
        lines += [f"  CONFIG.P{j}={j}" for j in range(50)]
    if tweak:
        lines[tweak] = lines[tweak] + "_changed"
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n")
    return p


def test_comparator_refuses_vacuous_dumps(tmp_path):
    empty = tmp_path / "e.dump"
    empty.write_text("# nothing\nDESIGN shell_bd\n")
    assert seam_dump.main(["compare", str(empty), str(empty)]) == 2


def test_comparator_sees_one_changed_value(tmp_path, capsys):
    a = _fake_dump(tmp_path, "a.dump")
    same = _fake_dump(tmp_path, "b.dump")
    changed = _fake_dump(tmp_path, "c.dump", tweak=500)
    assert seam_dump.main(["compare", str(a), str(same)]) == 0
    assert seam_dump.main(["compare", str(a), str(changed)]) == 1
    assert seam_dump.main(["compare", str(a), str(changed), "--expect", "differ"]) == 0
    assert seam_dump.main(["compare", str(a), str(same), "--expect", "differ"]) == 1


def test_golden_baseline_is_recorded():
    txt = (GOLDEN / "baseline_mb.txt").read_text()
    assert re.search(r"^mb_touch1_body_sha256 [0-9a-f]{64}$", txt, re.M)
    n = int(re.search(r"^mb_touch1_body_lines (\d+)$", txt, re.M).group(1))
    assert n > 10000


# =========================================================================== 8
# D13: the ONE deliberate change to the bare-metal BD since the fielded baseline
# (eafe787) is usd_spi_0 replacing axi_quad_spi_0 on 0x44A4 / M03. run_seam_gate.sh
# step 2 holds the Vivado dump to golden/d13_usd_delta_mb.txt EXACTLY; this holds
# every line of that golden to the cell swap, so the golden cannot quietly absorb
# a second change the next time someone regenerates it.
D13_DELTA = GOLDEN / "d13_usd_delta_mb.txt"
_M03_PATH = re.compile(r"^CELL /axi_interconnect_0(?: > (?:PIN|IPIN) M03_AXI\w*"
                       r"|/m03_couplers > |/tier2_xbar_0 > IPIN M03_AXI > )")


def d13_delta_problems(lines: list[str]) -> list[str]:
    bad = []
    for ln in lines:
        sign, rec = ln[:2], ln[2:]
        old, new = "axi_quad_spi_0" in rec, ("usd_spi_0" in rec or "/usd_" in rec)
        if sign not in ("- ", "+ "):
            bad.append(f"not a delta line: {ln!r}")
        elif old and new:
            bad.append(f"names both cells: {ln}")
        elif old:
            if sign != "- ":
                bad.append(f"axi_quad_spi_0 ADDED back: {ln}")
        elif new:
            if sign != "+ ":
                bad.append(f"usd_spi_0 REMOVED: {ln}")
        elif not _M03_PATH.match(rec):
            bad.append(f"outside the D13 cell swap and its M03 port: {ln}")
        m = re.search(r"^ASEG /\w+/Data/SEG_\S+ offset=(0x[0-9A-Fa-f]+) range=(0x[0-9A-Fa-f]+)", rec)
        if m and (int(m.group(1), 16), int(m.group(2), 16)) != (0x44A40000, 0x10000):
            bad.append(f"the 0x44A4_0000/64K page moved: {ln}")
    return bad


def test_d13_delta_is_only_the_usd_cell_swap():
    lines = seam_dump.read_delta(D13_DELTA)
    assert d13_delta_problems(lines) == []
    for must in ("- CELL /axi_quad_spi_0 vlnv=xilinx.com:ip:axi_quad_spi:3.2 type=ip",
                 "+ CELL /usd_spi_0 vlnv=soclabs.org:user:usd_spi:1.0 type=ip",
                 "+ CELL /usd_spi_0 > CONFIG.C_S_AXI_ADDR_WIDTH=32",
                 "- INET /axi_interconnect_0_M03_AXI : /axi_quad_spi_0/AXI_LITE",
                 "+ INET /axi_interconnect_0_M03_AXI : /usd_spi_0/s_axi"):
        assert must in lines, f"the D13 delta no longer says {must!r}"
    asegs = [ln for ln in lines if re.match(r"[-+] ASEG /microblaze_0/Data/SEG_", ln)]
    assert len(asegs) == 2 and all("offset=0x44A40000 range=0x00010000" in a for a in asegs)
    # the eight BD ports D13 adds, and nothing removed from the port list
    ports = sorted(ln for ln in lines if ln.startswith(("+ PORT", "- PORT")))
    assert [p.split()[2] for p in ports] == [
        "/usd_clk_o", "/usd_clk_oe", "/usd_cmd_o", "/usd_cmd_oe", "/usd_dat0_i",
        "/usd_dat3_o", "/usd_dat3_oe", "/usd_ncd_i"] and all(p[0] == "+" for p in ports)


def test_control_a_second_change_in_the_d13_golden_is_caught():
    lines = seam_dump.read_delta(D13_DELTA)
    for extra in ("- CELL /microblaze_0 > CONFIG.C_USE_DIV=1",              # the seam gate's own control
                  "+ CELL /axi_interconnect_0 > IPIN M04_AXI vlnv=x",        # a neighbouring port
                  "+ CELL /axi_quad_spi_0 vlnv=xilinx.com:ip:axi_quad_spi:3.2 type=ip",
                  "+ ASEG /microblaze_0/Data/SEG_usd_spi_0_reg0 offset=0x44A50000 range=0x00010000 x"):
        assert d13_delta_problems(lines + [extra]), f"the D13 golden check accepted {extra!r}"


# INJ: the SECOND deliberate change -- the DUTEGR inject path. run_seam_gate.sh
# step 2 requires baseline -> this tree == D13 golden + this golden; this holds
# every INJ line to the four inject nets, so the golden cannot absorb a third
# change either.
INJ_DELTA = GOLDEN / "inj_dutegr_delta_mb.txt"
_INJ_PAIRS = {"inj_m_tdata": "mgmt_s_tdata", "inj_m_tvalid": "mgmt_s_tvalid",
              "inj_m_tlast": "mgmt_s_tlast", "inj_m_tready": "mgmt_s_tready"}


def inj_delta_problems(lines: list[str]) -> list[str]:
    bad = []
    allowed_nets = {"/dut_egress_0_inj_m_tdata", "/dut_egress_0_inj_m_tvalid",
                    "/dut_egress_0_inj_m_tlast", "/eth_mac_test_subsystem_0_mgmt_s_tready"}
    for ln in lines:
        sign, rec = ln[:2], ln[2:]
        if sign not in ("- ", "+ "):
            bad.append(f"not a delta line: {ln!r}")
            continue
        m = re.match(r"CELL /(dut_egress_0|eth_mac_test_subsystem_0) > PIN (\w+) ", rec)
        n = re.match(r"NET (\S+) : (\S+)$", rec)
        if m:
            cell, pin = m.groups()
            if cell == "dut_egress_0" and not (pin in _INJ_PAIRS and sign == "+ "):
                bad.append(f"dut_egress_0 changed outside its new inj_m_* pins: {ln}")
            if cell == "eth_mac_test_subsystem_0" and pin not in _INJ_PAIRS.values():
                bad.append(f"a bridge pin other than mgmt_s_* moved: {ln}")
        elif n:
            net, member = n.groups()
            if sign == "+ " and net not in allowed_nets:
                bad.append(f"a member joined a net that is not an inject net: {ln}")
            elif sign == "+ " and not re.fullmatch(
                    r"/dut_egress_0/inj_m_t(data|valid|last|ready)"
                    r"|/eth_mac_test_subsystem_0/mgmt_s_t(data|valid|last|ready)", member):
                bad.append(f"an inject net gained a member that is not a pair end: {ln}")
            if sign == "- " and not (net in ("/gnd_axis_tdata8_dout", "/gnd_axis_ctrl_dout")
                                     and "/mgmt_s_" in member):
                bad.append(f"a net lost a member that is not a tied mgmt_s_*: {ln}")
        else:
            bad.append(f"outside the inject wiring (cell/CONFIG/port/address): {ln}")
    return bad


def test_inj_delta_is_only_the_inject_wiring():
    lines = seam_dump.read_delta(INJ_DELTA)
    assert inj_delta_problems(lines) == []
    # each inject net has exactly its two ends, and every pair is present
    for src, dst in _INJ_PAIRS.items():
        net = ("/eth_mac_test_subsystem_0_mgmt_s_tready" if src == "inj_m_tready"
               else f"/dut_egress_0_{src}")
        members = sorted(ln.split(" : ")[1] for ln in lines if ln.startswith(f"+ NET {net} : "))
        assert members == sorted([f"/dut_egress_0/{src}", f"/eth_mac_test_subsystem_0/{dst}"]), (net, members)
    # port A (uplink) never appears: it stays SAFE-TIED
    assert not [ln for ln in lines if "uplink" in ln]


def test_control_a_third_change_in_the_inj_golden_is_caught():
    lines = seam_dump.read_delta(INJ_DELTA)
    for extra in ("- NET /gnd_axis_ctrl_dout : /eth_mac_test_subsystem_0/uplink_s_tvalid",   # untying port A
                  "+ NET /dut_egress_0_inj_m_tvalid : /microblaze_0/Interrupt",           # a third member
                  "+ CELL /dut_egress_0 > CONFIG.DATA_DEPTH=4096",                        # a generic change
                  "+ CELL /eth_mac_test_subsystem_0 > PIN uplink_s_tvalid dir=I type=undef left= right= net=-"):
        assert inj_delta_problems(lines + [extra]), f"the INJ golden check accepted {extra!r}"


def test_delta_comparator_names_exactly_the_change(tmp_path, capsys):
    a = _fake_dump(tmp_path, "a.dump")
    b = _fake_dump(tmp_path, "b.dump", tweak=500)
    d = seam_dump.delta(seam_dump.body(a), seam_dump.body(b))
    assert len(d) == 2 and d[0].startswith("- CELL /c") and d[1].startswith("+ CELL /c")
    assert " > CONFIG.P" in d[0] and d[1].endswith("_changed")
    golden = tmp_path / "g.txt"
    golden.write_text("# header\n" + "\n".join(d) + "\n")
    assert seam_dump.main(["compare", str(a), str(b), "--expect-delta", str(golden)]) == 0
    assert seam_dump.main(["compare", str(a), str(a), "--expect-delta", str(golden)]) == 1
    c = _fake_dump(tmp_path, "c.dump", tweak=900)
    assert seam_dump.main(["compare", str(a), str(c), "--expect-delta", str(golden)]) == 1


def test_golden_d13_hash_is_recorded():
    txt = (GOLDEN / "baseline_mb.txt").read_text()
    assert re.search(r"^mb_touch1_d13_body_sha256 [0-9a-f]{64}$", txt, re.M)
    base = re.search(r"^mb_touch1_body_sha256 (\w+)$", txt, re.M).group(1)
    d13 = re.search(r"^mb_touch1_d13_body_sha256 (\w+)$", txt, re.M).group(1)
    assert base != d13, "the D13 dump hash equals the baseline's -- the cell swap is not in it"
