#!/usr/bin/env python3
"""gen_dts.py — the Linux harness device tree, derived from the shell BD.

WHY THIS EXISTS
---------------
``src/linux_harness/shell_linux.dts`` was hand-written in July against the
fork BD (``src/linux_harness/shell_linux_bd.tcl``) and then drifted from the
shell it has to describe: the fork never gained DUTEGR, USRACC, WDOG, TOUCH or
VPHY/GENCHK, and neither did the DTS. Under Linux a device tree that lies is
worse than a stale C header — a node for a page with no slave is a bus fault
the first time anything touches it, and a missing node is a service that
cannot start. This file derives the tree from the same BD lines
``tools/gen_regmap.py`` reads, so the two can no longer disagree.

THE OWNERSHIP RULE (LINUX_HARNESS_PLAN §3, SHELL_CONTRACT §2, HARNESSD_CONTRACT §3)
----------------------------------------------------------------------------------
Every block is owned by exactly one of:

  kernel    it gets its kernel driver node (console uartlite, AXI INTC, the
            patch-0003 AXI-timer clockevent, the LAN9220 on the EMC, D13's
            ``usd_spi`` -> spi-usd -> mmc-spi-slot) and NO ``generic-uio``;
  harnessd  it gets ONE node whose ONLY compatible is ``generic-uio`` (bound by
            ``uio_pdrv_genirq.of_id=generic-uio`` on the command line) plus a
            ``linux,uio-name`` — nothing a kernel driver could ever match;
  none      no node at all (the pre-D13 pad-less ``axi_quad_spi_0``).

A block with no owner is a hard error: a new BD block must be given an owner
before it can reach a device tree. ``tools/dts_gates.py`` re-checks the rule on
the COMMITTED file and proves, with negative controls, that the check can fail.

WHAT IS DERIVED, AND FROM WHAT
------------------------------
1. Contract-window blocks (0x44A0_0000..) — ``gen_regmap.derive()``.
2. CPU-side cells (AXI INTC, uartlite, AXI timer, the EMC in front of the
   LAN9220) — their ``assign_bd_address`` lines in the shell BD and, once it
   exists, the SHELL lane's ``cpu_mbv.tcl``.
3. DDR window and the 128 KiB LMB — ``cpu_mbv.tcl`` once it exists, until then
   the fork BD's SECTION 0 constants (the file the July silicon ran). The
   generated header names the source, so the switch-over shows in the diff.
4. The interrupt map — the ``connect_bd_net ... xlconcat_intr/In<n>`` lines.
5. OWNERSHIP — ``fpga/shell/generated/regmap_mbv.json`` (SHELL_CONTRACT §2:
   "every block in it has an owner of kernel, harnessd or none") when it
   exists, matched BY BASE ADDRESS; until then the interim OWNERS table below.
   When the JSON exists, every base it lists must agree with (1)-(3).

WHAT IS DECLARED
----------------
The interim owner table, the ``linux,uio-name`` spellings (HARNESSD_CONTRACT
§3), the MBV ISA string (SHELL_CONTRACT §7) and the per-driver node bodies,
which carry the silicon lessons as comments because a reader editing a device
tree needs them there.

    python3 tools/gen_dts.py [--check|--list|--out-dir DIR]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import genlib  # noqa: E402
from genlib import GenError  # noqa: E402
import gen_regmap  # noqa: E402

GENERATOR = "gen_dts.py"
OUT = "src/linux_harness/shell_linux.dts"

SHELL_BD = "fpga/shell/bd/shell_bd.tcl"
TOUCH_BD = "fpga/shell/bd/touch_iic_add.tcl"
CPU_MBV_BD = "fpga/shell/bd/cpu_mbv.tcl"          # the SHELL lane's CPU seam
FORK_BD = "src/linux_harness/shell_linux_bd.tcl"   # interim MBV-only facts
REGMAP_MBV_JSON = "fpga/shell/generated/regmap_mbv.json"

#: The gated BD additions the MBV static is built WITH. SHELL_CONTRACT §2: the
#: mint sets SHELL_TOUCH=1, so TOUCH gets a node and the INTC has 5 inputs.
GATES = {"SHELL_TOUCH": True}

#: shell_clk: hart, timebase, every AXI-Lite slave (SHELL_CONTRACT §7).
SHELL_CLK_HZ = 100_000_000

OWN_KERNEL, OWN_HARNESSD, OWN_NONE = "kernel", "harnessd", "none"

#: INTERIM owners, used only until fpga/shell/generated/regmap_mbv.json exists.
#: Copied from SHELL_CONTRACT §2 / HARNESSD_CONTRACT §3 (v1, 2026-09-23).
OWNERS = {
    "INTC": OWN_KERNEL, "UARTLITE": OWN_KERNEL, "TIMER": OWN_KERNEL,
    "EMC": OWN_KERNEL,
    "CLKRST": OWN_HARNESSD, "DFXCTL": OWN_HARNESSD,
    "HWICAP": OWN_HARNESSD,     # swap_fsm drives ICAP through UIO (DL6)
    "VPHY": OWN_HARNESSD,
    "OVLSTORE": OWN_NONE,       # axi_quad_spi_0 has no pads (D16); D13 replaces it
    "USD": OWN_KERNEL,          # usd_spi -> spi-usd -> mmc_spi -> /dev/mmcblk0
    "TELEM": OWN_HARNESSD,      # STATUS[0] = DDR calib in mbv (SHELL_CONTRACT §5)
    "GENCHK": OWN_HARNESSD, "JTAGBB": OWN_HARNESSD, "DBGBR": OWN_HARNESSD,
    "UARTBR": OWN_HARNESSD, "GPIO": OWN_HARNESSD,
    "MMCM_DRP": OWN_HARNESSD,   # firmware clkrst does the PG065 DRP sequence
    "CLCD": OWN_HARNESSD, "CLCDKVM": OWN_HARNESSD, "TOUCH": OWN_HARNESSD,
    "DUTEGR": OWN_HARNESSD, "USRACC": OWN_HARNESSD,
    "WDOG": OWN_HARNESSD,       # harnessd arms + kicks it; CONFIG_WATCHDOG is off
    "LMB_TAIL": OWN_HARNESSD,   # stage0 status block + diag mailbox v8
}

#: ``linux,uio-name`` per harnessd block — HARNESSD_CONTRACT §3 spellings.
#: Informational (the UIO HAL maps by physical address); the July names are
#: kept where a legacy v0.7 daemon opens a node by name.
UIO_NAMES = {
    "CLKRST": "clkrst", "DFXCTL": "dfxctl", "HWICAP": "hwicap",
    "VPHY": "vphy", "TELEM": "telem", "GENCHK": "genchk",
    "JTAGBB": "jtag-bb", "DBGBR": "dbgbr", "UARTBR": "uartbr",
    "GPIO": "gpio", "MMCM_DRP": "mmcm-drp", "CLCD": "clcd",
    "CLCDKVM": "clcd-kvm", "TOUCH": "touch-iic", "DUTEGR": "dutegr",
    "USRACC": "usracc", "WDOG": "wdog", "LMB_TAIL": "lmb-tail",
}

#: CPU-side BD cell -> block name.
CPU_CELLS = {"axi_intc_0": "INTC", "axi_uartlite_0": "UARTLITE",
             "axi_timer_0": "TIMER", "axi_emc_0": "EMC"}

#: The LMB map (plan §3, SHELL_CONTRACT §3). UIO maps whole pages, so the node
#: is the LMB's last 4 KiB page: stage0 status @+0xE00 (STAGE0 writes; harnessd
#: writes only linux_confirm), diag mailbox v8 @+0xF00 (harnessd). The rest of
#: the page is the top of the stage0 image — nobody under Linux writes it.
LMB_KB_EXPECTED = 128
LMB_TAIL_BYTES = 0x1000

#: MicroBlaze V ISA (SHELL_CONTRACT §7, XSA-audited in July). "sstc" is absent
#: on purpose (errata E1/E2, the patch-0003 binding); no zicbom/zbc.
ISA_EXTENSIONS = ["i", "m", "a", "c", "zicsr", "zifencei", "zicntr",
                  "zba", "zbb", "zbs"]
ISA_STRING = "rv32imac_zicsr_zifencei_zba_zbb_zbs"

#: LAN9220 bring-up MAC ("MPS" tail) — the bare-metal shell's, so the hub's
#: ARP cache and the 192.168.10.101 block survive the cutover.
ETH_MAC = "02 00 00 4D 50 53"

#: ramoops/pstore in the top MiB of DDR (S15pstore).
RAMOOPS_BYTES = 0x100000


# --------------------------------------------------------------------------- #
# Tcl scraping — the three statement shapes the BD uses
# --------------------------------------------------------------------------- #
def _logical_lines(text: str) -> list[str]:
    out, cur = [], ""
    for ln in text.splitlines():
        if ln.rstrip().endswith("\\"):
            cur += ln.rstrip()[:-1] + " "
            continue
        out.append(cur + ln)
        cur = ""
    if cur:
        out.append(cur)
    return out


_SET_RE = re.compile(r"^\s*set\s+(?:::)?(\w+)\s+([^\s;]+)")
_ASSIGN_RE = re.compile(
    r"^\s*assign_bd_address\s+-offset\s+(\S+)\s+-range\s+(\S+)\s+"
    r"(?:\[(?:get_bd_addr_segs|_find_seg)\s+\{([^}]+)\}\]|(\$\w+))")
_IRQ_RE = re.compile(
    r"^\s*connect_bd_net\s+\[get_bd_(pins|ports)\s+([^\]]+?)\]\s+"
    r"\[get_bd_pins\s+\$xlconcat_intr/In(\$\{?\w+\}?|\d+)\]")
_NPORTS_RE = re.compile(
    r"set_property\s+CONFIG\.NUM_PORTS\s+\{(\d+)\}\s+\$xlconcat_intr")


def _code(line: str) -> str:
    """The statement part of a Tcl line (drops `;#` and whole-line comments)."""
    if line.strip().startswith("#"):
        return ""
    return line.split(";#", 1)[0]


def _tcl_vars(lines: list[str]) -> dict[str, str]:
    env = {}
    for ln in lines:
        m = _SET_RE.match(_code(ln))
        if m:
            env[m.group(1)] = m.group(2)
    return env


def _resolve(tok: str, env: dict[str, str], where: str) -> str:
    t = tok.strip()
    if t.startswith("$"):
        name = t[1:].strip("{}").lstrip(":")
        if name not in env:
            raise GenError(f"{where}: Tcl variable {t} is not set in that file")
        return env[name]
    return t


def _size(tok, where: str) -> int:
    if isinstance(tok, int):
        return tok
    m = re.fullmatch(r"(\d+)\s*([KMG])i?B?", str(tok).strip())
    if m:
        return int(m.group(1)) << {"K": 10, "M": 20, "G": 30}[m.group(2)]
    try:
        return int(str(tok), 0)
    except ValueError:
        raise GenError(f"{where}: cannot read a size out of {tok!r}") from None


def parse_assignments(repo: Path, rel: str) -> list[dict]:
    """Every assign_bd_address in ``rel`` -> [{cell, seg, base, size, file}]."""
    lines = _logical_lines(genlib.read(repo, rel))
    env = _tcl_vars(lines)
    out = []
    for ln in lines:
        m = _ASSIGN_RE.match(_code(ln))
        if not m:
            continue
        seg = (m.group(3) or _resolve(m.group(4), env, rel)).split()[0]
        out.append({"cell": seg.split("/")[0], "seg": seg,
                    "base": int(_resolve(m.group(1), env, rel), 0),
                    "size": _size(_resolve(m.group(2), env, rel), rel),
                    "file": rel})
    return out


def parse_irqs(repo: Path, rel: str) -> tuple[dict[int, str], int | None]:
    """xlconcat_intr In<n> -> source (a cell, or ``port:<name>``), NUM_PORTS."""
    lines = _logical_lines(genlib.read(repo, rel))
    env = _tcl_vars(lines)
    irqs: dict[int, str] = {}
    nports = None
    for ln in lines:
        code = _code(ln)
        m = _IRQ_RE.match(code)
        if m:
            kind, src, idx = m.group(1), m.group(2).strip(), m.group(3)
            n = int(_resolve(idx, env, rel), 0)
            who = ("port:" + src.split()[0]) if kind == "ports" else \
                src.split()[0].lstrip("$").split("/")[0]
            if n in irqs and irqs[n] != who:
                raise GenError(f"{rel}: xlconcat In{n} wired twice "
                               f"({irqs[n]} and {who})")
            irqs[n] = who
        m = _NPORTS_RE.search(code)
        if m:
            nports = int(m.group(1))
    return irqs, nports


# --------------------------------------------------------------------------- #
# The node list
# --------------------------------------------------------------------------- #
class Node:
    def __init__(self, name, base, size, origin, cell="", gate=None):
        self.name, self.base, self.size = name, base, size
        self.origin, self.cell, self.gate = origin, cell, gate
        self.owner = None
        self.irq = None


def mbv_source(repo: Path) -> str:
    return CPU_MBV_BD if (repo / CPU_MBV_BD).is_file() else FORK_BD


def _json_owners(repo: Path) -> dict[int, tuple] | None:
    """base -> (owner, name, irq|None) from SHELL's regmap_mbv.json, or None.

    Deliberately tolerant of the container shape (a list, or {"blocks": [...]})
    and of how a base/size is spelled; strict about what matters: a base and
    an owner of kernel/harnessd/none on every entry."""
    p = repo / REGMAP_MBV_JSON
    if not p.is_file():
        return None
    try:
        doc = json.loads(p.read_text())
    except ValueError as e:
        raise GenError(f"{REGMAP_MBV_JSON}: not JSON ({e})") from None
    rows = doc.get("blocks", doc.get("map")) if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        raise GenError(f"{REGMAP_MBV_JSON}: expected a list of blocks (or "
                       f"{{\"blocks\": [...]}}) — see IMAGE_CONTRACT §9")
    out = {}
    for r in rows:
        if not isinstance(r, dict) or "base" not in r or "owner" not in r:
            raise GenError(f"{REGMAP_MBV_JSON}: every block needs 'base' and "
                           f"'owner' (IMAGE_CONTRACT §9); got {r!r}")
        base = r["base"] if isinstance(r["base"], int) else int(str(r["base"]).replace("_", ""), 0)
        owner = str(r["owner"]).lower()
        if owner in ("nobody", "vestigial"):
            owner = OWN_NONE
        irq = r.get("irq")
        out[base] = (owner, str(r.get("name", r.get("block", "?"))),
                     int(irq) if isinstance(irq, (int, str)) and str(irq).isdigit() else None)
    return out


def derive(repo: Path, blocks=None, json_owners="auto") -> dict:
    """Everything the renderer needs. ``blocks`` overrides gen_regmap.derive()
    and ``json_owners`` the JSON lookup (tools/dts_gates.py uses both for the
    D13 preview and the negative controls)."""
    if blocks is None:
        blocks = gen_regmap.derive(repo)
    if json_owners == "auto":
        json_owners = _json_owners(repo)
    nodes: list[Node] = []

    # 1. contract-window blocks
    for b in blocks:
        if b.legacy_of:                    # SWDBB: an alias of JTAGBB's page
            continue
        if b.gate and not GATES.get(b.gate, False):
            continue
        nodes.append(Node(b.name, b.base, _size(b.range, b.bd_file),
                          f"{b.bd_file} ({b.cell})", b.cell, b.gate))

    # 2. CPU-side cells: the shell BD, then the MBV source; both must agree
    msrc = mbv_source(repo)
    seen: dict[str, dict] = {}
    for rel in (SHELL_BD, msrc):
        for a in parse_assignments(repo, rel):
            if a["cell"] not in CPU_CELLS:
                continue
            prev = seen.get(a["cell"])
            if prev and (prev["base"], prev["size"]) != (a["base"], a["size"]):
                raise GenError(
                    f"{a['cell']} is 0x{prev['base']:08X}/0x{prev['size']:X} in "
                    f"{prev['file']} but 0x{a['base']:08X}/0x{a['size']:X} in "
                    f"{a['file']}")
            seen.setdefault(a["cell"], a)
    for cell, name in CPU_CELLS.items():
        if cell not in seen:
            raise GenError(f"no assign_bd_address for {cell} in {SHELL_BD} or "
                           f"{msrc} — the CPU side moved or this parser broke")
        a = seen[cell]
        nodes.append(Node(name, a["base"], a["size"], f"{a['file']} ({cell})", cell))

    # 3. MBV-only facts: DDR, LMB, clock
    menv = _tcl_vars(_logical_lines(genlib.read(repo, msrc)))
    massign = parse_assignments(repo, msrc)
    ddr = [a for a in massign if "ddr" in a["seg"].lower()]
    lmb = [a for a in massign if "lmb_bram_if_cntlr" in a["seg"]]
    if not ddr:
        raise GenError(f"{msrc}: no DDR assign_bd_address found")
    if not lmb or len({(a["base"], a["size"]) for a in lmb}) != 1:
        raise GenError(f"{msrc}: need ILMB == DLMB assign_bd_address lines, got {lmb}")
    ddr_base, ddr_size = ddr[0]["base"], ddr[0]["size"]
    lmb_base, lmb_size = lmb[0]["base"], lmb[0]["size"]
    if lmb_size != LMB_KB_EXPECTED << 10:
        raise GenError(
            f"{msrc}: the LMB is {lmb_size >> 10} KiB; the LMB map (stage0 "
            f"status @0x1FE00, mailbox @0x1FF00) is for {LMB_KB_EXPECTED} KiB. "
            f"Move LMB_KB_EXPECTED with the STAGE0/HARNESSD contracts.")
    if "CPU_CLK_HZ" in menv and int(menv["CPU_CLK_HZ"], 0) != SHELL_CLK_HZ:
        raise GenError(f"{msrc}: CPU_CLK_HZ {menv['CPU_CLK_HZ']} != {SHELL_CLK_HZ}")
    nodes.append(Node("LMB_TAIL", lmb_base + lmb_size - LMB_TAIL_BYTES,
                      LMB_TAIL_BYTES, f"{msrc} (local_ram, last 4 KiB page)"))

    # 4. interrupts: shell BD, gated additions, MBV source
    irqs, nports = parse_irqs(repo, SHELL_BD)
    if GATES.get("SHELL_TOUCH") and (repo / TOUCH_BD).is_file():
        gi, gn = parse_irqs(repo, TOUCH_BD)
        irqs.update(gi)
        nports = gn or nports
    if msrc == CPU_MBV_BD:
        mi, mn = parse_irqs(repo, msrc)
        irqs.update(mi)
        nports = mn or nports
    if nports is None:
        raise GenError("xlconcat_intr CONFIG.NUM_PORTS not found")
    by_cell = {n.cell: n for n in nodes if n.cell}
    for idx, who in sorted(irqs.items()):
        tgt = by_cell.get("axi_emc_0") if who == "port:eth_irq" else by_cell.get(who)
        if tgt is not None and tgt.name in ("UARTLITE", "TIMER", "EMC"):
            tgt.irq = idx       # kernel consumers only: harnessd polls (SHELL §4)
    for cell in ("axi_uartlite_0", "axi_timer_0", "axi_emc_0"):
        if by_cell[cell].irq is None:
            raise GenError(f"{cell}: no xlconcat_intr input — its kernel driver "
                           f"needs the interrupt")

    # 5. owners: the JSON by base if it exists, else the interim table
    for n in nodes:
        if json_owners is not None:
            if n.base not in json_owners:
                raise GenError(f"{n.name} @ 0x{n.base:08X} is in the BD but not "
                               f"in {REGMAP_MBV_JSON} — regenerate it "
                               f"(python3 tools/gen_regmap.py)")
            n.owner, jname, jirq = json_owners[n.base]
            if n.owner == OWN_KERNEL and n.irq is not None and jirq is not None \
                    and jirq != n.irq:
                raise GenError(f"{n.name}: the BD wires INTC In{n.irq} but "
                               f"{REGMAP_MBV_JSON} says irq {jirq} ({jname})")
        elif n.name in OWNERS:
            n.owner = OWNERS[n.name]
        else:
            raise GenError(
                f"block {n.name} @ 0x{n.base:08X} ({n.cell or n.origin}) has no "
                f"owner. Every block is kernel- or harnessd-owned, never both "
                f"(LINUX_HARNESS_PLAN §3): give it one in {REGMAP_MBV_JSON} "
                f"(SHELL) or, until that exists, in tools/{GENERATOR} OWNERS.")
    check_ownership(nodes)
    return {"nodes": sorted(nodes, key=lambda n: n.base),
            "ddr": (ddr_base, ddr_size), "lmb": (lmb_base, lmb_size),
            "nports": nports, "mbv_source": msrc,
            "owner_source": REGMAP_MBV_JSON if json_owners is not None
            else f"tools/{GENERATOR} OWNERS (interim)"}


def check_ownership(nodes: list[Node]) -> None:
    for n in nodes:
        if n.owner not in (OWN_KERNEL, OWN_HARNESSD, OWN_NONE):
            raise GenError(f"{n.name}: owner {n.owner!r} is not kernel/harnessd/none")
        if n.owner == OWN_KERNEL and n.name not in RENDER_KERNEL:
            raise GenError(f"{n.name} is kernel-owned but has no driver binding "
                           f"here — add a renderer or make it harnessd-owned")
        if n.owner == OWN_HARNESSD and n.name not in UIO_NAMES:
            raise GenError(f"{n.name} is harnessd-owned but has no UIO name "
                           f"(HARNESSD_CONTRACT §3)")
    live = sorted((n for n in nodes if n.owner != OWN_NONE), key=lambda n: n.base)
    for a, b in zip(live, live[1:]):
        if a.base + a.size > b.base:
            raise GenError(f"{a.name} [0x{a.base:08X}+0x{a.size:X}) overlaps "
                           f"{b.name} @ 0x{b.base:08X}: one page, two owners")


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _h(x: int) -> str:
    return f"0x{x:x}"


def _uio(n: Node, _ctx) -> list[str]:
    name = UIO_NAMES[n.name]
    gate = f" GATED: built only with {n.gate}=1 (the MBV mint sets it)." if n.gate else ""
    return [
        f"/* {n.name} — {n.origin}. harnessd-owned.{gate} */",
        f"uio_{name.replace('-', '_')}: {name}@{n.base:x} {{",
        '\tcompatible = "generic-uio";',
        f"\treg = <{_h(n.base)} {_h(n.size)}>;",
        f'\tlinux,uio-name = "{name}";',
        "};",
    ]


def _r_intc(n: Node, ctx) -> list[str]:
    return [
        f"/* AXI INTC — {n.origin}. Chained under the hart's local intc at hwirq",
        " * 9 (S-mode external). kind-of-intr 0 = every input LEVEL (the BD pins",
        " * and asserts C_KIND_OF_INTR=0); irq-xilinx-intc picks its irq_chip off",
        " * this word, and the 0003 timer / 0004 uartlite ISRs rely on LEVEL. */",
        f"axi_intc: interrupt-controller@{n.base:x} {{",
        '\tcompatible = "xlnx,xps-intc-1.00.a";',
        f"\treg = <{_h(n.base)} {_h(n.size)}>;",
        "\tinterrupt-controller;",
        "\t#address-cells = <0>;      /* dtc >= 1.6.1 interrupt_provider */",
        "\t#interrupt-cells = <2>;",
        "\txlnx,kind-of-intr = <0x0>;",
        f"\txlnx,num-intr-inputs = <{ctx['nports']}>;",
        "\tinterrupt-parent = <&cpu_intc>;",
        "\tinterrupts = <9>;",
        "};",
    ]


def _r_uart(n: Node, _ctx) -> list[str]:
    return [
        f"/* Console uartlite — {n.origin}. THE console (FT4232 lane 2, the",
        " * tty_02 share on the hub). Stock IRQ-driven; 115200 fixed in HW. */",
        f"uart0: serial@{n.base:x} {{",
        '\tcompatible = "xlnx,xps-uartlite-1.00.a";',
        f"\treg = <{_h(n.base)} {_h(n.size)}>;",
        "\tinterrupt-parent = <&axi_intc>;",
        f"\tinterrupts = <{n.irq} 4>;",
        "\tclocks = <&clk100>;",
        '\tclock-names = "s_axi_aclk";',
        "\tcurrent-speed = <115200>;",
        "\txlnx,data-bits = <8>;",
        "\txlnx,use-parity = <0>;",
        "};",
    ]


def _r_timer(n: Node, _ctx) -> list[str]:
    return [
        f"/* AXI timer — {n.origin}. THE KERNEL TICK (MBV errata E1 Sstc dead,",
        " * E2 STIP stuck): \"soclabs,mbv-timer\" binds the patch-0003 clockevent",
        " * (channel 0 on SEIP through the INTC); patch 0007 routes udelay onto its",
        " * free-running counter because rdtime freezes in a busy-spin.",
        " * clock-frequency is REQUIRED by that driver. Never claim",
        " * \"xlnx,axi-timer-2.0\" (it binds pwm-xilinx). Fixed-IP re-enable: restore",
        " * \"sstc\" on the cpu AND drop the soclabs prefix, in one commit. */",
        f"axi_timer: timer@{n.base:x} {{",
        '\tcompatible = "soclabs,mbv-timer", "xlnx,xps-timer-1.00.a";',
        f"\treg = <{_h(n.base)} {_h(n.size)}>;",
        "\tinterrupt-parent = <&axi_intc>;",
        f"\tinterrupts = <{n.irq} 4>;",
        "\tclocks = <&clk100>;",
        '\tclock-names = "s_axi_aclk";',
        f"\tclock-frequency = <{SHELL_CLK_HZ}>;",
        "\txlnx,count-width = <32>;",
        "\txlnx,one-timer-only = <0>;",
        "};",
    ]


def _r_emc(n: Node, _ctx) -> list[str]:
    return [
        f"/* LAN9220 behind the AXI EMC — {n.origin} (16 MiB decode; the chip",
        " * aliases every 256 B).",
        " * reg-io-width = <4>: the EMC assembles two 16-bit cycles into ONE 32-bit",
        " *   AXI beat, exactly as the bare-metal driver reads it. <2> popped the RX",
        " *   data FIFO twice per word and overran skbs into slab (the July 57 s",
        " *   hrtimer oops). Do not change it.",
        " * ETH_INT POLARITY TRIPLE (SHELL_CONTRACT §4; silicon 2026-07-19) — the",
        " *   three ship together or eth0 up livelocks the kernel:",
        " *   (1) NO smsc,irq-active-high / smsc,irq-push-pull here,",
        " *   (2) PULLTYPE PULLUP on ETH_INT,",
        " *   (3) the INTC sees ~ETH_INT (shell_top, MPS3_SHELL_CPU_MBV).",
        " *   tools/dts_gates.py checks all three. */",
        f"eth0: ethernet@{n.base:x} {{",
        '\tcompatible = "smsc,lan9220", "smsc,lan9115";',
        f"\treg = <{_h(n.base)} 0x10000>;",
        "\tinterrupt-parent = <&axi_intc>;",
        f"\tinterrupts = <{n.irq} 4>;",
        "\treg-io-width = <4>;",
        "\tsmsc,force-internal-phy;",
        f"\tlocal-mac-address = [{ETH_MAC}];",
        "\tvdd33a-supply = <&reg_3v3>;",
        "\tvddvario-supply = <&reg_3v3>;",
        '\tphy-mode = "mii";',
        "};",
    ]


def _r_usd(n: Node, _ctx) -> list[str]:
    return [
        f"/* User microSD — {n.origin}: D13's usd_spi (SPI mode, runtime divider,",
        " * debounced card detect, hardware pad gate). Kernel-owned: the spi-usd",
        " * controller (br2_external package mps3-spi-usd) + the stock mmc_spi slot",
        " * -> /dev/mmcblk0. The controller is also a 1-line GPIO chip whose line 0",
        " * is STATUS.CD_PRESENT, so the MMC core polls one register a second",
        " * instead of clocking CMD0 into an empty socket: no card => no SPI",
        " * traffic, no delay, no error (D13 hard rule 1). 400 kHz identification",
        " * is the MMC core's f_min; 12.5 MHz is D13's default data rate. */",
        f"usd: spi@{n.base:x} {{",
        '\tcompatible = "soclabs,usd-spi-1.0";',
        f"\treg = <{_h(n.base)} {_h(n.size)}>;",
        "\tclocks = <&clk100>;",
        "\t#address-cells = <1>;",
        "\t#size-cells = <0>;",
        "\tgpio-controller;",
        "\t#gpio-cells = <2>;",
        "",
        "\tusd_slot: mmc@0 {",
        '\t\tcompatible = "mmc-spi-slot";',
        "\t\treg = <0>;",
        "\t\tspi-max-frequency = <12500000>;",
        "\t\tvoltage-ranges = <3300 3300>;",
        "\t\t/* mmc-spi-slot's CD is index 0 of the LEGACY \"gpios\" property",
        "\t\t * (mmc_spi.c: mmc_gpiod_request_cd(mmc, NULL, 0, ...)); a cd-gpios",
        "\t\t * here would be ignored and the core would poll CMD0 instead. */",
        "\t\tgpios = <&usd 0 0>;\t/* line 0 = STATUS.CD_PRESENT, active high */",
        "\t};",
        "};",
    ]


RENDER_KERNEL = {"INTC": _r_intc, "UARTLITE": _r_uart, "TIMER": _r_timer,
                 "EMC": _r_emc, "USD": _r_usd}


def render(d: dict) -> str:
    nodes = d["nodes"]
    by = {n.name: n for n in nodes}
    uart = by["UARTLITE"]
    ddr_base, ddr_size = d["ddr"]
    has_usd = by.get("USD") is not None and by["USD"].owner == OWN_KERNEL
    skipped = [n for n in nodes if n.owner == OWN_NONE]
    ramoops = ddr_base + ddr_size - RAMOOPS_BYTES

    L = [
        "/dts-v1/;",
        "",
        "/*",
        f" * shell_linux.dts — GENERATED by tools/{GENERATOR}. DO NOT EDIT BY HAND.",
        " * Regenerate: python3 tools/gen_dts.py     Gate: python3 tools/dts_gates.py",
        " *",
        " * The MPS3 static shell with the MicroBlaze V CPU seam (SHELL_CPU=mbv):",
        f" *   contract blocks  {SHELL_BD} via gen_regmap.derive()",
        f" *   CPU-side cells   {SHELL_BD}" +
        (f" + {d['mbv_source']}" if d["mbv_source"] != SHELL_BD else ""),
        f" *   DDR / LMB        {d['mbv_source']}",
        f" *   owners           {d['owner_source']}",
        " *   gated additions  " + ", ".join(
            f"{g}={'1' if v else '0'}" for g, v in sorted(GATES.items())),
        " *",
        " * OWNERSHIP (LINUX_HARNESS_PLAN §3): a kernel-owned block has its driver",
        " * and no generic-uio; a harnessd-owned block has ONE node whose only",
        " * compatible is generic-uio (HARNESSD_CONTRACT §3). Never both.",
    ]
    if skipped:
        L.append(" * No node (owner none): " + ", ".join(
            f"{n.name} @0x{n.base:08X}" for n in skipped) + ".")
    L += [
        " *",
        " * BOOT MAP (OpenSBI 1.6 fw_payload / fw_jump, rv32): OpenSBI @0x8000_0000,",
        " * Image @0x8040_0000 with the rootfs EMBEDDED (no linux,initrd-* to",
        " * stamp), this DTB @0x8220_0000, padded by dtc -p 4096 for OpenSBI's",
        " * in-place fixups (IMAGE_CONTRACT §1.2).",
        " */",
        "",
        "/ {",
        "\t#address-cells = <1>;",
        "\t#size-cells = <1>;",
        '\tmodel = "SoCLabs MPS3 Linux harness shell (MicroBlaze V)";',
        '\tcompatible = "soclabs,mps3-harness-shell";',
        "",
        "\tchosen {",
        '\t\tstdout-path = "serial0:115200n8";',
        "\t\t/* uio_pdrv_genirq.of_id=generic-uio is LOAD-BEARING: it binds every",
        "\t\t * harnessd-owned node (the driver's of_id table is empty otherwise). */",
        f'\t\tbootargs = "console=ttyUL0,115200 earlycon=uartlite,mmio32,{_h(uart.base)}'
        ' uio_pdrv_genirq.of_id=generic-uio";',
        "\t};",
        "",
        "\taliases {",
        "\t\tserial0 = &uart0;",
        "\t\tethernet0 = &eth0;",
    ]
    if has_usd:
        L.append("\t\tspi0 = &usd;")
    L += [
        "\t};",
        "",
        "\tcpus {",
        "\t\t#address-cells = <1>;",
        "\t\t#size-cells = <0>;",
        f"\t\ttimebase-frequency = <{SHELL_CLK_HZ}>;",
        "",
        "\t\t/* MicroBlaze V, RV32IMAC + Sv32. The BD read-back-asserts",
        "\t\t * C_USE_COUNTERS=1 (rdtime) and C_INTERRUPT_WAKEUP=1 and keeps",
        "\t\t * C_USE_SSTC=1 latent: \"sstc\" is deliberately NOT listed (errata",
        "\t\t * E1/E2, see the timer node). No zicbom, no zbc. */",
        "\t\tcpu0: cpu@0 {",
        '\t\t\tdevice_type = "cpu";',
        '\t\t\tcompatible = "amd,mbv32", "riscv";',
        "\t\t\treg = <0>;",
        '\t\t\tstatus = "okay";',
        f"\t\t\tclock-frequency = <{SHELL_CLK_HZ}>;",
        '\t\t\tmmu-type = "riscv,sv32";',
        '\t\t\triscv,isa-base = "rv32i";',
        "\t\t\triscv,isa-extensions = " + ", ".join(f'"{e}"' for e in ISA_EXTENSIONS) + ";",
        f'\t\t\triscv,isa = "{ISA_STRING}";',
        "",
        "\t\t\tcpu_intc: interrupt-controller {",
        '\t\t\t\tcompatible = "riscv,cpu-intc";',
        "\t\t\t\tinterrupt-controller;",
        "\t\t\t\t#address-cells = <0>;   /* dtc >= 1.6.1 interrupt_provider */",
        "\t\t\t\t#interrupt-cells = <1>;",
        "\t\t\t};",
        "\t\t};",
        "\t};",
        "",
        "\t/* DDR4 only. The LMB (0x0, 128 KiB) is NOT memory to Linux. */",
        f"\tmemory@{ddr_base:x} {{",
        '\t\tdevice_type = "memory";',
        f"\t\treg = <{_h(ddr_base)} {_h(ddr_size)}>;",
        "\t};",
        "",
        "\treserved-memory {",
        "\t\t#address-cells = <1>;",
        "\t\t#size-cells = <1>;",
        "\t\tranges;",
        "",
        "\t\t/* ramoops/pstore, top MiB of DDR: survives a CPU-only warm reset",
        "\t\t * (S15pstore). The JTAG-readable diag mailbox is the LMB tail. */",
        f"\t\tramoops@{ramoops:x} {{",
        '\t\t\tcompatible = "ramoops";',
        f"\t\t\treg = <{_h(ramoops)} {_h(RAMOOPS_BYTES)}>;",
        "\t\t\trecord-size = <0x20000>;",
        "\t\t\tconsole-size = <0x40000>;",
        "\t\t\tpmsg-size = <0x20000>;",
        "\t\t};",
        "\t};",
        "",
        "\t/* shell_clk = clk_wiz_shell/clk_out1: no AXI interface, so a fixed",
        "\t * clock is the honest model. */",
        f"\tclk100: clock-{SHELL_CLK_HZ} {{",
        '\t\tcompatible = "fixed-clock";',
        "\t\t#clock-cells = <0>;",
        f"\t\tclock-frequency = <{SHELL_CLK_HZ}>;",
        '\t\tclock-output-names = "shell_clk";',
        "\t};",
        "",
        "\t/* LAN9220 supplies: hard-wired 3V3 on the MPS3. */",
        "\treg_3v3: regulator-3v3 {",
        '\t\tcompatible = "regulator-fixed";',
        '\t\tregulator-name = "board-3v3";',
        "\t\tregulator-min-microvolt = <3300000>;",
        "\t\tregulator-max-microvolt = <3300000>;",
        "\t\tregulator-always-on;",
        "\t};",
        "",
        "\tsoc {",
        '\t\tcompatible = "simple-bus";',
        "\t\t#address-cells = <1>;",
        "\t\t#size-cells = <1>;",
        "\t\tranges;",
    ]
    for n in nodes:
        if n.owner == OWN_NONE:
            continue
        body = (RENDER_KERNEL[n.name] if n.owner == OWN_KERNEL else _uio)(n, d)
        L.append("")
        L += [("\t\t" + ln) if ln else "" for ln in body]
    L += ["\t};", "};", ""]
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# Verification of DTS TEXT — shared with tools/dts_gates.py, which runs it on
# the COMMITTED file, so a hand edit that slipped past review still fails
# --------------------------------------------------------------------------- #
_OPEN_RE = re.compile(r"^\s*(?:(\w+):\s*)?(/|[\w,.+#-]+)(?:@([0-9a-fA-F]+))?\s*\{\s*$")
_PROP_RE = re.compile(r"^\s*([\w,#.+-]+)\s*(?:=\s*(.*?))?;\s*(?:/\*.*\*/)?\s*$")


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def parse_dts_nodes(text: str) -> list[dict]:
    """Every node with its path, unit address and properties (this file's shape
    only: one statement per line, no /include/, no overlays)."""
    nodes, stack = [], []
    for ln in _strip_comments(text).splitlines():
        if not ln.strip():
            continue
        m = _OPEN_RE.match(ln)
        if m:
            name = m.group(2)
            parent = stack[-1]["path"] if stack else ""
            node = {"label": m.group(1), "name": name,
                    "unit": int(m.group(3), 16) if m.group(3) else None,
                    "props": {}, "depth": len(stack),
                    "path": "" if name == "/" else f"{parent}/{name}"}
            nodes.append(node)
            stack.append(node)
            continue
        if ln.strip().startswith("};"):
            if stack:
                stack.pop()
            continue
        pm = _PROP_RE.match(ln)
        if pm and stack:
            stack[-1]["props"][pm.group(1)] = (pm.group(2) or "").strip()
    return nodes


def compats(v: str | None) -> list[str]:
    return re.findall(r'"([^"]*)"', v or "")


def _reg(v: str | None) -> tuple[int, int] | None:
    m = re.match(r"<\s*(0x[0-9a-fA-F]+|\d+)\s+(0x[0-9a-fA-F]+|\d+)", v or "")
    return (int(m.group(1), 0), int(m.group(2), 0)) if m else None


def verify_dts(text: str, stats: dict | None = None) -> list[str]:
    """The ownership rule, read off DTS text. Returns violations (empty = ok);
    fills ``stats`` with what it actually checked, so a caller can refuse a
    vacuous pass (a parser that finds no nodes finds no violations)."""
    bad = []
    names: dict[str, str] = {}
    spans = []
    nodes = [n for n in parse_dts_nodes(text)
             if n["path"].startswith("/soc/") and n["path"].count("/") == 2]
    if not nodes:
        bad.append("no /soc child nodes parsed — nothing was checked")
    for n in nodes:
        comp = compats(n["props"].get("compatible"))
        reg = _reg(n["props"].get("reg"))
        where = f"{n['name']}@{n['unit']:x}" if n["unit"] is not None else n["name"]
        uio = "generic-uio" in comp
        if uio:
            if comp != ["generic-uio"]:
                bad.append(f"{where}: generic-uio next to {comp} — a harnessd node "
                           f"carries NO compatible a kernel driver could match")
            nm = compats(n["props"].get("linux,uio-name"))
            if not nm:
                bad.append(f"{where}: UIO node without linux,uio-name")
            elif nm[0] in names:
                bad.append(f"{where}: uio-name {nm[0]!r} also on {names[nm[0]]}")
            else:
                names[nm[0]] = where
        elif "linux,uio-name" in n["props"]:
            bad.append(f"{where}: linux,uio-name on a kernel-owned node")
        if reg is None:
            bad.append(f"{where}: no reg")
            continue
        if n["unit"] is not None and reg[0] != n["unit"]:
            bad.append(f"{where}: reg base 0x{reg[0]:x} != unit address")
        spans.append((reg[0], reg[0] + reg[1], where, "uio" if uio else "kernel"))
    spans.sort()
    for a, b in zip(spans, spans[1:]):
        if a[1] > b[0]:
            bad.append(f"{a[2]} ({a[3]}) overlaps {b[2]} ({b[3]}): one page, "
                       f"two owners")
    if stats is not None:
        stats.update(nodes=len(nodes), uio=len(names),
                     kernel=sum(1 for sp in spans if sp[3] == "kernel"))
    return bad


# --------------------------------------------------------------------------- #
def build(repo: Path) -> dict[str, str]:
    text = render(derive(repo))
    bad = verify_dts(text)
    if bad:
        raise GenError("rendered tree breaks the ownership rule:\n  " + "\n  ".join(bad))
    return {OUT: text}


if __name__ == "__main__":
    sys.exit(genlib.run(build, GENERATOR))
