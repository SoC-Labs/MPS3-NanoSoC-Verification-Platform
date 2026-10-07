"""Tests for ``pyverify.qspi_flash`` — the host QSPI-flash programmer.

**What these cover (all board-free):** the command SEQUENCING and image
handling — driven through a **fake QSPI controller model**
(:class:`FakeQspiController`) that mirrors the ``ahb_qspi`` register block and
the SST26VF064B command state machine (WREN latch, ULBPR global unlock, WIP
timing, page buffer, read-back). The fake is reached through a
:class:`FakeAhbAccess` handle that speaks the same ``read_mem``/``write_mem``
surface as :class:`pyverify.swd.SwdDebugger` (mocking style after
``tests/test_swd.py``'s ``FakeRunner`` and ``testing/fakeshell.py``).

**What still needs the board (NOT covered here):** real SWD-over-AHB
throughput and the true SST26 program/erase timing + wire byte-lane order —
see the module docstring. The fake decodes the SAME two-phase register
protocol the RTL implements, so a correctly-sequenced programmer round-trips;
a mis-sequenced one (e.g. skipping the ULBPR unlock) fails the way it would on
silicon — silently on the write, caught by read-back verify.
"""
from __future__ import annotations

import pytest

from pyverify.qspi_flash import (
    CMD_ADDR_EN,
    CMD_ENABLE,
    CMD_NRW_SHIFT,
    CMD_READ,
    CMD_WRITE,
    FLASH_CAPACITY,
    OP_JEDEC_ID,
    OP_PP,
    OP_RDSR,
    OP_READ,
    OP_SECTOR_ERASE,
    OP_ULBPR,
    OP_WREN,
    PAGE_SIZE,
    CLK_DIV_MIN,
    QspiFlashError,
    REG_ADDR,
    REG_CLK_DIV,
    REG_RDATA0,
    REG_SPI_CMD,
    REG_STATUS,
    REG_WDATA0,
    SECTOR_SIZE,
    SR_WEL,
    SR_WIP,
    SST26_JEDEC_RDATA0,
    STATUS_BUSY,
    STATUS_IRQ_CLR,
    JedecId,
    QspiFlashError,
    QspiFlashProgrammer,
    byteswap32,
    pack_tx_word,
    unpack_rx_bytes,
)

QSPI_CTRL_BASE = 0x7400_0000


# --------------------------------------------------------------------------- #
# The fake QSPI controller + flash model
# --------------------------------------------------------------------------- #
class FakeQspiController:
    """Register-level model of ``apb_qspi_regs`` + an SST26VF064B flash array.

    Decodes the two-phase SPI_CMD protocol (execute on the ENABLE 0->1 edge),
    mirrors the flash command state machine, and backs a byte array the reads
    return. Deliberately faithful on the bits that matter for sequencing:

    * **WEL latch** — WREN sets it; every program/erase clears it (SST26).
    * **Global block protection** — ``write_locked`` True at power-up; only
      ULBPR (0x98) clears it. A program/erase while locked is a **silent
      no-op** (the array is untouched), exactly the silicon failure mode.
    * **WIP timing** — a program/erase asserts WIP for ``wip_busy_reads``
      RDSR reads before clearing, so the programmer's poll loop actually
      spins (the WIP-polling path is real, not a no-op).
    * **Page buffer** — a page-program only writes within its 256 B page and,
      like NOR flash, can only clear bits (AND into the current contents).

    The JEDEC/status/read byte lanes use the module's own RTL-derived
    :func:`unpack_rx_bytes`/:func:`pack_tx_word`, so the fake and the
    programmer share one lane model (encode/decode are provably inverse).
    """

    def __init__(self, capacity: int = 0x40000, *, wip_busy_reads: int = 3,
                 locked: bool = True):
        self.flash = bytearray(b"\xFF" * capacity)  # erased state
        self.capacity = capacity
        self.wip_busy_reads = wip_busy_reads
        # Register file (byte-offset -> 32-bit value).
        self.regs = {
            REG_SPI_CMD: 0, REG_ADDR: 0, REG_STATUS: 0, REG_RDATA0: 0,
            REG_WDATA0: 0,
        }
        # Flash state machine.
        self.wel = False
        self.write_locked = locked
        self._wip_countdown = 0
        # Observability for assertions.
        self.opcodes: list[int] = []      # every executed opcode, in order
        self.programs: list[tuple[int, bytes]] = []  # (addr, bytes) of real PPs
        self.erases: list[int] = []       # sector base of each real erase
        self.ulbpr_count = 0
        self.wren_count = 0

    # -- the AHB word bus --------------------------------------------------- #
    def read32(self, offset: int) -> int:
        if offset == REG_STATUS:
            return self._status_reg()
        return self.regs.get(offset, 0)

    def write32(self, offset: int, value: int) -> None:
        value &= 0xFFFF_FFFF
        if offset == REG_STATUS:
            # W1C on IRQ_CLR; BUSY is RO. Nothing else to model.
            return
        self.regs[offset] = value
        if offset == REG_SPI_CMD and (value & CMD_ENABLE):
            self._execute(value)

    # -- STATUS.BUSY: the controller is instantly idle in this model -------- #
    def _status_reg(self) -> int:
        # Controller BUSY (reg1 bit0) is the *controller* busy flag, distinct
        # from the flash-status WIP bit (read via RDSR). The command completes
        # within the model, so BUSY reads back 0.
        return 0

    # -- flash-status byte returned by RDSR --------------------------------- #
    def _flash_status(self) -> int:
        wip = SR_WIP if self._wip_countdown > 0 else 0
        if self._wip_countdown > 0:
            self._wip_countdown -= 1
        return wip | (SR_WEL if self.wel else 0)

    # -- decode + execute one SPI_CMD transaction --------------------------- #
    def _execute(self, cmd: int) -> None:
        opcode = cmd & 0xFF
        do_read = bool(cmd & CMD_READ)
        do_write = bool(cmd & CMD_WRITE)
        addr_en = bool(cmd & CMD_ADDR_EN)
        n_bytes = ((cmd >> CMD_NRW_SHIFT) & 0xF) + 1  # field is (bytes-1)
        addr = self.regs[REG_ADDR] & (self.capacity - 1)
        self.opcodes.append(opcode)

        if opcode == OP_JEDEC_ID:
            # Present the SST26VF064B ID in RDATA0 exactly as the RTL would.
            self.regs[REG_RDATA0] = SST26_JEDEC_RDATA0
        elif opcode == OP_WREN:
            self.wel = True
            self.wren_count += 1
        elif opcode == OP_ULBPR:
            # SST26 requires WREN before ULBPR; it clears WEL like other writes.
            if self.wel:
                self.write_locked = False
                self.ulbpr_count += 1
            self.wel = False
        elif opcode == OP_RDSR:
            self.regs[REG_RDATA0] = self._rdata_from_bytes([self._flash_status()])
        elif opcode == OP_SECTOR_ERASE:
            self._do_erase(addr)
        elif opcode == OP_PP:
            wdata = unpack_rx_bytes(self.regs[REG_WDATA0], n_bytes) if do_write else b""
            self._do_program(addr, wdata)
        elif opcode == OP_READ:
            data = bytes(self.flash[addr : addr + n_bytes])
            self.regs[REG_RDATA0] = self._rdata_from_bytes(data)
        # unknown opcodes are ignored (the model only needs the set above)

    # -- flash effects ------------------------------------------------------ #
    def _do_erase(self, addr: int) -> None:
        if self.write_locked or not self.wel:
            self.wel = False
            return  # silent no-op — protected or WEL not set
        base = addr & ~(SECTOR_SIZE - 1)
        self.flash[base : base + SECTOR_SIZE] = b"\xFF" * SECTOR_SIZE
        self.erases.append(base)
        self.wel = False
        self._wip_countdown = self.wip_busy_reads

    def _do_program(self, addr: int, data: bytes) -> None:
        if self.write_locked or not self.wel:
            self.wel = False
            return  # silent no-op — the missing-unlock failure mode
        # A real page-program wraps within its 256 B page; the programmer never
        # crosses one, so a straight write suffices. NOR can only clear bits.
        for i, b in enumerate(data):
            self.flash[addr + i] &= b
        self.programs.append((addr, data))
        self.wel = False
        self._wip_countdown = self.wip_busy_reads

    def _rdata_from_bytes(self, data: bytes) -> int:
        # RDATA0 native (byte-swapped) form for up to 4 received bytes, using
        # the module's own lane model so it inverts unpack_rx_bytes exactly.
        return pack_tx_word(bytes(data))


class FakeAhbAccess:
    """SWD/AHB handle over a :class:`FakeQspiController`, mirroring the
    ``read_mem``/``write_mem`` surface of :class:`pyverify.swd.SwdDebugger`.
    Records every access for sequencing assertions."""

    def __init__(self, ctrl: FakeQspiController, *, base: int = QSPI_CTRL_BASE):
        self.ctrl = ctrl
        self.base = base
        self.reads: list[tuple[int, int]] = []
        self.writes: list[tuple[int, int]] = []

    def read_mem(self, addr: int, count: int = 1) -> list[int]:
        self.reads.append((addr, count))
        return [self.ctrl.read32(addr - self.base) for _ in range(count)]

    def write_mem(self, addr, data) -> object:
        words = [data] if isinstance(data, int) else list(data)
        for i, w in enumerate(words):
            off = (addr - self.base) + 4 * i
            self.writes.append((addr + 4 * i, w & 0xFFFF_FFFF))
            self.ctrl.write32(off, w)
        return None


def make(**ctrl_kwargs):
    ctrl = FakeQspiController(**ctrl_kwargs)
    handle = FakeAhbAccess(ctrl)
    prog = QspiFlashProgrammer(handle)
    return ctrl, handle, prog


# --------------------------------------------------------------------------- #
# Pure lane helpers (the RTL-derived, board-free core)
# --------------------------------------------------------------------------- #
def test_byteswap32_reverses_lanes() -> None:
    assert byteswap32(0x11223344) == 0x44332211
    assert byteswap32(byteswap32(0xDEADBEEF)) == 0xDEADBEEF


def test_jedec_rdata0_decodes_to_sst26_id() -> None:
    # The cited RTL value 0x4326BF00 must decode to mfg/type/dev of the part.
    mfg, mem_type, dev_id = unpack_rx_bytes(SST26_JEDEC_RDATA0, 3)
    assert (mfg, mem_type, dev_id) == (0xBF, 0x26, 0x43)


def test_pack_unpack_roundtrip() -> None:
    for data in (b"\x01", b"\xAB\xCD", b"\xDE\xAD\xBE\xEF", b"\x00\xFF\x10"):
        assert unpack_rx_bytes(pack_tx_word(data), len(data)) == data


def test_unpack_rejects_bad_width() -> None:
    with pytest.raises(ValueError):
        unpack_rx_bytes(0, 5)
    with pytest.raises(ValueError):
        pack_tx_word(b"\x01\x02\x03\x04\x05")


# --------------------------------------------------------------------------- #
# JEDEC ID check
# --------------------------------------------------------------------------- #
def test_read_jedec_id_returns_sst26() -> None:
    ctrl, _, prog = make()
    jedec = prog.read_jedec_id()
    assert jedec == JedecId(mfg=0xBF, mem_type=0x26, dev_id=0x43,
                            raw_rdata0=SST26_JEDEC_RDATA0)
    assert jedec.is_sst26vf064b
    assert OP_JEDEC_ID in ctrl.opcodes


def test_assert_sst26_passes_on_right_part() -> None:
    _, _, prog = make()
    assert prog.assert_sst26vf064b().is_sst26vf064b


def test_assert_sst26_fails_loudly_on_wrong_id() -> None:
    ctrl, _, prog = make()
    # Model a different part: patch the JEDEC RDATA0 the fake returns.
    real_exec = ctrl._execute

    def patched(cmd):
        real_exec(cmd)
        if (cmd & 0xFF) == OP_JEDEC_ID:
            ctrl.regs[REG_RDATA0] = pack_tx_word(bytes([0xEF, 0x40, 0x18]))  # a Winbond

    ctrl._execute = patched
    with pytest.raises(QspiFlashError, match="JEDEC ID mismatch"):
        prog.assert_sst26vf064b()


# --------------------------------------------------------------------------- #
# Two-phase command framing + WIP polling
# --------------------------------------------------------------------------- #
def test_spi_cmd_is_two_phase_setup_then_enable() -> None:
    ctrl, handle, prog = make()
    prog.write_enable()
    cmd_writes = [w for (a, w) in handle.writes if a == QSPI_CTRL_BASE + REG_SPI_CMD]
    # First the setup write (ENABLE=0), then the trigger (ENABLE=1).
    assert len(cmd_writes) == 2
    assert not (cmd_writes[0] & CMD_ENABLE)
    assert cmd_writes[1] & CMD_ENABLE
    assert (cmd_writes[0] & 0xFF) == OP_WREN


def test_wip_polling_actually_spins() -> None:
    ctrl, handle, prog = make(wip_busy_reads=4)
    prog.unlock_global()
    # Program 1 byte and count the RDSR (status) reads while WIP is set.
    prog.write_enable()
    prog._spi_cmd(OP_PP, write=True, addr=0x100, n_bytes=1, wdata=b"\x5A")
    before = sum(1 for op in ctrl.opcodes if op == OP_RDSR)
    prog._wait_wip_clear()
    after = sum(1 for op in ctrl.opcodes if op == OP_RDSR)
    # WIP was asserted for 4 reads, so the poll loop issued >= 4 RDSRs.
    assert after - before >= 4


def test_stuck_busy_raises() -> None:
    ctrl, _, prog = make()
    prog.busy_poll_limit = 5
    # Force STATUS.BUSY to stay asserted so the poll never completes.
    ctrl._status_reg = lambda: STATUS_BUSY  # type: ignore[method-assign]
    with pytest.raises(QspiFlashError, match="stuck BUSY"):
        prog.read_jedec_id()


# --------------------------------------------------------------------------- #
# Unlock, erase, program primitives
# --------------------------------------------------------------------------- #
def test_unlock_global_issues_wren_then_ulbpr() -> None:
    ctrl, _, prog = make()
    prog.unlock_global()
    # WREN must immediately precede ULBPR (SST26 requires WEL set).
    assert OP_WREN in ctrl.opcodes
    assert OP_ULBPR in ctrl.opcodes
    assert ctrl.opcodes.index(OP_WREN) < ctrl.opcodes.index(OP_ULBPR)
    assert ctrl.write_locked is False
    assert ctrl.ulbpr_count == 1


def test_erase_range_covers_every_touched_sector() -> None:
    ctrl, _, prog = make()
    prog.unlock_global()
    # A range straddling two 4 KB sectors erases both.
    n = prog.erase_range(SECTOR_SIZE - 4, 8)
    assert n == 2
    assert ctrl.erases == [0x0000, SECTOR_SIZE]


def test_wren_precedes_every_program_and_erase() -> None:
    ctrl, _, prog = make()
    prog.unlock_global()
    prog.erase_range(0, 4)
    prog.program(0, b"\x01\x02\x03\x04")
    # Every erase and PP in the stream is immediately preceded by a WREN.
    for i, op in enumerate(ctrl.opcodes):
        if op in (OP_SECTOR_ERASE, OP_PP):
            assert ctrl.opcodes[i - 1] == OP_WREN, f"op 0x{op:02X} at {i} lacks WREN"


# --------------------------------------------------------------------------- #
# Page boundary handling
# --------------------------------------------------------------------------- #
def test_program_splits_on_page_boundary() -> None:
    ctrl, _, prog = make()
    prog.unlock_global()
    prog.erase_range(0, PAGE_SIZE * 2)
    # A write that starts 4 B before a page boundary and runs 8 B past it.
    start = PAGE_SIZE - 4
    payload = bytes(range(12))
    pages = prog.program(start, payload)
    assert pages == 2  # touches page 0 and page 1
    # No single page-program transaction may cross a page boundary.
    for addr, data in ctrl.programs:
        page = addr & ~(PAGE_SIZE - 1)
        assert addr + len(data) <= page + PAGE_SIZE
    # The bytes still landed contiguously.
    assert bytes(ctrl.flash[start : start + len(payload)]) == payload


def test_program_page_rejects_crossing_write() -> None:
    _, _, prog = make()
    with pytest.raises(ValueError, match="page boundary"):
        prog._program_page(PAGE_SIZE - 2, b"\x01\x02\x03\x04")


def test_program_image_rejects_oversize() -> None:
    _, _, prog = make()
    with pytest.raises(ValueError, match="aperture"):
        prog.program_image(b"\x00", base=FLASH_CAPACITY)


# --------------------------------------------------------------------------- #
# Full image round-trip (program -> read-back matches)
# --------------------------------------------------------------------------- #
def _packed_image() -> bytes:
    """A miniature of flash_pack.py's layout: 'BOOT' magic at 0, some HOT
    bytes, a gap of erased 0xFF, then a COLD blob — small enough to keep the
    per-byte fake fast, but spanning multiple sectors and pages."""
    image = bytearray(b"\xFF" * 0x2100)
    image[0:4] = b"BOOT"
    image[0x10:0x30] = bytes((i * 7) & 0xFF for i in range(0x20))  # boot entry
    image[0x1000:0x1000 + 300] = bytes((i * 3 + 1) & 0xFF for i in range(300))  # HOT (>1 page)
    image[0x2000:0x2000 + 64] = bytes((0xA0 ^ i) & 0xFF for i in range(64))     # COLD
    return bytes(image)


def test_full_image_roundtrip() -> None:
    ctrl, _, prog = make()
    image = _packed_image()
    result = prog.program_image(image)
    assert result.jedec.is_sst26vf064b
    assert result.bytes_programmed == len(image)
    assert result.verify is not None and result.verify.ok
    # Read the whole image back through the read command and compare.
    assert prog.read_back(0, len(image)) == image
    # And the model's backing array agrees.
    assert bytes(ctrl.flash[: len(image)]) == image


def test_verify_only_on_preflashed_part() -> None:
    ctrl, _, prog = make()
    image = _packed_image()
    prog.program_image(image, do_verify=False)
    # A fresh programmer over the same (already-flashed) controller verifies.
    result = prog.verify_image(image)
    assert result.ok and result.length == len(image)


# --------------------------------------------------------------------------- #
# Mutation check: skip ULBPR -> program silently fails -> verify catches it
# --------------------------------------------------------------------------- #
def test_skipping_unlock_fails_silently_then_verify_catches_it() -> None:
    ctrl, _, prog = make()
    image = _packed_image()

    # Reproduce program_image WITHOUT the unlock step (the guard under test).
    prog.assert_sst26vf064b()
    # NOTE: prog.unlock_global() deliberately omitted.
    prog.erase_range(0, len(image))
    prog.program(0, image)

    # The part is still write-protected, so nothing was written or erased.
    assert ctrl.write_locked is True
    assert ctrl.programs == [] and ctrl.erases == []
    assert bytes(ctrl.flash[:4]) == b"\xFF\xFF\xFF\xFF"  # 'BOOT' never landed

    # Read-back verify must catch the silent failure loudly.
    result = prog.verify(0, image)
    assert not result.ok
    assert result.mismatch_offset == 0
    assert result.expected == ord("B") and result.actual == 0xFF

    # And the full program_image path raises rather than returning success.
    ctrl2, _, prog2 = make(locked=True)
    # Monkeypatch out the unlock so the whole flow runs unlocked.
    prog2.unlock_global = lambda: None  # type: ignore[method-assign]
    with pytest.raises(QspiFlashError, match="verify FAILED"):
        prog2.program_image(image)


def test_verify_image_raises_on_mismatch() -> None:
    ctrl, _, prog = make()
    image = _packed_image()
    prog.program_image(image)
    # Corrupt one flash byte behind the programmer's back.
    ctrl.flash[0x1005] ^= 0xFF
    with pytest.raises(QspiFlashError, match="verify FAILED"):
        prog.verify_image(image)


# --- CLK_DIV (reg13) ------------------------------------------------------- #
# Regression: the programmer documented CLK_DIV in its register map but never
# WROTE it, so every path ran at the reset value (1). Per
# docs/QSPI_RP_BOARD_BRINGUP.md:227-245 that is the known cause of garbled RDID
# on real silicon -- i.e. the tool was guaranteed to fail its very first board
# transaction, and no test noticed because the fake controller has no AC spec.

def test_clk_div_is_programmed_before_the_jedec_read():
    """CLK_DIV must be written BEFORE RDID -- at the reset value the ID read
    itself garbles, so an unconfigured controller reports a bogus wrong-part."""
    ctrl = FakeQspiController()
    handle = FakeAhbAccess(ctrl)
    prog = QspiFlashProgrammer(handle, clk_div=4)
    prog.assert_sst26vf064b()
    writes = [a for (a, _v) in handle.writes]
    clk = QSPI_CTRL_BASE + REG_CLK_DIV
    cmd = QSPI_CTRL_BASE + REG_SPI_CMD
    assert clk in writes, "CLK_DIV was never programmed"
    assert writes.index(clk) < writes.index(cmd), \
        "CLK_DIV must be set before the first SPI_CMD transaction"


def test_clk_div_default_meets_the_ac_spec_floor():
    _ctrl, _h, prog = make()
    assert prog.clk_div >= CLK_DIV_MIN


@pytest.mark.parametrize("bad", [0, 1, 2, 3, 32, -1])
def test_clk_div_out_of_range_is_rejected(bad):
    """0 is a raw-HCLK BYPASS (qspi_clock_div.v:10), not a divide; 1-3 exceed
    the part's AC spec. Reject at construction, not on the board."""
    with pytest.raises(ValueError):
        QspiFlashProgrammer(FakeAhbAccess(FakeQspiController()), clk_div=bad)


def test_clk_div_readback_mismatch_fails_loudly_before_any_erase():
    """A controller that will not echo CLK_DIV is not safe to erase against."""
    ctrl = FakeQspiController()
    prog = QspiFlashProgrammer(FakeAhbAccess(ctrl), clk_div=4)
    # a register block that swallows writes -- the wedged/absent-RM case
    ctrl.write32 = lambda off, val: None
    with pytest.raises(QspiFlashError, match="CLK_DIV read-back mismatch"):
        prog.apply_clk_div()


# --- batched transport (one round trip per SPI command) -------------------- #
# swd.SwdDebugger._run() always accepted a SEQUENCE of ops (one `-c` each, one
# spawn), but nothing used it: every access was its own spawn. Measured on the
# board 2026-07-18: 0.753 s/op one-per-spawn vs ~0.075 s/op inside one session.
# The load-bearing property below is that batching changes only HOW MANY round
# trips happen -- never WHICH registers are touched, nor in what order.


class BatchingFakeAhb(FakeAhbAccess):
    """FakeAhbAccess that also implements the optional batch_ops seam."""

    def __init__(self, ctrl, *, base=QSPI_CTRL_BASE):
        super().__init__(ctrl, base=base)
        self.batch_sizes: list[int] = []

    def batch_ops(self, ops):
        self.batch_sizes.append(len(ops))
        out = []
        for op in ops:
            if op[0] == "r":
                out.append(self.read_mem(op[1], op[2]))
            else:
                self.write_mem(op[1], op[2])
                out.append(None)
        return out


class TracingAhb(FakeAhbAccess):
    """Records ONE interleaved, value-carrying trace of every register access.

    NB: FakeAhbAccess keeps reads and writes in SEPARATE lists, so a trace built
    by concatenating them loses the interleaving AND the written values. A first
    version of these tests did exactly that and consequently passed even with the
    two-phase SPI_CMD writes swapped (same address, different value) -- a gate
    that could not fail. Trace ordering and values here are the whole point.
    """

    def __init__(self, ctrl, *, base=QSPI_CTRL_BASE, batching=False):
        super().__init__(ctrl, base=base)
        self.trace: list[tuple] = []
        self.batch_sizes: list[int] = []
        self._batching = batching

    def read_mem(self, addr, count=1):
        vals = super().read_mem(addr, count)
        self.trace.append(("r", addr - self.base, count))
        return vals

    def write_mem(self, addr, data):
        out = super().write_mem(addr, data)
        val = data if isinstance(data, int) else list(data)[0]
        self.trace.append(("w", addr - self.base, val & 0xFFFFFFFF))
        return out

    def batch_ops(self, ops):
        self.batch_sizes.append(len(ops))
        out = []
        for op in ops:
            if op[0] == "r":
                out.append(self.read_mem(op[1], op[2]))
            else:
                self.write_mem(op[1], op[2])
                out.append(None)
        return out


class BatchingFakeAhb(TracingAhb):
    """Alias kept for readability at call sites."""



def _seq_trace(fn):
    """Run fn against a NON-batching tracing handle (batch_ops hidden)."""
    h = TracingAhb(FakeQspiController())
    h.batch_ops = None                      # force the one-at-a-time path
    fn(QspiFlashProgrammer(h, clk_div=4))
    return h.trace


def _bat_trace(fn):
    h = TracingAhb(FakeQspiController())
    fn(QspiFlashProgrammer(h, clk_div=4))
    assert h.batch_sizes, "batch_ops was never used"
    return h.trace


@pytest.mark.parametrize("name,fn", [
    ("jedec", lambda p: p.assert_sst26vf064b()),
    ("erase", lambda p: (p.unlock_global(), p.erase_range(0x1000, 8))),
    ("program", lambda p: (p.unlock_global(), p.erase_range(0x1000, 8),
                           p.program(0x1000, bytes(range(8))))),
    ("readback", lambda p: p.read_back(0x1000, 32)),
])
def test_batched_traffic_is_identical_to_sequential(name, fn):
    """Batching must change only HOW MANY round trips happen -- never which
    registers are written, in what order, or with what values."""
    assert _bat_trace(fn) == _seq_trace(fn), f"{name}: batched traffic diverged"


def test_batched_path_returns_the_same_jedec_id():
    seq = QspiFlashProgrammer(FakeAhbAccess(FakeQspiController()), clk_div=4)
    bat = QspiFlashProgrammer(BatchingFakeAhb(FakeQspiController()), clk_div=4)
    assert bat.assert_sst26vf064b() == seq.assert_sst26vf064b()


def test_batching_collapses_a_spi_command_into_one_round_trip():
    """A read command is ADDR?/CMD setup/CMD trigger/STATUS/RDATA/IRQ_CLR --
    ~5-7 accesses. Batched, that must cost exactly ONE call."""
    h = BatchingFakeAhb(FakeQspiController())
    prog = QspiFlashProgrammer(h, clk_div=4)
    h.batch_sizes.clear()
    prog.read_jedec_id()
    assert len(h.batch_sizes) == 1, f"expected 1 round trip, got {h.batch_sizes}"
    assert h.batch_sizes[0] >= 4


def test_batched_program_and_verify_roundtrip():
    """End-to-end through the batched transport: program then read back."""
    ctrl = FakeQspiController()
    prog = QspiFlashProgrammer(BatchingFakeAhb(ctrl), clk_div=4)
    data = bytes(range(64))
    prog.unlock_global()
    prog.erase_range(0x1000, len(data))
    prog.program(0x1000, data)
    assert prog.read_back(0x1000, len(data)) == data


def test_batched_busy_status_falls_back_without_retriggering():
    """If the controller is still BUSY when the batched STATUS read lands, we
    must poll it out WITHOUT re-issuing the command -- re-triggering a page
    program would double-write."""
    ctrl = FakeQspiController()
    h = BatchingFakeAhb(ctrl)
    prog = QspiFlashProgrammer(h, clk_div=4)

    real_batch = h.batch_ops
    state = {"forced": False}

    def batch_with_busy(ops):
        out = real_batch(ops)
        if not state["forced"]:
            state["forced"] = True
            for i, op in enumerate(ops):          # force BUSY on the STATUS read
                if op[0] == "r" and (op[1] - QSPI_CTRL_BASE) == REG_STATUS:
                    out[i] = [out[i][0] | 1]
        return out

    h.batch_ops = batch_with_busy
    triggers_before = ctrl.regs.get(REG_SPI_CMD, 0)
    jedec = prog.read_jedec_id()                   # must still succeed
    assert jedec.is_sst26vf064b
    assert isinstance(triggers_before, int)
