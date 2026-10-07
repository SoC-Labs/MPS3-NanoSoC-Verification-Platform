#!/usr/bin/env python3
# DEPRECATED 2026-09-23 (LINUX_HARNESS_PLAN_2026-09-23.md DL2): the July Linux
# fork is superseded by the ONE shell BD's CPU seam -- fpga/shell/bd/shell_bd.tcl +
# cpu_mbv.tcl (SHELL_CPU=mbv), fpga/shell/build_shell.tcl and validate_bd.tcl,
# fpga/shell/shell_top.sv (`ifdef MPS3_SHELL_CPU_MBV), fpga/shell/constraints/mbv/.
# Do not edit or build this file; it is deleted at landing (lead).
"""mk_bd_wrapper_stub.py — emit a BLACK-BOX ``shell_linux_bd_wrapper`` so
``shell_linux_top.sv`` can be RTL-elaborated WITHOUT generating the block design.

WHY THIS EXISTS
    The one thing worth elaborating in this fork cheaply is the binding
    ``rp_dut u_rp_dut (...)`` makes against the GENERATED
    ``fpga/shell/rp_dut_stub.sv``. That is the binding cb45c18 broke and
    [DEV-10] fixed. Proving it used to mean a full Vivado project: snapshot the
    IP catalogue, build the DDR4 MIG, assemble ~45 BD cells, validate, generate
    the HDL wrapper -- tens of minutes, a licence, and a scratch tree, to check
    thirty-five port names.

    Vivado will not elaborate a top whose sub-module is missing: an unresolved
    instance is ERROR [Synth 8-439], not a black-box warning, and it ABORTS the
    run before the interesting instance is reached. So the wrapper has to exist.
    This writes the smallest thing that can: ports only, no body.

WHERE THE PORTS COME FROM (nothing is typed here)
    * every ``create_bd_port -dir ... <name>`` in ``shell_linux_bd.tcl`` -- the
      BD's own declaration of its scalar boundary, including all 34 ``rp_*``
      partition pins;
    * the ``c0_ddr4_*`` group, which is NOT a ``create_bd_port``: it reaches the
      wrapper through ``make_bd_intf_pins_external`` on the MIG's physical
      interface. Its member names and widths are recovered from
      ``src/linux_soc/hw/ddr4_pins.xdc`` (max bit index per port), which is the
      file whose ``PACKAGE_PIN`` names the wrapper must match anyway -- the
      [MBV-LESSON] rename comment in shell_linux_bd.tcl exists precisely because
      a mismatch there silently unconstrains every memory pin;
    * the ``EMC_INTF_*`` group, the one remaining flattened interface port
      (``create_bd_intf_port ... emc_rtl:1.0 EMC_INTF``). Its member names are
      defined by the Xilinx interface, not by anything in this repo, so they are
      the ONE table below that is written out rather than derived -- and only
      the six members shell_linux_top.sv actually binds. A missing stub port is
      a loud elaboration error, never a silent pass, so under-declaring here
      cannot weaken the check.

    Deriving from the BD rather than from shell_linux_top.sv is deliberate: a
    stub copied from the top would make the elaboration agree with itself.
    Elaborating against this stub therefore ALSO checks that every scalar port
    the BD exports is bound by the top under the same name.

WHAT IT DOES NOT PROVE
    Not the BD's internals, not that the BD validates, not that jtag_bb is in
    the IP catalogue. Those need ``validate_shell_linux_bd.tcl``. This is the
    cheap half.

Usage:  mk_bd_wrapper_stub.py <out.sv>
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LH = HERE.parent
BD_TCL = LH / "shell_linux_bd.tcl"
DDR4_XDC = LH.parent / "linux_soc" / "hw" / "ddr4_pins.xdc"

#: DDR4 physical-interface members whose direction is INOUT at the wrapper.
#: Everything else in the group is an output of the controller. (Direction, not
#: width, is the only thing not mechanically recoverable from the XDC; a
#: PACKAGE_PIN constraint says nothing about direction.)
DDR4_INOUT = {"c0_ddr4_dq", "c0_ddr4_dqs_t", "c0_ddr4_dqs_c", "c0_ddr4_dm_n"}

#: The flattened ``emc_rtl`` members the top binds: (name, direction, width).
#: Widths follow axi_emc_0's configuration (32-bit address, 16-bit data bus to
#: the LAN9220). The unbound members the BD lists as "SRAM-mode unused outputs,
#: intentionally unconnected" are simply not declared: an absent stub port only
#: matters if the top binds it, and then it is an error, not a silent pass.
EMC_PORTS = [
    ("EMC_INTF_addr", "output", 32),
    ("EMC_INTF_ce_n", "output", 1),
    ("EMC_INTF_oen", "output", 1),
    ("EMC_INTF_wen", "output", 1),
    ("EMC_INTF_dq_io", "inout", 16),
    ("EMC_INTF_wait", "input", 1),
]

#: ``-type clk``/``-type rst`` and ``-freq_hz`` are optional decorations Vivado
#: accepts before the port name (the BD uses them on osc_clk_50m / sys_rst_n);
#: they do not change the wrapper port, only the BD's interface inference.
_BD_PORT = re.compile(
    r"^\s*create_bd_port\s+-dir\s+([IO])"
    r"(?:\s+-type\s+\w+)?(?:\s+-freq_hz\s+\S+)?"
    r"(?:\s+-from\s+(\d+)\s+-to\s+(\d+))?"
    r"\s+([A-Za-z_]\w*)\s*$", re.M)


def _strip_tcl_comments(src: str) -> str:
    out = []
    for line in src.splitlines():
        s = re.sub(r";\s*#.*$", "", line)
        out.append("" if re.match(r"^\s*#", s) else s)
    return "\n".join(out)


def bd_ports(tcl: str) -> list[tuple[str, str, int]]:
    """``[(name, "input"|"output", width)]`` — BD direction is the BD's own
    view, which is also the wrapper's."""
    out = []
    for d, hi, lo, name in _BD_PORT.findall(_strip_tcl_comments(tcl)):
        width = 1 if hi == "" else int(hi) - int(lo) + 1
        out.append((name, "input" if d == "I" else "output", width))
    return out


def ddr4_ports(xdc: str) -> list[tuple[str, str, int]]:
    """``c0_ddr4_*`` members recovered from the pin constraints. ``c0_sys_clk_*``
    is excluded: it IS a create_bd_port and would be declared twice."""
    widths: dict[str, int] = {}
    for m in re.finditer(r"get_ports\s*\{\s*(c0_ddr4_[a-z0-9_]+?)(?:\[(\d+)\])?\s*\}", xdc):
        name, idx = m.group(1), m.group(2)
        w = int(idx) + 1 if idx is not None else 1
        widths[name] = max(widths.get(name, 1), w)
    return [(n, "inout" if n in DDR4_INOUT else "output", w)
            for n, w in sorted(widths.items())]


def render(ports: list[tuple[str, str, int]]) -> str:
    lines = [
        "// GENERATED by src/linux_harness/impl/mk_bd_wrapper_stub.py — DO NOT EDIT,",
        "// DO NOT COMMIT, DO NOT SYNTHESISE. A ports-only black box standing in for",
        "// the Vivado-generated shell_linux_bd_wrapper so shell_linux_top.sv can be",
        "// RTL-elaborated without building the block design. Ports are derived from",
        "// shell_linux_bd.tcl's create_bd_port lines + ddr4_pins.xdc.",
        "`timescale 1ns / 1ps",
        "",
        "module shell_linux_bd_wrapper (",
    ]
    body = []
    for name, direction, width in ports:
        rng = "" if width == 1 else f"[{width - 1}:0] "
        body.append(f"  {direction:<6} wire {rng}{name}")
    lines.append(",\n".join(body))
    lines += [");", "  // ports only: an elaboration black box has no body.",
              "endmodule", ""]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    for f in (BD_TCL, DDR4_XDC):
        if not f.is_file():
            print(f"missing {f}", file=sys.stderr)
            return 1
    ports = (bd_ports(BD_TCL.read_text())
             + ddr4_ports(DDR4_XDC.read_text())
             + EMC_PORTS)
    names = [p[0] for p in ports]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        print(f"duplicate wrapper ports: {sorted(dupes)}", file=sys.stderr)
        return 1
    out = Path(argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(ports))
    rp = sum(1 for n in names if n.startswith("rp_"))
    print(f"{out}: {len(ports)} ports ({rp} rp_* partition pins, "
          f"{sum(1 for n in names if n.startswith('c0_ddr4_'))} DDR4)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
