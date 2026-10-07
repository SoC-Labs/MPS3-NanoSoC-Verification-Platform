"""socket_harness.registers — CSR bitfield models + a backend-agnostic operator facade.

This is the Python twin of the DFXCTL / VPHY / GENCHK / … register operations that
previously existed only in ``scripts/mps3_diag.tcl``.  Every :class:`RegBlock`,
:class:`Reg` and :class:`RegField` below is transcribed **verbatim** from the source
of truth, ``docs/contracts/shell-regmap.md`` (v0.5): offsets, bit positions and
access flags are the AXI4-Lite decode the MicroBlaze coordinator firmware targets
and the shell RTL decodes.

Layering
--------
* The models are **pure**: :meth:`RegBlock.decode` / :meth:`RegBlock.encode` are
  side-effect-free and unit-testable with no I/O (that is the whole point — they are
  the mutation-verifiable core, e.g. the ``RM_STATUS[2] = dut_eth_irq`` bit added by
  the 2026-07-24 re-mint).
* :class:`RegisterAccess` is a thin, **backend-agnostic** operator facade: the same
  instance runs against :class:`socket_harness.xsdb.XsdbRegisterEndpoint` on a board
  or an in-process fake in tests — it only ever calls
  ``read_word`` / ``write_word`` / ``read_block`` on the injected backend.

Notes carried from the contract (do not silently "fix" these):

* **DFXCTL 0x20 (``CONSOLE_POP``)** is *not* a documented register.  A read of that
  offset pops a UART console byte — an anecdotal read-aliasing side-effect of the
  decode in ``dfx_ctl.sv`` (shell-regmap.md CLCD note).  It is modelled purely so
  :meth:`RegisterAccess.dump_page` can *exclude* it (``read_safe=False``); it must
  never be read while sweeping a page.
* **UARTBR ``U0_RX`` / ``U1_RX`` / ``SWO_RX``** have destructive reads (each read
  pops a FIFO byte), so they are ``read_safe=False`` and likewise excluded from
  :meth:`dump_page`.
* **VPHY (0x44A3), GENCHK (0x44A6) and CLCDKVM (0x44AD)** are address-map
  *reservations*: the shipped static ``shell_bd.tcl`` stops ``assign_bd_address`` at
  CLCD (0x44AC), so these pages are **unmapped on the shipped silicon**.  Their
  blocks are marked ``RESERVED/not-instantiated on shipped static``
  (:attr:`RegBlock.reserved`); a consumer (probe/dump) must not assume they respond.

No I/O happens at import time.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, Tuple

# The bases and offsets below are NOT written here. They come from
# pyverify.regmap, which tools/gen_regmap.py derives from the shell block design
# and the CSR RTL -- see its module docstring for what that removes. This module
# keeps only what cannot be derived: the BITFIELD models and the access/read-safe
# semantics. A register that moves in RTL moves here on the next generation; a
# register that disappears raises at import instead of silently addressing a
# neighbour, which is what a stale literal did.
from pyverify.regmap import BY_NAME as _GEN

if TYPE_CHECKING:  # annotation-only; keeps this module runtime-pure and import-cheap
    from .session import RegisterBackend

__all__ = [
    "RegField",
    "Reg",
    "RegBlock",
    "RegisterAccess",
    "BLOCKS",
    # module-level block singletons (used directly by tests, e.g. DFXCTL.decode(...))
    "CLKRST",
    "DFXCTL",
    "HWICAP",
    "VPHY",
    "USD",
    "TELEM",
    "GENCHK",
    "SWDBB",
    "DBGBR",
    "UARTBR",
    "GPIO",
    "MMCM_DRP",
    "CLCD",
    "CLCDKVM",
]

def _base(block: str) -> int:
    """Base address of ``block``, from the generated table."""
    try:
        return _GEN[block].base
    except KeyError:
        raise KeyError(
            f"{block} is not in the generated register map (pyverify.regmap); "
            "the shell BD no longer assigns it an address"
        ) from None


def _off(block: str, register: str) -> int:
    """Byte offset of ``block.register``, from the generated table."""
    for r in _GEN[block].registers if block in _GEN else ():
        if r.name == register:
            return r.offset
    raise KeyError(
        f"{block}.{register} is not in the generated register map "
        "(pyverify.regmap) -- it moved, was renamed, or left the RTL"
    )


def _reserved_note(block: str) -> str:
    """The note for a block the shell BD does NOT instantiate.

    This used to be a constant string pinned to VPHY, GENCHK and CLCDKVM by
    hand. All three are wrong: ``shell_bd.tcl`` assigns every one of them an
    address (``:1399``, ``:1400``, ``:1407``) and has done since well before the
    then-fielded ``0xA8C1C535`` mint. The claim survived because nothing could check
    it. Now the generated table answers it, so an absent block says so and a
    present one cannot.
    """
    if block not in _GEN:
        return "NOT in the shell block design -- no slave at this page"
    gate = _GEN[block].gate
    if gate:
        return f"instantiated only when the shell is built with {gate}=1"
    return ""


# --------------------------------------------------------------------------- #
# Pure field / register / block models
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RegField:
    """One contiguous bitfield: ``[lsb + width - 1 : lsb]`` named ``name``.

    ``extract`` and ``place`` are pure masked shifts and are the primitives every
    ``decode``/``encode`` is built from.
    """

    lsb: int
    width: int
    name: str

    @property
    def mask(self) -> int:
        """Right-justified field mask, ``(1 << width) - 1``."""
        return (1 << self.width) - 1

    def extract(self, word: int) -> int:
        """Pull this field's value out of a whole register ``word``."""
        return (word >> self.lsb) & self.mask

    def place(self, value: int) -> int:
        """Shift ``value`` (masked to ``width``) into this field's position."""
        return (value & self.mask) << self.lsb


@dataclass(frozen=True)
class Reg:
    """A register at ``offset`` within its block.

    ``read_safe`` is ``False`` for offsets that must never be read during a blind
    page sweep — either a read side-effect (DFXCTL ``CONSOLE_POP`` @ 0x20) or a
    genuinely destructive FIFO pop (UARTBR ``*_RX``).  ``access`` is metadata only
    (``'rw'`` / ``'ro'`` / ``'wo'`` / ``'w1c'``); it does not gate anything here.
    """

    offset: int
    name: str
    fields: Tuple[RegField, ...] = ()
    read_safe: bool = True
    access: str = "rw"

    def field(self, name: str) -> RegField:
        for f in self.fields:
            if f.name == name:
                return f
        known = ", ".join(f.name for f in self.fields) or "(none)"
        raise ValueError(f"{self.name}: no field {name!r}; known: {known}")


@dataclass(frozen=True)
class RegBlock:
    """A 64 KiB CSR page at ``base``.

    ``spec_name`` is the lowercase key used across the package (it matches the CSR
    endpoint names in :mod:`socket_harness.endpoints`).  ``note`` carries the
    contract's status marker; blocks whose note starts with ``RESERVED`` are not
    instantiated on the shipped static (see :attr:`reserved`).
    """

    spec_name: str
    base: int
    regs: Tuple[Reg, ...]
    note: str = ""

    @property
    def reserved(self) -> bool:
        """True when the block is an address-map reservation (not on shipped silicon)."""
        return self.note.upper().startswith("RESERVED")

    def reg(self, name: str) -> Reg:
        for r in self.regs:
            if r.name == name:
                return r
        known = ", ".join(r.name for r in self.regs) or "(none)"
        raise ValueError(f"{self.spec_name}: no register {name!r}; known: {known}")

    def addr(self, reg_name: str) -> int:
        """Absolute AXI address of ``reg_name`` (``base + reg.offset``)."""
        return self.base + self.reg(reg_name).offset

    def decode(self, reg_name: str, word: int) -> Dict[str, int]:
        """PURE: ``{field.name: field.extract(word)}`` for every field of the register."""
        reg = self.reg(reg_name)
        return {f.name: f.extract(word) for f in reg.fields}

    def encode(self, reg_name: str, **fields: int) -> int:
        """PURE: OR the placed field values into one word.  ``ValueError`` on an unknown field."""
        reg = self.reg(reg_name)
        fmap = {f.name: f for f in reg.fields}
        word = 0
        for name, value in fields.items():
            fld = fmap.get(name)
            if fld is None:
                known = ", ".join(fmap) or "(none)"
                raise ValueError(
                    f"{self.spec_name}.{reg_name}: unknown field {name!r}; known: {known}"
                )
            word |= fld.place(int(value))
        return word


# small helpers to keep the transcription below dense and legible
def _bit(lsb: int, name: str) -> RegField:
    return RegField(lsb, 1, name)


def _fld(msb: int, lsb: int, name: str) -> RegField:
    return RegField(lsb, msb - lsb + 1, name)


# --------------------------------------------------------------------------- #
# The blocks — transcribed verbatim from docs/contracts/shell-regmap.md (v0.5)
# --------------------------------------------------------------------------- #

# CLKRST (0x44A0_0000) — DUT clock (DRP) + 3 resets
CLKRST = RegBlock(
    "clkrst",
    _base("CLKRST"),
    (
        Reg(_off("CLKRST", "RESET_CTRL"), "RESET_CTRL",
            (_bit(0, "dut_resetn"), _bit(1, "rp_resetn"), _bit(2, "dbg_resetn"))),
        Reg(_off("CLKRST", "DUT_CLK_SEL"), "DUT_CLK_SEL", (_fld(7, 0, "preset_id"),)),
        Reg(_off("CLKRST", "DUT_CLK_DRP"), "DUT_CLK_DRP", (_fld(15, 0, "drp"),)),
        Reg(_off("CLKRST", "STATUS"), "STATUS",
            (_bit(0, "mmcm_locked"), _bit(1, "dut_clk_alive")),
            read_safe=True, access="ro"),
    ),
)

# DFXCTL (0x44A1_0000) — decoupler + shutdown-mgr + RP reset gate + rm_id readback
DFXCTL = RegBlock(
    "dfxctl",
    _base("DFXCTL"),
    (
        Reg(_off("DFXCTL", "DECOUPLE"), "DECOUPLE", (_bit(0, "decouple_en"),)),
        Reg(_off("DFXCTL", "SHUTDOWN"), "SHUTDOWN", (_bit(0, "axi_shutdown"),)),
        Reg(_off("DFXCTL", "STATUS"), "STATUS",
            (_bit(0, "decoupled"), _bit(1, "rp_in_reset")), access="ro"),
        Reg(_off("DFXCTL", "RM_ID"), "RM_ID", (_fld(31, 0, "rm_id"),), access="ro"),
        # RM_STATUS[2] = dut_eth_irq — the exact bit the 2026-07-24 re-mint added
        # (2-FF synced DUT eth irq_out observability); this is the mutation target.
        Reg(_off("DFXCTL", "RM_STATUS"), "RM_STATUS",
            (_bit(0, "rm_id_valid"), _bit(1, "dut_lockup"), _bit(2, "dut_eth_irq")),
            access="ro"),
        # 0x20 is NOT a documented register: reading it pops a UART console byte, an
        # anecdotal read-aliasing side-effect of the decode in dfx_ctl.sv. Modelled
        # read_safe=False purely so dump_page() excludes it.
        Reg(0x20, "CONSOLE_POP", (), read_safe=False, access="ro"),
    ),
)

# HWICAP (0x44A2_0000) — partial-bitstream config port (standard Xilinx AXI HWICAP)
HWICAP = RegBlock(
    "hwicap",
    _base("HWICAP"),
    (),
    note="Standard Xilinx AXI HWICAP (WF/RF/SZ/CR/SR/…) — no custom fields modelled; PG134.",
)

# VPHY (0x44A3_0000) — virtual-PHY register model + link events (RESERVED)
VPHY = RegBlock(
    "vphy",
    _base("VPHY"),
    (
        Reg(_off("VPHY", "PHY_STATE"), "PHY_STATE",
            (_bit(0, "link_up"), _bit(1, "speed100"), _bit(2, "full_duplex"))),
        Reg(_off("VPHY", "PHY_ID"), "PHY_ID", (_fld(31, 0, "phy_id"),)),
        Reg(_off("VPHY", "LINK_EVENT"), "LINK_EVENT", (_bit(0, "force_down"), _bit(1, "pulse"))),
    ),
    note=_reserved_note("VPHY"),
)

# USD (0x44A4_0000) — the USER microSD slot in SPI mode (fpga/shell/ip/usd_spi,
# D13; was OVLSTORE, the pad-less AXI Quad SPI). A DATA write STARTS a shift on
# the card's pads, so a blind sweep may read it (no read side effect) but must
# never write it.
USD = RegBlock(
    "usd",
    _base("USD"),
    (
        Reg(_off("USD", "ID"), "ID", (_fld(31, 0, "id"),), access="ro"),
        Reg(_off("USD", "CTRL"), "CTRL",
            (_bit(0, "en"), _bit(1, "cs"), _bit(2, "wide"), _bit(3, "cd_pol"),
             _bit(4, "cd_ignore"))),
        Reg(_off("USD", "CLKDIV"), "CLKDIV", (_fld(15, 0, "div"),)),
        Reg(_off("USD", "DATA"), "DATA", (_fld(31, 0, "data"),)),
        Reg(_off("USD", "STATUS"), "STATUS",
            (_bit(0, "busy"), _bit(1, "cd_present"), _bit(2, "cd_raw"),
             _bit(3, "cd_changed"), _bit(4, "ovr"), _bit(5, "abort")),
            access="w1c"),
    ),
)

# TELEM (0x44A5_0000) — telemetry (INA228 I2C, status)
TELEM = RegBlock(
    "telem",
    _base("TELEM"),
    (
        Reg(_off("TELEM", "CTRL"), "CTRL", (_bit(0, "enable"), _bit(1, "alarm_en"))),
        Reg(_off("TELEM", "BUS_MV"), "BUS_MV", (_fld(31, 0, "bus_mv"),), access="ro"),
        Reg(_off("TELEM", "CURR_UA"), "CURR_UA", (_fld(31, 0, "curr_ua"),), access="ro"),
        Reg(_off("TELEM", "POWER_MW"), "POWER_MW", (_fld(31, 0, "power_mw"),), access="ro"),
        Reg(_off("TELEM", "STATUS"), "STATUS", (_bit(0, "alarm"), _bit(1, "i2c_err")), access="ro"),
    ),
)

# GENCHK (0x44A6_0000) — error-inject gen/checker control (RESERVED)
GENCHK = RegBlock(
    "genchk",
    _base("GENCHK"),
    (
        Reg(_off("GENCHK", "CTRL"), "CTRL", (_bit(0, "gen_en"), _bit(1, "chk_en"))),
        # INJECT one-hot; field names == pyverify.client.MACGEN_INJECTS minus 'none'.
        Reg(_off("GENCHK", "INJECT"), "INJECT",
            (_bit(0, "bad_fcs"), _bit(1, "runt"), _bit(2, "giant"),
             _bit(3, "ifg"), _bit(4, "dribble"))),
        Reg(_off("GENCHK", "TX_CNT"), "TX_CNT", (), access="ro"),
        Reg(_off("GENCHK", "RX_CNT"), "RX_CNT", (), access="ro"),
        Reg(_off("GENCHK", "ERR_CNT"), "ERR_CNT", (), access="ro"),
    ),
    note=_reserved_note("GENCHK"),
)

# SWDBB (0x44A7_0000) — SWD pin-wiggler (remote_bitbang backend)
SWDBB = RegBlock(
    "swdbb",
    _base("SWDBB"),
    (
        Reg(_off("SWDBB", "DRIVE"), "DRIVE", (_bit(0, "swclk"), _bit(1, "swdio_o"), _bit(2, "swdio_oe"))),
        Reg(_off("SWDBB", "SAMPLE"), "SAMPLE", (_bit(0, "swdio_i"),), access="ro"),
    ),
)

# DBGBR (0x44A8_0000) — Xilinx Debug Bridge (XVC/BSCAN)
DBGBR = RegBlock(
    "dbgbr",
    _base("DBGBR"),
    (),
    note="Xilinx Debug Bridge (AXI→BSCAN); XVC-over-TCP 2542; no custom fields; must be in static.",
)

# UARTBR (0x44A9_0000) — UART0/UART1/SWO AXIS ↔ MicroBlaze FIFOs
UARTBR = RegBlock(
    "uartbr",
    _base("UARTBR"),
    (
        # DUT→host pops are destructive (read_safe=False) → excluded from dump_page.
        Reg(_off("UARTBR", "U0_DATA"), "U0_RX", (_fld(7, 0, "data"), _bit(8, "valid")), read_safe=False),
        Reg(_off("UARTBR", "U1_DATA"), "U1_RX", (_fld(7, 0, "data"), _bit(8, "valid")), read_safe=False),
        Reg(_off("UARTBR", "SWO_RX"), "SWO_RX", (_fld(7, 0, "data"), _bit(8, "valid")), read_safe=False),
        Reg(_off("UARTBR", "FIFO_STATUS"), "FIFO_STATUS",
            (_bit(0, "u0_tx_full"), _bit(1, "u0_rx_empty"), _bit(2, "u1_tx_full"),
             _bit(3, "u1_rx_empty"), _bit(4, "swo_rx_empty")),
            access="ro"),
        Reg(_off("UARTBR", "SWO_CFG"), "SWO_CFG",
            (_fld(15, 0, "divisor"), _bit(16, "enable"),
             _bit(30, "overflow"), _bit(31, "frame_err"))),
    ),
)

# GPIO (0x44AA_0000) — board GPIO/PMOD passthrough + host mux
GPIO = RegBlock(
    "gpio",
    _base("GPIO"),
    (
        Reg(_off("GPIO", "IN"), "IN", (_fld(31, 0, "in"),), access="ro"),
        Reg(_off("GPIO", "OUT"), "OUT", (_fld(31, 0, "out"),)),
        Reg(_off("GPIO", "OE"), "OE", (_fld(31, 0, "oe"),)),
        Reg(_off("GPIO", "OWN"), "OWN", (_fld(31, 0, "own"),)),
    ),
)

# MMCM_DRP (0x44AB_0000) — DUT-clock MMCM DRP over AXI4-Lite (Clocking Wizard)
MMCM_DRP = RegBlock(
    "mmcm_drp",
    _base("MMCM_DRP"),
    (),
    note="Xilinx Clocking Wizard AXI-Lite DRP (PG065); DUT-clock reconfig; no custom fields; must be in static.",
)

# CLCD (0x44AC_0000) — QVGA HX8347-D 8080 bus master (LIVE, panel lit). No read side effects.
CLCD = RegBlock(
    "clcd",
    _base("CLCD"),
    (
        Reg(_off("CLCD", "CTRL"), "CTRL",
            (_bit(0, "enable"), _bit(1, "backlight"), _bit(2, "reset_n"),
             _bit(3, "fifo_reset"), _bit(4, "read_start"))),
        Reg(_off("CLCD", "CMD"), "CMD", (_fld(7, 0, "byte"),)),
        Reg(_off("CLCD", "DATA"), "DATA", (_fld(7, 0, "byte"),)),
        Reg(_off("CLCD", "STATUS"), "STATUS",
            (_bit(0, "fifo_full"), _bit(1, "fifo_empty"), _bit(2, "busy"),
             _fld(15, 8, "fifo_level")),
            access="ro"),
        # READ has NO side effect (contract): safe to sweep; reads 0 when READ_PATH=0.
        Reg(_off("CLCD", "READ"), "READ", (_fld(7, 0, "rdata"), _bit(8, "valid")),
            read_safe=True, access="ro"),
        Reg(_off("CLCD", "TIMING"), "TIMING",
            (_fld(7, 0, "wr_lo"), _fld(15, 8, "wr_hi"), _fld(23, 16, "cs_setup"))),
    ),
)

# CLCDKVM (0x44AD_0000) — CLCD KVM panel arbiter (RESERVED). EVENT is W1C, not read-to-clear.
CLCDKVM = RegBlock(
    "clcdkvm",
    _base("CLCDKVM"),
    (
        Reg(_off("CLCDKVM", "CTRL"), "CTRL",
            (_bit(0, "src_sel"), _bit(1, "force_harness"), _bit(2, "timeout_en"),
             _bit(3, "pb_en"), _bit(4, "dut_req_en"), _bit(5, "backlight"),
             _bit(6, "panel_rst_n"), _bit(7, "bl_rst_src"), _bit(8, "panel_rst_pulse"),
             _bit(9, "force_switch"), _bit(16, "src_sel_we"))),
        Reg(_off("CLCDKVM", "STATUS"), "STATUS",
            (_bit(0, "owner"), _bit(1, "switch_pending"), _bit(2, "tgt_owner"),
             _bit(3, "panel_rst_active"), _bit(4, "panel_settling"), _bit(5, "granting"),
             _bit(6, "draining"), _bit(7, "kvm_drives_pads"), _bit(8, "harness_quiet"),
             _bit(9, "dut_quiet"), _bit(10, "dut_req"), _bit(11, "pb_level"),
             _bit(12, "pb_raw"), _bit(13, "decoupled"), _bit(14, "rp_in_reset"),
             _bit(15, "interlock"), _fld(18, 16, "state")),
            access="ro"),
        # EVENT is RW1C — write 1 to clear; reads do NOT clear (read_safe stays True).
        Reg(_off("CLCDKVM", "EVENT"), "EVENT",
            (_bit(0, "harness_gained"), _bit(1, "harness_lost"), _bit(2, "dut_gained"),
             _bit(3, "dut_lost"), _bit(4, "timeout_fired"), _bit(5, "forced_revert"),
             _bit(6, "pb_toggle"), _bit(7, "panel_reset_done")),
            access="w1c"),
        Reg(_off("CLCDKVM", "PANEL_TMR"), "PANEL_TMR", (_fld(15, 0, "rst_us"), _fld(31, 16, "settle_us"))),
        Reg(_off("CLCDKVM", "TIMEOUT"), "TIMEOUT", (_fld(31, 0, "timeout_us"),)),
        Reg(_off("CLCDKVM", "DEBOUNCE"), "DEBOUNCE", (_fld(15, 0, "debounce_us"),)),
        Reg(_off("CLCDKVM", "TUNNEL"), "TUNNEL", (_fld(15, 0, "gpio_o"), _fld(31, 16, "gpio_oe")), access="ro"),
    ),
    note=_reserved_note("CLCDKVM"),
)


#: All CSR blocks keyed by lowercase ``spec_name`` — matches the CSR endpoint names
#: in :mod:`socket_harness.endpoints` (the cross-file base invariant asserted in
#: tests/test_endpoints.py and tests/test_registers.py).
BLOCKS: Dict[str, RegBlock] = {
    b.spec_name: b
    for b in (
        CLKRST, DFXCTL, HWICAP, VPHY, USD, TELEM, GENCHK,
        SWDBB, DBGBR, UARTBR, GPIO, MMCM_DRP, CLCD, CLCDKVM,
    )
}


def _block(name: str) -> RegBlock:
    try:
        return BLOCKS[name]
    except KeyError:
        known = ", ".join(sorted(BLOCKS))
        raise ValueError(f"unknown CSR block {name!r}; known: {known}") from None


# --------------------------------------------------------------------------- #
# Backend-agnostic operator facade
# --------------------------------------------------------------------------- #
class RegisterAccess:
    """Operator-level CSR ops over any :class:`~socket_harness.session.RegisterBackend`.

    The backend is injected, so the exact same instance drives an
    :class:`~socket_harness.xsdb.XsdbRegisterEndpoint` on the board or an in-process
    fake in tests — this class performs no I/O beyond the backend's
    ``read_word`` / ``write_word``.
    """

    def __init__(self, backend: "RegisterBackend") -> None:
        self.backend = backend

    # -- generic reg ops --------------------------------------------------- #
    def read_reg(self, block: str, reg: str) -> Dict[str, int]:
        """Read one register and decode it to ``{field: value}``."""
        blk = _block(block)
        word = self.backend.read_word(blk.addr(reg))
        return blk.decode(reg, word)

    def write_reg(self, block: str, reg: str, **fields: int) -> None:
        """Encode ``**fields`` into one word and write it."""
        blk = _block(block)
        word = blk.encode(reg, **fields)
        self.backend.write_word(blk.addr(reg), word)

    # -- convenience verbs ------------------------------------------------- #
    def rm_id(self) -> int:
        """Read DFXCTL.RM_ID — the RM-load-verify ground truth (raw 32-bit id)."""
        return self.backend.read_word(DFXCTL.addr("RM_ID"))

    def rm_status(self) -> Dict[str, int]:
        """Decode DFXCTL.RM_STATUS → ``{rm_id_valid, dut_lockup, dut_eth_irq}``."""
        return self.read_reg("dfxctl", "RM_STATUS")

    def vphy_link(self, *, down: bool, pulse: bool) -> None:
        """Inject a virtual-PHY link event (VPHY.LINK_EVENT)."""
        self.write_reg("vphy", "LINK_EVENT",
                       force_down=int(bool(down)), pulse=int(bool(pulse)))

    def genchk(self, *, gen: bool, chk: bool, inject: str) -> Dict[str, int]:
        """Drive GENCHK: set CTRL.gen_en/chk_en, arm INJECT one-hot, read the counters.

        ``inject`` is validated against the INJECT field names — which are exactly
        ``pyverify.client.MACGEN_INJECTS`` minus ``'none'`` — raising ``ValueError``
        on anything else (including ``'none'``: there is no bit to arm).
        """
        inject_names = tuple(f.name for f in GENCHK.reg("INJECT").fields)
        if inject not in inject_names:
            raise ValueError(
                f"genchk: unknown inject {inject!r}; known: {', '.join(inject_names)} "
                "(shell-regmap.md GENCHK.INJECT == pyverify MACGEN_INJECTS minus 'none')"
            )
        self.write_reg("genchk", "CTRL", gen_en=int(bool(gen)), chk_en=int(bool(chk)))
        self.write_reg("genchk", "INJECT", **{inject: 1})
        return {
            "tx_cnt": self.backend.read_word(GENCHK.addr("TX_CNT")),
            "rx_cnt": self.backend.read_word(GENCHK.addr("RX_CNT")),
            "err_cnt": self.backend.read_word(GENCHK.addr("ERR_CNT")),
        }

    # -- page sweep -------------------------------------------------------- #
    def dump_page(self, block: str) -> Dict[str, Dict[str, int]]:
        """Read + decode every ``read_safe`` register of a block.

        Correctness property: registers with ``read_safe=False`` are **skipped**, so
        a dump can never read DFXCTL ``CONSOLE_POP`` (0x20, pops a console byte) or
        the UARTBR ``*_RX`` FIFOs (destructive pops).
        """
        blk = _block(block)
        out: Dict[str, Dict[str, int]] = {}
        for reg in blk.regs:
            if not reg.read_safe:
                continue
            word = self.backend.read_word(blk.addr(reg.name))
            out[reg.name] = blk.decode(reg.name, word)
        return out
