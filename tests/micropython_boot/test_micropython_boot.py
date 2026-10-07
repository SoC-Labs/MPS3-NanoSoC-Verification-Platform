"""test_micropython_boot.py

Full-SoC RTL boot proof for the NanoSoC MicroPython image.

This boots the REAL `nanosoc` core (Cortex-M0 DesignStart + the whole SoC
fabric, IMEM preloaded with the built MicroPython word-hex) from its genuine
stage-0 BOOTROM, and watches UART2's TXD pad with an 8N1 receiver.

GATE 1 (the proof): the bytes coming off UART2 contain "MicroPython". That
string is printed by pyexec_friendly_repl() the moment the interpreter has
initialised (heap up, mp_init done) and reached the REPL — i.e. the demo's
firmware actually runs on the real RTL, not just links.

GATE 2 (bonus): feed `print(1+1)\r\n` into UART2 RXD and look for `2` echoed
back, proving the full REPL round-trip.

HONESTY: booting a real interpreter is MANY simulated milliseconds. This is
slow. The test streams every received byte to the log with its sim-time, and
if the banner is not seen within the budget it reports EXACTLY how far it got
(did the TX pad ever move? how many bytes? what were they?) rather than
pretending success or declaring a bare failure.

Knobs (env vars):
  UPY_MAX_MS      hard sim-time budget for GATE 1, in sim-ms (default 800)
  UPY_PROGRESS_MS progress heartbeat interval, in sim-ms  (default 2)
  UPY_GATE2       set to 1 to attempt the RX round-trip after the banner
"""

import os

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, Timer, First, Edge

# --- Timing ------------------------------------------------------------------
CLK_NS = 20            # 50 MHz dut_clk -> 20 ns period
BAUDDIV = 651          # firmware NANOSOC_UART2_BAUDDIV: 50 MHz / 76800 baud
BIT_CYCLES = BAUDDIV   # one UART bit == BAUDDIV dut_clk cycles exactly

BANNER = b"NanoSoC MicroPython"   # the port's custom boot banner (mpconfigport.h)

MAX_MS = float(os.environ.get("UPY_MAX_MS", "800"))
PROGRESS_MS = float(os.environ.get("UPY_PROGRESS_MS", "2"))
DO_GATE2 = os.environ.get("UPY_GATE2", "0") == "1"


def _printable(b):
    return "".join((chr(c) if 32 <= c < 127 else ".") for c in b)


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
            # Hunt for a start bit: line idles high, falls to 0.
            await FallingEdge(self.sig)
            if self.first_edge_ns is None:
                self.first_edge_ns = cocotb.utils.get_sim_time(unit="ns")
                dut._log.info(
                    f"[uart] FIRST TX pad transition at {self.first_edge_ns/1000:.2f} us "
                    "-- the CPU booted, ran stage-0, and started transmitting."
                )
            # Move to the centre of the start bit and confirm it is really low
            # (reject a glitch), then step bit-by-bit through the 8 data bits.
            await ClockCycles(dut.dut_clk, BIT_CYCLES // 2)
            if int(self.sig.value) != 0:
                continue  # not a genuine start bit; resync
            val = 0
            for i in range(8):
                await ClockCycles(dut.dut_clk, BIT_CYCLES)
                val |= (int(self.sig.value) & 1) << i
            # Stop bit (advance past it so the next FallingEdge is a new frame).
            await ClockCycles(dut.dut_clk, BIT_CYCLES)
            self.bytes.append(val)
            t_us = cocotb.utils.get_sim_time(unit="ns") / 1000.0
            dut._log.info(
                f"[uart] byte {len(self.bytes):4d} @ {t_us:9.2f} us : "
                f"0x{val:02x} '{_printable(bytes([val]))}'"
            )


async def uart_tx_byte(dut, byte):
    """Shift one 8N1 frame into UART2 RXD (dut_rxd pad), LSB first."""
    dut.dut_rxd.value = 0  # start bit
    await ClockCycles(dut.dut_clk, BIT_CYCLES)
    for i in range(8):
        dut.dut_rxd.value = (byte >> i) & 1
        await ClockCycles(dut.dut_clk, BIT_CYCLES)
    dut.dut_rxd.value = 1  # stop bit
    await ClockCycles(dut.dut_clk, BIT_CYCLES)


# =============================================================================
# GATE 1 — boot to the MicroPython banner.
# =============================================================================
@cocotb.test()
async def test_boots_to_micropython_banner(dut):
    cocotb.start_soon(Clock(dut.dut_clk, CLK_NS, unit="ns").start())

    dut.dut_rxd.value = 1  # UART idle-high
    dut.dut_resetn.value = 0
    await ClockCycles(dut.dut_clk, 64)
    dut.dut_resetn.value = 1
    dut._log.info("[boot] reset released; CPU should now fetch from the BOOTROM at 0x0")

    rx = UartRx(dut, dut.uart_txd)
    cocotb.start_soon(rx.run())

    # Wait for the banner or the budget, whichever comes first. Heartbeat the
    # progress so a long run is observably alive rather than apparently hung.
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
    dut._log.info("=" * 72)

    if not found:
        # Report the exact stage reached rather than a bare failure.
        if rx.first_edge_ns is None:
            where = ("the UART2 TX pad NEVER MOVED within the budget: the CPU "
                     "did not reach the point of driving the console. Check that "
                     "reset released and the BOOTROM is fetching.")
        elif len(raw) == 0:
            where = ("the TX pad moved but no full 8N1 frame decoded at "
                     f"BAUDDIV={BAUDDIV} -- a baud/clock mismatch, not a boot fail.")
        else:
            where = ("bytes ARE arriving on UART2 but the MicroPython banner did "
                     "not appear within the budget. This is 'built, links, correct "
                     "image, boots and transmits, but banner not seen in the sim "
                     "window' -- NOT 'REPL proven'. Raise UPY_MAX_MS to run longer.")
        assert False, (
            f"GATE 1 not reached in {MAX_MS} ms of sim: {where}\n"
            f"Received {len(raw)} bytes: {_printable(raw)!r}"
        )

    t_ms = cocotb.utils.get_sim_time(unit="ns") / 1_000_000.0
    idx = raw.find(BANNER)
    dut._log.info(
        f"[GATE 1 PASS] 'MicroPython' banner observed at t={t_ms:.3f} ms, "
        f"byte offset {idx}. The interpreter initialised and reached the REPL "
        "on the real nanosoc RTL."
    )

    # -------------------------------------------------------------------------
    # GATE 2 (bonus) — REPL round-trip. Only attempted if asked, since it costs
    # further sim time. We wait for the '>>> ' prompt, type an expression, and
    # look for its result echoed back.
    # -------------------------------------------------------------------------
    if not DO_GATE2:
        dut._log.info("[GATE 2] skipped (set UPY_GATE2=1 to attempt the RX round-trip)")
        return

    dut._log.info("[GATE 2] waiting for the '>>> ' prompt, then sending print(1+1)")
    # Give the prompt a moment to flush after the banner.
    mark = len(rx.bytes)
    for _ in range(400):
        await Timer(PROGRESS_MS * 1_000_000, unit="ns")
        if b">>>" in bytes(rx.bytes):
            break

    for ch in b"print(1+1)\r\n":
        await uart_tx_byte(dut, ch)

    # Wait for output after our command and look for the '2' result line.
    mark = len(rx.bytes)
    got2 = False
    for _ in range(400):
        await Timer(PROGRESS_MS * 1_000_000, unit="ns")
        after = bytes(rx.bytes[mark:])
        # The echoed command contains digits too; require a bare '2' on its own
        # line (\r\n2\r\n or 2 followed by CR/LF) after the echo.
        if b"\n2" in after or b"\r2" in after or after.strip().endswith(b"2"):
            got2 = True
            break

    after = bytes(rx.bytes[mark:])
    dut._log.info(f"[GATE 2] post-command bytes: {_printable(after)!r}")
    assert got2, (
        "GATE 2: sent 'print(1+1)' but did not observe '2' in the reply. "
        f"Post-command bytes: {_printable(after)!r}"
    )
    dut._log.info("[GATE 2 PASS] REPL evaluated print(1+1) and returned 2 -- full round-trip.")
