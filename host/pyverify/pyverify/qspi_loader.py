"""Host driver for the M0-side QSPI flash loader (``firmware/qspi_loader``).

WHY
    Programming flash directly from the host costs ~18 SWD register round trips
    per 4 bytes. Measured on silicon 2026-07-18: 256 B took 167 s (~1.5 B/s), so
    the 160 KB MicroPython image would be ~29 HOURS. A bulk block move over the
    same link runs at ~180 B/s. So we stop moving flash data a register at a
    time: bulk-load a chunk into IMEM, and let the M0 drive the controller
    locally where a register write costs nanoseconds instead of 75 ms.

    Expected: ~15 min for 160 KB, dominated by the SWD upload.

SHAPE
    ``start()`` puts the loader in IMEM and enters it; then ``unlock`` /
    ``erase`` / ``program`` / ``crc32`` are mailbox round trips (a handful of
    SWD ops each, regardless of payload size). ``program_image()`` chains them.

    Verification uses **CMD_CRC — computed on the DUT**. Reading a 160 KB image
    back over SWD to compare would itself take hours; a DUT-side CRC is a few
    ops.

STATUS
    PROVEN on silicon (2026-07-19..21; commits b63da06, fce26bb):
    enter/CRC/erase/program/verify all pass, and the full 160 KB MicroPython
    image programmed + DUT-side-CRC-verified in ~2m41s. First bring-up flushed
    out three DUT-ONLY silent-failure bugs the mirrored host model could not (the
    two SPI_CMD writes each need a read-back to land; BUSY must be waited for to
    ASSERT before clear, because the M0 polls faster than the SST26 completes;
    and XIP_ACTIVE must be cleared at entry after a flash boot). Those fixes live
    in firmware/qspi_loader.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

# --- mailbox contract — MUST match firmware/qspi_loader/qspi_loader.c -------- #
#: Fixed so the host needs no ELF parsing. Keep in step with the C file.
LOADER_LOAD_ADDR = 0x1000_0000
MAILBOX_ADDR = 0x1001_0000
BUFFER_ADDR = 0x1001_0040
BUFFER_SIZE = 0x0000_C000          # 48 KB payload window
STACK_TOP = 0x1002_0000            # IMEM top; stack grows down

MB_MAGIC = MAILBOX_ADDR + 0x00
MB_CMD = MAILBOX_ADDR + 0x04
MB_OFFSET = MAILBOX_ADDR + 0x08
MB_LENGTH = MAILBOX_ADDR + 0x0C
MB_STATUS = MAILBOX_ADDR + 0x10
MB_CRC = MAILBOX_ADDR + 0x14
MB_OPS = MAILBOX_ADDR + 0x18

MAGIC_VALUE = 0x5146_4C44          # "QFLD"

CMD_NONE, CMD_UNLOCK, CMD_ERASE, CMD_PROGRAM, CMD_CRC = 0, 1, 2, 3, 4
ST_IDLE, ST_BUSY, ST_OK = 0, 1, 2
ST_ERR = 0x8000_0000
_ERR_NAMES = {1: "controller BUSY timeout", 2: "flash WIP timeout",
              3: "bad length", 4: "bad command"}

# --- Cortex-M registers used to quiesce the previous image ------------------ #
# swd.load_image's landmine: a fresh image entered without a reset can wedge in
# Default_Handler because the OLD image's SysTick/NVIC state is still armed, and
# ARMv6-M cannot clear an ACTIVE exception. Reset-halt is NOT reliable on this
# DUT (the DP shares the core reset domain), so we disable the sources instead.
SYSTICK_CTRL = 0xE000_E010
NVIC_ICER0 = 0xE000_E180
NVIC_ICPR0 = 0xE000_E280


class LoaderError(RuntimeError):
    """The loader did not come up, or a mailbox command failed/timed out."""


def _decode_status(status: int) -> str:
    if status & ST_ERR:
        code = status & ~ST_ERR
        return f"ERR {code} ({_ERR_NAMES.get(code, 'unknown')})"
    return {ST_IDLE: "IDLE", ST_BUSY: "BUSY", ST_OK: "OK"}.get(status, hex(status))


class M0FlashLoader:
    """Drive ``firmware/qspi_loader`` over an :class:`~pyverify.swd.SwdDebugger`.

    ``stage`` puts bytes on the machine OpenOCD runs on (the hub) and returns a
    path there — ``load_image`` hands its path straight to OpenOCD, so a local
    path would not resolve. Injected so tests can run board-free.
    """

    def __init__(
        self,
        swd,
        stage: Callable[[bytes, str], str],
        *,
        loader_bin: Optional[bytes] = None,
        poll_interval_s: float = 0.2,
        command_timeout_s: float = 600.0,
    ) -> None:
        self.swd = swd
        self.stage = stage
        self.loader_bin = loader_bin
        self.poll_interval_s = poll_interval_s
        #: Default per-command deadline. Generous in production (a full-image
        #: erase is slow); tests set it low so an ordering regression FAILS
        #: rather than parking the suite on a 10-minute timeout.
        self.command_timeout_s = command_timeout_s

    # -- bring-up ----------------------------------------------------------- #
    def start(self, *, timeout_s: float = 30.0) -> None:
        """Halt the M0, quiesce the previous image, enter the loader, confirm live."""
        if self.loader_bin is None:
            raise LoaderError("no loader binary supplied (build firmware/qspi_loader)")

        self.swd.halt()
        # Disable and clear pending interrupts BEFORE entering: see the module
        # note on the Default_Handler wedge.
        self.swd.write_mem(SYSTICK_CTRL, 0)
        self.swd.write_mem(NVIC_ICER0, 0xFFFF_FFFF)
        self.swd.write_mem(NVIC_ICPR0, 0xFFFF_FFFF)

        path = self.stage(self.loader_bin, "qspi_loader.bin")
        self.swd.load_image(path, LOADER_LOAD_ADDR, halt=True)

        # Zero the magic so a STALE loader from a previous run cannot look live.
        self.swd.write_mem(MB_MAGIC, 0)
        # "halt" MUST lead this batch. Core registers are only writable while
        # halted, and every _run() is its OWN OpenOCD session -- the halt() above
        # does not carry over, so without this the core is running again by now
        # and OpenOCD fails "Could not write to register 'sp'". Observed on
        # silicon 2026-07-19.
        self.swd._run(
            ("halt",
             f"reg sp 0x{STACK_TOP:08x}",
             f"reg pc 0x{LOADER_LOAD_ADDR:08x}",
             "resume"),
            with_target=True,
        )

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.swd.read_mem(MB_MAGIC, 1)[0] == MAGIC_VALUE:
                return
            time.sleep(self.poll_interval_s)
        raise LoaderError(
            f"loader did not report magic {MAGIC_VALUE:#x} at {MB_MAGIC:#x} within "
            f"{timeout_s}s — it may not have entered (check SP/PC) or wedged in a "
            f"stale exception handler."
        )

    # -- mailbox ------------------------------------------------------------ #
    def _command(self, cmd: int, offset: int = 0, length: int = 0,
                 *, timeout_s: Optional[float] = None) -> None:
        # offset/length BEFORE cmd: the DUT polls cmd and reads the rest once it
        # sees a non-zero value, so cmd must be written last.
        self.swd.write_mem(MB_OFFSET, offset)
        self.swd.write_mem(MB_LENGTH, length)
        self.swd.write_mem(MB_STATUS, ST_BUSY)
        self.swd.write_mem(MB_CMD, cmd)

        timeout_s = self.command_timeout_s if timeout_s is None else timeout_s
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            status = self.swd.read_mem(MB_STATUS, 1)[0]
            if status == ST_OK:
                return
            if status & ST_ERR:
                raise LoaderError(
                    f"loader command {cmd} (offset={offset:#x} len={length}) "
                    f"failed: {_decode_status(status)}"
                )
            time.sleep(self.poll_interval_s)
        raise LoaderError(
            f"loader command {cmd} (offset={offset:#x} len={length}) did not "
            f"complete within {timeout_s}s"
        )

    def unlock(self) -> None:
        """WREN+ULBPR. The SST26 boots write-protected; without this every
        erase/program is a silent no-op."""
        self._command(CMD_UNLOCK)

    def erase(self, offset: int, length: int) -> None:
        self._command(CMD_ERASE, offset, length)

    def crc32(self, offset: int, length: int) -> int:
        """CRC32 of flash contents, computed ON THE DUT (no read-back)."""
        self._command(CMD_CRC, offset, length)
        return self.swd.read_mem(MB_CRC, 1)[0] & 0xFFFF_FFFF

    def program_chunk(self, offset: int, data: bytes) -> None:
        if not 0 < len(data) <= BUFFER_SIZE:
            raise ValueError(f"chunk must be 1..{BUFFER_SIZE} bytes, got {len(data)}")
        path = self.stage(data, f"payload_{offset:08x}.bin")
        self.swd.load_image(path, BUFFER_ADDR, halt=False)
        self._command(CMD_PROGRAM, offset, len(data))

    # -- the whole job ------------------------------------------------------ #
    def program_image(self, image: bytes, *, base: int = 0,
                      erase: bool = True, verify: bool = True) -> int:
        """Erase, program ``image`` at ``base`` in buffer-sized chunks, and
        (by default) verify with a DUT-side CRC. Returns the flash CRC32."""
        import binascii

        self.unlock()
        if erase:
            self.erase(base, len(image))
        for off in range(0, len(image), BUFFER_SIZE):
            self.program_chunk(base + off, image[off:off + BUFFER_SIZE])

        flash_crc = self.crc32(base, len(image))
        if verify:
            want = binascii.crc32(image) & 0xFFFF_FFFF
            if flash_crc != want:
                raise LoaderError(
                    f"CRC mismatch after programming {len(image)} B at {base:#x}: "
                    f"flash {flash_crc:#010x} != image {want:#010x}"
                )
        return flash_crc
