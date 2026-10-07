"""test_micropython_flash_boot.py

TRUE product boot-from-flash proof for the hybrid-XiP MicroPython build — the
ASIC-honest boot, with NO RAM_PRELOAD cheat for the application.

IMEM starts EMPTY (deterministic all-zero). The reworked stage-0 bootrom probes
QSPI, brings up XiP, bursts the boot table into SRAM (cache-safe), CPU-copies
the HOT image out of flash (0x1000) into IMEM (0x10000000), CRC32-verifies it,
then REMAPs IMEM->0x0 and jumps. The hot MicroPython runs and the COLD half
(lexer/parser/compiler/REPL) executes IN PLACE from flash (0x20000).

GATE 1 (THE boot-from-flash proof): UART2 emits "NanoSoC MicroPython". Because
IMEM started empty, reaching the banner can ONLY mean stage-0 copied the hot
image out of flash — an FPGA/silicon-honest path a RAM_PRELOAD bench can never
prove.

GATE 2 (cold-execute proof, UPY_GATE2=1): feed print(1+1)\r\n into UART2 RXD and
look for 2 — the parser/compiler/REPL that produce it are COLD (in flash).

BOOT TELEMETRY: the captured UART2 stream carries stage-0's FlashLoader hand-off
diagnostics BEFORE the banner:
  I=<w0>,<w1>  IMEM[0..1] — the copied hot image's SP + reset PC. Non-zero
               (e.g. 1800xxxx,0000xxxx) PROVES the copy landed; 00000000 means
               the copy failed.
  V=<w0>,<w1>  address-0 (bootrom vectors) BEFORE remap
  M=<remap>    REMAP readback (expect 00000001)
  W=<w0>,<w1>  address-0 AFTER remap — must equal I=
The test extracts and prints these verbatim.

HONESTY: this boots a real interpreter AND adds a ~40 KB CPU word-copy from
flash through the cache — many simulated ms. If the banner is not seen, the test
reports EXACTLY how far it got (did TX ever move? how many bytes? what were
they? did the I=/M=/W= hand-off print, i.e. did stage-0 reach the copy+jump?).

Knobs:
  UPY_MAX_MS      GATE-1 sim-time budget in sim-ms (default 1500 — higher than
                  the RAM_PRELOAD bench to cover the flash->IMEM copy)
  UPY_PROGRESS_MS heartbeat interval in sim-ms (default 2)
  UPY_GATE2       set 1 to attempt the print(1+1) round-trip
"""

import os
import re

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, Timer

CLK_NS = 20            # 50 MHz dut_clk
BAUDDIV = 651          # 50 MHz / 76800 baud
BIT_CYCLES = BAUDDIV

BANNER = b"NanoSoC MicroPython"

MAX_MS = float(os.environ.get("UPY_MAX_MS", "1500"))
PROGRESS_MS = float(os.environ.get("UPY_PROGRESS_MS", "2"))
DO_GATE2 = os.environ.get("UPY_GATE2", "0") == "1"


def _printable(b):
    return "".join((chr(c) if 32 <= c < 127 else ".") for c in b)


def _report_telemetry(dut, raw):
    """Surface stage-0's I=/V=/M=/W= hand-off telemetry from the UART2 stream."""
    txt = _printable(raw)
    found = False
    for key in ("I=", "V=", "M=", "W="):
        m = re.search(re.escape(key) + r"([0-9A-Fa-f,]+)", txt)
        if m:
            found = True
            dut._log.info(f"[telemetry] {key}{m.group(1)}")
    if not found:
        dut._log.info("[telemetry] no I=/M=/W= hand-off markers decoded yet "
                      "(stage-0 may not have reached the REMAP+jump)")
    return found


# =============================================================================
# UART 8N1 receiver on the uart_txd pad — LSB first, sample mid-bit.
# =============================================================================
class UartRx:
    def __init__(self, dut, signal):
        self.dut = dut
        self.sig = signal
        self.bytes = bytearray()
        self.first_edge_ns = None

    async def run(self):
        dut = self.dut
        while True:
            await FallingEdge(self.sig)
            if self.first_edge_ns is None:
                self.first_edge_ns = cocotb.utils.get_sim_time(unit="ns")
                dut._log.info(
                    f"[uart] FIRST TX pad transition at {self.first_edge_ns/1000:.2f} us "
                    "-- the CPU booted, ran stage-0, and started transmitting."
                )
            await ClockCycles(dut.dut_clk, BIT_CYCLES // 2)
            if int(self.sig.value) != 0:
                continue
            val = 0
            for i in range(8):
                await ClockCycles(dut.dut_clk, BIT_CYCLES)
                val |= (int(self.sig.value) & 1) << i
            await ClockCycles(dut.dut_clk, BIT_CYCLES)
            self.bytes.append(val)
            t_us = cocotb.utils.get_sim_time(unit="ns") / 1000.0
            dut._log.info(
                f"[uart] byte {len(self.bytes):4d} @ {t_us:9.2f} us : "
                f"0x{val:02x} '{_printable(bytes([val]))}'"
            )


async def uart_tx_byte(dut, byte):
    dut.dut_rxd.value = 0
    await ClockCycles(dut.dut_clk, BIT_CYCLES)
    for i in range(8):
        dut.dut_rxd.value = (byte >> i) & 1
        await ClockCycles(dut.dut_clk, BIT_CYCLES)
    dut.dut_rxd.value = 1
    await ClockCycles(dut.dut_clk, BIT_CYCLES)


async def uart_tx_char_echo_paced(dut, rx, ch):
    """Send one char, then wait for the REPL to ECHO it before returning.

    The NanoSoC UART2 RX is a single-byte register with NO FIFO, and the cold
    REPL readline path (in flash) can take a while to service the first chars of
    a line. Sending the command back-to-back overruns RX and silently drops
    bytes (observed: 'print' -> 'p' with 'rint' lost during the first-char cold
    readline init). Gating each char on its echo is proper software flow control
    and removes the race regardless of cold-path timing.
    """
    mark = len(rx.bytes)
    await uart_tx_byte(dut, ch)
    # Wait (bounded) for the echo of this char to come back on TX.
    for _ in range(300):
        if len(rx.bytes) > mark:
            return
        await ClockCycles(dut.dut_clk, BIT_CYCLES * 2)
    # No echo seen — proceed anyway (e.g. CR/LF may not echo 1:1).


# =============================================================================
# GATE 1 — boot to the MicroPython banner FROM AN EMPTY IMEM (flash-copy proof).
# =============================================================================
@cocotb.test()
async def test_boots_from_flash_to_banner(dut):
    cocotb.start_soon(Clock(dut.dut_clk, CLK_NS, unit="ns").start())

    dut.dut_rxd.value = 1
    dut.dut_resetn.value = 0
    await ClockCycles(dut.dut_clk, 64)
    dut.dut_resetn.value = 1
    dut._log.info("[boot] reset released; IMEM is EMPTY — stage-0 must copy the "
                  "hot image out of flash before anything can run")

    rx = UartRx(dut, dut.uart_txd)
    cocotb.start_soon(rx.run())

    budget_ns = MAX_MS * 1_000_000
    heartbeat_ns = PROGRESS_MS * 1_000_000
    elapsed = 0
    found = False
    while elapsed < budget_ns:
        await Timer(heartbeat_ns, unit="ns")
        elapsed += heartbeat_ns
        if BANNER in bytes(rx.bytes):
            found = True
            break
        t_ms = cocotb.utils.get_sim_time(unit="ns") / 1_000_000.0
        n = len(rx.bytes)
        tail = _printable(bytes(rx.bytes[-40:]))
        dut._log.info(f"[hb] t={t_ms:7.3f} ms  bytes={n:4d}  tail='{tail}'")

    raw = bytes(rx.bytes)
    dut._log.info("=" * 72)
    dut._log.info(f"[result] total UART2 bytes received: {len(raw)}")
    dut._log.info(f"[result] first TX transition: "
                  f"{'none' if rx.first_edge_ns is None else f'{rx.first_edge_ns/1000:.2f} us'}")
    if raw:
        dut._log.info(f"[result] verbatim ASCII:\n{_printable(raw)}")
        dut._log.info(f"[result] verbatim hex:\n{raw.hex()}")
    _report_telemetry(dut, raw)
    dut._log.info("=" * 72)

    if not found:
        if rx.first_edge_ns is None:
            where = ("the UART2 TX pad NEVER MOVED: the CPU did not reach the "
                     "point of driving the console. stage-0 never got past its "
                     "early boot (reset/bootrom fetch/QSPI probe).")
        elif len(raw) == 0:
            where = (f"the TX pad moved but no 8N1 frame decoded at BAUDDIV="
                     f"{BAUDDIV} -- a baud/clock mismatch, not a boot fail.")
        else:
            saw_handoff = any(k in _printable(raw) for k in ("I=", "M=", "W="))
            if saw_handoff:
                where = ("stage-0 REACHED the REMAP+jump (I=/M=/W= telemetry "
                         "present) but the MicroPython banner did not appear in "
                         "the window. If I= is non-zero the flash->IMEM copy "
                         "worked and this is 'copied+jumped, interpreter still "
                         "initialising' -- raise UPY_MAX_MS. If I=00000000 the "
                         "copy FAILED.")
            else:
                where = ("bytes ARE arriving but no I=/M=/W= hand-off yet: "
                         "stage-0 is mid-boot (probing/copying/CRC) and had not "
                         "reached REMAP+jump within the budget. Raise UPY_MAX_MS.")
        assert False, (
            f"GATE 1 not reached in {MAX_MS} ms of sim: {where}\n"
            f"Received {len(raw)} bytes: {_printable(raw)!r}"
        )

    t_ms = cocotb.utils.get_sim_time(unit="ns") / 1_000_000.0
    idx = raw.find(BANNER)
    dut._log.info(
        f"[GATE 1 PASS] 'NanoSoC MicroPython' banner observed at t={t_ms:.3f} ms, "
        f"byte offset {idx}. IMEM started EMPTY, so stage-0 copied the hot image "
        "out of flash, CRC-verified it, REMAPped and jumped -- boot-from-flash "
        "PROVEN on the real nanosoc RTL."
    )

    if not DO_GATE2:
        dut._log.info("[GATE 2] skipped (set UPY_GATE2=1 to attempt the RX round-trip)")
        return

    dut._log.info("[GATE 2] waiting for the '>>> ' prompt, then sending print(1+1)")
    for _ in range(400):
        await Timer(PROGRESS_MS * 1_000_000, unit="ns")
        if b">>>" in bytes(rx.bytes):
            break

    for ch in b"print(1+1)\r\n":
        await uart_tx_char_echo_paced(dut, rx, ch)

    mark = len(rx.bytes)
    got2 = False
    for _ in range(400):
        await Timer(PROGRESS_MS * 1_000_000, unit="ns")
        after = bytes(rx.bytes[mark:])
        if b"\n2" in after or b"\r2" in after or after.strip().endswith(b"2"):
            got2 = True
            break

    after = bytes(rx.bytes[mark:])
    dut._log.info(f"[GATE 2] post-command bytes: {_printable(after)!r}")
    assert got2, (
        "GATE 2: sent 'print(1+1)' but did not observe '2' in the reply. "
        f"Post-command bytes: {_printable(after)!r}"
    )
    dut._log.info("[GATE 2 PASS] REPL evaluated print(1+1) and returned 2 -- "
                  "cold code executed IN PLACE from flash.")
