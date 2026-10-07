#!/usr/bin/env python3
"""gen_regmap.py — derive the shell register map once, emit every view of it.

THE DRIFT THIS REMOVES
----------------------
The shell register map was written out by hand FOUR times — ``platform_regs.h``,
``tests/common/regmap.py``, ``docs/contracts/shell-regmap.md`` and (a subset)
``host/socket_harness/registers.py`` / the pyverify client — each citing the
others as its source. The conformance test that policed them covered 7 of the
15 blocks, so the other 8 could say anything. They did:

  * ``platform_regs.h:127`` (pre-generation) — "CLCDKVM — RESERVED, NOT
    INSTANTIATED ... NUM_MI is 15 ... an access to 0x44AD_xxxx today is
    unmapped", while ``fpga/shell/bd/shell_bd.tcl:578`` creates ``clcd_kvm_0``
    and ``:1407`` assigns it ``0x44AD0000``. Firmware guarded a live block
    behind an ``#ifdef`` that said it did not exist.
  * ``docs/contracts/shell-regmap.md`` — same claim in its map table, and its
    v0.6 changelog announced a TOUCH block at ``0x44AE_0000`` that the map table
    never gained a row for.

Both are now impossible to write: the map table IS the BD.

WHAT IS DERIVED, AND FROM WHAT
------------------------------
1. **Block base addresses — from the block design.** Every
   ``assign_bd_address -offset 0x44Ax_xxxx`` in ``fpga/shell/bd/shell_bd.tcl``
   (plus ``bd/touch_iic_add.tcl``, the ``SHELL_TOUCH=1`` gated addition) inside
   the ``0x44A0_0000..0x44B0_0000`` contract window. The base, the range, the
   owning BD cell and whether the block is gated all come from that one line.
   A block that is not in the BD has no base here; a block that is cannot be
   described as absent.

   Alongside them, :data:`RESERVATIONS` names the pages a DESIGNED-but-unbuilt
   block holds. Together they make this file the single authority on who owns
   each 64 KiB page of ``REGION_LO..REGION_HI``: :func:`check_page_allocation`
   refuses to generate if two owners land on one page, so the
   TOUCH-vs-staged-``axi_jtag`` double-claim on ``0x44AE_0000`` — two unbuilt
   drafts, each unable to see the other, one of which would have been silently
   shadowed at the next mint — cannot recur.

2. **Register offsets — from the owning RTL.** The BD segment names its cell;
   the cell's ``create_bd_cell -vlnv soclabs.org:user:<name>:1.0`` names the
   module; the module's ``.sv`` is found under ``fpga/`` and parsed for the
   uniform decode idiom every custom CSR block in this repo shares::

       localparam logic [IDX_W-1:0] IDX_STATUS = 'h3;  // 0x0C (ro)

   The word index is authoritative (offset = index × 4); the trailing comment is
   cross-checked against it and a disagreement is a hard error — that comment is
   what a human reads, so it must not be allowed to lie either.

   For a *wrapper* cell exposing several AXI-Lite surfaces
   (``eth_mac_test_subsystem_0`` carries VPHY and GENCHK), the BD interface name
   is followed into the wrapper RTL — the submodule whose ``.s_axi_awaddr`` is
   bound to ``s_axi_<iface>_awaddr`` is the block — and that submodule is parsed.

3. **Vendor blocks.** HWICAP, DBGBR (Debug Bridge), MMCM_DRP (clk_wiz AXI-Lite
   DRP) and WDOG (AXI Timebase WDT) are Xilinx IP: there is no RTL in this tree
   to parse, so their offsets are the literal PG tables in :data:`VENDOR_REGS`
   below, each with its PG citation. They are marked ``vendor`` in every emitted
   view, so a reader can see which offsets this repo can prove and which it is
   quoting. That distinction did not exist before and is worth more than
   pretending to derive them.

WHAT IS *NOT* DERIVED
---------------------
Bitfield ``#define``s (``CLKRST_STATUS_MMCM_LOCKED``, the CLCDKVM state codes,
the SPI control bits …). Nothing in the BD or in the decode idiom carries bit
semantics, and inventing a parser for the RTL's field logic would be a second
IR — precisely the SystemRDL keystone refactor that is shelved. Those stay
hand-written, OUTSIDE the fences, next to the prose that explains them.

Reservation bases are DECLARED, not derived — there is no BD line to read them
off, which is exactly why they need a table that other files quote rather than
restate. Every place that writes one is listed in the entry's ``pins`` and is
read back by ``tests/firmware_logic/test_regmap_conformance.py``.

The register NAME aliases in :data:`NAME_ALIASES` are also hand-written. They
exist because three sources historically spelled the same register differently
(``UARTBR_U0_TXRX`` in C, ``UARTBR_U0_DATA`` in Python, ``U0_TX``/``U0_RX`` in
the contract). A wrong alias is a compile error or a missing attribute, caught
instantly; a wrong OFFSET is a silent bus access into the wrong register on
silicon. The offsets are derived; the spellings are declared.

OUTPUTS
-------
``firmware/common/platform_regs.h``      fenced table (prose + bitfields stay)
``tests/common/regmap.py``               fenced table (BFM + bitfields stay)
``host/pyverify/pyverify/regmap.py``     wholly generated
``docs/contracts/shell-regmap.md``       fenced map table + fenced reservation
                                        table + fenced offset appendix + fenced
                                        MicroBlaze V (Linux) view
``fpga/shell/generated/regmap_mbv.json`` wholly generated: the SHELL_CPU=mbv
                                        physical map with an OWNER per block
                                        (kernel / harnessd / none), the INTC
                                        inputs and the LMB layout -- what
                                        tools/gen_dts.py (IMAGE lane) renders
                                        the Linux DTS from

THE MICROBLAZE V VIEW (2026-09-23, the CPU seam)
------------------------------------------------
Under Linux every block is owned by EITHER the kernel (a driver) OR harnessd
(the firmware services over UIO), never both (LINUX_HARNESS_PLAN_2026-09-23.md
§3). Which is which is DECLARED in :data:`MBV_OWNERS`, because nothing in the
BD can say it; everything else in that view is DERIVED: the contract-window
blocks as above, the housekeeping peripherals from shell_bd.tcl's
out-of-window assign lines, the LMB and DDR4 windows from
fpga/shell/bd/cpu_mbv.tcl's ``addr`` stage, the INTC input of each interrupt
source from the ``xlconcat_intr/In<n>`` wires, and the ISA string from
cpu_mbv.tcl's core config. A block the BD has and the table does not is a hard
error, and so is a table entry for a block that no longer exists -- so the
kernel-vs-harnessd split cannot silently miss a new block.

    python3 tools/gen_regmap.py [--check|--list|--out-dir DIR]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import genlib  # noqa: E402
from genlib import GenError  # noqa: E402

GENERATOR = "gen_regmap.py"

#: The AXI-Lite window this contract owns. Addresses outside it in the BD are
#: the MicroBlaze's own housekeeping peripherals (timer/uartlite/intc/EMC/LMB)
#: and are deliberately NOT part of the shell register contract.
#:
#: EXTENDED 2026-09-11, 0x44B0_0000 -> 0x44B3_0000, by the DUTEGR block (the DUT
#: Ethernet return path, ``fpga/shell/bd/shell_bd.tcl``'s
#: ``assign_bd_address -offset 0x44B20000``). The window had to grow because the
#: region below was FULL -- see :data:`REGION_HI`. Note the side effect: the
#: MAGICID and UART16550 reservations at 0x44B0/0x44B1 are now INSIDE the
#: contract window as well as inside the page-allocation region. Nothing
#: generated changes for them (a reservation renders from :data:`RESERVATIONS`,
#: never from the window), but if either is ever built its assign_bd_address
#: line will now be parsed here instead of silently skipped as MicroBlaze
#: housekeeping -- which is the behaviour you want.
#:
#: EXTENDED AGAIN 2026-09-14, 0x44B3_0000 -> 0x44B5_0000, by USRACC and WDOG
#: (``fpga/shell/bd/shell_bd.tcl``'s ``assign_bd_address -offset 0x44B30000``
#: and ``-offset 0x44B40000``). Same reason as the DUTEGR extension below: the
#: region was full again the moment DUTEGR took 0x44B2.
WINDOW_LO = 0x44A00000
WINDOW_HI = 0x44B50000

#: The PAGE-ALLOCATION region this map is the single authority for: the contract
#: window above, plus the pages above it that a staged, not-yet-built block has
#: reserved. Every 64 KiB page in here has exactly one owner -- a BD block or a
#: :data:`RESERVATIONS` entry -- and
#: ``tests/firmware_logic/test_regmap_conformance.py`` fails any tracked file
#: that declares a base in here for an owner this table does not know.
#:
#: EXTENDED 2026-09-11, 0x44B2_0000 -> 0x44B3_0000. The region was FULL: pages
#: 0x44A0..0x44AE all had a BD block and 0x44AF/0x44B0/0x44B1 are the staged
#: AXI-JTAG carry's reservations. This file's own rendered message for that
#: state is "a new block must extend REGION_HI, and say so", and DUTEGR --
#: the DUT Ethernet return path, at 0x44B2_0000 -- is the first block to do it.
#: Saying so is the point: extending the region silently is how a page stops
#: having exactly one owner.
#:
#: EXTENDED 2026-09-14, 0x44B3_0000 -> 0x44B5_0000, and SAYING SO as that rule
#: requires. Two blocks land together in the identity/watchdog wave and each
#: takes one page:
#:   0x44B3_0000  USRACC  the fabric's own build identity -- a USR_ACCESSE2
#:                wrapper, so the MicroBlaze can read the AXSS word the
#:                bitstream was stamped with and cross-check it against the
#:                version compiled into its own image (VERSIONING_PLAN.md §3.4,
#:                deferred there since 2026-07-14, built now because a re-key
#:                was happening anyway).
#:   0x44B4_0000  WDOG    xilinx.com:ip:axi_timebase_wdt:3.0 -- the shell
#:                watchdog (SERVICES_PARTITION.md §5). Vendor IP, so its
#:                offsets are quoted from PG128 in VENDOR_REGS, not derived.
REGION_LO = 0x44A00000
REGION_HI = 0x44B50000
PAGE = 0x10000

#: BD-comment spelling -> contract block name. The BD writes the block name in
#: the trailing `;# NAME` comment on each assign_bd_address line; three of them
#: are spelled differently there than in the contract.
BLOCK_ALIASES = {
    "MMCM_DUT_DRP": "MMCM_DRP",
    "CLCD_KVM": "CLCDKVM",
    "TOUCH_IIC": "TOUCH",
}

#: Register spellings per view, where the three sources historically disagreed.
#: key: (block, RTL IDX_ name) -> (c_name, py_name, md_name). Absent = the RTL
#: name is used verbatim in all three. See the module docstring on why this
#: table is safe to hand-maintain and an offset table is not.
NAME_ALIASES = {
    # dut_clkrst.sv calls them CLK_SEL/CLK_DRP; the contract has always said
    # DUT_CLK_SEL/DUT_CLK_DRP (they select the DUT clock, not the shell's).
    ("CLKRST", "CLK_SEL"): ("DUT_CLK_SEL", "DUT_CLK_SEL", "DUT_CLK_SEL"),
    ("CLKRST", "CLK_DRP"): ("DUT_CLK_DRP", "DUT_CLK_DRP", "DUT_CLK_DRP"),
    # One address, two directions: writing pushes a TX byte, reading pops an RX
    # byte. C named it after the window, Python after the payload, the contract
    # after the two directions. All three spellings are load-bearing in code
    # that exists today, so all three are emitted.
    ("UARTBR", "U0_DATA"): ("U0_TXRX", "U0_DATA", "U0_TX"),
    ("UARTBR", "U1_DATA"): ("U1_TXRX", "U1_DATA", "U1_TX"),
}

#: Xilinx IP register offsets, quoted from the product guides. There is no RTL
#: in this tree for these four blocks, so these are the one part of the map that
#: is DECLARED rather than derived — and they are labelled as such everywhere
#: they are emitted. Names carry any sub-prefix the C header uses.
VENDOR_REGS = {
    "HWICAP": ("Xilinx AXI HWICAP (PG134)", [
        ("GIER", 0x1C, "global interrupt enable"),
        ("ISR", 0x20, "interrupt status"),
        ("IER", 0x28, "interrupt enable"),
        ("WF", 0x100, "write FIFO — bitstream words, ICAP order"),
        ("RF", 0x104, "read FIFO"),
        ("SZ", 0x108, "transfer size, in 32-bit words"),
        ("CR", 0x10C, "control: [0] write (FIFO->ICAP), [1] read"),
        ("SR", 0x110, "status: [0] done, [2] EOS"),
        ("WFV", 0x114, "write FIFO vacancy, in words"),
        ("RFO", 0x118, "read FIFO occupancy, in words"),
    ]),
    "DBGBR": ("Xilinx Debug Bridge, AXI->BSCAN (PG245)", [
        ("LENGTH", 0x00, "rw — bits to shift this chunk (1..32)"),
        ("TMS", 0x04, "rw — TMS vector chunk, LSB shifts first"),
        ("TDI", 0x08, "rw — TDI vector chunk, LSB shifts first"),
        ("TDO", 0x0C, "ro — captured TDO chunk"),
        ("CTRL", 0x10, "[0] GO: write 1 to start, self-clears"),
    ]),
    "TOUCH": ("Xilinx AXI IIC (PG090)", [
        ("IIC_GIE", 0x01C, "global interrupt enable"),
        ("IIC_ISR", 0x020, "interrupt status (W1C)"),
        ("IIC_IER", 0x028, "interrupt enable"),
        ("IIC_SOFTR", 0x040, "soft reset — write the reset key 0x0000000A"),
        ("IIC_CR", 0x100, "control"),
        ("IIC_SR", 0x104, "status (ro)"),
        ("IIC_TX_FIFO", 0x108, "transmit FIFO (data + dynamic start/stop)"),
        ("IIC_RX_FIFO", 0x10C, "receive FIFO (ro, destructive pop)"),
        ("IIC_ADR", 0x110, "own slave address (slave mode)"),
        ("IIC_TX_FIFO_OCY", 0x114, "TX FIFO occupancy (ro)"),
        ("IIC_RX_FIFO_OCY", 0x118, "RX FIFO occupancy (ro)"),
        ("IIC_TEN_ADR", 0x11C, "10-bit slave address (unused here)"),
        ("IIC_RX_FIFO_PIRQ", 0x120, "RX FIFO programmable depth IRQ / dyn count"),
        ("IIC_GPO", 0x124, "general-purpose output"),
    ]),
    "WDOG": ("Xilinx AXI Timebase Watchdog Timer (PG128)", [
        ("TWCSR0", 0x00, "control/status 0: [3] WRS reset-status, [2] WDS "
                         "1st-expiry status (W1C — THE KICK), [1] EWDT1 enable"),
        ("TWCSR1", 0x04, "control/status 1: [0] EWDT2 — both enables must be "
                         "set for the watchdog to run"),
        ("TBR", 0x08, "ro: the free-running timebase counter"),
    ]),
    "MMCM_DRP": ("Xilinx Clocking Wizard AXI4-Lite DRP (PG065)", [
        ("SW_RESET", 0x000, "[0] soft reset (self-clearing)"),
        ("STATUS", 0x004, "ro: [0] LOCKED"),
        ("CFG_REG0", 0x200, "[7:0] DIVCLK_DIVIDE, [15:8] CLKFBOUT_MULT, [25:16] frac"),
        ("CFG_REG2", 0x208, "[7:0] CLKOUT0_DIVIDE, [17:8] CLKOUT0_FRAC"),
        ("LOAD", 0x25C, "[0] LOAD, [1] SEN — latch+apply CFG_REG*"),
    ]),
}

#: The one block with no address of its own: swd_bb was REPLACED by jtag_bb in
#: the 0x44A7 page at the A6 SWD->JTAG cutover, but the legacy SWD shells'
#: firmware (swd_server.c, compiled-but-dormant) still needs its offsets. It is
#: emitted as an alias of whichever block actually holds that page, so it can
#: never drift away from it, and is marked legacy in every view.
LEGACY_ALIAS = ("SWDBB", "JTAGBB", "fpga/shell/ip/swd_bb/swd_bb.sv",
                "retired at the A6 SWD->JTAG cutover; shares the JTAGBB page")

#: Pages claimed by a block that is DESIGNED but not in any block design yet.
#:
#: THE DOUBLE-CLAIM THIS TABLE RESOLVES
#: ------------------------------------
#: ``fpga/shell/bd/touch_iic_add.tcl`` assigned the touch AXI IIC
#: ``0x44AE_0000`` and its own comment said that page CLASHED with the staged
#: AXI-JTAG/UART carry, which pinned ``axi_jtag`` at the same base in
#: ``fpga/shell/bd/axijtag_uart_carry.tcl`` and in
#: ``host/socket_harness/carry_across.py``. Two unbuilt drafts, one page: free
#: until both rode one mint, and then a mint.
#:
#: RESOLVED 2026-09-11 in favour of TOUCH, on the contract:
#: ``docs/contracts/shell-regmap.md`` (this contract, v0.6) and
#: ``docs/ARCHITECTURE.md``'s address map both already awarded ``0x44AE_0000``
#: to TOUCH, a firmware driver (``firmware/touch/``) and an XDC are written
#: against it, and ``shell_bd.tcl`` already sources its BD addition under
#: ``SHELL_TOUCH=1``. The carry's claim lived only in planning scaffolding that
#: no build sources. A contract beats a plan, so the carry's three pages moved
#: up one -- contiguous and in the same relative order as the KR260 reference
#: they were copied from.
#:
#: ``pins`` is every tracked place that writes the literal. The conformance test
#: reads them back and fails if one drifts from this table, so these bases stay
#: declared HERE and quoted there, not the other way round.
RESERVATIONS = [
    ("AXIJTAG", 0x44AF0000, "64K",
     "axi_jtag:1.0 AXI->JTAG shifter into the DUT SWJ-DP (staged carry-across; "
     "MUTUALLY EXCLUSIVE with the fielded JTAGBB @0x44A7)",
     [("fpga/shell/bd/axijtag_uart_carry.tcl", "::MPS3_AXIJTAG_BASE"),
      ("host/socket_harness/carry_across.py", "AXIJTAG_BASE")]),
    ("MAGICID", 0x44B00000, "64K",
     "axi_gpio:2.0 all-inputs magic word 0x4A544147 ('JTAG') -- aperture-alive "
     "preflight (staged carry-across)",
     [("fpga/shell/bd/axijtag_uart_carry.tcl", "::MPS3_MAGICID_BASE"),
      ("host/socket_harness/carry_across.py", "MAGIC_BASE")]),
    ("UART16550", 0x44B10000, "64K",
     "axi_uart16550:2.0 ns16550a console, regfile at +0x1000 (staged "
     "carry-across)",
     [("fpga/shell/bd/axijtag_uart_carry.tcl", "::MPS3_UART16550_BASE"),
      ("host/socket_harness/carry_across.py", "UART16550_BASE")]),
]

#: Base-declaration spelling -> contract owner name, for the spellings a
#: consumer legitimately uses that are not the contract name. The conformance
#: test's tree scan resolves through this; anything it cannot resolve is a NEW
#: claimant and fails, which is the point.
DECL_ALIASES = {
    # src/linux_harness/shell_linux_bd.tcl: the PARKED Linux fork (a separate
    # static) still carries the vestigial axi_quad_spi window on this page. In
    # the main map the page is USD's (D13); the fork never loads shell firmware
    # that touches it, so its claim resolves to the page's owner, not to a
    # block that no longer exists.
    "OVL": "USD",
    "DRP": "MMCM_DRP",        # src/linux_harness/shell_linux_bd.tcl
    "MAGIC": "MAGICID",       # host/socket_harness/carry_across.py
    "TOUCH_IIC": "TOUCH",     # fpga/shell/bd/touch_iic_add.tcl's ;# comment
    "KVM": "CLCDKVM",         # tests/csr_decode_width/test_decode_width_clcd_kvm.py
}


# --------------------------------------------------------------------------- #
# Derivation 1 — block bases, from the block design
# --------------------------------------------------------------------------- #
_ASSIGN_RE = re.compile(
    r"^\s*assign_bd_address\s+-offset\s+(0x[0-9A-Fa-f]+)\s+-range\s+(\S+)\s*\\?\s*\n?"
    r"\s*\[get_bd_addr_segs\s*\{([^}]+)\}\]"
    r"[^\n;]*;#\s*(.+?)\s*$",
    re.MULTILINE,
)
_CELL_RE = re.compile(
    r"create_bd_cell\s+-type\s+ip\s+-vlnv\s+[\w.]+:[\w]+:(\w+):[\d.]+\s+(\w+)")


class Block:
    def __init__(self, name, base, rng, cell, iface, gate, bd_file, bd_line):
        self.name = name
        self.base = base
        self.range = rng
        self.cell = cell
        self.iface = iface
        self.gate = gate            # None, or the env flag that enables it
        self.bd_file = bd_file
        self.bd_line = bd_line
        self.source = ""            # human description of where offsets came from
        self.derived = False        # True = parsed from RTL in this tree
        self.regs = []              # [(rtl_name, offset, note)]
        self.legacy_of = None

    @property
    def sort_key(self):
        return (self.base, self.legacy_of is not None)


def _block_name(comment: str) -> str:
    tok = re.split(r"[\s(]", comment.strip())[0]
    tok = tok.upper().replace("-", "_")
    return BLOCK_ALIASES.get(tok, tok)


def parse_bd_addresses(repo: Path) -> list[Block]:
    """Every AXI-Lite slave the BD puts inside the contract window."""
    sources = [
        (Path("fpga/shell/bd/shell_bd.tcl"), None),
        (Path("fpga/shell/bd/touch_iic_add.tcl"), "SHELL_TOUCH"),
    ]
    cells: dict[str, str] = {}
    blocks: list[Block] = []
    for rel, gate in sources:
        text = genlib.read(repo, str(rel))
        for m in _CELL_RE.finditer(text):
            cells.setdefault(m.group(2), m.group(1))
        for m in _ASSIGN_RE.finditer(text):
            base = int(m.group(1), 16)
            if not (WINDOW_LO <= base < WINDOW_HI):
                continue
            seg = m.group(3).split("/")
            line = text[:m.start()].count("\n") + 1
            blocks.append(Block(_block_name(m.group(4)), base, m.group(2),
                                seg[0], seg[1] if len(seg) > 1 else "",
                                gate, str(rel), line))
    if not blocks:
        raise GenError("no assign_bd_address lines matched inside the "
                       f"0x{WINDOW_LO:08X}..0x{WINDOW_HI:08X} window — the BD "
                       "moved or this parser broke")
    dup = {}
    for b in blocks:
        dup.setdefault(b.base, []).append(b.name)
    for base, names in sorted(dup.items()):
        if len(names) > 1:
            raise GenError(f"two blocks assigned 0x{base:08X}: {names}")
    for b in blocks:
        b.cell_module = cells.get(b.cell)
    return blocks


# --------------------------------------------------------------------------- #
# Derivation 2 — register offsets, from the owning RTL
# --------------------------------------------------------------------------- #
_IDX_RE = re.compile(
    r"^\s*localparam\s+logic\s*\[IDX_W-1:0\]\s+IDX_(\w+)\s*=\s*'h([0-9A-Fa-f]+)\s*;"
    r"\s*(?://\s*(.*?))?\s*$",
    re.MULTILINE,
)
_OFF_IN_COMMENT = re.compile(r"0x([0-9A-Fa-f]{2,3})")


def _is_build_copy(p: Path) -> bool:
    """True for a path inside any Vivado build tree or its scratch.

    A build dir (``build/``, ``build_mint_2026_09/``, ``build_results_*/`` …)
    holds COPIES of the shell IP under ``ipshared/<hash>/src/*.sv``, and
    ``.Xil/`` is Vivado's scratch. Those copies parse identically to the
    canonical ``fpga/shell/ip/**`` / ``fpga/ethernet/**`` sources, so deriving
    from one changes nothing but the source-path comment in every emitted view
    -- which was enough to fail the freshness gate in any tree that had built a
    mint (2026-09-16). The old test, ``"build" not in p.parts``, matched only a
    directory named exactly ``build``.
    """
    return any(part.startswith("build") or part == ".Xil" for part in p.parts)


def find_rtl(repo: Path, module: str) -> Path | None:
    hits = sorted(p for p in (repo / "fpga").rglob(f"{module}.sv")
                  if not _is_build_copy(p.relative_to(repo)))
    return hits[0] if hits else None


def parse_idx_decode(path: Path) -> list[tuple[str, int, str]]:
    """The uniform CSR decode idiom -> [(name, byte offset, note)].

    The word index is authoritative. The trailing ``// 0xNN`` comment is
    cross-checked against it: that comment is what a human reads off the RTL, so
    a disagreement between the two is a real defect and fails here rather than
    being quietly preferred one way or the other.
    """
    out = []
    for m in _IDX_RE.finditer(path.read_text()):
        name, idx, note = m.group(1), int(m.group(2), 16), (m.group(3) or "").strip()
        off = idx * 4
        c = _OFF_IN_COMMENT.search(note)
        if c and int(c.group(1), 16) != off:
            raise GenError(
                f"{path}: IDX_{name} = 'h{idx:x} is byte offset 0x{off:02X}, "
                f"but its comment says 0x{c.group(1).upper()}")
        note = _OFF_IN_COMMENT.sub("", note, count=1).strip(" -\t")
        out.append((name, off, note))
    return out


def resolve_wrapper_submodule(text: str, iface: str) -> str | None:
    """Which submodule of a multi-surface wrapper owns BD interface ``iface``?

    ``eth_mac_test_subsystem`` exposes ``s_axi_vphy`` and ``s_axi_genchk``; the
    submodule bound as ``.s_axi_awaddr (s_axi_<iface>_awaddr)`` is the one.
    """
    for m in re.finditer(r"\b(\w+)\s*(?:#\s*\(.*?\))?\s*\w*\s*\(", text, re.S):
        pass  # (instantiation parsing is done positionally below)
    for m in re.finditer(r"\.s_axi_awaddr\s*\(\s*%s_awaddr\s*\)" % re.escape(iface),
                         text):
        head = text[:m.start()]
        inst = None
        for im in re.finditer(r"^\s*(\w+)\s*#?\s*\(", head, re.MULTILINE):
            inst = im.group(1)
        if inst:
            return inst
    return None


def attach_registers(repo: Path, blocks: list[Block]) -> None:
    for b in blocks:
        if b.name in VENDOR_REGS:
            pg, regs = VENDOR_REGS[b.name]
            b.source, b.derived = pg, False
            b.regs = [(n, o, note) for n, o, note in regs]
            continue
        if not b.cell_module:
            raise GenError(f"{b.name}: BD cell '{b.cell}' has no create_bd_cell "
                           f"-vlnv line and {b.name} is not in VENDOR_REGS")
        rtl = find_rtl(repo, b.cell_module)
        if rtl is None:
            raise GenError(f"{b.name}: no fpga/**/{b.cell_module}.sv for BD cell "
                           f"'{b.cell}', and {b.name} is not in VENDOR_REGS")
        regs = parse_idx_decode(rtl)
        if not regs:
            sub = resolve_wrapper_submodule(rtl.read_text(), b.iface)
            if not sub:
                raise GenError(
                    f"{b.name}: {rtl.relative_to(repo)} has no IDX_ decode and no "
                    f"submodule bound to BD interface '{b.iface}'")
            subrtl = find_rtl(repo, sub)
            if subrtl is None:
                raise GenError(f"{b.name}: wrapper submodule '{sub}' has no RTL")
            regs = parse_idx_decode(subrtl)
            if not regs:
                raise GenError(f"{b.name}: {subrtl.relative_to(repo)} has no "
                               "IDX_ decode either")
            rtl = subrtl
        b.regs = regs
        b.derived = True
        b.source = str(rtl.relative_to(repo))


def add_legacy_alias(blocks: list[Block]) -> None:
    name, of, rtl, why = LEGACY_ALIAS
    host = next((b for b in blocks if b.name == of), None)
    if host is None:
        raise GenError(f"legacy alias {name} points at {of}, which the BD does "
                       "not assign — the page moved")
    alias = Block(name, host.base, host.range, host.cell, host.iface,
                  host.gate, host.bd_file, host.bd_line)
    alias.legacy_of, alias.source, alias.derived = of, rtl, True
    alias.regs = parse_idx_decode(Path(rtl) if Path(rtl).is_absolute()
                                  else genlib.ROOT / rtl)
    alias.why = why
    blocks.append(alias)


def check_page_allocation(blocks: list[Block]) -> None:
    """ONE owner per 64 KiB page, across the BD *and* the staged reservations.

    :func:`parse_bd_addresses` already refuses two BD blocks at one base. This is
    the other half, and it is the half that was missing: the touch AXI IIC and
    the staged ``axi_jtag`` both claimed ``0x44AE_0000`` from different files,
    neither of which could see the other. A collision here is a hard error at
    generation time -- i.e. in ``check_generated_fresh.py``, in both ``make
    check`` and ``make check-ci`` -- so it can never reach a mint.
    """
    owner: dict[int, str] = {}
    for b in blocks:
        if b.legacy_of:                     # SWDBB deliberately shares JTAGBB's page
            continue
        if not (REGION_LO <= b.base < REGION_HI):
            raise GenError(
                f"{b.name} @ 0x{b.base:08X} is outside the page-allocation "
                f"region 0x{REGION_LO:08X}..0x{REGION_HI:08X}; the map cannot "
                f"be the authority for a page it does not cover")
        owner[b.base] = f"{b.name} (BD: {b.bd_file}:{b.bd_line})"
    for name, base, _rng, why, pins in RESERVATIONS:
        if base % PAGE:
            raise GenError(f"reservation {name} @ 0x{base:08X} is not "
                           f"64 KiB-page aligned")
        if not (REGION_LO <= base < REGION_HI):
            raise GenError(
                f"reservation {name} @ 0x{base:08X} is outside the "
                f"page-allocation region 0x{REGION_LO:08X}..0x{REGION_HI:08X}")
        if base in owner:
            raise GenError(
                f"page 0x{base:08X} is claimed TWICE: {owner[base]} and the "
                f"{name} reservation ({why}). One page, one owner -- move one "
                f"of them in tools/gen_regmap.py's RESERVATIONS (and in every "
                f"file it lists under `pins`) before this can generate.")
        owner[base] = f"{name} (RESERVED)"


def derive(repo: Path) -> list[Block]:
    blocks = parse_bd_addresses(repo)
    attach_registers(repo, blocks)
    add_legacy_alias(blocks)
    check_page_allocation(blocks)
    return sorted(blocks, key=lambda b: b.sort_key)


def names_for(block: str, rtl_name: str) -> tuple[str, str, str]:
    return NAME_ALIASES.get((block, rtl_name), (rtl_name, rtl_name, rtl_name))


def _origin(b: Block) -> str:
    if b.legacy_of:
        return f"{b.source} (legacy, {b.why})"
    return f"{b.source}" + ("" if b.derived else " [vendor doc, not derived]")


def _gate_note(b: Block) -> str:
    return f" — GATED: built only with {b.gate}=1" if b.gate else ""


def _free_pages(blocks: list[Block]) -> list[int]:
    """Pages in the region that nobody -- BD or reservation -- has claimed."""
    taken = {b.base for b in blocks if not b.legacy_of}
    taken |= {base for _n, base, _r, _w, _p in RESERVATIONS}
    return [a for a in range(REGION_LO, REGION_HI, PAGE) if a not in taken]


def _reserved_lines(blocks: list[Block]) -> list[str]:
    """The reservation note, as plain text lines (no comment syntax)."""
    free = _free_pages(blocks)
    L = [
        "Pages RESERVED by a block that is DESIGNED but in no block design yet.",
        "No base is emitted for them -- nothing can drive a slave that is not in",
        "the fabric, and a base for one is how a page gets claimed twice. They are",
        "listed so the NEXT block takes a page nobody holds. The authority is",
        "tools/gen_regmap.py's RESERVATIONS; every file that writes one of these",
        "literals is listed there and checked against it by",
        "tests/firmware_logic/test_regmap_conformance.py.",
    ]
    for name, base, rng, why, _pins in RESERVATIONS:
        L.append("")
        L.append(f"  0x{base:08X} ({rng})  {name}")
        L.append(f"      {why}")
    L.append("")
    L.append(f"Region 0x{REGION_LO:08X}..0x{REGION_HI:08X}: "
             + ("FULL -- a new block must extend REGION_HI, and say so."
                if not free else
                "free pages "
                + ", ".join(f"0x{a:08X}" for a in free) + "."))
    return L


def _reserved_pages_note(style: str, blocks: list[Block]) -> list[str]:
    body = _reserved_lines(blocks)
    if style == "c":
        return [""] + ["/* " + body[0]] + [" * " + ln if ln else " *"
                                           for ln in body[1:]] + [" */"]
    return [""] + [("# " + ln).rstrip() for ln in body]


# --------------------------------------------------------------------------- #
# The MicroBlaze V (Linux) view -- SHELL_CPU=mbv
# --------------------------------------------------------------------------- #
MBV_CPU_TCL = "fpga/shell/bd/cpu_mbv.tcl"
MBV_JSON = "fpga/shell/generated/regmap_mbv.json"

#: Who owns each block under Linux: "kernel" (a kernel driver binds it; no UIO
#: node), "harnessd" (a generic-uio node; mps3-harnessd maps it by physical
#: base), or "none" (in the fabric, used by nobody). The second field is the
#: consumer, for a human and for IMAGE's DTS generator. DECLARED -- it is a
#: decision, not a fact the BD holds -- and checked both ways against the map.
MBV_OWNERS = {
    # the kernel's (LINUX_HARNESS_PLAN §3: console, intc, clocksource, eth0,
    # DDR, and the uSD -- D13's usd_spi_0 at 0x44A4, SHELL_CONTRACT L-3)
    "UARTLITE": ("kernel", "console ttyUL0 -- xlnx,xps-uartlite-1.00.a"),
    "INTC":     ("kernel", "root irqchip -- xlnx,xps-intc-1.00.a, kind-of-intr 0x0"),
    "TIMER":    ("kernel", "clockevent -- soclabs,mbv-timer (errata E1/E2) + xlnx,xps-timer-1.00.a"),
    "LAN9220":  ("kernel", "eth0 -- smsc,lan9220 (native active-low IRQ, inverted in shell_top)"),
    "USD":      ("kernel", "uSD -- spi-usd controller -> mmc_spi -> mmcblk0 (D13's usd_spi_0 at 0x44A4)"),
    # harnessd's: every soclabs CSR block and every vendor block the firmware
    # services drive (swap FSM, XVC, JTAG, clocks, CLCD/KVM, touch, WDOG...)
    "CLKRST":   ("harnessd", "clkrst service"),
    "DFXCTL":   ("harnessd", "swap_fsm (decoupler clamp, RM verify)"),
    "HWICAP":   ("harnessd", "swap_fsm / config_agent (ICAP writes)"),
    "VPHY":     ("harnessd", "coordinator link/macgen"),
    "TELEM":    ("harnessd", "coordinator; STATUS[0] = DDR4 calib in mbv (stage0 reads it first)"),
    "GENCHK":   ("harnessd", "coordinator macgen"),
    "JTAGBB":   ("harnessd", "jtag_server :6921 / xvc_server"),
    "DBGBR":    ("harnessd", "xvc_server :2542 (debug_bridge)"),
    "UARTBR":   ("harnessd", "uart_over_eth :6930-6932"),
    "GPIO":     ("harnessd", "heartbeat LED, switches"),
    "MMCM_DRP": ("harnessd", "clkrst set_clk (DUT MMCM DRP)"),
    "CLCD":     ("harnessd", "clcd status/apps pages"),
    "CLCDKVM":  ("harnessd", "clcd_kvm"),
    "TOUCH":    ("harnessd", "touch (polls the STMPE811 over I2C; never the INTC)"),
    "DUTEGR":   ("harnessd", "coordinator dutrx"),
    "USRACC":   ("harnessd", "coordinator version (usr_access)"),
    "WDOG":     ("harnessd", "kicked from the harnessd service loop"),
    # in the fabric, used by nobody: none today. (OVLSTORE -- the pad-less
    # axi_quad_spi_0 -- was the one "none" block until D13 put usd_spi_0 on its
    # page; the "none" owner stays legal for the next such block.)
}
#: Owners declared ahead of their block (allowed to be absent from the BD).
#: Empty since D13 landed usd_spi_0 (USD moved up into MBV_OWNERS).
MBV_PENDING: dict = {}
#: The CPU's own peripherals outside the contract window: BD cell -> name.
HOUSEKEEPING = {
    "axi_uartlite_0": "UARTLITE",
    "axi_intc_0": "INTC",
    "axi_timer_0": "TIMER",
    "axi_emc_0": "LAN9220",
}
#: xlconcat inputs driven by a BD PORT rather than a cell pin: port -> block.
IRQ_PORTS = {"eth_irq": "LAN9220", "clcd_tint_i": "TOUCH"}

_ANY_ASSIGN_RE = re.compile(
    r"^\s*assign_bd_address\s+-offset\s+(0x[0-9A-Fa-f]+)\s+-range\s+(\S+)\s*\\?\s*\n?"
    r"\s*\[get_bd_addr_segs\s*\{([^}]+)\}\]", re.MULTILINE)
_IRQ_RE = re.compile(
    r"connect_bd_net\s+\[get_bd_(pins|ports)\s+([\w/$]+)\]\s+"
    r"\[get_bd_pins\s+\$xlconcat_intr/In(\d+)\]")


def _range_bytes(r: str) -> int:
    m = re.fullmatch(r"(\d+)([KMG]?)", r)
    if not m:
        raise GenError(f"unparseable -range {r!r}")
    return int(m.group(1)) * {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30}[m.group(2)]


def _stage_body(text: str, stage: str, rel: str) -> str:
    m = re.search(r"^%s \{\n(.*?)^\}" % re.escape(stage), text, re.S | re.M)
    if not m:
        raise GenError(f"{rel}: no `{stage}` stage")
    return m.group(1)


def derive_mbv(repo: Path, blocks: list[Block]) -> dict:
    """The SHELL_CPU=mbv physical map, as data (see MBV_OWNERS)."""
    bd = genlib.read(repo, "fpga/shell/bd/shell_bd.tcl")
    touch = genlib.read(repo, "fpga/shell/bd/touch_iic_add.tcl")
    cpu = genlib.read(repo, MBV_CPU_TCL)

    # -- blocks: the contract window (already derived) + housekeeping --------
    out = []
    for b in blocks:
        if b.legacy_of:
            continue
        out.append({"name": b.name, "base": b.base, "size": _range_bytes(b.range),
                    "cell": b.cell, "gate": b.gate,
                    "regs_from": "rtl" if b.derived else "vendor-pg"})
    for m in _ANY_ASSIGN_RE.finditer(bd):
        base = int(m.group(1), 16)
        cell = m.group(3).split("/")[0]
        if WINDOW_LO <= base < WINDOW_HI or cell not in HOUSEKEEPING:
            continue
        out.append({"name": HOUSEKEEPING[cell], "base": base,
                    "size": _range_bytes(m.group(2)), "cell": cell, "gate": None,
                    "regs_from": "vendor-pg"})
    missing_hk = set(HOUSEKEEPING.values()) - {b["name"] for b in out}
    if missing_hk:
        raise GenError(f"shell_bd.tcl no longer assigns {sorted(missing_hk)}")

    # -- ownership, checked BOTH ways ----------------------------------------
    names = {b["name"] for b in out}
    unowned = sorted(names - set(MBV_OWNERS))
    if unowned:
        raise GenError(f"MBV view: block(s) {unowned} have no owner in MBV_OWNERS -- "
                       "decide kernel or harnessd (LINUX_HARNESS_PLAN §3) before this "
                       "can generate; a block with no owner gets no DTS node at all")
    stale = sorted(set(MBV_OWNERS) - names)
    if stale:
        raise GenError(f"MBV view: MBV_OWNERS names {stale}, which the BD no longer "
                       "has -- move it to MBV_PENDING or delete it")
    for b in out:
        b["owner"], b["consumer"] = MBV_OWNERS[b["name"]]
        b["kind"] = "slave"

    # -- interrupts: xlconcat In<n> == INTC input == DTS interrupt number ---
    by_cell = {b["cell"]: b["name"] for b in out}
    irqs = []
    for src, gate in ((bd, None), (touch, "SHELL_TOUCH")):
        for m in _IRQ_RE.finditer(src):
            kind, obj, n = m.group(1), m.group(2), int(m.group(3))
            if kind == "ports":
                blk = IRQ_PORTS.get(obj)
                source = f"port {obj}"
            else:
                cell = obj.lstrip("$").split("/")[0]
                blk = by_cell.get(cell)
                source = obj.lstrip("$")
            if blk is None:
                raise GenError(f"xlconcat_intr/In{n} source {obj!r} maps to no block")
            irqs.append({"in": n, "source": source, "block": blk, "gate": gate,
                         "owner": MBV_OWNERS[blk][0]})
    irqs.sort(key=lambda i: i["in"])
    if [i["in"] for i in irqs] != list(range(len(irqs))):
        raise GenError(f"xlconcat inputs are not dense 0..n: {[i['in'] for i in irqs]}")
    for i in irqs:
        for b in out:
            if b["name"] == i["block"]:
                b["irq"] = i["in"]
    for b in out:
        b.setdefault("irq", None)

    # -- memories: cpu_mbv.tcl's `addr` stage --------------------------------
    addr = _stage_body(cpu, "addr", MBV_CPU_TCL)
    mem = {}
    for m in _ANY_ASSIGN_RE.finditer(addr):
        seg = m.group(3)
        mem[seg] = (int(m.group(1), 16), _range_bytes(m.group(2)))
    lmb = {v for k, v in mem.items() if k.split("/")[0] in ("ilmb_bram_if_cntlr", "dlmb_bram_if_cntlr")}
    ddr = [v for k, v in mem.items() if k.startswith("ddr4_0/")]
    if len(lmb) != 1 or len(ddr) != 1:
        raise GenError(f"{MBV_CPU_TCL} `addr`: expected ONE LMB window (both LMBs "
                       f"identical) and ONE DDR4 window, got LMB={lmb} DDR={ddr}")
    (lmb_base, lmb_size), (ddr_base, ddr_size) = lmb.pop(), ddr[0]
    memories = [
        {"name": "LMB", "base": lmb_base, "size": lmb_size, "owner": "stage0+harnessd",
         "cached": False, "note": "the MBV's own ILMB/DLMB (TDP BRAM local_ram); reset vector"},
        {"name": "DDR4", "base": ddr_base, "size": ddr_size, "owner": "kernel",
         "cached": True, "note": "MIG ddr4_0 via smartconnect_ddr; == the I/D-cache aperture"},
    ]
    # harnessd's UIO window onto the LMB: the last 4 KiB page (UIO maps whole
    # pages), which holds the stage0 status block (read-only to harnessd) and
    # the diag mailbox. Not a BD segment -- a WINDOW into the LMB the CPU
    # decodes itself (SHELL_CONTRACT §3) -- so it is kind "window", and the
    # Vivado cross-check (tests/shell_cpu_seam seam_dump.py regmap) skips it.
    out.append({"name": "LMB_TAIL", "base": lmb_base + lmb_size - 0x1000, "size": 0x1000,
                "cell": "local_ram", "gate": None, "regs_from": "lmb_map",
                "owner": "harnessd", "irq": None, "kind": "window",
                "consumer": "diag mailbox v8 at +0xF00; stage0 status block at +0xE00 "
                            "(harnessd reads it, never writes)"})
    # plan §3 layout, anchored to the END of the LMB like diag v8's formula
    lmb_map = [
        {"name": "stage0", "base": lmb_base, "size": lmb_size - 0x200, "owner": "STAGE0"},
        {"name": "stage0_status", "base": lmb_base + lmb_size - 0x200, "size": 0x100, "owner": "STAGE0"},
        {"name": "diag_mailbox", "base": lmb_base + lmb_size - 0x100, "size": 0x100, "owner": "HARNESSD"},
    ]

    # -- the CPU, from cpu_mbv.tcl's core config ------------------------------
    core = _stage_body(cpu, "core", MBV_CPU_TCL)
    cfg = dict(re.findall(r"CONFIG\.(C_\w+)\s+\{([^}]*)\}", core))
    for k in ("C_USE_MULDIV", "C_USE_ATOMIC", "C_USE_COMPRESSION", "C_USE_BITMAN_A",
              "C_USE_BITMAN_B", "C_USE_BITMAN_S", "C_USE_MMU", "C_BASE_VECTORS",
              "C_ICACHE_BASEADDR", "C_ICACHE_HIGHADDR", "C_USE_SSTC"):
        if k not in cfg:
            raise GenError(f"{MBV_CPU_TCL}: core config has no {k}")
    isa = "rv32i" + "".join(x for k, x in (("C_USE_MULDIV", "m"), ("C_USE_ATOMIC", "a"),
                                           ("C_USE_COMPRESSION", "c")) if cfg[k] == "1")
    isa += "".join(x for k, x in (("C_USE_BITMAN_A", "_zba"), ("C_USE_BITMAN_B", "_zbb"),
                                  ("C_USE_BITMAN_S", "_zbs")) if cfg[k] == "1")
    fm = re.search(r"CONFIG\.CLKOUT1_REQUESTED_OUT_FREQ\s+\{([\d.]+)\}", bd)
    if not fm:
        raise GenError("shell_bd.tcl: clk_wiz_shell CLKOUT1_REQUESTED_OUT_FREQ not found")
    if int(cfg["C_ICACHE_BASEADDR"], 16) != ddr_base or \
            int(cfg["C_ICACHE_HIGHADDR"], 16) != ddr_base + ddr_size - 1:
        raise GenError(f"{MBV_CPU_TCL}: cache aperture != the DDR4 window")
    cpu_info = {
        "cell": "microblaze_riscv_0",
        "updatemem_proc": "u_shell/shell_bd_i/microblaze_riscv_0",
        "isa": isa,
        "isa_note": "no zicbom (no such IP parameter); do NOT advertise sstc (errata E1/E2)",
        "mmu": "sv32" if cfg["C_USE_MMU"] == "3" else f"C_USE_MMU={cfg['C_USE_MMU']}",
        "clock_hz": int(round(float(fm.group(1)) * 1e6)),
        "reset_vector": int(cfg["C_BASE_VECTORS"], 16),
        "cache_aperture": [ddr_base, ddr_base + ddr_size - 1],
    }

    ddr_calib = {"block": "TELEM", "enable": "CTRL (+0x00) bit 1 = 1",
                 "read": "STATUS (+0x10) bit 0", "source": "ddr4_0/c0_init_calib_complete"}
    out.sort(key=lambda b: b["base"])
    return {"cpu": cpu_info, "memories": memories, "lmb_map": lmb_map,
            "blocks": out, "irqs": irqs, "ddr_calib": ddr_calib,
            "pending_owners": {k: {"owner": o, "consumer": c}
                               for k, (o, c) in sorted(MBV_PENDING.items())}}


def emit_mbv_json(v: dict) -> str:
    import json

    def hx(n):
        return f"0x{n:08X}"
    doc = {
        "_generated": f"{GENERATOR} -- DO NOT EDIT. Derived from fpga/shell/bd/shell_bd.tcl, "
                      f"{MBV_CPU_TCL}, fpga/shell/bd/touch_iic_add.tcl and the CSR RTL; "
                      "owners from gen_regmap.py MBV_OWNERS. Contract: "
                      "docs/planning/linux_lanes/SHELL_CONTRACT.md",
        "variant": "SHELL_CPU=mbv",
        "cpu": {**v["cpu"], "reset_vector": hx(v["cpu"]["reset_vector"]),
                "cache_aperture": [hx(a) for a in v["cpu"]["cache_aperture"]]},
        "memories": [{**m, "base": hx(m["base"]), "size": hx(m["size"])} for m in v["memories"]],
        "lmb_map": [{**m, "base": hx(m["base"]), "size": hx(m["size"])} for m in v["lmb_map"]],
        "blocks": [{**b, "base": hx(b["base"]), "size": hx(b["size"])} for b in v["blocks"]],
        "irqs": v["irqs"],
        "ddr_calib": v["ddr_calib"],
        "pending_owners": v["pending_owners"],
    }
    return json.dumps(doc, indent=2, sort_keys=False) + "\n"


def emit_md_mbv(v: dict) -> str:
    L = ["| Base | Size | Block | BD cell | Owner under Linux | INTC In | Consumer |",
         "|---|---|---|---|---|---|---|"]
    rows = [(m["base"], m["size"], m["name"], "(memory)", m["owner"], None, m["note"])
            for m in v["memories"]]
    rows += [(b["base"], b["size"], b["name"], b["cell"], b["owner"], b["irq"],
              b["consumer"] + (f" -- GATED: {b['gate']}=1" if b["gate"] else ""))
             for b in v["blocks"]]
    for base, size, name, cell, owner, irq, cons in sorted(rows):
        L.append(f"| 0x{base:08X} | 0x{size:X} | {name} | `{cell}` | **{owner}** | "
                 f"{'' if irq is None else irq} | {cons} |")
    L.append("")
    lm = ", ".join(f"{m['name']} 0x{m['base']:05X}+0x{m['size']:X} ({m['owner']})"
                   for m in v["lmb_map"])
    c = v["cpu"]
    L.append(f"CPU `{c['cell']}`: {c['isa']}, {c['mmu']}, {c['clock_hz']} Hz, reset "
             f"vector 0x{c['reset_vector']:08X}. LMB layout: {lm}. DDR4 calibration: "
             f"{v['ddr_calib']['block']} {v['ddr_calib']['enable']}, then "
             f"{v['ddr_calib']['read']}. Machine-readable: `{MBV_JSON}`.")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# View 1 — firmware/common/platform_regs.h
# --------------------------------------------------------------------------- #
def emit_c(blocks: list[Block]) -> tuple[str, str]:
    bases, offs = [], []
    bases.append("/* Block base addresses. Each is the -offset of an "
                 "assign_bd_address line")
    bases.append(" * in the shell block design; nothing here can claim a block "
                 "the BD does not")
    bases.append(" * instantiate, or omit one it does. */")
    for b in blocks:
        if b.legacy_of:
            c = f"#define MPS3_{b.name}_BASE"
            bases.append(f"/* {b.name}: {b.why}. Same page as {b.legacy_of}. */")
            bases.append(f"{c:<28}0x{b.base:08X}u  /* {b.source} */")
            continue
        c = f"#define MPS3_{b.name}_BASE"
        bases.append(f"{c:<28}0x{b.base:08X}u  /* {b.range}, {b.cell} @ "
                     f"{b.bd_file}:{b.bd_line}{_gate_note(b)} */")
    bases += _reserved_pages_note("c", blocks)
    offs.append("/* Register offsets, one block per stanza. Bit-field defines "
                "for these are")
    offs.append(" * NOT generated -- they live outside the fences, beside the "
                "prose that")
    offs.append(" * explains them. */")

    for b in blocks:
        offs.append("")
        offs.append(f"/* ---- {b.name} @ 0x{b.base:08X} — {_origin(b)}"
                    f"{_gate_note(b)} ---- */")
        for rtl_name, off, note in b.regs:
            cname, _, _ = names_for(b.name, rtl_name)
            sym = f"#define {b.name}_{cname}"
            tail = f"  /* {note} */" if note else ""
            offs.append(f"{sym:<32}0x{off:02X}u{tail}")
    return "\n".join(bases), "\n".join(offs)


# --------------------------------------------------------------------------- #
# View 2/3 — the two Python tables
# --------------------------------------------------------------------------- #
def emit_py_constants(blocks: list[Block]) -> str:
    out = []
    for b in blocks:
        out.append("")
        gate = f"  # GATED: {b.gate}=1 builds only" if b.gate else ""
        legacy = f"  # legacy: {b.why}" if b.legacy_of else ""
        out.append(f"# {b.name} @ 0x{b.base:08X} ({b.range}) — {_origin(b)}")
        out.append(f"{b.name}_BASE = 0x{b.base:08X}{gate}{legacy}")
        for rtl_name, off, note in b.regs:
            _, pyname, _ = names_for(b.name, rtl_name)
            sym = f"{b.name}_{pyname}"
            tail = f"  # {note}" if note else ""
            out.append(f"{sym} = 0x{off:02X}{tail}")
    out += _reserved_pages_note("py", blocks)
    return "\n".join(out)


def emit_pyverify(blocks: list[Block]) -> str:
    """host/pyverify/pyverify/regmap.py — wholly generated, machine-readable.

    Unlike the other three views this one is a DATA structure, not a flat pile
    of constants: the conformance test and any future host tool walk BLOCKS
    rather than re-deriving anything.
    """
    L = [
        '"""pyverify.regmap — the shell register map, as data.',
        "",
        "GENERATED by tools/gen_regmap.py — DO NOT EDIT. Every value here is",
        "derived from fpga/shell/bd/shell_bd.tcl (block bases) and the owning CSR",
        "RTL under fpga/ (register offsets); the four Xilinx blocks are quoted",
        "from their product guides and carry ``derived = False``.",
        "",
        "Re-run::",
        "",
        "    python3 tools/gen_regmap.py",
        "",
        "and see scripts/harness_gates/check_generated_fresh.py, which fails CI",
        "if this file and its siblings do not match a fresh generation.",
        '"""',
        "from __future__ import annotations",
        "",
        "from typing import NamedTuple",
        "",
        "",
        "class Register(NamedTuple):",
        '    """One register: byte offset from its block base."""',
        "",
        "    name: str",
        "    offset: int",
        "    note: str",
        "",
        "",
        "class Reservation(NamedTuple):",
        '    """A 64 KiB page held for a designed-but-unbuilt block."""',
        "",
        "    name: str",
        "    base: int",
        "    range: str",
        "    why: str",
        "    pins: tuple[tuple[str, str], ...]   # (tracked file, symbol) pairs",
        "",
        "",
        "class Block(NamedTuple):",
        '    """One AXI4-Lite slave page in the 0x44A0_0000 contract window."""',
        "",
        "    name: str",
        "    base: int",
        "    range: str",
        "    cell: str",
        "    source: str",
        "    derived: bool          # True = parsed from RTL in this tree",
        "    gate: str | None       # env flag the BD needs to instantiate it",
        "    legacy_of: str | None  # shares another block's page",
        "    registers: tuple[Register, ...]",
        "",
        "",
        "BLOCKS: tuple[Block, ...] = (",
    ]
    for b in blocks:
        L.append("    Block(")
        L.append(f"        name={b.name!r},")
        L.append(f"        base=0x{b.base:08X},")
        L.append(f"        range={b.range!r},")
        L.append(f"        cell={b.cell!r},")
        L.append(f"        source={b.source!r},")
        L.append(f"        derived={b.derived!r},")
        L.append(f"        gate={b.gate!r},")
        L.append(f"        legacy_of={b.legacy_of!r},")
        L.append("        registers=(")
        for rtl_name, off, note in b.regs:
            _, pyname, _ = names_for(b.name, rtl_name)
            L.append(f"            Register({pyname!r}, 0x{off:02X}, {note!r}),")
        L.append("        ),")
        L.append("    ),")
    L += [
        ")",
        "",
        "#: Pages held for a block that is designed but in no block design yet.",
        "#: They have NO base constant anywhere in firmware or the bench on",
        "#: purpose: a base for a slave that is not in the fabric is how one page",
        "#: comes to be claimed twice (0x44AE_0000 was, by TOUCH and by the staged",
        "#: axi_jtag). `pins` lists every tracked file that writes the literal;",
        "#: tests/firmware_logic/test_regmap_conformance.py reads them back.",
        "RESERVATIONS: tuple[Reservation, ...] = (",
    ]
    for name, base, rng, why, pins in RESERVATIONS:
        L.append("    Reservation(")
        L.append(f"        name={name!r},")
        L.append(f"        base=0x{base:08X},")
        L.append(f"        range={rng!r},")
        L.append(f"        why={why!r},")
        L.append("        pins=(")
        for f, sym in pins:
            L.append(f"            ({f!r}, {sym!r}),")
        L.append("        ),")
        L.append("    ),")
    L += [
        ")",
        "",
        "#: The page-allocation region this map is the single authority for.",
        f"REGION_LO = 0x{REGION_LO:08X}",
        f"REGION_HI = 0x{REGION_HI:08X}",
        "PAGE = 0x10000",
        "",
        "#: name -> Block. A legacy alias (SWDBB) is present under its own name",
        "#: and shares the base of the block that actually holds the page.",
        "BY_NAME: dict[str, Block] = {b.name: b for b in BLOCKS}",
        "",
        "",
        "def addr(block: str, register: str) -> int:",
        '    """Absolute address of ``block.register`` (raises KeyError if absent)."""',
        "    b = BY_NAME[block]",
        "    for r in b.registers:",
        "        if r.name == register:",
        "            return b.base + r.offset",
        "    raise KeyError(f'{block} has no register {register!r}')",
        "",
        "",
        "#: page base -> owner name, for EVERY page in the region: the BD",
        "#: blocks and the reservations in one dict. A page absent from it",
        "#: is free.",
        "PAGE_OWNER: dict[int, str] = {",
        "    **{b.base: b.name for b in BLOCKS if b.legacy_of is None},",
        "    **{r.base: r.name for r in RESERVATIONS},",
        "}",
        "",
    ]
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# View 4 — docs/contracts/shell-regmap.md
# --------------------------------------------------------------------------- #
def emit_md_map(blocks: list[Block]) -> str:
    L = ["| Base | Block | Owner | BD cell | Notes |",
         "|---|---|---|---|---|"]
    for b in blocks:
        if b.legacy_of:
            note = f"**legacy** — {b.why}"
        elif b.gate:
            note = f"**gated** — instantiated only when built with `{b.gate}=1`"
        else:
            note = "instantiated by the shell BD"
        if not b.derived:
            note += "; offsets quoted from the vendor PG, not derived"
        hi, lo = b.base >> 16, b.base & 0xFFFF
        L.append(f"| `0x{hi:04X}_{lo:04X}` | {b.name} | `{b.source}` | "
                 f"`{b.cell}` | {note} |")
    return "\n".join(L)


def emit_md_reservations(blocks: list[Block]) -> str:
    L = ["| Page | Reserved for | Range | Pinned in | Why |",
         "|---|---|---|---|---|"]
    for name, base, rng, why, pins in RESERVATIONS:
        hi, lo = base >> 16, base & 0xFFFF
        where = "<br>".join(f"`{f}` `{sym}`" for f, sym in pins)
        L.append(f"| `0x{hi:04X}_{lo:04X}` | **{name}** | {rng} | {where} | "
                 f"{why} |")
    free = _free_pages(blocks)
    L.append("")
    L.append(f"Region `0x{REGION_LO:08X}`..`0x{REGION_HI:08X}` — "
             + ("**fully allocated**: a new block must extend `REGION_HI` in "
                "`tools/gen_regmap.py` and say so here."
                if not free else
                "free pages: "
                + ", ".join(f"`0x{a:08X}`" for a in free) + "."))
    return "\n".join(L)


def emit_md_offsets(blocks: list[Block]) -> str:
    L = ["| Block | Register | Off | Address | Derived from |",
         "|---|---|---|---|---|"]
    for b in blocks:
        for rtl_name, off, _note in b.regs:
            _, _, md = names_for(b.name, rtl_name)
            L.append(f"| {b.name} | `{md}` | 0x{off:02X} | "
                     f"`0x{b.base + off:08X}` | "
                     f"{'RTL ' if b.derived else 'PG '}`{b.source}` |")
    n_reg = sum(len(b.regs) for b in blocks)
    L.append("")
    L.append(f"{len(blocks)} blocks, {n_reg} registers. "
             f"{sum(1 for b in blocks if b.derived)} blocks' offsets are parsed "
             "out of RTL in this tree; the rest are quoted from Xilinx product "
             "guides and are marked `PG` above.")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
def build(repo: Path) -> dict[str, str]:
    blocks = derive(repo)

    c_bases, c_offs = emit_c(blocks)
    h = genlib.read(repo, "firmware/common/platform_regs.h")
    h = genlib.splice(h, "regmap-bases", "c", GENERATOR, c_bases)
    h = genlib.splice(h, "regmap-offsets", "c", GENERATOR, c_offs)

    py = genlib.read(repo, "tests/common/regmap.py")
    py = genlib.splice(py, "regmap", "py", GENERATOR, emit_py_constants(blocks))

    md = genlib.read(repo, "docs/contracts/shell-regmap.md")
    md = genlib.splice(md, "regmap-map", "md", GENERATOR, emit_md_map(blocks))
    md = genlib.splice(md, "regmap-reservations", "md", GENERATOR,
                       emit_md_reservations(blocks))
    md = genlib.splice(md, "regmap-offsets", "md", GENERATOR,
                       emit_md_offsets(blocks))
    mbv = derive_mbv(repo, blocks)
    md = genlib.splice(md, "regmap-mbv", "md", GENERATOR, emit_md_mbv(mbv))

    return {
        "firmware/common/platform_regs.h": h,
        "tests/common/regmap.py": py,
        "host/pyverify/pyverify/regmap.py": emit_pyverify(blocks),
        "docs/contracts/shell-regmap.md": md,
        MBV_JSON: emit_mbv_json(mbv),
    }


if __name__ == "__main__":
    sys.exit(genlib.run(build, GENERATOR))
