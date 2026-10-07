"""Board-free tests for the M0 flash-loader host driver.

The fake below models what firmware/qspi_loader/qspi_loader.c actually does with
the mailbox — poll cmd, act, clear cmd, set status — so these exercise the
PROTOCOL (ordering, error decoding, chunking, DUT-side CRC), not just the
driver's own bookkeeping. The loader firmware is now PROVEN on silicon (commits
b63da06, fce26bb), but NOT by these tests — they are board-free, so they exercise
the protocol only; the silicon proof lives in scripts/qspi_loader_bringup.py runs.
"""
from __future__ import annotations

import binascii

import pytest

from pyverify.qspi_loader import (
    BUFFER_ADDR,
    BUFFER_SIZE,
    CMD_ERASE,
    CMD_PROGRAM,
    CMD_UNLOCK,
    LOADER_LOAD_ADDR,
    MAGIC_VALUE,
    MB_CMD,
    MB_CRC,
    MB_LENGTH,
    MB_MAGIC,
    MB_OFFSET,
    MB_STATUS,
    M0FlashLoader,
    LoaderError,
    NVIC_ICER0,
    ST_ERR,
    ST_OK,
    STACK_TOP,
    SYSTICK_CTRL,
)

FLASH_SIZE = 0x80_0000
SECTOR = 4096


class FakeDut:
    """SwdDebugger-shaped fake that runs the loader's mailbox state machine."""

    def __init__(self, *, come_up: bool = True, fail_code: int = 0):
        self.mem: dict[int, int] = {}
        self.flash = bytearray(b"\xFF" * FLASH_SIZE)
        self.staged: dict[int, bytes] = {}      # addr -> bytes from load_image
        self.come_up = come_up
        self.fail_code = fail_code
        self.entered = False
        self.ops: list[str] = []
        self.unlocked = False
        self.halted = False

    # -- SwdDebugger surface ------------------------------------------------ #
    def halt(self):
        self.halted = True

    def read_mem(self, addr, count=1):
        return [self.mem.get(addr + 4 * i, 0) for i in range(count)]

    def write_mem(self, addr, data):
        words = [data] if isinstance(data, int) else list(data)
        for i, w in enumerate(words):
            self.mem[addr + 4 * i] = w & 0xFFFFFFFF
        if addr == MB_CMD and words[0] != 0:
            self._run_command(words[0])

    def load_image(self, path, addr=None, **kw):
        self.staged[addr] = self._files[path]
        self.ops.append(f"load_image@{addr:#x}")

    def _run(self, ops, **kw):
        self.ops.extend(ops)
        if any(o.startswith("resume") for o in ops):
            self.entered = True
            if self.come_up:
                self.mem[MB_MAGIC] = MAGIC_VALUE

    # -- the DUT-side mailbox state machine --------------------------------- #
    def _run_command(self, cmd):
        if self.fail_code:
            self.mem[MB_CMD] = 0
            self.mem[MB_STATUS] = ST_ERR | self.fail_code
            return
        off = self.mem.get(MB_OFFSET, 0)
        length = self.mem.get(MB_LENGTH, 0)
        # Mirror the firmware's ERR_BAD_LEN guard (qspi_loader.c do_erase/
        # do_program). Without it a stale length=0 makes the erase loop run
        # ~1M times, so a cmd-ordering regression HANGS the suite instead of
        # failing it — nearly as bad as passing.
        bad = (cmd == CMD_ERASE and length == 0) or (
            cmd == CMD_PROGRAM and (length == 0 or length > BUFFER_SIZE))
        if bad:                                        # erase may span many buffers;
            self.mem[MB_CMD] = 0                       # only PROGRAM is window-limited
            self.mem[MB_STATUS] = ST_ERR | 3           # ERR_BAD_LEN
            return
        if cmd == CMD_UNLOCK:
            self.unlocked = True
        elif cmd == CMD_ERASE:
            if not self.unlocked:                       # silent no-op, like the part
                pass
            else:
                first = off & ~(SECTOR - 1)
                last = (off + length - 1) & ~(SECTOR - 1)
                for a in range(first, last + 1, SECTOR):
                    self.flash[a:a + SECTOR] = b"\xFF" * SECTOR
        elif cmd == CMD_PROGRAM:
            if self.unlocked:
                data = self.staged.get(BUFFER_ADDR, b"")[:length]
                for i, b in enumerate(data):            # NAND-like: only clears bits
                    self.flash[off + i] &= b
        elif cmd == 4:  # CMD_CRC
            crc = binascii.crc32(bytes(self.flash[off:off + length])) & 0xFFFFFFFF
            self.mem[MB_CRC] = crc
        self.mem[MB_CMD] = 0
        self.mem[MB_STATUS] = ST_OK


def make(**kw):
    dut = FakeDut(**kw)
    files: dict[str, bytes] = {}
    dut._files = files

    def stage(data: bytes, name: str) -> str:
        path = f"/hub/tmp/{name}"
        files[path] = data
        return path

    ldr = M0FlashLoader(dut, stage, loader_bin=b"\x00" * 756,
                        poll_interval_s=0, command_timeout_s=1.0)
    return dut, ldr


# --- bring-up -------------------------------------------------------------- #

def test_start_quiesces_interrupts_before_entering():
    """swd.load_image's landmine: entering a fresh image without a reset can
    wedge in Default_Handler on the OLD image's armed SysTick/NVIC. Reset-halt
    is unreliable on this DUT, so the driver must disable them explicitly."""
    dut, ldr = make()
    ldr.start()
    assert dut.mem[SYSTICK_CTRL] == 0
    assert dut.mem[NVIC_ICER0] == 0xFFFFFFFF


def test_start_loads_to_imem_and_sets_sp_pc_then_resumes():
    dut, ldr = make()
    ldr.start()
    assert f"load_image@{LOADER_LOAD_ADDR:#x}" in dut.ops
    assert f"reg sp 0x{STACK_TOP:08x}" in dut.ops
    assert f"reg pc 0x{LOADER_LOAD_ADDR:08x}" in dut.ops
    assert dut.ops.index(f"reg pc 0x{LOADER_LOAD_ADDR:08x}") < dut.ops.index("resume")
    assert dut.entered


def test_start_clears_magic_first_so_a_stale_loader_cannot_look_live():
    """If a previous run left MAGIC in IMEM, polling it without clearing would
    report 'live' instantly even if this entry failed."""
    dut, ldr = make(come_up=False)
    dut.mem[MB_MAGIC] = MAGIC_VALUE          # stale from a previous session
    with pytest.raises(LoaderError, match="did not report magic"):
        ldr.start(timeout_s=0.05)


def test_start_without_a_binary_is_refused():
    dut, _ = make()
    with pytest.raises(LoaderError, match="no loader binary"):
        M0FlashLoader(dut, lambda d, n: "/x", loader_bin=None).start()


# --- mailbox protocol ------------------------------------------------------ #

def test_command_writes_cmd_last():
    """The DUT polls cmd and reads offset/length once it sees a non-zero value,
    so cmd MUST be written after them or it acts on stale parameters."""
    dut, ldr = make()
    ldr.start()
    order: list[int] = []
    real = dut.write_mem

    def spy(addr, data):
        order.append(addr)
        real(addr, data)

    dut.write_mem = spy
    ldr.erase(0x100000, 4096)
    assert order.index(MB_OFFSET) < order.index(MB_CMD)
    assert order.index(MB_LENGTH) < order.index(MB_CMD)


def test_error_status_is_decoded_not_swallowed():
    dut, ldr = make(fail_code=2)          # ERR_WIP_TIMEOUT
    ldr.start()
    with pytest.raises(LoaderError, match="flash WIP timeout"):
        ldr.erase(0x100000, 4096)


def test_command_timeout_raises():
    dut, ldr = make()
    ldr.start()
    # writes land, but the DUT state machine never runs -> status stays BUSY
    dut.write_mem = lambda addr, data: dut.mem.__setitem__(addr, data)
    with pytest.raises(LoaderError, match="did not complete"):
        ldr._command(CMD_ERASE, 0x100000, 4096, timeout_s=0.05)


# --- the whole job --------------------------------------------------------- #

def test_program_image_unlocks_erases_programs_and_crc_verifies():
    dut, ldr = make()
    ldr.start()
    image = bytes((i * 31 + 7) & 0xFF for i in range(1024))
    crc = ldr.program_image(image, base=0x100000)
    assert dut.unlocked
    assert bytes(dut.flash[0x100000:0x100000 + len(image)]) == image
    assert crc == binascii.crc32(image) & 0xFFFFFFFF


def test_program_image_chunks_larger_than_the_buffer():
    dut, ldr = make()
    ldr.start()
    image = bytes(range(256)) * (BUFFER_SIZE // 256 + 8)   # > one buffer
    ldr.program_image(image, base=0x100000)
    assert bytes(dut.flash[0x100000:0x100000 + len(image)]) == image
    assert sum(1 for o in dut.ops if o == f"load_image@{BUFFER_ADDR:#x}") >= 2


def test_verify_catches_a_corrupted_flash():
    """A CRC that disagrees must raise, not be reported as success."""
    dut, ldr = make()
    ldr.start()
    image = bytes(range(256))
    real = dut._run_command

    def corrupt(cmd):
        real(cmd)
        if cmd == CMD_PROGRAM:
            dut.flash[0x100000] ^= 0xFF      # flip a byte after programming
    dut._run_command = corrupt
    with pytest.raises(LoaderError, match="CRC mismatch"):
        ldr.program_image(image, base=0x100000)


def test_chunk_larger_than_buffer_is_refused():
    dut, ldr = make()
    ldr.start()
    with pytest.raises(ValueError, match="chunk must be"):
        ldr.program_chunk(0x100000, b"\x00" * (BUFFER_SIZE + 1))


def test_entry_batch_halts_before_writing_core_registers():
    """Core registers are only writable while halted, and every _run() is its
    own OpenOCD session — so the entry batch must halt ITSELF. Without this the
    board fails 'Could not write to register sp' (seen on silicon 2026-07-19)."""
    dut, ldr = make()
    ldr.start()
    batch = [o for o in dut.ops if o in ("halt", "resume") or o.startswith("reg ")]
    assert batch[0] == "halt", f"entry batch must start with halt, got {batch}"
    assert batch.index("halt") < batch.index(f"reg sp 0x{STACK_TOP:08x}")
    assert batch[-1] == "resume"
