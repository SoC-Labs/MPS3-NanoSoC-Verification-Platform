"""``QspiFlashProgrammer`` — host-side QSPI-flash programming over SWD/AHB.

The missing half of the boot-from-flash product path. ``flash_pack.py``
(``firmware/micropython/flash_pack.py``) *builds* the packed QSPI image
(boot table @0x0, HOT @0x1000, COLD @0x20000); this module *writes* that
``.bin`` to the board's on-board **SST26VF064B** flash and reads it back.

Why over SWD, and not the shell
-------------------------------
The on-board 8 MB QSPI flash is now **solely owned by the RP/DUT**: commit
``541a513`` relinquished the DFX shell's OVLSTORE pads, so the shell can no
longer drive the flash pins (``docs/MPS3_ONBOARD_FLASH_MAPPING.md`` §6 —
"single-flash contention"). The only remaining programming path is to drive
the **DUT's own QSPI controller** (``ahb_qspi``): halt the M0, reach into the
controller's APB register block over the existing SWD/AHB debug path
(``host/openocd/``, :mod:`pyverify.swd`), issue SST26 command transactions,
then release the core.

The controller and its register map
-----------------------------------
The SoC's controller is SoC Labs' own ``top_ahb_qspi`` / ``apb_qspi_regs``
(``ahb_qspi/logical/apb_qspi_regs/logical/apb_qspi_regs.v``). Its register
aperture is at ``NANOSOC_QSPI_CTRL_BASE = 0x74000000`` (the XiP read aperture
is ``0x70000000``) — ``firmware/micropython/port/xip_bringup.h:38-45``. The
byte offsets below are the ``apb_qspi_regs.v`` word registers (PADDR word
index x 4), cross-named in ``ahb_qspi/uvm/ahb_qspi/env/ahb_qspi_pkg.sv:20-41``:

===========  ======  ===================================================
offset       reg     fields (apb_qspi_regs.v:42-71, 122-168)
===========  ======  ===================================================
``0x00``     reg0    CTRL: QIO_MODE[0], XIP_ACTIVE[8], MODE_CODE[23:16],
                     CONT_READ[24], NO_CMD[25]
``0x04``     reg1    STATUS: BUSY[0] (RO), IRQ_CLR[8] (W1C)
``0x08``     reg2    SPI_CMD: CMD[7:0], ENABLE[8], READ[9], WRITE[10],
                     ADDR_EN[11], DUMMY[15:12], N_RW_BYTES[19:16]
``0x0C``     reg3    ADDR[21:0]  (4 MB / 3-byte reach)
``0x10-1C``  reg4-7  RDATA0-3 (RO, byte-swapped from the controller)
``0x20-2C``  reg8-11 WDATA0-3 (bit/nibble-transformed to the controller)
``0x30``     reg12   AHB_SPI_SETUP: AHB_CMD[7:0], AHB_DUMMY[15:12] (XiP)
``0x34``     reg13   CLK_DIV[4:0]
===========  ======  ===================================================

The command transaction is the two-phase protocol the UVM bench uses
(``ahb_qspi/uvm/ahb_qspi/sequences/qspi_cmd_sequence.sv``): optionally set
ADDR (reg3) and WDATA (reg8+); write SPI_CMD with ENABLE=0 (setup) then again
with ENABLE=1 (trigger); poll STATUS.BUSY until clear; read RDATA (reg4+) if
reading; write STATUS.IRQ_CLR. ``N_RW_BYTES`` is off-by-one: field value N
means **N+1 bytes** (``qspi_cmd_sequence.sv:9-10``).

SST26VF064B command set (``ahb_qspi_pkg.sv:105-130``, the controller's own
regression VIP is the SST26VF064B model — ``ahb_qspi/uvm/ahb_qspi/tb/top.sv:111``):
JEDEC ``0x9F`` (mfg ``0xBF`` / type ``0x26`` / dev ``0x43``; a 3-byte read
lands as ``0x4326BF00`` in RDATA0), WREN ``0x06``, **ULBPR ``0x98`` global
block-protection unlock** (the part powers up with every block
write-protected — ``docs/MPS3_ONBOARD_FLASH_MAPPING.md`` §2/§5/§6, so this
must precede any erase/program), sector-erase ``0x20`` (4 KB), page-program
``0x02`` (256 B pages), read-status ``0x05`` (WIP = bit0, WEL = bit1),
read ``0x03`` (used for read-back verify — no dummy cycles).

What is board-UNPROVEN here
---------------------------
This is a host tool; it cannot be fully validated without the board (or the
SST26 sim VIP driven through a real SWD/AHB transport). Unit tests exercise
the **command sequencing and image handling** against a fake controller model
(``tests/test_qspi_flash.py``); what only real hardware/sim can confirm:

* **Real SWD-over-AHB throughput.** Every register access is one OpenOCD
  ``mdw``/``mww`` batch (:mod:`pyverify.swd`) — seconds per op over
  ``remote_bitbang`` (``docs/SWD_BRINGUP_PLAN.md`` §5). Programming a full
  image this way is *minutes-to-hours*; a production tool would batch bigger
  transfers (N_RW_BYTES reaches 16 B/command) or use a firmware helper.
* **Actual SST26 program/erase timing** (WIP-busy durations) and the true
  WDATA/RDATA **byte-lane order** for >4-byte transfers. To keep the lane
  model exact and RTL-grounded, this tool transfers data **4 bytes per
  command** (one word, uniform with the JEDEC/status decode derived from
  ``apb_qspi_regs.v``'s reg4 byte-swap); the wider-transfer lane packing is
  not modelled here.

A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
license.  Copyright (C) 2026, SoC Labs (www.soclabs.org)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol, Sequence, Union, runtime_checkable

__all__ = [
    # bases / geometry
    "QSPI_CTRL_BASE",
    "QSPI_XIP_BASE",
    "FLASH_ADDR_MASK",
    "FLASH_CAPACITY",
    "PAGE_SIZE",
    "SECTOR_SIZE",
    # register offsets
    "REG_CTRL",
    "REG_STATUS",
    "REG_SPI_CMD",
    "REG_ADDR",
    "REG_RDATA0",
    "REG_WDATA0",
    # SPI_CMD field masks
    "CMD_ENABLE",
    "CMD_READ",
    "CMD_WRITE",
    "CMD_ADDR_EN",
    "CMD_DUMMY_SHIFT",
    "CMD_NRW_SHIFT",
    "STATUS_BUSY",
    "STATUS_IRQ_CLR",
    # SST26 opcodes / identity
    "OP_WREN",
    "OP_WRDI",
    "OP_RDSR",
    "OP_JEDEC_ID",
    "OP_PP",
    "OP_SECTOR_ERASE",
    "OP_READ",
    "OP_ULBPR",
    "SR_WIP",
    "SR_WEL",
    "SST26_MFG_ID",
    "SST26_DEV_TYPE",
    "SST26_DEV_ID",
    "SST26_JEDEC_RDATA0",
    # lane helpers
    "byteswap32",
    "pack_tx_word",
    "unpack_rx_bytes",
    # API
    "AhbAccess",
    "QspiFlashError",
    "JedecId",
    "VerifyResult",
    "ProgramResult",
    "QspiFlashProgrammer",
]

# --- controller bases / flash geometry ------------------------------------- #
#: QSPI controller register aperture (xip_bringup.h:41, NANOSOC_QSPI_CTRL_BASE).
QSPI_CTRL_BASE = 0x7400_0000
#: XiP execute-in-place read aperture (xip_bringup.h:44, NANOSOC_QSPI_XIP_BASE).
QSPI_XIP_BASE = 0x7000_0000
#: The controller's ADDR reg is 22-bit (apb_qspi_regs.v:25/136) — 4 MB reach,
#: 3-byte addressing (a subset of the 8 MB SST26VF064B; the boot image lives in
#: the first ~0x30000). Addresses are masked to this before hitting reg3.
FLASH_ADDR_MASK = 0x3F_FFFF
FLASH_CAPACITY = FLASH_ADDR_MASK + 1  # 4 MiB addressable aperture
#: SST26VF064B page-program page and (uniform) 4 KB sector-erase granularity
#: (docs/MPS3_ONBOARD_FLASH_MAPPING.md; OP_SECTOR_ERASE = 0x20).
PAGE_SIZE = 256
SECTOR_SIZE = 4096

# --- apb_qspi_regs.v register byte offsets (ahb_qspi_pkg.sv:20-41) ---------- #
REG_CTRL = 0x00     # reg0
REG_STATUS = 0x04   # reg1
REG_SPI_CMD = 0x08  # reg2
REG_ADDR = 0x0C     # reg3
REG_RDATA0 = 0x10   # reg4  (reg5-7 at +4/+8/+0xC)
REG_WDATA0 = 0x20   # reg8  (reg9-11 at +4/+8/+0xC)
REG_AHB_SPI_SETUP = 0x30  # reg12
REG_CLK_DIV = 0x34  # reg13, CLK_DIV[4:0]

# --- CLK_DIV (reg13) ------------------------------------------------------- #
# SCLK = HCLK/(2*CLK_DIV) for CLK_DIV != 0; CLK_DIV=0 is an explicit raw-HCLK
# BYPASS (qspi_clock_div.v:10), NOT a divide. The field RESETS TO 1 (HCLK/2),
# which is out of spec for the part and is a KNOWN, OBSERVED silicon failure:
# CLK_DIV=1 garbles RDID (docs/QSPI_RP_BOARD_BRINGUP.md:227-245). Nothing in
# RTL enforces a floor -- it is a firmware/host obligation, so the host must
# program it before the FIRST transaction, JEDEC included.
CLK_DIV_MIN = 4     # SCLK <= HCLK/8: the flash AC spec + the timing signoff
CLK_DIV_MAX = 0x1F  # 5-bit field

# --- SPI_CMD (reg2) field masks (apb_qspi_regs.v:53-60, ahb_qspi_pkg.sv:91-100)
CMD_ENABLE = 1 << 8
CMD_READ = 1 << 9
CMD_WRITE = 1 << 10
CMD_ADDR_EN = 1 << 11
CMD_DUMMY_SHIFT = 12  # DUMMY_CYCLES[15:12]
CMD_NRW_SHIFT = 16    # N_RW_BYTES[19:16], value N -> N+1 bytes

# --- STATUS (reg1) (apb_qspi_regs.v:49-51, ahb_qspi_pkg.sv:85-86) ----------- #
STATUS_BUSY = 1 << 0     # controller busy (RO)
STATUS_IRQ_CLR = 1 << 8  # write-1 to clear IRQ_QSPI_FINISHED

# --- SST26VF064B opcodes / identity (ahb_qspi_pkg.sv:105-130) --------------- #
OP_WREN = 0x06
OP_WRDI = 0x04
OP_RDSR = 0x05
OP_JEDEC_ID = 0x9F
OP_PP = 0x02
OP_SECTOR_ERASE = 0x20  # 4 KB sector erase
OP_READ = 0x03          # normal read (no dummy) — used for read-back verify
OP_ULBPR = 0x98         # global block-protection unlock

SR_WIP = 1 << 0  # status-register write-in-progress
SR_WEL = 1 << 1  # status-register write-enable-latch

SST26_MFG_ID = 0xBF     # Microchip/SST
SST26_DEV_TYPE = 0x26
SST26_DEV_ID = 0x43     # 64 Mbit / 8 MB
#: What a 3-byte JEDEC read yields in RDATA0 after apb_qspi_regs.v's reg4
#: byte-swap: {dev_id, type, mfg, 0x00} (ahb_qspi_pkg.sv:130, cross-checked in
#: the UVM scoreboard). The decode below recovers (mfg, type, dev_id) from it.
SST26_JEDEC_RDATA0 = 0x4326_BF00


# --------------------------------------------------------------------------- #
# Byte-lane helpers (RTL-derived; pure)
# --------------------------------------------------------------------------- #
def byteswap32(word: int) -> int:
    """Reverse the 4 byte lanes of a 32-bit word — exactly what
    ``apb_qspi_regs.v`` does presenting reg4 (``PRDATA = {QSPI_RDATA[7:0],
    [15:8], [23:16], [31:24]}``, :211)."""
    word &= 0xFFFF_FFFF
    return (
        ((word & 0x0000_00FF) << 24)
        | ((word & 0x0000_FF00) << 8)
        | ((word & 0x00FF_0000) >> 8)
        | ((word & 0xFF00_0000) >> 24)
    )


def unpack_rx_bytes(rdata0: int, n: int) -> bytes:
    """Recover the ``n`` (<=4) received flash bytes, in wire order (byte 0 =
    first out of the flash), from RDATA0.

    Grounded in ``apb_qspi_regs.v``: the controller shifts received bytes in
    MSB-first, right-aligned, so an ``n``-byte read occupies the low ``n*8``
    bits; reg4 then byte-swaps the low word. Inverting: ``value =
    byteswap32(rdata0)`` and ``byte_i = (value >> 8*(n-1-i)) & 0xFF``. This
    reproduces the cited JEDEC value: ``unpack_rx_bytes(0x4326BF00, 3)`` ==
    ``bytes([0xBF, 0x26, 0x43])``.
    """
    if not 0 < n <= 4:
        raise ValueError(f"n must be 1..4 (one RDATA word), got {n}")
    value = byteswap32(rdata0)
    return bytes((value >> (8 * (n - 1 - i))) & 0xFF for i in range(n))


def pack_tx_word(data: bytes) -> int:
    """Pack up to 4 flash bytes (byte 0 sent first) into a WDATA0 word — the
    inverse of :func:`unpack_rx_bytes`, so a hypothetical read-after-write
    round-trips through the same lane model."""
    n = len(data)
    if not 0 < n <= 4:
        raise ValueError(f"expected 1..4 bytes for one WDATA word, got {n}")
    value = 0
    for i, b in enumerate(data):
        value |= (b & 0xFF) << (8 * (n - 1 - i))
    return byteswap32(value)


# --------------------------------------------------------------------------- #
# Access seam + result dataclasses
# --------------------------------------------------------------------------- #
@runtime_checkable
class AhbAccess(Protocol):
    """The SWD/AHB word-access handle the programmer drives — the subset of
    :class:`pyverify.swd.SwdDebugger` it needs. Any object with these two
    methods works (the tests pass a fake QSPI-controller model)."""

    def read_mem(self, addr: int, count: int = 1) -> List[int]:
        ...

    def write_mem(self, addr: int, data: Union[int, Sequence[int]]) -> object:
        ...  # pragma: no cover

    # OPTIONAL. A transport that can run many ops in ONE round trip should
    # implement this (pyverify.swd.SwdDebugger does). QspiFlashProgrammer
    # detects it and collapses each SPI command's ~7 register accesses into a
    # single spawn; without it the one-op-per-round-trip path is used unchanged.
    def batch_ops(self, ops: Sequence[tuple]) -> list:
        ...


class QspiFlashError(Exception):
    """A programming step failed loudly: wrong JEDEC ID (not an
    SST26VF064B), a read-back verify mismatch, or the controller never went
    un-BUSY. Never raised for a merely-slow op — only for a real fault."""


@dataclass(frozen=True)
class JedecId:
    """A decoded ``0x9F`` JEDEC-ID read."""

    mfg: int
    mem_type: int
    dev_id: int
    raw_rdata0: int

    @property
    def is_sst26vf064b(self) -> bool:
        return (
            self.mfg == SST26_MFG_ID
            and self.mem_type == SST26_DEV_TYPE
            and self.dev_id == SST26_DEV_ID
        )

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"JEDEC mfg=0x{self.mfg:02X} type=0x{self.mem_type:02X} dev=0x{self.dev_id:02X}"


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of a read-back verify. ``ok`` True means every byte matched;
    otherwise ``mismatch_offset`` names the first differing flash offset."""

    ok: bool
    length: int
    mismatch_offset: Optional[int] = None
    expected: Optional[int] = None
    actual: Optional[int] = None


@dataclass(frozen=True)
class ProgramResult:
    """What a full :meth:`QspiFlashProgrammer.program_image` did."""

    jedec: JedecId
    bytes_programmed: int
    sectors_erased: int
    pages_programmed: int
    verify: Optional[VerifyResult]


# --------------------------------------------------------------------------- #
# The programmer
# --------------------------------------------------------------------------- #
class QspiFlashProgrammer:
    """Drive the DUT's ``ahb_qspi`` controller to program the on-board
    SST26VF064B, given an SWD/AHB word-access ``handle`` (:class:`AhbAccess`).

    The M0 must already be halted (so the host owns the controller registers);
    the caller halts before and resumes/resets after — see ``pyverify.cli``'s
    ``flash`` verb. Every method fails loudly (:class:`QspiFlashError`) on an
    ID or verify mismatch, or a stuck controller.
    """

    def __init__(
        self,
        handle: AhbAccess,
        *,
        ctrl_base: int = QSPI_CTRL_BASE,
        xip_base: int = QSPI_XIP_BASE,
        busy_poll_limit: int = 100_000,
        wip_poll_limit: int = 1_000_000,
        clk_div: int = CLK_DIV_MIN,
    ) -> None:
        if not CLK_DIV_MIN <= clk_div <= CLK_DIV_MAX:
            raise ValueError(
                f"clk_div={clk_div} out of range [{CLK_DIV_MIN}, {CLK_DIV_MAX}]. "
                f"CLK_DIV=0 is a raw-HCLK bypass and 1-3 exceed the SST26VF064B "
                f"AC spec (CLK_DIV=1 is the reset value and is the known cause of "
                f"garbled RDID on silicon)."
            )
        self.handle = handle
        self.ctrl_base = ctrl_base
        self.xip_base = xip_base
        self.busy_poll_limit = busy_poll_limit
        self.wip_poll_limit = wip_poll_limit
        self.clk_div = clk_div

    # -- raw register access ------------------------------------------------ #
    def _rd(self, offset: int) -> int:
        return self.handle.read_mem(self.ctrl_base + offset, 1)[0] & 0xFFFF_FFFF

    def _wr(self, offset: int, value: int) -> None:
        self.handle.write_mem(self.ctrl_base + offset, value & 0xFFFF_FFFF)

    # -- the two-phase SPI command transaction ------------------------------ #
    def _spi_cmd(
        self,
        opcode: int,
        *,
        read: bool = False,
        write: bool = False,
        addr: Optional[int] = None,
        n_bytes: int = 0,
        wdata: bytes = b"",
        dummy: int = 0,
    ) -> Optional[bytes]:
        """One SST26 command via reg2 (SPI_CMD), mirroring
        ``qspi_cmd_sequence.sv``: set ADDR/WDATA, write SPI_CMD with ENABLE=0
        then ENABLE=1, poll STATUS.BUSY, read RDATA, clear IRQ.

        Returns the ``n_bytes`` read bytes when ``read``, else ``None``.
        ``n_bytes`` is capped at 4 (one RDATA/WDATA word) — see the module
        docstring's board-unproven note on the wider lane packing.
        """
        if n_bytes > 4:
            raise ValueError("this tool transfers <=4 data bytes per command")

        # The command word (reg2) — computed first so the batched and
        # one-at-a-time paths are driven from exactly the same value.
        addr_en = addr is not None
        n_rw = (n_bytes - 1) if n_bytes > 0 else 0  # N_RW field is (bytes-1)
        cmd_base = (
            (opcode & 0xFF)
            | (CMD_READ if read else 0)
            | (CMD_WRITE if write else 0)
            | (CMD_ADDR_EN if addr_en else 0)
            | ((dummy & 0xF) << CMD_DUMMY_SHIFT)
            | ((n_rw & 0xF) << CMD_NRW_SHIFT)
        )

        # Whole-command batch when the transport supports it (must be decided
        # BEFORE any register is written, or the batch re-issues writes the
        # sequential prologue already made — caught by the identical-traffic test).
        batch = getattr(self.handle, "batch_ops", None)
        if batch is not None:
            return self._spi_cmd_batched(
                batch, opcode, cmd_base, addr_en, addr, write, wdata, read, n_bytes,
            )

        # 1. flash address (reg3) — only the low 22 bits reach the controller.
        if addr_en:
            self._wr(REG_ADDR, addr & FLASH_ADDR_MASK)

        # 2. write data (reg8) for a program transfer.
        if write and wdata:
            self._wr(REG_WDATA0, pack_tx_word(wdata))

        # 3. two-phase SPI_CMD (reg2): setup (ENABLE=0) then trigger (ENABLE=1).
        self._wr(REG_SPI_CMD, cmd_base)                # phase 1: setup
        self._wr(REG_SPI_CMD, cmd_base | CMD_ENABLE)   # phase 2: trigger

        # 4. poll STATUS.BUSY until the transaction completes.
        self._wait_not_busy(opcode)

        # 5. read back RDATA0 if this was a read.
        result: Optional[bytes] = None
        if read and n_bytes > 0:
            result = unpack_rx_bytes(self._rd(REG_RDATA0), n_bytes)

        # 6. clear the finished-IRQ (STATUS.IRQ_CLR, W1C).
        self._wr(REG_STATUS, STATUS_IRQ_CLR)
        return result

    def _spi_cmd_batched(
        self, batch, opcode, cmd_base, addr_en, addr, write, wdata, read, n_bytes,
    ) -> Optional[bytes]:
        """:meth:`_spi_cmd` as ONE transport round trip.

        Emits exactly the same register accesses, in the same order, as the
        one-at-a-time path -- OpenOCD executes its ``-c`` ops sequentially. The
        BUSY read is *optimistic*: at CLK_DIV>=4 an SST26 command completes in
        microseconds, while one round trip costs ~75 ms, so BUSY is clear by the
        time the batch's STATUS read executes in every realistic case. If it is
        NOT clear we fall back to polling -- WITHOUT re-triggering the command
        (it has already been issued; re-issuing would double-program).

        Measured payoff: ~7 spawns -> 1, i.e. ~0.75 s -> ~0.1 s per SPI command.
        """
        ops: "list[tuple]" = []
        if addr_en:
            ops.append(("w", self.ctrl_base + REG_ADDR, addr & FLASH_ADDR_MASK))
        if write and wdata:
            ops.append(("w", self.ctrl_base + REG_WDATA0, pack_tx_word(wdata)))
        ops.append(("w", self.ctrl_base + REG_SPI_CMD, cmd_base))
        ops.append(("w", self.ctrl_base + REG_SPI_CMD, cmd_base | CMD_ENABLE))
        status_idx = len(ops)
        ops.append(("r", self.ctrl_base + REG_STATUS, 1))
        rdata_idx = None
        if read and n_bytes > 0:
            rdata_idx = len(ops)
            ops.append(("r", self.ctrl_base + REG_RDATA0, 1))
        ops.append(("w", self.ctrl_base + REG_STATUS, STATUS_IRQ_CLR))

        res = batch(ops)
        status = res[status_idx][0] & 0xFFFF_FFFF

        if status & STATUS_BUSY:
            # Rare: the controller was still busy when the batched STATUS read
            # landed. The command is already in flight -- just wait it out and
            # re-read, never re-trigger.
            self._wait_not_busy(opcode)
            result = (
                unpack_rx_bytes(self._rd(REG_RDATA0), n_bytes)
                if (read and n_bytes > 0) else None
            )
            self._wr(REG_STATUS, STATUS_IRQ_CLR)
            return result

        if rdata_idx is None:
            return None
        return unpack_rx_bytes(res[rdata_idx][0] & 0xFFFF_FFFF, n_bytes)

    def _wait_not_busy(self, opcode: int) -> None:
        for _ in range(self.busy_poll_limit):
            if not (self._rd(REG_STATUS) & STATUS_BUSY):
                return
        raise QspiFlashError(
            f"QSPI controller stuck BUSY after opcode 0x{opcode:02X} "
            f"({self.busy_poll_limit} polls) — STATUS.BUSY never cleared"
        )

    # -- clock -------------------------------------------------------------- #
    def apply_clk_div(self) -> int:
        """Program CLK_DIV and read it back, returning the read-back value.

        MUST run before the first transaction on a freshly-reset controller:
        CLK_DIV resets to 1, which garbles RDID on real hardware. The read-back
        is the point -- the bring-up doc's instruction is to *read back CLK_DIV
        before trusting any flash transaction*, since a wedged or absent
        register block otherwise shows up later as an unexplained ID mismatch.
        """
        self._wr(REG_CLK_DIV, self.clk_div)
        got = self._rd(REG_CLK_DIV) & CLK_DIV_MAX
        if got != self.clk_div:
            raise QspiFlashError(
                f"CLK_DIV read-back mismatch: wrote {self.clk_div}, read {got} "
                f"(reg13 @ 0x{self.ctrl_base + REG_CLK_DIV:08X}). The controller "
                f"register block is not responding as expected -- suspect the RP "
                f"is not loaded with a QSPI-bearing RM, the DUT is held in reset, "
                f"or the AHB aperture is wrong. Do NOT erase anything until this "
                f"reads back clean."
            )
        return got

    # -- SST26 primitives --------------------------------------------------- #
    def read_jedec_id(self) -> JedecId:
        """Issue ``0x9F`` and decode (mfg, type, dev_id) from RDATA0."""
        raw = self._raw_jedec_rdata0()
        mfg, mem_type, dev_id = unpack_rx_bytes(raw, 3)
        return JedecId(mfg=mfg, mem_type=mem_type, dev_id=dev_id,
                       raw_rdata0=raw & 0xFFFF_FFFF)

    def _raw_jedec_rdata0(self) -> int:
        """Run the ``0x9F`` transaction and return the raw RDATA0 word (the
        controller-native, byte-swapped form — e.g. 0x4326BF00)."""
        self._spi_cmd(OP_JEDEC_ID, read=True, n_bytes=3)
        return self._rd(REG_RDATA0)

    def assert_sst26vf064b(self) -> JedecId:
        """Read the JEDEC ID and fail loudly unless it is an SST26VF064B.

        Programs CLK_DIV first: at the reset value (1) the RDID read itself
        garbles, so asserting the ID on an unconfigured controller reports a
        bogus "wrong part" instead of the real "clock too fast".
        """
        self.apply_clk_div()
        jedec = self.read_jedec_id()
        if not jedec.is_sst26vf064b:
            raise QspiFlashError(
                f"JEDEC ID mismatch: read {jedec} (RDATA0=0x{jedec.raw_rdata0:08X}); "
                f"expected SST26VF064B mfg=0x{SST26_MFG_ID:02X} type=0x{SST26_DEV_TYPE:02X} "
                f"dev=0x{SST26_DEV_ID:02X}. Wrong part, dead controller, or the M0 "
                f"is not halted and is racing the register block."
            )
        return jedec

    def write_enable(self) -> None:
        """WREN (0x06) — sets the flash WEL. Auto-clears after each
        program/erase, so it precedes *every* write op."""
        self._spi_cmd(OP_WREN)

    def read_status(self) -> int:
        """RDSR (0x05) — the flash status byte (WIP=bit0, WEL=bit1)."""
        rx = self._spi_cmd(OP_RDSR, read=True, n_bytes=1)
        assert rx is not None
        return rx[0]

    def unlock_global(self) -> None:
        """WREN then ULBPR (0x98) — clear the global block-protection so
        erase/program can take effect. SST26 boots fully write-protected
        (docs/MPS3_ONBOARD_FLASH_MAPPING.md §5); skipping this makes every
        subsequent erase/program a silent no-op (which verify then catches)."""
        self.write_enable()
        self._spi_cmd(OP_ULBPR)

    def _wait_wip_clear(self) -> None:
        for _ in range(self.wip_poll_limit):
            if not (self.read_status() & SR_WIP):
                return
        raise QspiFlashError(
            f"flash WIP never cleared after {self.wip_poll_limit} RDSR polls "
            f"— program/erase did not finish"
        )

    def sector_erase(self, addr: int) -> None:
        """WREN + sector-erase (0x20, 4 KB) of the sector containing ``addr``,
        then poll WIP to completion."""
        self.write_enable()
        self._spi_cmd(OP_SECTOR_ERASE, addr=addr & ~(SECTOR_SIZE - 1))
        self._wait_wip_clear()

    def erase_range(self, base: int, length: int) -> int:
        """Sector-erase every 4 KB sector spanning ``[base, base+length)``.
        Returns the number of sectors erased."""
        if length <= 0:
            return 0
        first = base & ~(SECTOR_SIZE - 1)
        last = (base + length - 1) & ~(SECTOR_SIZE - 1)
        count = 0
        for sector in range(first, last + 1, SECTOR_SIZE):
            self.sector_erase(sector)
            count += 1
        return count

    def _program_page(self, page_addr: int, data: bytes) -> None:
        """Program up to one 256 B page (``data`` must not cross the page
        boundary). Each <=4-byte sub-transfer is its own WREN + page-program
        (0x02) at an incrementing address, then WIP-polled — the register
        model transfers <=4 data bytes per command (module docstring)."""
        if not data:
            return
        page_base = page_addr & ~(PAGE_SIZE - 1)
        if page_addr - page_base + len(data) > PAGE_SIZE:
            raise ValueError(
                f"program at 0x{page_addr:06X} len {len(data)} crosses the "
                f"256 B page boundary at 0x{page_base + PAGE_SIZE:06X}"
            )
        for off in range(0, len(data), 4):
            chunk = data[off : off + 4]
            self.write_enable()
            self._spi_cmd(OP_PP, write=True, addr=page_addr + off,
                          n_bytes=len(chunk), wdata=chunk)
            self._wait_wip_clear()

    def program(self, base: int, data: bytes) -> int:
        """Program ``data`` starting at flash offset ``base``, split on 256 B
        page boundaries. The caller must have erased the range first. Returns
        the number of pages touched."""
        if not data:
            return 0
        pages = 0
        pos = base
        end = base + len(data)
        while pos < end:
            page_end = (pos & ~(PAGE_SIZE - 1)) + PAGE_SIZE
            stop = min(page_end, end)
            self._program_page(pos, data[pos - base : stop - base])
            pages += 1
            pos = stop
        return pages

    def read_back(self, base: int, length: int) -> bytes:
        """Read ``length`` bytes from flash offset ``base`` via the ``0x03``
        read command (no dummy cycles), 4 bytes per transfer."""
        out = bytearray()
        pos = base
        remaining = length
        while remaining > 0:
            n = min(4, remaining)
            rx = self._spi_cmd(OP_READ, read=True, addr=pos, n_bytes=n)
            assert rx is not None
            out.extend(rx)
            pos += n
            remaining -= n
        return bytes(out)

    def verify(self, base: int, expected: bytes) -> VerifyResult:
        """Read ``base..base+len(expected)`` back and compare byte-for-byte."""
        actual = self.read_back(base, len(expected))
        for i, (e, a) in enumerate(zip(expected, actual)):
            if e != a:
                return VerifyResult(
                    ok=False, length=len(expected),
                    mismatch_offset=base + i, expected=e, actual=a,
                )
        return VerifyResult(ok=True, length=len(expected))

    # -- the full flow ------------------------------------------------------ #
    def program_image(
        self,
        image: bytes,
        *,
        base: int = 0,
        erase: bool = True,
        do_verify: bool = True,
    ) -> ProgramResult:
        """Full path: assert the JEDEC ID, unlock (WREN+ULBPR), erase the
        needed sectors, page-program the whole ``image`` at ``base``, then
        read-back-verify. Raises :class:`QspiFlashError` on an ID mismatch or
        (when ``do_verify``) any verify mismatch."""
        if base + len(image) > FLASH_CAPACITY:
            raise ValueError(
                f"image ({len(image)} B @ 0x{base:X}) exceeds the controller's "
                f"{FLASH_CAPACITY // (1024 * 1024)} MB addressable aperture"
            )
        jedec = self.assert_sst26vf064b()
        self.unlock_global()
        sectors = self.erase_range(base, len(image)) if erase else 0
        pages = self.program(base, image)
        result: Optional[VerifyResult] = None
        if do_verify:
            result = self.verify(base, image)
            if not result.ok:
                raise QspiFlashError(
                    f"verify FAILED at flash 0x{result.mismatch_offset:06X}: "
                    f"expected 0x{result.expected:02X}, read 0x{result.actual:02X}. "
                    f"Programmed {pages} pages after {sectors} sector-erases — a "
                    f"silent-failure guard (e.g. missing ULBPR unlock, un-erased "
                    f"sector, or the M0 racing the controller)."
                )
        return ProgramResult(
            jedec=jedec, bytes_programmed=len(image),
            sectors_erased=sectors, pages_programmed=pages, verify=result,
        )

    def verify_image(self, image: bytes, *, base: int = 0) -> VerifyResult:
        """Read-back-verify an already-flashed ``image`` (no erase/program).
        Asserts the JEDEC ID first; raises on a verify mismatch."""
        self.assert_sst26vf064b()
        result = self.verify(base, image)
        if not result.ok:
            raise QspiFlashError(
                f"verify FAILED at flash 0x{result.mismatch_offset:06X}: "
                f"expected 0x{result.expected:02X}, read 0x{result.actual:02X}"
            )
        return result
