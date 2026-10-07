#!/usr/bin/env python3
"""dts_gates.py — the gates on the Linux harness device tree, each with a
negative control that must FAIL (a gate that cannot fail is a comment).

    python3 tools/dts_gates.py [--usd-rtl PATH/usd_spi.sv]

G1 FRESH      src/linux_harness/shell_linux.dts == a fresh gen_dts.py render.
              (check_generated_fresh.py also covers this; repeated here so
              this file is a complete statement of the DTS contract.)
G2 OWNERSHIP  on the COMMITTED text: a generic-uio node carries no other
              compatible and has a unique linux,uio-name; no kernel node has a
              uio-name; no two /soc nodes overlap (one page, one owner).
G3 GENERATOR  the generator refuses a block with no owner and a kernel-owned
              block it has no driver binding for.
G4 DTC        the committed DTS compiles with dtc -p 4096 and no warnings.
G5 ETH_INT    the polarity triple (SHELL_CONTRACT §4): the eth node has no
              smsc,irq-active-high / smsc,irq-push-pull, the XDC pulls ETH_INT
              up, and under MPS3_SHELL_CPU_MBV shell_top feeds the INTC ~ETH_INT.
G6 D13        the D13 preview: with OVLSTORE replaced by USD (as D13's regmap
              patch does), the tree renders a spi-usd controller + mmc-spi-slot
              whose CD is gpios[0] (NOT cd-gpios: mmc_spi reads index 0 of
              the legacy "gpios"), passes G2 and compiles.
G8 INTC       only kernel-owned nodes reference AXI INTC inputs, and In0
              (HWICAP) / In4 (TOUCH pen-down) are referenced by nothing, so they
              stay masked (SHELL_CONTRACT §4: harnessd polls both).
G7 DRIVER     spi-usd.c's register offsets and ID equal the USD block's —
              gen_regmap's derived offsets once USD is in the BD, else the RTL
              given with --usd-rtl (D13's usd_spi.sv), else SKIP (said so).
G9 DUT FLASH  ILA finding #24: nothing in the harness may be able to program or
              erase the DUT's SST26. The tree (as written AND as dtc reads it
              back) has no node, reg or ranges in the DUT's QSPI windows
              (qspi_mem 0x7000_0000 / qspi_ctrl 0x7400_0000, 64 MiB each) and
              no "jedec,spi-nor" anywhere. (The kernel side is mps3_image.py
              kconfig-gate: no MTD / MTD_SPI_NOR / SPI_XILINX*.)
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_dts  # noqa: E402
import gen_regmap  # noqa: E402
from genlib import GenError  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DTS = ROOT / gen_dts.OUT
XDC = ROOT / "fpga/shell/constraints/mps3_harness.xdc"
TOP = ROOT / "fpga/shell/shell_top.sv"
FORK_TOP = ROOT / "src/linux_harness/impl/shell_linux_top.sv"
DRIVER = ROOT / "src/linux_harness/sw/br2_external/package/mps3-spi-usd/src/spi-usd.c"

results: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, why: str = "") -> None:
    results.append((name, ok, why))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {why}" if why else ""))


def neg(name: str, failed: bool, why: str = "") -> None:
    """A negative control PASSES when the check it exercises FAILED."""
    rec(f"{name} [negative control]", failed,
        why if failed else "the check did NOT fail on a broken input")


# --------------------------------------------------------------------------- #
def dtc_compile(text: str) -> tuple[int, str]:
    dtc = shutil.which("dtc")
    if dtc is None:
        return -1, "dtc not on PATH"
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "t.dts"
        src.write_text(text)
        p = subprocess.run([dtc, "-I", "dts", "-O", "dtb", "-p", "4096",
                            "-o", str(Path(td) / "t.dtb"), str(src)],
                           capture_output=True, text=True)
        return p.returncode, p.stderr.strip()


def eth_node(text: str) -> dict | None:
    for n in gen_dts.parse_dts_nodes(text):
        if "smsc,lan9115" in gen_dts.compats(n["props"].get("compatible")):
            return n
    return None


def eth_flags_ok(text: str) -> tuple[bool, str]:
    n = eth_node(text)
    if n is None:
        return False, "no smsc911x node"
    bad = [p for p in ("smsc,irq-active-high", "smsc,irq-push-pull") if p in n["props"]]
    return (not bad), (f"{bad} present" if bad else "no smsc,irq-* flags")


def xdc_pullup_ok(xdc: str) -> tuple[bool, str]:
    ok = re.search(r"^\s*set_property\s+PULLTYPE\s+PULLUP\s+\[get_ports\s+\{?ETH_INT\}?\]",
                   xdc, re.M) is not None
    return ok, "PULLTYPE PULLUP on ETH_INT" if ok else "no PULLUP on ETH_INT"


def mbv_eth_irq_expr(sv: str, define: str = "MPS3_SHELL_CPU_MBV") -> str | None:
    """The expression bound to .eth_irq when ``define`` is set (a tiny
    `ifdef/`ifndef/`else/`endif tracker; other defines are treated as unset)."""
    stack: list[bool] = []          # is the current branch active?
    for ln in sv.splitlines():
        s = ln.strip()
        m = re.match(r"`(ifdef|ifndef)\s+(\w+)", s)
        if m:
            on = (m.group(2) == define) == (m.group(1) == "ifdef")
            stack.append(on)
            continue
        if s.startswith("`else"):
            if stack:
                stack[-1] = not stack[-1]
            continue
        if s.startswith("`endif"):
            if stack:
                stack.pop()
            continue
        if all(stack):
            m = re.search(r"\.eth_irq\s*\(\s*([^)]*?)\s*\)", ln.split("//")[0])
            if m:
                return m.group(1)
    return None


def top_inverts(sv: str) -> tuple[bool, str]:
    e = mbv_eth_irq_expr(sv)
    if e is None:
        return False, ".eth_irq not bound under MPS3_SHELL_CPU_MBV"
    ok = re.fullmatch(r"~\s*ETH_INT", e) is not None
    return ok, f".eth_irq({e}) under MPS3_SHELL_CPU_MBV"


class _Blk:
    """A stand-in gen_regmap.Block for the D13 preview / negative controls."""
    def __init__(self, name, base, rng, cell, gate=None):
        self.name, self.base, self.range, self.cell, self.gate = name, base, rng, cell, gate
        self.legacy_of, self.bd_file = None, "fpga/shell/bd/shell_bd.tcl"


def d13_blocks() -> list:
    out = []
    for b in gen_regmap.derive(ROOT):
        if b.name == "OVLSTORE":
            out.append(_Blk("USD", b.base, b.range, "usd_spi_0"))
        else:
            out.append(b)
    return out


def intc_inputs_ok(text: str) -> tuple[bool, str]:
    used, bad = {}, []
    for n in gen_dts.parse_dts_nodes(text):
        if not (n["path"].startswith("/soc/") and n["path"].count("/") == 2):
            continue
        if n["props"].get("interrupt-parent", "").replace(" ", "") != "<&axi_intc>":
            continue
        m = re.match(r"<\s*(\d+)", n["props"].get("interrupts", ""))
        if not m:
            continue
        where = f"{n['name']}@{n['unit']:x}"
        used[int(m.group(1))] = where
        if "generic-uio" in gen_dts.compats(n["props"].get("compatible")):
            bad.append(f"{where} (a UIO node) takes INTC In{m.group(1)}")
    for i in (0, 4):
        if i in used:
            bad.append(f"INTC In{i} is referenced by {used[i]} — it must stay masked")
    return (not bad and bool(used)), ("; ".join(bad) if bad else
        "INTC inputs used: " + ", ".join(f"In{k}={v}" for k, v in sorted(used.items()))
        + " — In0/In4 unreferenced")


def usd_tree_ok(text: str) -> tuple[bool, str]:
    nodes = gen_dts.parse_dts_nodes(text)
    usd = [n for n in nodes
           if "soclabs,usd-spi-1.0" in gen_dts.compats(n["props"].get("compatible"))]
    slot = [n for n in nodes
            if "mmc-spi-slot" in gen_dts.compats(n["props"].get("compatible"))]
    rc, err = dtc_compile(text)
    bad = gen_dts.verify_dts(text)
    ok = (len(usd) == 1 and usd[0]["unit"] == 0x44A40000
          and "gpio-controller" in usd[0]["props"]
          and len(slot) == 1 and "gpios" in slot[0]["props"]
          and "cd-gpios" not in slot[0]["props"]
          and "voltage-ranges" in slot[0]["props"]
          and not bad and rc == 0 and not err)
    return ok, ("spi@44a40000 (gpio-controller) + mmc@0 (gpios[0]=CD, voltage-ranges), "
                "ownership clean, dtc clean" if ok else
                f"usd={len(usd)} slot={len(slot)} own={bad} dtc={rc} {err}")


#: The DUT's QSPI windows (its own map: qspi_mem, qspi_ctrl) -- DRIVER_MATRIX §2.6.
DUT_FLASH_WINDOWS = ((0x70000000, 0x74000000, "qspi_mem"), (0x74000000, 0x78000000, "qspi_ctrl"))


def _dtc_readback(text: str) -> str | None:
    """The tree as dtc reads it back (-O dts): macros, labels and /include/s
    resolved, every cell in hex. None when dtc is missing or refuses."""
    dtc = shutil.which("dtc")
    if dtc is None:
        return None
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "t.dts"
        src.write_text(text)
        p = subprocess.run([dtc, "-I", "dts", "-O", "dts", "-q", str(src)],
                           capture_output=True, text=True)
        return p.stdout if p.returncode == 0 else None


def _dut_flash_hits(text: str) -> list[str]:
    hits = []
    for m in re.finditer(r"^\s*(?:[\w-]+:\s*)?([\w,.+-]+)@([0-9a-fA-F]+)(?:,[0-9a-fA-F]+)*\s*\{",
                         text, re.M):
        unit = int(m.group(2), 16)
        for lo, hi, what in DUT_FLASH_WINDOWS:
            if lo <= unit < hi:
                hits.append(f"node {m.group(1)}@{m.group(2)} is in the DUT's {what}")
    for m in re.finditer(r"^\s*(reg|ranges)\s*=\s*([^;]*);", text, re.M):
        for c in re.findall(r"0x[0-9a-fA-F]+|\b\d+\b", m.group(2)):
            v = int(c, 0)
            for lo, hi, what in DUT_FLASH_WINDOWS:
                if lo <= v < hi:
                    hits.append(f"{m.group(1)} cell {c} is in the DUT's {what}")
    if re.search(r"jedec\s*,\s*spi-nor", text):
        hits.append('"jedec,spi-nor" in the tree')
    return hits


def dut_flash_ok(text: str) -> tuple[bool, str]:
    """G9: no way into the DUT's SST26 from this tree, on the text as written and
    as dtc reads it back (so a macro or label cannot hide a node). Refuses a
    vacuous pass: the tree must have /soc nodes and dtc must read it back."""
    back = _dtc_readback(text)
    nodes = [n for n in gen_dts.parse_dts_nodes(text) if n["path"].startswith("/soc/")]
    if back is None or not nodes:
        return False, "vacuous: " + ("dtc could not read the tree back" if back is None
                                     else "no /soc nodes parsed")
    hits = sorted(set(_dut_flash_hits(text) + _dut_flash_hits(back)))
    return (not hits, "; ".join(hits) if hits else
            f"{len(nodes)} /soc nodes: none in 0x7000_0000-0x77FF_FFFF, no jedec,spi-nor")


def driver_regs() -> dict[str, int]:
    src = DRIVER.read_text()
    regs = {m.group(1): int(m.group(2), 0) for m in re.finditer(
        r"#define\s+USD_REG_(\w+)\s+(0x[0-9A-Fa-f]+)", src)}
    m = re.search(r"#define\s+USD_ID_VALUE\s+(0x[0-9A-Fa-f]+)", src)
    if m:
        regs["__ID_VALUE__"] = int(m.group(1), 0)
    return regs


def rtl_regs(path: Path) -> dict[str, int]:
    regs = {name: off for name, off, _ in gen_regmap.parse_idx_decode(path)}
    m = re.search(r"32'h([0-9A-Fa-f_]{8,9})\s*;?\s*//[^\n]*USD1|USD_ID_VALUE\s*=\s*32'h([0-9A-Fa-f_]+)",
                  path.read_text())
    if m:
        regs["__ID_VALUE__"] = int((m.group(1) or m.group(2)).replace("_", ""), 16)
    return regs


def regs_match(drv: dict[str, int], ref: dict[str, int]) -> tuple[bool, str]:
    names = ("ID", "CTRL", "CLKDIV", "DATA", "STATUS")
    bad = [f"{n}: driver {drv.get(n)} vs {ref.get(n)}" for n in names
           if n not in drv or n not in ref or drv[n] != ref[n]]
    if "__ID_VALUE__" in ref and drv.get("__ID_VALUE__") != ref["__ID_VALUE__"]:
        bad.append(f"ID value: driver {drv.get('__ID_VALUE__')!r} vs {ref['__ID_VALUE__']:#x}")
    return (not bad), ("; ".join(bad) if bad else "ID/CTRL/CLKDIV/DATA/STATUS agree")


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--usd-rtl", type=Path, default=None,
                    help="D13's usd_spi.sv, for G7 before USD is in the BD")
    args = ap.parse_args()
    committed = DTS.read_text() if DTS.is_file() else ""
    print("== DTS gates (tools/dts_gates.py) ==")

    # G1 freshness
    try:
        fresh = gen_dts.build(ROOT)[gen_dts.OUT]
        rec("G1 fresh", fresh == committed,
            "committed == generated" if fresh == committed
            else "stale: python3 tools/gen_dts.py")
    except GenError as e:
        fresh = None
        rec("G1 fresh", False, f"generator failed: {e}")
    neg("G1 fresh", (committed + " ") != (fresh or committed), "one appended byte is seen")

    # G2 ownership on the committed text
    st: dict = {}
    bad = gen_dts.verify_dts(committed, st)
    rec("G2 ownership", not bad and st.get("uio", 0) > 0 and st.get("kernel", 0) > 0,
        "; ".join(bad) or f"{st['nodes']} /soc nodes checked: {st['uio']} harnessd "
        f"(generic-uio only), {st['kernel']} kernel, no overlap")
    mut = re.sub(r'(compatible = "smsc,lan9220", "smsc,lan9115")',
                 r'\1, "generic-uio"', committed)
    neg("G2 kernel node + generic-uio", bool(gen_dts.verify_dts(mut)))
    mut = committed.replace("\t};\n};", "\t\tdup: dup@41c00000 {\n\t\t\tcompatible = "
                            "\"generic-uio\";\n\t\t\treg = <0x41c00000 0x10000>;\n\t\t\t"
                            "linux,uio-name = \"dup\";\n\t\t};\n\t};\n};", 1)
    neg("G2 UIO node on the timer page", bool(gen_dts.verify_dts(mut)))
    mut = committed.replace('\t\t\tlinux,uio-name = "wdog";\n', "", 1)
    neg("G2 UIO node without uio-name", bool(gen_dts.verify_dts(mut)))

    # G3 generator refusals
    blocks = gen_regmap.derive(ROOT) + [_Blk("NEWBLK", 0x44BF0000, "64K", "new_0")]
    try:
        gen_dts.derive(ROOT, blocks=blocks, json_owners=None)
        neg("G3 block with no owner", False)
    except GenError as e:
        neg("G3 block with no owner", True, str(e).split(".")[0])
    saved = dict(gen_dts.OWNERS)
    try:
        gen_dts.OWNERS["CLKRST"] = gen_dts.OWN_KERNEL
        gen_dts.derive(ROOT, json_owners=None)
        neg("G3 kernel-owned block with no driver binding", False)
    except GenError as e:
        neg("G3 kernel-owned block with no driver binding", True, str(e).split(" — ")[0])
    finally:
        gen_dts.OWNERS.clear()
        gen_dts.OWNERS.update(saved)
    rec("G3 generator", True, "renders with every block owned (see G1)")

    # G4 dtc
    rc, err = dtc_compile(committed)
    if rc == -1:
        rec("G4 dtc", False, err)
    else:
        rec("G4 dtc", rc == 0 and not err, err or "dtc -p 4096 clean")
        rc2, _ = dtc_compile(committed.replace("ranges;", "ranges", 1))
        neg("G4 dtc on a broken tree", rc2 != 0)

    # G5 ETH_INT triple
    ok1, w1 = eth_flags_ok(committed)
    ok2, w2 = xdc_pullup_ok(XDC.read_text()) if XDC.is_file() else (False, "no XDC")
    topfile = TOP if gen_dts.mbv_source(ROOT) == gen_dts.CPU_MBV_BD else FORK_TOP
    if topfile == FORK_TOP:
        # the fork top has no ifdef: its single binding is the MBV one
        e = re.search(r"\.eth_irq\s*\(\s*([^)]*?)\s*\)", FORK_TOP.read_text())
        ok3, w3 = (e is not None and e.group(1).replace(" ", "") == "~ETH_INT",
                   f"{FORK_TOP.name}: .eth_irq({e.group(1) if e else '?'})")
    else:
        ok3, w3 = top_inverts(TOP.read_text())
    rec("G5 ETH_INT polarity triple", ok1 and ok2 and ok3, f"{w1}; {w2}; {w3}")
    neg("G5 DTS with smsc,irq-active-high",
        not eth_flags_ok(committed.replace("\t\t\treg-io-width = <4>;\n",
                                           "\t\t\treg-io-width = <4>;\n\t\t\tsmsc,irq-active-high;\n", 1))[0])
    neg("G5 MBV top without the inversion",
        not top_inverts("`ifdef MPS3_SHELL_CPU_MBV\n  .eth_irq (ETH_INT),\n`else\n"
                        "  .eth_irq (~ETH_INT),\n`endif\n")[0])

    # G8 INTC inputs
    ok, why = intc_inputs_ok(committed)
    rec("G8 INTC inputs", ok, why)
    neg("G8 a UIO node taking In0", not intc_inputs_ok(committed.replace(
        '\t\t\tlinux,uio-name = "hwicap";\n',
        '\t\t\tlinux,uio-name = "hwicap";\n\t\t\tinterrupt-parent = <&axi_intc>;\n'
        '\t\t\tinterrupts = <0 4>;\n', 1))[0])

    # G9 DUT flash (ILA #24)
    ok, why = dut_flash_ok(committed)
    rec("G9 no way into the DUT's SST26", ok, why)
    node = ("\t\tdutflash: {n}@{a:x} {{\n\t\t\tcompatible = \"generic-uio\";\n\t\t\t"
            "reg = <0x{a:x} 0x1000>;\n\t\t\tlinux,uio-name = \"dutflash\";\n\t\t}};\n\t}};\n}};")
    neg("G9 a node at 0x7000_0000 (qspi_mem)", not dut_flash_ok(committed.replace(
        "\t};\n};", node.format(n="flash", a=0x70000000), 1))[0])
    neg("G9 a node at 0x7400_0000 (qspi_ctrl)", not dut_flash_ok(committed.replace(
        "\t};\n};", node.format(n="spi", a=0x74000000), 1))[0])
    neg("G9 a jedec,spi-nor child under the usd controller", not dut_flash_ok(committed.replace(
        '"mmc-spi-slot"', '"jedec,spi-nor"', 1))[0])
    hidden = committed.replace(   # 0x38000000 * 2 = 0x7000_0000: no literal in the window
        "\t};\n};", "\t\tq: dutq {\n\t\t\treg = <(0x38000000 * 2) 0x100>;\n\t\t};\n\t};\n};", 1)
    neg("G9 a DUT-window reg written as an expression (only dtc's read-back sees it)",
        not _dut_flash_hits(hidden) and not dut_flash_ok(hidden)[0])

    # G6 D13 preview
    try:
        text = gen_dts.render(gen_dts.derive(ROOT, blocks=d13_blocks(), json_owners=None))
        ok, why = usd_tree_ok(text)
        rec("G6 D13 preview (USD kernel-owned)", ok, why)
        neg("G6 on a slot whose CD is cd-gpios (mmc_spi ignores it)",
            not usd_tree_ok(text.replace("\t\tgpios = <&usd", "\t\tcd-gpios = <&usd"))[0])
        neg("G6 on a USD node that also claims generic-uio",
            not usd_tree_ok(text.replace('"soclabs,usd-spi-1.0"',
                                         '"soclabs,usd-spi-1.0", "generic-uio"'))[0])
    except GenError as e:
        rec("G6 D13 preview (USD kernel-owned)", False, str(e))

    # G7 driver offsets
    if not DRIVER.is_file():
        rec("G7 spi-usd register map", False, f"{DRIVER} missing")
    else:
        drv = driver_regs()
        ref, src = None, None
        usd = [b for b in gen_regmap.derive(ROOT) if b.name == "USD"]
        if usd and usd[0].regs:
            ref = {n: off for n, off, _ in usd[0].regs}
            src = "gen_regmap (USD in the BD)"
        elif args.usd_rtl and args.usd_rtl.is_file():
            ref, src = rtl_regs(args.usd_rtl), str(args.usd_rtl)
        if ref is None:
            print("  SKIP  G7 spi-usd register map — USD is not in the BD yet; "
                  "pass --usd-rtl <usd_spi.sv> to check against D13's RTL")
        else:
            ok, why = regs_match(drv, ref)
            rec("G7 spi-usd register map", ok, f"{why} ({src})")
            bad_ref = dict(ref)
            bad_ref["DATA"] = ref.get("DATA", 0) + 4
            neg("G7 with a moved DATA register", not regs_match(drv, bad_ref)[0])

    nfail = sum(1 for _, ok, _ in results if not ok)
    print(f"== {len(results) - nfail}/{len(results)} PASS" +
          (f", {nfail} FAIL" if nfail else ""))
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
